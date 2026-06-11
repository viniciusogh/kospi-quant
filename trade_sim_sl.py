"""손절(SL) 최적화 — TP +20% 고정, SL 스윕(-5~-20% + 손절없음). 다음날 시가 진입.
healthy10 진입. 청산: 고가 +20% 도달 익절 / 종가 SL 도달 → 다음날 시가 손절 / 최대 60일.
실행: python trade_sim_sl.py
"""
import os, re
import pandas as pd, numpy as np

_DIR = os.path.dirname(os.path.abspath(__file__))
OHLC = os.path.join(_DIR, "ohlc_cache")
UNI = os.path.join(_DIR, "latest_kospi_supply.csv")
STEP, MAXHOLD, LIQ_FLOOR, TP = 5, 60, 1e10, 0.20


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
                             "l": d["low"].values, "v": d["value"].values,
                             "pos": {dt: i for i, dt in enumerate(d["date"].values)}, "dates": d["date"].values}
    all_dates = sorted({dt for p in panels.values() for dt in p["dates"]})
    anchors = all_dates[60:len(all_dates) - 2:STEP]

    entries = []
    for D in anchors:
        rows = []
        for c, p in panels.items():
            i = p["pos"].get(D)
            if i is None or i < 60 or i + 2 >= len(p["c"]) or p["v"][i-4:i+1].mean() < LIQ_FLOOR:
                continue
            cl = p["c"]; dr = np.diff(cl[i-20:i+1]) / cl[i-20:i]
            rows.append((c, i, cl[i]/cl[i-60:i+1].max(), cl[i]/cl[i-19:i+1].mean()-1, cl[i]/cl[i-5]-1, dr.std()))
        if len(rows) < 50:
            continue
        df = pd.DataFrame(rows, columns=["c", "i", "hi60", "disp20", "ret5", "vol20"])
        for f in ["hi60", "disp20", "ret5"]:
            df[f+"z"] = (df[f]-df[f].mean())/(df[f].std()+1e-9)
        df["score"] = df[["hi60z", "disp20z", "ret5z"]].sum(axis=1)
        decile = df.nlargest(max(10, int(len(df)*0.10)), "score")
        for _, r in decile.nsmallest(10, "vol20").iterrows():
            entries.append((r["c"], int(r["i"])))
    print(f"진입 {len(entries)}건 (다음날 시가 매수, TP +20% 고정)\n")

    def sim(sl):
        """sl=None 이면 손절 없음(TP+만기만)."""
        rets, outc, holds = [], [], []
        for c, e in entries:
            p = panels[c]; last = len(p["c"]) - 1; es = e + 1
            if es > last:
                continue
            entry = p["o"][es]
            if not entry or entry <= 0:
                continue
            ret, oc, hold = None, "만기", 0
            for t in range(es, min(es+MAXHOLD, last)+1):
                if p["h"][t] >= entry*(1+TP):
                    ret, oc, hold = TP, "익절", t-es; break
                if sl is not None and p["c"][t] <= entry*(1+sl):
                    ex = t+1 if t+1 <= last else t
                    ret, oc, hold = p["o"][ex]/entry-1, "손절", ex-es; break
            if ret is None:
                te = min(es+MAXHOLD, last); ret = p["c"][te]/entry-1; hold = te-es
            rets.append(ret); outc.append(oc); holds.append(hold)
        rets = np.array(rets); outc = np.array(outc)
        return (rets.mean()*100, (rets > 0).mean()*100, np.mean(holds), rets.std()*100,
                (outc == "익절").mean()*100, (outc == "손절").mean()*100, (outc == "만기").mean()*100)

    print(f"{'SL':>7} {'평균%/건':>9} {'승률%':>6} {'표준편차':>7} {'평균보유':>7} {'익절%':>6} {'손절%':>6} {'3개월':>8}")
    best = None
    for sl in [-0.05, -0.07, -0.10, -0.12, -0.15, -0.20, None]:
        m, w, h, sd, tg, st, to = sim(sl)
        m3 = ((1+m/100)**(63/h)-1)*100 if h > 0 else 0
        sll = "손절없음" if sl is None else f"{sl*100:.0f}%"
        print(f"{sll:>7} {m:>9.2f} {w:>6.0f} {sd:>7.1f} {h:>7.1f} {tg:>6.0f} {st:>6.0f} {m3:>7.1f}%")
        if best is None or m > best[1]:
            best = (sll, m)
    print(f"\n[해석] TP +20% 고정 시 평균수익 최대 = SL {best[0]} (건당 {best[1]:.2f}%). "
          "표준편차(변동성)도 함께 보고 — 수익 비슷하면 손절 타이트한 쪽이 안전.")


if __name__ == "__main__":
    main()
