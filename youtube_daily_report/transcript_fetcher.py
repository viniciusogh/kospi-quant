"""
YouTube 영상에서 자막(스크립트)을 추출하는 모듈
youtube-transcript-api 사용 (API 키 불필요)
"""
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound
from youtube_transcript_api._errors import VideoUnavailable


# 자막 언어 우선순위
LANGUAGE_PRIORITY = ["ko", "ko-KR", "en", "en-US"]


def get_transcript(video_id: str) -> dict:
    """
    영상 ID로 자막 추출
    반환: {"video_id": ..., "language": ..., "text": ..., "segments": [...]}
    """
    try:
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)

        transcript = None
        language_used = None

        # 한국어 자막 우선 시도
        for lang in LANGUAGE_PRIORITY:
            try:
                transcript = transcript_list.find_transcript([lang])
                language_used = lang
                break
            except NoTranscriptFound:
                continue

        # 못 찾으면 자동생성 자막 포함 전체에서 한국어 우선
        if transcript is None:
            try:
                transcript = transcript_list.find_generated_transcript(["ko", "ko-KR"])
                language_used = "ko (자동생성)"
            except NoTranscriptFound:
                # 마지막 수단: 아무 언어나 가져온 뒤 번역
                try:
                    transcript = transcript_list.find_transcript(["ko"])
                    language_used = "ko"
                except NoTranscriptFound:
                    available = [t.language_code for t in transcript_list]
                    if not available:
                        return {"video_id": video_id, "error": "자막 없음", "text": ""}
                    transcript = transcript_list.find_transcript([available[0]])
                    language_used = available[0]

        segments = transcript.fetch()

        # 타임스탬프 포함 세그먼트 리스트
        segment_list = []
        for seg in segments:
            # 새 버전: seg가 dict 또는 FetchedTranscriptSnippet 객체
            if hasattr(seg, "text"):
                text = seg.text
                start = seg.start
            else:
                text = seg.get("text", "")
                start = seg.get("start", 0)
            segment_list.append({"start": round(start, 1), "text": text.strip()})

        # 전체 텍스트 (개행으로 연결)
        full_text = " ".join(s["text"] for s in segment_list if s["text"])

        print(f"  [자막] {video_id} → 언어: {language_used}, 세그먼트: {len(segment_list)}개")

        return {
            "video_id": video_id,
            "language": language_used,
            "text": full_text,
            "segments": segment_list,
            "error": None,
        }

    except TranscriptsDisabled:
        print(f"  [자막] {video_id} → 자막 비활성화됨")
        return {"video_id": video_id, "error": "자막 비활성화", "text": ""}
    except VideoUnavailable:
        print(f"  [자막] {video_id} → 영상 접근 불가")
        return {"video_id": video_id, "error": "영상 접근 불가", "text": ""}
    except Exception as e:
        print(f"  [자막] {video_id} → 오류: {e}")
        return {"video_id": video_id, "error": str(e), "text": ""}


def get_transcripts_for_videos(videos: list[dict]) -> list[dict]:
    """
    영상 목록 전체의 자막 추출 및 영상 정보와 합치기
    """
    print(f"\n[자막 추출] 총 {len(videos)}개 영상 처리 중...")
    results = []

    for video in videos:
        video_id = video["video_id"]
        print(f"\n  처리 중: {video['title'][:50]}...")
        transcript_data = get_transcript(video_id)

        results.append({
            **video,
            "transcript": transcript_data.get("text", ""),
            "transcript_language": transcript_data.get("language", ""),
            "transcript_segments": transcript_data.get("segments", []),
            "transcript_error": transcript_data.get("error"),
        })

    success_count = sum(1 for r in results if r["transcript"])
    print(f"\n[자막 추출] 완료: {success_count}/{len(videos)}개 성공")
    return results


def format_transcript_for_llm(video_data: dict, max_chars: int = 0) -> str:
    """
    LLM에 전달할 형식으로 자막 텍스트 포맷팅
    max_chars=0 이면 전체 자막 사용 (Gemini 100만 토큰 컨텍스트 활용)
    """
    transcript = video_data.get("transcript", "")
    if not transcript:
        return f"[자막 없음: {video_data.get('transcript_error', '알 수 없는 오류')}]"

    if max_chars > 0 and len(transcript) > max_chars:
        transcript = transcript[:max_chars] + "...(이후 생략)"

    return transcript


if __name__ == "__main__":
    # 테스트
    test_id = "dQw4w9WgXcQ"  # 테스트용
    result = get_transcript(test_id)
    print(result.get("text", "")[:500])
