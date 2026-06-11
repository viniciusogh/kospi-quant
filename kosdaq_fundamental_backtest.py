"""코스닥 펀더멘탈 팩터 백테스트 — 밸류/퀄리티/성장/안정이 30일 수익률을 예측하나.

라이브 코스닥 모델(kosdaq.py)과 동일 팩터 정의로 검증:
  밸류=(1/PER + 1/PBR)/2, 퀄리티=ROE, 성장=(매출증가율+영익증가율)/2, 안정=-부채비율
재무비율 FHKST66430300(시장 J=코스닥도 동작) 1콜/종목 → point-in-time(분기말+60일 공시지연) 조인.
기존 kosdaq_ohlc_cache(2023~) 재활용. 수급 팩터는 별도(다음 단계).
실행: python kosdaq_fundamental_backtest.py  (첫 실행은 재무 수집 ~5분, 이후 캐시)
"""
import os, re, time, random
import pandas as pd, numpy as np
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from momentum_backtest import token, _get, BASE, APP_KEY, APP_SECRET, KST, spearman

_DIR = os.path.dirname(os.path.abspath(__file__))
OHLC = os.path.join(_DIR, "kosdaq_ohlc_cache")
FIN  = os.path.join(_DIR, "kosdaq_fin_cache")
UNI  = os.path.join(_DIR, "latest_kosdaq.csv")
FWD, STEP, LIQ_PCT, REPORT_LAG, WORKERS = 30, 5, 0.30, 60, 2


def log(m): print(f"[{datetime.now(KST):%H:%M:%S}] {m}")


def excluded(name, sector):
    s, n = str(sector), str(name)
    return ("ETF" in s or "ETN" in s or "스팩" in n or bool(re.search(r"우[BC]?$", n)))


def fetch_fin(code, tok):
    """FHKST66430300 분기 재무비율 → [stac_yymm,roe,lblt,bps,grs,eps,opg] + avail. 1콜. 캐시."""
    cache = os.path.join(FIN, f"{code}.pkl")
    if os.path.exists(cache):
        return pd.read_pickle(cache)
    url = f"{BASE}/uapi/domestic-stock/v1/finance/financial-ratio"
    hdr = {"authorization": f"Bearer {tok}", "appkey": APP_KEY, "appsecret": APP_SECRET,
           "tr_id": "FHKST66430300", "custtype": "P"}
    j = _get(url, hdr, {"FID_DIV_CLS_CODE": "1", "fid_cond_mrkt_div_code": "J",
                        "fid_input_iscd": code})
    time.sleep(random.uniform(0.2, 0.35))
    o = (j or {}).get("output", []) or []
    os.makedirs(FIN, exist_ok=True)
    if not o:
        pd.to_pickle(None, cache); return None
    df = pd.DataFrame(o)
    keep = {"stac_yymm": "stac_yymm", "roe_val": "roe", "lblt_rate": "lblt", "bps": "bps",
            "grs": "grs", "eps": "eps", "bsop_prfi_inrt": "opg"}
    df = df[[c for c in keep if c in df.columns]].rename(columns=keep)
    for c in ["roe", "lblt", "bps", "grs", "eps", "opg"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["stac_yymm"]).drop_duplicates("stac_yymm").sort_values("stac_yymm")
    df["avail"] = (pd.to_datetime(df["stac_yymm"], format="%Y%m") + pd.offsets.MonthEnd(0)
                   + pd.Timedelta(days=REPORT_LAG)).dt.strftime("%Y%m%d")
    df = df.reset_index(drop=True)
    pd.to_pickle(df, cache)
    return df


def fin_asof(fin, anchor):
    elig = fin[fin["avail"] <= anchor]
    return elig.iloc[-1] if len(elig) else None


# 단일 팩터(클수록 좋은 방향): 가치=수익률/장부수익률, 퀄=ROE, 성장, 안정=저부채
FEATS = ["ey", "by", "roe", "grs", "opg", "lowdebt"]


