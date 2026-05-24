# 프로젝트 컨텍스트 & 규칙

## 프로젝트 개요
코스피 멀티팩터 퀀트 종목 추천 + 다채널 YouTube 영상 자동 분석 시스템.
모든 결과는 Notion에 자동 업로드 + `investment-chatbot` (LLM Wiki vault) 의 데이터 소스.

## 파일 구조
- `수급.py` : 코스피 수급·재무 멀티팩터 점수 계산 + Notion 업로드 (KIS API, 시총 상위 500 → TOP30)
- `kosdaq.py` : 코스닥 수급·재무 멀티팩터 점수 계산 + Notion 업로드 (KIS API, 시총 상위 200 → TOP30, 수급.py 와 동일 로직)
- `quality.py` : 코스피 Quality 모델 (ROE 0.40 + 1/PER 0.20 + 1/PBR 0.20 + 저부채 0.20). 수급/모멘텀/성장 미반영. 시총 상위 200 → TOP30. 자본잠식·부채>300% 종목 사전 제외, 매출 +100% 이상은 분할의심으로 ROE 신뢰성 0 처리. fin_ratio_cache.csv 공유 (수급 호출 생략)
- `intersection.py` : 수급 TOP100 ∩ Quality TOP100 교집합 산출 + Notion 페이지 push. `latest_수급_reco.csv` + `latest_quality_reco.csv` 입력. 결과 Notion 페이지 제목: `🔥 {date} KOSPI 교집합 추천종목`
- `us_quant.py` : S&P 500 멀티팩터 퀀트 분석 (yfinance)
- `youtube_report.py` : 다채널 영상 자막 수집 → Gemini 분석 → Notion 업로드 (채널별 일일 페이지). 채널 목록은 파일 상단 `CHANNELS` 리스트 참조. 분석본은 `latest_youtube_analysis.json` 에도 누적 저장 (3일 보존)
- `daily_recommend.py` : 일일 종합 종목 추천 — 정량 데이터 + 유튜브 화자 의견 (`latest_youtube_analysis.json`) 의 교집합을 Gemini 분석. 노션 페이지 `💎 {date} 종합 추천` push
- `KOSPI재무데이터한투.csv` / `KOSDAQ재무데이터한투.csv` : 시장별 종목 마스터 (한투 .mst 에서 파싱)
- `latest_kospi_supply.csv` : 가장 최근 코스피 전 종목 수급/재무 raw (2400+ 종목)
- `latest_kospi_quality.csv` : 가장 최근 KOSPI Quality 전 종목 점수
- `latest_kosdaq.csv` : 가장 최근 코스닥 추천종목
- `latest_수급_reco.csv` / `latest_quality_reco.csv` : 멀티팩터/Quality TOP100. **investment-chatbot vault ETL 이 의존** (교집합 wiki 자동 생성). `.gitignore` 풀려서 commit/push 됨 (2026-05-24~)
- `fin_ratio_cache.csv` / `kosdaq_fin_ratio_cache.csv` : 시장별 재무비율 주간 캐시
- `us_fin_cache.csv` : US 재무비율 주간 캐시
- `kospi_score_history.csv` / `kosdaq_score_history.csv` / `us_score_history.csv` : 5일 EMA 평탄화용 점수 이력
- `processed_videos.json` : 이미 처리한 YouTube 영상 ID 목록
- `notion_daily_pages.json` : YouTube 채널별 일일 Notion 페이지 ID 캐시 (youtube_report.py 만 쓰기, daily_recommend.py 는 읽기만)

## 자동화 스케줄 (전부 GitHub Actions)
- 코스피 + 코스닥 퀀트 : 평일 17:30 KST (`Daily Quant Analysis` workflow)
- US 퀀트 : 매일 07:00 KST (`Daily US Quant Analysis`)
- YouTube 분석 : 매 1-2시간 (`Daily YouTube Analysis`). 클라우드에서 자막 fetch 정상 작동 확인 (2026-05-24)
- 종합 추천 : `Daily Recommendation` workflow
- **로컬 cron/launchd 없음** (2026-05-24 폐기). 노트북은 데이터 받기만 — `investment-chatbot/etl/run_daily.sh` 의 git pull 이 갱신 담당

