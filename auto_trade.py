"""리포트가 고른 종목을 텔레그램 승인을 받은 뒤 한투 실계좌로 매수한다.

  python auto_trade.py propose   # 오늘 Report DB 행 → 주문안 → 텔레그램에 승인 버튼 전송
  python auto_trade.py poll      # 버튼 눌렸는지 확인 → 승인이면 주문 전송
  python auto_trade.py setup     # 텔레그램 chat_id 찾기 (봇에게 아무 말이나 보낸 뒤 실행)
  python auto_trade.py status    # 현재 상태 출력

승인 없이는 절대 주문이 나가지 않는다. 승인이 있어도 TRADE_ENABLED=1 이 아니면 드라이런이다.
게이트 미충족이어도 주문안은 만든다(2026-09-06 규환님 결정) — 대신 알림 첫 줄에 게이트를 박는다.
"""
import os, sys, json, time, csv
from datetime import datetime, timedelta

import requests
import _env  # noqa: F401  (.env 로드)
from momentum_backtest import token, _get, BASE, APP_KEY, APP_SECRET, KST

_DIR = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(_DIR, ".trade_state.json")
LOG = os.path.join(_DIR, "trade_log.csv")

TR_KEY = os.environ.get("KIS_TRADE_APP_KEY") or APP_KEY
TR_SECRET = os.environ.get("KIS_TRADE_APP_SECRET") or APP_SECRET
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
NOTION_KEY = os.environ.get("NOTION_API_KEY", "")
NOTION_DB = os.environ.get("NOTION_DAILY_DB_ID", "")

# 안전장치 — 기본값이 전부 '안 나간다' 쪽이다
ENABLED = os.environ.get("TRADE_ENABLED", "0") == "1"   # 0 이면 승인해도 드라이런
MAX_KRW = int(os.environ.get("TRADE_MAX_KRW", "100000"))  # 1회 주문 상한
TTL_H = int(os.environ.get("TRADE_APPROVE_TTL_H", "6"))   # 승인 유효시간
ORD_DVSN = os.environ.get("TRADE_ORD_DVSN", "00")         # 00 지정가 / 01 시장가

# 실전 현금매수. 첫 실주문 전까지 미검증 — docs/KIS_API_REFERENCE.md '주식주문(현금)' 참조
TR_BUY = os.environ.get("KIS_TR_BUY", "TTTC0802U")


def log(m):
    print(f"[{datetime.now(KST):%H:%M:%S}] {m}")


def today():
    return datetime.now(KST).strftime("%Y-%m-%d")


def account():
    """KIS_ACCOUNTS 의 첫 계좌. '12345678-01:라벨' 형식."""
    raw = (os.environ.get("KIS_ACCOUNTS") or "").split(",")[0].strip()
    acct = raw.partition(":")[0]
    cano, _, prdt = acct.partition("-")
    return cano, (prdt or "01")


def load_state():
    try:
        return json.load(open(STATE))
    except Exception:
        return {}


def save_state(s):
    json.dump(s, open(STATE, "w"), ensure_ascii=False, indent=1)


# ── 텔레그램 ──────────────────────────────────────────────────────────
def _tg(method, **payload):
    if not TG_TOKEN:
        log("⚠️ TELEGRAM_BOT_TOKEN 없음")
        return None
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}",
                          json=payload, timeout=20)
        j = r.json()
        if not j.get("ok"):
            log(f"⚠️ 텔레그램 {method} 실패: {str(j)[:150]}")
            return None
        return j.get("result")
    except Exception as e:
        log(f"⚠️ 텔레그램 {method} 예외: {type(e).__name__} {e}")
        return None


def notify(text, buttons=None):
    p = {"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"}
    if buttons:
        p["reply_markup"] = {"inline_keyboard": [buttons]}
    return _tg("sendMessage", **p)


# ── KIS ───────────────────────────────────────────────────────────────
def _trade_token():
    """매매키 전용 토큰. 캐시를 시세 토큰과 분리한다 (키가 다르면 토큰도 다름)."""
    if not os.environ.get("KIS_TRADE_APP_KEY"):
        return token()
    cache = os.path.join(_DIR, ".kis_trade_token.json")
    try:
        c = json.load(open(cache))
        if time.time() - c["ts"] < 6 * 3600:
            return c["token"]
    except Exception:
        pass
    r = requests.post(f"{BASE}/oauth2/tokenP", timeout=20,
                      json={"grant_type": "client_credentials",
                            "appkey": TR_KEY, "appsecret": TR_SECRET})
    j = r.json()
    if "access_token" not in j:
        log(f"❌ 매매토큰 발급 실패: {j.get('error_description') or str(j)[:120]}")
        return None
    json.dump({"token": j["access_token"], "ts": time.time()}, open(cache, "w"))
    return j["access_token"]


