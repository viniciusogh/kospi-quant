"""과거 모멘텀 추천을 Report DB 에 소급 기록한다.

momentum_history.csv 에 2026-06-10 부터 날짜·종목·순위·진입가가 남아 있다.
섹터 이력은 2026-09-01 부터라 과거 순환매 맥락은 복원 불가 → 모멘텀 1위와 진입가만 남긴다.
이것만으로도 '그날 1위를 샀으면 어땠나' 를 계산할 수 있다(--eval 이 채운다).

실행: python backfill_archive.py [--from YYYY-MM-DD] [--dry]
"""
import os, sys
import _env
import pandas as pd
import requests

import momentum_daily as M

_DIR = os.path.dirname(os.path.abspath(__file__))
DB = os.environ["NOTION_DAILY_DB_ID"]
API = "https://api.notion.com/v1"
_H = {"Authorization": f"Bearer {os.environ['NOTION_API_KEY']}",
      "Content-Type": "application/json", "Notion-Version": "2022-06-28"}


def existing_dates():
    """이미 종목코드가 기록된 날짜 — 덮어쓰지 않는다."""
    out, cur = set(), None
    while True:
        body = {"page_size": 100}
        if cur:
            body["start_cursor"] = cur
        r = requests.post(f"{API}/databases/{DB}/query", headers=_H, json=body, timeout=30).json()
        for p in r.get("results", []):
            pr = p["properties"]
            code = "".join(x["plain_text"] for x in pr.get("종목코드", {}).get("rich_text", []))
            dt = (pr.get("날짜", {}).get("date") or {}).get("start")
            if dt and code:
                out.add(dt)
        if not r.get("has_more"):
            break
        cur = r.get("next_cursor")
    return out


def row_id(date):
    r = requests.post(f"{API}/databases/{DB}/query", headers=_H, timeout=30,
                      json={"filter": {"property": "날짜", "date": {"equals": date}}, "page_size": 1}).json()
    res = r.get("results") or []
    return res[0]["id"] if res else None


def main():
    frm = "2026-06-10"
    if "--from" in sys.argv:
        frm = sys.argv[sys.argv.index("--from") + 1]
    dry = "--dry" in sys.argv

    h = pd.read_csv(os.path.join(_DIR, "momentum_history.csv"), encoding="utf-8-sig",
                    dtype={"code": str})
    h["code"] = h["code"].str.zfill(6)
    h = h[h["date"] >= frm]
    top = h[h["rank"] == 1].sort_values("date")
    done = existing_dates()
    M.log(f"▶ 백필 대상 {len(top)}일 ({top['date'].min()} ~ {top['date'].max()}) · 이미 기록 {len(done)}일")

    made = skipped = 0
    for _, r in top.iterrows():
        date = str(r["date"])
        if date in done:
            skipped += 1; continue
        props = {
            "날짜": {"date": {"start": date}},
            "추천종목": {"rich_text": [{"type": "text", "text": {"content": str(r["종목명"])}}]},
            "종목코드": {"rich_text": [{"type": "text", "text": {"content": r["code"]}}]},
            "진입가": {"number": float(r["price"])},
        }
        title = f"[소급] 모멘텀 1위 {r['종목명']} · 점수 {r['score']:.1f}"
        if dry:
            M.log(f"  (dry) {date} {r['종목명']}({r['code']}) {r['price']:,.0f}원"); made += 1; continue
        pid = row_id(date)
        if pid:
            requests.patch(f"{API}/pages/{pid}", headers=_H, timeout=30, json={"properties": props})
        else:
            props["이름"] = {"title": [{"type": "text", "text": {"content": title}}]}
            rr = requests.post(f"{API}/pages", headers=_H, timeout=30,
                               json={"parent": {"database_id": DB}, "properties": props})
            if rr.status_code != 200:
                M.log(f"  ⚠️ {date} 실패 {rr.status_code}: {rr.text[:120]}"); continue
        made += 1
        if made % 10 == 0:
            M.log(f"  … {made}일 처리")
    M.log(f"✅ 백필 {made}일 · 건너뜀 {skipped}일. 이제 daily_archive.py --eval 로 수익률을 채운다.")


if __name__ == "__main__":
    main()