def main():
    meta = pd.read_csv(UNI); col = meta.columns[0]
    meta[col] = meta[col].astype(str).str.zfill(6)
    codes = meta[~meta.apply(lambda r: excluded(r["종목명"], r["섹터"]), axis=1)][col].tolist()
    codes = [c for c in codes if os.path.exists(os.path.join(OHLC, f"{c}.pkl"))]
    log(f"유니버스 {len(codes)}개 (OHLC 보유)")

    tok = token()
    fins, t0 = {}, time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(fetch_fin, c, tok): c for c in codes}
        for n, f in enumerate(as_completed(futs), 1):
            r = f.result()
            if r is not None and len(r):
                fins[futs[f]] = r
            if n % 100 == 0:
                log(f"  재무 {n}/{len(codes)} ({time.time()-t0:.0f}s)")
    log(f"재무 확보 {len(fins)}개")

    obs, panels = [], {}
    for c, fdf in fins.items():
        d = pd.read_pickle(os.path.join(OHLC, f"{c}.pkl"))
        if d is None or len(d) < 120:
            continue
        cl, val, dates = d["close"].values, d["value"].values, d["date"].values
        panels[c] = {"o": d["open"].values, "h": d["high"].values, "c": cl, "dates": dates}
        last = len(cl) - FWD - 1
        for i in range(60, last, STEP):
            px = cl[i]
            if not px or px <= 0:
                continue
            row = fin_asof(fdf, dates[i])
            if row is None:
                continue
            bps, eps = row.get("bps"), row.get("eps")
            obs.append({
                "code": c, "anchor": dates[i], "ix": i,
                "liq": val[i-19:i+1].mean(),
                "fwd": cl[i+FWD] / px - 1,
                "ey": (eps / px) if (pd.notna(eps) and eps and px) else np.nan,   # 1/PER
                "by": (bps / px) if (pd.notna(bps) and bps and px) else np.nan,   # 1/PBR
                "roe": row.get("roe", np.nan),
                "grs": row.get("grs", np.nan),
                "opg": row.get("opg", np.nan),
                "lowdebt": (-row.get("lblt")) if pd.notna(row.get("lblt")) else np.nan,
            })
    obs = pd.DataFrame(obs)
    log(f"raw obs {len(obs)}  anchor {obs['anchor'].nunique()}")

    # 유동성 컷(anchor별 하위 30%) + 레짐 라벨
    obs = obs[obs.groupby("anchor")["liq"].transform(lambda s: s >= s.quantile(LIQ_PCT))].copy()
    obs["regime"] = obs["anchor"].str[:4] + np.where(obs["anchor"].str[4:6] <= "06", "H1", "H2")
    regimes = sorted(obs["regime"].unique())
    log(f"유동성컷 후 obs {len(obs)}  anchor {obs['anchor'].nunique()}")

    # ===== 팩터별 IC (레짐별) — anchor별 spearman 후 레짐 평균 =====
    ic = (obs.melt(id_vars=["anchor", "regime", "fwd"], value_vars=FEATS,
                   var_name="f", value_name="v").dropna(subset=["v"])
             .groupby(["f", "regime", "anchor"])
             .apply(lambda g: spearman(g["v"].values, g["fwd"].values), include_groups=False)
             .rename("ic").reset_index())
    print("\n===== 코스닥 펀더멘탈 팩터별 IC_mean (30일, 레짐별) =====")
    print(ic.groupby(["f", "regime"])["ic"].mean().unstack("regime").reindex(FEATS)[regimes]
            .round(3).to_string())
    print("\n===== 전체기간 IC 요약 (mean/IR/양수레짐비율) =====")
    summ = ic.groupby("f")["ic"].agg(["mean", "std", "count"]).assign(
        IR=lambda d: d["mean"] / (d["std"] + 1e-9))
    posreg = ic.groupby(["f", "regime"])["ic"].mean().groupby("f").apply(lambda s: (s > 0).mean())
    summ["양수레짐%"] = (posreg * 100)
    print(summ.reindex(FEATS).round(3).to_string())

    # ===== 합성 펀더멘탈 점수 분위스프레드 (라이브 상대가중 밸류.30/퀄.25/성장.15/안정.10) =====
    for f in FEATS:
        obs[f + "_z"] = obs.groupby("anchor")[f].transform(
            lambda s: (s - s.mean()) / (s.std() + 1e-9)).fillna(0)
    obs["value_z"]  = (obs["ey_z"] + obs["by_z"]) / 2
    obs["growth_z"] = (obs["grs_z"] + obs["opg_z"]) / 2
    SCORES = {
        "live5(밸류.3퀄.25성장.15안정.1)": (0.30 * obs["value_z"] + 0.25 * obs["roe_z"]
                                      + 0.15 * obs["growth_z"] + 0.10 * obs["lowdebt_z"]),
        "slim3(ey+roe+저부채)": obs["ey_z"] + obs["roe_z"] + obs["lowdebt_z"],
        "slim2(ey+roe)":      obs["ey_z"] + obs["roe_z"],
    }
    for name, s in SCORES.items():
        obs[name] = s

    def spread(g, col):
        if len(g) < 20:
            return None
        hi, lo = g[col].quantile(0.9), g[col].quantile(0.1)
        return pd.Series({"top": g[g[col] >= hi]["fwd"].mean(),
                          "bot": g[g[col] <= lo]["fwd"].mean(),
                          "uni": g["fwd"].mean()})

    print("\n===== 합성점수별 분위스프레드 (상위10% - 하위10%, 30일 %) =====")
    for name in SCORES:
        pa = (obs.groupby(["regime", "anchor"])
                 .apply(lambda g: spread(g, name), include_groups=False).dropna().reset_index())
        pa["spread"] = pa["top"] - pa["bot"]
        gm = pa[["top", "bot", "spread"]].mean() * 100
        byreg = pa.groupby("regime")["spread"].mean() * 100
        print(f"\n[{name}] 스프레드={gm['spread']:.2f}%p (상위{gm['top']:.2f}/하위{gm['bot']:.2f})  "
              f"스프레드>0승률={(pa['spread']>0).mean():.0%}  양수레짐={(byreg>0).sum()}/{byreg.size}")
        print("  레짐별: " + "  ".join(f"{r}:{byreg[r]:+.1f}" for r in regimes if r in byreg.index))
    print("[해석] slim 이 live5 대비 스프레드·양수레짐 개선되면 잘듣는 2~3팩터만 남기는 게 정답.")

    # ===== 트레이드 시뮬: 다음날 시가 진입 / 고가 +20% 터치 익절 / 종가 -10% 이탈 시 다음날 시가 손절 =====
    TP, SL, MAXHOLD = 0.20, -0.10, 60

    def sim(entries):
        rr = []
        for c, e, rg in entries:
            p = panels[c]; last = len(p["c"]) - 1; s = e + 1
            if s > last:
                continue
            entry = p["o"][s]
            if not entry or entry <= 0:
                continue
            ret = None
            for t in range(s, min(s + MAXHOLD, last) + 1):
                if p["h"][t] >= entry * (1 + TP):           # 장중 +20% 터치 → 즉시 익절
                    ret = TP; break
                if p["c"][t] <= entry * (1 + SL):           # 종가 -10% 이탈 → 다음날 시가 손절
                    ex = t + 1 if t + 1 <= last else t
                    ret = p["o"][ex] / entry - 1; break
            if ret is None:                                  # 보유한도 도달 → 종가 청산
                te = min(s + MAXHOLD, last); ret = p["c"][te] / entry - 1
            rr.append((rg, ret))
        d = pd.DataFrame(rr, columns=["rg", "ret"])
        rm = d.groupby("rg")["ret"].mean()
        return d["ret"].mean() * 100, (d["ret"] > 0).mean() * 100, (rm > 0).sum(), len(rm), len(d)

    def picks(col, top):
        es = []
        for an, g in obs.groupby("anchor"):
            if len(g) < 20:
                continue
            q = g[col].quantile(0.9 if top else 0.1)
            sub = g[g[col] >= q] if top else g[g[col] <= q]
            for _, r in sub.iterrows():
                es.append((r["code"], int(r["ix"]), r["regime"]))
        return es

    print(f"\n===== 트레이드 시뮬 (다음날시가 진입 / TP+20% 터치 / SL-10% 종가→다음날시가) =====")
    print(f"{'전략':>30} {'건당%':>7} {'승률%':>6} {'양수레짐':>8} {'건수':>7}")
    for name in SCORES:
        m, w, pos, nreg, n = sim(picks(name, True))
        print(f"{name + ' 상위10%':>30} {m:>7.2f} {w:>6.0f} {pos:>5}/{nreg} {n:>7}")
    m, w, pos, nreg, n = sim(picks("slim2(ey+roe)", False))
    print(f"{'slim2 하위10%(대조)':>30} {m:>7.2f} {w:>6.0f} {pos:>5}/{nreg} {n:>7}")
    print("[해석] slim 상위10%가 live5·하위 대비 건당·양수레짐 우위면 슬림화가 정답.")

    # ===== 레짐필터: 시장모멘텀(코스닥 등가중 프록시지수, point-in-time) =====
    SLIM = "slim3(ey+roe+저부채)"
    sret, scnt = {}, {}
    for p in panels.values():
        c, dt = p["c"], p["dates"]
        r = c[1:] / c[:-1] - 1
        for k in range(len(r)):
            d = dt[k + 1]
            sret[d] = sret.get(d, 0.0) + r[k]
            scnt[d] = scnt.get(d, 0) + 1
    mdates = sorted(sret)
    lvl, x = [], 1.0
    for d in mdates:
        x *= (1 + sret[d] / scnt[d]); lvl.append(x)
    mom = {d: (lvl[i] / lvl[i - 60] - 1) for i, d in enumerate(mdates) if i >= 60}
    obs["mktmom"] = obs["anchor"].map(mom)

    print("\n===== 시장모멘텀(직전60일 %) 반기평균 — 2025 강세 확인 =====")
    print((obs.groupby("regime")["mktmom"].mean() * 100).reindex(regimes).round(2).to_string())

    aspread = (obs.groupby("anchor").apply(lambda g: spread(g, SLIM), include_groups=False)
                  .dropna().reset_index())
    aspread["spread"] = aspread["top"] - aspread["bot"]
    amom = obs.groupby("anchor")["mktmom"].first()
    aspread["mktmom"] = aspread["anchor"].map(amom)
    aspread = aspread.dropna(subset=["mktmom"])
    aspread["bucket"] = pd.qcut(aspread["mktmom"], 3, labels=["저(약세/횡보)", "중", "고(강세랠리)"])
    print("\n===== slim3 스프레드 × 시장모멘텀 3분위 (가설: 고모멘텀=펀더실패) =====")
    bt = aspread.groupby("bucket", observed=True).agg(
        스프레드_p=("spread", lambda s: s.mean() * 100),
        상위_p=("top", lambda s: s.mean() * 100),
        하위_p=("bot", lambda s: s.mean() * 100),
        anchor수=("spread", "size"),
        모멘텀평균_p=("mktmom", lambda s: s.mean() * 100))
    print(bt.round(2).to_string())

    thr = aspread["mktmom"].quantile(2 / 3)
    allowed = set(amom[amom <= thr].index)

    def picks_f(col, top, allow=None):
        es = []
        for an, g in obs.groupby("anchor"):
            if len(g) < 20 or (allow is not None and an not in allow):
                continue
            q = g[col].quantile(0.9 if top else 0.1)
            sub = g[g[col] >= q] if top else g[g[col] <= q]
            for _, r in sub.iterrows():
                es.append((r["code"], int(r["ix"]), r["regime"]))
        return es

    print(f"\n===== slim3 상위10% 시뮬: 무필터 vs 고모멘텀회피(직전60일>{thr*100:.1f}% 레짐 진입금지) =====")
    print(f"{'전략':>20} {'건당%':>7} {'승률%':>6} {'양수레짐':>8} {'건수':>7}")
    for label, allow in [("무필터", None), ("고모멘텀 회피", allowed)]:
        m, w, pos, nreg, n = sim(picks_f(SLIM, True, allow))
        print(f"{label:>20} {m:>7.2f} {w:>6.0f} {pos:>5}/{nreg} {n:>7}")
    print("[해석] 3분위가 단조(고<저)면 진짜 레짐효과(2025만 아님). 필터로 건당·양수레짐 오르면 실전가치↑.")


if __name__ == "__main__":
    main()
