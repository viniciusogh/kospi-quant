"""B안 검증: 적응형 IC-가중(매일 갱신) vs 고정 가중 — walk-forward.

결정시점 T 에서 '라벨 완성된 과거 anchor'(anchor+30거래일 경과)의 IC만 사용 → lookahead 차단.
적응 가중 = 후보 feature 의 트레일링 IC(음수는 0). 매 anchor 재계산 = '매일 로직 갱신'.
고정(모멘텀만)보다 실제로 나은지 비교. 캐시 재활용(API 콜 없음).
실행: python walkforward.py
"""
import os
import pandas as pd, numpy as np
from datetime import datetime, timedelta
from momentum_backtest import (token, features_at, CACHE_DIR, FWD, ANCHOR_STEP, LIQ_PCT,
                               spearman, KST)
from value_increment import fetch_fin, fin_asof, FIN_CACHE, SUP_N, UNIVERSE_CSV
from supply_increment import fetch_supply, supply_features

FEATS = ["hi60", "disp20", "ret5", "val", "qual", "growth", "nf20", "nf5", "nf_pos20"]
MOM   = ["hi60", "disp20", "ret5"]
TRAIN_N    = 60     # 트레일링 학습 anchor 수
WARMUP     = 35     # 최소 학습 anchor (이전엔 eval 안 함)
LABEL_LAG  = 46     # 30거래일 ≈ 46일. 이 날짜 지나야 라벨 완성 → 학습 사용 가능


def log(m): print(f"[{datetime.now(KST):%H:%M:%S}] {m}")


def build_obs():
    tok = token()  # 캐시 토큰 재사용
    uni = pd.read_csv(UNIVERSE_CSV)
    codes = uni[uni.columns[0]].astype(str).str.zfill(6).tolist()
    liq = []
    for c in codes:
        p = os.path.join(CACHE_DIR, f"{c}.pkl")
        if os.path.exists(p):
            d = pd.read_pickle(p)
            if d is not None and len(d) > 60:
                liq.append((c, d["value"].tail(120).mean(), d))
    liq.sort(key=lambda x: x[1], reverse=True)
    sel = liq[:SUP_N]
    rows = []
    for c, _, pdf in sel:
        fdf = fetch_fin(c, tok)
        if fdf is None or not len(fdf):
            continue
        sdf = fetch_supply(c, tok)              # 수급 캐시 (없으면 해당종목 제외 = supply 기간만)
        sf = supply_features(sdf)
        if sf is None:
            continue
        sf = sf.set_index("date")
        last = len(pdf) - FWD - 1
        for i in range(60, last, ANCHOR_STEP):
            fe = features_at(pdf, i)
            if not fe:
                continue
            dt = pdf["date"].iloc[i]
            row = fin_asof(fdf, dt)
            if row is None or dt not in sf.index:
                continue
            srow = sf.loc[dt]
            if srow.isna().any():
                continue
            bps, px = row.get("bps"), pdf["close"].iloc[i]
            if not bps or bps <= 0 or pd.isna(bps):
                continue
            q = {"hi60": fe["hi60"], "disp20": fe["disp20"], "ret5": fe["ret5"],
                 "val": bps / px, "qual": row.get("roe", np.nan), "growth": row.get("grs", np.nan),
                 "nf20": srow["nf20"], "nf5": srow["nf5"], "nf_pos20": srow["nf_pos20"],
                 "liq": fe["liq"], "fwd": fe["fwd"], "code": c, "anchor": dt}
            if any(pd.isna(v) for v in [q["qual"], q["growth"]]):
                continue
            rows.append(q)
    obs = pd.DataFrame(rows)
    obs = obs[obs.groupby("anchor")["liq"].transform(lambda s: s >= s.quantile(LIQ_PCT))].copy()
    obs["regime"] = obs["anchor"].str[:4] + np.where(obs["anchor"].str[4:6] <= "06", "H1", "H2")
    for f in FEATS:
        obs[f + "_z"] = obs.groupby("anchor")[f].transform(lambda s: (s - s.mean()) / (s.std() + 1e-9))
    return obs


