"""우측 꼬리 측정 — 10%컷 vs 20%컷 vs 유니버스기준선의 급등주 발굴력.
질문: 기본판(10%컷)이 정말 대박종목을 더 잘 찾나, 아니면 체리피킹 기억인가.
측정: 선정 시점 다음날시가 진입 → 60거래일 내 '고점' 기준 +30/+50/+100% 도달률(우측꼬리=발굴력)
      / 종가저점 -30% 이하율(좌측꼬리=집중대가) / 30일 고정보유 실현수익(평균·승률).
기준선 = 1단계(거래대금100억) 통과 전종목 — 선정이 무조건 대비 엣지인지 확인.
audit_momentum.py 와 선정·진입 동일. step5 표본, 같은 종목 반복선정은 건별 카운트(=리스트 노출 빈도).
실행: python right_tail.py
"""
import os, re
import pandas as pd, numpy as np

_DIR = os.path.dirname(os.path.abspath(__file__))
OHLC = os.path.join(_DIR, "ohlc_cache")
UNI = os.path.join(_DIR, "latest_kospi_supply.csv")
LIQ_FLOOR, MAXFWD, HOLD = 1e10, 60, 30
SPLIT = "20250101"


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
def pool(D):
    if D not in _RAW:
        _RAW[D] = raw_pool(D)
    return _RAW[D]


def select(D, pct):
    df = pool(D)
    if df is None:
        return []
    sel = df.nlargest(max(10, int(len(df)*pct)), "score").nsmallest(10, "vol20")
    return [(r["c"], int(r["i"])) for _, r in sel.iterrows()]


def fwd_stats(c, e):
    """진입=다음날시가. 반환: (60일내 고점수익, 종가저점수익, 30일고정보유 실현수익)."""
    p = panels[c]; last = len(p["c"]) - 1; es = e + 1
    if es > last:
        return None
    entry = p["o"][es]
    if not entry or entry <= 0:
        return None
    end = min(es + MAXFWD, last)
    seg_h = p["h"][es:end+1]; seg_c = p["c"][es:end+1]
    maxup = seg_h.max()/entry - 1
    maxdn = seg_c.min()/entry - 1
    te = min(es + HOLD, last)
    ret30 = p["c"][te]/entry - 1
    return maxup, maxdn, ret30


anchors = all_dates[60:len(all_dates)-2:5]


def measure(picker, dfilter=None):
    ups, dns, r30 = [], [], []
    for D in anchors:
        if dfilter and not dfilter(D):
            continue
        for c, e in picker(D):
            r = fwd_stats(c, e)
            if r:
                ups.append(r[0]); dns.append(r[1]); r30.append(r[2])
    return np.array(ups), np.array(dns), np.array(r30)


def pick_cut(pct):
    return lambda D: select(D, pct)
def pick_all(D):
    df = pool(D)
    return [] if df is None else [(r["c"], int(r["i"])) for _, r in df.iterrows()]


def report(label, ups, dns, r30):
    n = len(ups)
    p30 = (ups >= 0.30).mean()*100; p50 = (ups >= 0.50).mean()*100; p100 = (ups >= 1.00).mean()*100
    dn30 = (dns <= -0.30).mean()*100; dn50 = (dns <= -0.50).mean()*100
    print(f"{label:>22} {n:>6} | {p30:>7.1f} {p50:>7.1f} {p100:>7.1f} | {dn30:>7.1f} {dn50:>7.1f} | "
          f"{r30.mean()*100:>7.1f} {np.median(r30)*100:>7.1f} {(r30>0).mean()*100:>6.0f}")


print(f"\n진입=신호 다음날시가. 우측꼬리=진입후 {MAXFWD}일내 '고점' 도달률. 좌측꼬리=종가저점. 실현={HOLD}일 고정보유.\n")
hdr = (f"{'구성':>22} {'선정수':>6} | {'≥+30%':>7} {'≥+50%':>7} {'≥+100%':>7} | "
       f"{'≤-30%':>7} {'≤-50%':>7} | {'평균%':>7} {'중앙%':>7} {'승률%':>6}")

print("="*len(hdr)); print("【전체기간 2023~26】"); print(hdr); print("-"*len(hdr))
for lab, pk in [("기준선(거래대금통과全)", pick_all), ("기본판 10%컷·저변동10", pick_cut(0.10)),
                ("게이트판 20%컷·저변동10", pick_cut(0.20))]:
    report(lab, *measure(pk))

for tag, flt in [("H1 혼조 2023~24", lambda D: D < SPLIT), ("H2 강세 2025~26", lambda D: D >= SPLIT)]:
    print("\n" + "="*len(hdr)); print(f"【{tag}】"); print(hdr); print("-"*len(hdr))
    for lab, pk in [("기준선(거래대금통과全)", pick_all), ("기본판 10%컷·저변동10", pick_cut(0.10)),
                    ("게이트판 20%컷·저변동10", pick_cut(0.20))]:
        report(lab, *measure(pk, flt))

print("\n[해석] 우측꼬리: 기본판(10%컷)이 기준선·게이트판보다 ≥+50%·≥+100% 도달률 높으면 '발굴력' 실재 → 워치리스트로 살릴 명분. "
      "단 좌측꼬리(≤-30%)도 같이 높으면 '대박도 쪽박도 많은' 고변동 = 재량 리스크관리 필수. "
      "기준선과 비슷하면 선정 엣지 없음(=체리피킹 기억).")
