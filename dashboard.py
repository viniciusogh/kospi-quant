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
ANCHOR_TEXT = "💰 보유 현황"      # 페이지 제목과 중복되지 않도록. 삽입 기준점 겸 머리말
NV = "2022-06-28"          # 블록/DB 조작
NV_UPLOAD = "2026-03-11"   # 파일 업로드 API 최소 버전. 이 버전으로 DB 날짜필터를 쓰면 빈결과가 되므로 업로드에만 사용          # 블록/DB 조작은 이 버전 고정 (파일업로드용 최신 버전은 DB쿼리를 깨뜨림)
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


def _find_by_title(parent):
    """부모 페이지의 자식 중 제목이 일치하는 child_page 를 찾는다 (상태파일 대체).
    반환 (page_id, 조회성공여부). 조회 자체가 실패했는데 '없음'으로 오판하면
    대시보드 페이지가 중복 생성되므로 성공여부를 분리해서 돌려준다."""
    try:
        kids = children(parent)
    except Exception:
        return None, False
    if not kids:                      # 부모 페이지에 자식이 0개일 수는 없다(Report DB 등 존재) → 조회 실패로 본다
        return None, False
    for b in kids:
        if b.get("type") == "child_page" and b["child_page"].get("title") == TITLE:
            return b["id"], True
    return None, True


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
    found, ok = _find_by_title(parent)       # 상태파일 없는 환경(GitHub Actions)에서도 같은 페이지를 찾는다
    if found:
        st["page_id"] = found
        _save(st)
        return found
    if not ok:                               # 조회 실패 → 새로 만들지 않는다(중복 페이지 방지)
        raise RuntimeError("노션 부모 페이지 조회 실패 — 대시보드 페이지 생성을 보류합니다")
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


def _append(bid, blocks, after=None, tries=3):
    """일시 지연·5xx·429 재시도. 1개 실패가 리포트 전체를 중단시키지 않게.
    after: 그 블록 바로 뒤에 삽입(기본은 맨 끝)."""
    import time
    body = {"children": blocks}
    if after:
        body["after"] = after
    for attempt in range(tries):
        try:
            r = requests.patch(f"{API}/blocks/{bid}/children", headers=_h(),
                               json=body, timeout=40)
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


# 리포트 슬롯 — 페이지에 늘 이 순서로 놓인다. 상태파일 없이 제목으로 판별하므로
# GitHub Actions(상태파일 없음)와 로컬이 같은 페이지를 일관되게 갱신할 수 있다.
# 검사 순서 주의: "추세게이트" 를 "모멘텀 추천" 보다 먼저 봐야 부분일치 충돌이 없다.
SLOT_RULES = [(1, "내 보유종목"), (2, "핵심 요약"), (3, "섹터 장세"),
              (5, "추세게이트"), (4, "모멘텀 추천"), (6, "유튜브")]


def _slot_of(title):
    for slot, needle in SLOT_RULES:        # 게이트를 모멘텀보다 먼저 검사 (부분일치 충돌 방지)
        if needle in title:
            return slot
    return 99


def _layout(pid):
    """페이지를 앵커/divider 기준으로 나눈다. 블록 id 를 저장하지 않고 매번 구조에서 판별."""
    ch = children(pid)
    ai = next((i for i, b in enumerate(ch) if b["type"] == "heading_1"), None)
    di = next((i for i, b in enumerate(ch) if b["type"] == "divider"), None)
    if ai is None or di is None or di < ai:
        return ch, None, None, [], []
    return ch, ch[ai]["id"], ch[di]["id"], ch[ai + 1:di], ch[di + 1:]


def set_holdings(blocks):
    """보유현황 섹션 교체 — 앵커와 divider 사이만 갈아낀다. 리포트 토글은 건드리지 않음."""
    pid, _ = _skeleton()
    _, anchor, _, mid, _ = _layout(pid)
    if anchor is None:
        print("  ⚠️ 대시보드 뼈대(앵커/divider) 를 찾지 못함 — 중단")
        return pid
    _delete([b["id"] for b in mid])
    after = anchor
    for b in blocks:                      # after 로 순서 유지하려면 1개씩 (묶으면 역순 위험)
        r = _append(pid, [b], after=after)
        if not r:
            break
        after = r[0]["id"]
    return pid


def clear_reports():
    """리포트 섹션 비우기 — divider 뒤 전부. (하루 1회, 첫 리포트 붙기 전)"""
    pid, _ = _skeleton()
    _, _, div, _, tail = _layout(pid)
    if div is None:
        return pid
    _delete([b["id"] for b in tail])
    return pid


def _slot_slot(tail, div, slot, keep_title=None):
    """슬롯 삽입 위치와 기존 토글을 찾는다.
    keep_title 이 주어지고 제목까지 같으면 그 토글을 지우지 않고 반환(이어붙이기용).
    반환 (after_block_id, 재사용할_토글_id 또는 None)."""
    after, reuse = div, None
    for b in tail:
        if b["type"] != "toggle":
            continue
        t = "".join(x.get("plain_text", "") for x in b["toggle"]["rich_text"])
        sv = _slot_of(t)
        if sv == slot:
            if keep_title is not None and t == keep_title:
                reuse = b["id"]          # 같은 날 같은 리포트 → 재사용
            else:
                _delete([b["id"]])       # 이전 실행분·날짜 바뀜 → 교체
            continue
        if sv < slot:
            after = b["id"]
    return after, reuse


