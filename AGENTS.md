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
- `momentum_daily.py` : **30일 모멘텀 추천 (healthy10 진입)**. 3단계 거름망: ①ETF/ETN/우선주 제외 + 5일평균 거래대금 100억↑ ②모멘텀(hi60+disp20+ret5 z합) 상위10% ③저변동성(vol20) 10개 압축. 전 종목 최근 100일 1콜. `latest_momentum_reco.csv` 저장 + Notion `🚀 {date} KOSPI 30일 모멘텀 추천` push (상위 카드에 Gemini 검색그라운딩 '이슈'). 제안 청산 = +20% 익절/-10% 손절. Notion 헬퍼는 수급.py 재활용 (`NOTION_API_KEY` 없으면 로컬 생략)
- `momentum_backtest.py` / `supply_increment.py` / `value_increment.py` / `walkforward.py` / `trade_sim*.py` : **연구용 1회성 스크립트** (factor·진입·청산 검증). 운영 아님. 토큰캐시(`.kis_token.json`)·가격캐시(`price_cache/`)·OHLC캐시(`ohlc_cache/`) 정의. 결론은 아래 "모멘텀 연구" 참조
- `auto_trade.py` : **리포트 추천 → 텔레그램 승인 → 한투 실계좌 매수**. `propose`(주문안 전송)·`poll`(승인 확인→주문)·`setup`(chat_id)·`status`. 상세는 아래 '자동매수'
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
- 모멘텀 레포트 : 평일 17:35 KST (`Daily Momentum Report`, 수급 직후)
- **로컬 cron/launchd 없음** (2026-05-24 폐기). 노트북은 데이터 받기만 — `investment-chatbot/etl/run_daily.sh` 의 git pull 이 갱신 담당

## 모멘텀 연구 결론 (2026-06-06) — momentum_daily.py 근거
30일 forward-return 예측 신호 검증 (KOSPI 2023~2026, 전종목, point-in-time 거래대금컷, 레짐별 IC + 분위 스프레드):
- **가격 모멘텀(hi60 60일고점근접 + disp20 MA20이격 + ret5)만 채택.** 7레짐 IC 양수, walk-forward IR~0.15·승률~51%. 추세장 강·반전장(2024H2) 약.
- **수급 미반영** : 가격과 중복 (직교IC≈0, 결합시 −0.32%p 악화).
- **가치/퀄리티 미반영** : 직교IC +0.06 있으나 실전 분위엔 안 잡힘, 가중스윕서 평균·IR 다 깎임 (폭락보험만).
- **적응형(매일 로직 자동갱신) 3회 기각** : 6피처·9피처(수급포함)·signed/clip·3/6mo 전부 사전등록 검증서 고정 모멘텀에 패배. 트레일링IC가 mean-revert에 whipsaw + 30일 라벨지연 + 레짐 6개로 학습 불가. 추가 세팅탐색 금지(과적합).
- **진입·청산 연구 (trade_sim*.py, OHLC 백테스트)**: 손절-10%/익절+20% 룰을 OHLC 정밀 시뮬. 핵심 발견 = **진입이 청산보다 중요.** 기존 "최고급등 top10" 진입은 소진된 꼭지라 ~본전. **3단계 healthy10**(거래대금100억↑→모멘텀top10%→저변동성10) 진입으로 바꾸니 건당 +1.3%/30일·승률42%·7레짐중 5개 양수 (단일레짐 착시 아님). 청산은 +20/-10(하락장 방어, 부드러움) vs 30일보유(평균 약간↑, 드로다운↑) 취향 — healthy10이면 둘 다 양수. → momentum_daily.py 가 healthy10 채택.
- ⚠️ 한계 : 생존편향(오늘 상장종목만), 거래비용 미반영(세후 연~10-13% 추정), 단일 강세장 편중 표본, 롱온리라 하락장 약.

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

### 산출물 검증 게이트 (제거 금지, 2026-08-29)
- `sanity_check.py` — 산출물 CSV 6개를 검증하고 치명 실패 시 종료코드 1. `daily_quant.yml`·`momentum_daily.yml` 의 **커밋 스텝 앞**에 배치돼 있어, 깨진 산출물은 레포에 안 들어가고 런이 빨간불이 된다
- 검증 항목: 행수 범위(전종목 2000~3200 / reco 100 / 모멘텀 10) · 필수 컬럼 · 기준일이 당일(영업일 기준) · 핵심 수치 전부 NaN/0 아님 · 종목코드 중복 없음
- **왜**: 파이프라인 스텝 다수가 `continue-on-error: true` 라 실패가 success 로 묻혔다. 자막 5일간 0개(프록시 402)·NameError 8일 정지가 모두 며칠 뒤 대시보드 경고로야 발견됐다. 스텝 성공여부가 아니라 **산출물**로 판정하는 이유 = 스텝이 성공하고도 쓰레기를 내놓는 게 무음 실패의 본질
- 게이트가 죽으면 커밋이 전부 skip 되고 하위 소비자는 어제 데이터를 유지한다. **낡은 데이터 > 깨진 데이터** 가 의도다
- 기대 형태가 바뀌면(TOP100→TOP50 등) `SPEC` 을 같이 고칠 것. 게이트를 지우거나 `continue-on-error` 를 붙이는 것으로 통과시키지 말 것

