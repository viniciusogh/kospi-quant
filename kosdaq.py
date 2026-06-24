import requests
import pandas as pd
import numpy as np
import time
import random
import os
import json
import re
import _env  # .env 자동 로드
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
# 모멘텀 레포트와 동일한 토글/분기표·Gemini 형식 재사용 (signal-agnostic 헬퍼)
from momentum_daily import (fetch_income, fetch_ebitda, _quarter_table, _para,
                            _gemini_update, _trim_phrase, _is_clean)

KST = timezone(timedelta(hours=9))   # GitHub Actions 러너는 UTC, 모든 날짜·시각은 KST 기준

# ==========================
# 환경설정 (로컬 fallback + GitHub Secrets 겸용)
# ==========================
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

APP_KEY    = os.environ["APP_KEY"]
APP_SECRET = os.environ["APP_SECRET"]

input_csv = os.environ.get("INPUT_CSV",
    os.path.join(_BASE_DIR, "KOSDAQ재무데이터한투.csv"))

# Notion
NOTION_API_KEY        = os.environ.get("NOTION_API_KEY", "")   # 없으면 업로드 생략(로컬 dry-run)
NOTION_PARENT_PAGE_ID = os.environ.get("NOTION_PARENT_PAGE_ID", "3324a00632f880fbb014d766d87a1079")

# Gemini (종목 설명 생성용 — 키 없으면 설명 생략, 카드/표는 정상)
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# KRX (코스닥 포함, KIS API 시장코드 J 가 코스피·코스닥 모두 커버)
MRKT_CODE = "J"

# 병렬 설정
WORKERS_NETFLOW = 4
WORKERS_MKTCAP  = 5

# 추천 설정 (코스닥은 시총 상위 200개에서 30개 추천)
TOP_MKTCAP_N = 200
TOP_RECO_N   = 30

# 재무비율 캐시 (주간 갱신) - 코스닥 별도 캐시
FIN_RATIO_CACHE      = os.environ.get("KOSDAQ_FIN_RATIO_CACHE",
    os.path.join(_BASE_DIR, "kosdaq_fin_ratio_cache.csv"))
FIN_RATIO_CACHE_DAYS = 7

# 점수 시계열 평탄화 (단일 시점 노이즈 완화) - 코스닥 별도 이력
KOSDAQ_SCORE_HISTORY = os.path.join(_BASE_DIR, "kosdaq_score_history.csv")
EMA_SPAN            = 5     # 영업일 기준
HISTORY_RETAIN_D    = 60    # 이력 보관 기간 (일)

# 엑셀 출력 디렉토리 (로컬=Desktop, CI=스크립트 폴더)
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", os.path.expanduser("~/Desktop"))

# ==========================
# 로그 유틸
# ==========================
def _ts() -> str:
    return datetime.now(KST).strftime("%H:%M:%S")

def log(msg: str):
    print(f"[{_ts()}] {msg}")

# ==========================
# Access Token (필수 문구 포함)
# ==========================
def get_access_token(appkey: str, appsecret: str) -> str:
    """
    한국투자증권 API용 Access Token 자동 발급 함수.
    The access token is automatically issued and used without any manual copy-paste.
    """
    url = "https://openapi.koreainvestment.com:9443/oauth2/tokenP"
    headers = {"Content-Type": "application/json"}
    data = {"grant_type": "client_credentials", "appkey": appkey, "appsecret": appsecret}
    # 일시적 연결실패(ConnectTimeout) 대비 재시도+백오프 (2026-06-16: 6/16 토큰 타임아웃 전멸 대응)
    for attempt in range(1, 5):
        try:
            r = requests.post(url, headers=headers, json=data, timeout=15)
            r.raise_for_status()
            log("✅ Access Token 발급 성공")
            return r.json()["access_token"]
        except Exception as e:
            log(f"⚠️ Access Token 발급 실패 ({attempt}/4): {e}")
            if attempt < 4:
                time.sleep(min(5 * attempt, 20))
    log("❌ Access Token 발급 최종 실패 (4회)")
    return None

# ==========================
# 재시도 가능한 안전한 GET 요청
# ==========================
def safe_request_get(url, headers, params, max_retry=3, timeout=3):
    for attempt in range(1, max_retry + 1):
        try:
            r = requests.get(url, headers=headers, params=params, timeout=timeout)
            if r.status_code == 200:
                return r
            log(f"⚠️ API 오류 (status={r.status_code}), 재시도 {attempt}/{max_retry}")
        except Exception as e:
            log(f"⚠️ 요청 실패({attempt}/{max_retry}): {e}")
        time.sleep(0.4 * attempt)
    return None

def polite_sleep():
    time.sleep(random.uniform(0.25, 0.45))

# ==========================
# TS Z-score
# ==========================
def rolling_zscore(series: pd.Series, window: int = 15, min_periods: int = 8) -> pd.Series:
    m = series.rolling(window, min_periods=min_periods).mean()
    s = series.rolling(window, min_periods=min_periods).std()
    return (series - m) / (s + 1e-8)

# ==========================
# 30일 수급(inquire-investor) 조회
# ==========================
def get_netflow_history(code: str, access_token: str) -> pd.DataFrame:
    url = "https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/quotations/inquire-investor"
    headers = {
        "Content-Type": "application/json",
        "authorization": f"Bearer {access_token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "FHKST01010900",
    }
    params = {
        "FID_COND_MRKT_DIV_CODE": MRKT_CODE,
        "FID_INPUT_ISCD": code,
    }

    r = safe_request_get(url, headers, params, max_retry=3, timeout=3)
    polite_sleep()
    if r is None:
        return pd.DataFrame()

    try:
        data = r.json()
        if data.get("rt_cd") != "0":
            return pd.DataFrame()

        rows = data.get("output", [])
        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)[["stck_bsop_date", "frgn_ntby_tr_pbmn", "orgn_ntby_tr_pbmn"]].copy()
        df.rename(columns={"stck_bsop_date": "date"}, inplace=True)

        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df["frgn_ntby_tr_pbmn"] = pd.to_numeric(df["frgn_ntby_tr_pbmn"], errors="coerce")
        df["orgn_ntby_tr_pbmn"] = pd.to_numeric(df["orgn_ntby_tr_pbmn"], errors="coerce")

        df.dropna(subset=["date"], inplace=True)
        df.sort_values("date", inplace=True)
        df.reset_index(drop=True, inplace=True)

        df["netflow_pbmn"] = df["frgn_ntby_tr_pbmn"].fillna(0) + df["orgn_ntby_tr_pbmn"].fillna(0)

        # 혹시 중복일자 있으면 합산
        if df["date"].duplicated().any():
            df = (
                df.groupby("date", as_index=False)
                .agg(
                    {
                        "frgn_ntby_tr_pbmn": "sum",
                        "orgn_ntby_tr_pbmn": "sum",
                        "netflow_pbmn": "sum",
                    }
                )
                .sort_values("date")
                .reset_index(drop=True)
            )

        return df
    except Exception:
        return pd.DataFrame()

