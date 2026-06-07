"""추세 오버레이 백테스트: KOSPI 지수가 상승추세(200/120일선 위)일 때만 healthy10 진입.
롱온리 약점(하락장)을 시장 on/off 스위치로 방어하는지 검증. OHLC 캐시 재활용.
사전등록: 하락장(23H2·24H2) 개선 AND 전체 안 망가지면 → 오버레이 채택.
실행: python trend_overlay.py
"""
import os, re, time, random, requests
import pandas as pd, numpy as np
from datetime import datetime, timedelta
from momentum_backtest import token, _get, BASE, APP_KEY, APP_SECRET, KST

_DIR = os.path.dirname(os.path.abspath(__file__))
OHLC = os.path.join(_DIR, "ohlc_cache")
UNI = os.path.join(_DIR, "latest_kospi_supply.csv")
IDX_CACHE = os.path.join(_DIR, "kospi_index.pkl")
STEP, MAXHOLD, LIQ_FLOOR = 5, 60, 1e10


def excluded(name, sector):
    s, n = str(sector), str(name)
    return ("ETF" in s or "ETN" in s or "스팩" in n or bool(re.search(r"우[BC]?$", n)))


def fetch_index(tok):
    if os.path.exists(IDX_CACHE):
        return pd.read_pickle(IDX_CACHE)
    url = f"{BASE}/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice"
    hdr = {"authorization": f"Bearer {tok}", "appkey": APP_KEY, "appsecret": APP_SECRET,
           "tr_id": "FHKUP03500100", "custtype": "P"}
    today = datetime.now(KST); start = datetime(2022, 1, 1, tzinfo=KST)  # MA200 위해 1년 더
    rows, d2 = [], today
    while d2 > start:
        d1 = d2 - timedelta(days=60)        # 콜당 ~50행 한도 → 윈도 좁혀 구멍 방지
        j = _get(url, hdr, {"FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": "0001",
                            "FID_INPUT_DATE_1": max(d1, start).strftime("%Y%m%d"),
                            "FID_INPUT_DATE_2": d2.strftime("%Y%m%d"), "FID_PERIOD_DIV_CODE": "D"})
        time.sleep(random.uniform(0.2, 0.3))
        if j and j.get("rt_cd") == "0":
            for r in j.get("output2", []) or []:
                if r.get("bstp_nmix_prpr"):
                    rows.append({"date": r["stck_bsop_date"], "close": float(r["bstp_nmix_prpr"])})
        d2 = d1 - timedelta(days=1)
    idx = pd.DataFrame(rows).drop_duplicates("date").sort_values("date").reset_index(drop=True)
    idx["ma200"] = idx["close"].rolling(200).mean()
    idx["ma120"] = idx["close"].rolling(120).mean()
    pd.to_pickle(idx, IDX_CACHE)
    return idx


def main():
    tok = token()
    idx = fetch_index(tok)
    idx_pos = {d: i for i, d in enumerate(idx["date"].values)}
    print(f"KOSPI 지수 {len(idx)}일 ({idx['date'].iloc[0]}~{idx['date'].iloc[-1]})")

    meta = pd.read_csv(UNI); col = meta.columns[0]; meta[col] = meta[col].astype(str).str.zfill(6)
    codes = meta[~meta.apply(lambda r: excluded(r["종목명"], r["섹터"]), axis=1)][col].tolist()
    panels = {}
    for c in codes:
        f = os.path.join(OHLC, f"{c}.pkl")
        if os.path.exists(f):
            d = pd.read_pickle(f)
            if d is not None and len(d) > 120:
                panels[c] = {"o": d["open"].values, "h": d["high"].values, "c": d["close"].values,
                             "v": d["value"].values, "pos": {dt: i for i, dt in enumerate(d["date"].values)},
                             "dates": d["date"].values}
    all_dates = sorted({dt for p in panels.values() for dt in p["dates"]})
    anchors = all_dates[60:len(all_dates) - 1:STEP]

    def trade(c, e):
        p = panels[c]; entry = p["c"][e]; last = len(p["c"]) - 1
        for t in range(e+1, min(e+MAXHOLD, last)+1):
            if p["h"][t] >= entry*1.20: return 0.20
            if p["c"][t] <= entry*0.90:
                ex = t+1 if t+1 <= last else t; return p["o"][ex]/entry-1
        te = min(e+MAXHOLD, last); return p["c"][te]/entry-1

    def trend(D):
        i = idx_pos.get(D)
        if i is None or pd.isna(idx["ma200"].iloc[i]):
            return None
        return (idx["close"].iloc[i] >= idx["ma200"].iloc[i], idx["close"].iloc[i] >= idx["ma120"].iloc[i])

    rec = {"전체(필터X)": [], "MA200 위": [], "MA120 위": []}
    up200 = up_total = 0
    for D in anchors:
        tr = trend(D)
        rows = []
        for c, p in panels.items():
            i = p["pos"].get(D)
            if i is None or i < 60 or i+1 >= len(p["c"]) or p["v"][i-4:i+1].mean() < LIQ_FLOOR:
                continue
            cl = p["c"]; dr = np.diff(cl[i-20:i+1])/cl[i-20:i]
            rows.append((c, i, cl[i]/cl[i-60:i+1].max(), cl[i]/cl[i-19:i+1].mean()-1, cl[i]/cl[i-5]-1, dr.std()))
        if len(rows) < 50:
            continue
        df = pd.DataFrame(rows, columns=["c", "i", "hi60", "disp20", "ret5", "vol20"])
        for f in ["hi60", "disp20", "ret5"]:
            df[f+"z"] = (df[f]-df[f].mean())/(df[f].std()+1e-9)
        df["score"] = df[["hi60z", "disp20z", "ret5z"]].sum(axis=1)
        picks = df.nlargest(max(10, int(len(df)*0.10)), "score").nsmallest(10, "vol20")
        rg = D[:4] + ("H1" if D[4:6] <= "06" else "H2")
        if tr is not None:
            up_total += 1
            if tr[0]:
                up200 += 1
        for _, r in picks.iterrows():
            ret = trade(r["c"], int(r["i"]))
            rec["전체(필터X)"].append((rg, ret))
            if tr is not None and tr[0]:
                rec["MA200 위"].append((rg, ret))
            if tr is not None and tr[1]:
                rec["MA120 위"].append((rg, ret))

    print(f"\nMA200 위 진입비율 {up200/max(up_total,1):.0%} (나머지는 현금)\n")
    regimes = sorted({rg for rg, _ in rec["전체(필터X)"]})
    print(f"{'전략':>12} {'전체평균%':>8} {'승률%':>6} {'양수레짐':>7} {'23H2%':>7} {'24H2%':>7} {'건수':>6}")
    for name in ["전체(필터X)", "MA200 위", "MA120 위"]:
        t = pd.DataFrame(rec[name], columns=["rg", "ret"])
        rm = t.groupby("rg")["ret"].mean()
        print(f"{name:>12} {t['ret'].mean()*100:>8.2f} {(t['ret']>0).mean()*100:>6.0f} "
              f"{(rm>0).sum():>4}/{len(rm)} {rm.get('2023H2',np.nan)*100:>7.2f} {rm.get('2024H2',np.nan)*100:>7.2f} {len(t):>6}")
    print("\n[해석] MA200/120 필터가 23H2·24H2 개선 & 전체 안 망가지면 → 추세 오버레이 채택.")


if __name__ == "__main__":
    main()
