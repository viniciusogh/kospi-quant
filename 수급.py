import requests
import pandas as pd
import numpy as np
import time
import random
import os
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# ==========================
# 환경설정 (로컬 fallback + GitHub Secrets 겸용)
# ==========================
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

APP_KEY    = os.environ.get("APP_KEY",    "PSF96Bj6V3iGD0QtAtdkN1oRSfP8vt3Eu3cJ")
APP_SECRET = os.environ.get("APP_SECRET", "B3zd/+MjbJLSGBHOHzXeyAl3zVAMs3Od0F2dpF6s0yINTP5+7tkMLRRijsD8CKR2YPcU/bp7nxS1K8wrvirCEm0EIyKuBGVOZcSCw+uCmmPAVIzsQTWA3wR7KgPFYIZEKmw37HzAIxn1wMy8H1DHxPsu6A2s9gpGptPK2W94f1hpHcVPqiQ=")

input_csv = os.environ.get("INPUT_CSV",
    os.path.join(_BASE_DIR, "KOSPI재무데이터한투.csv"))

# Notion
NOTION_API_KEY        = os.environ.get("NOTION_API_KEY",        "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")
NOTION_PARENT_PAGE_ID = os.environ.get("NOTION_PARENT_PAGE_ID", "3324a00632f880fbb014d766d87a1079")

# 코스피
MRKT_CODE = "J"

# 병렬 설정
WORKERS_NETFLOW = 4
WORKERS_MKTCAP  = 5

# 추천 설정
TOP_MKTCAP_N = 200
TOP_RECO_N   = 30

# 재무비율 캐시 (주간 갱신)
FIN_RATIO_CACHE      = os.environ.get("FIN_RATIO_CACHE",
    os.path.join(_BASE_DIR, "fin_ratio_cache.csv"))
FIN_RATIO_CACHE_DAYS = 7

# 엑셀 출력 디렉토리 (로컬=Desktop, CI=스크립트 폴더)
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", os.path.expanduser("~/Desktop"))

# ==========================
# 로그 유틸
# ==========================
def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")

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
    try:
        r = requests.post(url, headers=headers, json=data, timeout=5)
        r.raise_for_status()
        token_data = r.json()
        log("✅ Access Token 발급 성공")
        return token_data["access_token"]
    except Exception as e:
        log(f"❌ Access Token 발급 실패: {e}")
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
    를 가져온다.
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
        return None, None, None, None

    try:
        data = r.json().get("output", {})
        mktcap = int(data.get("hts_avls", 0))
        per = data.get("per")
        eps = data.get("eps")
        pbr = data.get("pbr")

        # 숫자 변환 시도, 실패하면 None
        def _safe(v):
            try:
                return float(v) if v not in (None, "", "0", "0.0") else None
            except Exception:
                return None

        per = _safe(per)
        eps = _safe(eps)
        pbr = _safe(pbr)

        return mktcap, per, eps, pbr
    except Exception:
        return None, None, None, None

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
    cache_days = 1 if datetime.now().month in EARNINGS_MONTHS else FIN_RATIO_CACHE_DAYS

    if os.path.exists(FIN_RATIO_CACHE):
        age_days = (time.time() - os.path.getmtime(FIN_RATIO_CACHE)) / 86400
        season = "실적시즌" if datetime.now().month in EARNINGS_MONTHS else "비시즌"
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
def _cross_z(s: pd.Series) -> pd.Series:
    """단면(Cross-sectional) Z-score"""
    return (s - s.mean()) / (s.std() + 1e-8)


