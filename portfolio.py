"""보유 포트폴리오 통합 조회 → portfolio.json (대시보드 데이터층).

한투(한국투자증권)는 KIS 잔고조회 API 로 자동, 나무증권·케이뱅크는 API 가 없어 holdings_manual.csv 수기 입력.
수기분 현재가는 KIS 시세 API 로 채워서 두 소스의 수익률 계산 기준을 통일한다.
실행: python portfolio.py   (.env 에 APP_KEY/APP_SECRET/KIS_CANO 필요)
"""
import os, csv, json
from datetime import datetime

import requests
from momentum_backtest import token, _get, BASE, APP_KEY, APP_SECRET, KST

_DIR = os.path.dirname(os.path.abspath(__file__))
MANUAL = os.path.join(_DIR, "holdings_manual.csv")
OUT = os.path.join(_DIR, "portfolio.json")
CANO = os.environ.get("KIS_CANO", "")
ACNT = os.environ.get("KIS_ACNT_PRDT_CD", "01")

# KIS 앱키는 계좌 단위 발급 — 잔고조회용 키는 시세용(APP_KEY)과 다를 수 있다.
# 기존 APP_KEY 는 매일 도는 모멘텀 리포트가 쓰므로 절대 교체하지 말고, 매매계좌 키를 따로 둔다.
TR_KEY = os.environ.get("KIS_TRADE_APP_KEY") or APP_KEY
TR_SECRET = os.environ.get("KIS_TRADE_APP_SECRET") or APP_SECRET
_SEPARATE_KEY = bool(os.environ.get("KIS_TRADE_APP_KEY"))


def _trade_token():
    """매매계좌 키가 따로면 그 키로 토큰 발급(캐시 분리), 아니면 기존 토큰 재사용."""
    if not _SEPARATE_KEY:
        return token()
    import json, time
    cache = os.path.join(_DIR, ".kis_trade_token.json")
    if os.path.exists(cache):
        try:
            c = json.load(open(cache))
            if time.time() - c["ts"] < 6 * 3600:
                return c["token"]
        except Exception:
            pass
    r = requests.post(f"{BASE}/oauth2/tokenP", timeout=20,
                      json={"grant_type": "client_credentials",
                            "appkey": TR_KEY, "appsecret": TR_SECRET})
    tok = r.json()["access_token"]
    json.dump({"token": tok, "ts": time.time()}, open(cache, "w"))
    return tok


def _hdr(tok, tr_id, trade=True):
    k, s = (TR_KEY, TR_SECRET) if trade else (APP_KEY, APP_SECRET)
    return {"authorization": f"Bearer {tok}", "appkey": k, "appsecret": s,
            "tr_id": tr_id, "custtype": "P"}


