"""
일일 경제 보고서 자동화 메인 스크립트

실행 방법:
  python main.py                  # 오늘 보고서 생성
  python main.py --date 2026-03-29  # 특정 날짜 보고서 생성
  python main.py --model gpt-4o-mini  # 저렴한 모델 사용
  python main.py --no-cache       # 캐시 무시하고 재실행
"""
import os
import sys
import argparse
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv

# 환경 변수 로드 (.env 파일)
load_dotenv(Path(__file__).parent / ".env")

from youtube_fetcher import get_today_videos, get_video_details
from transcript_fetcher import get_transcripts_for_videos
from analyzer import analyze_all_videos, synthesize_daily_report
from report_generator import save_report, generate_no_video_report, print_report_summary

KST = timezone(timedelta(hours=9))


def run_pipeline(
    target_date: datetime | None = None,
    model: str = "gpt-4o",
    use_cache: bool = True,
) -> str | None:
    """
    전체 파이프라인 실행
    반환: 생성된 보고서 파일 경로 (또는 None)
    """
    if target_date is None:
        target_date = datetime.now(KST)

    date_str = target_date.strftime("%Y-%m-%d")
    cache_path = Path(__file__).parent / "reports" / f"{date_str}_캐시.json"

    print(f"\n{'='*60}")
    print(f"  📈 일일 경제 보고서 생성 시작")
    print(f"  날짜: {date_str} | 모델: {model}")
    print(f"{'='*60}\n")

    # ─── 1단계: 영상 목록 수집 ───
    # 캐시 확인 (재실행 시 YouTube API 쿼터 절약)
    analyzed_videos = None
    if use_cache and cache_path.exists():
        print(f"[캐시] {cache_path} 에서 데이터 로드 중...")
        with open(cache_path, "r", encoding="utf-8") as f:
            analyzed_videos = json.load(f)
        print(f"  → {len(analyzed_videos)}개 영상 캐시 데이터 사용")

    if analyzed_videos is None:
        videos = get_today_videos(target_date)

        if not videos:
            print(f"\n⚠️  {date_str}에 업로드된 영상이 없습니다.")
            md_path = generate_no_video_report(date_str)
            return md_path

        # ─── 2단계: 영상 상세 정보 (길이, 조회수) ───
        print("\n[상세 정보] 영상 메타데이터 조회 중...")
        video_ids = [v["video_id"] for v in videos]
        details = get_video_details(video_ids)
        for v in videos:
            v.update(details.get(v["video_id"], {}))

        # ─── 3단계: 자막 추출 ───
        videos_with_transcripts = get_transcripts_for_videos(videos)

        # ─── 4단계: 개별 영상 LLM 분석 ───
        analyzed_videos = analyze_all_videos(videos_with_transcripts, model=model)

        # 캐시 저장 (segments 제외)
        cache_data = []
        for v in analyzed_videos:
            v_copy = {k: val for k, val in v.items() if k != "transcript_segments"}
            cache_data.append(v_copy)
        cache_path.parent.mkdir(exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache_data, f, ensure_ascii=False, indent=2)
        print(f"\n[캐시] 중간 결과 저장: {cache_path}")

    # ─── 5단계: 종합 보고서 생성 ───
    print_report_summary(analyzed_videos)
    report_markdown = synthesize_daily_report(date_str, analyzed_videos, model=model)

    # ─── 6단계: 파일 저장 ───
    paths = save_report(date_str, report_markdown, analyzed_videos)

    print(f"\n{'='*60}")
    print(f"  ✅ 완료!")
    print(f"  보고서: {paths['md_path']}")
    print(f"  데이터: {paths['json_path']}")
    print(f"{'='*60}\n")

    return paths["md_path"]


def main():
    parser = argparse.ArgumentParser(
        description="@3protv 채널 오늘자 영상 분석 → 일일 경제 보고서 생성"
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="분석할 날짜 (YYYY-MM-DD, 기본값: 오늘)",
    )
    parser.add_argument(
        "--model",
        type=str,
        default="gpt-4o",
        choices=["gpt-4o", "gpt-4o-mini", "gpt-4-turbo"],
        help="사용할 OpenAI 모델 (기본값: gpt-4o)",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="캐시 무시하고 처음부터 다시 실행",
    )

    args = parser.parse_args()

    # 날짜 파싱
    target_date = None
    if args.date:
        try:
            naive = datetime.strptime(args.date, "%Y-%m-%d")
            target_date = naive.replace(tzinfo=KST)
        except ValueError:
            print(f"[오류] 날짜 형식이 잘못되었습니다: {args.date} (YYYY-MM-DD 형식 사용)")
            sys.exit(1)

    result_path = run_pipeline(
        target_date=target_date,
        model=args.model,
        use_cache=not args.no_cache,
    )

    if result_path:
        print(f"보고서 위치: {result_path}")
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
