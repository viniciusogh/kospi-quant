"""손절/익절 전략 백테스트 v2 (OHLC 정밀). 장중 고가로 +20% 즉시 익절,
종가 -10% → 다음날 시가 손절. OHLC 재수집(ohlc_cache/) 후 시뮬.

전략: 5거래일마다 모멘텀 top-N 진입(진입가=당일 종가).
 - 보유 중 어느 날 고가 ≥ +20% → 그날 +20%에 익절 (시장가, 슬리피지 無 가정)
 - 종가 ≤ -10% → 다음날 시가에 손절
 - 둘 다 없으면 MAXHOLD 거래일 후 종가 청산
실행: python trade_sim_v2.py        (수집은 FETCH=1 일 때만)
"""
import os, re, time, random, requests
import pandas as pd, numpy as np
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from momentum_backtest import token, _get, BASE, APP_KEY, APP_SECRET, KST

_DIR = os.path.dirname(os.path.abspath(__file__))
OHLC = os.path.join(_DIR, "ohlc_cache")
UNI = os.path.join(_DIR, "latest_kospi_supply.csv")
TOPN, STEP, LIQ_PCT = 10, 5, 0.30
TARGET, STOP, MAXHOLD = 0.20, -0.10, 60


def log(m): print(f"[{datetime.now(KST):%H:%M:%S}] {m}")


def excluded(name, sector):
    s, n = str(sector), str(name)
    return ("ETF" in s or "ETN" in s or "스팩" in n or bool(re.search(r"우[BC]?$", n)))


def fetch_ohlc(code, tok):
    """2023~현재 OHLCV 체이닝. [date,open,high,low,close,value] 오름차순. 캐시."""
    cache = os.path.join(OHLC, f"{code}.pkl")
    if os.path.exists(cache):
        return pd.read_pickle(cache)
    url = f"{BASE}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
    hdr = {"authorization": f"Bearer {tok}", "appkey": APP_KEY, "appsecret": APP_SECRET,
           "tr_id": "FHKST03010100", "custtype": "P"}
    today = datetime.now(KST); start = datetime(2023, 1, 1, tzinfo=KST)
    rows, d2 = [], today
    while d2 > start:
        d1 = d2 - timedelta(days=150)
        j = _get(url, hdr, {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
                            "FID_INPUT_DATE_1": max(d1, start).strftime("%Y%m%d"),
                            "FID_INPUT_DATE_2": d2.strftime("%Y%m%d"),
                            "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0"})
        time.sleep(random.uniform(0.2, 0.35))
        if j and j.get("rt_cd") == "0":
            for r in j.get("output2", []) or []:
                if r.get("stck_clpr") and r["stck_clpr"] != "0":
                    rows.append({"date": r["stck_bsop_date"], "open": float(r["stck_oprc"]),
                                 "high": float(r["stck_hgpr"]), "low": float(r["stck_lwpr"]),
                                 "close": float(r["stck_clpr"]), "value": float(r.get("acml_tr_pbmn", 0) or 0)})
        d2 = d1 - timedelta(days=1)
    df = pd.DataFrame(rows).drop_duplicates("date").sort_values("date").reset_index(drop=True) if rows else None
    os.makedirs(OHLC, exist_ok=True); pd.to_pickle(df, cache)
    return df if df is not None and len(df) > 120 else None


def collect(codes):
    tok = token()
    log(f"OHLC 수집 {len(codes)}개 (캐시 재활용)")
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = [ex.submit(fetch_ohlc, c, tok) for c in codes]
        for n, _ in enumerate(as_completed(futs), 1):
            if n % 200 == 0:
                log(f"  {n}/{len(codes)} ({time.time()-t0:.0f}s)")
    log("OHLC 수집 완료")


