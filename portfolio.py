"""보유 포트폴리오 통합 조회 → portfolio.json (대시보드 데이터층).

한투(한국투자증권)는 KIS 잔고조회 API 로 자동, 나무증권·케이뱅크는 API 가 없어 holdings_manual.csv 수기 입력.
수기분 현재가는 KIS 시세 API 로 채워서 두 소스의 수익률 계산 기준을 통일한다.
실행: python portfolio.py   (.env 에 APP_KEY/APP_SECRET/KIS_CANO 필요)
"""
import os, csv, json
from datetime import datetime, timedelta

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
    # KIS 는 토큰 발급을 1분당 1회로 제한 → 실패해도 죽지 않고 None 반환(다른 증권사는 계속 조회)
    for attempt in range(3):
        try:
            r = requests.post(f"{BASE}/oauth2/tokenP", timeout=20,
                              json={"grant_type": "client_credentials",
                                    "appkey": TR_KEY, "appsecret": TR_SECRET})
            j = r.json()
            if "access_token" in j:
                json.dump({"token": j["access_token"], "ts": time.time()}, open(cache, "w"))
                return j["access_token"]
            msg = j.get("error_description") or j.get("msg1") or str(j)[:80]
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
        if attempt < 2:
            time.sleep(20 * (attempt + 1))       # 1분 제한 → 20s·40s 백오프
    print(f"  ⚠️ 한투 토큰 발급 실패 → 한투 건너뜀 ({msg})")
    return None


def _hdr(tok, tr_id, trade=True):
    k, s = (TR_KEY, TR_SECRET) if trade else (APP_KEY, APP_SECRET)
    return {"authorization": f"Bearer {tok}", "appkey": k, "appsecret": s,
            "tr_id": tr_id, "custtype": "P"}


def _kis_accounts():
    """조회할 한투 계좌 목록. KIS_ACCOUNTS="12345678-01:ISA,87654321-01:위탁" (라벨 생략 가능).
    없으면 기존 단일 KIS_CANO/KIS_ACNT_PRDT_CD 로 폴백."""
    raw = os.environ.get("KIS_ACCOUNTS", "").strip()
    if not raw:
        return [(CANO, ACNT, "")] if CANO else []
    out = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        acct, _, label = item.partition(":")
        cano, _, prdt = acct.strip().partition("-")
        if cano:
            out.append((cano, prdt or "01", label.strip()))
    return out


