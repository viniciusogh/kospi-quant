"""지수 현황 대시보드 — 코스피/코스피선물/나스닥/USD-KRW 1주일 시간봉 차트를 Notion 에 매시 갱신.
데이터: KIS(코스피지수·선물 실시간) + yfinance(나스닥·환율, 시간봉). GitHub Actions 매시 실행.
선물 과거 시계열은 연속근월물 코드에 없어 → 현물(KS200) 추이 배경 + 오늘 선물값/베이시스 마킹."""
import os, io, json, time, sys
import _env  # .env 자동 로드
import requests
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import yfinance as yf

KST = timezone(timedelta(hours=9))
APP_KEY    = os.environ["APP_KEY"]
APP_SECRET = os.environ["APP_SECRET"]
KIS_BASE   = "https://openapi.koreainvestment.com:9443"
TOKEN_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".kis_token.json")

PARENT_PAGE_ID  = os.environ.get("NOTION_PARENT_PAGE_ID", "3324a00632f880fbb014d766d87a1079")
SENTINEL        = "📈 지수 현황"    # 일별 페이지 내 콜아웃 식별 마커(매시 이 블록만 교체)
NOTION_VER_DB   = "2022-06-28"     # DB 쿼리·블록 (수급.py 검증). 신버전은 database→data source 분리로 날짜필터 빈결과
NOTION_VER_FILE = "2026-03-11"     # 파일 업로드 + 이미지 블록 최소 버전


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
    cur = float(o["bstp_nmix_prpr"]); chg = float(o["bstp_nmix_prdy_vrss"])
    return cur, chg


def get_kospi_future(tok):
    j = kis_get("FHMIF10000000", "/uapi/domestic-futureoption/v1/quotations/inquire-price",
                {"FID_COND_MRKT_DIV_CODE": "F", "FID_INPUT_ISCD": "101000"}, tok)
    o = j["output1"]
    cur = float(o["futs_prpr"]); chg = float(o["futs_prdy_vrss"])
    return cur, chg, o.get("hts_kor_isnm", "")

# ──────────────────────────── yfinance ────────────────────────────
def yf_week(ticker):
    """1주일 60분봉 종가 시리즈."""
    h = yf.Ticker(ticker).history(period="7d", interval="60m")["Close"].dropna()
    return h

def yf_prev_close(ticker):
    d = yf.Ticker(ticker).history(period="5d")["Close"].dropna()
    return float(d.iloc[-2]) if len(d) >= 2 else float(d.iloc[-1])

# ──────────────────────────── 차트 ────────────────────────────
def render(panels, asof):
    plt.rcParams.update({"font.size": 10, "axes.titlesize": 12, "figure.dpi": 130})
    fig, axes = plt.subplots(2, 2, figsize=(13, 8))
    fig.suptitle(f"Index Dashboard  |  {asof} KST  |  1-week (60m)", fontsize=15, fontweight="bold")
    for ax, p in zip(axes.flat, panels):
        ser, cur, chg = p["ser"], p["cur"], p["chg"]
        up = chg >= 0
        col = "#d62728" if up else "#1f77b4"   # 한국식: 상승=빨강
        ax.plot(ser.index, ser.values, color="#555", lw=1.3)
        ax.fill_between(ser.index, ser.values, ser.min(), color=col, alpha=0.06)
        ax.axhspan(ser.min(), ser.max(), color="#ffe9a8", alpha=0.25, zorder=0)
        ax.scatter([ser.index[-1]], [cur], s=95, color=col, zorder=5, edgecolor="white", lw=1.3)
        arrow = "▲" if up else "▼"
        prev = cur - chg
        pct = 100 * chg / prev if prev else 0
        sub = f"{cur:,.2f}   {arrow}{abs(chg):,.2f} ({pct:+.2f}%)"
        if p.get("basis") is not None:
            sub += f"   basis {p['basis']:+.2f}"
        ax.set_title(f"{p['title']}\n{sub}", color=col, fontweight="bold", loc="left")
        wk = 100 * (cur / ser.iloc[0] - 1) if ser.iloc[0] else 0
        box = f"1wk {wk:+.1f}%\nrange {ser.min():,.1f} ~ {ser.max():,.1f}"
        ax.text(0.015, 0.97, box, transform=ax.transAxes, va="top", fontsize=8.5,
                bbox=dict(boxstyle="round", fc="white", ec="#ccc", alpha=0.85))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
        ax.grid(alpha=0.25)
    plt.tight_layout(rect=[0, 0, 1, 0.95])
    buf = io.BytesIO()
    plt.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()

# ──────────────────────────── Notion ────────────────────────────
def nheaders(key, ver=NOTION_VER_DB, json_ct=True):
    h = {"Authorization": f"Bearer {key}", "Notion-Version": ver}
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


def get_date_page(key, date_str):
    """오늘 날짜 리포트 페이지(=NOTION_DAILY_DB_ID DB row) (id, title). 수급.py 와 동일 규약.
    여러 row 면 '준비 중' 아닌(=리포트 제목 붙은) 걸 우선. 없으면 '준비 중' 생성(daily flow 와 공유)."""
    rows = query_today_rows(key, date_str)
    log(f"  오늘({date_str}) DB row {len(rows)}개: {[_row_title(x) for x in rows]}")
    if rows:
        named = [x for x in rows if _row_title(x) != "준비 중"]
        pick = (named or rows)[0]
        return pick["id"], _row_title(pick)
    db_id = os.environ["NOTION_DAILY_DB_ID"]
    r = _nreq("POST", "https://api.notion.com/v1/pages", headers=nheaders(key),
              json={"parent": {"database_id": db_id},
                    "properties": {"이름": {"title": [{"text": {"content": "준비 중"}}]},
                                   "날짜": {"date": {"start": date_str}}}})
    return r.json()["id"], "준비 중"


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