def main():
    meta = pd.read_csv(UNI)
    col = meta.columns[0]; meta[col] = meta[col].astype(str).str.zfill(6)
    codes = meta[~meta.apply(lambda r: excluded(r["종목명"], r["섹터"]), axis=1)][col].tolist()

    if os.environ.get("FETCH") == "1":
        collect(codes)

    panels = {}
    for c in codes:
        f = os.path.join(OHLC, f"{c}.pkl")
        if os.path.exists(f):
            d = pd.read_pickle(f)
            if d is not None and len(d) > 120:
                panels[c] = {"o": d["open"].values, "h": d["high"].values, "l": d["low"].values,
                             "c": d["close"].values, "v": d["value"].values,
                             "pos": {dt: i for i, dt in enumerate(d["date"].values)},
                             "dates": d["date"].values}
    log(f"유니버스 {len(panels)}개")
    if len(panels) < 50:
        log("OHLC 캐시 부족 → FETCH=1 로 먼저 수집 필요"); return

    all_dates = sorted({dt for p in panels.values() for dt in p["dates"]})
    anchors = all_dates[60:len(all_dates) - 1:STEP]

    trades = []
    for D in anchors:
        feats = []
        for c, p in panels.items():
            i = p["pos"].get(D)
            if i is None or i < 60 or i + 1 >= len(p["c"]):
                continue
            cl = p["c"]
            feats.append((c, i, cl[i]/cl[i-60:i+1].max(), cl[i]/cl[i-19:i+1].mean()-1,
                          cl[i]/cl[i-5]-1, p["v"][i-19:i+1].mean()))
        if len(feats) < 50:
            continue
        df = pd.DataFrame(feats, columns=["c", "i", "hi60", "disp20", "ret5", "liq"])
        df = df[df["liq"] >= df["liq"].quantile(LIQ_PCT)]
        for f in ["hi60", "disp20", "ret5"]:
            df[f+"z"] = (df[f]-df[f].mean())/(df[f].std()+1e-9)
        df["score"] = df[["hi60z", "disp20z", "ret5z"]].sum(axis=1)
        for _, row in df.nlargest(TOPN, "score").iterrows():
            p = panels[row["c"]]; e = int(row["i"]); entry = p["c"][e]; last = len(p["c"])-1
            outcome, ret, hold = "timeout", None, 0
            for t in range(e+1, min(e+MAXHOLD, last)+1):
                if p["h"][t]/entry - 1 >= TARGET:                 # 장중 +20% 도달 → 익절
                    outcome, ret, hold = "target", TARGET, t-e; break
                if p["c"][t]/entry - 1 <= STOP:                   # 종가 -10% → 다음날 시가 손절
                    ex = t+1 if t+1 <= last else t
                    outcome, ret, hold = "stop", p["o"][ex]/entry-1, ex-e; break
            if ret is None:
                te = min(e+MAXHOLD, last); ret = p["c"][te]/entry-1; hold = te-e
            te = min(e+30, last); base30 = p["c"][te]/entry-1
            trades.append((ret, outcome, hold, base30))

    t = pd.DataFrame(trades, columns=["ret", "outcome", "hold", "base30"])
    log(f"트레이드 {len(t)}건")
    print("\n=== 전략 v2 (장중+20% 익절 / 종가-10%→다음날 시가 손절 / 최대60) ===")
    print(f"  평균 {t['ret'].mean()*100:+.2f}%/건  중앙값 {t['ret'].median()*100:+.2f}%  승률 {(t['ret']>0).mean():.0%}  평균보유 {t['hold'].mean():.1f}일")
    oc = t["outcome"].value_counts(normalize=True)
    print(f"  익절 {oc.get('target',0):.0%}(+20%) / 손절 {oc.get('stop',0):.0%}({t[t.outcome=='stop']['ret'].mean()*100:+.1f}%) / 만기 {oc.get('timeout',0):.0%}({t[t.outcome=='timeout']['ret'].mean()*100:+.1f}%)")
    print(f"=== baseline (규칙없이 30일 보유) : {t['base30'].mean()*100:+.2f}%/건  승률 {(t['base30']>0).mean():.0%} ===")
    avg, hold = t["ret"].mean(), t["hold"].mean()
    print(f"\n[100만원] 건당 {avg*100:+.2f}%, 평균보유 {hold:.0f}일 → 3개월 ~{((1+avg)**(63/hold)-1)*100:+.0f}% (복리·비용無·낙관)")


if __name__ == "__main__":
    main()