def _one_balance(tok, cano, prdt, label):
    """계좌 1개 잔고. 실패해도 다른 계좌는 계속 조회하도록 예외 대신 빈 결과 반환."""
    url = f"{BASE}/uapi/domestic-stock/v1/trading/inquire-balance"
    params = {"CANO": cano, "ACNT_PRDT_CD": prdt, "AFHR_FLPR_YN": "N", "OFL_YN": "",
              "INQR_DVSN": "02", "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N",
              "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00",
              "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}
    name = f"한투{' ' + label if label else ''}"
    rows, cash = [], {}
    while True:
        r = requests.get(url, headers=_hdr(tok, "TTTC8434R"), params=params, timeout=20)
        j = r.json()
        if j.get("rt_cd") != "0":
            print(f"  ⚠️ {name}({cano}-{prdt}) 잔고조회 실패: {j.get('msg_cd')} {j.get('msg1')}")
            return rows, cash
        for o in j.get("output1", []) or []:
            qty = float(o.get("hldg_qty") or 0)
            if qty <= 0:
                continue
            rows.append({"broker": name, "code": o["pdno"], "name": o["prdt_name"].strip(),
                         "qty": qty, "avg": float(o.get("pchs_avg_pric") or 0),
                         "price": float(o.get("prpr") or 0)})
        o2 = (j.get("output2") or [{}])[0]
        cash = {"예수금": float(o2.get("dnca_tot_amt") or 0),
                "d2예수금": float(o2.get("prvs_rcdl_excc_amt") or 0)}
        if j.get("tr_cont") not in ("F", "M"):
            break
        params["CTX_AREA_FK100"] = j.get("ctx_area_fk100", "")
        params["CTX_AREA_NK100"] = j.get("ctx_area_nk100", "")
    print(f"  {name}({cano}-{prdt}) 보유 {len(rows)}종목")
    return rows, cash


def kis_balance(tok):
    """한투 국내주식 잔고 — 계좌 여러 개(위탁·ISA·CMA) 합산."""
    accts = _kis_accounts()
    if not accts:
        print("  ⚠️ 한투 계좌 미설정(KIS_ACCOUNTS/KIS_CANO) → 건너뜀")
        return [], {}
    if not tok:                      # 토큰 발급 실패 — 이미 사유를 출력했다
        return [], {}
    rows, summary = [], {}
    for cano, prdt, label in accts:
        r, c = _one_balance(tok, cano, prdt, label)
        rows += r
        for k, v in c.items():
            summary[k] = summary.get(k, 0.0) + v
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


def market_open_today():
    """오늘 국내장이 열리나. 토스 장운영 API — 주말뿐 아니라 대체공휴일까지 잡는다.
    (2026-08-17 이 광복절 대체공휴일이라 휴장인데 30분마다 갱신이 돌던 문제.)
    판단 불가면 True 로 두어 조회를 막지 않는다."""
    tok = _toss_token()
    if not tok:
        return True
    try:
        r = requests.get(f"{TOSS_BASE}/api/v1/market-calendar/KR",
                         headers={"Authorization": f"Bearer {tok}"}, timeout=15)
        if r.status_code != 200:
            return True
        today = (r.json().get("result") or {}).get("today") or {}
        return today.get("integrated") is not None
    except Exception:
        return True


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


def recent_changes(code, tok, n=5):
    """최근 n거래일 전일대비 등락률 [(YYYYMMDD, 등락)] — 종가 기준, 최신순.
    KIS 일봉 1콜. 보유종목 추이를 표에 붙이기 위한 용도."""
    today = datetime.now(KST)
    j = _get(f"{BASE}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
             _hdr(tok, "FHKST03010100", trade=False),
             {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
              "FID_INPUT_DATE_1": (today - timedelta(days=45)).strftime("%Y%m%d"),
              "FID_INPUT_DATE_2": today.strftime("%Y%m%d"),
              "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0"})
    if not j or j.get("rt_cd") != "0":
        return []
    rows = sorted((r["stck_bsop_date"], float(r["stck_clpr"]))
                  for r in (j.get("output2") or [])
                  if r.get("stck_clpr") and r["stck_clpr"] != "0")
    out = []
    for i in range(len(rows) - 1, 0, -1):
        prev = rows[i - 1][1]
        if prev:
            out.append((rows[i][0], rows[i][1] / prev - 1))
        if len(out) >= n:
            break
    return out


def _hist_cell(hist):
    """최근 5일 셀: '8/18(화) -2.3%' 를 줄바꿈으로. 상승 빨강·하락 파랑(국내 관습)."""
    if not hist:
        return [{"type": "text", "text": {"content": "—"}, "annotations": {"color": "gray"}}]
    wd = "월화수목금토일"
    rich = []
    for i, (d, chg) in enumerate(hist):
        dt = datetime.strptime(d, "%Y%m%d")
        col = "red" if chg > 0 else ("blue" if chg < 0 else "gray")
        rich.append({"type": "text",
                     "text": {"content": f"{'' if i == 0 else chr(10)}{dt.month}/{dt.day}({wd[dt.weekday()]}) "},
                     "annotations": {"color": "gray"}})
        rich.append({"type": "text", "text": {"content": f"{chg*100:+.1f}%"},
                     "annotations": {"color": col}})
    return rich


def health_warnings():
    """파이프라인 정지를 대시보드에 노출한다.

    2026-08-12~17 에 유튜브 분석이 5일간 0건이었는데 워크플로가 success 로 떠서 아무도 몰랐다.
    실패를 종료코드로 드러내는 것과 별개로, **사용자가 매일 보는 화면**에 띄워야 실제로 발견된다.
    """
    warn = []
    today = datetime.now(KST).date()

    def busgap(last_str):
        """마지막 갱신일 이후 지난 **영업일** 수. 주말·연휴에 오탐이 뜨지 않게 달력일이 아니라
        영업일로 센다(numpy busday_count 는 토·일 제외. 공휴일은 임계값 3으로 흡수)."""
        try:
            import numpy as np
            return int(np.busday_count(last_str, today.strftime("%Y-%m-%d")))
        except Exception:
            return 0

    # 유튜브 분석본이 최근에 쌓이고 있나 (프록시·Gemini 어느 쪽이 막혀도 여기서 드러난다)
    try:
        d = json.load(open(os.path.join(_DIR, "latest_youtube_analysis.json")))
        last = max(d.keys()) if d else None
        if last and busgap(last) >= 3:
            warn.append(f"유튜브 분석이 영업일 {busgap(last)}일째 멈춤 (마지막 {last}) "
                        f"— 프록시 대역폭·Gemini 크레딧 확인")
    except Exception:
        pass

    # 모멘텀 리포트가 최신 거래일 기준인가 (daily_quant 는 평일만 실행)
    try:
        import pandas as pd
        h = pd.read_csv(os.path.join(_DIR, "momentum_history.csv"), encoding="utf-8-sig")
        last = str(h["date"].max())
        if busgap(last) >= 3:
            warn.append(f"모멘텀 리포트가 영업일 {busgap(last)}일째 갱신 안 됨 (마지막 {last}) "
                        f"— daily_quant 실행 확인")
    except Exception:
        pass
    return warn


def _rt(text, bold=False, color=None):
    a = {"bold": bold}
    if color:
        a["color"] = color
    return [{"type": "text", "text": {"content": str(text)}, "annotations": a}]


def _cell_rows(data):
    """표 행: 종목(증권사) / 수량 / 평단→현재가 / 평가금액 / 수익률. 등락색은 한국식(상승 빨강)."""
    head = [{"object": "block", "type": "table_row", "table_row": {"cells": [
        _rt("종목", True), _rt("수량", True), _rt("평단 → 현재가", True),
        _rt("평가금액", True), _rt("수익률", True), _rt("최근 5일 (전일대비)", True)]}}]
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
            _rt(f"{r['ret']*100:+.1f}%", True, col),
            _hist_cell(r.get("hist"))]}})
    return head + rows


