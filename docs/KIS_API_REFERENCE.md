# KIS (한국투자증권) OpenAPI 레퍼런스

이 파일은 본 프로젝트(`수급.py`, 향후 KIS 기반 모듈)에서 사용하는 한국투자증권 OpenAPI의 **응답 필드와 TR_ID 매핑**을 담은 단일 출처입니다.

## ⚡ 작업 규칙 (반드시 지킬 것)

KIS API 관련 작업(엔드포인트 추가·필드 추출·디버그) 시 **새로 호출해서 응답 키를 추측하지 말고**, 먼저 이 문서에서 필드명을 찾을 것. 예를 들어 종목 업종명은 별도 search-stock-info 호출이 필요한 게 아니라 이미 호출 중인 `FHKST01010100` (inquire-price) 응답의 `bstp_kor_isnm` 필드에 있음.

이 문서에서 못 찾을 때만 raw 응답을 dump 해서 키 확인.

---

## 인증·요청 공통

- 도메인: `https://openapi.koreainvestment.com:9443`
- 모의: `https://openapivts.koreainvestment.com:29443`
- 헤더: `authorization: Bearer <token>`, `appkey`, `appsecret`, `tr_id`
- 토큰 발급: `POST /oauth2/tokenP` — **1분에 1회 제한** (재시도 시 60s 이상 대기)

---

# 매매 (계좌·주문) — `auto_trade.py`

시세용 `APP_KEY` 와 **다른 앱키**를 쓴다. KIS 앱키는 계좌 단위로 발급되므로 매매계좌 키를
`KIS_TRADE_APP_KEY`/`KIS_TRADE_APP_SECRET` 로 따로 두고, 토큰 캐시도 `.kis_trade_token.json` 으로 분리한다.
계좌는 `KIS_ACCOUNTS="<계좌번호>-<상품코드>:라벨"` 형식. **실제 계좌번호는 `.env` 에만 — 이 저장소는 public 이다.** **라벨은 사람 메모일 뿐 API 가 검증하지 않는다**
(2026-09-06: 라벨이 `:ISA` 였으나 상품코드 01 은 종합위탁이었다).
**앱키 하나에 계좌 하나.** 앱키가 안 묶인 계좌는 전부 `INVALID_CHECK_ACNO` 라, 어느 계좌에 묶였는지는
후보 계좌를 순회 조회해서 `rt_cd=0` 하나를 찾는 방식으로 특정할 수 있다.

계좌 유효성 판별법 — 잘못된 계좌는 `rt_cd=2 OPSQ2000 INVALID_CHECK_ACNO` 로 튕긴다.
`rt_cd=0` 이면 그 앱키에 묶인 실계좌다. `조회할 내용이 없습니다(KIOK0560)` 는 **성공이며 잔고가 빈 것**이다.

## 매수가능조회 — `TTTC8908R`
`GET /uapi/domestic-stock/v1/trading/inquire-psbl-order`
- Request: `CANO`, `ACNT_PRDT_CD`, `PDNO`, `ORD_UNPR`, `ORD_DVSN`, `CMA_EVLU_AMT_ICLD_YN`, `OVRS_ICLD_YN`
- Response (output): `ord_psbl_cash`(현금 주문가능), `ord_psbl_sbst`(대용), `ruse_psbl_amt`(재사용),
  `max_buy_amt` / `max_buy_qty`(최대 매수금액·수량), `nrcvb_buy_amt`, `psbl_qty_calc_unpr`, `cma_evlu_amt`

## 잔고조회 — `TTTC8434R`
`GET /uapi/domestic-stock/v1/trading/inquire-balance`
- Request: 위 계좌 2종 + `AFHR_FLPR_YN`, `OFL_YN`, `INQR_DVSN`(02), `UNPR_DVSN`(01),
  `FUND_STTL_ICLD_YN`, `FNCG_AMT_AUTO_RDPT_YN`, `PRCS_DVSN`(00), `CTX_AREA_FK100`/`NK100`