def _thdr(tok, tr_id, extra=None):
    h = {"authorization": f"Bearer {tok}", "appkey": TR_KEY, "appsecret": TR_SECRET,
         "tr_id": tr_id, "custtype": "P", "content-type": "application/json"}
    return {**h, **(extra or {})}


def quote(code):
    """현재가. 시세는 기존 APP_KEY 로 (매매키와 분리)."""
    j = _get(f"{BASE}/uapi/domestic-stock/v1/quotations/inquire-price",
             {"authorization": f"Bearer {token()}", "appkey": APP_KEY,
              "appsecret": APP_SECRET, "tr_id": "FHKST01010100"},
             {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code})
    try:
        return int((j.get("output") or {}).get("stck_prpr"))
    except Exception:
        return None


def buyable(tok, cano, prdt, code, price):
    """현금 매수가능금액. 실패하면 None (0 과 구분해야 한다)."""
    r = requests.get(f"{BASE}/uapi/domestic-stock/v1/trading/inquire-psbl-order",
                     headers=_thdr(tok, "TTTC8908R"), timeout=20,
                     params={"CANO": cano, "ACNT_PRDT_CD": prdt, "PDNO": code,
                             "ORD_UNPR": str(price), "ORD_DVSN": ORD_DVSN,
                             "CMA_EVLU_AMT_ICLD_YN": "N", "OVRS_ICLD_YN": "N"})
    j = r.json()
    if j.get("rt_cd") != "0":
        log(f"⚠️ 매수가능조회 실패: {j.get('msg_cd')} {j.get('msg1')}")
        return None
    return int((j.get("output") or {}).get("ord_psbl_cash") or 0)


def place_order(tok, cano, prdt, code, qty, price):
    """현금 매수. hashkey 필수. 반환 (성공여부, 메시지, 주문번호)."""
    body = {"CANO": cano, "ACNT_PRDT_CD": prdt, "PDNO": code,
            "ORD_DVSN": ORD_DVSN, "ORD_QTY": str(qty),
            "ORD_UNPR": "0" if ORD_DVSN == "01" else str(price)}
    hr = requests.post(f"{BASE}/uapi/hashkey", timeout=20, json=body,
                       headers={"content-type": "application/json",
                                "appkey": TR_KEY, "appsecret": TR_SECRET})
    if hr.status_code != 200:
        return False, f"hashkey 실패 {hr.status_code}", ""
    h = _thdr(tok, TR_BUY, {"hashkey": hr.json()["HASH"]})
    r = requests.post(f"{BASE}/uapi/domestic-stock/v1/trading/order-cash",
                      headers=h, json=body, timeout=30)
    j = r.json()
    ok = j.get("rt_cd") == "0"
    odno = (j.get("output") or {}).get("ODNO", "")
    return ok, f"{j.get('msg_cd')} {str(j.get('msg1') or '').strip()}", odno


# ── 노션 Report DB ────────────────────────────────────────────────────
def latest_pick():
    """가장 최근 '추천이 실린' Report 행.

    daily_archive.py 는 장 마감 뒤 그날 날짜로 행을 쓰고, 그 안의 추천은 **다음 영업일에 살 종목**이다
    (9/4 행 본문이 '9/7(월) 에 살 종목' 이라고 쓴다). 그래서 아침에 오늘 날짜로 찾으면 안 나온다.
    → 종목코드가 채워진 가장 최근 행을 가져오고, 너무 오래됐으면 거부한다."""
    r = requests.post(f"https://api.notion.com/v1/databases/{NOTION_DB}/query", timeout=30,
                      headers={"Authorization": f"Bearer {NOTION_KEY}",
                               "Notion-Version": "2022-06-28",
                               "Content-Type": "application/json"},
                      json={"filter": {"property": "종목코드", "rich_text": {"is_not_empty": True}},
                            "sorts": [{"property": "날짜", "direction": "descending"}],
                            "page_size": 1})
    if r.status_code != 200:
        log(f"⚠️ Report DB 조회 실패 {r.status_code}: {r.text[:150]}")
        return None
    res = r.json().get("results") or []
    if not res:
        return None
    p = res[0]["properties"]

    def txt(k):
        return "".join(x["plain_text"] for x in (p.get(k, {}).get("rich_text") or []))

    asof = ((p.get("날짜") or {}).get("date") or {}).get("start") or ""
    # 리포트가 며칠 멈춰 있으면 낡은 추천을 사게 된다. 4일 넘으면 거부 (연휴 감안)
    try:
        age = (datetime.strptime(today(), "%Y-%m-%d") - datetime.strptime(asof, "%Y-%m-%d")).days
    except Exception:
        age = 99
    gate = ((p.get("게이트") or {}).get("select") or {}).get("name") or ""
    return {"page": res[0]["id"], "asof": asof, "age": age, "code": txt("종목코드"),
            "name": txt("추천종목"), "entry": (p.get("진입가") or {}).get("number"), "gate": gate}