def compute_multifactor_score(df: pd.DataFrame) -> pd.Series:
    """
    수급 0.35 + 밸류 0.20 + 퀄리티 0.25 + 성장 0.15 + 안정성 0.05
    각 팩터는 단면 Z-score 표준화 후 가중합산.
    데이터 없는 종목은 해당 팩터 기여도 = 0 (중립) 처리.
    """
    # 수급 팩터
    z_supply = _cross_z(df["strength_score"].fillna(0))

    # 밸류 팩터: 1/PER + 1/PBR (저평가일수록 역수 크게)
    inv_per = df["per"].apply(lambda x: 1 / x if pd.notna(x) and x > 0 else np.nan)
    inv_pbr = df["pbr"].apply(lambda x: 1 / x if pd.notna(x) and x > 0 else np.nan)
    z_val = (_cross_z(inv_per).fillna(0) + _cross_z(inv_pbr).fillna(0)) / 2

    # 퀄리티 팩터: ROE
    z_quality = _cross_z(df["roe"]).fillna(0)

    # 성장 팩터: 매출증가율 + 영업이익증가율
    z_growth = (_cross_z(df["rev_growth"]).fillna(0) + _cross_z(df["op_profit_growth"]).fillna(0)) / 2

    # 안정성 팩터: 부채비율 낮을수록 유리 → 음수 취해 z-score
    debt_filled = df["debt_ratio"].fillna(df["debt_ratio"].median()).fillna(100)
    z_safety = _cross_z(-debt_filled).fillna(0)

    return (
        0.25 * z_supply
        + 0.20 * z_val
        + 0.25 * z_quality
        + 0.15 * z_growth
        + 0.15 * z_safety
    )


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
    }
    return df.rename(columns={c: col_map.get(c, c) for c in df.columns})