def archive_page(key, pid):
    _nreq("PATCH", f"https://api.notion.com/v1/pages/{pid}", headers=nheaders(key),
          json={"archived": True})


def cleanup_legacy(key, keep_id, keep_title, date_str):
    """과거 잘못 만든 구조 자가정리:
    ①루트에 박은 '지수 현황' child page  ②중복 '준비 중' row.
    중복 row 판별: keep 가 리포트 제목 행이면(=오늘 본 행 확정) 나머지 '준비 중' row 는 전부 잔재 → 아카이브.
    또는 '📈 지수 현황' child page 를 가진 옛 구조 행. (아침에 아직 이름 안 붙은 정상 '준비 중' 은 보존)"""
    for b in list_children(key, PARENT_PAGE_ID):
        if b.get("type") == "child_page" and b["child_page"].get("title") == SENTINEL:
            archive_page(key, b["id"]); log(f"  🧹 루트 오배치 페이지 아카이브: {b['id']}")
    real_named = keep_title != "준비 중"
    for row in query_today_rows(key, date_str):
        if row["id"] == keep_id:
            continue
        title = _row_title(row)
        kids = list_children(key, row["id"])
        has_mine = any(c.get("type") == "child_page" and c["child_page"].get("title") == SENTINEL
                       for c in kids)
        if has_mine or (real_named and title == "준비 중"):
            archive_page(key, row["id"]); log(f"  🧹 중복 '{title}' row 아카이브: {row['id']}")


def remove_my_blocks(key, page_id):
    """일별 페이지 직속 children 중 내 콜아웃(SENTINEL)·이미지만 삭제 (리포트 링크는 보존)."""
    for b in list_children(key, page_id):
        if _is_my_callout(b) or b.get("type") == "image":
            try:
                _nreq("DELETE", f"https://api.notion.com/v1/blocks/{b['id']}", headers=nheaders(key))
                time.sleep(0.2)
            except Exception as e:
                log(f"  ⚠️ 블록 삭제 실패: {e}")


def upload_png(key, png_bytes):
    r = _nreq("POST", "https://api.notion.com/v1/file_uploads",
              headers=nheaders(key, NOTION_VER_FILE),
              json={"filename": "index_dashboard.png", "content_type": "image/png"})
    fu = r.json()
    files = {"file": ("index_dashboard.png", png_bytes, "image/png")}
    _nreq("POST", f"https://api.notion.com/v1/file_uploads/{fu['id']}/send",
          headers=nheaders(key, NOTION_VER_FILE, json_ct=False), files=files, timeout=60)
    return fu["id"]


def update_notion(key, png_bytes, lines, asof, date_str):
    page_id, page_title = get_date_page(key, date_str)
    cleanup_legacy(key, page_id, page_title, date_str)
    remove_my_blocks(key, page_id)
    file_id = upload_png(key, png_bytes)
    # 줄별 분리: 헤더 1줄 + 지수별 1줄씩
    rich = [{"type": "text", "text": {"content": f"{SENTINEL} · {asof} KST 기준\n"},
             "annotations": {"bold": True}}]
    rich.append({"type": "text", "text": {"content": "\n".join(lines)}})
    children = [
        {"object": "block", "type": "callout",
         "callout": {"icon": {"emoji": "🕒"}, "color": "gray_background", "rich_text": rich}},
        {"object": "block", "type": "image",
         "image": {"type": "file_upload", "file_upload": {"id": file_id}}},
    ]
    _nreq("PATCH", f"https://api.notion.com/v1/blocks/{page_id}/children",
          headers=nheaders(key, NOTION_VER_FILE), json={"children": children})
    return page_id


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
    tok = get_token()

    kospi_cur, kospi_chg = get_kospi_index(tok)
    fut_cur, fut_chg, fut_name = get_kospi_future(tok)
    log(f"KOSPI {kospi_cur} ({kospi_chg:+}) | FUT {fut_cur} ({fut_chg:+}) {fut_name}")

    kospi_ser = yf_week("^KS11")
    ks200_ser = yf_week("^KS200")
    ndx_ser   = yf_week("^IXIC")
    fx_ser    = yf_week("USDKRW=X")

    ndx_cur = float(ndx_ser.iloc[-1]); ndx_chg = ndx_cur - yf_prev_close("^IXIC")
    fx_cur  = float(fx_ser.iloc[-1]);  fx_chg  = fx_cur - yf_prev_close("USDKRW=X")

    # 실시간 현재값을 마지막 점으로 반영 (시간봉 마지막 봉이 약간 지연되므로)
    now = pd.Timestamp(datetime.now(KST))
    kospi_ser.loc[now] = kospi_cur

    panels = [
        {"title": "KOSPI (Composite)",       "ser": kospi_ser, "cur": kospi_cur, "chg": kospi_chg},
        {"title": f"KOSPI200 Futures ({fut_name.strip()})", "ser": ks200_ser, "cur": fut_cur,
         "chg": fut_chg, "basis": fut_cur - float(ks200_ser.iloc[-1])},
        {"title": "NASDAQ (Composite)",      "ser": ndx_ser,   "cur": ndx_cur,   "chg": ndx_chg},
        {"title": "USD / KRW",               "ser": fx_ser,    "cur": fx_cur,    "chg": fx_chg},
    ]
    png = render(panels, asof)

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "latest_index_dashboard.png")
    open(out, "wb").write(png)
    log(f"차트 저장: {out} ({len(png)//1024} KB)")

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
        pid = update_notion(key, png, lines, asof, date_str)
        log(f"✅ Notion 갱신 완료: {pid}")
    except Exception as e:
        log(f"❌ Notion 갱신 실패: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
