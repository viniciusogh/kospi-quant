"""코스닥 모멘텀 백테스트 — KOSPI와 동일 틀(healthy10 + TP20/SL10/다음날시가)이 코스닥서도 먹히나.
FETCH=1 이면 코스닥 OHLC 수집(kosdaq_ohlc_cache/). 그 다음 레짐 IC + 분위스프레드 + 매매 시뮬.
실행: FETCH=1 python kosdaq_backtest.py  (첫1회)  이후 python kosdaq_backtest.py
"""
import os, re, time, random, requests
import pandas as pd, numpy as np
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from momentum_backtest import token, _get, BASE, APP_KEY, APP_SECRET, KST, spearman

_DIR = os.path.dirname(os.path.abspath(__file__))
KCACHE = os.path.join(_DIR, "kosdaq_ohlc_cache")
UNI = os.path.join(_DIR, "latest_kosdaq.csv")
STEP, MAXHOLD, LIQ_FLOOR, TP, SL = 5, 60, 1e10, 0.20, -0.10


def excluded(name, sector):
    s, n = str(sector), str(name)
    return ("ETF" in s or "ETN" in s or "스팩" in n or bool(re.search(r"우[BC]?$", n)))


def fetch_ohlc(code, tok):
    cache = os.path.join(KCACHE, f"{code}.pkl")
    if os.path.exists(cache):
        return pd.read_pickle(cache)
    url = f"{BASE}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
    hdr = {"authorization": f"Bearer {tok}", "appkey": APP_KEY, "appsecret": APP_SECRET,
           "tr_id": "FHKST03010100", "custtype": "P"}
    today = datetime.now(KST); start = datetime(2023, 1, 1, tzinfo=KST); rows, d2 = [], today
    while d2 > start:
        d1 = d2 - timedelta(days=150)
        j = _get(url, hdr, {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
                            "FID_INPUT_DATE_1": max(d1, start).strftime("%Y%m%d"),
                            "FID_INPUT_DATE_2": d2.strftime("%Y%m%d"),
                            "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0"})
        time.sleep(random.uniform(0.2, 0.35))
        if j and j.get("rt_cd") == "0":
            for r in j.get("output2", []) or []:
                if r.get("stck_clpr") and r["stck_clpr"] != "0":
                    rows.append({"date": r["stck_bsop_date"], "open": float(r["stck_oprc"]),
                                 "high": float(r["stck_hgpr"]), "close": float(r["stck_clpr"]),
                                 "value": float(r.get("acml_tr_pbmn", 0) or 0)})
        d2 = d1 - timedelta(days=1)
    df = pd.DataFrame(rows).drop_duplicates("date").sort_values("date").reset_index(drop=True) if rows else None
    os.makedirs(KCACHE, exist_ok=True); pd.to_pickle(df, cache)
    return df if df is not None and len(df) > 120 else None