- output1(array): `pdno`, `prdt_name`, `hldg_qty`, `pchs_avg_pric`, `prpr`
- output2[0]: `dnca_tot_amt`(예수금), `prvs_rcdl_excc_amt`(D+2), `tot_evlu_amt`
- 페이징: `tr_cont` 이 `F`/`M` 이면 `ctx_area_fk100`/`nk100` 을 넘겨 계속 조회

## 투자계좌자산현황 — `CTRP6548R`
`GET /uapi/domestic-stock/v1/trading/inquire-account-balance`
- Request: `CANO`, `ACNT_PRDT_CD`, `INQR_DVSN_1`, `BSPR_BF_DT_APLY_YN`
- output2: `tot_asst_amt`(총자산), `nass_tot_amt`(순자산), `evlu_amt_smtl`, `dncl_amt`, `pchs_amt_smtl` 등
- output1(array, 20행): 자산구분별 `evlu_amt`, `whol_weit_rt`(비중)

## 일별 주문체결조회 — `TTTC8001R`
`GET /uapi/domestic-stock/v1/trading/inquire-daily-ccld`
- Request: 계좌 2종 + `INQR_STRT_DT`, `INQR_END_DT`, `SLL_BUY_DVSN_CD`(00 전체),
  `INQR_DVSN`(00), `PDNO`, `CCLD_DVSN`(00), `ORD_GNO_BRNO`, `ODNO`, `INQR_DVSN_3`(00),
  `INQR_DVSN_1`, `CTX_AREA_FK100`/`NK100`

## 해시키 — `POST /uapi/hashkey`
주문 API 는 `hashkey` 헤더가 필요하다. 헤더는 `content-type`/`appkey`/`appsecret` 만 (토큰 불필요).
- Response: `{"BODY": {...보낸 그대로...}, "HASH": "<64자 hex>"}` → 이 `HASH` 를 주문 요청의 `hashkey` 헤더에 넣는다

## 주식주문(현금) — `TTTC0802U`(매수) / `TTTC0801U`(매도)
`POST /uapi/domestic-stock/v1/trading/order-cash` · 모의는 `VTTC0802U`/`VTTC0801U`
- Body: `CANO`, `ACNT_PRDT_CD`, `PDNO`, `ORD_DVSN`, `ORD_QTY`, `ORD_UNPR`
- `ORD_DVSN`: `00` 지정가 · `01` 시장가(이때 `ORD_UNPR="0"`)
- Response output: `ODNO`(주문번호), `ORD_TMD`(주문시각), `KRX_FWDG_ORD_ORGNO`
- ⚠️ **2026-09-06 기준 미검증** — 예수금 0원이라 실주문을 넣어보지 못했다.
  첫 실주문에서 `rt_cd`/`msg_cd` 를 확인하고 이 줄을 지울 것. TR_ID 가 거부되면
  `KIS_TR_BUY` 환경변수로 갈아끼울 수 있게 해 뒀다.

---

# 국내주식

## 주식현재가 시세 — `FHKST01010100`

- URL: `/uapi/domestic-stock/v1/quotations/inquire-price`
- HTTP: GET
- API ID: `v1_국내주식-008`

### Request

| 파라미터 | 설명 | 예시 |
|---|---|---|
| FID_COND_MRKT_DIV_CODE | 시장 분류 | J:KRX, NX:NXT, UN:통합 |
| FID_INPUT_ISCD | 입력 종목코드 | 005930 (삼성전자), ETN은 앞에 Q |

### Response (output object) — 핵심 필드

