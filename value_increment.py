"""3단계: 가치/퀄리티가 가격 모멘텀 '위에' 직교 신호를 더하는지 증분 테스트.

재무비율(FHKST66430300, 분기) → point-in-time 정렬(분기말+60일 지난 것만 = lookahead 방지)
가격 obs(캐시 재활용)에 결합. 핵심: momentum 깨진 2024H2 반전장을 value가 메우나?

scope: supply_increment 와 동일 유동 상위 SUP_N · 2024-07~. 재무는 종목당 1콜.
실행: python value_increment.py
"""
import os, time, random
import pandas as pd, numpy as np
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from momentum_backtest import (token, _get, BASE, APP_KEY, APP_SECRET, KST,
                               features_at, CACHE_DIR, FWD, ANCHOR_STEP, LIQ_PCT, spearman)

FIN_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fin_hist_cache")
UNIVERSE_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "latest_kospi_supply.csv")
SUP_N    = int(os.environ.get("SUP_N", "400"))
REPORT_LAG = 60         # 분기말 후 공시까지 보수적 지연(일)
WORKERS  = 2            # 재무 endpoint 레이트리밋 → 워커 낮춤


def log(m): print(f"[{datetime.now(KST):%H:%M:%S}] {m}")


def _ensure(p):
    os.makedirs(os.path.dirname(p), exist_ok=True); return p


def fetch_fin(code, tok):
    """FHKST66430300 분기 재무비율 → [stac_yymm, roe, lblt, bps, grs] 오름차순. 1콜. 캐시."""
    cache = os.path.join(FIN_CACHE, f"{code}.pkl")
    if os.path.exists(cache):
        return pd.read_pickle(cache)
    url = f"{BASE}/uapi/domestic-stock/v1/finance/financial-ratio"
    hdr = {"authorization": f"Bearer {tok}", "appkey": APP_KEY, "appsecret": APP_SECRET,
           "tr_id": "FHKST66430300", "custtype": "P"}
    j = _get(url, hdr, {"FID_DIV_CLS_CODE": "1", "fid_cond_mrkt_div_code": "J",
                        "fid_input_iscd": code})
    time.sleep(random.uniform(0.2, 0.35))
    o = (j or {}).get("output", []) or []
    if not o:
        pd.to_pickle(None, _ensure(cache)); return None
    df = pd.DataFrame(o)
    keep = {"stac_yymm": "stac_yymm", "roe_val": "roe", "lblt_rate": "lblt", "bps": "bps",
            "grs": "grs", "eps": "eps"}
    df = df[[c for c in keep if c in df.columns]].rename(columns=keep)
    for c in ["roe", "lblt", "bps", "grs", "eps"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["stac_yymm"]).drop_duplicates("stac_yymm").sort_values("stac_yymm")
    df["avail"] = (pd.to_datetime(df["stac_yymm"], format="%Y%m") + pd.offsets.MonthEnd(0)
                   + pd.Timedelta(days=REPORT_LAG)).dt.strftime("%Y%m%d")
    pd.to_pickle(df.reset_index(drop=True), _ensure(cache))
    return df.reset_index(drop=True)


def fin_asof(fin, anchor):
    """anchor(YYYYMMDD) 시점에 이미 공시된 최신 분기 행. 없으면 None."""
    elig = fin[fin["avail"] <= anchor]
    return elig.iloc[-1] if len(elig) else None


VAL_FEATS = ["val", "qual", "growth"]   # 1/PBR, ROE, 매출증가율
PRICE_Z   = ["hi60_z", "disp20_z", "ret5_z"]


