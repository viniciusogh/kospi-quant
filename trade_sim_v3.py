"""진입 개선 검증: 3단계 거름망 (거래대금 100억↑ → 모멘텀 top10% → 저변동성 10).
기존 'top10 최고급등' 진입과 비교. OHLC 캐시 재활용.
가설: 모멘텀 상위군 중 저변동성 압축 = 과열 꼭지 제거 → 기존보다 나음 & 양수면 채택.
실행: python trade_sim_v3.py
"""
import os, re
import pandas as pd, numpy as np

_DIR = os.path.dirname(os.path.abspath(__file__))
OHLC = os.path.join(_DIR, "ohlc_cache")
UNI = os.path.join(_DIR, "latest_kospi_supply.csv")
STEP, MAXHOLD = 5, 60
LIQ_FLOOR = 1e10           # 5일평균 거래대금 100억원 (acml_tr_pbmn 단위=원)


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

    # 각 anchor 별 후보 테이블 (거래대금 100억 필터 후)
    pool_sizes = []
    ent = {"hottest10": [], "healthy10": [], "decile_all": []}
    for D in anchors:
        rows = []
        for c, p in panels.items():
            i = p["pos"].get(D)
            if i is None or i < 60 or i + 1 >= len(p["c"]):
                continue
            cl = p["c"]
            liq5 = p["v"][i-4:i+1].mean()
            if liq5 < LIQ_FLOOR:                      # 1단계: 거래대금 하한
                continue
            dr = np.diff(cl[i-20:i+1]) / cl[i-20:i]
            rows.append((c, i, cl[i]/cl[i-60:i+1].max(), cl[i]/cl[i-19:i+1].mean()-1,
                         cl[i]/cl[i-5]-1, dr.std()))
        if len(rows) < 50:
            continue
        df = pd.DataFrame(rows, columns=["c", "i", "hi60", "disp20", "ret5", "vol20"])
        pool_sizes.append(len(df))
        for f in ["hi60", "disp20", "ret5"]:
            df[f+"z"] = (df[f]-df[f].mean())/(df[f].std()+1e-9)
        df["score"] = df[["hi60z", "disp20z", "ret5z"]].sum(axis=1)
        n10 = max(10, int(len(df)*0.10))
        decile = df.nlargest(n10, "score")            # 2단계: 모멘텀 top10%
        for _, r in df.nlargest(10, "score").iterrows():      # 기존: 최고급등 10
            ent["hottest10"].append((r["c"], int(r["i"])))
        for _, r in decile.nsmallest(10, "vol20").iterrows():  # 3단계: 저변동성 10
            ent["healthy10"].append((r["c"], int(r["i"])))
        for _, r in decile.iterrows():                # 참고: 분위 전체
            ent["decile_all"].append((r["c"], int(r["i"])))
    print(f"평균 pool(100억↑) {np.mean(pool_sizes):.0f}개, top10%≈{np.mean(pool_sizes)*0.1:.0f}개\n")

    def sim(entries, exit_kind):
        rets = []
        for c, e in entries:
            p = panels[c]; entry = p["c"][e]; last = len(p["c"]) - 1
            ret = None
            if exit_kind == "hold30":
                te = min(e+30, last); ret = p["c"][te]/entry-1
            else:  # tp20sl10
                for t in range(e+1, min(e+MAXHOLD, last)+1):
                    if p["h"][t] >= entry*1.20:
                        ret = 0.20; break
                    if p["c"][t] <= entry*0.90:
                        ex = t+1 if t+1 <= last else t; ret = p["o"][ex]/entry-1; break
                if ret is None:
                    te = min(e+MAXHOLD, last); ret = p["c"][te]/entry-1
            rets.append(ret)
        r = np.array(rets)
        return r.mean()*100, (r > 0).mean()*100, len(r)

    print(f"{'진입방식':>12} {'청산':>10} {'평균%':>7} {'승률%':>6} {'건수':>6}")
    for em in ["hottest10", "healthy10", "decile_all"]:
        for ek in ["hold30", "tp20sl10"]:
            m, w, n = sim(ent[em], ek)
            print(f"{em:>12} {ek:>10} {m:>7.2f} {w:>6.0f} {n:>6}")
    print("\n[해석] healthy10(저변동성 압축)이 hottest10 보다 평균↑ & 양수면 → 진입 개선 채택.")


if __name__ == "__main__":
    main()