def log_row(**kw):
    new = not os.path.exists(LOG)
    with open(LOG, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["ts", "date", "code", "name", "qty", "price",
                                          "gate", "result", "msg", "odno", "dry"])
        if new:
            w.writeheader()
        w.writerow(kw)


# ── 명령 ──────────────────────────────────────────────────────────────
def cmd_setup():
    """봇에게 아무 메시지나 보낸 뒤 실행하면 chat_id 를 찍어준다."""
    ups = _tg("getUpdates") or []
    if not ups:
        print("메시지가 없습니다. 텔레그램에서 봇에게 아무 말이나 먼저 보내세요.")
        return
    seen = {}
    for u in ups:
        m = u.get("message") or u.get("callback_query", {}).get("message") or {}
        c = m.get("chat") or {}
        if c.get("id"):
            seen[c["id"]] = c.get("username") or c.get("first_name") or ""
    for cid, who in seen.items():
        print(f"TELEGRAM_CHAT_ID={cid}   ({who})")


def cmd_status():
    s = load_state()
    print(json.dumps(s, ensure_ascii=False, indent=1) if s else "상태 없음")
    print(f"\nTRADE_ENABLED={'1 (실주문)' if ENABLED else '0 (드라이런)'} · 상한 {MAX_KRW:,}원 · "
          f"{'지정가' if ORD_DVSN == '00' else '시장가'} · 계좌 {'-'.join(account())}")


def cmd_propose():
    d = today()
    s = load_state()
    if s.get("date") == d and s.get("status") in ("proposed", "approved", "ordered", "rejected"):
        log(f"오늘({d}) 이미 처리됨: {s['status']} → 종료")
        return

    # 대체공휴일까지 잡는다 (요일 검사만으로는 못 거른다 — portfolio.py 가 2026-08-17 에 당했다)
    try:
        from portfolio import market_open_today
        if not market_open_today():
            log("휴장일 → 종료")
            return
    except Exception as e:
        log(f"  장운영 확인 실패(계속 진행): {type(e).__name__}")

    row = latest_pick()
    if not row:
        log("추천이 실린 Report 행이 하나도 없음 → 종료")
        return
    if row["age"] > 4:
        notify(f"⚠️ 가장 최근 추천이 {row['asof']} ({row['age']}일 전) 입니다. "
               f"리포트가 멈춘 것 같아 주문안을 만들지 않았습니다.")
        save_state({"date": d, "status": "skipped", "reason": f"낡은 추천 {row['asof']}"})
        return
    if not row["code"] or not row["name"]:
        notify(f"📭 <b>{d}</b> 오늘 살 종목이 없습니다.")
        save_state({"date": d, "status": "skipped", "reason": "추천 없음"})
        return

    px = quote(row["code"]) or int(row["entry"] or 0)
    if not px:
        log("현재가 조회 실패 → 종료")
        return

    tok = _trade_token()
    if not tok:
        return
    cano, prdt = account()
    cash = buyable(tok, cano, prdt, row["code"], px)
    if cash is None:
        notify("⚠️ 매수가능금액 조회에 실패했습니다. 주문안을 만들지 않았습니다.")
        return

    budget = min(MAX_KRW, cash)
    qty = budget // px
    gate_line = "🟢 게이트 통과" if row["gate"] == "통과" else f"🛑 게이트 {row['gate'] or '미상'}"

    if qty < 1:
        notify(f"{gate_line}\n📭 <b>{row['name']}</b> ({row['code']}) 를 추천했지만 "
               f"매수가능금액이 {cash:,}원이라 1주도 못 삽니다. (주가 {px:,}원)")
        save_state({"date": d, "status": "skipped", "reason": f"자금부족 {cash}"})
        return

    tokid = f"{d}:{row['code']}:{int(time.time())}"
    amount = qty * px
    notify(
        f"{gate_line}\n"
        f"<b>{row['name']}</b> ({row['code']})  {qty}주  {px:,}원\n"
        f"주문금액 <b>{amount:,}원</b> · 매수가능 {cash:,}원\n"
        f"{'지정가' if ORD_DVSN == '00' else '시장가'} · 계좌 {cano}-{prdt}\n"
        f"{row['asof']} 리포트 추천 · 진입가 {int(row['entry'] or 0):,}원"
        + ("" if ENABLED else "\n\n⚠️ TRADE_ENABLED=0 — 승인해도 실제 주문은 안 나갑니다(드라이런)")
        + (f"\n\n※ 게이트가 {row['gate']}입니다. 자체 백테스트는 게이트 미충족 구간에서 "
           f"이 전략이 손실이었습니다." if row["gate"] != "통과" else ""),
        buttons=[{"text": "✅ 매수 승인", "callback_data": f"buy:{tokid}"},
                 {"text": "❌ 거절", "callback_data": f"no:{tokid}"}])

    save_state({"date": d, "status": "proposed", "token": tokid, "code": row["code"],
                "name": row["name"], "qty": qty, "price": px, "gate": row["gate"],
                "sent": time.time(), "offset": s.get("offset", 0)})
    log(f"주문안 전송: {row['name']} {qty}주 × {px:,}원 = {amount:,}원 (게이트 {row['gate']})")