# ==========================
# 수급 강화 점수 계산(모델 없음)
# ==========================
def compute_strength_score(code: str, name: str, df_nf: pd.DataFrame):
    """
    반환: dict(최신일 기준 1행 요약)
    """
    if df_nf is None or df_nf.empty:
        return None

    df = df_nf.copy()
    # 최소 길이(10~15일은 필요)
    if len(df) < 15:
        return None

    # features
    df["nf_sum_5"] = df["netflow_pbmn"].rolling(5).sum()

    abs_nf = df["netflow_pbmn"].abs()
    # 급증 강도: 최근 3일 abs 평균 / 직전 10일 abs 평균(lookahead 방지 위해 shift)
    df["nf_surge_3_10"] = (abs_nf.rolling(3).mean() / (abs_nf.rolling(10).mean().shift(3) + 1e-8) - 1).clip(-5, 5)

    df["nf_pos_ratio_10"] = (df["netflow_pbmn"] > 0).astype(int).rolling(10).mean()

    # TS z
    df["nf_sum_5_internal_z"] = rolling_zscore(df["nf_sum_5"], window=15, min_periods=8)
    df["nf_surge_3_10_internal_z"] = rolling_zscore(df["nf_surge_3_10"], window=15, min_periods=8)
    df["nf_pos_ratio_10_internal_z"] = rolling_zscore(df["nf_pos_ratio_10"], window=15, min_periods=8)

    # 최신 유효 행
    latest = df.dropna(subset=[
        "nf_sum_5_internal_z",
        "nf_surge_3_10_internal_z",
        "nf_pos_ratio_10_internal_z"
    ])
    if latest.empty:
        return None

    last = latest.iloc[-1].copy()
    strength_score = float(
        last["nf_sum_5_internal_z"]
        + last["nf_surge_3_10_internal_z"]
        + last["nf_pos_ratio_10_internal_z"]
    )

    out = {
        "code": code,
        "name": name,
        "date": last["date"],
        "frgn_ntby_tr_pbmn": float(last.get("frgn_ntby_tr_pbmn", np.nan)),
        "orgn_ntby_tr_pbmn": float(last.get("orgn_ntby_tr_pbmn", np.nan)),
        "netflow_pbmn": float(last.get("netflow_pbmn", np.nan)),

        "nf_sum_5": float(last.get("nf_sum_5", np.nan)),
        "nf_surge_3_10": float(last.get("nf_surge_3_10", np.nan)),
        "nf_pos_ratio_10": float(last.get("nf_pos_ratio_10", np.nan)),

        "nf_sum_5_internal_z": float(last.get("nf_sum_5_internal_z", np.nan)),
        "nf_surge_3_10_internal_z": float(last.get("nf_surge_3_10_internal_z", np.nan)),
        "nf_pos_ratio_10_internal_z": float(last.get("nf_pos_ratio_10_internal_z", np.nan)),

        "strength_score": strength_score,
    }
    return out

# ==========================
# 시가총액 + PER + EPS 조회(inquire-price)
# ==========================
def get_valuation_info(code: str, access_token: str):
    """
    inquire-price (FHKST01010100) output에서
    - hts_avls: 시가총액
    - per: PER
    - eps: EPS
    - bstp_kor_isnm: 업종 한글명 (섹터 중립화·Notion 표시용)
    - prdy_ctrt: 전일 대비율(%) (Notion 표시용, 0% 도 유효값)
    """
    url = "https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/quotations/inquire-price"
    headers = {
        "authorization": f"Bearer {access_token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "FHKST01010100",
    }
    params = {
        "FID_COND_MRKT_DIV_CODE": MRKT_CODE,
        "FID_INPUT_ISCD": code,
    }

    r = safe_request_get(url, headers, params, max_retry=3, timeout=3)
    polite_sleep()
    if r is None:
        return None, None, None, None, None, None

    try:
        data = r.json().get("output", {})
        mktcap = int(data.get("hts_avls", 0))
        per = data.get("per")
        eps = data.get("eps")
        pbr = data.get("pbr")
        industry_raw = data.get("bstp_kor_isnm")
        industry = (str(industry_raw).strip()
                    if industry_raw and str(industry_raw).strip() not in ("", "0")
                    else None)

        # 숫자 변환 시도, 실패하면 None ("0" 도 None 처리: PER/PBR/EPS 에서는 결측 의미)
        def _safe(v):
            try:
                return float(v) if v not in (None, "", "0", "0.0") else None
            except Exception:
                return None

        # 등락률은 0 도 유효값 (가격 변동 없음)
        def _safe_pct(v):
            try:
                return float(v) if v not in (None, "") else None
            except Exception:
                return None

        per = _safe(per)
        eps = _safe(eps)
        pbr = _safe(pbr)
        prdy_ctrt = _safe_pct(data.get("prdy_ctrt"))

        return mktcap, per, eps, pbr, industry, prdy_ctrt
    except Exception:
        return None, None, None, None, None, None

# ==========================
# 일별 종가 조회 + 이동평균선 정배열 확인
# ==========================
def get_daily_prices(code: str, access_token: str, days: int = 160) -> list:
    """FHKST03010100: 일별 종가 리스트 반환 (오래된 순서)"""
    from datetime import timedelta
    url = "https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
    end_dt   = datetime.now(KST)
    start_dt = end_dt - timedelta(days=days)
    headers = {
        "authorization": f"Bearer {access_token}",
        "appkey": APP_KEY, "appsecret": APP_SECRET,
        "tr_id": "FHKST03010100",
    }
    params = {
        "FID_COND_MRKT_DIV_CODE": MRKT_CODE,
        "FID_INPUT_ISCD": code,
        "FID_INPUT_DATE_1": start_dt.strftime("%Y%m%d"),
        "FID_INPUT_DATE_2": end_dt.strftime("%Y%m%d"),
        "FID_PERIOD_DIV_CODE": "D",
        "FID_ORG_ADJ_PRC": "0",
    }
    r = safe_request_get(url, headers, params, max_retry=3, timeout=5)
    if r is None:
        return []
    try:
        output = r.json().get("output2", [])
        prices = []
        for row in output:
            try:
                p = float(row.get("stck_clpr", 0))
                if p > 0:
                    prices.append(p)
            except Exception:
                pass
        prices.reverse()   # 오래된 날짜 순서로
        return prices
    except Exception:
        return []


def is_jeong_baeyeol(prices: list) -> bool:
    """정배열 확인: 5MA > 20MA > 60MA
    (KIS API 최대 100개 반환 제한으로 120MA 제외)
    """
    if len(prices) < 60:
        return False
    ma5  = sum(prices[-5:])  / 5
    ma20 = sum(prices[-20:]) / 20
    ma60 = sum(prices[-60:]) / 60
    return ma5 > ma20 > ma60


# ==========================
# 재무비율 조회 (FHKST66430300)
# ==========================
def get_financial_ratio(code: str, access_token: str):
    """재무비율 API: ROE, 부채비율, 매출증가율, 영업이익증가율 (연간 최신)"""
    url = "https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/finance/financial-ratio"
    headers = {
        "authorization": f"Bearer {access_token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "FHKST66430300",
    }
    params = {
        "FID_COND_MRKT_DIV_CODE": MRKT_CODE,
        "FID_DIV_CLS_CODE": "0",   # 0=연간
        "FID_INPUT_ISCD": code,
    }

    r = safe_request_get(url, headers, params, max_retry=3, timeout=3)
    polite_sleep()
    if r is None:
        return None, None, None, None

    try:
        output = r.json().get("output", [])
        if not output:
            return None, None, None, None

        latest = output[0]   # 가장 최근 연간 데이터

        def _f(val):
            try:
                return float(val) if val not in (None, "", "0", "0.0") else None
            except Exception:
                return None

        return (
            _f(latest.get("roe_val")),
            _f(latest.get("lblt_rate")),
            _f(latest.get("grs")),
            _f(latest.get("bsop_prfi_inrt")),
        )
    except Exception:
        return None, None, None, None


