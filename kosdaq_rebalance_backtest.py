"""코스닥 펀더멘탈 점수 리밸런스 포트폴리오 백테스트 — 상위5 동일가중, 월/분기 교체.

느린 펀더 신호엔 TP/SL이 아니라 주기적 리밸런싱이 맞나 검증.
점수 = 배포된 라이브 소프트틸트의 펀더 0.80 부분(밸류.30·퀄.28·성장.07·안정.15, 섹터중립 z,
1/PER=EPS/종가·ROE윈저±40 — 라이브 compute_multifactor_score 동일). 수급(0.20)은 과거데이터 없어 제외.
유니버스=시총상위200(point-in-time≈현재주식수×과거종가). 매수/매도=리밸런스 다음날 시가.
보유 종목이 순위 상위5서 빠지면 교체, 유지면 보유. 손절/익절 없음.
실행: python kosdaq_rebalance_backtest.py
"""
import os, re
import pandas as pd, numpy as np

_DIR = os.path.dirname(os.path.abspath(__file__))
OHLC = os.path.join(_DIR, "kosdaq_ohlc_cache")
FIN  = os.path.join(_DIR, "kosdaq_fin_cache")
UNI  = os.path.join(_DIR, "latest_kosdaq.csv")
N, CAP_N = 5, 200


def excluded(name, sector):
    s, n = str(sector), str(name)
    return ("ETF" in s or "ETN" in s or "스팩" in n or bool(re.search(r"우[BC]?$", n)))


def cross_z(s):
    return ((s - s.mean()) / (s.std() + 1e-8)).clip(-3, 3)


def cz_sec(s, sec, mn=5):
    pool = cross_z(s); out = pd.Series(index=s.index, dtype=float)
    for v, idx in sec.groupby(sec).groups.items():
        sub = s.loc[idx]
        out.loc[idx] = cross_z(sub) if (len(sub) >= mn and sub.std(skipna=True) > 1e-8) else pool.loc[idx]
    return out


def fin_asof(fin, D):
    elig = fin[fin["avail"] <= D]
    return elig.iloc[-1] if len(elig) else None


def load():
    meta = pd.read_csv(UNI); col = meta.columns[0]
    meta[col] = meta[col].astype(str).str.zfill(6)
    meta = meta[~meta.apply(lambda r: excluded(r["종목명"], r["섹터"]), axis=1)]
    sect = dict(zip(meta[col], meta["섹터"]))
    capnow = dict(zip(meta[col], pd.to_numeric(meta["시가총액"], errors="coerce")))
    name = dict(zip(meta[col], meta["종목명"]))
    codes = [c for c in meta[col] if os.path.exists(os.path.join(OHLC, f"{c}.pkl"))
             and os.path.exists(os.path.join(FIN, f"{c}.pkl"))]
    panels, fins = {}, {}
    for c in codes:
        d = pd.read_pickle(os.path.join(OHLC, f"{c}.pkl"))
        f = pd.read_pickle(os.path.join(FIN, f"{c}.pkl"))
        if d is None or len(d) < 120 or f is None or not len(f):
            continue
        panels[c] = {"o": d["open"].values, "c": d["close"].values,
                     "dates": d["date"].values, "pos": {dt: i for i, dt in enumerate(d["date"].values)}}
        fins[c] = f
    shares = {c: capnow[c] / panels[c]["c"][-1] for c in panels
              if c in capnow and pd.notna(capnow[c]) and panels[c]["c"][-1] > 0}
    return meta, sect, name, panels, fins, shares


def score_at(D, panels, fins, shares, sect):
    rows = []
    for c, p in panels.items():
        i = p["pos"].get(D)
        if i is None or i < 60 or c not in shares:
            continue
        row = fin_asof(fins[c], D)
        if row is None:
            continue
        px = p["c"][i]; eps = row.get("eps")
        rows.append({"c": c, "mcap": shares[c] * px, "sec": sect.get(c),
                     "ey": (eps / px) if (pd.notna(eps) and px) else np.nan,
                     "roe": row.get("roe"), "grs": row.get("grs"),
                     "opg": row.get("opg"), "lblt": row.get("lblt")})
    df = pd.DataFrame(rows)
    if len(df) < CAP_N // 2:
        return None
    df = df.nlargest(min(CAP_N, len(df)), "mcap").reset_index(drop=True)
    sec = df["sec"]
    df["score"] = (0.30 * cz_sec(df["ey"], sec).fillna(0)
                   + 0.28 * cz_sec(df["roe"].clip(-40, 40), sec).fillna(0)
                   + 0.07 * ((cross_z(df["grs"]).fillna(0) + cross_z(df["opg"]).fillna(0)) / 2)
                   + 0.15 * cz_sec(-df["lblt"].fillna(df["lblt"].median()).fillna(100), sec).fillna(0))
    return df