### 자동매수 (2026-09-06 신설)

`auto_trade.py` — Report DB 가 고른 종목을 **텔레그램 승인을 받은 뒤에만** 한투 실계좌로 매수한다.
대상 계좌는 `.env` 의 `KIS_ACCOUNTS` 에만 둔다 — **저장소가 public 이라 계좌번호·잔고를 문서나 커밋 메시지에 쓰지 않는다.**
KIS 매매 API 레퍼런스는 `docs/KIS_API_REFERENCE.md` 의 '매매' 절.
매매 앱키는 **계좌 1개에만** 묶인다 — 이 키로 다른 계좌를 부르면 `INVALID_CHECK_ACNO` 다. 계좌를 바꾸려면 그 계좌로 앱키를 재발급해야 한다 (2026-09-06 실증).

- **승인 없이는 주문이 나가지 않는다.** 텔레그램 인라인 버튼을 규환님이 눌러야만 집행된다.
  버튼을 누른 사람의 `from.id` 가 `TELEGRAM_CHAT_ID` 와 다르면 무시한다
- **승인이 있어도 `TRADE_ENABLED=1` 이 아니면 드라이런.** 기본값 0 — 켜는 건 사람이 명시적으로
- **게이트 미충족이어도 주문안은 만든다** (2026-09-06 규환님 결정). 대신 알림 첫 줄에 게이트를 박고,
  자체 백테스트가 미충족 구간에서 손실이었다는 경고를 같이 보낸다. 숨기지 말 것
- 추천이 실린 **가장 최근 행**을 읽는다. 오늘 날짜로 찾으면 안 된다 —
  `daily_archive.py` 는 장 마감 뒤 그날 날짜로 쓰고 그 추천은 **다음 영업일에 살 종목**이다
- 상한 `TRADE_MAX_KRW`(10만원) · 승인 유효 `TRADE_APPROVE_TTL_H`(6h) · 하루 1건 · 장중(09:00~15:20)만 집행
- 추천이 4일 넘게 낡으면 거부한다 (리포트가 멈췄는데 옛 종목을 사는 사고 방지)
- **`.trade_state.json`·`trade_log.csv` 는 gitignore.** 저장소가 public 이라 계좌 활동이 새면 안 된다

노브: `TRADE_ENABLED`(0) · `TRADE_MAX_KRW`(100000) · `TRADE_APPROVE_TTL_H`(6) ·
`TRADE_ORD_DVSN`(00 지정가) · `KIS_TR_BUY`(TTTC0802U)

launchd: `com.vinicius.autotrade-propose`(평일 08:40 1회) · `com.vinicius.autotrade-poll`(09:00~15:20 매 5분).
사본은 `launchd/`. 주말은 `run_auto_trade.sh` 가 요일로, 대체공휴일은 `market_open_today()` 가 거른다.

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

## 비용·과금 구조 (2026-08-18 사고 후 정리 — 반드시 읽을 것)

유료 서비스가 **둘**이고, 둘 다 조용히 소진되면 파이프라인이 멈춘다. 과거에 5일간 아무도 몰랐다.

| 서비스 | 용도 | 소진되면 | 확인 위치 |
|---|---|---|---|
| **Webshare** 회전 프록시 | 유튜브 자막 취득(클라우드 IP 는 유튜브가 차단) | `402 Payment Required` → 자막 0건 | dashboard.webshare.io — **대역폭 잔량**(월 1GB), 요청당 실측 131KB ≈ 월 8,000요청 |
| **Gemini API** | 종목 분석·유튜브 분석·헤드라인 | `429 prepayment credits are depleted` → 분석 0건 | ai.studio/projects — **선불 크레딧 잔액**. 월 지출 한도(₩35,000)와 **다른 값**이다 |

### 겪은 사고와 원인 (같은 실수 반복 금지)

1. **Webshare 월 1GB 를 3일에 소진** — 돈 문제가 아니라 설계 문제였다.
   `retries_when_blocked` 기본값 **10** × 자막 실패 영상을 processed 마킹 안 함(매 실행 재시도) × **매시간 실행**
   = 실패 영상 8개에만 하루 640회. → 재시도 2회, 실패 3회 후 영구 스킵(`failed_videos.json`), 일일 상한(`yt_quota.json`).
2. **Gemini 실패 → 자막 재취득** 2차 낭비. 자막은 받았는데 Gemini 가 실패하면 마킹을 안 해서
   다음 실행이 같은 자막을 다시 받는다(대역폭 재소모). → Gemini 실패도 카운터에 반영해 3회 후 포기·마킹.