# ==========================
# 재무비율 캐시 로드 or 전 종목 일괄 조회
# ==========================
def load_or_fetch_fin_ratios(codes: list, access_token: str) -> pd.DataFrame:
    """
    FIN_RATIO_CACHE 파일이 FIN_RATIO_CACHE_DAYS일 이내면 캐시 재사용,
    아니면 전 종목 재조회 후 저장.
    반환: DataFrame[code, roe, debt_ratio, rev_growth, op_profit_growth]
    """
    # 실적 시즌(1·2·4·5·7·8·10·11월)은 매일 갱신, 비시즌은 7일 캐시
    EARNINGS_MONTHS = {1, 2, 4, 5, 7, 8, 10, 11}
    cache_days = 1 if datetime.now(KST).month in EARNINGS_MONTHS else FIN_RATIO_CACHE_DAYS

    if os.path.exists(FIN_RATIO_CACHE):
        age_days = (time.time() - os.path.getmtime(FIN_RATIO_CACHE)) / 86400
        season = "실적시즌" if datetime.now(KST).month in EARNINGS_MONTHS else "비시즌"
        if age_days < cache_days:
            log(f"✅ 재무비율 캐시 재사용 ({season} / 나이: {age_days:.1f}일 / 만료: {cache_days}일)")
            return pd.read_csv(FIN_RATIO_CACHE, dtype={"code": str})
        log(f"▶ 재무비율 캐시 만료 ({season} / 나이: {age_days:.1f}일 → 갱신)")

    log(f"▶ 재무비율 조회 시작 ({len(codes)}개 종목, workers=5)")
    start = time.time()
    results = []

    def _worker(code):
        roe, debt_ratio, rev_growth, op_growth = get_financial_ratio(code, access_token)
        return {"code": code, "roe": roe, "debt_ratio": debt_ratio,
                "rev_growth": rev_growth, "op_profit_growth": op_growth}

    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = {ex.submit(_worker, c): i for i, c in enumerate(codes)}
        for cnt, future in enumerate(as_completed(futures), start=1):
            results.append(future.result())
            elapsed = time.time() - start
            speed = cnt / elapsed if elapsed > 0 else 0
            remaining = (len(codes) - cnt) / speed if speed > 0 else 0
            print(
                f"[재무 {cnt}/{len(codes)} | {cnt/len(codes)*100:5.2f}%] "
                f"경과 {elapsed:6.1f}s | 남은 {remaining:6.1f}s | 속도 {speed:.2f} 종목/s"
            )

    fin_df = pd.DataFrame(results)
    os.makedirs(os.path.dirname(FIN_RATIO_CACHE), exist_ok=True)
    fin_df.to_csv(FIN_RATIO_CACHE, index=False)
    log(f"✅ 재무비율 조회 완료 + 캐시 저장: {FIN_RATIO_CACHE}")
    return fin_df


# ==========================
# 멀티팩터 점수 계산 (단면 Z-score 가중합)
# ==========================
def _cross_z(s: pd.Series, clip: float = 3.0) -> pd.Series:
    """단면(Cross-sectional) Z-score + 클리핑 (±clip)"""
    z = (s - s.mean()) / (s.std() + 1e-8)
    return z.clip(-clip, clip)


def compute_slim3_score(df: pd.DataFrame) -> pd.Series:
    """slim3 = 이익수익률(1/PER) + ROE(±40윈저) + 저부채(-부채비율), 동일가중 단면 z합.

    백테 검증(2026-06, kosdaq_fundamental_backtest.py): 코스닥은 가격모멘텀이 IC 음수로 죽었지만
    이 셋(ey·roe·lowdebt)은 정상 레짐 롱숏 +3~5%p/반기로 유효. 수급·성장·PBR은 노이즈/부호반전이라 제외.
    데이터 없으면 해당 팩터 z=0(중립). ROE 윈저는 자본잠식 종목의 z 폭주 차단(라이브 전용).
    """
    def _ey(x):   # 1/PER, 부호 그대로 (= eps/px). 적자(PER<0)면 자연히 음수. 0/결측만 NaN.
        return (1.0 / x) if (pd.notna(x) and x != 0) else np.nan
    z_ey      = _cross_z(df["per"].apply(_ey)).fillna(0)
    roe_w     = pd.to_numeric(df["roe"], errors="coerce").clip(-40, 40)
    z_roe     = _cross_z(roe_w).fillna(0)
    z_lowdebt = _cross_z(-pd.to_numeric(df["debt_ratio"], errors="coerce")).fillna(0)

    df["f_ey"], df["f_roe"], df["f_lowdebt"] = z_ey, z_roe, z_lowdebt
    return z_ey + z_roe + z_lowdebt


# ==========================
# 점수 EMA 평탄화 + 순위 변동 (US 모듈과 동일 패턴, 종목코드 기준)
# ==========================
def load_score_history() -> pd.DataFrame:
    if not os.path.exists(KOSDAQ_SCORE_HISTORY):
        return pd.DataFrame(columns=["date", "code", "raw_score"])
    return pd.read_csv(KOSDAQ_SCORE_HISTORY, parse_dates=["date"], dtype={"code": str})

def smooth_with_ema(universe: pd.DataFrame, today: datetime) -> tuple[pd.Series, pd.DataFrame]:
    """오늘 raw_score를 history에 누적하고, 종목코드별 EMA 점수를 반환.

    첫 실행 시 history가 비어 있어도 EMA = raw_score 로 자연 폴백.
    같은 날 재실행해도 안전 (today 행 덮어씀).
    """
    history = load_score_history()
    today_ts = pd.Timestamp(today.date())

    today_df = pd.DataFrame({
        "date":      today_ts,
        "code":      universe["code"].values,
        "raw_score": universe["raw_score"].values,
    })

    history = history[history["date"] != today_ts]
    history = (today_df if history.empty
               else pd.concat([history, today_df], ignore_index=True))
    history = history.sort_values(["code", "date"])

    history["smoothed"] = history.groupby("code")["raw_score"].transform(
        lambda x: x.ewm(span=EMA_SPAN, adjust=False).mean()
    )

    cutoff  = today_ts - pd.Timedelta(days=HISTORY_RETAIN_D)
    history = history[history["date"] >= cutoff]

    smoothed_today = (history[history["date"] == today_ts]
                      .set_index("code")["smoothed"])
    return smoothed_today, history

