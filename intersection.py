"""
교집합 추천종목 — 수급 멀티팩터 + Quality 두 전략 모두 통과한 종목
- latest_수급_reco.csv  (수급.py 실행 후 생성)
- latest_quality_reco.csv (quality.py 실행 후 생성)
두 파일을 읽어 종목코드 기준 교집합 → Notion 새 페이지 업로드
"""

import os
import _env  # .env 자동 로드
import sys
import requests
import pandas as pd
from datetime import datetime
import pytz

# ─── 경로 ────────────────────────────────────────────────────────────────────
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SUGUP_CSV   = os.path.join(_BASE_DIR, "latest_수급_reco.csv")
QUALITY_CSV = os.path.join(_BASE_DIR, "latest_quality_reco.csv")

# ─── Notion ──────────────────────────────────────────────────────────────────
NOTION_API_KEY        = os.environ["NOTION_API_KEY"]
NOTION_PARENT_PAGE_ID = os.environ.get("NOTION_PARENT_PAGE_ID", "3324a00632f880fbb014d766d87a1079")

KST = pytz.timezone("Asia/Seoul")


def log(msg: str):
    ts = datetime.now(KST).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def load_reco(path: str, label: str) -> pd.DataFrame:
    if not os.path.exists(path):
        log(f"❌ {label} CSV 없음: {path}")
        log(f"   → {label} 스크립트를 먼저 실행하세요.")
        sys.exit(1)
    df = pd.read_csv(path, dtype={"종목코드": str})
    df["종목코드"] = df["종목코드"].str.zfill(6)
    # 신선도 가드: CSV 기준일이 오늘이 아니면 stale → 교집합 발행 중단 (2026-06-16: 6/16 stale 발행 대응)
    if "기준일" in df.columns and len(df):
        csv_date = str(df["기준일"].astype(str).max())[:10]
        today = datetime.now(KST).strftime("%Y-%m-%d")
        if csv_date != today:
            log(f"⚠️ {label} CSV가 오늘({today}) 아닌 {csv_date} 데이터 → 교집합 발행 중단(stale 방지)")
            sys.exit(0)
    log(f"✅ {label} 추천종목 로드: {len(df)}개")
    return df


def find_intersection(df_sugup: pd.DataFrame, df_quality: pd.DataFrame) -> pd.DataFrame:
    """
    종목코드 기준 교집합 찾기.
    수급 데이터 기준으로 병합 (멀티팩터점수, 수급강화점수 포함)
    quality에서 퀄리티점수 추가.
    """
    sugup_codes   = set(df_sugup["종목코드"])
    quality_codes = set(df_quality["종목코드"])
    common_codes  = sugup_codes & quality_codes

    if not common_codes:
        return pd.DataFrame()

    # 수급 데이터에서 교집합 추출
    inter = df_sugup[df_sugup["종목코드"].isin(common_codes)].copy()

    # quality 점수 + 재무 지표 병합
    # (수급_reco 가 순수 수급만 가지도록 변경됨 — 2026-05-24)
    q_cols = ["종목코드"]
    for c in ["퀄리티점수", "PER", "PBR", "EPS", "ROE(%)", "부채비율(%)",
              "매출증가율(%)", "영업이익증가율(%)"]:
        if c in df_quality.columns:
            q_cols.append(c)
    inter = inter.merge(df_quality[q_cols], on="종목코드", how="left")

    # 멀티팩터점수 기준 정렬
    sort_col = "멀티팩터점수" if "멀티팩터점수" in inter.columns else inter.columns[0]
    inter.sort_values(sort_col, ascending=False, inplace=True)
    inter.reset_index(drop=True, inplace=True)
    inter.insert(0, "교집합랭킹", range(1, len(inter) + 1))

    return inter


def _get_or_create_date_page(date_str: str, headers: dict, root_parent_id: str) -> str:
    """노션 database 의 today row 찾거나 생성. row ID = 그 날짜 페이지 ID.
    NOTION_DAILY_DB_ID 환경변수 = database ID."""
    import os
    db_id = os.environ.get("NOTION_DAILY_DB_ID", "")
    if not db_id:
        # fallback (옛 페이지 구조) - 이 파일이 root_parent_id 아래 child_page 검색
        try:
            r = requests.post(
                "https://api.notion.com/v1/databases/" + root_parent_id + "/query",
                headers=headers, timeout=15
            )
        except Exception:
            pass
        raise RuntimeError("NOTION_DAILY_DB_ID 가 설정되지 않음")

    # database 에서 today row 찾기
    r = requests.post(
        f"https://api.notion.com/v1/databases/{db_id}/query",
        headers=headers,
        json={"filter": {"property": "날짜", "date": {"equals": date_str}}, "page_size": 1},
        timeout=15,
    )
    if r.status_code == 200:
        results = r.json().get("results", [])
        if results:
            return results[0]["id"]

    # 없으면 새 row 생성
    body = {
        "parent": {"database_id": db_id},
        "properties": {
            "이름": {"title": [{"type": "text", "text": {"content": "준비 중"}}]},
            "날짜": {"date": {"start": date_str}},
        },
    }
    r = requests.post("https://api.notion.com/v1/pages", headers=headers, json=body, timeout=15)
    r.raise_for_status()
    return r.json()["id"]


