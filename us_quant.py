import os
import time
import requests
import pandas as pd
from io import StringIO
import numpy as np
import yfinance as yf
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

KST = timezone(timedelta(hours=9))   # GitHub Actions 러너는 UTC, 모든 날짜·시각은 KST 기준

# ==========================
# 환경설정
# ==========================
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

NOTION_API_KEY        = os.environ.get("NOTION_API_KEY",        "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")
NOTION_PARENT_PAGE_ID = os.environ.get("NOTION_PARENT_PAGE_ID", "3324a00632f880fbb014d766d87a1079")

TOP_MCAP_N        = 300   # 시총 상위 N개에서 필터
TOP_RECO_N        = 20    # 최종 추천 종목 수
WORKERS           = 10    # 재무 데이터 조회 병렬 수

US_FIN_CACHE      = os.path.join(_BASE_DIR, "us_fin_cache.csv")
US_FIN_CACHE_DAYS = 7
OUTPUT_DIR        = os.environ.get("OUTPUT_DIR", os.path.expanduser("~/Desktop"))

# 점수 시계열 평탄화 (단일 시점 노이즈 완화)
US_SCORE_HISTORY  = os.path.join(_BASE_DIR, "us_score_history.csv")
EMA_SPAN          = 5     # 영업일 기준
HISTORY_RETAIN_D  = 60    # 이력 보관 기간 (일)

# ==========================
# 유틸
# ==========================
def log(msg: str):
    print(f"[{datetime.now(KST).strftime('%H:%M:%S')}] {msg}")

# ==========================
# S&P 500 종목 리스트 (Wikipedia)
# ==========================
def get_sp500_tickers() -> list[dict]:
    """Wikipedia에서 S&P 500 종목 코드·이름·섹터 가져오기"""
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
        html = requests.get(url, headers=headers, timeout=10).text
        tables = pd.read_html(StringIO(html))
        df = tables[0]
        result = []
        for _, row in df.iterrows():
            sym = str(row['Symbol']).replace('.', '-')   # BRK.B → BRK-B
            result.append({
                "ticker": sym,
                "name":   str(row.get('Security', sym)),
                "sector": str(row.get('GICS Sector', '')),
            })
        return result
    except Exception as e:
        log(f"❌ S&P 500 리스트 로드 실패: {e}")
        return []

# ==========================
# 가격 데이터 다운로드: 3개월 모멘텀 + 정배열 용 종가 로우데이터
# ==========================
def get_price_data(tickers: list[str]) -> tuple[dict, dict, dict]:
    """
    yf.download()로 일괄 다운로드
    반환: (momentum_dict, prices_dict, prdy_ctrt_dict)
      - momentum_dict:   {ticker: 3개월 수익률 (소수)}
      - prices_dict:     {ticker: pd.Series 종가 시계열} ← 정배열 계산용
      - prdy_ctrt_dict:  {ticker: 전일 대비 등락률(%)} ← Notion 표시용
    """
    log(f"▶ 가격 데이터 다운로드 시작 ({len(tickers)}개 종목)")
    try:
        raw = yf.download(
            tickers, period="4mo", interval="1d",
            progress=False, auto_adjust=True,
            group_by="ticker", threads=True,
        )
        momentum       = {}
        prices_dict    = {}
        prdy_ctrt_dict = {}
        for t in tickers:
            try:
                prices = (raw['Close'] if len(tickers) == 1 else raw[t]['Close']).dropna()
                prices_dict[t] = prices
                if len(prices) >= 60:
                    momentum[t] = float(prices.iloc[-1] / prices.iloc[-63] - 1)
                if len(prices) >= 2:
                    prdy_ctrt_dict[t] = float(prices.iloc[-1] / prices.iloc[-2] - 1) * 100
            except Exception:
                pass
        log(f"✅ 다운로드 완료: 모멘텀 {len(momentum)}개 / 종가데이터 {len(prices_dict)}개 / 일일등락 {len(prdy_ctrt_dict)}개")
        return momentum, prices_dict, prdy_ctrt_dict
    except Exception as e:
        log(f"❌ 가격 다운로드 실패: {e}")
        return {}, {}, {}