| 필드 | 설명 |
|---|---|
| iscd_stat_cls_code | 종목 상태 구분 코드 |
| rprs_mrkt_kor_name | 대표 시장 한글명 |
| **bstp_kor_isnm** | **업종 한글명** ← 섹터 중립화·표시용 |
| stck_prpr | 주식 현재가 |
| prdy_vrss | 전일 대비 |
| prdy_ctrt | 전일 대비율 |
| acml_tr_pbmn | 누적 거래 대금 |
| acml_vol | 누적 거래량 |
| stck_oprc | 시가 |
| stck_hgpr | 최고가 |
| stck_lwpr | 최저가 |
| stck_mxpr | 상한가 |
| stck_llam | 하한가 |
| stck_sdpr | 기준가 |
| hts_frgn_ehrt | HTS 외국인 소진율 |
| frgn_ntby_qty | 외국인 순매수 수량 |
| pgtr_ntby_qty | 프로그램매매 순매수 수량 |
| cpfn | 자본금 |
| stck_fcam | 액면가 |
| lstn_stcn | 상장 주수 |
| **hts_avls** | **HTS 시가총액** |
| **per** | **PER** |
| **pbr** | **PBR** |
| **eps** | **EPS** |
| **bps** | **BPS** |
| stac_month | 결산 월 |
| vol_tnrt | 거래량 회전율 |
| d250_hgpr / d250_lwpr | 250일 최고/최저가 |
| stck_dryy_hgpr / stck_dryy_lwpr | 연중 최고/최저가 |
| w52_hgpr / w52_lwpr | 52주 최고/최저가 |
| frgn_hldn_qty | 외국인 보유 수량 |
| invt_caful_yn | 투자유의 여부 |
| mrkt_warn_cls_code | 시장경고코드 |
| short_over_yn | 단기과열 여부 |
| sltr_yn | 정리매매 여부 |
| mang_issu_cls_code | 관리종목 여부 |

> **수급.py 에서 이미 호출 중. `bstp_kor_isnm` 으로 업종명 추출 가능 — 별도 API 호출 불필요.**

## 주식현재가 시세2 — `FHPST01010000`

- URL: `/uapi/domestic-stock/v1/quotations/inquire-price-2`
- 모의 미지원

### Response — 추가 핵심 필드 (시세1 과 다른 부분)

| 필드 | 설명 |
|---|---|
| **bstp_cls_code** | **업종 구분 코드** (4자리) |
| **bstp_kor_isnm** | 업종 한글명 (※ 거래소 정보로 일부 종목 미회신) |
| crdt_rate | 신용 비율 |
| marg_rate | 증거금 비율 |
| stck_prpr / prdy_vrss / prdy_ctrt | 현재가/전일대비/등락률 |
| acml_tr_pbmn / acml_vol | 누적 거래대금/거래량 |
| stange_runup_yn | 이상급등 여부 |
| ssts_hot_yn | 공매도 과열 여부 |
| low_current_yn | 저유동성 종목 여부 |
| vi_cls_code | VI 적용 구분 |

## 주식현재가 일자별 — `FHKST01010400`

- URL: `/uapi/domestic-stock/v1/quotations/inquire-daily-price`
- 최근 30일/주/월 제한

### Response (output array)

| 필드 | 설명 |
|---|---|
| stck_bsop_date | 영업일자 |
| stck_oprc / stck_hgpr / stck_lwpr / stck_clpr | OHLC |
| acml_vol | 거래량 |
| prdy_vrss / prdy_vrss_sign / prdy_ctrt | 전일 대비·부호·등락률 |
| hts_frgn_ehrt | HTS 외국인 소진율 |
| frgn_ntby_qty | 외국인 순매수 수량 |
| flng_cls_code | 락 구분 |

## 주식현재가 투자자 — `FHKST01010900`

- URL: `/uapi/domestic-stock/v1/quotations/inquire-investor`
- **수급.py 에서 사용 중 (30일 수급 데이터)**
- 외국인 = 외국인 + 기타외국인. 당일 데이터는 장 종료 후 제공.

### Response (output array) — 핵심

| 필드 | 설명 |
|---|---|
| stck_bsop_date | 영업일자 |
| stck_clpr | 종가 |
| prsn_ntby_qty / frgn_ntby_qty / orgn_ntby_qty | 개인/외국인/기관 순매수 수량 |
| **prsn_ntby_tr_pbmn** | 개인 순매수 거래대금 |
| **frgn_ntby_tr_pbmn** | 외국인 순매수 거래대금 |
| **orgn_ntby_tr_pbmn** | 기관계 순매수 거래대금 |
| {prsn,frgn,orgn}_shnu_vol / _shnu_tr_pbmn | 매수 거래량/대금 |
| {prsn,frgn,orgn}_seln_vol / _seln_tr_pbmn | 매도 거래량/대금 |

