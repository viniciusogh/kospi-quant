"""지수 현황 — 코스피/코스피선물/나스닥/USD-KRW 현재가를 일별 리포트 페이지의 콜아웃 1개로 매시 갱신.
데이터: KIS(코스피지수·선물 실시간) + yfinance(나스닥·환율). GitHub Actions 매시 실행.
⚠️ DB row 아카이브/삭제 절대 금지(2026-06-24 리포트 삭제 사고). 콜아웃만 제자리 갱신, 차트·별도페이지 없음."""
import os, json, time, sys
import _env  # .env 자동 로드
import requests
from datetime import datetime, timezone, timedelta
import yfinance as yf

KST = timezone(timedelta(hours=9))
APP_KEY    = os.environ["APP_KEY"]
APP_SECRET = os.environ["APP_SECRET"]
KIS_BASE   = "https://openapi.koreainvestment.com:9443"
TOKEN_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".kis_token.json")

SENTINEL      = "📈 지수 현황"    # 일별 페이지 내 콜아웃 식별 마커(매시 이 블록만 제자리 갱신)
NOTION_VER_DB = "2022-06-28"     # DB 쿼리·블록 (수급.py 검증). 신버전은 database→data source 분리로 날짜필터 빈결과


def log(msg): print(f"[{datetime.now(KST).strftime('%H:%M:%S')}] {msg}")

# ──────────────────────────── KIS ────────────────────────────
def get_token():
    # 로컬: 캐시 재사용(23h 유효). Actions: 캐시 없으면 발급.
    try:
        c = json.load(open(TOKEN_CACHE))
        if time.time() - c.get("ts", 0) < 23 * 3600 and c.get("token"):
            return c["token"]
    except Exception:
        pass
    # KIS 토큰 엔드포인트 일시 ConnectTimeout 잦음(AGENTS: 6/16 전멸) → 재시도+백오프
    tok = None
    for attempt in range(1, 5):
        try:
            r = requests.post(KIS_BASE + "/oauth2/tokenP", timeout=15,
                              json={"grant_type": "client_credentials", "appkey": APP_KEY, "appsecret": APP_SECRET})
            r.raise_for_status()
            tok = r.json()["access_token"]
            break
        except Exception as e:
            log(f"⚠️ 토큰 발급 실패({attempt}/4): {str(e)[:80]}")
            if attempt < 4:
                time.sleep(min(5 * attempt, 20))
    if not tok:
        raise RuntimeError("KIS 토큰 발급 최종 실패")
    try:
        json.dump({"token": tok, "ts": time.time()}, open(TOKEN_CACHE, "w"))
    except Exception:
        pass
    return tok


def kis_get(tr, url, params, tok):
    h = {"authorization": f"Bearer {tok}", "appkey": APP_KEY, "appsecret": APP_SECRET,
         "tr_id": tr, "custtype": "P"}
    for attempt in range(1, 4):
        try:
            r = requests.get(KIS_BASE + url, headers=h, params=params, timeout=10)
            return r.json()
        except Exception as e:
            log(f"⚠️ KIS {tr} 실패({attempt}/3): {str(e)[:80]}")
            time.sleep(2 * attempt)
    raise RuntimeError(f"KIS {tr} 최종 실패")


def get_kospi_index(tok):
    j = kis_get("FHPUP02100000", "/uapi/domestic-stock/v1/quotations/inquire-index-price",
                {"FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": "0001"}, tok)
    o = j["output"]
    return float(o["bstp_nmix_prpr"]), float(o["bstp_nmix_prdy_vrss"])


def get_kospi_future(tok):
    j = kis_get("FHMIF10000000", "/uapi/domestic-futureoption/v1/quotations/inquire-price",
                {"FID_COND_MRKT_DIV_CODE": "F", "FID_INPUT_ISCD": "101000"}, tok)
    o = j["output1"]
    return float(o["futs_prpr"]), float(o["futs_prdy_vrss"]), o.get("hts_kor_isnm", "")