def compute_rank_change(history: pd.DataFrame) -> dict:
    """종목코드별 (어제 순위 - 오늘 순위). + = 상승, None = 신규."""
    if history["date"].nunique() < 2:
        return {}

    dates_sorted = sorted(history["date"].unique())
    today_d, prev_d = dates_sorted[-1], dates_sorted[-2]

    def rank_map(d):
        rows = history[history["date"] == d].sort_values("smoothed", ascending=False)
        return {c: i + 1 for i, c in enumerate(rows["code"].tolist())}

    today_r = rank_map(today_d)
    prev_r  = rank_map(prev_d)
    return {c: (prev_r.get(c) - r if c in prev_r else None)
            for c, r in today_r.items()}

def fmt_rank_change(change) -> str:
    if change is None or pd.isna(change):
        return "🆕"
    if change == 0:
        return "—"
    return f"📈+{int(change)}" if change > 0 else f"📉{int(change)}"

def fmt_pct(v) -> str:
    """전일 대비 가격 등락률(%) 포맷. 0/None/NaN 처리."""
    if v is None or pd.isna(v):
        return "-"
    sign = "+" if v > 0 else ""
    return f"{sign}{float(v):.2f}%"


# ==========================
# 한국어 컬럼명(최종 출력용)
# ==========================
def to_korean_columns(df: pd.DataFrame) -> pd.DataFrame:
    col_map = {
        "code": "종목코드",
        "name": "종목명",
        "date": "기준일",

        "frgn_ntby_tr_pbmn": "외국인_순매수대금(백만원)",
        "orgn_ntby_tr_pbmn": "기관_순매수대금(백만원)",
        "netflow_pbmn": "외국인+기관_순매수대금(백만원)",

        "nf_sum_5": "수급합(5일)",
        "nf_surge_3_10": "수급급증(3vs10_강도)",
        "nf_pos_ratio_10": "매수우위비율(10일)",

        "nf_sum_5_internal_z": "수급합(5일)_TS_Z",
        "nf_surge_3_10_internal_z": "수급급증(3vs10)_TS_Z",
        "nf_pos_ratio_10_internal_z": "매수우위비율(10일)_TS_Z",

        "market_cap": "시가총액",
        "strength_score": "수급강화점수",
        "rank": "랭킹",

        # 추가: PER, EPS, 재무지표, 멀티팩터
        "per": "PER",
        "eps": "EPS",
        "pbr": "PBR",
        "roe": "ROE(%)",
        "debt_ratio": "부채비율(%)",
        "rev_growth": "매출증가율(%)",
        "op_profit_growth": "영업이익증가율(%)",
        "multi_score": "멀티팩터점수",
        "jeong_baeyeol": "정배열",
        "industry": "섹터",
        "rank_change": "순위변동",
        "prdy_ctrt": "당일등락(%)",
    }
    return df.rename(columns={c: col_map.get(c, c) for c in df.columns})

# ==========================
# Notion 업로드
# ==========================
def _get_or_create_date_page(date_str: str, headers: dict, root_parent_id: str) -> str:
    """노션 database 의 today row 찾거나 생성. row ID = 그 날짜 페이지 ID.
    NOTION_DAILY_DB_ID 환경변수 = database ID."""
    import os
    db_id = os.environ.get("NOTION_DAILY_DB_ID", "")
    if not db_id:
        # fallback (옛 페이지 구조) - 이 파일이 root_parent_id 아래 child_page 검색
        try:
            r = requests.post(
                "https://api.notion.com/v1/databases/" + root_parent_id + "/query",
                headers=headers, timeout=15
            )
        except Exception:
            pass
        raise RuntimeError("NOTION_DAILY_DB_ID 가 설정되지 않음")

    # database 에서 today row 찾기
    r = requests.post(
        f"https://api.notion.com/v1/databases/{db_id}/query",
        headers=headers,
        json={"filter": {"property": "날짜", "date": {"equals": date_str}}, "page_size": 1},
        timeout=15,
    )
    if r.status_code == 200:
        results = r.json().get("results", [])
        if results:
            return results[0]["id"]

    # 없으면 새 row 생성
    body = {
        "parent": {"database_id": db_id},
        "properties": {
            "이름": {"title": [{"type": "text", "text": {"content": "준비 중"}}]},
            "날짜": {"date": {"start": date_str}},
        },
    }
    r = requests.post("https://api.notion.com/v1/pages", headers=headers, json=body, timeout=15)
    r.raise_for_status()
    return r.json()["id"]


def _archive_same_title_pages(title: str, headers: dict, parent_id: str):
    """부모(날짜) 페이지의 자식 목록을 직접 조회해 동일 제목 child_page 를 archive — 중복 방지.
    /v1/search 는 인덱싱 지연으로 연속 실행 시 누락 → children 직접 조회(즉시 일관성)로 변경(2026-06)."""
    try:
        cursor = None
        while True:
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            r = requests.get(
                f"https://api.notion.com/v1/blocks/{parent_id}/children",
                headers=headers, params=params, timeout=15,
            )
            if r.status_code != 200:
                return
            data = r.json()
            for blk in data.get("results", []):
                if blk.get("type") != "child_page":
                    continue
                actual = blk.get("child_page", {}).get("title", "")
                if actual.strip() == title.strip() and not blk.get("archived", False):
                    requests.patch(
                        f"https://api.notion.com/v1/pages/{blk['id']}",
                        headers=headers, json={"archived": True}, timeout=15,
                    )
                    log(f"  기존 동일 제목 페이지 archive: {blk['id']}")
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
    except Exception as e:
        log(f"  기존 페이지 확인 중 오류 (무시): {e}")


# ==========================
# Gemini 펀더멘탈 심층분석 (모멘텀 레포트와 동일 형식, 신호만 펀더로)
# ==========================
KOSDAQ_ANALYSIS_CACHE = os.path.join(_BASE_DIR, "kosdaq_analysis.json")
FUND_SECTIONS = [("💡 요약", "요약"), ("📊 사업·실적", "사업실적"), ("⚡ 상승 촉매", "촉매"),
                 ("💵 밸류·재무", "밸류"), ("📈 강세론", "강세론"), ("⚠️ 리스크", "리스크"),
                 ("👀 관전 포인트", "관전")]
FUND_KEYS = ["issue", "요약", "사업실적", "촉매", "밸류", "강세론", "리스크", "관전"]


def kosdaq_debt_ranks(all_df: pd.DataFrame) -> dict:
    """all_df(섹터·부채비율) → code별 동일섹터 내 부채 순위(1=최저부채). 절대수치 판정 없이 상대위치."""
    d = all_df.dropna(subset=["debt_ratio", "industry"]).copy()
    out = {}
    for sec, g in d.groupby("industry"):
        nn = len(g)
        if nn < 4:
            continue
        g = g.assign(rk=g["debt_ratio"].rank(method="min"))   # 1 = 섹터 내 최저 부채
        for _, x in g.iterrows():
            pos = x["rk"] / nn
            band = "저부채 그룹" if pos <= 1/3 else ("고부채 그룹" if pos > 2/3 else "중간")
            out[x["code"]] = {"debt": float(x["debt_ratio"]), "rank": int(x["rk"]), "n": nn, "band": band}
    return out