def cmd_poll():
    s = load_state()
    if s.get("status") != "proposed":
        log(f"대기 중인 주문안 없음 (상태 {s.get('status') or '없음'})")
        return
    if s.get("date") != today():
        s["status"] = "expired"; save_state(s)
        log("전날 주문안 → 만료")
        return
    if time.time() - s.get("sent", 0) > TTL_H * 3600:
        s["status"] = "expired"; save_state(s)
        notify(f"⌛ <b>{s['name']}</b> 주문안이 {TTL_H}시간 지나 만료됐습니다.")
        return

    ups = _tg("getUpdates", offset=s.get("offset", 0), timeout=0) or []
    decision = None
    for u in ups:
        s["offset"] = u["update_id"] + 1
        cq = u.get("callback_query")
        if not cq:
            continue
        if str((cq.get("from") or {}).get("id")) != str(TG_CHAT):
            continue                      # 다른 사람 탭은 무시
        data = cq.get("data") or ""
        if not data.endswith(s["token"]):
            continue                      # 지난 주문안의 버튼
        _tg("answerCallbackQuery", callback_query_id=cq["id"])
        decision = data.split(":", 1)[0]
    save_state(s)

    if decision is None:
        log("아직 회신 없음")
        return
    if decision == "no":
        s["status"] = "rejected"; save_state(s)
        notify(f"❌ <b>{s['name']}</b> 주문을 취소했습니다.")
        log_row(ts=datetime.now(KST).isoformat(), date=s["date"], code=s["code"],
                name=s["name"], qty=s["qty"], price=s["price"], gate=s["gate"],
                result="rejected", msg="", odno="", dry=int(not ENABLED))
        return

    now = datetime.now(KST)
    if not (now.replace(hour=9, minute=0) <= now <= now.replace(hour=15, minute=20)):
        log("승인됐지만 장중이 아님 — 다음 폴링에서 재시도")
        return

    if not ENABLED:
        s["status"] = "ordered"; s["dry"] = True; save_state(s)
        notify(f"🧪 드라이런: <b>{s['name']}</b> {s['qty']}주 {s['price']:,}원 주문을 "
               f"만들었지만 전송하지 않았습니다. (TRADE_ENABLED=1 로 켜세요)")
        log_row(ts=now.isoformat(), date=s["date"], code=s["code"], name=s["name"],
                qty=s["qty"], price=s["price"], gate=s["gate"], result="dryrun",
                msg="TRADE_ENABLED=0", odno="", dry=1)
        return

    tok = _trade_token()
    cano, prdt = account()
    ok, msg, odno = place_order(tok, cano, prdt, s["code"], s["qty"], s["price"])
    s["status"] = "ordered" if ok else "failed"
    s["msg"] = msg; s["odno"] = odno
    save_state(s)
    notify((f"✅ 주문 전송 완료 — <b>{s['name']}</b> {s['qty']}주 {s['price']:,}원\n주문번호 {odno}"
            if ok else f"❌ 주문 실패 — {s['name']}\n{msg}"))
    log_row(ts=now.isoformat(), date=s["date"], code=s["code"], name=s["name"],
            qty=s["qty"], price=s["price"], gate=s["gate"],
            result="ordered" if ok else "failed", msg=msg, odno=odno, dry=0)
    log(f"{'주문 성공' if ok else '주문 실패'}: {msg}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    {"propose": cmd_propose, "poll": cmd_poll,
     "setup": cmd_setup, "status": cmd_status}.get(cmd, cmd_status)()