def is_ma_aligned(prices: pd.Series) -> bool:
    """정배열 확인: 5MA > 20MA > 60MA (yfinance 4개월 데이터 재사용, 추가 API 호출 없음)"""
    if len(prices) < 60:
        return False
    ma5  = float(prices.iloc[-5:].mean())
    ma20 = float(prices.iloc[-20:].mean())
    ma60 = float(prices.iloc[-60:].mean())
    return ma5 > ma20 > ma60

# ==========================
# 단일 종목 재무 데이터 (yfinance info)
# ==========================
def _fetch_one(ticker: str) -> dict:
    try:
        info = yf.Ticker(ticker).info
        return {
            "ticker":      ticker,
            "market_cap":  info.get("marketCap"),
            "pe":          info.get("trailingPE"),
            "pb":          info.get("priceToBook"),
            "roe":         info.get("returnOnEquity"),    # 소수점 (0.45 = 45%)
            "rev_growth":  info.get("revenueGrowth"),     # 소수점
            "earn_growth": info.get("earningsGrowth"),    # 소수점
            "debt_equity": info.get("debtToEquity"),      # %
        }
    except Exception:
        return {"ticker": ticker}

# ==========================
# 재무 데이터 캐시 로드 or 일괄 조회
# ==========================
def load_or_fetch_us_fundamentals(tickers: list[str]) -> pd.DataFrame:
    if os.path.exists(US_FIN_CACHE):
        age_days = (time.time() - os.path.getmtime(US_FIN_CACHE)) / 86400
        if age_days < US_FIN_CACHE_DAYS:
            log(f"✅ US 재무 캐시 재사용 (나이: {age_days:.1f}일)")
            return pd.read_csv(US_FIN_CACHE, dtype={"ticker": str})
        log(f"▶ US 재무 캐시 만료 ({age_days:.1f}일 → 갱신)")

    log(f"▶ US 재무 데이터 조회 시작 ({len(tickers)}개 종목, workers={WORKERS})")
    start = time.time()
    results = []

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(_fetch_one, t): i for i, t in enumerate(tickers)}
        for cnt, future in enumerate(as_completed(futures), 1):
            results.append(future.result())
            if cnt % 50 == 0 or cnt == len(tickers):
                elapsed = time.time() - start
                speed   = cnt / elapsed if elapsed > 0 else 0
                rem     = (len(tickers) - cnt) / speed if speed > 0 else 0
                print(f"[재무 {cnt}/{len(tickers)} | {cnt/len(tickers)*100:5.1f}%] "
                      f"경과 {elapsed:5.0f}s | 남은 {rem:5.0f}s | 속도 {speed:.1f}/s")

    fin_df = pd.DataFrame(results)
    fin_df.to_csv(US_FIN_CACHE, index=False)
    log(f"✅ US 재무 캐시 저장: {US_FIN_CACHE}")
    return fin_df

# ==========================
# 멀티팩터 점수 계산 (단면 Z-score ±3 클리핑)
# ==========================
def _cz(s: pd.Series, clip: float = 3.0) -> pd.Series:
    z = (s - s.mean()) / (s.std() + 1e-8)
    return z.clip(-clip, clip)

# 섹터 중립화 임계값: 종목 수가 이보다 작으면 전체 풀 폴백
_SECTOR_MIN_N = 5

def _cz_by_sector(s: pd.Series, sector: pd.Series, min_n: int = _SECTOR_MIN_N) -> pd.Series:
    """섹터 내 Z-score. 작은 섹터·섹터 결측 시 전체 풀 Z-score로 자연 폴백."""
    if sector is None or sector.isna().all():
        return _cz(s)
    pool_z = _cz(s)
    out = pd.Series(index=s.index, dtype=float)
    for sec, idx in sector.groupby(sector).groups.items():
        sub = s.loc[idx]
        if len(sub) >= min_n and sub.std(skipna=True) > 1e-8:
            out.loc[idx] = _cz(sub)
        else:
            out.loc[idx] = pool_z.loc[idx]
    return out

