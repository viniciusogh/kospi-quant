# 프로젝트 컨텍스트 & 규칙

## 프로젝트 개요
코스피 멀티팩터 퀀트 종목 추천 + 3proTV YouTube 영상 자동 분석 시스템.
모든 결과는 Notion에 자동 업로드된다.

## 파일 구조
- `수급.py` : 코스피 수급·재무 멀티팩터 점수 계산 + Notion 업로드 (KIS API, 시총 상위 500 → TOP30)
- `kosdaq.py` : 코스닥 수급·재무 멀티팩터 점수 계산 + Notion 업로드 (KIS API, 시총 상위 200 → TOP30, 수급.py 와 동일 로직)
- `quality.py` : 코스피 Quality 모델 (ROE 0.40 + 1/PER 0.20 + 1/PBR 0.20 + 저부채 0.20). 수급/모멘텀/성장 미반영. 시총 상위 200 → TOP30. 자본잠식·부채>300% 종목 사전 제외, 매출 +100% 이상은 분할의심으로 ROE 신뢰성 0 처리. fin_ratio_cache.csv 공유 (수급 호출은 생략)
- `us_quant.py` : S&P 500 멀티팩터 퀀트 분석 (yfinance)
- `youtube_report.py` : 다채널 영상 자막 수집 → Gemini 분석 → Notion 업로드 (채널별 일일 페이지). 채널 목록은 파일 상단 `CHANNELS` 리스트 참조 (현재 6개: 3proTV, 오선의 미국 증시 라이브, 머니인사이드, 강민우 돈깡TV, 전인구경제연구소, 채국장의 코스피 1만 코스닥 3천). 분석본은 `latest_youtube_analysis.json` 에도 누적 저장 (3일 보존, `daily_recommend.py` 가 자막 원본 대신 이걸 읽어 Gemini 토큰 절약)
- `daily_recommend.py` : 일일 종합 종목 추천 — 정량 데이터 (한투 TOP 30 × 3 모델) + 유튜브 화자 의견 (`latest_youtube_analysis.json`) 의 교집합을 Gemini 가 분석. 매일 22:30 KST cron 권장. 노션 페이지 `💎 {date} 종합 추천` 으로 push. 비용 절감: 자막 원본 대신 분석본 사용 + TOP 30 만 입력 + `max_output_tokens=1500`.
- `KOSPI재무데이터한투.csv` / `KOSDAQ재무데이터한투.csv` : 시장별 종목 마스터 (한투 .mst 에서 파싱)
- `latest_results.csv` : 가장 최근 코스피 퀀트 결과 (종목 질문 시 이 파일 읽기)
- `latest_kosdaq_results.csv` : 가장 최근 코스닥 퀀트 결과
- `fin_ratio_cache.csv` / `kosdaq_fin_ratio_cache.csv` : 시장별 재무비율 주간 캐시
- `us_fin_cache.csv` : US 재무비율 주간 캐시
- `kospi_score_history.csv` / `kosdaq_score_history.csv` / `us_score_history.csv` : 5일 EMA 평탄화용 점수 이력
- `processed_videos.json` : 이미 처리한 YouTube 영상 ID 목록
- `notion_daily_pages.json` : 날짜별 Notion 페이지 ID 캐시

## 자동화 스케줄 (cron + launchd)
- 코스피 + 코스닥 퀀트 : 평일 17:30 KST (GitHub Actions, 동일 워크플로에서 순차 실행)
- US 퀀트 : 매일 07:00 KST (GitHub Actions)
- YouTube 분석 : 10분마다 (로컬 cron + launchd RunAtLoad)
- git pull : 30분마다 (로컬 cron)

## API & 서비스
- KIS API : 한국투자증권 (코스피 수급·시세·재무)
- yfinance : Yahoo Finance (US 주가·재무, 무료)
- Gemini API : gemini-2.5-flash 사용, **유료 플랜** (쿼터 걱정 불필요)
- Notion API : 모든 결과물 업로드
- GitHub Actions : 코스피/US 퀀트는 GitHub에서 실행 (노트북 불필요)
- **yt-dlp** (`brew install yt-dlp`) : YouTube 채널 영상 목록 수집. RSS endpoint(`feeds/videos.xml`)가 2026-05-09 경 무인증 접근 차단되어 RSS → yt-dlp `--flat-playlist` 로 전환. flat 모드는 timestamp 안 주므로 `published` 는 today 하드코딩 (cutoff 로직 사실상 무력화 — 영향 미미, yt-dlp 도 최근 15편만 가져옴).