3. **`daily_quant` 가 매일 2번 실행** — cron-job.org(16:45)와 GitHub 네이티브 schedule(18시경) 둘 다 발동.
   모멘텀 Gemini·KIS 2배. 네이티브는 '백업' 이라 삭제 대신 `MOM_SKIP_IF_DONE`(대시보드에 오늘자
   토글이 있으면 Gemini 수집 전 return) → 1차 실패 시에만 2차가 일한다.
4. **Gemini 병렬 5** 가 분당 한도를 자체 유발 → `YT_WORKERS=2`.

### 규칙

- **무음 실패 금지**: 인프라 장애가 워크플로 success 로 위장되면 안 된다. 처리 대상이 있는데
  성공 0 이면 `SystemExit(1)`. (youtube_report, daily_recommend 적용)
- **재시도에 상한을 둘 것**: 실패를 무한 재시도하는 코드는 유료 자원을 태운다. 반드시 카운터 + 포기 조건.
- **밀린 backlog 를 한 번에 처리하지 말 것**: `YT_DAYS_BACK=1`(어제+오늘), `YT_MAX_PER_DAY=20`.
- **대시보드 상태 경고**: `portfolio.py:health_warnings()` 가 유튜브·모멘텀 정지를 감지해
  통합 대시보드 최상단에 🚨 콜아웃으로 띄운다. 사용자가 매일 보는 화면이라 여기가 가장 빨리 발견된다.
  **파이프라인을 추가하면 이 함수에 점검 항목도 추가할 것.**

### 비용 조절 노브 (환경변수)

`WEBSHARE_RETRIES`(2) · `YT_MAX_FAIL`(3) · `YT_MAX_PER_DAY`(20) · `YT_DAYS_BACK`(1) · `YT_WORKERS`(2)
· `YT_ANALYSIS_DAYS`(7, daily_recommend 입력 기간 — 실측 216,218자. 3 으로 낮추면 약 60% 절감)
· `MOM_SKIP_IF_DONE`(1) · `SKIP_NOTION_REPORT`(폐지한 리포트용) · `MOM_TARGET`(dashboard)

### 폐지된 리포트 (재도입 금지 — 사용자가 안 봄)

US 추천종목(워크플로 schedule 제거) · KOSPI 수급 · KOSPI Quality · 교집합 (노션 업로드만 차단,
**CSV 생성은 유지해야 함** — `latest_kospi_supply.csv`=모멘텀 유니버스, `latest_kospi_quality.csv`=모멘텀
부채순위·보유종목 리포트 섹터/PER 순위). 코스닥 리포트도 폐지 상태 유지.

## 분석 캐시 만료 (2026-08-25)

`momentum_analysis.json` 의 8섹션 프로즈는 `ANALYSIS_TTL_DAYS`(기본 5일)마다 재생성된다.

원래 7일 만료였으나 **한 번도 작동하지 않았다**: 신선도를 `date` 로 재는데 그 `date` 를
매 실행마다 오늘로 갱신해서, 매일 등장하는 종목은 영원히 만료되지 않았다. 프로즈가 20일 넘게
고정된 채 수급 막대만 갱신돼 "외국인 순매도"(실제 +305억 최대 매수) 같은 모순이 생겼다.
→ `full`(마지막 전체분석일)을 따로 기록해 그걸로 판정한다. **`full` 은 재사용 시 갱신 금지.**

교훈: **자기 만료일을 스스로 연장하는 캐시는 만료되지 않는다.** 신선도 기준 필드와
매 실행 갱신되는 필드는 반드시 분리할 것.

비용: 종목당 주 1.4회 전체분석. 부담되면 `ANALYSIS_TTL_DAYS` 를 올린다.

## 유튜브 처리량 한도 (2026-08-27 재조정)

| 값 | 지금 | 왜 |
|---|---|---|
| `YT_MAX_PER_DAY` | 40 | 20 이던 이유는 **무료 티어**의 Gemini 429(8/18 실측 35건 전건 실패). Tier 1 전환으로 해소 |
| `YT_MAX_VIDEOS` (채널당 1회) | 6 | 15 였을 때 3proTV 하나가 14편으로 일일 한도의 70% 를 먹고 나머지 채널이 밀렸다 |
| `YT_WORKERS` | 2 | 5 는 분당 한도를 넘겨 429 를 자체 유발 |

**실질 제약이 쿼터 → 월 지출 한도로 바뀌었다.** 한도를 더 올리기 전에 반드시 확인:
- aistudio.google.com/app/apikey → 월 지출 한도 사용률
- **월 지출 한도 ≠ 선불 크레딧 잔액** — 크레딧이 먼저 소진되면 한도가 남아도 429 가 난다

Gemini 키를 momentum(2리포트) + daily_recommend + youtube 가 **공유**한다.
유튜브 한도를 올리면 다른 리포트가 같이 죽을 수 있으므로, 올린 뒤 하루는 세 리포트가
모두 정상 생성되는지 확인할 것.
