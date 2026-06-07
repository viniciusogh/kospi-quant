"""3단계 압축 기준 비교: 저변동성(현재) vs 저PBR vs 고ROE vs 콤보.
동일 풀(모멘텀 top10% ∩ 재무있음)에서 10개 선택 → +20/-10 청산 → 레짐별.
재무는 point-in-time(분기말+60일 지난 것만, lookahead 차단). FETCH=1 일때 재무 수집.
사전등록 채택조건: 평균↑ AND 7반기중≥5양수 AND 하락장(23H2·24H2) 드로다운 안 깊어짐.
실행: FETCH=1 python quality_compress.py  (첫 1회)  이후 python quality_compress.py
"""
import os, re, time
import pandas as pd, numpy as np
from concurrent.futures import ThreadPoolExecutor, as_completed
from momentum_backtest import token
from value_increment import fetch_fin, fin_asof

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

    if os.environ.get("FETCH") == "1":
        tok = token(); t0 = time.time()
        print(f"재무 수집 {len(codes)}개 (workers=2)")
        with ThreadPoolExecutor(max_workers=2) as ex:
            futs = [ex.submit(fetch_fin, c, tok) for c in codes]
            for n, _ in enumerate(as_completed(futs), 1):
                if n % 200 == 0:
                    print(f"  {n}/{len(codes)} ({time.time()-t0:.0f}s)")
        print("재무 수집 완료")

    panels, fins = {}, {}
    for c in codes:
        f = os.path.join(OHLC, f"{c}.pkl")
        if os.path.exists(f):
            d = pd.read_pickle(f)
            if d is not None and len(d) > 120:
                panels[c] = {"o": d["open"].values, "h": d["high"].values, "c": d["close"].values,
                             "v": d["value"].values, "pos": {dt: i for i, dt in enumerate(d["date"].values)},
                             "dates": d["date"].values}
                fd = fetch_fin(c, None)   # 캐시만 (None 토큰 — 캐시 있으면 반환)
                if fd is not None and len(fd):
                    fins[c] = fd
    print(f"가격 {len(panels)} · 재무 {len(fins)}")
    all_dates = sorted({dt for p in panels.values() for dt in p["dates"]})
    anchors = all_dates[60:len(all_dates) - 1:STEP]

    def regime(D): return D[:4] + ("H1" if D[4:6] <= "06" else "H2")

    def trade(c, e):
        p = panels[c]; entry = p["c"][e]; last = len(p["c"]) - 1
        for t in range(e+1, min(e+MAXHOLD, last)+1):
            if p["h"][t] >= entry*1.20: return 0.20
            if p["c"][t] <= entry*0.90:
                ex = t+1 if t+1 <= last else t; return p["o"][ex]/entry-1
        te = min(e+MAXHOLD, last); return p["c"][te]/entry-1

    methods = ["저변동성", "저PBR", "저PER", "고ROE", "콤보(vol+PBR)"]
    rec = {m: [] for m in methods}
    for D in anchors:
        rg = regime(D)
        rows = []
        for c, p in panels.items():
            i = p["pos"].get(D)
            if i is None or i < 60 or i+1 >= len(p["c"]) or p["v"][i-4:i+1].mean() < LIQ_FLOOR:
                continue
            cl = p["c"]
            rows.append((c, i, cl[i]/cl[i-60:i+1].max(), cl[i]/cl[i-19:i+1].mean()-1, cl[i]/cl[i-5]-1))
        if len(rows) < 50:
            continue
        df = pd.DataFrame(rows, columns=["c", "i", "hi60", "disp20", "ret5"])
        for f in ["hi60", "disp20", "ret5"]:
            df[f+"z"] = (df[f]-df[f].mean())/(df[f].std()+1e-9)
        df["score"] = df[["hi60z", "disp20z", "ret5z"]].sum(axis=1)
        decile = df.nlargest(max(10, int(len(df)*0.10)), "score")
        # 재무 있는 종목만 + vol20/PBR/ROE 계산 (공정 동일풀)
        cand = []
        for _, r in decile.iterrows():
            c = r["c"]; i = int(r["i"])
            if c not in fins:
                continue
            row = fin_asof(fins[c], D)
            if row is None:
                continue
            bps, roe, eps = row.get("bps"), row.get("roe"), row.get("eps")
            if not bps or bps <= 0 or pd.isna(bps) or pd.isna(roe):
                continue
            cl = panels[c]["c"]; dr = np.diff(cl[i-20:i+1])/cl[i-20:i]
            per = cl[i]/eps if (eps and eps > 0 and not pd.isna(eps)) else np.nan   # 적자=PER무의미
            cand.append({"c": c, "i": i, "vol": dr.std(), "pbr": cl[i]/bps, "per": per, "roe": roe})
        if len(cand) < 10:
            continue
        cd = pd.DataFrame(cand)
        cd["rk_vol"] = cd["vol"].rank(); cd["rk_pbr"] = cd["pbr"].rank()
        per_pool = cd.dropna(subset=["per"])
        picks = {
            "저변동성": cd.nsmallest(10, "vol"),
            "저PBR": cd.nsmallest(10, "pbr"),
            "저PER": per_pool.nsmallest(10, "per") if len(per_pool) >= 10 else cd.iloc[0:0],
            "고ROE": cd.nlargest(10, "roe"),
            "콤보(vol+PBR)": cd.assign(s=cd.rk_vol+cd.rk_pbr).nsmallest(10, "s"),
        }
        for m, pk in picks.items():
            for _, r in pk.iterrows():
                rec[m].append((rg, trade(r["c"], int(r["i"]))))

    base = pd.DataFrame(rec["저변동성"], columns=["rg", "ret"])
    regimes = sorted(base["rg"].unique())
    print(f"\n동일풀 트레이드 {len(base)}건/방식\n")
    print(f"{'압축기준':>14} {'전체평균%':>8} {'승률%':>6} {'양수레짐':>7} {'23H2%':>7} {'24H2%':>7}")
    base_mean = base["ret"].mean()
    res = {}
    for m in methods:
        t = pd.DataFrame(rec[m], columns=["rg", "ret"])
        rm = t.groupby("rg")["ret"].mean()
        res[m] = {"mean": t["ret"].mean(), "pos": (rm > 0).sum(), "nreg": len(rm),
                  "h2_23": rm.get("2023H2", np.nan), "h2_24": rm.get("2024H2", np.nan)}
        print(f"{m:>14} {t['ret'].mean()*100:>8.2f} {(t['ret']>0).mean()*100:>6.0f} "
              f"{(rm>0).sum():>4}/{len(rm)} {rm.get('2023H2',np.nan)*100:>7.2f} {rm.get('2024H2',np.nan)*100:>7.2f}")

    b = res["저변동성"]
    print(f"\n=== 사전등록 판정 (vs 저변동성 {b['mean']*100:+.2f}%, 23H2 {b['h2_23']*100:+.2f}, 24H2 {b['h2_24']*100:+.2f}) ===")
    for m in ["저PBR", "저PER", "고ROE", "콤보(vol+PBR)"]:
        r = res[m]
        c1 = r["mean"] > b["mean"]                       # 수익 개선
        c2 = r["pos"] >= 5                               # 안정성
        c3 = r["h2_23"] >= b["h2_23"] and r["h2_24"] >= b["h2_24"]  # 하락장 방어
        verdict = "채택" if (c1 and c2 and c3) else "기각"
        print(f"  {m}: 수익↑[{'O' if c1 else 'X'}] 안정[{'O' if c2 else 'X'}] 하락방어[{'O' if c3 else 'X'}] → {verdict}")
    print("\n셋 다 기각이면 저변동성 유지 (찜찜함 해소).")


if __name__ == "__main__":
    main()
