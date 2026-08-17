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


TOSS_BASE = "https://openapi.tossinvest.com"


def _toss_token():
    """토스 OAuth2 client_credentials. 유효 24h → 디스크 캐시로 재발급 억제."""
    cid, sec = os.environ.get("TOSS_CLIENT_ID"), os.environ.get("TOSS_CLIENT_SECRET")
    if not (cid and sec):
        return None
    import time
    cache = os.path.join(_DIR, ".toss_token.json")
    if os.path.exists(cache):
        try:
            c = json.load(open(cache))
            if time.time() < c["exp"] - 300:
                return c["token"]
        except Exception:
            pass
    r = requests.post(f"{TOSS_BASE}/oauth2/token", timeout=20,
                      data={"grant_type": "client_credentials",
                            "client_id": cid, "client_secret": sec})
    if r.status_code != 200:
        print(f"  ⚠️ 토스 토큰 발급 실패 HTTP {r.status_code}: {r.text[:160]}")
        return None
    j = r.json()
    json.dump({"token": j["access_token"], "exp": time.time() + float(j.get("expires_in", 86400))},
              open(cache, "w"))
    return j["access_token"]


def _ip_help(resp):
    """토스 허용IP 위반이면 현재 공인 IP 를 찍어준다 — 가정용 유동 IP 라 수시로 터진다."""
    try:
        if "ip-not-allowed" not in resp.text:
            return False
    except Exception:
        return False
    ip = "?"
    try:
        ip = requests.get("https://api.ipify.org", timeout=8).text.strip()
    except Exception:
        pass
    print(f"  ⛔ 토스 허용 IP 불일치. 현재 공인 IP = {ip}")
    print("     토스증권 → 설정 > Open API > 허용 IP 관리 > [IP 추가] 에 위 값을 등록하면 즉시 해결.")
    print("     (통신사 유동 IP 라 몇 시간~며칠 단위로 바뀜. 기존 IP 는 지우지 말고 추가만 하면 됨)")
    return True


def toss_holdings():
    """토스증권 보유주식. accountSeq 는 /accounts 로 자동 조회 (수동 입력 불필요)."""
    tok = _toss_token()
    if not tok:
        return []
    h = {"Authorization": f"Bearer {tok}"}
    seq = os.environ.get("TOSS_ACCOUNT_SEQ")
    if not seq:
        r = requests.get(f"{TOSS_BASE}/api/v1/accounts", headers=h, timeout=20)
        if r.status_code != 200:
            if not _ip_help(r):
                print(f"  ⚠️ 토스 계좌조회 실패 HTTP {r.status_code}: {r.text[:160]}")
            return []
        accts = (r.json().get("result") or [])
        broker = [a for a in accts if a.get("accountType") == "BROKERAGE"] or accts
        if not broker:
            print("  ⚠️ 토스 계좌 없음")
            return []
        seq = broker[0]["accountSeq"]
        print(f"  토스 계좌 {broker[0].get('accountNo')} (seq={seq}) 사용"
              + (f" · 계좌 {len(accts)}개 중 첫번째" if len(accts) > 1 else ""))
    r = requests.get(f"{TOSS_BASE}/api/v1/holdings", timeout=20,
                     headers={**h, "X-Tossinvest-Account": str(seq)})
    if r.status_code != 200:
        if not _ip_help(r):
            print(f"  ⚠️ 토스 보유조회 실패 HTTP {r.status_code}: {r.text[:160]}")
        return []
    res = r.json().get("result") or {}
    rows, skipped = [], 0
    for it in res.get("items") or []:
        if it.get("marketCountry") != "KR":       # 해외분은 통화가 달라 원화 합산에서 제외
            skipped += 1
            continue
        rows.append({"broker": "토스", "code": str(it["symbol"]).zfill(6),
                     "name": it.get("name") or "", "qty": float(it["quantity"]),
                     "avg": float(it["averagePurchasePrice"]),
                     "price": float(it["lastPrice"])})
    if skipped:
        print(f"  ℹ️ 토스 해외종목 {skipped}건은 원화 합산에서 제외")
    return rows


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


NOTION_TITLE = "💼 통합 포트폴리오"
PAGE_REF = os.path.join(_DIR, ".notion_portfolio_page.json")


def _rt(text, bold=False, color=None):
    a = {"bold": bold}
    if color:
        a["color"] = color
    return [{"type": "text", "text": {"content": str(text)}, "annotations": a}]


def _cell_rows(data):
    """표 행: 종목(증권사) / 수량 / 평단→현재가 / 평가금액 / 수익률. 등락색은 한국식(상승 빨강)."""
    head = [{"object": "block", "type": "table_row", "table_row": {"cells": [
        _rt("종목", True), _rt("수량", True), _rt("평단 → 현재가", True),
        _rt("평가금액", True), _rt("수익률", True)]}}]
    rows = []
    for r in data["positions"]:
        col = "red" if r["ret"] > 0 else ("blue" if r["ret"] < 0 else "gray")
        star = " ⭐" if r["signal"] else ""
        name = [{"type": "text", "text": {"content": f"{r['name']}{star}"}, "annotations": {"bold": True}},
                {"type": "text", "text": {"content": f"\n{r['broker']}"}, "annotations": {"color": "gray"}}]
        rows.append({"object": "block", "type": "table_row", "table_row": {"cells": [
            name, _rt(f"{r['qty']:,.0f}"),
            _rt(f"{r['avg']:,.0f} → {r['price']:,.0f}"),
            _rt(f"{r['eval']:,.0f}"),
            _rt(f"{r['ret']*100:+.1f}%", True, col)]}})
    return head + rows