def _gemini_full_fund(c, r, ebitda=None, drank=None):
    """펀더 종목 7섹션 심층분석. 분기 숫자는 표로 별도 → prose엔 나열 금지. 수급·모멘텀 미포함."""
    code = r["code"]
    per, roe, pbr = r.get("per"), r.get("roe"), r.get("pbr")
    per_str = f"PER {per:.1f}" if (per and per > 0) else "PER 적자/-"
    pbr_str = f"PBR {pbr:.1f}" if (pbr and pbr > 0) else "PBR -"
    roe_str = f"ROE {roe:.1f}%" if (roe is not None and not pd.isna(roe)) else "ROE -"
    if drank:
        debt_str = (f"부채비율 {drank['debt']:.0f}%(동일섹터 {drank['n']}개 중 부채 낮은순 "
                    f"{drank['rank']}위·{drank['band']})")
    elif r.get("debt_ratio") is not None and not pd.isna(r.get("debt_ratio")):
        debt_str = f"부채비율 {r['debt_ratio']:.0f}%(섹터순위 미산출)"
    else:
        debt_str = "부채비율 미제공"
    eb = ebitda or {}
    cf_parts = []
    if eb.get("ebitda") is not None:
        cf_parts.append(f"최근4분기 EBITDA {eb['ebitda']:,.0f}억")
    if eb.get("ev_ebitda") is not None:
        cf_parts.append(f"EV/EBITDA {eb['ev_ebitda']:.1f}배")
    cf_str = ("현금흐름 " + "·".join(cf_parts)) if cf_parts else "현금흐름(EBITDA) 미제공"
    stat = (f"{r['name']}({code}, {r.get('industry', '-')}): {per_str}·{pbr_str}·{roe_str}, "
            f"{debt_str}, {cf_str}")
    prompt = (
        "너는 한국 코스닥 종목 애널리스트다. 최근 뉴스·공시·실적을 광범위하게 검색해 아래 종목의 '펀더멘탈 심층 분석'을 작성하라.\n"
        f"데이터: {stat}\n\n"
        "이 종목은 '이익수익률(저PER)·ROE·저부채' 펀더 점수로 선정됐다(가격모멘텀·수급 미반영).\n"
        "규칙:\n"
        "- 밸류 수치는 위 제공된 PER/PBR/ROE/부채(섹터순위)/EBITDA만 사용(웹의 다른 수치 인용 금지). 없으면 '미제공'.\n"
        "- 부채비율은 절대수치로 단정 말고 반드시 '동일 섹터 내 순위'로 해석(동종 대비 레버리지 부담/여력).\n"
        "- 문체: 자연스러운 완결 문장. 개조식·가운뎃점 나열 금지.\n"
        "- ⚠️ 분기 매출·영업이익 수치는 표로 따로 보여주므로 prose엔 숫자 나열 말고 의미·방향만.\n"
        "- 각 섹션 3~6문장, 실제 사실·뉴스·공시 근거. 모르면 '확인 안 됨'.\n"
        "아래 라벨 형식으로만 출력:\n"
        "이슈: <핵심 키워드 3개 ·로 연결, 40자내>\n"
        "요약: <펀더 투자포인트 thesis 2~3문장>\n"
        "사업실적: <사업 개요 + 최근 실적의 의미·방향(숫자 나열 금지) 3~5문장>\n"
        "촉매: <실적·정책·공시·뉴스 등 펀더 재평가 요인 시간순 3~5문장>\n"
        "밸류: <① 저PER(이익수익률)의 적정성 ② ROE 수익성 ③ 부채비율을 '동일섹터 내 순위'로 해석(절대수치 판정 금지) ④ EBITDA(최근4분기 합산=TTM, 비금융만)로 현금흐름. 단 은행·보험·증권은 EBITDA 비표준이라 생략 → ROE·자본적정성으로 대체. 동종 대비 적정성 4~6문장>\n"
        "강세론: <펀더 재평가/실적 개선 시나리오 근거 3~4문장>\n"
        "리스크: <구체적 하방 리스크 3~5문장. 특히 2025형 저가잡주 랠리 시 소외·실적 둔화 우려>\n"
        "관전: <실적발표일·정책·지표 체크포인트 3~4문장>\n"
        "추정: <다음 분기 매출·영업이익 증권사 컨센서스 방향을 ▲상향/▼하향/→유지 중 하나로 시작 + 근거 1문장. 못 찾으면 '컨센서스 미확인'>")
    resp = c.models.generate_content(model="gemini-2.5-flash", contents=prompt,
        config={"tools": [{"google_search": {}}], "thinking_config": {"thinking_budget": 0}, "max_output_tokens": 12000})
    d = {k: "" for k in FUND_KEYS}; d["추정"] = ""; cur = None
    for line in (resp.text or "").splitlines():
        s = line.strip()
        m = re.match(r"^\**\s*(이슈|요약|사업실적|촉매|밸류|강세론|리스크|관전|추정)\s*[:：]\s*(.*)", s)
        if m:
            cur = "issue" if m.group(1) == "이슈" else m.group(1); d[cur] = m.group(2).strip()
        elif cur and s:
            d[cur] += " " + s
    d["issue"] = _trim_phrase(d["issue"]) if _is_clean(_trim_phrase(d["issue"])) else ""
    for k in FUND_KEYS[1:] + ["추정"]:
        d[k] = d[k].strip().strip("'\"")
    return d


def gemini_analyze_fund(top, ebitdas, dranks, cache):
    """캐시 인식: 재등장(7일내) 종목은 7섹션 재사용 + 오늘 업데이트만 호출. 신규/묵은건 전체분석."""
    from google import genai
    c = genai.Client(api_key=GEMINI_API_KEY)
    today = datetime.now(KST); out = {}
    for _, r in top.iterrows():
        code = r["code"]
        cached = cache.get(code); fresh = False
        if cached and cached.get("date"):
            try:
                fresh = (today - datetime.strptime(cached["date"], "%Y-%m-%d").replace(tzinfo=KST)).days <= 7
            except Exception:
                fresh = False
        try:
            if fresh:
                a = {k: cached.get(k, "") for k in FUND_KEYS}
                u = _gemini_update(c, r["name"], code, cached.get("요약", ""))
                a["업데이트"], a["추정"] = u["업데이트"], u["추정"]
                log(f"  {r['name']}: 캐시 재사용 + 업데이트")
            else:
                a = _gemini_full_fund(c, r, ebitdas.get(code), dranks.get(code)); a["업데이트"] = ""
                log(f"  {r['name']}: 전체 분석")
        except Exception as e:
            log(f"  Gemini {code} 실패: {str(e)[:80]}")
            a = (cached or {k: "" for k in FUND_KEYS}); a.setdefault("업데이트", "")
        a["date"] = today.strftime("%Y-%m-%d")
        out[code] = a; cache[code] = a
    return out


# ==========================
# Notion 레포트 (모멘텀 레포트와 동일 형식 — 토글 펼치면 상세분석)
# ==========================
SCREENER_WARN = (
    "이건 '사서 이긴다' 리스트가 아니라 하위회피 스크리너입니다. "
    "백테스트(2026): 코스닥 펀더 롱온리 5종목은 등가중 벤치에 짐(누적 +4% vs 벤치 +43%). "
    "알파는 저품질·고PER·고부채 '회피'에 있음 — 정상 레짐 롱숏은 +3~5%p/반기지만 2025형 잡주랠리엔 역행. "
    "→ '안 살 종목 거르는' 용도로 보세요.")