# ──────────────────────────── yfinance ────────────────────────────
def yf_last(ticker):
    """최근 60분봉 마지막 종가(현재가 근사)."""
    return float(yf.Ticker(ticker).history(period="5d", interval="60m")["Close"].dropna().iloc[-1])

def yf_prev_close(ticker):
    d = yf.Ticker(ticker).history(period="5d")["Close"].dropna()
    return float(d.iloc[-2]) if len(d) >= 2 else float(d.iloc[-1])

# ──────────────────────────── Notion ────────────────────────────
def nheaders(key, json_ct=True):
    h = {"Authorization": f"Bearer {key}", "Notion-Version": NOTION_VER_DB}
    if json_ct:
        h["Content-Type"] = "application/json"
    return h


def _nreq(method, url, *, retry=3, timeout=30, **kw):
    """Notion 일시 ReadTimeout·5xx·429 재시도. 4xx(429 제외)는 즉시 반환."""
    last = None
    for attempt in range(1, retry + 1):
        try:
            r = requests.request(method, url, timeout=timeout, **kw)
            if r.status_code < 400 or (400 <= r.status_code < 500 and r.status_code != 429):
                return r
            log(f"  ⚠️ Notion {r.status_code} ({attempt}/{retry}): {r.text[:120]}")
            last = r
        except Exception as e:
            log(f"  ⚠️ Notion 요청 예외({attempt}/{retry}): {str(e)[:100]}")
        time.sleep(2 * attempt)
    if last is not None:
        return last
    raise RuntimeError(f"Notion {method} {url} 최종 실패")


def _row_title(row):
    for prop in row.get("properties", {}).values():
        if prop.get("type") == "title":
            t = prop.get("title", [])
            return t[0]["plain_text"] if t else ""
    return ""


def query_today_rows(key, date_str, size=10):
    db_id = os.environ["NOTION_DAILY_DB_ID"]
    r = _nreq("POST", f"https://api.notion.com/v1/databases/{db_id}/query", headers=nheaders(key),
              json={"filter": {"property": "날짜", "date": {"equals": date_str}}, "page_size": size})
    return r.json().get("results", [])


def list_children(key, parent_id):
    out, cursor = [], None
    while True:
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        j = _nreq("GET", f"https://api.notion.com/v1/blocks/{parent_id}/children",
                  headers=nheaders(key), params=params).json()
        out.extend(j.get("results", []))
        if not j.get("has_more"):
            return out
        cursor = j.get("next_cursor")


def _is_my_callout(b):
    if b.get("type") != "callout":
        return False
    rt = b["callout"].get("rich_text", [])
    return bool(rt) and rt[0].get("plain_text", "").startswith(SENTINEL)


def cleanup_my_extras(key, page_id):
    """명확히 '내 것'만 제거: '📈 지수 현황' child page + 내 지수 콜아웃(SENTINEL).
    이미지·리포트(다른 제목 child_page)·다른 블록은 절대 안 건드림 (오삭제 위험 0)."""
    for b in list_children(key, page_id):
        t = b.get("type")
        mine = (t == "child_page" and b["child_page"].get("title") == SENTINEL) or _is_my_callout(b)
        if mine:
            try:
                _nreq("DELETE", f"https://api.notion.com/v1/blocks/{b['id']}", headers=nheaders(key))
                log(f"  🧹 잔재 제거({t}): {b['id']}")
                time.sleep(0.2)
            except Exception as e:
                log(f"  ⚠️ 잔재 제거 실패: {e}")


def append_callout(key, page_id, lines, asof):
    rich = [{"type": "text", "text": {"content": f"{SENTINEL} · {asof} KST 기준\n"},
             "annotations": {"bold": True}},
            {"type": "text", "text": {"content": "\n".join(lines)}}]
    body = {"icon": {"emoji": "🕒"}, "color": "gray_background", "rich_text": rich}
    _nreq("PATCH", f"https://api.notion.com/v1/blocks/{page_id}/children", headers=nheaders(key),
          json={"children": [{"object": "block", "type": "callout", "callout": body}]})