def _blocks(data):
    t = data["total"]
    out = []
    for w in health_warnings():
        out.append({"object": "block", "type": "callout", "callout": {
            "icon": {"type": "emoji", "emoji": "🚨"}, "color": "red_background",
            "rich_text": [{"type": "text", "text": {"content": "점검 필요 — " + w},
                           "annotations": {"bold": True}}]}})
    col = "red_background" if t["pl"] > 0 else ("blue_background" if t["pl"] < 0 else "gray_background")
    held = [r for r in data["positions"] if r["signal"]]
    out += [{"object": "block", "type": "callout", "callout": {
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
        "table_width": 6, "has_column_header": True, "has_row_header": False,
        "children": _cell_rows(data)}})
    sig = (f"⭐ 오늘 모멘텀 리포트 추천과 겹치는 보유: "
           + ", ".join(f"{r['name']}({'/'.join(r['signal'])})" for r in held)) if held else \
          "⭐ 오늘 리포트 추천과 겹치는 보유 종목 없음"
    out.append({"object": "block", "type": "callout", "callout": {
        "icon": {"type": "emoji", "emoji": "🎯"}, "color": "gray_background",
        "rich_text": _rt(sig)}})
    return out


def upload_notion(data):
    """보유현황 섹션만 제자리 갱신. 같은 페이지의 리포트 토글은 건드리지 않는다.
    섹션 관리·안전장치는 dashboard.py 참조 (앵커 뒤 삽입으로 상단 유지)."""
    if not os.environ.get("NOTION_API_KEY"):
        print("  ℹ️ NOTION_API_KEY 없음 → 노션 업로드 생략")
        return
    import dashboard
    dashboard.set_holdings(_blocks(data))
    print(f"  ✅ 노션 보유현황 갱신: {dashboard.url()}")


def main():
    if os.environ.get("SKIP_MARKET_CHECK") != "1" and not market_open_today():
        print("휴장일 — 시세가 바뀌지 않으므로 갱신 생략 (강제 실행: SKIP_MARKET_CHECK=1)")
        return
    rows, summary = kis_balance(_trade_token())
    rows += toss_holdings()
    rows += manual_rows()
    if not rows:
        print("보유 종목 없음 (KIS_CANO 미설정 + holdings_manual.csv 없음)")
        return
    tok_px = token()                   # 시세는 기존 리포트용 키로 (매매키에 시세권한 없을 수 있음)
    fill_price(tok_px, rows)
    for r in rows:                     # 최근 5일 전일대비 (종목당 KIS 1콜)
        r["hist"] = recent_changes(r["code"], tok_px)

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
