"""
분석 결과를 마크다운 보고서 파일로 저장하는 모듈
"""
import os
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

KST = timezone(timedelta(hours=9))
REPORTS_DIR = Path(__file__).parent / "reports"


def save_report(date_str: str, report_markdown: str, analyzed_videos: list[dict]) -> dict:
    """
    보고서를 마크다운 파일로 저장
    반환: {"md_path": ..., "json_path": ...}
    """
    REPORTS_DIR.mkdir(exist_ok=True)

    # 보고서 헤더 추가
    generated_at = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST")
    header = f"""# 📈 일일 경제 보고서 — {date_str}
> 소스 채널: [@3protv](https://www.youtube.com/@3protv)
> 생성 시각: {generated_at}
> 분석 영상 수: {len(analyzed_videos)}개

---

"""
    full_report = header + report_markdown

    # 마크다운 저장
    md_filename = f"{date_str}_경제보고서.md"
    md_path = REPORTS_DIR / md_filename
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(full_report)
    print(f"\n[저장] 보고서: {md_path}")

    # 원본 데이터(JSON) 저장 — transcript_segments는 용량이 크므로 제외
    json_filename = f"{date_str}_분석데이터.json"
    json_path = REPORTS_DIR / json_filename
    save_data = []
    for v in analyzed_videos:
        v_copy = {k: val for k, val in v.items() if k != "transcript_segments"}
        # transcript 텍스트도 길면 1000자로 자름
        if len(v_copy.get("transcript", "")) > 1000:
            v_copy["transcript"] = v_copy["transcript"][:1000] + "...(생략)"
        save_data.append(v_copy)

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(save_data, f, ensure_ascii=False, indent=2)
    print(f"[저장] 분석 데이터: {json_path}")

    return {"md_path": str(md_path), "json_path": str(json_path)}


def generate_no_video_report(date_str: str) -> str:
    """오늘 업로드된 영상이 없을 때 생성하는 보고서"""
    generated_at = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST")
    REPORTS_DIR.mkdir(exist_ok=True)

    content = f"""# 📈 일일 경제 보고서 — {date_str}
> 소스 채널: [@3protv](https://www.youtube.com/@3protv)
> 생성 시각: {generated_at}

---

## 알림

{date_str} 기준으로 [@3protv](https://www.youtube.com/@3protv) 채널에 업로드된 영상이 없습니다.

보고서를 생성할 수 없습니다.
"""
    md_path = REPORTS_DIR / f"{date_str}_경제보고서.md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"[저장] 영상 없음 보고서: {md_path}")
    return str(md_path)


def print_report_summary(analyzed_videos: list[dict]):
    """콘솔에 분석 요약 출력"""
    print("\n" + "="*60)
    print("분석 완료 요약")
    print("="*60)
    for v in analyzed_videos:
        analysis = v.get("analysis", {})
        print(f"\n📹 {v.get('title', '')}")
        print(f"   URL: {v.get('url', '')}")
        print(f"   요약: {analysis.get('summary', '없음')[:100]}...")
        print(f"   심리: {analysis.get('sentiment', '불명')}")
        topics = analysis.get("key_topics", [])
        if topics:
            print(f"   토픽: {', '.join(topics[:5])}")