## 국내주식 기간별 시세 — `FHKST03010100`

- URL: `/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice`
- 한 번 호출에 최대 100건. **수급.py 에서 일별 종가 조회용으로 사용.**

### Request

| 파라미터 | 값 |
|---|---|
| FID_COND_MRKT_DIV_CODE | J/NX/UN |
| FID_INPUT_ISCD | 종목코드 |
| FID_INPUT_DATE_1 / FID_INPUT_DATE_2 | 시작/종료일 (YYYYMMDD) |
| FID_PERIOD_DIV_CODE | D/W/M/Y |
| FID_ORG_ADJ_PRC | 0:수정주가 1:원주가 |

### Response

- output1 (object): 기본정보 (per/pbr/eps/hts_avls/lstn_stcn 등)
- output2 (array): 일자별 OHLCV — `stck_bsop_date`, `stck_clpr`, `stck_oprc`, `stck_hgpr`, `stck_lwpr`, `acml_vol`, `acml_tr_pbmn`, `flng_cls_code`, `prtt_rate`, `mod_yn`

## 재무 데이터 시리즈

### 대차대조표 — `FHKST66430100`
- URL: `/uapi/domestic-stock/v1/finance/balance-sheet`
- output array: `stac_yymm`, `cras`(유동자산), `fxas`(고정자산), `total_aset`, `flow_lblt`(유동부채), `fix_lblt`(고정부채), `total_lblt`, `cpfn`(자본금), `cfp_surp`(자본잉여금 ※99.99), `prfi_surp`(이익잉여금 ※99.99), `total_cptl`

### 손익계산서 — `FHKST66430200`
- URL: `/uapi/domestic-stock/v1/finance/income-statement`
- output array: `stac_yymm`, `sale_account`(매출액), `sale_cost`, `sale_totl_prfi`(매출총이익), `depr_cost`(※99.99), `sell_mang`(※99.99), `bsop_prti`(영업이익), `bsop_non_ernn`(※99.99), `bsop_non_expn`(※99.99), `op_prfi`(경상이익), `spec_prfi`/`spec_loss`, `thtr_ntin`(당기순이익)

### 재무비율 — `FHKST66430300` ← 수급.py 에서 사용 중
- URL: `/uapi/domestic-stock/v1/finance/financial-ratio`
- output array:
  - `stac_yymm` 결산년월
  - **`grs`** 매출액 증가율
  - **`bsop_prfi_inrt`** 영업이익 증가율 (적자지속/흑자전환/적자전환은 0)
  - `ntin_inrt` 순이익 증가율
  - **`roe_val`** ROE 값
  - `eps`, `sps`(주당매출액), `bps`, `rsrv_rate`(유보비율)
  - **`lblt_rate`** 부채 비율

### 수익성비율 — `FHKST66430400`
- URL: `/uapi/domestic-stock/v1/finance/profit-ratio`
- output array: `cptl_ntin_rate`(총자본순이익율), `self_cptl_ntin_inrt`(자기자본순이익율 = ROE), `sale_ntin_rate`(매출액순이익율), `sale_totl_rate`(매출액총이익율)

### 기타주요비율 — `FHKST66430500`
- URL: `/uapi/domestic-stock/v1/finance/other-major-ratios`
- output array: `payout_rate`(배당성향, ※무시), `eva`, `ebitda`, **`ev_ebitda`**

### 안정성비율 — `FHKST66430600`
- URL: `/uapi/domestic-stock/v1/finance/stability-ratio`
- output array: `lblt_rate`(부채비율), `bram_depn`(차입금의존도), `crnt_rate`(유동비율), `quck_rate`(당좌비율)

### 성장성비율 — `FHKST66430800`
- URL: `/uapi/domestic-stock/v1/finance/growth-ratio`
- output array: `grs`(매출액증가율), `bsop_prfi_inrt`(영업이익증가율), `equt_inrt`(자기자본증가율), `totl_aset_inrt`(총자산증가율)