def compute_us_multifactor(df: pd.DataFrame) -> pd.Series:
    """
    모멘텀(20%) + 밸류(25%) + 퀄리티ROE(25%) + 성장(15%) + 안정성(15%)
    밸류·퀄리티·안정성은 섹터 내 Z-score(섹터 중립), 모멘텀·성장은 시장 전체 Z-score.
    음수 PE/PB → 페널티(-1.0), 데이터 없음 → 중립(0)
    """
    sec = df["sector"] if "sector" in df.columns else None

    # 모멘텀 팩터 (섹터 회전 보존 → 시장 전체)
    z_mom = _cz(df["momentum"].fillna(0))

    # 밸류 팩터 (섹터 중립)
    def inv_v(x):
        if pd.isna(x): return np.nan
        return 1/x if x > 0 else -1.0

    inv_pe = df["pe"].apply(inv_v)
    inv_pb = df["pb"].apply(inv_v)
    z_val  = (_cz_by_sector(inv_pe, sec).fillna(0)
            + _cz_by_sector(inv_pb, sec).fillna(0)) / 2

    # 퀄리티 팩터 (섹터 중립, ROE: 소수점 → % 변환)
    z_quality = _cz_by_sector(df["roe"] * 100, sec).fillna(0)

    # 성장 팩터 (시장 전체)
    z_growth = (_cz(df["rev_growth"] * 100).fillna(0)
                + _cz(df["earn_growth"] * 100).fillna(0)) / 2

    # 안정성 팩터 (섹터 중립: 금융 ↔ 비금융 자본구조 차이 흡수)
    debt_filled = df["debt_equity"].fillna(df["debt_equity"].median()).fillna(100)
    z_safety = _cz_by_sector(-debt_filled, sec).fillna(0)

    return (
        0.20 * z_mom
        + 0.25 * z_val
        + 0.25 * z_quality
        + 0.15 * z_growth
        + 0.15 * z_safety
    )

# ==========================
# 점수 EMA 평탄화 + 순위 변동
# ==========================
def load_score_history() -> pd.DataFrame:
    if not os.path.exists(US_SCORE_HISTORY):
        return pd.DataFrame(columns=["date", "ticker", "raw_score"])
    return pd.read_csv(US_SCORE_HISTORY, parse_dates=["date"])

def smooth_with_ema(universe: pd.DataFrame, today: datetime) -> tuple[pd.Series, pd.DataFrame]:
    """오늘 raw_score를 history에 누적하고, 티커별 EMA 점수를 반환.

    첫 실행 시 history가 비어 있어도 EMA = raw_score 로 자연 폴백.
    같은 날 재실행해도 안전 (today 행 덮어씀).
    """
    history = load_score_history()
    today_ts = pd.Timestamp(today.date())

    today_df = pd.DataFrame({
        "date":      today_ts,
        "ticker":    universe["ticker"].values,
        "raw_score": universe["raw_score"].values,
    })

    history = history[history["date"] != today_ts]
    history = (today_df if history.empty
               else pd.concat([history, today_df], ignore_index=True))
    history = history.sort_values(["ticker", "date"])

    history["smoothed"] = history.groupby("ticker")["raw_score"].transform(
        lambda x: x.ewm(span=EMA_SPAN, adjust=False).mean()
    )

    cutoff  = today_ts - pd.Timedelta(days=HISTORY_RETAIN_D)
    history = history[history["date"] >= cutoff]

    smoothed_today = (history[history["date"] == today_ts]
                      .set_index("ticker")["smoothed"])
    return smoothed_today, history