def kis_balance(tok):
    """한투 국내주식 잔고. CANO 없으면 빈 결과 (수기분만으로도 대시보드는 돌게)."""
    if not CANO:
        print("  ⚠️ KIS_CANO 없음 → 한투 자동조회 건너뜀")
        return [], {}
    url = f"{BASE}/uapi/domestic-stock/v1/trading/inquire-balance"
    params = {"CANO": CANO, "ACNT_PRDT_CD": ACNT, "AFHR_FLPR_YN": "N", "OFL_YN": "",
              "INQR_DVSN": "02", "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N",
              "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00",
              "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}
    rows, summary = [], {}
    while True:
        r = requests.get(url, headers=_hdr(tok, "TTTC8434R"), params=params, timeout=20)
        j = r.json()
        if j.get("rt_cd") != "0":
            print(f"  ⚠️ 한투 잔고조회 실패: {j.get('msg_cd')} {j.get('msg1')}")
            return rows, summary
        for o in j.get("output1", []) or []:
            qty = float(o.get("hldg_qty") or 0)
            if qty <= 0:
                continue
            rows.append({"broker": "한투", "code": o["pdno"], "name": o["prdt_name"].strip(),
                         "qty": qty, "avg": float(o.get("pchs_avg_pric") or 0),
                         "price": float(o.get("prpr") or 0)})
        o2 = (j.get("output2") or [{}])[0]
        summary = {"예수금": float(o2.get("dnca_tot_amt") or 0),
                   "d2예수금": float(o2.get("prvs_rcdl_excc_amt") or 0)}
        if j.get("tr_cont") not in ("F", "M"):
            break
        params["CTX_AREA_FK100"] = j.get("ctx_area_fk100", "")
        params["CTX_AREA_NK100"] = j.get("ctx_area_nk100", "")
    return rows, summary


def manual_rows():
    """holdings_manual.csv: broker,code,name,qty,avg — 나무증권·케이뱅크 등 API 없는 증권사."""
    if not os.path.exists(MANUAL):
        return []
    out = []
    with open(MANUAL, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            code = (r.get("code") or "").strip().zfill(6)
            qty = float(r.get("qty") or 0)
            if not code.isdigit() or qty <= 0:
                continue
            out.append({"broker": (r.get("broker") or "").strip(), "code": code,
                        "name": (r.get("name") or "").strip(), "qty": qty,
                        "avg": float(r.get("avg") or 0), "price": 0.0})
    return out


def fill_price(tok, rows):
    """현재가 미확보분(수기 입력) 시세 채우기."""
    url = f"{BASE}/uapi/domestic-stock/v1/quotations/inquire-price"
    for r in rows:
        if r["price"] > 0:
            continue
        j = _get(url, _hdr(tok, "FHKST01010100", trade=False),
                 {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": r["code"]})
        o = (j or {}).get("output") or {}
        r["price"] = float(o.get("stck_prpr") or 0)
        if not r["name"]:
            r["name"] = (o.get("rprs_mrkt_kor_name") or "").strip() or r["code"]


def signal_overlap(codes):
    """보유종목이 오늘 리포트 추천에 들어있나 (기본판/게이트판)."""
    hit = {}
    for label, fn in [("기본판", "latest_momentum_reco.csv"),
                      ("게이트판", "latest_momentum_reco_v20g.csv")]:
        p = os.path.join(_DIR, fn)
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                c = (r.get("code") or "").strip().zfill(6)
                if c in codes:
                    hit.setdefault(c, []).append(f"{label}#{r.get('rank')}")
    return hit


def main():
    rows, summary = kis_balance(_trade_token())
    rows += manual_rows()
    if not rows:
        print("보유 종목 없음 (KIS_CANO 미설정 + holdings_manual.csv 없음)")
        return
    fill_price(token(), rows)          # 시세는 기존 리포트용 키로 (매매키에 시세권한 없을 수 있음)

    hit = signal_overlap({r["code"] for r in rows})
    for r in rows:
        r["eval"] = r["qty"] * r["price"]
        r["cost"] = r["qty"] * r["avg"]
        r["pl"] = r["eval"] - r["cost"]
        r["ret"] = (r["price"] / r["avg"] - 1) if r["avg"] else 0.0
        r["signal"] = hit.get(r["code"], [])
    rows.sort(key=lambda r: -r["eval"])

    tot_eval = sum(r["eval"] for r in rows)
    tot_cost = sum(r["cost"] for r in rows)
    by_broker = {}
    for r in rows:
        b = by_broker.setdefault(r["broker"], {"eval": 0.0, "cost": 0.0, "n": 0})
        b["eval"] += r["eval"]; b["cost"] += r["cost"]; b["n"] += 1

    data = {"asof": datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
            "total": {"eval": tot_eval, "cost": tot_cost, "pl": tot_eval - tot_cost,
                      "ret": (tot_eval / tot_cost - 1) if tot_cost else 0.0},
            "cash": summary, "by_broker": by_broker, "positions": rows}
    json.dump(data, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print(f"\n===== 보유 현황 ({data['asof']}) =====")
    print(f"{'종목':<14}{'증권사':<7}{'수량':>7}{'평단':>11}{'현재가':>11}{'평가금액':>13}{'수익률':>9}  신호")
    for r in rows:
        print(f"{r['name'][:13]:<14}{r['broker']:<7}{r['qty']:>7.0f}{r['avg']:>11,.0f}"
              f"{r['price']:>11,.0f}{r['eval']:>13,.0f}{r['ret']*100:>8.1f}%  {','.join(r['signal'])}")
    print(f"\n총평가 {tot_eval:,.0f}원 · 매입 {tot_cost:,.0f}원 · "
          f"손익 {tot_eval-tot_cost:+,.0f}원 ({data['total']['ret']*100:+.1f}%)")
    for b, v in by_broker.items():
        print(f"  {b}: {v['eval']:,.0f}원 ({v['n']}종목, {(v['eval']/v['cost']-1)*100 if v['cost'] else 0:+.1f}%)")
    print(f"저장 {OUT}")


if __name__ == "__main__":
    main()