def main():
    log("obs 구성 (캐시)")
    obs = build_obs()
    anchors = sorted(obs["anchor"].unique())
    log(f"obs {len(obs)}  anchor {len(anchors)}")

    # anchor 별 feature IC + 라벨완성일
    aic = {f: {} for f in FEATS}
    label_ready = {}
    for a, g in obs.groupby("anchor"):
        for f in FEATS:
            aic[f][a] = spearman(g[f].values, g["fwd"].values)
        label_ready[a] = (datetime.strptime(a, "%Y%m%d") + timedelta(days=LABEL_LAG)).strftime("%Y%m%d")

    def decile_spread(g, score):
        g = g.assign(_s=score)
        hi, lo = g["_s"].quantile(0.9), g["_s"].quantile(0.1)
        return g[g["_s"] >= hi]["fwd"].mean() - g[g["_s"] <= lo]["fwd"].mean()

    EVAL_GATE = 25   # 공통 eval: 가장 긴 창(6mo≈25anchor) 학습 가능한 anchor 만 → 동일 표본 비교

    def run(mode, window=None, signed=False):
        """mode='fixed_mom' | 'adapt'. 동일 EVAL_GATE 통과 anchor 만 평가."""
        recs, wlog = [], []
        for ti, T in enumerate(anchors):
            usable = [a for a in anchors[:ti] if label_ready[a] <= T]
            if len(usable) < EVAL_GATE:        # 공통 게이트
                continue
            g = obs[obs["anchor"] == T]
            if len(g) < 20:
                continue
            if mode == "fixed_mom":
                sc = sum(g[f + "_z"] for f in MOM)
            else:
                train = usable[-window:]
                w = {f: np.nanmean([aic[f][a] for a in train]) for f in FEATS}
                if not signed:
                    w = {f: max(0.0, v) for f, v in w.items()}
                s = sum(abs(v) for v in w.values()) or 1.0
                w = {f: v / s for f, v in w.items()}
                sc = sum(w[f] * g[f + "_z"] for f in FEATS)
                wlog.append((T, w))
            recs.append((g["regime"].iloc[0], T, decile_spread(g, sc)))
        return recs, wlog

    def stat(recs):
        df = pd.DataFrame(recs, columns=["regime", "anchor", "sp"]).dropna()
        rm = df.groupby("regime")["sp"].mean()
        return {"평균%": df["sp"].mean()*100, "IR": df["sp"].mean()/(df["sp"].std()+1e-9),
                "최악레짐%": rm.min()*100, "양수레짐": f"{(rm>0).sum()}/{len(rm)}",
                "승률": f"{(df['sp']>0).mean():.0%}", "_ir": df["sp"].mean()/(df["sp"].std()+1e-9),
                "_worst": rm.min()}

    schemes = {
        "고정(모멘텀)":       run("fixed_mom")[0],
        "적응9 6mo·clip":    run("adapt", window=25, signed=False)[0],
        "적응9 6mo·signed":  run("adapt", window=25, signed=True)[0],
        "적응9 3mo·signed":  run("adapt", window=12, signed=True)[0],
    }
    print("\n===== 사전등록 walk-forward · 9피처(가격+가치+수급) 풀 (공통 eval, 30일수익률) =====")
    print(f"{'scheme':>17} {'평균%':>7} {'IR':>6} {'최악레짐%':>9} {'양수레짐':>7} {'승률':>6}")
    S = {}
    for name, r in schemes.items():
        s = stat(r); S[name] = s
        print(f"{name:>17} {s['평균%']:>7.2f} {s['IR']:>6.2f} {s['최악레짐%']:>9.2f} {s['양수레짐']:>7} {s['승률']:>6}")

    base = S["고정(모멘텀)"]
    print("\n===== 사전등록 판정 (적응9 변형 중 하나라도 IR↑ AND 최악레짐↑ 면 재고) =====")
    any_win = False
    for name in ["적응9 6mo·clip", "적응9 6mo·signed", "적응9 3mo·signed"]:
        ir_win = S[name]["_ir"] > base["_ir"]
        w_win = S[name]["_worst"] > base["_worst"]
        any_win = any_win or (ir_win and w_win)
        print(f"  {name}: IR {S[name]['_ir']:.2f}{'>' if ir_win else '<='}{base['_ir']:.2f} "
              f"[{'O' if ir_win else 'X'}]  최악 {S[name]['_worst']*100:.2f}"
              f"{'>' if w_win else '<='}{base['_worst']*100:.2f} [{'O' if w_win else 'X'}]")
    print(f"\n  >>> 판정: {'적응형 재고 (수급 포함이 이김)' if any_win else '적응형 최종기각 → A 확정, 적응 탐색 종료'}")


if __name__ == "__main__":
    main()