DISCLAIMER = (
    "📊 선정: 시총 상위 200  →  펀더 점수(이익수익률 + ROE + 저부채) 상위 10\n"
    "📈 백테(2023~26): 정상레짐 롱숏 +3~5%p/반기 · 7개 반기 중 5개 + · 생존편향(상폐 누락)으로 절대수익 과대\n"
    "ℹ️ 수급·가격모멘텀 미반영(코스닥은 모멘텀 IC 음수 — 코스피와 정반대) · 종목 '이슈'는 AI 검색 추정(확정 아님) · 투자판단 보조용")

METHOD = (
    "🧮 이 리포트는 어떻게 만들어지나 — 시총 필터 + 펀더 점수(slim3)\n\n"
    "[1단계] 시가총액 상위 200\n"
    "코스닥 전 종목 중 시총 상위 200개만 후보로 둡니다. 거래가 얇은 부실 마이크로캡·잡주를 1차로 거르는 관문입니다.\n\n"
    "[2단계] 펀더멘탈 점수 (slim3)\n"
    "이익수익률(1/PER) + ROE + 저부채(낮은 부채비율), 세 지표를 그날 200종목과 비교해 표준화(Z-score)한 뒤 동일가중 합산합니다.\n"
    "　· 이익수익률: 이익 대비 주가가 쌀수록(저PER) ↑\n"
    "　· ROE: 자기자본 대비 이익이 클수록 ↑ (±40% 윈저로 자본잠식 왜곡 차단)\n"
    "　· 저부채: 부채비율이 낮을수록 ↑\n"
    "　매출성장·PBR·수급은 백테스트에서 노이즈/부호반전이라 뺐습니다.\n\n"
    "[왜 펀더인가] 코스닥은 가격 모멘텀이 안 먹힙니다(IC 7개 반기 전부 음수 — 코스피와 정반대). "
    "반면 이 세 펀더 지표는 정상 레짐에서 롱숏 +3~5%p/반기로 유효했습니다.\n\n"
    "[한계] 롱온리 5~10종목으론 등가중 벤치를 못 이깁니다. 알파는 '하위(저품질·고PER·고부채) 회피'에 있으니, "
    "이 리스트는 매수 확신보다 '거를 종목 판별'용으로 보세요. 2025형 저가잡주 랠리엔 역행합니다.\n\n"
    "🔁 점수·순위는 매일 갱신되며 5일 EMA로 평탄화하고, 종목별 '전일 대비 변화'를 함께 표기합니다.")


def _delta_line(dl):
    """전일 대비 변화 한 줄."""
    if not dl:
        return ""
    if dl.get("new"):
        return "📌 전일대비: 신규 진입 (어제 추천 밖)"
    parts = []
    rp = dl.get("rank_prev")
    if rp is not None:
        parts.append(f"순위 {rp}위 유지" if rp == dl.get("_rank") else f"어제 {rp}위")
    if dl.get("score_prev") is not None and dl.get("_score") is not None:
        parts.append(f"점수 {dl['score_prev']:.2f}→{dl['_score']:.2f}")
    return ("📌 전일대비: " + " · ".join(parts)) if parts else ""


def _fnum(v, fmt, dash="—"):
    try:
        return fmt.format(float(v)) if (v is not None and not pd.isna(v)) else dash
    except Exception:
        return dash


def _stock_toggle_fund(rank, r, a, ebitda=None, drank=None, dl=None):
    """종목 1개 = 접이식 토글. 제목줄=펀더 요약지표, 펼치면 점수분해 + 7섹션 + 분기표."""
    icon = {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, "📈")
    sec = r.get("industry") if pd.notna(r.get("industry")) else "-"
    per_s, pbr_s = _fnum(r.get("per"), "{:.1f}"), _fnum(r.get("pbr"), "{:.1f}")
    roe_s, debt_s = _fnum(r.get("roe"), "{:.0f}"), _fnum(r.get("debt_ratio"), "{:.0f}")
    price = r.get("price")
    price_s = f"{int(price):,}원" if (price and not pd.isna(price)) else "—"
    chg = r.get("prdy_ctrt") or 0
    chg_color = "red" if chg > 0 else ("blue" if chg < 0 else "gray")   # 상승 빨강·하락 파랑(국내 관습)
    score = float(r.get("multi_score", 0) or 0)

    def gray(t): return {"type": "text", "text": {"content": t}, "annotations": {"color": "gray"}}
    title_rich = [
        {"type": "text", "text": {"content": f"{icon} {rank}. {r['name']} "}, "annotations": {"bold": True}},
        {"type": "text", "text": {"content": f"{chg:+.1f}% "}, "annotations": {"bold": True, "color": chg_color}},
        gray(f"({r['code']}) · {sec}  |  펀더 {score:.2f} · PER {per_s} · ROE {roe_s}% · 부채 {debt_s}% · {price_s}")]

    kids = []
    if dl:
        dl = {**dl, "_rank": rank, "_score": score}
        dtxt = _delta_line(dl)
        if dtxt:
            kids.append(_para([{"type": "text", "text": {"content": dtxt},
                                "annotations": {"bold": True, "color": "blue"}}]))
    if a.get("업데이트"):
        kids.append(_para([
            {"type": "text", "text": {"content": "🆕 오늘 업데이트  "}, "annotations": {"bold": True, "color": "green"}},
            {"type": "text", "text": {"content": a["업데이트"]}}]))
    # 펀더 점수 분해 (막대 대신 텍스트 — 무엇이 점수를 끌어올렸나)
    drk = f" (섹터 부채 낮은순 {drank['rank']}/{drank['n']}위·{drank['band']})" if drank else ""
    fline = (f"🏢 PER {per_s} · PBR {pbr_s} · ROE {roe_s}% · 부채 {debt_s}%{drk}\n"
             f"🧮 펀더 {score:.2f} = 이익수익률 {float(r.get('f_ey', 0)):+.2f} · "
             f"ROE {float(r.get('f_roe', 0)):+.2f} · 저부채 {float(r.get('f_lowdebt', 0)):+.2f}")
    if a.get("issue"):
        fline += f"\n📰 이슈 — {a['issue']}"
    kids.append(_para([gray(fline)]))
    for label, key in FUND_SECTIONS:
        v = (a.get(key) or "").strip()
        if v:
            kids.append(_para([
                {"type": "text", "text": {"content": f"{label}\n"}, "annotations": {"bold": True}},
                {"type": "text", "text": {"content": v[:1900]}}]))
    return {"object": "block", "type": "toggle", "toggle": {
        "rich_text": title_rich,
        "color": "blue_background" if rank <= 3 else "default",
        "children": kids}}


