# 📈 일일 경제 보고서 자동화

[@3protv](https://www.youtube.com/@3protv) 채널의 오늘자 영상 자막을 분석하여 일일 경제 보고서를 자동 생성하는 시스템입니다.

## 파이프라인

```
YouTube Data API → 오늘 영상 목록
        ↓
youtube-transcript-api → 한국어 자막 추출
        ↓
OpenAI GPT-4o → 개별 영상 분석 (JSON)
        ↓
OpenAI GPT-4o → 종합 일일 보고서 생성 (Markdown)
        ↓
reports/YYYY-MM-DD_경제보고서.md 저장
```

## 설치

```bash
cd youtube_daily_report
pip install -r requirements.txt
```

## API 키 설정

```bash
cp .env.example .env
# .env 파일을 열어 API 키 입력
```

### 필요한 API 키

| 키 | 발급 경로 | 비용 |
|---|---|---|
| `YOUTUBE_API_KEY` | [Google Cloud Console](https://console.cloud.google.com) → YouTube Data API v3 | 무료 (일 10,000 쿼터) |
| `OPENAI_API_KEY` | [OpenAI Platform](https://platform.openai.com/api-keys) | 유료 (gpt-4o 기준 영상 5개 약 $0.10~0.30) |

### YouTube API 키 발급 방법
1. https://console.cloud.google.com 접속
2. 새 프로젝트 생성
3. "API 및 서비스" → "라이브러리" → "YouTube Data API v3" 검색 후 활성화
4. "사용자 인증 정보" → "API 키 만들기"

## 사용법

```bash
# 오늘 보고서 생성
python main.py

# 특정 날짜 보고서 생성
python main.py --date 2026-03-29

# 저렴한 모델 사용 (품질↓, 비용↓)
python main.py --model gpt-4o-mini

# 캐시 무시하고 처음부터 재실행
python main.py --no-cache
```

## 출력 파일

`reports/` 폴더에 저장됩니다:

- `YYYY-MM-DD_경제보고서.md` — 최종 보고서 (마크다운)
- `YYYY-MM-DD_분석데이터.json` — 영상별 상세 분석 데이터
- `YYYY-MM-DD_캐시.json` — 중간 캐시 (재실행 시 API 쿼터 절약)

## 보고서 구조

생성되는 보고서의 섹션:
1. **오늘의 핵심 요약** — 핵심 내용 bullet point
2. **글로벌 시장 동향** — 주요 지수, 환율, 원자재
3. **국내 시장 동향** — 코스피/코스닥, 주목 섹터/종목
4. **주요 경제 이슈 & 분석** — 오늘의 핵심 경제 이슈
5. **리스크 요인** — 주의해야 할 리스크
6. **투자 관심 포인트** — 기회 요인 및 관심 섹터
7. **분석 영상 목록** — 소스 영상 링크

## cron으로 매일 자동 실행 (macOS)

매일 오후 10시(KST)에 자동 실행:

```bash
# crontab 편집
crontab -e

# 아래 줄 추가 (Python 경로는 `which python3` 로 확인)
0 13 * * * /usr/bin/python3 /Users/vinicius/Desktop/퀀트스코어/youtube_daily_report/main.py >> /Users/vinicius/Desktop/퀀트스코어/youtube_daily_report/cron.log 2>&1
```

> cron은 UTC 기준이므로 KST 22:00 = UTC 13:00

### launchd 방식 (더 안정적, macOS 권장)

```bash
# plist 파일 생성 및 등록
cp com.quantscore.dailyreport.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.quantscore.dailyreport.plist
```

`com.quantscore.dailyreport.plist` 파일이 프로젝트에 포함되어 있습니다.

## 채널 ID 변경 방법

다른 채널로 변경하려면 `youtube_fetcher.py`의 `CHANNEL_ID` 값을 수정하세요.

채널 ID 확인 방법:
```
https://www.youtube.com/@채널명/about
→ 페이지 소스에서 "channelId" 검색
```