def _archive_same_title_pages(title: str, headers: dict, parent_id: str):
    """동일 제목 페이지가 부모 하위에 이미 있으면 archive — 중복 방지."""
    try:
        r = requests.post(
            "https://api.notion.com/v1/search", headers=headers, timeout=15,
            json={"query": title, "filter": {"property": "object", "value": "page"}},
        )
        if r.status_code != 200:
            return
        target_parent = parent_id.replace("-", "")
        for result in r.json().get("results", []):
            parent = result.get("parent", {})
            if parent.get("type") != "page_id":
                continue
            if parent.get("page_id", "").replace("-", "") != target_parent:
                continue
            t_arr = result.get("properties", {}).get("title", {}).get("title", [])
            actual = t_arr[0]["text"]["content"] if t_arr else ""
            if actual.strip() == title.strip() and not result.get("archived", False):
                page_id = result["id"]
                requests.patch(
                    f"https://api.notion.com/v1/pages/{page_id}",
                    headers=headers, json={"archived": True}, timeout=15,
                )
                log(f"  기존 동일 제목 페이지 archive: {page_id}")
    except Exception as e:
        log(f"  기존 페이지 확인 중 오류 (무시): {e}")


def upload_to_notion(df: pd.DataFrame, today_str: str):
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }

    def cell(text):
        return [{"type": "text", "text": {"content": str(text)}}]

    def _v(val):
        try:
            return None if (val is None or pd.isna(val)) else val
        except Exception:
            return None

    # 컬럼 구성
    base_cols = ["교집합랭킹", "종목코드", "종목명", "섹터", "정배열",
                 "멀티팩터점수", "수급강화점수"]
    opt_cols  = ["퀄리티점수", "PER", "PBR", "ROE(%)", "부채비율(%)",
                 "당일등락(%)", "순위변동"]
    col_labels = base_cols + [c for c in opt_cols if c in df.columns]

    rows = [{"type": "table_row", "table_row": {"cells": [cell(c) for c in col_labels]}}]

    for _, row in df.iterrows():
        cells = []
        for col in col_labels:
            val = row.get(col, "-")
            v = _v(val)
            if v is None:
                cells.append(cell("-"))
            elif col == "정배열":
                cells.append(cell("✅" if v else "-"))
            elif col in ("멀티팩터점수", "수급강화점수", "퀄리티점수"):
                cells.append(cell(f"{float(v):.3f}"))
            elif col in ("PER", "ROE(%)"):
                cells.append(cell(f"{float(v):.1f}"))
            elif col == "PBR":
                cells.append(cell(f"{float(v):.2f}"))
            elif col == "부채비율(%)":
                cells.append(cell(f"{float(v):.1f}"))
            elif col == "당일등락(%)":
                sign = "+" if float(v) >= 0 else ""
                cells.append(cell(f"{sign}{float(v):.2f}%"))
            else:
                cells.append(cell(v))
        rows.append({"type": "table_row", "table_row": {"cells": cells}})

    title = f"🔥 {today_str} KOSPI 교집합 추천종목"
    heading = (
        f"수급 멀티팩터 + Quality 두 전략 모두 통과 | "
        f"TOP{len(df)} ({today_str})"
    )

    date_parent_id = _get_or_create_date_page(today_str, headers, NOTION_PARENT_PAGE_ID)

    body = {
        "parent": {"page_id": date_parent_id},
        "properties": {"title": {"title": [{"text": {"content": title}}]}},
        "children": [
            {
                "object": "block",
                "type": "callout",
                "callout": {
                    "rich_text": [{"type": "text", "text": {"content":
                        "💡 이 페이지는 수급 멀티팩터(📊)와 KOSPI Quality(💎) 두 전략을 "
                        "동시에 통과한 종목만 표시합니다. 이중 검증 강력 매수 시그널."
                    }}],
                    "icon": {"type": "emoji", "emoji": "🔥"},
                },
            },
            {
                "object": "block",
                "type": "heading_2",
                "heading_2": {"rich_text": [{"type": "text", "text": {"content": heading}}]},
            },
            {
                "object": "block",
                "type": "table",
                "table": {
                    "table_width": len(col_labels),
                    "has_column_header": True,
                    "has_row_header": False,
                    "children": rows,
                },
            },
        ],
    }

    # 동일 제목 페이지 있으면 archive (중복 방지)
    _archive_same_title_pages(title, headers, date_parent_id)

    try:
        r = requests.post("https://api.notion.com/v1/pages", headers=headers,
                          json=body, timeout=15)
        if r.status_code == 200:
            log(f"✅ Notion 업로드 완료: {r.json().get('url', '')}")
        else:
            log(f"❌ Notion 업로드 실패 ({r.status_code}): {r.text[:300]}")
    except Exception as e:
        log(f"❌ Notion 요청 오류: {e}")


def main():
    today_str = datetime.now(KST).strftime("%Y-%m-%d")
    log(f"▶ 교집합 추천종목 계산 시작 ({today_str})")

    df_sugup   = load_reco(SUGUP_CSV,   "수급 멀티팩터")
    df_quality = load_reco(QUALITY_CSV, "KOSPI Quality")

    inter = find_intersection(df_sugup, df_quality)

    if inter.empty:
        log("⚠️  교집합 종목 없음 — 오늘은 두 전략 동시 통과 종목 없음")
        return

    log(f"✅ 교집합 종목: {len(inter)}개")
    for _, r in inter.iterrows():
        log(f"   {int(r['교집합랭킹'])}위 {r['종목코드']} {r['종목명']}")

    log("▶ Notion 업로드 시작")
    upload_to_notion(inter, today_str)


if __name__ == "__main__":
    main()