## 예탁원 배당정보 — `HHKDB669102C0`

- URL: `/uapi/domestic-stock/v1/ksdinfo/dividend`
- 배당 일정 조회 (예탁원 자료, 정보용)
- output1 array: `record_date`, `sht_cd`, `isin_name`, `divi_kind`, `face_val`, `per_sto_divi_amt`(현금배당금), `divi_rate`(현금배당률%), `stk_divi_rate`(주식배당률%), `divi_pay_dt`, `stk_div_pay_dt`, `odd_pay_dt`, `stk_kind`, `high_divi_gb`(고배당여부)

## 종목조건검색 목록 — `HHKST03900300`

- URL: `/uapi/domestic-stock/v1/quotations/psearch-title`
- HTS [0110] 사용자조건검색 목록. 결과를 psearch-result 의 input(seq)으로 사용.
- output2: `user_id`, `seq`(조건키, 0번부터), `grp_nm`, `condition_nm`

## 시세분석

### 국내기관·외국인 매매종목 가집계 — `FHPTJ04400000`
- URL: `/uapi/domestic-stock/v1/quotations/foreign-institution-total`
- 장중 가집계 (외국인 09:30/11:20/13:20/14:30, 기관 10:00/11:20/13:20/14:30)
- output: `hts_kor_isnm`, `mksc_shrn_iscd`, `ntby_qty`, `frgn_ntby_qty/tr_pbmn`, `orgn_ntby_qty/tr_pbmn`, 세부 기관별(`ivtr`, `bank`, `insu`, `mrbn`, `fund`, `etc_orgt`, `etc_corp`)

### 외국계 매매종목 가집계 — `FHKST644100C0`
- URL: `/uapi/domestic-stock/v1/quotations/frgnmem-trade-estimate`
- output: `stck_shrn_iscd`, `hts_kor_isnm`, `glob_ntsl_qty`, `glob_total_seln_qty`, `glob_total_shnu_qty`

### 종목별 투자자매매동향(일별) — `FHPTJ04160001`
- URL: `/uapi/domestic-stock/v1/quotations/investor-trade-by-stock-daily`
- HTS [0416] — 단위: 금액(백만원), 수량(주)
- output1: `stck_prpr`, `prdy_vrss`, `acml_vol`, `rprs_mrkt_kor_name`
- output2 (array): `stck_bsop_date`, `stck_clpr`, OHLCV, 모든 투자자 그룹 별 순매수/매수/매도 (수량 + 거래대금) — `prsn_*`, `frgn_*` (등록/비등록 포함), `orgn_*`, `scrt_*`, `ivtr_*`, `pe_fund_*`, `bank_*`, `insu_*`, `mrbn_*`, `fund_*`, `etc_*`, `etc_orgt_*`, `etc_corp_*`, `bold_yn`

### 시장별 투자자매매동향(일별) — `FHPTJ04040000`
- URL: `/uapi/domestic-stock/v1/quotations/inquire-investor-daily-by-market`
- 시장구분코드 U(업종) 사용. KOSPI=KSP, KOSDAQ=KSQ
- output (array): 업종 지수 OHLC + 투자자별 순매수 수량/대금 (frgn/prsn/orgn/scrt/ivtr/pe_fund/bank/insu/mrbn/fund/etc/etc_orgt/etc_corp)

### 종목별 외국계 순매수추이 — `FHKST644400C0`
- URL: `/uapi/domestic-stock/v1/quotations/frgnmem-pchs-trend`
- KRX 만 지원
- output (array): `bsop_hour`, `stck_prpr`, `prdy_vrss/sign/ctrt`, `acml_vol`, `frgn_seln_vol`, `frgn_shnu_vol`, **`glob_ntby_qty`**, `frgn_ntby_qty_icdc`

---

# 해외주식

## 현재가 상세 — `HHDFS76200200`