def main():
    meta = pd.read_csv(UNI); col = meta.columns[0]; meta[col] = meta[col].astype(str).str.zfill(6)
    codes = meta[~meta.apply(lambda r: excluded(r["종목명"], r["섹터"]), axis=1)][col].tolist()
    if os.environ.get("FETCH") == "1":
        tok = token(); t0 = time.time(); print(f"코스닥 OHLC 수집 {len(codes)}개")
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = [ex.submit(fetch_ohlc, c, tok) for c in codes]
            for n, _ in enumerate(as_completed(futs), 1):
                if n % 200 == 0:
                    print(f"  {n}/{len(codes)} ({time.time()-t0:.0f}s)")
        print("수집 완료")

    panels = {}
    for c in codes:
        f = os.path.join(KCACHE, f"{c}.pkl")
        if os.path.exists(f):
            d = pd.read_pickle(f)
            if d is not None and len(d) > 120:
                panels[c] = {"o": d["open"].values, "h": d["high"].values, "c": d["close"].values,
                             "v": d["value"].values, "pos": {dt: i for i, dt in enumerate(d["date"].values)},
                             "dates": d["date"].values}
    print(f"유니버스 {len(panels)}개")
    all_dates = sorted({dt for p in panels.values() for dt in p["dates"]})
    anchors = all_dates[60:len(all_dates) - 2:STEP]

    # healthy10 선정 + 레짐별 IC + 매매(TP20/SL10/다음날시가)
    obs, entries = [], []
    for D in anchors:
        rg = D[:4] + ("H1" if D[4:6] <= "06" else "H2")
        rows = []
        for c, p in panels.items():
            i = p["pos"].get(D)
            if i is None or i < 60 or i + 2 >= len(p["c"]) or p["v"][i-4:i+1].mean() < LIQ_FLOOR:
                continue
            cl = p["c"]; dr = np.diff(cl[i-20:i+1]) / cl[i-20:i]
            fwd = cl[min(i+30, len(cl)-1)] / cl[i] - 1
            rows.append((c, i, cl[i]/cl[i-60:i+1].max(), cl[i]/cl[i-19:i+1].mean()-1, cl[i]/cl[i-5]-1, dr.std(), fwd))
        if len(rows) < 30:
            continue
        df = pd.DataFrame(rows, columns=["c", "i", "hi60", "disp20", "ret5", "vol20", "fwd"])
        for f in ["hi60", "disp20", "ret5"]:
            df[f+"z"] = (df[f]-df[f].mean())/(df[f].std()+1e-9)
        df["score"] = df[["hi60z", "disp20z", "ret5z"]].sum(axis=1)
        # 모멘텀 점수 IC (전체 풀)
        obs.append((rg, spearman(df["score"].values, df["fwd"].values), len(df)))
        for _, r in df.nlargest(max(10, int(len(df)*0.10)), "score").nsmallest(10, "vol20").iterrows():
            entries.append((r["c"], int(r["i"]), rg))

    ic = pd.DataFrame(obs, columns=["rg", "ic", "n"]).dropna()
    print(f"\n평균 pool {ic['n'].mean():.0f}개\n=== 모멘텀 점수 IC (레짐별) ===")
    print(ic.groupby("rg")["ic"].mean().round(3).to_string())
    print(f"전체 IC {ic['ic'].mean():.3f}")

    # 여러 진입 전략 매매 시뮬 (TP20/SL10/다음날시가) — 같은 OHLC 재활용
    def build_entries(mode):
        es = []
        for D in anchors:
            rg = D[:4] + ("H1" if D[4:6] <= "06" else "H2")
            rows = []
            for c, p in panels.items():
                i = p["pos"].get(D)
                if i is None or i < 60 or i + 2 >= len(p["c"]) or p["v"][i-4:i+1].mean() < LIQ_FLOOR:
                    continue
                cl = p["c"]; dr = np.diff(cl[i-20:i+1]) / cl[i-20:i]
                rows.append((c, i, cl[i]/cl[i-60:i+1].max(), cl[i]/cl[i-19:i+1].mean()-1, cl[i]/cl[i-5]-1, dr.std()))
            if len(rows) < 30:
                continue
            df = pd.DataFrame(rows, columns=["c", "i", "hi60", "disp20", "ret5", "vol20"])
            for f in ["hi60", "disp20", "ret5"]:
                df[f+"z"] = (df[f]-df[f].mean())/(df[f].std()+1e-9)
            df["score"] = df[["hi60z", "disp20z", "ret5z"]].sum(axis=1)
            if mode == "모멘텀상위(healthy10)":
                pick = df.nlargest(max(10, int(len(df)*0.10)), "score").nsmallest(10, "vol20")
            elif mode == "역발상(모멘텀하위10%)":
                pick = df.nsmallest(max(10, int(len(df)*0.10)), "score").nsmallest(10, "vol20")
            elif mode == "저변동성만":
                pick = df.nsmallest(10, "vol20")
            else:  # 저PER 없음 → 저변동성+역발상 콤보
                pick = df.nsmallest(max(10, int(len(df)*0.10)), "score").head(10)
            for _, r in pick.iterrows():
                es.append((r["c"], int(r["i"]), rg))
        return es

    def sim(es):
        rr = []
        for c, e, rg in es:
            p = panels[c]; last = len(p["c"]) - 1; s = e + 1
            if s > last:
                continue
            entry = p["o"][s]
            if not entry or entry <= 0:
                continue
            ret = None
            for t in range(s, min(s+MAXHOLD, last)+1):
                if p["h"][t] >= entry*(1+TP): ret = TP; break
                if p["c"][t] <= entry*(1+SL):
                    ex = t+1 if t+1 <= last else t; ret = p["o"][ex]/entry-1; break
            if ret is None:
                te = min(s+MAXHOLD, last); ret = p["c"][te]/entry-1
            rr.append((rg, ret))
        d = pd.DataFrame(rr, columns=["rg", "ret"])
        rm = d.groupby("rg")["ret"].mean()
        return d["ret"].mean()*100, (d["ret"] > 0).mean()*100, (rm > 0).sum(), len(rm), len(d)

    print(f"\n=== 코스닥 진입전략 비교 (TP+20/SL-10/다음날시가) ===")
    print(f"{'전략':>20} {'건당%':>7} {'승률%':>6} {'양수레짐':>7} {'건수':>6}")
    for mode in ["모멘텀상위(healthy10)", "역발상(모멘텀하위10%)", "저변동성만"]:
        m, w, pos, nreg, n = sim(build_entries(mode))
        print(f"{mode:>20} {m:>7.2f} {w:>6.0f} {pos:>4}/{nreg} {n:>6}")
    print("\n[해석] 양수·양수레짐 많은 전략이 코스닥서 유효. 모멘텀상위가 음수면 코스피 로직 부적합.")


if __name__ == "__main__":
    main()
