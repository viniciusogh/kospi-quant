"""손절/익절 전략 백테스트 v1 (종가 근사). price_cache 재활용, API 콜 없음.

전략: 5거래일마다 모멘텀 top-N 진입 → +20% 도달 시 그날 종가 익절,
-10% 도달 시 다음날 종가 손절(시가 근사), 최대 MAXHOLD 보유 후 종가 청산.
비교: 같은 진입을 규칙 없이 30거래일 고정보유(baseline).
⚠️ 종가 근사 — 장중 고가/시가 미반영이라 실제와 차이 있음 (v2서 OHLC 정밀).
"""
import os, re
import pandas as pd, numpy as np

_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(_DIR, "price_cache")
UNI = os.path.join(_DIR, "latest_kospi_supply.csv")
TOPN, STEP, LIQ_PCT = 10, 5, 0.30
TARGET, STOP, MAXHOLD = 0.20, -0.10, 60


def excluded(name, sector):
    s, n = str(sector), str(name)
    return ("ETF" in s or "ETN" in s or "스팩" in n or bool(re.search(r"우[BC]?$", n)))


def main():
    meta = pd.read_csv(UNI)
    col = meta.columns[0]
    meta[col] = meta[col].astype(str).str.zfill(6)
    ok = set(meta[~meta.apply(lambda r: excluded(r["종목명"], r["섹터"]), axis=1)][col])

    panels = {}
    for f in os.listdir(CACHE):
        c = f[:-4]
        if c not in ok:
            continue
        d = pd.read_pickle(os.path.join(CACHE, f))
        if d is not None and len(d) > 120:
            panels[c] = {"close": d["close"].values, "value": d["value"].values,
                         "pos": {dt: i for i, dt in enumerate(d["date"].values)},
                         "dates": d["date"].values}
    print(f"유니버스 {len(panels)}개")

    all_dates = sorted({dt for p in panels.values() for dt in p["dates"]})
    anchors = all_dates[60:len(all_dates) - 1:STEP]

    trades = []   # (ret, outcome, hold, base30)
    for D in anchors:
        # 횡단 모멘텀 점수
        feats = []
        for c, p in panels.items():
            i = p["pos"].get(D)
            if i is None or i < 60 or i + 1 >= len(p["close"]):
                continue
            cl = p["close"]
            feats.append((c, i, cl[i] / cl[i-60:i+1].max(), cl[i]/cl[i-19:i+1].mean()-1,
                          cl[i]/cl[i-5]-1, p["value"][i-19:i+1].mean()))
        if len(feats) < 50:
            continue
        df = pd.DataFrame(feats, columns=["c", "i", "hi60", "disp20", "ret5", "liq"])
        df = df[df["liq"] >= df["liq"].quantile(LIQ_PCT)]
        for f in ["hi60", "disp20", "ret5"]:
            df[f+"z"] = (df[f]-df[f].mean())/(df[f].std()+1e-9)
        df["score"] = df[["hi60z", "disp20z", "ret5z"]].sum(axis=1)
        top = df.nlargest(TOPN, "score")

        for _, row in top.iterrows():
            p = panels[row["c"]]; cl = p["close"]; e = int(row["i"]); entry = cl[e]
            last = len(cl) - 1
            outcome, ret, hold = "timeout", None, MAXHOLD
            for t in range(e+1, min(e+MAXHOLD, last)+1):
                r = cl[t]/entry - 1
                if r >= TARGET:
                    outcome, ret, hold = "target", r, t-e; break
                if r <= STOP:
                    ex = t+1 if t+1 <= last else t      # 다음날 종가(시가 근사)
                    outcome, ret, hold = "stop", cl[ex]/entry-1, ex-e; break
            if ret is None:
                 te = min(e+MAXHOLD, last); ret = cl[te]/entry-1; hold = te-e
            base_t = min(e+30, last); base30 = cl[base_t]/entry - 1   # 규칙없이 30일 보유
            trades.append((ret, outcome, hold, base30))

    t = pd.DataFrame(trades, columns=["ret", "outcome", "hold", "base30"])
    print(f"\n총 트레이드 {len(t)}건  (top{TOPN} × {STEP}거래일마다 진입)")
    print("\n=== 전략 (손절-10%/익절+20%/최대보유60) ===")
    print(f"  평균 수익률/건 : {t['ret'].mean()*100:+.2f}%   중앙값 {t['ret'].median()*100:+.2f}%")
    print(f"  승률(>0)       : {(t['ret']>0).mean():.0%}")
    print(f"  평균 보유일     : {t['hold'].mean():.1f}거래일")
    oc = t["outcome"].value_counts(normalize=True)
    print(f"  결과 분포      : 익절 {oc.get('target',0):.0%} / 손절 {oc.get('stop',0):.0%} / 만기 {oc.get('timeout',0):.0%}")
    print(f"  익절건 평균 {t[t.outcome=='target']['ret'].mean()*100:+.1f}% · "
          f"손절건 평균 {t[t.outcome=='stop']['ret'].mean()*100:+.1f}% · "
          f"만기건 평균 {t[t.outcome=='timeout']['ret'].mean()*100:+.1f}%")
    print("\n=== baseline (같은 진입, 규칙없이 30거래일 보유) ===")
    print(f"  평균 수익률/건 : {t['base30'].mean()*100:+.2f}%   승률 {(t['base30']>0).mean():.0%}")
    print(f"\n→ 전략 평균 {t['ret'].mean()*100:+.2f}% vs 보유 {t['base30'].mean()*100:+.2f}%/건. "
          f"차이 {(t['ret'].mean()-t['base30'].mean())*100:+.2f}%p")

    # 100만원 환산 (건당 평균을 보유기간 기준 단순 연환산 — 낙관 근사)
    avg, hold = t["ret"].mean(), t["hold"].mean()
    ann = (1+avg)**(252/hold) - 1
    print(f"\n[100만원 환산] 건당 평균 {avg*100:+.2f}%, 평균보유 {hold:.0f}거래일 → "
          f"연환산 ~{ann*100:+.0f}% (복리·비용無·낙관). 3개월 ~{((1+avg)**(63/hold)-1)*100:+.0f}%")


if __name__ == "__main__":
    main()