- URL: `/uapi/overseas-price/v1/quotations/price-detail`
- output:
  - `rsym`, `pvol`(전일거래량), `open`, `high`, `low`, `last`, `base`(전일종가)
  - `tomv`(시가총액), `pamt`(전일거래대금), `uplp`/`dnlp`(상하한가)
  - `h52p/d`, `l52p/d` (52주 최고·최저)
  - **`perx`, `pbrx`, `epsx`, `bpsx`** (PER/PBR/EPS/BPS)
  - `shar`(상장주수), `mcap`(자본금), `curr`(통화), `zdiv`(소수점), `vnit`(매매단위)
  - 원환산: `t_xprc`, `t_xdif`, `t_xrat`, `p_xprc`, `p_xdif`, `p_xrat`, `t_rate`/`p_rate`
  - `e_ordyn`(거래가능), `e_hogau`(호가단위), **`e_icod`(업종/섹터 — 한글 라벨)**, `e_parp`(액면가)
  - `tvol`(거래량), `tamt`(거래대금), `etyp_nm`(ETP분류명)

> **해외주식 업종은 `e_icod` 필드** — KIS 가 제공. KOSPI 와 다른 점.

## 기간별 시세 — `HHDFS76240000`

- URL: `/uapi/overseas-price/v1/quotations/dailyprice`
- 한 번에 100건. 미국=실시간 무료, 홍콩/베트남/중국/일본=15분 지연
- 거래소(EXCD): NYS, NAS, AMS, TSE, HKS, SHS, SZS, HSX, HNX
- GUBN: 0(일)/1(주)/2(월), MODP: 0(미반영)/1(수정주가반영)
- output1: `rsym`, `zdiv`, `nrec`(전일종가)
- output2 (array): `xymd`, `clos`(종가), `sign`(1상한/2상승/3보합/4하한/5하락), `diff`, `rate`, `open`, `high`, `low`, `tvol`, `tamt`, `pbid`/`vbid`, `pask`/`vask`

## 종목·지수·환율 기간별 시세 — `FHKST03030100`

- URL: `/uapi/overseas-price/v1/quotations/inquire-daily-chartprice`
- ⚠️ 미국주식은 **다우30/나스닥100/S&P500 만 조회 가능**. 그 외엔 `HHDFS76240000` 사용.
- FID_COND_MRKT_DIV_CODE: N(해외지수), X(환율), I(국채), S(금선물)
- output1: 기본정보 (`hts_kor_isnm`, `ovrs_nmix_prpr`, `ovrs_prod_oprc/hgpr/lwpr`, `prdy_vrss/sign/ctrt`)
- output2 (array): `stck_bsop_date`, `ovrs_nmix_prpr/oprc/hgpr/lwpr`, `acml_vol`, `mod_yn`

## 시세분석 — 순위 시리즈

거래소(EXCD) 코드: NYS, NAS, AMS, HKS, SHS, SZS, HSX, HNX, TSE.

| TR_ID | 이름 | 핵심 응답 (output2 array) |
|---|---|---|
| HHDFS76260000 | 가격급등락 | `rsym`, `excd`, `symb`, `knam`, `last`, `sign`, `diff`, `rate`, `tvol`, `pask`/`pbid`, `n_base`/`n_diff`/`n_rate`, `enam`, `e_ordyn` |
| HHDFS76280000 | 매수체결강도상위 (분단위) | 위 + `tpow`(당일체결강도), `powx`(체결강도) |
| HHDFS76300000 | 신고/신저가 | `name`, `last` 등 + GUBN(1=신고/0=신저), GUBN2(0=일시/1=유지) |
| HHDFS76330000 | 거래증가율순위 | + `n_tvol`(평균거래량), `n_rate`(증가율), `rank` |
| HHDFS76290000 | 상승율/하락율 (일단위) | GUBN(0=하락/1=상승), `rank`, NDAY |
| HHDFS76310010 | 거래량순위 | + `tamt`, `a_tvol`(평균거래량), `rank`, PRC1/PRC2(가격필터) |
| HHDFS76320010 | 거래대금순위 | + `tamt`, `a_tamt` |
| HHDFS76340000 | 거래회전율순위 | + `n_tvol`, `shar`(상장주식수), `tover`(회전율) |
| **HHDFS76350100** | **시가총액순위** | + `shar`, **`tomv`(시가총액)**, `grav`(비중), `rank` |