def upload_notion_fund(top, analysis, ebitdas, dranks, deltas, newly, dropped, incomes, today_str):
    """Notion 업로드(모멘텀 레포트와 동일 흐름): 헤더 생성 → 종목별 토글 append → 토글 안 분기표·추정 append."""
    analysis = analysis or {}; ebitdas = ebitdas or {}; dranks = dranks or {}; deltas = deltas or {}; incomes = incomes or {}
    headers = {"Authorization": f"Bearer {NOTION_API_KEY}",
               "Content-Type": "application/json", "Notion-Version": "2022-06-28"}
    title = f"🇰🇷 {today_str} KOSDAQ 펀더 추천 (저PER·고ROE·저부채)"

    header = [
        {"object": "block", "type": "callout", "callout": {
            "rich_text": [{"type": "text", "text": {"content": SCREENER_WARN}, "annotations": {"bold": True}}],
            "icon": {"type": "emoji", "emoji": "⚠️"}, "color": "orange_background"}},
        {"object": "block", "type": "callout", "callout": {
            "rich_text": [{"type": "text", "text": {"content": DISCLAIMER}}],
            "icon": {"type": "emoji", "emoji": "📐"}, "color": "yellow_background"}},
        {"object": "block", "type": "toggle", "toggle": {
            "rich_text": [{"type": "text", "text": {"content": "🧮 산출 방법 — 펼쳐 보기 (시총 필터 + 펀더 점수)"},
                           "annotations": {"bold": True, "color": "gray"}}],
            "children": [_para([{"type": "text", "text": {"content": METHOD}, "annotations": {"color": "gray"}}])]}},
    ]
    if newly or dropped:
        chg = []
        if newly:
            chg.append(f"🆕 신규 진입: {', '.join(newly)}")
        if dropped:
            chg.append(f"📉 이탈: {', '.join(dropped)}")
        header.append({"object": "block", "type": "callout", "callout": {
            "rich_text": [{"type": "text", "text": {"content": "어제 대비  " + "   ·   ".join(chg)}}],
            "icon": {"type": "emoji", "emoji": "📌"}, "color": "blue_background"}})
    header.append({"object": "block", "type": "heading_3",
                   "heading_3": {"rich_text": [{"type": "text", "text": {"content": "🏆 상위 10 — 종목을 펼치면 상세 분석"}}]}})

    date_parent = _get_or_create_date_page(today_str, headers, NOTION_PARENT_PAGE_ID)
    _archive_same_title_pages(title, headers, date_parent)
    _archive_same_title_pages(f"🇰🇷 {today_str} KOSDAQ 추천종목", headers, date_parent)   # 옛 포맷 페이지 정리

    r0 = None
    for attempt in range(3):
        try:
            r0 = requests.post("https://api.notion.com/v1/pages", headers=headers, timeout=30,
                               json={"parent": {"page_id": date_parent},
                                     "properties": {"title": {"title": [{"text": {"content": title}}]}},
                                     "children": header})
            if r0.status_code == 200:
                break
            log(f"  ⚠️ 페이지 생성 {r0.status_code} ({attempt+1}/3): {r0.text[:150]}")
        except Exception as e:
            log(f"  ⚠️ 페이지 생성 예외({attempt+1}/3): {str(e)[:120]}")
        time.sleep(2 * (attempt + 1))
    if r0 is None or r0.status_code != 200:
        log("❌ Notion 페이지 생성 최종 실패"); return
    page_id = r0.json()["id"]; page_url = r0.json().get("url", "")

    def append(block_id, blocks):
        for attempt in range(3):    # 일시 timeout·5xx·429 재시도 — 1종목 실패가 전체 중단 막음
            try:
                rr = requests.patch(f"https://api.notion.com/v1/blocks/{block_id}/children",
                                    headers=headers, json={"children": blocks}, timeout=30)
                time.sleep(0.35)
                if rr.status_code == 200:
                    return rr.json().get("results", [])
                log(f"  ⚠️ append {rr.status_code} ({attempt+1}/3): {rr.text[:120]}")
                if rr.status_code < 500 and rr.status_code != 429:
                    return None
            except Exception as e:
                log(f"  ⚠️ append 예외({attempt+1}/3): {str(e)[:120]}")
            time.sleep(2 * (attempt + 1))
        return None

    for rank, (_, r) in enumerate(top.head(10).iterrows(), 1):
        tog = _stock_toggle_fund(rank, r, analysis.get(r["code"], {}),
                                 ebitdas.get(r["code"]), dranks.get(r["code"]), deltas.get(r["code"]))
        res = append(page_id, [tog])
        if not res:
            continue
        tid = res[0]["id"]
        a = analysis.get(r["code"], {})
        extra = []
        qt = _quarter_table(incomes.get(r["code"]))
        if qt:
            extra.append(_para([{"type": "text", "text": {"content": "📊 분기 실적 추이 (단일분기)"}, "annotations": {"bold": True}}]))
            extra.append(qt)
        if a.get("추정"):
            extra.append(_para([{"type": "text", "text": {"content": "📈 다음 분기 컨센서스  "}, "annotations": {"bold": True}},
                                {"type": "text", "text": {"content": a["추정"]}}]))
        if extra:
            append(tid, extra)
    log(f"✅ Notion 업로드 완료: {page_url}")


