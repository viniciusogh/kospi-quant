"""손절/익절 폭 민감도 — 우승구성(20%컷·저변동10·이탈청산게이트·10슬롯) 고정, TP×SL만 스윕.
질문: +20/-10 이 최적인가, 아니면 청산폭 튜닝으로 더 나아지나. H1(혼조)/H2(강세) 양쪽 우위만 진짜.
선정·게이트·진입(다음날시가)·MAXHOLD는 audit_momentum.py 와 동일. 결과론 과적합 경계.
실행: python tp_sl_sweep.py
"""
import os, re
import pandas as pd, numpy as np

_DIR = os.path.dirname(os.path.abspath(__file__))
OHLC = os.path.join(_DIR, "ohlc_cache")
UNI = os.path.join(_DIR, "latest_kospi_supply.csv")
LIQ_FLOOR, MAXHOLD = 1e10, 60
PICK, POOLN, K = "lowvol", 10, 10
# 두 라이브 구성 각각 검증 (외삽 금지): 기본=10%컷·게이트없음 / 게이트판=20%컷·이탈청산
CONFIGS = [(0.10, None, "기본판(10%컷·게이트없음)"), (0.20, "entryexit", "게이트판(20%컷·이탈청산)")]
PCT, GATE = 0.20, "entryexit"   # 런타임에 set_config 로 교체
SPLIT = "20250101"


def set_config(pct, gate):
    global PCT, GATE, _RAW
    PCT, GATE = pct, gate
    _RAW = {}   # 컷% 바뀌면 후보 캐시 무효화


def excluded(name, sector):
    s, n = str(sector), str(name)
    return ("ETF" in s or "ETN" in s or "스팩" in n or bool(re.search(r"우[BC]?$", n)))


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
print(f"유니버스 {len(panels)}, 기간 {all_dates[60]}~{all_dates[-2]}")

idx = pd.read_pickle(os.path.join(_DIR, "kospi_index.pkl"))
REG = {}
for _, rr in idx.iterrows():
    if pd.isna(rr["ma200"]) or pd.isna(rr["ma120"]):
        continue
    c_, m2, m1 = rr["close"], rr["ma200"], rr["ma120"]
    REG[rr["date"]] = "강세" if (c_ > m2 and c_ > m1) else ("약세" if (c_ < m2 and c_ < m1) else "보합")
def regime(D):
    if D in REG:
        return REG[D]
    prev = [d for d in REG if d <= D]
    return REG[max(prev)] if prev else "보합"


def raw_pool(D):
    rows = []
    for c, p in panels.items():
        i = p["pos"].get(D)
        if i is None or i < 60 or i + 2 >= len(p["c"]) or p["v"][i-4:i+1].mean() < LIQ_FLOOR:
            continue
        cl = p["c"]; dr = np.diff(cl[i-20:i+1]) / cl[i-20:i]
        rows.append((c, i, cl[i]/cl[i-60:i+1].max(), cl[i]/cl[i-19:i+1].mean()-1, cl[i]/cl[i-5]-1, dr.std()))
    if len(rows) < 50:
        return None
    df = pd.DataFrame(rows, columns=["c", "i", "hi60", "disp20", "ret5", "vol20"])
    for f in ["hi60", "disp20", "ret5"]:
        df[f+"z"] = (df[f]-df[f].mean())/(df[f].std()+1e-9)
    df["score"] = df[["hi60z", "disp20z", "ret5z"]].sum(axis=1)
    return df


_RAW = {}
def candidates(D):
    if D not in _RAW:
        _RAW[D] = raw_pool(D)
    df = _RAW[D]
    if df is None:
        return []
    decile = df.nlargest(max(POOLN, int(len(df)*PCT)), "score")
    sel = decile.nsmallest(POOLN, "vol20")
    return [(r["c"], int(r["i"])) for _, r in sel.sort_values("score", ascending=False).iterrows()]


def trade(c, e, tp, sl, maxhold):
    p = panels[c]; last = len(p["c"]) - 1; es = e + 1
    if es > last:
        return None
    entry = p["o"][es]
    if not entry or entry <= 0:
        return None
    ed = p["dates"][es]
    for t in range(es, min(es+maxhold, last)+1):
        if tp is not None and p["h"][t] >= entry*(1+tp):
            return {"ret": tp, "entry_d": ed, "exit_d": p["dates"][t]}
        if p["c"][t] <= entry*(1+sl):
            ex = t+1 if t+1 <= last else t
            return {"ret": p["o"][ex]/entry-1, "entry_d": ed, "exit_d": p["dates"][ex]}
    te = min(es+maxhold, last)
    return {"ret": p["c"][te]/entry-1, "entry_d": ed, "exit_d": p["dates"][te]}