def get_or_create_report(toggle_title, color="gray_background"):
    """리포트 토글을 '있으면 재사용, 없으면 생성'. 유튜브처럼 하루 동안 이어붙이는 경우에 쓴다.
    같은 슬롯에 제목이 다른(=어제자) 토글이 있으면 지우고 새로 만든다."""
    pid, _ = _skeleton()
    _, _, div, _, tail = _layout(pid)
    if div is None:
        print("  ⚠️ 대시보드 뼈대를 찾지 못함 — 중단")
        return None
    after, reuse = _slot_slot(tail, div, _slot_of(toggle_title), keep_title=toggle_title)
    if reuse:
        return reuse
    r = _append(pid, [{"object": "block", "type": "toggle", "toggle": {
        "rich_text": [{"type": "text", "text": {"content": toggle_title},
                       "annotations": {"bold": True}}],
        "color": color, "children": []}}], after=after)
    return r[0]["id"] if r else None


def add_report(toggle_title, header_blocks, items=None, color="gray_background"):
    """리포트 토글 upsert. 같은 슬롯이 이미 있으면 지우고 같은 자리에 다시 넣는다.

    같은 워크플로가 하루에 두 번 돌아도(오늘 daily_quant 2회 실행) 중복되지 않는다.
    items: [(자식토글, [2차블록…])] — 자식은 1개씩 append(요청당 100블록 한도), table 은 2차로.
    """
    pid, _ = _skeleton()
    _, anchor, div, _, tail = _layout(pid)
    if div is None:
        print("  ⚠️ 대시보드 뼈대를 찾지 못함 — 중단")
        return None
    after, _ = _slot_slot(tail, div, _slot_of(toggle_title))   # 같은 슬롯 기존분은 교체

    r = _append(pid, [{"object": "block", "type": "toggle", "toggle": {
        "rich_text": [{"type": "text", "text": {"content": toggle_title},
                       "annotations": {"bold": True}}],
        "color": color, "children": header_blocks or []}}], after=after)
    if not r:
        print(f"  ⚠️ 리포트 토글 생성 실패: {toggle_title[:40]}")
        return None
    tid = r[0]["id"]
    for tog, extra in (items or []):
        res = _append(tid, [tog])
        if not res:
            continue
        if extra:
            _append(res[0]["id"], extra)
    return tid


def upload_image(png_bytes, filename="chart.png"):
    """노션 파일 업로드 → file_upload id. 업로드 API 는 최신 버전을 요구하므로 이 호출만 NV_UPLOAD.
    (그 버전으로 /databases/{id}/query 를 쓰면 날짜필터가 빈결과가 된다 — index_ticker 때 확인된 함정)"""
    h = {"Authorization": f"Bearer {os.environ['NOTION_API_KEY']}",
         "Content-Type": "application/json", "Notion-Version": NV_UPLOAD}
    r = requests.post(f"{API}/file_uploads", headers=h, timeout=30,
                      json={"filename": filename, "content_type": "image/png"})
    if r.status_code not in (200, 201):
        print(f"  ⚠️ 파일업로드 생성 실패 {r.status_code}: {r.text[:150]}")
        return None
    fid = r.json()["id"]
    h2 = {"Authorization": f"Bearer {os.environ['NOTION_API_KEY']}", "Notion-Version": NV_UPLOAD}
    r2 = requests.post(f"{API}/file_uploads/{fid}/send", headers=h2, timeout=120,
                       files={"file": (filename, png_bytes, "image/png")})
    if r2.status_code not in (200, 201):
        print(f"  ⚠️ 파일 전송 실패 {r2.status_code}: {r2.text[:150]}")
        return None
    return fid


def append_image(block_id, png_bytes, filename="chart.png"):
    """이미지 블록을 붙인다. file_upload 타입은 업로드 버전 헤더가 필요하다."""
    fid = upload_image(png_bytes, filename)
    if not fid:
        return False
    h = {"Authorization": f"Bearer {os.environ['NOTION_API_KEY']}",
         "Content-Type": "application/json", "Notion-Version": NV_UPLOAD}
    r = requests.patch(f"{API}/blocks/{block_id}/children", headers=h, timeout=60,
                       json={"children": [{"object": "block", "type": "image",
                                           "image": {"type": "file_upload",
                                                     "file_upload": {"id": fid}}}]})
    if r.status_code != 200:
        print(f"  ⚠️ 이미지 블록 추가 실패 {r.status_code}: {r.text[:150]}")
        return False
    return True


def append_image_after(parent_id, after_id, png_bytes, filename="chart.png"):
    """이미지를 특정 블록 바로 뒤에 넣는다(맨 위 유지용). 업로드는 NV_UPLOAD 버전 필요."""
    fid = upload_image(png_bytes, filename)
    if not fid:
        return False
    h = {"Authorization": f"Bearer {os.environ['NOTION_API_KEY']}",
         "Content-Type": "application/json", "Notion-Version": NV_UPLOAD}
    r = requests.patch(f"{API}/blocks/{parent_id}/children", headers=h, timeout=60,
                       json={"after": after_id,
                             "children": [{"object": "block", "type": "image",
                                           "image": {"type": "file_upload",
                                                     "file_upload": {"id": fid}}}]})
    if r.status_code != 200:
        print(f"  ⚠️ 이미지 삽입 실패 {r.status_code}: {r.text[:150]}")
        return False
    return True


def append_blocks(block_id, blocks, chunk=40):
    """토글 안에 블록을 나눠 넣는다. 요청당 100블록 한도와 중첩 제약을 피하기 위한 공개 헬퍼."""
    ok = 0
    for i in range(0, len(blocks), chunk):
        r = _append(block_id, blocks[i:i + chunk])
        if not r:
            break
        ok += len(blocks[i:i + chunk])
    return ok


def url():
    st = _state()
    pid = st.get("page_id", "")
    return f"https://notion.so/{pid.replace('-', '')}" if pid else ""