# ==========================
# Notion 업로드
# ==========================
def upload_to_notion(reco_kor: pd.DataFrame):
    """추천종목 표를 Notion 새 페이지에 업로드"""
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }
    today_str = datetime.today().strftime("%Y-%m-%d")

    col_labels = ["랭킹", "종목코드", "종목명", "멀티팩터점수", "수급강화점수",
                  "시가총액(억)", "외국인순매수(백만)", "기관순매수(백만)",
                  "PER", "PBR", "ROE(%)", "부채비율(%)"]

    def cell(text):
        return [{"type": "text", "text": {"content": str(text)}}]

    rows = [{"type": "table_row", "table_row": {"cells": [cell(c) for c in col_labels]}}]

    for _, row in reco_kor.iterrows():
        try:
            def _v(val):
                """None / NaN 모두 None으로 통일"""
                try:
                    return None if (val is None or pd.isna(val)) else val
                except Exception:
                    return None

            per_val  = _v(row.get("PER", None))
            pbr_val  = _v(row.get("PBR", None))
            roe_val  = _v(row.get("ROE(%)", None))
            debt_val = _v(row.get("부채비율(%)", None))
            rows.append({"type": "table_row", "table_row": {"cells": [
                cell(int(row.get("랭킹", ""))),
                cell(row.get("종목코드", "")),
                cell(row.get("종목명", "")),
                cell(f"{float(row.get('멀티팩터점수', 0)):.3f}"),
                cell(f"{float(row.get('수급강화점수', 0)):.3f}"),
                cell(f"{int(row.get('시가총액', 0)):,}"),
                cell(f"{float(row.get('외국인_순매수대금(백만원)', 0)):,.0f}"),
                cell(f"{float(row.get('기관_순매수대금(백만원)', 0)):,.0f}"),
                cell(f"{float(per_val):.1f}"  if per_val  is not None else "-"),
                cell(f"{float(pbr_val):.2f}"  if pbr_val  is not None else "-"),
                cell(f"{float(roe_val):.1f}"  if roe_val  is not None else "-"),
                cell(f"{float(debt_val):.1f}" if debt_val is not None else "-"),
            ]}})
        except Exception:
            continue

    body = {
        "parent": {"page_id": NOTION_PARENT_PAGE_ID},
        "properties": {"title": {"title": [{"text": {"content": f"📊 {today_str} 추천종목"}}]}},
        "children": [
            {
                "object": "block",
                "type": "heading_2",
                "heading_2": {"rich_text": [{"type": "text", "text": {"content": f"코스피 멀티팩터 추천종목 TOP{len(reco_kor)} ({today_str})"}}]},
            },
            {
                "object": "block",
                "type": "table",
                "table": {
                    "table_width": len(col_labels),
                    "has_column_header": True,
                    "has_row_header": False,
                    "children": rows,
                },
            },
        ],
    }

    try:
        r = requests.post("https://api.notion.com/v1/pages", headers=headers, json=body, timeout=15)
        if r.status_code == 200:
            log(f"✅ Notion 업로드 완료: {r.json().get('url', '')}")
        else:
            log(f"❌ Notion 업로드 실패 ({r.status_code}): {r.text[:300]}")
    except Exception as e:
        log(f"❌ Notion 요청 오류: {e}")


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
    log(f"✅ 로드 완료: 총 {total}개 (코스피)")

    # ------------------------------------
    # 1) 수급 점수 계산 (병렬)
    # ------------------------------------
    log(f"▶ 수급(30일) 조회 + 점수 계산 시작 (workers={WORKERS_NETFLOW})")
    start_time = time.time()
    score_rows = []

    def nf_worker(row):
        code = row["단축코드"]
        name = row.get("한글명", "")
        df_nf = get_netflow_history(code, access_token)
        if df_nf.empty:
            return None
        return compute_strength_score(code, name, df_nf)

    with ThreadPoolExecutor(max_workers=WORKERS_NETFLOW) as ex:
        futures = {
            ex.submit(nf_worker, row): idx
            for idx, (_, row) in enumerate(base_df.iterrows())
        }
        for cnt, future in enumerate(as_completed(futures), start=1):
            res = future.result()
            if res is not None:
                score_rows.append(res)

            # 진행률 로그(요구 포맷 유지)
            elapsed = time.time() - start_time
            speed = cnt / elapsed if elapsed > 0 else 0
            remaining = (total - cnt) / speed if speed > 0 else 0
            pct = cnt / total * 100

            print(
                f"[{cnt}/{total} | {pct:5.2f}%] "
                f"경과 {elapsed:6.1f}s | 남은 {remaining:6.1f}s | "
                f"속도 {speed:4.2f} 종목/s"
            )

    if not score_rows:
        log("❌ 수급 점수 데이터가 없습니다. 종료합니다.")
        return

    score_df = pd.DataFrame(score_rows)
    # 전체 종목: 점수 내림차순 + 랭킹
    score_df.sort_values("strength_score", ascending=False, inplace=True)
    score_df.reset_index(drop=True, inplace=True)
    score_df["rank"] = np.arange(1, len(score_df) + 1)

    log(f"✅ 수급 점수 계산 완료: 유효 {len(score_df)}개")

    # ------------------------------------
    # 2) 시가총액 + PER + EPS 조회 (전 종목, 병렬=5)
    # ------------------------------------
    log(f"▶ 시가총액 + PER/EPS 조회 시작 (전 종목, workers={WORKERS_MKTCAP})")
    start_cap = time.time()

    caps = [None] * total
    pers = [None] * total
    epss = [None] * total
    pbrs = [None] * total
    rows = list(base_df.reset_index(drop=True).iterrows())

    def cap_worker(pos_and_row):
        pos, row = pos_and_row
        code = row["단축코드"]
        mktcap, per, eps, pbr = get_valuation_info(code, access_token)
        time.sleep(0.15)  # 호출 매너
        return pos, mktcap, per, eps, pbr

    with ThreadPoolExecutor(max_workers=WORKERS_MKTCAP) as ex:
        futures = {ex.submit(cap_worker, pr): pr[0] for pr in rows}
        for cnt, future in enumerate(as_completed(futures), start=1):
            pos, cap, per, eps, pbr = future.result()
            caps[pos] = cap
            pers[pos] = per
            epss[pos] = eps
            pbrs[pos] = pbr

            elapsed = time.time() - start_cap
            speed = cnt / elapsed if elapsed > 0 else 0
            remaining = (total - cnt) / speed if speed > 0 else 0
            pct = cnt / total * 100

            print(
                f"[시총 {cnt}/{total} | {pct:5.2f}%] "
                f"경과 {elapsed:6.1f}s | 남은 {remaining:6.1f}s | "
                f"속도 {speed:4.2f} 종목/s"
            )

    cap_df = base_df[["단축코드", "한글명"]].copy()
    cap_df["market_cap"] = caps
    cap_df["per"] = pers
    cap_df["eps"] = epss
    cap_df["pbr"] = pbrs
    cap_df.rename(columns={"단축코드": "code", "한글명": "name"}, inplace=True)

    # 시총 결측/0 제거 후 상위 200
    cap_df["market_cap"] = pd.to_numeric(cap_df["market_cap"], errors="coerce")
    cap_df = cap_df.dropna(subset=["market_cap"])
    cap_df = cap_df[cap_df["market_cap"] > 0].copy()
    cap_df.sort_values("market_cap", ascending=False, inplace=True)
    top200 = cap_df.head(TOP_MKTCAP_N)[["code", "market_cap"]].copy()

    log(f"✅ 시가총액 + PER/EPS 조회 완료: 유효 {len(cap_df)}개 / 시총상위200 확보")

    # ------------------------------------
    # 3) 전체 점수표에 시총/밸류 붙이기 + 추천 N개 뽑기
    # ------------------------------------
    all_df = score_df.merge(cap_df[["code", "market_cap", "per", "eps", "pbr"]], on="code", how="left")

    # ------------------------------------
    # 3.5) 재무비율 조회/캐시 + 멀티팩터 점수 계산
    # ------------------------------------
    fin_df = load_or_fetch_fin_ratios(all_df["code"].tolist(), access_token)
    all_df = all_df.merge(fin_df, on="code", how="left")
    all_df["multi_score"] = compute_multifactor_score(all_df)
    log("✅ 멀티팩터 점수 계산 완료")

    # 추천: 시총 상위 200 유니버스에 포함되는 종목 중 점수 상위 TOP_RECO_N
    uni_df = all_df.merge(top200[["code"]], on="code", how="inner").copy()
    uni_df.sort_values("multi_score", ascending=False, inplace=True)
    uni_df.reset_index(drop=True, inplace=True)
    uni_df["rank"] = np.arange(1, len(uni_df) + 1)
    reco_df = uni_df.head(TOP_RECO_N).copy()

    log(f"✅ 추천 유니버스(시총상위200) 내 유효 종목: {len(uni_df)}개")
    log(f"✅ 추천 종목(상위 {TOP_RECO_N}) 생성 완료")

    # ------------------------------------
    # 4) 엑셀 저장 규칙
    # ------------------------------------
    today_str = datetime.today().strftime("%Y-%m-%d")
    output_file = os.path.join(OUTPUT_DIR, f"{today_str}_국내퀀트데이터.xlsx")

    # 시트: 전체 종목 / 추천 종목
    all_kor = to_korean_columns(all_df.sort_values("multi_score", ascending=False).reset_index(drop=True))
    all_kor["랭킹"] = np.arange(1, len(all_kor) + 1)  # 한국어 컬럼명과 별개로 확실히
    all_kor.drop(columns=["rank"], inplace=True, errors="ignore")

    reco_kor = to_korean_columns(reco_df)
    reco_kor["랭킹"] = np.arange(1, len(reco_kor) + 1)
    reco_kor.drop(columns=["rank"], inplace=True, errors="ignore")

    log("▶ 엑셀 저장 시작")
    with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
        all_kor.to_excel(writer, index=False, sheet_name="전체 종목")
        reco_kor.to_excel(writer, index=False, sheet_name="추천 종목")

    log(f"🎉 엑셀 저장 완료: {output_file}")

    # ------------------------------------
    # 5) Notion 업로드
    # ------------------------------------
    log("▶ Notion 업로드 시작")
    upload_to_notion(reco_kor)

if __name__ == "__main__":
    main()