## API & 서비스
- KIS API : 한국투자증권 (코스피 수급·시세·재무)
- yfinance : Yahoo Finance (US 주가·재무, 무료)
- Gemini API : `gemini-2.5-flash`, **유료 플랜** (쿼터 걱정 불필요)
- Notion API : 모든 결과물 업로드
- GitHub Actions : 모든 cron 의 단일 실행 환경
- **yt-dlp** (`brew install yt-dlp`) : YouTube 채널 영상 목록 수집

## 핵심 제약사항 (반드시 지킬 것)

### KIS API 작업
- **모든 KIS API 관련 작업 시작 전, `docs/KIS_API_REFERENCE.md` 를 먼저 읽을 것**
- TR_ID·응답 필드명·엔드포인트 매핑이 단일 출처로 정리됨
- 응답 키를 새로 호출해 추측하지 말고 이 문서에서 먼저 검색
- 예: KOSPI 종목 업종명은 `FHKST01010100` 응답의 `bstp_kor_isnm`
- 문서에 없는 새 엔드포인트 사용 시 raw 응답 확인 후 문서에 키 추가하고 사용

### 로컬 스크립트 실행 금지
- **자동화는 전부 GitHub Actions 에서 돔**. 로컬 cron 폐기됨 (2026-05-24)
- 로컬 실행은 디버그/테스트 시 단발만
- `interact 모드 실행 금지` → Warp 크레딧 대량 소모

### Git
- 항상 `git pull --rebase --autostash && git push` 순서
- conflict 방지: 노트북에서 데이터 파일 직접 수정 X

### investment-chatbot 연동
- 사용자 노트북의 `~/Projects/investment-chatbot/` 가 이 repo 의 CSV/JSON 의존
- 매일 22:00 + 노트북 wake 시 `run_daily.sh` 가 자동 git pull → vault ETL
- 따라서 reco CSV (`latest_수급_reco.csv`, `latest_quality_reco.csv`) 가 push 되어야 함 (`.gitignore` 풀려있음)

## Notion 구조
- 코스피 추천종목 : `3324a00632f880fbb014d766d87a1079` 하위에 날짜별 페이지 (제목: `📊 {date} 추천종목`)
- 코스닥 추천종목 : 동일 부모 페이지 (`🇰🇷 {date} KOSDAQ 추천종목`)
- US 추천종목 : 동일 부모 페이지 (`🇺🇸 {date} US 추천종목`)
- KOSPI Quality 추천종목 : 동일 부모 페이지 (`💎 {date} KOSPI Quality 추천종목`)
- KOSPI 교집합 추천종목 : 동일 부모 페이지 (`🔥 {date} KOSPI 교집합 추천종목`)
- YouTube 분석 : `3484a00632f880988b41e8b13d7fbb0b` 하위에 채널별 일일 페이지 (`{emoji} {date} {channel_name} 분석`)
  - `notion_daily_pages.json` 키 형식: `f"{date}_{slug}"`. 레거시 날짜-only 키는 3proTV 호환 fallback
  - Notion API 100 블록/요청 한도 회피: `append_to_page` 가 외부 블록 1개씩 전송

## 종목 질문 답변 방법
사용자가 종목을 물어보면:
1. `latest_kospi_supply.csv` 읽어서 해당 종목 조회
2. `fin_ratio_cache.csv` 에서 추가 재무 데이터 확인
3. 기준일자 먼저 알려주기

## 지금까지 겪은 주요 이슈 + 해결
- Gemini 무료 → 쿼터 소진 → **유료 전환**
- `gemini-2.0-flash` 구버전 → **gemini-2.5-flash 사용 중**
- YouTube IP 차단 (옛 정보) → **2026-05-24 클라우드 작동 확인**, 로컬 cron 폐기
- lock 파일 FileNotFoundError → finally 블록에 try/except
- Notion 아카이브 오류 → archived 감지 시 ID 자동 삭제 로직 내장
- 노트북 cron 과 클라우드 cron 의 git conflict (2026-05-22) → 노트북 cron 전체 폐기로 해결
- `latest_*_reco.csv` 가 .gitignore 라 investment-chatbot 노트북에 안 옴 → .gitignore 풀고 workflow commit step 추가 (2026-05-24)
