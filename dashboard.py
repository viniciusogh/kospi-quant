"""통합 대시보드 노션 페이지의 섹션 관리 (보유현황 / 리포트 토글).

한 페이지에 갱신 주기가 다른 두 섹션이 공존한다:
  [앵커 heading]  ← 영구. 절대 지우지 않는다 (삽입 기준점)
    보유현황 블록  ← 장중 30분마다 교체 (portfolio.py)
  [divider]        ← 영구
    리포트 토글들  ← 하루 1회 교체 (momentum_daily.py 등)

노션 API 로 확인한 제약 (2026-08-17 실측):
- append children 은 항상 맨 끝에 붙는다 → 상단 유지에는 `after` 파라미터가 필요. 2022-06-28 에서 작동함.
- `after` 로 맨 앞에 넣을 방법은 없다 → 영구 앵커 블록을 두고 그 뒤에 삽입한다.
- 토글>토글>문단 3단계는 한 요청에 되지만, **table 은 3단계에 못 들어간다**(400).
  → 종목 토글을 먼저 만들고 분기표는 그 토글 id 로 별도 append (2단계).
"""
import os, json
import requests

_DIR = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(_DIR, ".notion_dashboard.json")
TITLE = "💼 통합 포트폴리오"
ANCHOR_TEXT = "💼 통합 포트폴리오"
NV = "2022-06-28"          # 블록/DB 조작은 이 버전 고정 (파일업로드용 최신 버전은 DB쿼리를 깨뜨림)
API = "https://api.notion.com/v1"


def _h():
    return {"Authorization": f"Bearer {os.environ['NOTION_API_KEY']}",
            "Content-Type": "application/json", "Notion-Version": NV}


def _state():
    if os.path.exists(STATE):
        try:
            return json.load(open(STATE))
        except Exception:
            pass
    return {}


def _save(st):
    json.dump(st, open(STATE, "w"), ensure_ascii=False, indent=1)


def children(bid, limit=100):
    out, cur = [], None
    while True:
        p = {"page_size": limit}
        if cur:
            p["start_cursor"] = cur
        r = requests.get(f"{API}/blocks/{bid}/children", headers=_h(), params=p, timeout=25)
        if r.status_code != 200:
            return out
        j = r.json()
        out += j.get("results", [])
        if not j.get("has_more"):
            return out
        cur = j.get("next_cursor")


def _title_of(page):
    return "".join(t.get("plain_text", "") for t
                   in (page.get("properties", {}).get("title", {}).get("title") or []))


def page_id():
    """대시보드 페이지 id. 없으면 앵커+divider 뼈대까지 만들어 반환.
    안전장치: 기억한 id 의 제목이 다르면 건드리지 않고 새로 만든다(오삭제 방지)."""
    st = _state()
    pid = st.get("page_id")
    if not pid:      # portfolio.py 가 먼저 만든 페이지 인수 — 사용자가 이미 가진 링크를 유지
        old = os.path.join(_DIR, ".notion_portfolio_page.json")
        if os.path.exists(old):
            try:
                pid = json.load(open(old)).get("page_id")
                if pid:
                    print(f"  ℹ️ 기존 포트폴리오 페이지 인수 (링크 유지)")
            except Exception:
                pid = None
    if pid:
        r = requests.get(f"{API}/pages/{pid}", headers=_h(), timeout=25)
        if r.status_code == 200:
            j = r.json()
            if not j.get("archived") and _title_of(j) == TITLE:
                return pid
        print("  ⚠️ 기억한 대시보드 페이지가 내 것이 아니거나 사라짐 → 새로 만듦")
    parent = os.environ.get("NOTION_PARENT_PAGE_ID", "3324a00632f880fbb014d766d87a1079")
    r = requests.post(f"{API}/pages", headers=_h(), timeout=30, json={
        "parent": {"page_id": parent},
        "properties": {"title": {"title": [{"text": {"content": TITLE}}]}},
        "children": [
            {"object": "block", "type": "heading_1",
             "heading_1": {"rich_text": [{"type": "text", "text": {"content": ANCHOR_TEXT}}]}},
            {"object": "block", "type": "divider", "divider": {}},
        ]})
    r.raise_for_status()
    pid = r.json()["id"]
    ch = children(pid)
    _save({"page_id": pid, "anchor": ch[0]["id"], "divider": ch[1]["id"],
           "holdings": [], "reports": []})
    return pid


