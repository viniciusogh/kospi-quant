"""익절(TP) 최적화 — 실제 거래패턴 반영: 신호 당일 종가로 선정 → '다음날 시가' 매수.
SL -10% 고정, TP를 5~20% 스윕. healthy10 진입(거래대금100억→모멘텀top10%→저변동성10).
청산: 보유 중 고가가 진입가*(1+TP) 도달 → 익절 / 종가 -10% → 다음날 시가 손절 / 최대 60일.
실행: python trade_sim_tp.py
"""
import os, re
import pandas as pd, numpy as np

_DIR = os.path.dirname(os.path.abspath(__file__))
OHLC = os.path.join(_DIR, "ohlc_cache")
UNI = os.path.join(_DIR, "latest_kospi_supply.csv")
STEP, MAXHOLD, LIQ_FLOOR, SL = 5, 60, 1e10, -0.10


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
    anchors = all_dates[60:len(all_dates) - 2:STEP]    # -2: 다음날 진입 + 청산 여유

    # healthy10 진입 (code, signal_idx e). 진입은 e+1 시가.
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
    print(f"진입 {len(entries)}건 (신호 다음날 시가 매수, SL {SL*100:.0f}% 고정)\n")

    def sim(tp):
        """tp=None 이면 익절 없음(SL+만기만)."""
        rets, outc, holds = [], [], []
        for c, e in entries:
            p = panels[c]; last = len(p["c"]) - 1
            es = e + 1                      # 다음날 진입
            if es > last:
                continue
            entry = p["o"][es]              # 다음날 시가
            if not entry or entry <= 0:
                continue
            ret, oc, hold = None, "만기", 0
            for t in range(es, min(es+MAXHOLD, last)+1):
                if tp is not None and p["h"][t] >= entry*(1+tp):   # 장중 +TP 도달 → 익절
                    ret, oc, hold = tp, "익절", t-es; break
                if p["c"][t] <= entry*(1+SL):           # 종가 -10% → 다음날 시가 손절
                    ex = t+1 if t+1 <= last else t
                    ret, oc, hold = p["o"][ex]/entry-1, "손절", ex-es; break
            if ret is None:
                te = min(es+MAXHOLD, last); ret = p["c"][te]/entry-1; hold = te-es
            rets.append(ret); outc.append(oc); holds.append(hold)
        rets = np.array(rets); outc = np.array(outc)
        return (rets.mean()*100, (rets > 0).mean()*100, np.mean(holds),
                (outc == "익절").mean()*100, (outc == "손절").mean()*100, (outc == "만기").mean()*100)

    print(f"{'TP':>7} {'평균%/건':>9} {'승률%':>6} {'평균보유':>7} {'익절%':>6} {'손절%':>6} {'만기%':>6} {'3개월환산':>9}")
    best = None
    for tp in [0.05, 0.10, 0.15, 0.20, 0.25, 0.30, None]:
        m, w, h, tg, st, to = sim(tp)
        tpl = "익절없음" if tp is None else f"{tp*100:.0f}%"
        m3 = ((1+m/100)**(63/h)-1)*100 if h > 0 else 0
        print(f"{tpl:>7} {m:>9.2f} {w:>6.0f} {h:>7.1f} {tg:>6.0f} {st:>6.0f} {to:>6.0f} {m3:>8.1f}%")
        if best is None or m > best[1]:
            best = (tpl, m)
    print(f"\n[해석] SL -10% 고정 시 평균수익 최대 = TP {best[0]} (건당 {best[1]:.2f}%). "
          "단 백테스트 최적 ≠ 미래 최적(과적합 주의), 곡선 모양으로 판단.")


if __name__ == "__main__":
    main()
