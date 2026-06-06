"""2단계: 수급이 가격신호 '위에' 직교 신호를 더하는지 증분 테스트.

가격 obs(momentum_backtest 캐시 재활용) + 수급 feature(FHPTJ04160001) 결합.
핵심 질문: 가격모멘텀이 깨진 레짐(2024H2)에서 수급이 구멍을 메우나?

scope(naive): 유동 상위 SUP_N 종목 × 2024-07~현재. 수급은 1콜=30일 → 체이닝+캐시.
실행: python supply_increment.py
"""
import os, time, random, requests
import pandas as pd, numpy as np
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from momentum_backtest import (token, _get, BASE, APP_KEY, APP_SECRET, KST,
                               features_at, fetch_prices, CACHE_DIR, FWD, ANCHOR_STEP,
                               LIQ_PCT, spearman)

SUP_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "supply_cache")
UNIVERSE_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "latest_kospi_supply.csv")
SUP_N    = int(os.environ.get("SUP_N", "400"))          # 유동 상위 N (수급 fetch 비용 제한)
SUP_START = os.environ.get("SUP_START", "20240701")     # 가격실패 레짐(2024H2) 포함하도록
WORKERS  = 4


def log(m): print(f"[{datetime.now(KST):%H:%M:%S}] {m}")


def fetch_supply(code, tok, start=SUP_START):
    """FHPTJ04160001 체이닝 → start~현재 [date, net(외인+기관 순매수대금), val(거래대금)] 백만원. 캐시."""
    cache = os.path.join(SUP_CACHE, f"{code}.pkl")
    if os.path.exists(cache):
        return pd.read_pickle(cache)
    url = f"{BASE}/uapi/domestic-stock/v1/quotations/investor-trade-by-stock-daily"
    hdr = {"authorization": f"Bearer {tok}", "appkey": APP_KEY, "appsecret": APP_SECRET,
           "tr_id": "FHPTJ04160001", "custtype": "P"}
    rows, cur, empty = [], datetime.now(KST), 0
    for _ in range(30):                      # 콜 캡 (~2년+ + 빈날 후퇴 여유)
        j = _get(url, hdr, {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
                            "FID_INPUT_DATE_1": cur.strftime("%Y%m%d"),
                            "FID_ORG_ADJ_PRC": " ", "FID_ETC_CLS_CODE": " "})
        time.sleep(random.uniform(0.2, 0.35))
        o2 = (j or {}).get("output2", []) or []
        if not o2:
            if not rows and empty < 6:        # 오늘 TIME LIMIT/주말 → 며칠 후퇴
                cur -= timedelta(days=1); empty += 1; continue
            break
        empty = 0
        rows += o2
        dates = [r["stck_bsop_date"] for r in o2 if r.get("stck_bsop_date")]
        earliest = min(dates)
        if earliest <= start:
            break
        cur = datetime.strptime(earliest, "%Y%m%d") - timedelta(days=1)
    if not rows:
        pd.to_pickle(None, _ensure(cache)); return None
    df = pd.DataFrame(rows)
    df = df[["stck_bsop_date", "frgn_ntby_tr_pbmn", "orgn_ntby_tr_pbmn", "acml_tr_pbmn"]].copy()
    for c in df.columns[1:]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["net"] = df["frgn_ntby_tr_pbmn"].fillna(0) + df["orgn_ntby_tr_pbmn"].fillna(0)
    df = (df.rename(columns={"stck_bsop_date": "date", "acml_tr_pbmn": "val"})[["date", "net", "val"]]
            .drop_duplicates("date").sort_values("date").reset_index(drop=True))
    pd.to_pickle(df, _ensure(cache))
    return df


