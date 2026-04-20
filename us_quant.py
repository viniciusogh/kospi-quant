import os
import time
import requests
import pandas as pd
from io import StringIO
import numpy as np
import yfinance as yf
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

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

# ==========================
# 유틸
# ==========================
def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

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
# 3개월 가격 모멘텀 (yfinance 일괄 다운로드)
# ==========================
def get_price_momentum(tickers: list[str]) -> dict:
    """yf.download()로 전 종목 3개월 수익률 일괄 계산"""
    log(f"▶ 가격 모멘텀 다운로드 시작 ({len(tickers)}개 종목)")
    try:
        raw = yf.download(
            tickers, period="4mo", interval="1d",
            progress=False, auto_adjust=True,
            group_by="ticker", threads=True,
        )
        momentum = {}
        # multi-ticker: raw[ticker]['Close']
        for t in tickers:
            try:
                if len(tickers) == 1:
                    prices = raw['Close'].dropna()
                else:
                    prices = raw[t]['Close'].dropna()
                if len(prices) >= 60:
                    ret = float(prices.iloc[-1] / prices.iloc[-63] - 1)
                    momentum[t] = ret
            except Exception:
                pass
        log(f"✅ 모멘텀 계산 완료: {len(momentum)}개 종목")
        return momentum
    except Exception as e:
        log(f"❌ 가격 다운로드 실패: {e}")
        return {}

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

def compute_us_multifactor(df: pd.DataFrame) -> pd.Series:
    """
    모멘텀(25%) + 밸류(20%) + 퀄리티ROE(25%) + 성장(15%) + 안정성(15%)
    음수 PE/PB → 페널티(-1.0), 데이터 없음 → 중립(0)
    """
    # 모멘텀 팩터
    z_mom = _cz(df["momentum"].fillna(0))

    # 밸류 팩터
    def inv_v(x):
        if pd.isna(x): return np.nan
        return 1/x if x > 0 else -1.0

    inv_pe = df["pe"].apply(inv_v)
    inv_pb = df["pb"].apply(inv_v)
    z_val  = (_cz(inv_pe).fillna(0) + _cz(inv_pb).fillna(0)) / 2

    # 퀄리티 팩터 (ROE: 소수점 → % 변환)
    z_quality = _cz(df["roe"] * 100).fillna(0)

    # 성장 팩터
    z_growth = (_cz(df["rev_growth"] * 100).fillna(0)
                + _cz(df["earn_growth"] * 100).fillna(0)) / 2

    # 안정성 팩터
    debt_filled = df["debt_equity"].fillna(df["debt_equity"].median()).fillna(100)
    z_safety = _cz(-debt_filled).fillna(0)

    return (
        0.25 * z_mom
        + 0.20 * z_val
        + 0.25 * z_quality
        + 0.15 * z_growth
        + 0.15 * z_safety
    )

# ==========================
# Notion 업로드
# ==========================
def upload_us_to_notion(reco_df: pd.DataFrame):
    headers = {
        "Authorization":  f"Bearer {NOTION_API_KEY}",
        "Content-Type":   "application/json",
        "Notion-Version": "2022-06-28",
    }
    today_str = datetime.today().strftime("%Y-%m-%d")

    col_labels = ["랭킹", "티커", "종목명", "섹터", "멀티팩터점수",
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
            cell(row.get("ticker", "")),
            cell(row.get("name", "")[:30]),
            cell(row.get("sector", "")[:20]),
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
    today_str = datetime.today().strftime("%Y-%m-%d")
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

    # 가격 모멘텀 (일괄 다운로드)
    momentum = get_price_momentum(tickers)

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

    # 멀티팩터 점수 계산
    universe["multi_score"] = compute_us_multifactor(universe)
    log("✅ 멀티팩터 점수 계산 완료")

    # 상위 TOP_RECO_N
    reco_df = universe.sort_values("multi_score", ascending=False).head(TOP_RECO_N)
    log(f"✅ 추천 종목 TOP {TOP_RECO_N} 선정:")
    for rank, (_, row) in enumerate(reco_df.iterrows(), 1):
        log(f"  {rank:2d}. {row['ticker']:6s} {row['name'][:28]:28s} "
            f"점수:{row['multi_score']:.3f}  3M:{row.get('momentum', 0)*100:.1f}%")

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
