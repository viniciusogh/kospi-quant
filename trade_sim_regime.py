"""healthy10 진입(거래대금100억↑ → 모멘텀top10% → 저변동성10) 레짐별 검증.
청산: +20%/-10% (주력) + 30일보유(참고). 단일레짐 착시 아닌지 반기별 확인.
실행: python trade_sim_regime.py
"""
import os, re
import pandas as pd, numpy as np

_DIR = os.path.dirname(os.path.abspath(__file__))
OHLC = os.path.join(_DIR, "ohlc_cache")
UNI = os.path.join(_DIR, "latest_kospi_supply.csv")
STEP, MAXHOLD, LIQ_FLOOR = 5, 60, 1e10


def excluded(name, sector):
    s, n = str(sector), str(name)
    return ("ETF" in s or "ETN" in s or "스팩" in n or bool(re.search(r"우[BC]?$", n)))


def main():
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

    entries = []  # (code, idx, regime)
    for D in anchors:
        rg = D[:4] + ("H1" if D[4:6] <= "06" else "H2")
        rows = []
        for c, p in panels.items():
            i = p["pos"].get(D)
            if i is None or i < 60 or i + 1 >= len(p["c"]):
                continue
            cl = p["c"]
            if p["v"][i-4:i+1].mean() < LIQ_FLOOR:
                continue
            dr = np.diff(cl[i-20:i+1]) / cl[i-20:i]
            rows.append((c, i, cl[i]/cl[i-60:i+1].max(), cl[i]/cl[i-19:i+1].mean()-1,
                         cl[i]/cl[i-5]-1, dr.std()))
        if len(rows) < 50:
            continue
        df = pd.DataFrame(rows, columns=["c", "i", "hi60", "disp20", "ret5", "vol20"])
        for f in ["hi60", "disp20", "ret5"]:
            df[f+"z"] = (df[f]-df[f].mean())/(df[f].std()+1e-9)
        df["score"] = df[["hi60z", "disp20z", "ret5z"]].sum(axis=1)
        decile = df.nlargest(max(10, int(len(df)*0.10)), "score")
        for _, r in decile.nsmallest(10, "vol20").iterrows():
            entries.append((r["c"], int(r["i"]), rg))

    def trade(c, e, kind):
        p = panels[c]; entry = p["c"][e]; last = len(p["c"]) - 1
        if kind == "hold30":
            te = min(e+30, last); return p["c"][te]/entry-1
        for t in range(e+1, min(e+MAXHOLD, last)+1):
            if p["h"][t] >= entry*1.20:
                return 0.20
            if p["c"][t] <= entry*0.90:
                ex = t+1 if t+1 <= last else t; return p["o"][ex]/entry-1
        te = min(e+MAXHOLD, last); return p["c"][te]/entry-1

    rec = []
    for c, e, rg in entries:
        rec.append({"regime": rg, "tp20sl10": trade(c, e, "tp"), "hold30": trade(c, e, "hold30")})
    t = pd.DataFrame(rec)
    regimes = sorted(t["regime"].unique())

    print(f"healthy10 진입 {len(t)}건\n")
    print(f"{'레짐':>8} {'건수':>5} {'+20/-10 평균%':>13} {'승률%':>6} {'30일보유 평균%':>14} {'승률%':>6}")
    for rg in regimes:
        g = t[t.regime == rg]
        print(f"{rg:>8} {len(g):>5} {g['tp20sl10'].mean()*100:>13.2f} {(g['tp20sl10']>0).mean()*100:>6.0f}"
              f" {g['hold30'].mean()*100:>14.2f} {(g['hold30']>0).mean()*100:>6.0f}")
    print(f"{'전체':>8} {len(t):>5} {t['tp20sl10'].mean()*100:>13.2f} {(t['tp20sl10']>0).mean()*100:>6.0f}"
          f" {t['hold30'].mean()*100:>14.2f} {(t['hold30']>0).mean()*100:>6.0f}")
    pos = sum(t[t.regime == rg]['tp20sl10'].mean() > 0 for rg in regimes)
    print(f"\n[+20/-10] 양수 레짐 {pos}/{len(regimes)}. 대부분 양수면 단일레짐 착시 아님 → 채택.")


if __name__ == "__main__":
    main()
