"""청산룰 비교 (OHLC 캐시 재활용). 동일 진입(top10 모멘텀, 5거래일마다) →
이론 기반 청산룰 6종 비교. '제일 높은 거 고르기' 아니라 원리로 판단.
실행: python trade_sim_compare.py
"""
import os, re
import pandas as pd, numpy as np

_DIR = os.path.dirname(os.path.abspath(__file__))
OHLC = os.path.join(_DIR, "ohlc_cache")
UNI = os.path.join(_DIR, "latest_kospi_supply.csv")
TOPN, STEP, LIQ_PCT, MAXHOLD = 10, 5, 0.30, 60


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
    print(f"유니버스 {len(panels)}개")
    all_dates = sorted({dt for p in panels.values() for dt in p["dates"]})
    anchors = all_dates[60:len(all_dates) - 1:STEP]

    # 진입 목록 (code, entry_idx) — 모든 룰 공통
    entries = []
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
            entries.append((row["c"], int(row["i"])))
    print(f"진입 {len(entries)}건\n")

    def sim(rule):
        rets, holds = [], []
        for c, e in entries:
            p = panels[c]; entry = p["c"][e]; last = len(p["c"]) - 1; peak = entry
            ret, hold = None, 0
            for t in range(e+1, min(e+MAXHOLD, last)+1):
                hi, cl = p["h"][t], p["c"][t]
                peak = max(peak, cl)
                r = rule(entry, hi, cl, peak, t-e)
                if r is not None:                       # r = (exit_type,)  -> 청산 신호
                    ex = t+1 if r == "open" and t+1 <= last else t
                    px = p["o"][ex] if r == "open" else (entry*1.20 if r == "tp20" else
                          entry*1.30 if r == "tp30" else p["c"][t])
                    ret, hold = px/entry-1, ex-e; break
            if ret is None:
                te = min(e+MAXHOLD, last); ret = p["c"][te]/entry-1; hold = te-e
            rets.append(ret); holds.append(hold)
        rets = np.array(rets)
        return rets.mean()*100, np.median(rets)*100, (rets > 0).mean()*100, np.mean(holds)

    # 청산룰 정의: 신호면 'open'(다음날시가)/'tp20'/'tp30'(목표가체결)/'close' 반환, 아니면 None
    def hold30(en, hi, cl, pk, d): return "close" if d >= 30 else None
    def hold60(en, hi, cl, pk, d): return None    # 만기까지
    def sl10(en, hi, cl, pk, d): return "open" if cl <= en*0.90 else None
    def trail15(en, hi, cl, pk, d): return "open" if cl <= pk*0.85 else None
    def tp20sl10(en, hi, cl, pk, d):
        if hi >= en*1.20: return "tp20"
        if cl <= en*0.90: return "open"
        return None
    def tp30sl10(en, hi, cl, pk, d):
        if hi >= en*1.30: return "tp30"
        if cl <= en*0.90: return "open"
        return None

    rules = [("30일 보유", hold30), ("60일 보유", hold60), ("손절-10%만", sl10),
             ("트레일링-15%", trail15), ("+20%/-10%(현재)", tp20sl10), ("+30%/-10%", tp30sl10)]
    print(f"{'청산룰':>16} {'평균%':>7} {'중앙%':>7} {'승률%':>6} {'평균보유':>7}")
    for name, fn in rules:
        m, md, w, h = sim(fn)
        print(f"{name:>16} {m:>7.2f} {md:>7.2f} {w:>6.0f} {h:>7.1f}")
    print("\n[해석] 승자 달리게(손절만/트레일링)가 +20%캡보다 나으면 = 모멘텀 정석 확인.")


if __name__ == "__main__":
    main()
