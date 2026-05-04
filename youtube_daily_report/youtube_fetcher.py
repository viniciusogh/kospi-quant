"""
YouTube 채널에서 오늘 업로드된 영상 목록을 가져오는 모듈
RSS 피드 방식 사용 → API 키 불필요, 완전 무료
"""
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta


# 한국 시간 기준 (UTC+9)
KST = timezone(timedelta(hours=9))

# @3protv 채널 ID
CHANNEL_ID = "UCp2kTEhO8bUybus3_AXWEww"
RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"

# XML 네임스페이스
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt":   "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}


def get_today_videos(target_date: datetime | None = None) -> list[dict]:
    """
    오늘(KST 기준) 채널에 업로드된 영상 목록 반환
    RSS는 최근 15개만 제공하므로, 그 안에서 오늘 날짜 필터링
    """
    if target_date is None:
        target_date = datetime.now(KST)

    date_str = target_date.strftime("%Y-%m-%d")
    print(f"[RSS] {date_str} (KST) 업로드 영상 검색 중...")
    print(f"  URL: {RSS_URL}")

    # RSS 피드 다운로드
    try:
        req = urllib.request.Request(
            RSS_URL,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            xml_data = resp.read()
    except Exception as e:
        print(f"[오류] RSS 피드 다운로드 실패: {e}")
        raise

    root = ET.fromstring(xml_data)
    videos = []

    for entry in root.findall("atom:entry", NS):
        # 영상 ID
        video_id_elem = entry.find("yt:videoId", NS)
        if video_id_elem is None:
            continue
        video_id = video_id_elem.text

        # 제목
        title_elem = entry.find("atom:title", NS)
        title = title_elem.text if title_elem is not None else ""

        # 설명
        desc_elem = entry.find("media:group/media:description", NS)
        description = desc_elem.text if desc_elem is not None else ""

        # 업로드 시각 (published)
        published_elem = entry.find("atom:published", NS)
        if published_elem is None:
            continue
        published_utc = datetime.fromisoformat(
            published_elem.text.replace("Z", "+00:00")
        )
        published_kst = published_utc.astimezone(KST)

        # 오늘 날짜 필터
        if published_kst.strftime("%Y-%m-%d") != date_str:
            continue

        # 썸네일
        thumb_elem = entry.find("media:group/media:thumbnail", NS)
        thumbnail = thumb_elem.attrib.get("url", "") if thumb_elem is not None else ""

        videos.append({
            "video_id": video_id,
            "title": title,
            "description": description or "",
            "published_at": published_kst.strftime("%Y-%m-%d %H:%M:%S KST"),
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "thumbnail": thumbnail,
        })

    print(f"[RSS] {len(videos)}개 영상 발견")
    for v in videos:
        print(f"  - [{v['published_at']}] {v['title']}")
        print(f"    {v['url']}")

    return videos


if __name__ == "__main__":
    videos = get_today_videos()
    if not videos:
        print("오늘 업로드된 영상이 없습니다.")