def compute_rank_change(history: pd.DataFrame) -> dict:
    """티커별 (오늘 순위 - 어제 순위) 부호 반전. + = 상승, None = 신규."""
    if history["date"].nunique() < 2:
        return {}

    dates_sorted = sorted(history["date"].unique())
    today_d, prev_d = dates_sorted[-1], dates_sorted[-2]

    def rank_map(d):
        rows = history[history["date"] == d].sort_values("smoothed", ascending=False)
        return {t: i + 1 for i, t in enumerate(rows["ticker"].tolist())}

    today_r = rank_map(today_d)
    prev_r  = rank_map(prev_d)
    return {t: (prev_r.get(t) - r if t in prev_r else None)
            for t, r in today_r.items()}

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
# Notion 업로드
# ==========================
def upload_us_to_notion(reco_df: pd.DataFrame):
    headers = {
        "Authorization":  f"Bearer {NOTION_API_KEY}",
        "Content-Type":   "application/json",
        "Notion-Version": "2022-06-28",
    }
    today_str = datetime.now(KST).strftime("%Y-%m-%d")

    col_labels = ["랭킹", "순위변동", "전일등락(%)", "티커", "종목명", "섹터", "정배열", "멀티팩터점수",
                  "3M수익률(%)", "시가총액(B$)", "PER", "PBR",
                  "ROE(%)", "부채비율(%)"]

    def cell(text):
        return [{"type": "text", "text": {"content": str(text)}}]

    def _v(val):
        try:
            return None if (val is None or pd.isna(val)) else val
        except Exception:
            return None

    rows = [{"type": "table_row", "table_row": {"cells": [cell(c) for c in col_labels]}}]

    for rank, (_, row) in enumerate(reco_df.iterrows(), 1):
        pe_val   = _v(row.get("pe"))
        pb_val   = _v(row.get("pb"))
        roe_val  = _v(row.get("roe"))
        debt_val = _v(row.get("debt_equity"))
        mom_val  = _v(row.get("momentum"))
        mcap_val = _v(row.get("market_cap"))

        rows.append({"type": "table_row", "table_row": {"cells": [
            cell(rank),
            cell(fmt_rank_change(row.get("rank_change"))),
            cell(fmt_pct(row.get("prdy_ctrt"))),
            cell(row.get("ticker", "")),
            cell(row.get("name", "")[:30]),
            cell(row.get("sector", "")[:20]),
            cell("✅" if row.get("ma_aligned") else "-"),
            cell(f"{float(row.get('multi_score', 0)):.3f}"),
            cell(f"{float(mom_val)*100:.1f}" if mom_val is not None else "-"),
            cell(f"{float(mcap_val)/1e9:.1f}" if mcap_val else "-"),
            cell(f"{float(pe_val):.1f}"   if pe_val   is not None else "-"),
            cell(f"{float(pb_val):.2f}"   if pb_val   is not None else "-"),
            cell(f"{float(roe_val)*100:.1f}" if roe_val is not None else "-"),
            cell(f"{float(debt_val):.1f}" if debt_val is not None else "-"),
        ]}})

    body = {
        "parent":     {"page_id": NOTION_PARENT_PAGE_ID},
        "properties": {"title": {"title": [{"text": {"content": f"🇺🇸 {today_str} US 추천종목"}}]}},
        "children": [
            {
                "object": "block", "type": "heading_2",
                "heading_2": {"rich_text": [{"type": "text", "text": {
                    "content": f"S&P 500 멀티팩터 추천종목 TOP{len(reco_df)} ({today_str})"
                }}]},
            },
            {
                "object": "block", "type": "table",
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
        r = requests.post("https://api.notion.com/v1/pages", headers=headers, json=body, timeout=30)
        if r.status_code == 200:
            log(f"✅ US Notion 업로드 완료: {r.json().get('url', '')}")
        else:
            log(f"❌ US Notion 업로드 실패 ({r.status_code}): {r.text[:200]}")
    except Exception as e:
        log(f"❌ US Notion 요청 오류: {e}")

# ==========================
# 메인
# ==========================
def main():
    today_str = datetime.now(KST).strftime("%Y-%m-%d")
    log(f"▶ US 퀀트 분석 시작 ({today_str})")

    # S&P 500 종목 리스트
    sp500 = get_sp500_tickers()
    if not sp500:
        log("❌ 종목 리스트 로드 실패. 종료.")
        return

    tickers     = [s["ticker"] for s in sp500]
    name_map    = {s["ticker"]: s["name"]   for s in sp500}
    sector_map  = {s["ticker"]: s["sector"] for s in sp500}
    log(f"✅ S&P 500 종목 {len(tickers)}개 로드")

    # 가격 데이터 (모멘텀 + 정배열용 종가 데이터 동시 확보)
    momentum, prices_dict, prdy_ctrt_dict = get_price_data(tickers)

    # 재무 데이터 (캐시 or 새로 조회)
    fin_df = load_or_fetch_us_fundamentals(tickers)

    # 데이터 병합
    mom_df = pd.DataFrame([{"ticker": k, "momentum": v} for k, v in momentum.items()])
    df = fin_df.merge(mom_df, on="ticker", how="left")
    df["name"]   = df["ticker"].map(name_map)
    df["sector"] = df["ticker"].map(sector_map)

    # 시가총액 상위 N개 필터 (대형주)
    df["market_cap"] = pd.to_numeric(df["market_cap"], errors="coerce")
    universe = df.dropna(subset=["market_cap"]).nlargest(TOP_MCAP_N, "market_cap").copy()
    log(f"✅ 시총 상위 {TOP_MCAP_N}개 유니버스 확정")

    # 멀티팩터 raw 점수 계산
    universe["raw_score"] = compute_us_multifactor(universe)
    log("✅ 멀티팩터 raw 점수 계산 완료")

    # 5일 EMA 평탄화 (이력 누적, 단일 시점 노이즈 완화)
    today_dt = datetime.now(KST)
    smoothed, history_updated = smooth_with_ema(universe, today_dt)
    universe["multi_score"] = universe["ticker"].map(smoothed).fillna(universe["raw_score"])
    history_updated.to_csv(US_SCORE_HISTORY, index=False)
    n_days = history_updated["date"].nunique()
    log(f"✅ EMA 평탄화 (span={EMA_SPAN}일, 누적 이력 {n_days}일치)")

    # 어제 대비 순위 변동
    rank_changes = compute_rank_change(history_updated)

    # 상위 TOP_RECO_N
    reco_df = universe.sort_values("multi_score", ascending=False).head(TOP_RECO_N).copy()
    reco_df["rank_change"] = reco_df["ticker"].map(rank_changes)
    reco_df["prdy_ctrt"] = reco_df["ticker"].map(prdy_ctrt_dict)

    # 정배열 확인 (이미 다운로드된 가격 데이터 재사용, 추가 API 없음)
    reco_df["ma_aligned"] = reco_df["ticker"].apply(
        lambda t: is_ma_aligned(prices_dict[t]) if t in prices_dict else False
    )
    ma_cnt = reco_df["ma_aligned"].sum()
    log(f"✅ 정배열 확인 완료: {ma_cnt}/{TOP_RECO_N}개 종목")

    log(f"✅ 추천 종목 TOP {TOP_RECO_N} 선정:")
    for rank, (_, row) in enumerate(reco_df.iterrows(), 1):
        jb = "✅" if row["ma_aligned"] else "-"
        rc = fmt_rank_change(row.get("rank_change"))
        log(f"  {rank:2d}. {row['ticker']:6s} {row['name'][:28]:28s} "
            f"점수:{row['multi_score']:.3f}  3M:{row.get('momentum', 0)*100:.1f}%  "
            f"정배열:{jb}  전일:{rc}")

    # 엑셀 저장
    output_file = os.path.join(OUTPUT_DIR, f"{today_str}_US퀀트데이터.xlsx")
    reco_df.to_excel(output_file, index=False)
    log(f"🎉 엑셀 저장 완료: {output_file}")

    # Notion 업로드
    log("▶ Notion 업로드 시작")
    upload_us_to_notion(reco_df)

    log("🎉 US 퀀트 분석 완료!")

if __name__ == "__main__":
    main()