def fwd_open(p, D, Dn):
    """D 다음날 시가 → Dn 다음날 시가 수익률. 둘다 있어야."""
    i, j = p["pos"].get(D), p["pos"].get(Dn)
    if i is None or j is None or i + 1 >= len(p["o"]) or j + 1 >= len(p["o"]):
        return None
    e, x = p["o"][i + 1], p["o"][j + 1]
    return (x / e - 1) if (e and e > 0 and x and x > 0) else None


def metrics(rets, ppy, label):
    r = pd.Series(rets).dropna()
    eq = (1 + r).cumprod()
    mdd = (eq / eq.cummax() - 1).min()
    cagr = eq.iloc[-1] ** (ppy / len(r)) - 1
    sharpe = r.mean() / (r.std() + 1e-9) * np.sqrt(ppy)
    return (f"{label:>14} 누적{(eq.iloc[-1]-1)*100:>7.1f}% CAGR{cagr*100:>6.1f}% "
            f"MDD{mdd*100:>6.1f}% Sharpe{sharpe:>5.2f} 승률{(r>0).mean()*100:>4.0f}% 기간{len(r)}")


def backtest(freq, panels, fins, shares, sect, name):
    cal = sorted({dt for p in panels.values() for dt in p["dates"]})
    seen, rd = set(), []
    for d in cal:
        key = d[:6] if freq == "M" else (d[:4], (int(d[4:6]) - 1) // 3)
        if key not in seen and d >= "20230701":
            seen.add(key); rd.append(d)
    ppy = 12 if freq == "M" else 4

    top, uni, ls, regs, prev = [], [], [], [], None
    holdlog = []
    for k in range(len(rd) - 1):
        D, Dn = rd[k], rd[k + 1]
        df = score_at(D, panels, fins, shares, sect)
        if df is None:
            continue
        df = df.sort_values("score", ascending=False)
        picks = df.head(N)["c"].tolist()
        bots = df.tail(N)["c"].tolist()
        # 기간 수익률 (다음날시가→다음날시가)
        def avg(cs):
            v = [fwd_open(panels[c], D, Dn) for c in cs if c in panels]
            v = [x for x in v if x is not None]
            return np.mean(v) if v else None
        t, b, u = avg(picks), avg(bots), avg(df["c"].tolist())
        if t is None or u is None:
            continue
        top.append(t); uni.append(u); ls.append((t - b) if b is not None else np.nan)
        regs.append(D[:4] + ("H1" if D[4:6] <= "06" else "H2"))
        turn = "" if prev is None else f"교체{len(set(picks)-set(prev))}"
        prev = picks
        holdlog.append((D, [name.get(c, c) for c in picks], t, u, turn))

    print(f"\n========== {('월별' if freq=='M' else '분기')} 리밸런스 (상위{N} 동일가중, 시총상위200) ==========")
    print(metrics(top, ppy, "상위5 롱온리"))
    print(metrics(uni, ppy, "유니버스(벤치)"))
    print(metrics([x for x in ls if pd.notna(x)], ppy, "롱숏(상5-하5)"))
    # 레짐별 상위5 vs 벤치
    t = pd.DataFrame({"reg": regs, "top": top, "uni": uni})
    t["excess"] = t["top"] - t["uni"]
    print("\n  레짐별 상위5 기간평균% (vs 벤치 초과%):")
    g = t.groupby("reg").agg(top=("top", "mean"), uni=("uni", "mean"), ex=("excess", "mean"), n=("top", "size"))
    for r_, row in g.iterrows():
        print(f"    {r_}: 상위5 {row['top']*100:+5.1f}  벤치 {row['uni']*100:+5.1f}  초과 {row['ex']*100:+5.1f}  (n={int(row['n'])})")
    print(f"  벤치대비 초과 승률: {(t['excess']>0).mean()*100:.0f}%  누적초과: {((1+t['top']).prod()/(1+t['uni']).prod()-1)*100:+.1f}%p")
    print(f"\n  최근 5회 상위5 종목:")
    for D, nm, t_, u_, turn in holdlog[-5:]:
        print(f"    {D}: {', '.join(nm)}  → 기간{t_*100:+.1f}% (벤치{u_*100:+.1f}%) {turn}")


def main():
    meta, sect, name, panels, fins, shares = load()
    print(f"유니버스 {len(panels)}개 (OHLC+재무), 시총프록시 {len(shares)}개")
    for freq in ["M", "Q"]:
        backtest(freq, panels, fins, shares, sect, name)
    print("\n[해석] 상위5가 벤치(유니버스 등가중)를 누적·승률서 이기면 펀더 랭킹이 매매가치. "
          "롱숏>0이면 신호 자체는 +. MDD·Sharpe로 5종목 집중 변동성 확인.")


if __name__ == "__main__":
    main()