공통 Request: `KEYB`(공백), `AUTH`(공백), `EXCD`, `NDAY` 또는 `MIXN`, `VOL_RANG`(0~6).

## 업종

### 해외주식 업종별 시세 — `HHDFS76370000`
- URL: `/uapi/overseas-price/v1/quotations/industry-theme`
- ICOD = `HHDFS76370100` 으로 조회한 업종코드
- output2: `rsym`, `excd`, `symb`, `name`, `last`, `sign`, `diff`, `rate`, `tvol`, `vask`/`pask`/`pbid`/`vbid`, `seqn`(순위), `ename`, `e_ordyn`

### 해외주식 업종별 코드조회 — `HHDFS76370100`
- URL: `/uapi/overseas-price/v1/quotations/industry-price`
- output2: `icod`(업종코드), `name`(업종명)

## 권리·뉴스

### 기간별 권리 — `CTRGT011R`
- URL: `/uapi/overseas-price/v1/quotations/period-rights`
- RGHT_TYPE_CD: 01(유상), 02(무상), 03(배당), 11(합병), 14(액면분할), 15(액면병합), 17(감자), 54(WR청구), 61(원리금상환), 71(WR소멸), 74(배당옵션), 75(특별배당), 76(ISINCODE변경), 77(실권주청약)
- output: `bass_dt`, `rght_type_cd`, `pdno`, `prdt_name`, `acpl_bass_dt`, `sbsc_strt_dt`/`sbsc_end_dt`, `cash_alct_rt`, `stck_alct_rt`, `crcy_cd`, `alct_frcr_unpr`, `stkp_dvdn_frcr_amt2~4`, `dfnt_yn`(확정여부)

### 해외뉴스 종합 (제목) — `HHPSTH60100C1`
- URL: `/uapi/overseas-price/v1/quotations/news-title`
- NATION_CD: CN, HK, US (공백=전체)
- outblock1 (array): `info_gb`, `news_key`, `data_dt`, `data_tm`, `class_cd`, `class_name`, `source`, `nation_cd`, `exchange_cd`, `symb`, `symb_name`, `title`

### 해외주식 권리종합 (ICE) — `HHDFS78330900`
- URL: `/uapi/overseas-price/v1/quotations/rights-by-ice`
- NCOD: CN, HK, US, JP, VN
- output1: `anno_dt`, `ca_title`, `div_lock_dt`, `pay_dt`, `record_dt`, `validity_dt`, `local_end_dt`, `lock_dt`, `delist_dt`, `redempt_dt`, `early_redempt_dt`, `effective_dt`

### 해외속보 (제목) — `FHKST01011801`
- URL: `/uapi/overseas-price/v1/quotations/brknews-title`
- 최대 100건
- output: `cntt_usiq_srno`, `news_ofer_entp_code`, `data_dt`, `data_tm`, `hts_pbnt_titl_cntt`, `news_lrdv_code`, `dorg`, `iscd1~10`, `kor_isnm1~10`

---

## 본 프로젝트(`수급.py`) 활용 매핑

| KIS API | TR_ID | 본 프로젝트 사용처 |
|---|---|---|
| 주식현재가 시세 | FHKST01010100 | `get_valuation_info` (시총·PER·PBR·EPS·**bstp_kor_isnm 업종**) |
| 주식현재가 투자자 | FHKST01010900 | `get_netflow_history` (30일 수급) |
| 국내주식 기간별시세 | FHKST03010100 | `get_daily_prices` (정배열용 종가 시계열) |
| 국내주식 재무비율 | FHKST66430300 | `get_financial_ratio` (ROE, 부채비율, 매출증가율, 영업이익증가율) |

---

## 갱신 가이드

- 새 KIS API 사용 추가 시 → 이 문서에 응답 필드 정리 추가
- 응답 키가 문서와 다르면 → raw 응답 확인 후 문서 수정 (코드의 키 추측 금지)
- 한투 API 문서 업데이트(공식): https://apiportal.koreainvestment.com/apiservice-category
