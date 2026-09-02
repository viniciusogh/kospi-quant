"""추천 이력에 제안 청산 규칙(+15% 익절 / -10% 손절)과 레짐 게이트를 적용해 성적을 낸다.

'매수 후 무한보유' 로 계산하면 평균 -5.2% 인데, 그건 전략 성적이 아니다.
리포트가 제안하는 청산은 +15%/-10% 이고 게이트는 비강세장 진입을 금지한다.
그 둘을 적용했을 때 실제로 얼마나 방어되는지 계산한다.

한계(반드시 인지):
- **종가 기준**이다. 장중에 -10% 를 찍고 회복한 날은 손절로 안 잡힌다 → 실제보다 낙관적.
- 같은 종목이 여러 날 추천되면 각각 별개 거래로 센다(실제로는 한 번만 살 것).
- 수수료·세금·슬리피지 미반영.

실행: python sim_exits.py [--tp 15] [--sl 10] [--hold 30]
"""
import os, sys, json
import _env
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

import momentum_daily as M
from momentum_backtest import token, KST

_DIR = os.path.dirname(os.path.abspath(__file__))


def price_series(code, tok, days=200):
    """일자별 종가 시계열. FID_ORG_ADJ_PRC=0(수정주가)로 분할·배당 왜곡을 줄인다."""
    url = f"{M.BASE}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
    hdr = {"authorization": f"Bearer {tok}", "appkey": M.APP_KEY, "appsecret": M.APP_SECRET,
           "tr_id": "FHKST03010100", "custtype": "P"}
    out = {}
    d2 = datetime.now(KST)
    for _ in range(3):                       # 콜당 ~100행
        d1 = d2 - timedelta(days=100)
        j = M._get(url, hdr, {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
                              "FID_INPUT_DATE_1": d1.strftime("%Y%m%d"),
                              "FID_INPUT_DATE_2": d2.strftime("%Y%m%d"),
                              "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0"})
        if j and j.get("rt_cd") == "0":
            for r in j.get("output2", []) or []:
                if r.get("stck_clpr"):
                    out[r["stck_bsop_date"]] = float(r["stck_clpr"])
        d2 = d1 - timedelta(days=1)
        if len(out) > days:
            break
    return pd.Series(out).sort_index()


def index_gate(tok):
    """날짜별 게이트 통과 여부 — 지수 ≥ 200일선 AND ≥ 120일선."""
    url = f"{M.BASE}/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice"
    hdr = {"authorization": f"Bearer {tok}", "appkey": M.APP_KEY, "appsecret": M.APP_SECRET,
           "tr_id": "FHKUP03500100", "custtype": "P"}
    rows = {}
    d2 = datetime.now(KST)
    for _ in range(9):
        d1 = d2 - timedelta(days=60)
        j = M._get(url, hdr, {"FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": "0001",
                              "FID_INPUT_DATE_1": d1.strftime("%Y%m%d"),
                              "FID_INPUT_DATE_2": d2.strftime("%Y%m%d"), "FID_PERIOD_DIV_CODE": "D"})
        if j and j.get("rt_cd") == "0":
            for r in j.get("output2", []) or []:
                if r.get("bstp_nmix_prpr"):
                    rows[r["stck_bsop_date"]] = float(r["bstp_nmix_prpr"])
        d2 = d1 - timedelta(days=1)
    s = pd.Series(rows).sort_index()
    ma200, ma120 = s.rolling(200).mean(), s.rolling(120).mean()
    return {d: bool(s[d] >= ma200[d] and s[d] >= ma120[d])
            for d in s.index if not (np.isnan(ma200[d]) or np.isnan(ma120[d]))}


def simulate(entry_date, entry_px, ser, tp, sl, hold):
    """진입 다음 거래일부터 종가로 청산 판정. (수익률, 보유일, 청산사유)"""
    fut = ser[ser.index > entry_date]
    if fut.empty:
        return None
    for i, (d, px) in enumerate(fut.items(), 1):
        r = px / entry_px - 1
        if r >= tp:
            return r, i, "익절"
        if r <= -sl:
            return r, i, "손절"
        if i >= hold:
            return r, i, "기간만료"
    last = fut.iloc[-1] / entry_px - 1
    return last, len(fut), "보유중"


def main():
    tp = float(sys.argv[sys.argv.index("--tp") + 1]) / 100 if "--tp" in sys.argv else 0.15
    sl = float(sys.argv[sys.argv.index("--sl") + 1]) / 100 if "--sl" in sys.argv else 0.10
    hold = int(sys.argv[sys.argv.index("--hold") + 1]) if "--hold" in sys.argv else 30

    h = pd.read_csv(os.path.join(_DIR, "momentum_history.csv"), encoding="utf-8-sig",
                    dtype={"code": str})
    h["code"] = h["code"].str.zfill(6)
    top = h[h["rank"] == 1].sort_values("date")
    tok = token()

    M.log(f"▶ 청산규칙 적용 시뮬 (익절 +{tp*100:.0f}% / 손절 -{sl*100:.0f}% / 최대 {hold}거래일)")
    gate = index_gate(tok)
    M.log(f"  게이트 복원: {len(gate)}일 · 통과 {sum(gate.values())}일 ({sum(gate.values())/len(gate)*100:.0f}%)")

    sers = {}
    for code in top["code"].unique():
        sers[code] = price_series(code, tok)
    M.log(f"  가격 시계열 {len(sers)}종목")

    res = []
    for _, r in top.iterrows():
        d = str(r["date"]).replace("-", "")
        ser = sers.get(r["code"])
        if ser is None or ser.empty:
            continue
        out = simulate(d, float(r["price"]), ser, tp, sl, hold)
        if not out:
            continue
        ret, days, why = out
        res.append({"date": str(r["date"]), "name": r["종목명"], "code": r["code"],
                    "ret": ret, "days": days, "why": why, "gate": gate.get(d)})
    df = pd.DataFrame(res)
    if df.empty:
        M.log("❌ 시뮬 결과 없음"); return

    def stat(x, label):
        if x.empty:
            print(f"  {label:22s} 거래 0건"); return
        print(f"  {label:22s} {len(x):>3}건 · 평균 {x['ret'].mean()*100:+6.1f}% · "
              f"중위 {x['ret'].median()*100:+6.1f}% · 승률 {(x['ret']>0).mean()*100:>3.0f}% · "
              f"평균보유 {x['days'].mean():.0f}일")

    print(f"\n=== 청산규칙 적용 (익절 +{tp*100:.0f}% / 손절 -{sl*100:.0f}% / {hold}일) ===")
    stat(df, "전체")
    stat(df[df["gate"] == True], "게이트 통과일만")
    stat(df[df["gate"] == False], "게이트 미충족일만")
    print("\n  청산 사유 분포:")
    for why, n in df["why"].value_counts().items():
        sub = df[df["why"] == why]
        print(f"    {why:8s} {n:>3}건 · 평균 {sub['ret'].mean()*100:+6.1f}%")
    print("\n  ⚠️ 종가 기준이라 장중 손절 미포착 — 실제보다 낙관적. 수수료·세금 미반영.")
    df.to_csv(os.path.join(_DIR, "sim_exits_result.csv"), index=False, encoding="utf-8-sig")
    print(f"\n  저장: sim_exits_result.csv")


if __name__ == "__main__":
    main()