def slot_portfolio(tp, sl, maxhold, dstart="0", dend="9"):
    slots = [{"eq": 1.0/K, "open": None, "epx": None} for _ in range(K)]; rets = []; curve = []
    days = [d for d in all_dates[60:len(all_dates)-2] if dstart <= d <= dend]
    for D in days:
        up = (regime(D) == "강세")
        for s in slots:
            if s["open"] and s["open"]["exit_d"] <= D:
                s["eq"] *= (1 + s["open"]["ret"]); rets.append(s["open"]["ret"]); s["open"] = None; s["epx"] = None
        if GATE == "entryexit" and not up:
            for s in slots:
                if s["open"]:
                    p = panels[s["open"]["c"]]; ix = p["pos"].get(D)
                    r = (p["c"][ix]/s["epx"]-1) if (ix is not None and s["epx"]) else s["open"]["ret"]
                    s["eq"] *= (1+r); rets.append(r); s["open"] = None; s["epx"] = None
        held = {s["open"]["c"] for s in slots if s["open"]}
        cl = candidates(D); ci = 0
        allow_entry = (GATE is None) or up
        for s in slots:
            if s["open"] is not None or not allow_entry:
                continue
            while ci < len(cl) and cl[ci][0] in held:
                ci += 1
            if ci >= len(cl):
                break
            c, e = cl[ci]; ci += 1
            tr = trade(c, e, tp, sl, maxhold)
            if tr:
                s["open"] = {"c": c, **tr}; s["epx"] = panels[c]["o"][e+1]; held.add(c)
        val = 0.0
        for s in slots:
            if s["open"] and s["open"]["entry_d"] <= D < s["open"]["exit_d"]:
                p = panels[s["open"]["c"]]; ix = p["pos"].get(D)
                val += s["eq"] * (p["c"][ix]/s["epx"]) if (ix is not None and s["epx"]) else s["eq"]
            else:
                val += s["eq"]
        curve.append((D, val))
    final = sum((s["eq"]*(1+s["open"]["ret"])) if s["open"] else s["eq"] for s in slots)
    cv = pd.Series({d: v for d, v in curve}); mdd = (cv/cv.cummax()-1).min()
    r = np.array(rets)
    return (final-1)*100, mdd*100, (r > 0).mean()*100 if len(r) else 0, len(r)


def row(tp, sl, maxhold=MAXHOLD):
    full, mdd, wr, nt = slot_portfolio(tp, sl, maxhold)
    h1 = slot_portfolio(tp, sl, maxhold, "0", SPLIT)[0]
    h2 = slot_portfolio(tp, sl, maxhold, SPLIT, "9")[0]
    return full, h1, h2, mdd, wr, nt


for pct, gate, clabel in CONFIGS:
    set_config(pct, gate)
    print(f"\n\n{'='*72}\n■ {clabel} · 저변동{POOLN}·{K}슬롯·최대{MAXHOLD}일. 청산폭만 스윕.\n{'='*72}")

    # ===== SL 고정 -10%, TP 스윕 =====
    print(f"\n----- SL -10% 고정, 익절 TP 스윕 -----")
    print(f"{'TP':>8} {'전체%':>8} {'H1(혼조)%':>9} {'H2(강세)%':>9} {'MDD%':>7} {'승률%':>6} {'거래':>6}")
    for tp in [0.10, 0.15, 0.20, 0.25, 0.30, 0.40, None]:
        full, h1, h2, mdd, wr, nt = row(tp, -0.10)
        base = " ←현행" if tp == 0.20 else ("  (익절무제한)" if tp is None else "")
        print(f"{('무제한' if tp is None else f'+{tp*100:.0f}%'):>8} {full:>8.1f} {h1:>9.1f} {h2:>9.1f} {mdd:>7.1f} {wr:>6.0f} {nt:>6}{base}")

    # ===== TP 고정 +20%, SL 스윕 =====
    print(f"\n----- TP +20% 고정, 손절 SL 스윕 -----")
    print(f"{'SL':>8} {'전체%':>8} {'H1(혼조)%':>9} {'H2(강세)%':>9} {'MDD%':>7} {'승률%':>6} {'거래':>6}")
    for sl in [-0.05, -0.07, -0.10, -0.13, -0.15, -0.20]:
        full, h1, h2, mdd, wr, nt = row(0.20, sl)
        base = " ←현행" if sl == -0.10 else ""
        print(f"{f'{sl*100:.0f}%':>8} {full:>8.1f} {h1:>9.1f} {h2:>9.1f} {mdd:>7.1f} {wr:>6.0f} {nt:>6}{base}")

    # ===== TP×SL 전체 그리드 (전체%) =====
    print(f"\n----- TP×SL 그리드 (전체 누적%) — 봉우리 vs 고원 확인용 -----")
    TPS = [0.15, 0.20, 0.25, 0.30]; SLS = [-0.07, -0.10, -0.13, -0.15]
    print(f"{'TP\\SL':>8}" + "".join(f"{f'{s*100:.0f}%':>9}" for s in SLS))
    for tp in TPS:
        cells = [slot_portfolio(tp, sl, MAXHOLD)[0] for sl in SLS]
        print(f"{f'+{tp*100:.0f}%':>8}" + "".join(f"{v:>9.1f}" for v in cells))

print("\n[해석] 각 구성 독립 판정. 현행 +20/-10 주변이 '고원'(이웃칸도 높음)이면 강건, 혼자 '봉우리'면 과적합. "
      "H1·H2 둘 다 +인 칸만 채택 후보. 익절은 같은봉 고가즉시(낙관)·손절은 종가확인 익일시가(현실) 비대칭 유지.")