def main():
    tok = token()
    uni = pd.read_csv(UNIVERSE_CSV)
    col = uni.columns[0]
    codes_all = uni[col].astype(str).str.zfill(6).tolist()
    liq = []
    for c in codes_all:
        p = os.path.join(CACHE_DIR, f"{c}.pkl")
        if os.path.exists(p):
            d = pd.read_pickle(p)
            if d is not None and len(d) > 60:
                liq.append((c, d["value"].tail(120).mean(), d))
    liq.sort(key=lambda x: x[1], reverse=True)
    sel = liq[:SUP_N]
    log(f"유동 상위 {len(sel)} 종목 재무 수집")

    fin = {}
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(fetch_fin, c, tok): c for c, _, _ in sel}
        for n, f in enumerate(as_completed(futs), 1):
            r = f.result()
            if r is not None and len(r):
                fin[futs[f]] = r
            if n % 50 == 0:
                log(f"  재무 {n}/{len(sel)} ({time.time()-t0:.0f}s)")
    log(f"재무 확보 {len(fin)}")

    pmap = {c: d for c, _, d in sel}
    obs = []
    for c, fdf in fin.items():
        pdf = pmap[c]
        last = len(pdf) - FWD - 1
        for i in range(60, last, ANCHOR_STEP):
            fe = features_at(pdf, i)
            if not fe:
                continue
            dt = pdf["date"].iloc[i]
            row = fin_asof(fdf, dt)
            if row is None:
                continue
            bps, px = row.get("bps"), pdf["close"].iloc[i]
            if not bps or bps <= 0 or pd.isna(bps):
                continue
            fe.update({"code": c, "anchor": dt,
                       "val": bps / px,                    # 1/PBR (높을수록 저평가)
                       "qual": row.get("roe", np.nan),     # ROE
                       "growth": row.get("grs", np.nan)})  # 매출증가율
            obs.append(fe)
    obs = pd.DataFrame(obs).dropna(subset=VAL_FEATS)
    if obs.empty:
        log("결합 obs 0 — 중단."); return
    obs = obs[obs.groupby("anchor")["liq"].transform(lambda s: s >= s.quantile(LIQ_PCT))].copy()
    obs["regime"] = obs["anchor"].str[:4] + np.where(obs["anchor"].str[4:6] <= "06", "H1", "H2")
    log(f"결합 obs {len(obs)}  anchor {obs['anchor'].nunique()}")

    for f in ["hi60", "disp20", "ret5"] + VAL_FEATS:
        obs[f + "_z"] = obs.groupby("anchor")[f].transform(lambda s: (s - s.mean()) / (s.std() + 1e-9))
    obs["price_score"] = obs[PRICE_Z].sum(axis=1)
    obs["comb_score"] = obs["price_score"] + obs[[f + "_z" for f in VAL_FEATS]].sum(axis=1)
    regimes = sorted(obs["regime"].unique())

    print("\n===== 가치/퀄리티 단독 IC_mean (레짐별) =====")
    ic = (obs.melt(id_vars=["anchor", "regime", "fwd"], value_vars=VAL_FEATS,
                   var_name="f", value_name="v")
             .groupby(["f", "regime", "anchor"])
             .apply(lambda g: spearman(g["v"].values, g["fwd"].values), include_groups=False)
             .rename("ic").reset_index())
    print(ic.groupby(["f", "regime"])["ic"].mean().unstack("regime")[regimes].round(3).to_string())

    print("\n===== 가치 직교 IC (가격score 회귀잔차 vs fwd, 레짐별) =====")
    def resid_ic(g):
        if len(g) < 20:
            return np.nan
        x, y = g["price_score"].values, g["fwd"].values
        b = np.polyfit(x, y, 1)
        res = y - (b[0] * x + b[1])
        return spearman(g[[f + "_z" for f in VAL_FEATS]].sum(axis=1).values, res)
    ro = obs.groupby(["regime", "anchor"]).apply(resid_ic, include_groups=False).dropna()
    print(ro.groupby("regime").mean().reindex(regimes).round(3).to_string() if len(ro) else "  표본부족")

    # ===== 가중치 스윕: score = price + w·value, 최적 w 탐색 =====
    val_sum = obs[[f + "_z" for f in VAL_FEATS]].sum(axis=1)

    def spread_series(score):
        out = {}
        obs["_s"] = score
        for (rg, an), g in obs.groupby(["regime", "anchor"]):
            if len(g) < 20:
                continue
            hi, lo = g["_s"].quantile(0.9), g["_s"].quantile(0.1)
            out[(rg, an)] = g[g["_s"] >= hi]["fwd"].mean() - g[g["_s"] <= lo]["fwd"].mean()
        return pd.Series(out)

    print("\n===== 가중치 스윕: 분위 스프레드 (price + w·value), 30일수익률 =====")
    print(f"{'w':>5} {'평균%':>7} {'IR':>6} {'최악레짐%':>9} {'2024H2%':>8} {'양수레짐':>7}")
    for w in [0.0, 0.25, 0.5, 0.75, 1.0, 1.5]:
        s = spread_series(obs["price_score"] + w * val_sum)
        df = s.rename("sp").reset_index()
        df.columns = ["regime", "anchor", "sp"]
        reg_mean = df.groupby("regime")["sp"].mean()
        ir = df["sp"].mean() / (df["sp"].std() + 1e-9)
        h2 = reg_mean.get("2024H2", np.nan)
        print(f"{w:>5.2f} {df['sp'].mean()*100:>7.2f} {ir:>6.2f} "
              f"{reg_mean.min()*100:>9.2f} {h2*100:>8.2f} {(reg_mean>0).sum():>4}/{len(reg_mean)}")
    print("\n[해석] w=0=가격만. IR↑(일관성)·최악레짐↑(방어)·2024H2↑ 되는 w 가 최적 혼합비.")


if __name__ == "__main__":
    main()