def _ensure(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def supply_features(df):
    """수급 df → date 인덱스 feature: 20일 순매수/거래대금 비, 매수우위비율."""
    if df is None or len(df) < 25:
        return None
    d = df.copy()
    d["nf20"] = d["net"].rolling(20).sum() / (d["val"].rolling(20).sum().abs() + 1e-9)
    d["nf5"]  = d["net"].rolling(5).sum() / (d["val"].rolling(5).sum().abs() + 1e-9)
    d["nf_pos20"] = (d["net"] > 0).rolling(20).mean()
    return d[["date", "nf20", "nf5", "nf_pos20"]]


PRICE_FEATS = ["hi60", "disp20", "ret5"]   # 1단계서 안정 확인된 가격 신호
SUP_FEATS   = ["nf20", "nf5", "nf_pos20"]


def main():
    tok = token()

    # 유동 상위 SUP_N (가격캐시의 최근 거래대금 기준)
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
    log(f"유동 상위 {len(sel)} 종목 수급 수집 (start={SUP_START})")

    # 수급 수집 (캐시)
    sup = {}
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(fetch_supply, c, tok): c for c, _, _ in sel}
        for n, f in enumerate(as_completed(futs), 1):
            c = futs[f]
            r = f.result()
            if r is not None:
                sup[c] = r
            if n % 50 == 0:
                log(f"  수급 {n}/{len(sel)} ({time.time()-t0:.0f}s)")
    log(f"수급 확보 {len(sup)}")

    # 가격 obs + 수급 merge
    obs = []
    pmap = {c: d for c, _, d in sel}
    for c, sdf in sup.items():
        sf = supply_features(sdf)
        if sf is None:
            continue
        sf = sf.set_index("date")
        pdf = pmap[c]
        last = len(pdf) - FWD - 1
        for i in range(60, last, ANCHOR_STEP):
            fe = features_at(pdf, i)
            if not fe:
                continue
            dt = pdf["date"].iloc[i]
            if dt not in sf.index:
                continue
            srow = sf.loc[dt]
            if srow.isna().any():
                continue
            fe.update({"code": c, "anchor": dt,
                       "nf20": srow["nf20"], "nf5": srow["nf5"], "nf_pos20": srow["nf_pos20"]})
            obs.append(fe)
    obs = pd.DataFrame(obs)
    if obs.empty:
        log("결합 obs 0 — 수급/가격 날짜 매칭 실패. 중단."); return
    obs = obs[obs.groupby("anchor")["liq"].transform(lambda s: s >= s.quantile(LIQ_PCT))].copy()
    obs["regime"] = obs["anchor"].str[:4] + np.where(obs["anchor"].str[4:6] <= "06", "H1", "H2")
    log(f"결합 obs {len(obs)}  anchor {obs['anchor'].nunique()}")

    # z-score per anchor
    allf = PRICE_FEATS + SUP_FEATS
    for f in allf:
        obs[f + "_z"] = obs.groupby("anchor")[f].transform(lambda s: (s - s.mean()) / (s.std() + 1e-9))
    obs["price_score"] = obs[[f + "_z" for f in PRICE_FEATS]].sum(axis=1)
    obs["comb_score"]  = obs["price_score"] + obs[[f + "_z" for f in SUP_FEATS]].sum(axis=1)

    regimes = sorted(obs["regime"].unique())

    # 1) 수급 단독 IC (레짐별)
    print("\n===== 수급 feature 단독 IC_mean (레짐별) =====")
    ic = (obs.melt(id_vars=["anchor", "regime", "fwd"], value_vars=SUP_FEATS,
                   var_name="f", value_name="v")
             .groupby(["f", "regime", "anchor"])
             .apply(lambda g: spearman(g["v"].values, g["fwd"].values), include_groups=False)
             .rename("ic").reset_index())
    print((ic.groupby(["f", "regime"])["ic"].mean().unstack("regime")[regimes]).round(3).to_string())

    # 2) 수급 직교성: 가격score 통제 후 수급 잔차 IC
    print("\n===== 수급 직교 IC (가격score 회귀잔차 vs fwd, 레짐별) =====")
    def resid_ic(g):
        if len(g) < 20:
            return np.nan
        x = g["price_score"].values; y = g["fwd"].values
        b = np.polyfit(x, y, 1)
        res = y - (b[0] * x + b[1])
        sup_combined = g[[f + "_z" for f in SUP_FEATS]].sum(axis=1).values
        return spearman(sup_combined, res)
    ro = obs.groupby(["regime", "anchor"]).apply(resid_ic, include_groups=False).dropna()
    print(ro.groupby("regime").mean().reindex(regimes).round(3).to_string() if len(ro) else "  표본부족")

    # 3) 분위 스프레드: price-only vs price+수급
    def spread(g, scol):
        if len(g) < 20:
            return None
        hi, lo = g[scol].quantile(0.9), g[scol].quantile(0.1)
        return g[g[scol] >= hi]["fwd"].mean() - g[g[scol] <= lo]["fwd"].mean()
    rows = []
    for (rg, an), g in obs.groupby(["regime", "anchor"]):
        sp_p, sp_c = spread(g, "price_score"), spread(g, "comb_score")
        if sp_p is not None and sp_c is not None:
            rows.append({"regime": rg, "price_only": sp_p, "price+수급": sp_c, "증분": sp_c - sp_p})
    sp = pd.DataFrame(rows)
    print("\n===== 분위 스프레드 (30일수익률 %): 가격만 vs 가격+수급 =====")
    if sp.empty:
        print("  표본부족"); return
    print((sp.groupby("regime")[["price_only", "price+수급", "증분"]].mean() * 100).round(2).to_string())
    g = sp[["price_only", "price+수급", "증분"]].mean() * 100
    print(f"\n전체: 가격만={g['price_only']:.2f}%p  가격+수급={g['price+수급']:.2f}%p  증분={g['증분']:.2f}%p")
    print("[해석] 직교IC 양수+안정 & 증분>0(특히 2024H2) 이면 수급이 진짜 추가정보.")


if __name__ == "__main__":
    main()