def _blocks(data):
    t = data["total"]
    col = "red_background" if t["pl"] > 0 else ("blue_background" if t["pl"] < 0 else "gray_background")
    held = [r for r in data["positions"] if r["signal"]]
    out = [{"object": "block", "type": "callout", "callout": {
        "icon": {"type": "emoji", "emoji": "💰"}, "color": col,
        "rich_text": [
            {"type": "text", "text": {"content": f"총평가 {t['eval']:,.0f}원\n"}, "annotations": {"bold": True}},
            {"type": "text", "text": {"content":
                f"매입 {t['cost']:,.0f}원  ·  손익 {t['pl']:+,.0f}원 ({t['ret']*100:+.2f}%)\n"}},
            {"type": "text", "text": {"content": f"{data['asof']} KST 기준"}, "annotations": {"color": "gray"}}]}}]
    if len(data["by_broker"]) > 1:
        parts = [f"{b} {v['eval']:,.0f}원({v['n']})" for b, v in data["by_broker"].items()]
        out.append({"object": "block", "type": "paragraph",
                    "paragraph": {"rich_text": _rt("증권사별: " + "  ·  ".join(parts), color="gray")}})
    out.append({"object": "block", "type": "table", "table": {
        "table_width": 5, "has_column_header": True, "has_row_header": False,
        "children": _cell_rows(data)}})
    sig = (f"⭐ 오늘 모멘텀 리포트 추천과 겹치는 보유: "
           + ", ".join(f"{r['name']}({'/'.join(r['signal'])})" for r in held)) if held else \
          "⭐ 오늘 리포트 추천과 겹치는 보유 종목 없음"
    out.append({"object": "block", "type": "callout", "callout": {
        "icon": {"type": "emoji", "emoji": "🎯"}, "color": "gray_background",
        "rich_text": _rt(sig)}})
    return out


def upload_notion(data):
    """고정 페이지 1개를 제자리 갱신. 매번 새 페이지를 만들지 않는다.

    안전장치(2026-06-24 리포트 오삭제 사고 재발 방지): 페이지 id 를 로컬에 기억하고,
    삭제 전 그 페이지의 제목이 NOTION_TITLE 인지 확인한다. DB row 는 절대 건드리지 않는다.
    """
    key = os.environ.get("NOTION_API_KEY")
    if not key:
        print("  ℹ️ NOTION_API_KEY 없음 → 노션 업로드 생략")
        return
    h = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
         "Notion-Version": "2022-06-28"}          # 블록/DB 조작은 이 버전 고정 (파일업로드 버전은 DB쿼리 깨짐)
    parent = os.environ.get("NOTION_PARENT_PAGE_ID", "3324a00632f880fbb014d766d87a1079")

    pid = None
    if os.path.exists(PAGE_REF):
        try:
            pid = json.load(open(PAGE_REF)).get("page_id")
        except Exception:
            pid = None
    if pid:                                        # 기억한 페이지가 정말 내 페이지인지 검증
        r = requests.get(f"https://api.notion.com/v1/pages/{pid}", headers=h, timeout=20)
        ok = False
        if r.status_code == 200:
            j = r.json()
            title = "".join(t.get("plain_text", "") for t
                            in (j.get("properties", {}).get("title", {}).get("title") or []))
            ok = (not j.get("archived")) and title == NOTION_TITLE
        if not ok:
            print("  ⚠️ 기억한 페이지가 내 것이 아니거나 사라짐 → 새로 만듦 (기존 페이지 안 건드림)")
            pid = None

    if pid:                                        # 기존 페이지 자식만 제거 후 재작성
        r = requests.get(f"https://api.notion.com/v1/blocks/{pid}/children?page_size=100",
                         headers=h, timeout=20)
        for b in (r.json().get("results") or []):
            requests.delete(f"https://api.notion.com/v1/blocks/{b['id']}", headers=h, timeout=20)
        requests.patch(f"https://api.notion.com/v1/blocks/{pid}/children", headers=h, timeout=30,
                       json={"children": _blocks(data)})
    else:
        r = requests.post("https://api.notion.com/v1/pages", headers=h, timeout=30,
                          json={"parent": {"page_id": parent},
                                "properties": {"title": {"title": [{"text": {"content": NOTION_TITLE}}]}},
                                "children": _blocks(data)})
        if r.status_code >= 300:
            print(f"  ⚠️ 노션 페이지 생성 실패 {r.status_code}: {r.text[:200]}")
            return
        pid = r.json()["id"]
        json.dump({"page_id": pid}, open(PAGE_REF, "w"))
    print(f"  ✅ 노션 갱신: https://notion.so/{pid.replace('-','')}")


def main():
    rows, summary = kis_balance(_trade_token())
    rows += toss_holdings()
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
    upload_notion(data)


if __name__ == "__main__":
    main()