def _skeleton():
    """앵커·divider id 확보 (기존 페이지가 뼈대 없이 만들어졌던 경우 보정)."""
    pid = page_id()
    st = _state()
    if st.get("anchor") and st.get("divider") and st.get("migrated"):
        return pid, st
    ch = children(pid)
    anchor = next((b["id"] for b in ch if b["type"] == "heading_1"), None)
    div = next((b["id"] for b in ch if b["type"] == "divider"), None)
    new = []
    if not anchor:
        r = requests.patch(f"{API}/blocks/{pid}/children", headers=_h(), timeout=25, json={
            "children": [{"object": "block", "type": "heading_1",
                          "heading_1": {"rich_text": [{"type": "text",
                                                       "text": {"content": ANCHOR_TEXT}}]}}]})
        anchor = r.json()["results"][0]["id"]; new.append(anchor)
    if not div:
        r = requests.patch(f"{API}/blocks/{pid}/children", headers=_h(), timeout=25,
                           json={"after": anchor,
                                 "children": [{"object": "block", "type": "divider", "divider": {}}]})
        div = r.json()["results"][0]["id"]
    st.update({"page_id": pid, "anchor": anchor, "divider": div})
    st.setdefault("holdings", []); st.setdefault("reports", [])
    _save(st)

    # 일회성 정리: 앵커는 항상 첫 블록이어야 한다. 앞에 뭔가 있으면 구버전 구현(페이지에
    # 직접 append 하던 방식)이 남긴 잔재다. 제목 검증을 통과한 내 페이지에서만, 앵커 앞만 지운다.
    ch = children(pid)
    idx = next((i for i, b in enumerate(ch) if b["id"] == anchor), 0)
    if idx > 0:
        stale = [b["id"] for b in ch[:idx]]
        print(f"  🧹 구버전 잔재 블록 {len(stale)}개 정리 (앵커 앞)")
        _delete(stale)
    st["migrated"] = True
    _save(st)
    return pid, st


def _delete(ids):
    for b in ids:
        requests.delete(f"{API}/blocks/{b}", headers=_h(), timeout=20)


def set_holdings(blocks):
    """보유현황 섹션 교체 — 앵커 바로 뒤에 넣어 항상 페이지 상단에 유지. 리포트 토글은 안 건드림."""
    pid, st = _skeleton()
    _delete(st.get("holdings", []))
    ids = []
    after = st["anchor"]
    for b in blocks:                      # after 로 순서 유지하려면 1개씩 (묶으면 역순 위험)
        r = requests.patch(f"{API}/blocks/{pid}/children", headers=_h(), timeout=30,
                           json={"after": after, "children": [b]})
        if r.status_code != 200:
            print(f"  ⚠️ 보유현황 블록 추가 실패 {r.status_code}: {r.text[:140]}")
            break
        nid = r.json()["results"][0]["id"]
        ids.append(nid); after = nid
    st["holdings"] = ids
    _save(st)
    return pid


def clear_reports():
    """리포트 섹션 비우기 (하루 1회, 첫 리포트가 붙기 전에 호출)."""
    pid, st = _skeleton()
    _delete(st.get("reports", []))
    st["reports"] = []
    _save(st)
    return pid


def _append(bid, blocks, tries=3):
    """일시 지연·5xx·429 재시도. 1개 실패가 리포트 전체를 중단시키지 않게."""
    import time
    for attempt in range(tries):
        try:
            r = requests.patch(f"{API}/blocks/{bid}/children", headers=_h(),
                               json={"children": blocks}, timeout=40)
            time.sleep(0.35)
            if r.status_code == 200:
                return r.json().get("results", [])
            print(f"  ⚠️ append {r.status_code} ({attempt+1}/{tries}): {r.text[:120]}")
            if r.status_code < 500 and r.status_code != 429:
                return None            # 4xx(429 제외)는 재시도 무의미
        except Exception as e:
            print(f"  ⚠️ append 예외({attempt+1}/{tries}): {str(e)[:110]}")
        time.sleep(2 * (attempt + 1))
    return None


def add_report(toggle_title, header_blocks, items=None, color="gray_background"):
    """리포트 토글 1개를 페이지 맨 끝에 추가.

    header_blocks: 토글 바로 안에 들어갈 머리말 (콜아웃·heading 등)
    items: [(자식토글 블록, [2차 블록…])] — 자식 토글을 1개씩 append 하고(요청당 100블록 한도 회피)
           2차 블록(table 등, 3단계 불가)은 그 토글 id 로 따로 넣는다.
    """
    pid, st = _skeleton()
    r = _append(pid, [{"object": "block", "type": "toggle", "toggle": {
        "rich_text": [{"type": "text", "text": {"content": toggle_title},
                       "annotations": {"bold": True}}],
        "color": color, "children": header_blocks or []}}])
    if not r:
        print(f"  ⚠️ 리포트 토글 생성 실패: {toggle_title[:40]}")
        return None
    tid = r[0]["id"]
    st.setdefault("reports", []).append(tid)
    _save(st)

    for tog, extra in (items or []):
        res = _append(tid, [tog])
        if not res:
            continue
        if extra:
            _append(res[0]["id"], extra)
    return tid


def url():
    st = _state()
    pid = st.get("page_id", "")
    return f"https://notion.so/{pid.replace('-', '')}" if pid else ""