def update_notion(key, lines, asof, date_str):
    """⚠️ DB row 아카이브/삭제 절대 안 함(2026-06-24 사고 재발방지)."""
    rows = query_today_rows(key, date_str)
    log(f"  오늘({date_str}) DB row {len(rows)}개: {[_row_title(x) for x in rows]}")
    if not rows:
        db_id = os.environ["NOTION_DAILY_DB_ID"]
        r = _nreq("POST", "https://api.notion.com/v1/pages", headers=nheaders(key),
                  json={"parent": {"database_id": db_id},
                        "properties": {"이름": {"title": [{"text": {"content": "준비 중"}}]},
                                       "날짜": {"date": {"start": date_str}}}})
        picked = r.json()["id"]
        append_callout(key, picked, lines, asof)
        return picked
    # 리포트(child_page) 최다 행 = 실제 일별 페이지
    picked, best_n = rows[0]["id"], -1
    for row in rows:
        n = sum(1 for c in list_children(key, row["id"]) if c.get("type") == "child_page")
        if n > best_n:
            picked, best_n = row["id"], n
    if len(rows) > 1:
        log(f"  ⚠️ row 여러개 — 자식 {best_n}개 행 선택, 나머지는 콜아웃/차트 잔재만 청소")
    # 모든 행에서 내 잔재(차트·지수현황페이지·구콜아웃) 제거 → 콜아웃은 picked 에 1개만
    for row in rows:
        cleanup_my_extras(key, row["id"])
    append_callout(key, picked, lines, asof)
    return picked


def restore_pages(key, ids):
    """아카이브된 페이지 복구 (2026-06-24 잘못 아카이브한 리포트 행 되살리기)."""
    for pid in [x.strip() for x in ids if x.strip()]:
        try:
            r = _nreq("PATCH", f"https://api.notion.com/v1/pages/{pid}",
                      headers=nheaders(key), json={"archived": False})
            log(f"  ♻️ 복구({r.status_code}): {pid}")
        except Exception as e:
            log(f"  ⚠️ 복구 실패 {pid}: {e}")


def fmt(name, cur, chg):
    prev = cur - chg
    pct = 100 * chg / prev if prev else 0
    arrow = "🔺" if chg >= 0 else "🔻"
    return f"{name} {cur:,.2f}  {arrow}{abs(chg):,.2f} ({pct:+.2f}%)"

# ──────────────────────────── main ────────────────────────────
def main():
    now_kst = datetime.now(KST)
    asof = now_kst.strftime("%Y-%m-%d %H:%M")
    date_str = now_kst.strftime("%Y-%m-%d")

    # 복구 모드: INDEX_RESTORE=id1,id2 → 잘못 아카이브한 페이지 되살리고 종료
    restore = os.environ.get("INDEX_RESTORE")
    if restore:
        restore_pages(os.environ["NOTION_API_KEY"], restore.split(","))
        return

    tok = get_token()
    kospi_cur, kospi_chg = get_kospi_index(tok)
    fut_cur, fut_chg, fut_name = get_kospi_future(tok)
    log(f"KOSPI {kospi_cur} ({kospi_chg:+}) | FUT {fut_cur} ({fut_chg:+}) {fut_name}")

    ndx_cur = yf_last("^IXIC");    ndx_chg = ndx_cur - yf_prev_close("^IXIC")
    fx_cur  = yf_last("USDKRW=X"); fx_chg  = fx_cur - yf_prev_close("USDKRW=X")

    lines = [
        fmt("코스피", kospi_cur, kospi_chg),
        fmt(f"선물 {fut_name.strip()}", fut_cur, fut_chg),
        fmt("나스닥", ndx_cur, ndx_chg),
        fmt("USD/KRW", fx_cur, fx_chg),
    ]
    print("\n".join(lines))

    key = os.environ.get("NOTION_API_KEY")
    if not key:
        log("NOTION_API_KEY 없음 → Notion 업로드 생략 (로컬). Actions 에선 업로드됨")
        return
    try:
        pid = update_notion(key, lines, asof, date_str)
        log(f"✅ Notion 갱신 완료: {pid}")
    except Exception as e:
        log(f"❌ Notion 갱신 실패: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