# ==========================
# 메인
# ==========================
def main():
    log("▶ Access Token 발급 시작")
    access_token = get_access_token(APP_KEY, APP_SECRET)
    if not access_token:
        return
    log("✅ Access Token 발급 완료")

    # 종목 리스트
    log("▶ 베이스 종목 리스트 로드")
    base_df = pd.read_csv(input_csv, dtype={"단축코드": str})
    base_df["단축코드"] = base_df["단축코드"].str.zfill(6)
    total = len(base_df)
    log(f"✅ 로드 완료: 총 {total}개 (코스닥)")

    # ------------------------------------
    # 1) 시가총액 + PER/EPS/PBR/섹터/등락 조회 (전 종목, 병렬)
    #    (수급 30일 조회 단계는 slim3 펀더 점수에 불필요 → 제거)
    # ------------------------------------
    log(f"▶ 시가총액 + PER/EPS 조회 시작 (전 종목, workers={WORKERS_MKTCAP})")
    start_cap = time.time()

    caps = [None] * total; pers = [None] * total; epss = [None] * total
    pbrs = [None] * total; industries = [None] * total; prdy_ctrts = [None] * total
    rows = list(base_df.reset_index(drop=True).iterrows())

    def cap_worker(pos_and_row):
        pos, row = pos_and_row
        mktcap, per, eps, pbr, industry, prdy_ctrt = get_valuation_info(row["단축코드"], access_token)
        time.sleep(0.15)  # 호출 매너
        return pos, mktcap, per, eps, pbr, industry, prdy_ctrt

    with ThreadPoolExecutor(max_workers=WORKERS_MKTCAP) as ex:
        futures = {ex.submit(cap_worker, pr): pr[0] for pr in rows}
        for cnt, future in enumerate(as_completed(futures), start=1):
            pos, cap, per, eps, pbr, industry, prdy_ctrt = future.result()
            caps[pos], pers[pos], epss[pos] = cap, per, eps
            pbrs[pos], industries[pos], prdy_ctrts[pos] = pbr, industry, prdy_ctrt
            if cnt % 200 == 0 or cnt == total:
                log(f"  시총 {cnt}/{total} ({time.time()-start_cap:.0f}s)")

    cap_df = base_df[["단축코드", "한글명"]].copy()
    cap_df["market_cap"] = caps; cap_df["per"] = pers; cap_df["eps"] = epss
    cap_df["pbr"] = pbrs; cap_df["industry"] = industries; cap_df["prdy_ctrt"] = prdy_ctrts
    cap_df.rename(columns={"단축코드": "code", "한글명": "name"}, inplace=True)

    # 시총 결측/0 제거 후 상위 200
    cap_df["market_cap"] = pd.to_numeric(cap_df["market_cap"], errors="coerce")
    cap_df = cap_df.dropna(subset=["market_cap"])
    cap_df = cap_df[cap_df["market_cap"] > 0].copy()
    cap_df.sort_values("market_cap", ascending=False, inplace=True)
    top200_codes = set(cap_df.head(TOP_MKTCAP_N)["code"])
    log(f"✅ 시가총액 조회 완료: 유효 {len(cap_df)}개 / 시총상위{TOP_MKTCAP_N} 확보")

    # ------------------------------------
    # 2) 재무비율(ROE/부채) 붙이기 — 전 종목 (캐시 공유)
    # ------------------------------------
    fin_df = load_or_fetch_fin_ratios(cap_df["code"].tolist(), access_token)
    all_df = cap_df.merge(fin_df, on="code", how="left")

    # ------------------------------------
    # 3) slim3 펀더 점수 (시총상위200 단면 z) + EMA 평탄화 + 추천 TOP_RECO_N
    # ------------------------------------
    uni_df = all_df[all_df["code"].isin(top200_codes)].copy()
    uni_df["raw_score"] = compute_slim3_score(uni_df)
    log("✅ slim3 펀더 점수 계산 완료 (이익수익률 + ROE + 저부채, 시총상위200 단면)")

    today_dt = datetime.now(KST)
    smoothed, history_updated = smooth_with_ema(uni_df, today_dt)
    uni_df["multi_score"] = uni_df["code"].map(smoothed).fillna(uni_df["raw_score"])
    history_updated.to_csv(KOSDAQ_SCORE_HISTORY, index=False)
    log(f"✅ EMA 평탄화 (span={EMA_SPAN}일, 누적 이력 {history_updated['date'].nunique()}일치)")

    rank_changes = compute_rank_change(history_updated)
    uni_df.sort_values("multi_score", ascending=False, inplace=True)
    uni_df.reset_index(drop=True, inplace=True)
    uni_df["rank"] = np.arange(1, len(uni_df) + 1)
    reco_df = uni_df.head(TOP_RECO_N).copy()
    reco_df["rank_change"] = reco_df["code"].map(rank_changes)
    today_str = today_dt.strftime("%Y-%m-%d")
    log(f"✅ 추천 종목(시총상위200 내 펀더 상위 {TOP_RECO_N}) 생성 완료")

    # ------------------------------------
    # 4) 전일 대비 신규 진입 / 이탈 (추천 이력 비교)
    # ------------------------------------
    reco_hist_path = os.path.join(_BASE_DIR, "kosdaq_reco_history.csv")
    rhist = pd.read_csv(reco_hist_path, dtype={"code": str}) if os.path.exists(reco_hist_path) else pd.DataFrame()
    deltas, prev_names = {}, {}
    if len(rhist):
        rhist["code"] = rhist["code"].str.zfill(6)
        prev_dates = sorted(d for d in rhist["date"].unique() if d < today_str)
        if prev_dates:
            pv = rhist[rhist["date"] == prev_dates[-1]]
            prev_names = dict(zip(pv["code"], pv["name"]))
            pmap = pv.set_index("code")[["rank", "multi_score"]].to_dict("index")
            for _, r in reco_df.iterrows():
                p = pmap.get(r["code"])
                deltas[r["code"]] = ({"rank_prev": int(p["rank"]), "score_prev": float(p["multi_score"])}
                                     if p else {"new": True})
    today_codes = set(reco_df["code"])
    newly = [r["name"] for _, r in reco_df.iterrows() if deltas.get(r["code"], {}).get("new")]
    dropped = [prev_names[c] for c in prev_names if c not in today_codes]
    # 이력 갱신 (오늘 행 교체 + 60일 보관)
    snap = reco_df[["code", "name", "rank", "multi_score"]].copy(); snap.insert(0, "date", today_str)
    rhist = pd.concat([rhist[rhist["date"] != today_str] if len(rhist) else rhist, snap], ignore_index=True)
    rhist = rhist[rhist["date"] >= (today_dt - timedelta(days=HISTORY_RETAIN_D)).strftime("%Y-%m-%d")]
    rhist.to_csv(reco_hist_path, index=False, encoding="utf-8-sig")

    # ------------------------------------
    # 5) 상위 10 enrich: 현재가 · 분기실적 · EBITDA · 섹터 부채순위 + Gemini 심층분석
    # ------------------------------------
    top10 = reco_df.head(10).copy()
    log("▶ 상위10 현재가·분기실적·EBITDA 수집")
    prices, incomes, ebitdas = {}, {}, {}
    for code in top10["code"]:
        px = get_daily_prices(code, access_token)
        prices[code] = px[-1] if px else None
        incomes[code] = fetch_income(code, access_token)
        ebitdas[code] = fetch_ebitda(code, access_token)
        time.sleep(0.15)
    reco_df["price"] = reco_df["code"].map(prices)
    top10["price"] = top10["code"].map(prices)
    dranks = kosdaq_debt_ranks(all_df)

    cache = {}
    if os.path.exists(KOSDAQ_ANALYSIS_CACHE):
        try:
            cache = json.load(open(KOSDAQ_ANALYSIS_CACHE, encoding="utf-8"))
        except Exception:
            cache = {}
    analysis = gemini_analyze_fund(top10, ebitdas, dranks, cache) if GEMINI_API_KEY else {}
    if analysis:
        json.dump(cache, open(KOSDAQ_ANALYSIS_CACHE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    # ------------------------------------
    # 6) 엑셀 + latest_kosdaq.csv 저장 (다운스트림용 — 컬럼 유지)
    # ------------------------------------
    output_file = os.path.join(OUTPUT_DIR, f"{today_str}_코스닥퀀트데이터.xlsx")
    all_kor = to_korean_columns(all_df.sort_values("market_cap", ascending=False).reset_index(drop=True))
    reco_kor = to_korean_columns(reco_df)
    reco_kor["랭킹"] = np.arange(1, len(reco_kor) + 1)
    reco_kor.drop(columns=["rank"], inplace=True, errors="ignore")
    log("▶ 엑셀 저장 시작")
    with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
        all_kor.to_excel(writer, index=False, sheet_name="전체 종목")
        reco_kor.to_excel(writer, index=False, sheet_name="추천 종목")
    log(f"🎉 엑셀 저장 완료: {output_file}")

    csv_path = os.path.join(_BASE_DIR, "latest_kosdaq.csv")
    all_kor["기준일자"] = today_str
    all_kor.to_csv(csv_path, index=False, encoding="utf-8-sig")
    log(f"✅ latest_kosdaq.csv 저장: {csv_path}")

    # ------------------------------------
    # 7) Notion 업로드 (모멘텀 레포트와 동일 형식)
    # ------------------------------------
    if NOTION_API_KEY:
        log("▶ Notion 업로드 시작")
        upload_notion_fund(top10, analysis, ebitdas, dranks, deltas, newly, dropped, incomes, today_str)
    else:
        log("NOTION_API_KEY 없음 → Notion 업로드 생략 (로컬). Actions 에선 업로드됨")

if __name__ == "__main__":
    main()