## 핵심 제약사항 (반드시 지킬 것)

### KIS API 작업
- **모든 KIS(한국투자증권) API 관련 작업 시작 전, `docs/KIS_API_REFERENCE.md` 를 먼저 읽을 것**
- TR_ID·응답 필드명·엔드포인트 매핑이 단일 출처로 정리되어 있음
- 응답 키를 새로 호출해 추측하지 말고 이 문서에서 먼저 검색
- 예: KOSPI 종목 업종명은 `FHKST01010100` 응답의 `bstp_kor_isnm` (별도 search-stock-info 불필요)
- 문서에 없는 새 엔드포인트 사용 시 raw 응답 확인 후 문서에 키 추가하고 사용

### 스크립트 실행
- **interact 모드로 스크립트 실행 금지** → Warp 크레딧 대량 소모
- 스크립트 실행이 필요하면 wait 모드로 짧게 확인하거나, 사용자에게 직접 실행하도록 안내
- 테스트는 `python3 -c "..."` 단발 명령으로 최소화

### Git
- 항상 `git pull --rebase && git push` 순서 (충돌 방지)
- 원격 앞설 때: `git pull --rebase` 먼저

### YouTube 자막
- YouTube 자막은 **로컬 IP에서만 작동** (GitHub Actions Azure IP 차단됨)
- youtube_report.yml 워크플로는 동작 안 함 (있어도 무용지물)

### Lock 파일
- `youtube_report.lock` : 10분마다 실행되므로 중복 방지 필수
- stale lock (30분 이상) 자동 제거 로직 내장됨 — `LOCK_MAX_AGE_HOURS = 0.5`
- 정상 실행은 1~2분 안에 끝나므로 30분 마진은 충분
- 크래시 시 수동 삭제: `rm youtube_report.lock`

## Notion 구조
- 코스피 추천종목 : `3324a00632f880fbb014d766d87a1079` 하위에 날짜별 페이지 (제목: `📊 {date} 추천종목`)
- 코스닥 추천종목 : 동일 부모 페이지 (제목: `🇰🇷 {date} KOSDAQ 추천종목`)
- US 추천종목 : 동일 부모 페이지 (제목: `🇺🇸 {date} US 추천종목`)
- KOSPI Quality 추천종목 : 동일 부모 페이지 (제목: `💎 {date} KOSPI Quality 추천종목`)
- YouTube 분석 : `3484a00632f880988b41e8b13d7fbb0b` 하위에 **채널별 일일 페이지** (제목: `{emoji} {date} {channel_name} 분석`)
  - 하루에 채널 수만큼 페이지 (현재 6개), 영상별 토글 블록, 오래된 영상이 위/최신이 아래
  - `notion_daily_pages.json` 키 형식: `f"{date}_{slug}"` (예: `2026-05-07_3proTV`). 레거시 날짜-only 키는 3proTV 호환 fallback 으로만 처리됨.
  - Notion API 100 블록/요청 한도 회피: `append_to_page` 가 외부 블록 1개씩 전송 (토글 1 + nested children 95 = 96 < 100)

## 종목 질문 답변 방법
사용자가 종목을 물어보면:
1. `latest_results.csv` 읽어서 해당 종목 조회
2. `fin_ratio_cache.csv`에서 추가 재무 데이터 확인
3. 기준일자 먼저 알려주기

## 지금까지 겪은 주요 이슈
- Gemini 무료 플랜 → 쿼터 소진 반복 → **유료 전환 완료**
- `gemini-2.0-flash`, `gemini-2.0-flash-lite` 구버전 → **gemini-2.5-flash 사용 중**
- YouTube IP 차단 → 로컬 전용, GitHub Actions 불가
- lock 파일 FileNotFoundError → finally 블록에 try/except 추가됨
- Notion 아카이브 오류 → archived 감지 시 ID 자동 삭제 로직 내장
