"""일일 30일-모멘텀 추천 — 검증된 고정 로직(hi60+disp20+ret5).

각 종목 최근 ~100거래일 1콜 → 오늘 시점 feature → 횡단 z합 점수 → 유동성컷 → TOP N.
검증: walk-forward IR ~0.14, 승률 ~51%, 추세장 강·반전장 약 (자세히 [[momentum-30d-research]]).
실행: python momentum_daily.py   (UNIV_TOP=300 로 테스트 축소 가능)
"""
import os, time, random, re, requests
import pandas as pd, numpy as np
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from momentum_backtest import token, _get, BASE, APP_KEY, APP_SECRET, KST


def _excluded(name, sector):
    """레포트=개별주. ETF/ETN·우선주·스팩 제외."""
    s, n = str(sector), str(name)
    return ("ETF" in s or "ETN" in s or "스팩" in n or bool(re.search(r"우[BC]?$", n)))

_DIR = os.path.dirname(os.path.abspath(__file__))
UNIVERSE_CSV = os.path.join(_DIR, "latest_kospi_supply.csv")
UNIV_TOP  = int(os.environ.get("UNIV_TOP", "0"))   # 0=전종목, n=시총상위 n (테스트용)
TOP_N     = 10                    # 최종 추천 종목 수
# 파라미터화 (2026-06 백테스트): 기본=현행 10%·게이트없음. 20%컷+게이트 리포트는 env로 별도 실행.
MOM_CUT   = float(os.environ.get("MOM_CUT", "0.10"))   # 2단계 모멘텀 상위 컷
MOM_GATE  = os.environ.get("MOM_GATE", "0") == "1"     # 추세이탈 게이트: 비강세(200·120일선 이탈)면 현금
MOM_LABEL = os.environ.get("MOM_LABEL", "")            # 제목 구분자 (예: " (20%컷·추세게이트)")
MOM_TARGET = os.environ.get("MOM_TARGET", "page")  # page=날짜별 자기 페이지 / dashboard=통합 대시보드 토글
MOM_TAG   = os.environ.get("MOM_TAG", "")              # 출력파일 구분자(변형별 분리 — 기본 현행 파일명 유지)
# 상태/출력 파일 — 변형은 별도(10% 현행과 충돌 방지). 분석캐시는 코드별이라 공유 안전.
OUT_CSV  = os.path.join(_DIR, f"latest_momentum_reco{MOM_TAG}.csv")
HIST_CSV = os.path.join(_DIR, f"momentum_history{MOM_TAG}.csv")   # 날짜별 누적 (전일대비용)
CACHE_JSON = os.path.join(_DIR, "momentum_analysis.json")  # 종목별 분석 캐시 (토큰 절약, 공유)
LIQ_FLOOR = 1e10                  # 1단계: 5일평균 거래대금 100억원 하한
WORKERS   = 4


def log(m): print(f"[{datetime.now(KST):%H:%M:%S}] {m}")


def fetch_recent(code, tok):
    """최근 ~100거래일 일봉 1콜. [close array, value array(거래대금)]."""
    today = datetime.now(KST)
    j = _get(f"{BASE}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
             {"authorization": f"Bearer {tok}", "appkey": APP_KEY, "appsecret": APP_SECRET,
              "tr_id": "FHKST03010100", "custtype": "P"},
             {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
              "FID_INPUT_DATE_1": (today - timedelta(days=150)).strftime("%Y%m%d"),
              "FID_INPUT_DATE_2": today.strftime("%Y%m%d"),
              "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0"})
    time.sleep(random.uniform(0.2, 0.35))
    if not j or j.get("rt_cd") != "0":
        return None
    rows = [(r["stck_bsop_date"], float(r["stck_clpr"]), float(r.get("acml_tr_pbmn", 0) or 0))
            for r in (j.get("output2", []) or []) if r.get("stck_clpr") and r["stck_clpr"] != "0"]
    if len(rows) < 65:
        return None
    rows.sort()                      # 날짜 오름차순
    c = np.array([x[1] for x in rows]); v = np.array([x[2] for x in rows])
    o1 = j.get("output1", {}) or {}  # PER/PBR (정보용, 선정엔 미사용)
    per = float(o1.get("per") or 0); pbr = float(o1.get("pbr") or 0)
    return c, v, per, pbr, rows[-1][0]   # 마지막 거래일 (실제 기준일)


def investor_flows(code, tok):
    """FHKST01010900: 최근 일별 외인/기관/개인 순매수대금(백만원). 5일 합 + 방향 라벨."""
    j = _get(f"{BASE}/uapi/domestic-stock/v1/quotations/inquire-investor",
             {"authorization": f"Bearer {tok}", "appkey": APP_KEY, "appsecret": APP_SECRET,
              "tr_id": "FHKST01010900", "custtype": "P"},
             {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code})
    time.sleep(random.uniform(0.15, 0.3))
    o = (j or {}).get("output", []) or []
    if not o:
        return None
    def g(r, k):
        try: return float(r.get(k) or 0)
        except: return 0
    f5 = sum(g(r, "frgn_ntby_tr_pbmn") for r in o[:5])
    o5 = sum(g(r, "orgn_ntby_tr_pbmn") for r in o[:5])
    p5 = sum(g(r, "prsn_ntby_tr_pbmn") for r in o[:5])
    return {"frgn5": f5 / 100, "orgn5": o5 / 100, "prsn5": p5 / 100,  # 백만→억
            "frgn1": g(o[0], "frgn_ntby_tr_pbmn") / 100, "orgn1": g(o[0], "orgn_ntby_tr_pbmn") / 100}


INCOME_CACHE = os.path.join(_DIR, "fin_is_cache")


def fetch_income(code, tok):
    """FHKST66430200 손익계산서(분기누적) → 단일분기 [분기, 매출억, 영업익억] 최근 5개."""
    cache = os.path.join(INCOME_CACHE, f"{code}.pkl")
    if os.path.exists(cache):
        return pd.read_pickle(cache)
    j = _get(f"{BASE}/uapi/domestic-stock/v1/finance/income-statement",
             {"authorization": f"Bearer {tok}", "appkey": APP_KEY, "appsecret": APP_SECRET,
              "tr_id": "FHKST66430200", "custtype": "P"},
             {"FID_DIV_CLS_CODE": "1", "fid_cond_mrkt_div_code": "J", "fid_input_iscd": code})
    time.sleep(random.uniform(0.15, 0.3))
    o = (j or {}).get("output", []) or []
    res = None
    if o:
        df = pd.DataFrame(o)[["stac_yymm", "sale_account", "bsop_prti"]].copy()
        for c in ["sale_account", "bsop_prti"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.dropna().drop_duplicates("stac_yymm").sort_values("stac_yymm").reset_index(drop=True)
        # 분기누적 → 단일분기 (같은 연도 내 직전 분기 차감, 1분기=03월은 그대로)
        rows = []
        for i, x in df.iterrows():
            yy, mm = x["stac_yymm"][:4], x["stac_yymm"][4:]
            if mm == "03" or i == 0 or df.iloc[i-1]["stac_yymm"][:4] != yy:
                sale, op = x["sale_account"], x["bsop_prti"]
            else:
                sale = x["sale_account"] - df.iloc[i-1]["sale_account"]
                op = x["bsop_prti"] - df.iloc[i-1]["bsop_prti"]
            rows.append({"q": x["stac_yymm"], "sale": sale, "op": op})
        res = rows[-5:]
    os.makedirs(INCOME_CACHE, exist_ok=True); pd.to_pickle(res, cache)
    return res


EBITDA_CACHE = os.path.join(_DIR, "fin_ebitda_cache")


def fetch_ebitda(code, tok):
    """FHKST66430500 기타주요비율 → TTM(최근 4분기) EBITDA(억)·EV/EBITDA. 1콜. 캐시.
    데이터는 누적(YTD)이라: TTM = 최신YTD + 직전연도FY - 직전연도同기간YTD (최신분기 반영+12개월 규모).
    최신행이 12월(연간)이면 그대로 사용."""
    cache = os.path.join(EBITDA_CACHE, f"{code}.pkl")
    if os.path.exists(cache):
        return pd.read_pickle(cache)
    j = _get(f"{BASE}/uapi/domestic-stock/v1/finance/other-major-ratios",
             {"authorization": f"Bearer {tok}", "appkey": APP_KEY, "appsecret": APP_SECRET,
              "tr_id": "FHKST66430500", "custtype": "P"},
             {"fid_input_iscd": code, "fid_div_cls_code": "1", "fid_cond_mrkt_div_code": "J"})
    time.sleep(random.uniform(0.15, 0.3))
    o = (j or {}).get("output", []) or []
    res = None
    if o:
        def _f(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        eb = {str(r.get("stac_yymm")): _f(r.get("ebitda")) for r in o if r.get("stac_yymm")}
        ym = sorted(eb, reverse=True)
        latest = ym[0]; yr, mm = int(latest[:4]), latest[4:]
        if mm == "12":                       # 최신이 연간 결산
            ttm, basis = eb[latest], latest
        else:                                # TTM = 최신YTD + 직전FY - 직전연도 同기간YTD
            pfy, psame = f"{yr-1}12", f"{yr-1}{mm}"
            if eb.get(latest) is not None and eb.get(pfy) is not None and eb.get(psame) is not None:
                ttm, basis = eb[latest] + eb[pfy] - eb[psame], f"TTM~{latest}"
            elif eb.get(pfy) is not None:
                ttm, basis = eb[pfy], pfy        # 폴백: 직전 연간
            else:
                ttm, basis = eb.get(latest), latest
        # EV/EBITDA: 연간 결산행 API값(표준 연간배수), 0이면 무효
        evr = next((r for r in o if str(r.get("stac_yymm", "")).endswith("12")), None)
        eveb = _f(evr.get("ev_ebitda")) if evr else None
        res = {"q": basis, "ebitda": ttm, "ev_ebitda": eveb if (eveb and eveb > 0) else None}
    os.makedirs(EBITDA_CACHE, exist_ok=True); pd.to_pickle(res, cache)
    return res


def roe_latest(code, tok):
    """최근 0 아닌 ROE (FHKST66430300). KIS가 미발표 최근분기를 0.00으로 주므로 0은 건너뜀."""
    try:
        from value_increment import fetch_fin
        fd = fetch_fin(code, tok)
        if fd is not None and len(fd):
            nz = fd[pd.to_numeric(fd["roe"], errors="coerce").fillna(0) != 0]
            if len(nz):
                return float(nz.iloc[-1].get("roe"))
    except Exception:
        pass
    return None


def kospi_trend(tok):
    """KOSPI 지수 현재 추세 (200/120일선 대비). 레포트 정보용 — 매매 게이트 아님."""
    url = f"{BASE}/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice"
    hdr = {"authorization": f"Bearer {tok}", "appkey": APP_KEY, "appsecret": APP_SECRET,
           "tr_id": "FHKUP03500100", "custtype": "P"}
    today = datetime.now(KST); rows = []; d2 = today
    for _ in range(7):                       # 콜당 ~50행 → 60일 윈도로 ~250거래일 확보
        d1 = d2 - timedelta(days=60)
        j = _get(url, hdr, {"FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": "0001",
                            "FID_INPUT_DATE_1": d1.strftime("%Y%m%d"),
                            "FID_INPUT_DATE_2": d2.strftime("%Y%m%d"), "FID_PERIOD_DIV_CODE": "D"})
        time.sleep(random.uniform(0.2, 0.3))
        if j and j.get("rt_cd") == "0":
            for r in j.get("output2", []) or []:
                if r.get("bstp_nmix_prpr"):
                    rows.append((r["stck_bsop_date"], float(r["bstp_nmix_prpr"])))
        d2 = d1 - timedelta(days=1)
    if len(rows) < 200:
        return None
    s = pd.Series(dict(rows)).sort_index()   # 날짜 오름차순
    c = s.iloc[-1]; ma200 = s.tail(200).mean(); ma120 = s.tail(120).mean()
    uptrend = (c >= ma200 and c >= ma120)   # 백테스트 레짐게이트 정의 (강세) — 비강세면 경고
    if uptrend:
        txt, emo, color = "상승추세 (200·120일선 위) — 진입 우호", "📈", "green_background"
    elif c >= ma200:
        txt, emo, color = "상승추세 (200일선 위·단기 조정) — 보통", "📊", "yellow_background"
    else:
        txt, emo, color = "하락추세 (200일선 아래) — 진입 주의", "📉", "orange_background"
    return {"text": f"{txt}  ·  지수 {c:,.0f} / 200일선 {ma200:,.0f}", "emoji": emo,
            "color": color, "uptrend": uptrend}


def score_today(code, tok):
    r = fetch_recent(code, tok)
    if r is None:
        return None
    c, v, per, pbr, asof = r
    px = c[-1]
    dr = np.diff(c[-21:]) / c[-21:-1]
    chg = (px / c[-2] - 1) if len(c) >= 2 and c[-2] else 0.0   # 당일등락
    return {"code": code, "price": px, "asof": asof, "chg": chg,
            "hi60": px / c[-61:].max(),
            "disp20": px / c[-20:].mean() - 1,
            "ret5": px / c[-6] - 1,
            "ret20": px / c[-21] - 1,
            "vol20": dr.std(),
            "per": per, "pbr": pbr,         # 정보용 퀄리티 지표
            "liq5": v[-5:].mean()}      # 1단계 필터용 5일 평균 거래대금


def main():
    tok = token()
    uni = pd.read_csv(UNIVERSE_CSV)
    col = uni.columns[0]
    uni[col] = uni[col].astype(str).str.zfill(6)
    uni = uni[~uni.apply(lambda r: _excluded(r["종목명"], r["섹터"]), axis=1)]
    uni = uni.sort_values("시가총액", ascending=False)
    if UNIV_TOP:
        uni = uni.head(UNIV_TOP)
    meta = uni.set_index(col)[["종목명", "섹터", "시가총액"]].to_dict("index")
    codes = uni[col].tolist()
    log(f"유니버스 {len(codes)}개")

    rows, t0 = [], time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(score_today, c, tok): c for c in codes}
        for n, f in enumerate(as_completed(futs), 1):
            r = f.result()
            if r:
                rows.append(r)
            if n % 200 == 0:
                log(f"  {n}/{len(codes)} ({time.time()-t0:.0f}s)")
    df = pd.DataFrame(rows)
    log(f"가격 확보 {len(df)}")

    # 섹터 내 PER/PBR 상대위치 (전체 종목 기준, 저평가/평균/고평가 라벨용)
    df["섹터"] = df["code"].map(lambda c: meta.get(c, {}).get("섹터", ""))
    df["_per"] = df["per"].where(df["per"] > 0)
    df["_pbr"] = df["pbr"].where(df["pbr"] > 0)
    df["per_pct"] = df.groupby("섹터")["_per"].rank(pct=True)      # 0=섹터내 최저PER(쌈)
    df["pbr_pct"] = df.groupby("섹터")["_pbr"].rank(pct=True)
    df["per_rank"] = df.groupby("섹터")["_per"].rank()             # 1=섹터내 최저PER
    df["pbr_rank"] = df.groupby("섹터")["_pbr"].rank()
    df["sec_n"] = df.groupby("섹터")["_per"].transform("count")

    # 3단계 거름망 (healthy10): 거래대금 100억↑ → 모멘텀 top10% → 저변동성 10
    df = df[df["liq5"] >= LIQ_FLOOR].copy()                       # 1단계
    log(f"거래대금 {LIQ_FLOOR/1e8:.0f}억↑ 통과 {len(df)}")
    for fcol in ["hi60", "disp20", "ret5"]:
        df[fcol + "_z"] = (df[fcol] - df[fcol].mean()) / (df[fcol].std() + 1e-9)
    df["score"] = df[["hi60_z", "disp20_z", "ret5_z"]].sum(axis=1)
    decile = df.nlargest(max(10, int(len(df) * MOM_CUT)), "score")  # 2단계: 모멘텀 상위 MOM_CUT
    final = decile.nsmallest(TOP_N, "vol20").copy()              # 3단계: 저변동성 10
    final.sort_values("score", ascending=False, inplace=True)
    final.reset_index(drop=True, inplace=True)
    final["rank"] = np.arange(1, len(final) + 1)

    final["종목명"] = final["code"].map(lambda c: meta.get(c, {}).get("종목명", ""))
    final["섹터"]  = final["code"].map(lambda c: meta.get(c, {}).get("섹터", ""))
    out = final[["rank", "code", "종목명", "섹터", "price", "score", "chg", "ret20", "ret5", "vol20",
                 "per", "pbr", "per_pct", "pbr_pct", "per_rank", "pbr_rank", "sec_n", "hi60", "liq5"]]

    # 전일 대비: '실제 마지막 거래일(asof)' 기준. 장 전/장중 실행 시 어제 데이터를 오늘로 오기재하는 것 방지
    asof_raw = str(df["asof"].max())                  # YYYYMMDD
    today_str = f"{asof_raw[:4]}-{asof_raw[4:6]}-{asof_raw[6:8]}"
    deltas, prev_names = {}, {}
    hist = pd.read_csv(HIST_CSV, dtype={"code": str}) if os.path.exists(HIST_CSV) else pd.DataFrame()
    if len(hist):
        hist["code"] = hist["code"].str.zfill(6)
        prev_dates = sorted(d for d in hist["date"].unique() if d < today_str)
        if prev_dates:
            pv = hist[hist["date"] == prev_dates[-1]]
            prev_names = dict(zip(pv["code"], pv["종목명"]))
            pmap = pv.set_index("code")[["rank", "price", "score"]].to_dict("index")
            for _, r in out.iterrows():
                p = pmap.get(r["code"])
                deltas[r["code"]] = ({"rank_prev": int(p["rank"]),
                                      "price_chg": (r["price"]/p["price"]-1) if p["price"] else None,
                                      "score_prev": p["score"]} if p else {"new": True})
    today_codes = set(out["code"])
    newly = [r["종목명"] for _, r in out.iterrows() if deltas.get(r["code"], {}).get("new")]
    dropped = [prev_names[c] for c in prev_names if c not in today_codes]

    out.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    # 히스토리에 오늘 기록 추가 (오늘 날짜 기존 행은 교체)
    snap = out[["code", "종목명", "rank", "price", "score"]].copy(); snap.insert(0, "date", today_str)
    hist = pd.concat([hist[hist["date"] != today_str] if len(hist) else hist, snap], ignore_index=True)
    hist.to_csv(HIST_CSV, index=False, encoding="utf-8-sig")
    log(f"저장 {OUT_CSV} (최종 {len(out)}종목)")

    print(f"\n===== 오늘 건강 모멘텀 (저변동성 압축) TOP {TOP_N} =====")
    show = out.head(TOP_N).copy()
    show["price"] = show["price"].map(lambda x: f"{int(x):,}")
    show["ret20%"] = (show["ret20"] * 100).round(1)
    print(show[["rank", "code", "종목명", "섹터", "price", "score", "ret20%"]].to_string(index=False))

    top10 = out.head(TOP_N).copy()
    trend = kospi_trend(tok)
    if trend:
        log(f"KOSPI 추세: {trend['emoji']} {trend['text']}")
    # 게이트 발동: MOM_GATE 이고 비강세(추세이탈)면 현금 모드 — 추천 비우고 enrichment 생략
    # 중복 실행 방지: daily_quant 는 cron-job.org(주)와 GitHub schedule(백업) 이 매일 둘 다 발동해
    # 같은 리포트를 2번 만들고 있었다(모멘텀 Gemini·KIS 호출 2배). 대시보드에 이미 오늘자
    # 토글이 있으면 Gemini 수집 전에 빠진다 — 1차가 실패했을 때만 2차가 실제로 일하므로
    # 백업 성격은 그대로 유지된다.
    if MOM_TARGET == "dashboard" and os.environ.get("MOM_SKIP_IF_DONE", "1") == "1":
        try:
            import dashboard as _D
            _title = f"🚀 {today_str} KOSPI 30일 모멘텀 추천{MOM_LABEL}"
            _, _, _div, _, _tail = _D._layout(_D.page_id())
            for _b in _tail:
                if _b["type"] != "toggle":
                    continue
                _t = "".join(x.get("plain_text", "") for x in _b["toggle"]["rich_text"])
                if _t == _title:
                    log(f"⏭️ 오늘자 리포트가 이미 대시보드에 있음 → 건너뜀 (강제: MOM_SKIP_IF_DONE=0)")
                    return
        except Exception as e:
            log(f"  중복 확인 실패(계속 진행): {str(e)[:70]}")

    cash_mode = MOM_GATE and trend is not None and not trend.get("uptrend", True)
    flows, roes, incomes, ebitdas = {}, {}, {}, {}
    if not cash_mode:
        log("상위10 수급·ROE·EBITDA 수집")
        for code in top10["code"]:
            flows[code] = investor_flows(code, tok)
            roes[code] = roe_latest(code, tok)
            incomes[code] = fetch_income(code, tok)
            ebitdas[code] = fetch_ebitda(code, tok)
    else:
        log("⚠️ 추세 이탈 + 게이트 ON → 현금 모드 (추천 생략)")
    import json
    cache = {}
    if os.path.exists(CACHE_JSON):
        try:
            cache = json.load(open(CACHE_JSON, encoding="utf-8"))
        except Exception:
            cache = {}
    dranks = {} if cash_mode else load_debt_ranks()
    analysis = {} if cash_mode else (gemini_analyze(top10, flows, roes, cache, ebitdas, dranks) if os.environ.get("GEMINI_API_KEY") else {})
    if analysis:                                  # 캐시 저장 (등장한 종목만 유지 + 오래된건 정리)
        json.dump({k: v for k, v in cache.items()}, open(CACHE_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    if os.environ.get("NOTION_API_KEY"):
        upload_notion(top10, analysis, trend, flows, roes, deltas, newly, dropped, incomes,
                      today_str, cash=cash_mode, ebitdas=ebitdas, dranks=dranks)
    else:
        log("NOTION_API_KEY 없음 → Notion 업로드 생략 (로컬). Actions 에선 업로드됨")
        for _, r in top10.iterrows():
            a = analysis.get(r["code"], {})
            print(f"\n{'='*70}\n▶▶ {r['종목명']} ({r['code']}) | 이슈: {a.get('issue','')}")
            if a.get("업데이트"):
                print(f"\n🆕 오늘 업데이트: {a['업데이트']}")
            for k in ["요약", "사업실적", "촉매", "수급분석", "밸류", "강세론", "리스크", "관전"]:
                if a.get(k):
                    print(f"\n【{k}】 {a[k]}")


def _trim_phrase(t):
    """키워드구만 남김. 군더더기·따옴표·서술꼬리 제거, 쉼표→·, 정크 버림."""
    t = t.strip().replace("\n", " ").replace("'", "").replace('"', "").replace("`", "")
    # 알려진 군더더기 표현 제거 (위치 무관)
    t = re.sub(r"(다음과\s*같습니다|다음과\s*같은|로\s*요약할\s*수\s*있습니다|로\s*요약됩니다|"
               r"급등\s*(이유|요인|배경)는?|핵심\s*이슈는?)", "", t)
    # 서두 라벨 제거
    t = re.sub(r"^.*?(이슈는|요인은|배경은|원인은|이유는|요약하면|핵심은)\s*[:：]?\s*", "", t)
    # 서술 꼬리(문장) 잘라내기
    m = re.search(r"(요약|습니다|입니다|된다|했다|봅니다|됐다|때문|기인)", t)
    if m:
        t = t[:m.start()]
    t = t.replace(", ", "·").replace(",", "·")
    t = re.sub(r"\s*(으로|등으로|등)\s*$", "", t.strip())
    t = t.strip(" ·.:：")
    if len(t) > 46:
        cut = t[:46]
        t = cut[:cut.rfind("·")].strip(" ·") if "·" in cut else cut.strip()
    return "" if len(t) < 6 else t          # 정크(너무 짧음) 버림


def _is_clean(t):
    return bool(t) and "촉매 미확인" not in t and "특이" not in t


# 줄글 8섹션 → 구조화 항목. 리서치 리포트 문체는 한 번에 읽을 때 피로도가 높다(사용자 지적).
# 결론(한줄·태그·실적결론·밸류한줄)을 먼저 보여주고 근거(촉매·강세론·리스크·관전)는 불릿으로.
SEC_KEYS = ["issue", "한줄", "태그", "실적결론", "실적근거", "촉매",
            "수급분석", "밸류한줄", "강세론", "리스크", "관전"]
LIST_KEYS = {"실적근거", "촉매", "강세론", "리스크", "관전"}      # ' || ' 로 나뉜 항목 목록


def parse_items(v):
    """'제목 :: 설명 || 제목 :: 설명' → [(제목, 설명, 여분…)]. 구분자 없으면 제목만."""
    out = []
    for chunk in (v or "").split("||"):
        parts = [x.strip() for x in chunk.split("::")]
        if parts and parts[0]:
            # LLM 이 붙이는 마크다운 불릿·번호 제거 (- * · 1. ①)
            parts[0] = re.sub(r"^[\-\*·•]+\s*|^\d+[.)]\s*|^[①-⑩]\s*", "", parts[0]).strip()
            if parts[0]:
                out.append(tuple(parts))
    return out


def _gemini_full(c, r, fl, roe, ebitda=None, drank=None):
    """종목 전체 심층 분석 (8섹션). 분기 숫자는 표로 별도 제공 → prose엔 나열 금지."""
    code = r["code"]
    hi = "60일 신고가" if r.get("hi60", 0) >= 0.999 else f"60일 고점대비 {(r['hi60']-1)*100:.0f}%"
    sup = (f"최근5일 수급 외인 {fl.get('frgn5',0):+.0f}억·기관 {fl.get('orgn5',0):+.0f}억·개인 {fl.get('prsn5',0):+.0f}억"
           if fl else "수급 데이터 없음")
    n = r.get("sec_n", 0)
    per_str = (f"PER {r['per']:.0f}(섹터 {int(r['per_rank'])}/{int(n)}위)"
               if (r.get('per', 0) > 0 and not pd.isna(r.get('per_rank')) and n) else "PER 적자/-")
    roe_str = f"·ROE {roe:.1f}%" if roe is not None else ""
    qtrend = ""; lblt = None
    try:
        from value_increment import fetch_fin
        fd = fetch_fin(code, None)
        if fd is not None and len(fd):
            lblt = fd.iloc[-1].get("lblt")
            qtrend = "참고 분기추이: " + " · ".join(
                f"{x['stac_yymm']} ROE{x['roe']:.1f}/EPS{x['eps']:.0f}/매출{x['grs']:.0f}%" for _, x in fd.tail(4).iterrows())
    except Exception:
        pass
    # 부채(부채비율, 동일섹터 내 순위) + 현금흐름(EBITDA·EV/EBITDA) — 밸류 파트 핵심 데이터
    if drank:   # latest_kospi_quality.csv 기반 섹터 내 상대위치 (절대수치 판정 X)
        debt_str = (f"부채비율 {drank['debt']:.0f}%(동일섹터 {drank['n']}개 중 부채 낮은순 "
                    f"{drank['rank']}위·{drank['band']})")
    elif lblt is not None and not pd.isna(lblt):
        debt_str = f"부채비율 {lblt:.0f}%(섹터순위 미산출)"
    else:
        debt_str = "부채비율 미제공"
    # 현금흐름(연간 EBITDA·EV/EBITDA). 금융·보험·증권은 EBITDA 비표준 지표라 생략.
    is_fin = str(r.get("섹터", "")) in {"금융", "증권", "보험", "외국증권"}
    eb = ebitda or {}
    if is_fin:
        cf_str = "현금흐름: 은행·보험·증권업은 EBITDA 비표준 → 생략(자산운용·수익성으로 평가)"
    else:
        cf_parts = []
        if eb.get("ebitda") is not None:
            cf_parts.append(f"최근4분기 EBITDA {eb['ebitda']:,.0f}억")
        if eb.get("ev_ebitda") is not None:
            cf_parts.append(f"EV/EBITDA {eb['ev_ebitda']:.1f}배")
        cf_str = ("현금흐름 " + "·".join(cf_parts)) if cf_parts else "현금흐름(EBITDA) 미제공"
    stat = (f"{r['종목명']}({code}, {r['섹터']}): 20일 {r['ret20']*100:+.0f}%·{hi}, "
            f"{per_str}·PBR {r['pbr']:.1f}{roe_str}, {debt_str}, {cf_str}, {sup}. {qtrend}")
    prompt = (
        "너는 한국 주식 애널리스트다. 최근 뉴스·공시·실적을 광범위하게 검색해 아래 종목의 '심층 분석 리포트'를 작성하라.\n"
        f"데이터: {stat}\n\n"
        "규칙:\n"
        "- 밸류에이션 수치는 위 제공된 PER/PBR/ROE/섹터순위/부채비율/EBITDA만 사용(웹의 다른 수치 절대 인용 금지). 없으면 '미제공'.\n"
        "- 부채비율은 절대수치로 정상/위험을 단정하지 말 것. 반드시 제공된 '동일 섹터 내 부채 순위'로 해석하라 — 같은 업종 피어들 사이에서 부채가 높은 편인지 낮은 편인지가 핵심이다.\n"
        "- ⚠️ **줄글 금지.** 리서치 리포트 문체(서론-전개-결론)로 쓰지 마라. 각 항목은 결론부터, 짧게.\n"
        "- 실제 사실·뉴스·공시·증권사 리포트 근거로만. 모르면 그 항목을 비워라(추측 금지).\n"
        "- 분기 ROE/EPS/매출·수급 금액은 리포트에 표·막대로 따로 보여주므로 반복하지 마라.\n\n"
        "출력 형식 — 아래 라벨만 사용. **목록 항목은 한 줄에 하나씩** 줄바꿈으로 나열하고,\n"
        "항목 안에서 제목과 설명은 ` :: ` 로 구분한다.\n"
        "이슈: <촉매 키워드 3개 ·로 연결, 40자내>\n"
        "한줄: <투자판단 결론부터 2문장, 90자내. '무엇이 핵심 동력이고 무엇이 리스크인지'>\n"
        "태그: <🟢(강점)/🔴(약점)/🟡(주의) 중 하나 + 2~6자 라벨, 4개를 ·로 구분. "
        "예: 🟢 본업 회복 · 🟢 성장동력 · 🔴 높은 부채 · 🟡 밸류 부담>\n"
        "실적결론: <최근 실적을 8자 이내 대비로. 예: 매출 ↓ / 수익성 ↑>\n"
        "실적근거: <항목 3~4개. 각 '지표 :: 방향·수치' 16자내. 예: 주택 GPM :: 하반기 12~15% 전망>\n"
        "촉매: <주가를 끌어올릴 요인 4~5개. 각 '제목(10자내) :: 한 줄 설명(30자내)'. 제목은 명사구>\n"
        "강세론: <상승 시나리오 3개. 각 '핵심(14자내) :: 근거 한 줄(30자내)'>\n"
        "리스크: <하방 리스크 3개. 각 '핵심(14자내) :: 영향 한 줄(30자내)'>\n"
        "관전: <체크포인트 4~5개를 **중요한 순서대로**. 각 줄은 '항목(24자내)'. "
        "시점이 있으면 '항목 :: 시점' (예: 11/04, 2026 하반기). 없으면 시점 생략>\n"
        "밸류한줄: <PER·PBR·ROE·부채를 종합한 **판단만** 한 줄, 30자내. "
        "수치는 바로 위에 이미 표시되므로 절대 반복하지 마라. 예: 수익성 회복 중이나 레버리지·PER 부담>\n"
        "수급분석: <외인/기관/개인 흐름의 해석 2문장. 금액 나열 말고 누가 주도/이탈하는지>\n"
        "추정: <다음 분기 컨센서스 방향을 ▲상향/▼하향/→유지 중 하나로 시작하고 근거 1문장. "
        "못 찾으면 '컨센서스 미확인'>")
    resp = c.models.generate_content(model="gemini-2.5-flash", contents=prompt,
        config={"tools": [{"google_search": {}}], "thinking_config": {"thinking_budget": 0}, "max_output_tokens": 14000})
    d = {k: "" for k in SEC_KEYS}; d["추정"] = ""; cur = None
    for line in resp.text.splitlines():
        s = line.strip()
        m = re.match(r"^\**\s*(이슈|한줄|태그|실적결론|실적근거|촉매|수급분석|밸류한줄|"
                     r"강세론|리스크|관전|추정)\s*[:：]\s*(.*)", s)
        if m:
            cur = "issue" if m.group(1) == "이슈" else m.group(1); d[cur] = m.group(2).strip()
        elif cur and s:
            d[cur] += ("||" if cur in LIST_KEYS else " ") + s
    d["issue"] = _trim_phrase(d["issue"]) if _is_clean(_trim_phrase(d["issue"])) else ""
    for k in SEC_KEYS[1:] + ["추정"]:
        d[k] = d[k].strip().strip("'\"")
    return d


def _gemini_update(c, name, code, prev_summary):
    """재등장 종목: 오늘 새 뉴스 + 다음분기 컨센서스만 가볍게 (토큰 절약). {업데이트, 추정} 반환."""
    prompt = (f"한국 주식 '{name}({code})'의 어제 분석 요약: {prev_summary}\n"
              "오늘 새로 나온 뉴스·공시·증권사 리포트를 검색해서 아래 2줄만 출력:\n"
              "업데이트: <어제와 겹치지 않는 새 변화 1~2문장. 서두 없이 사실 바로. 새 게 없으면 '오늘 특이 변화 없음'>\n"
              "추정: <다음 분기 매출·영업이익 컨센서스 방향을 ▲상향/▼하향/→유지 중 하나로 시작하고 근거 1문장. 못 찾으면 '컨센서스 미확인'>")
    try:
        resp = c.models.generate_content(model="gemini-2.5-flash", contents=prompt,
            config={"tools": [{"google_search": {}}], "thinking_config": {"thinking_budget": 0}, "max_output_tokens": 2500})
        upd, est, cur = "", "", None
        for line in resp.text.splitlines():
            s = line.strip()
            mm = re.match(r"^\**\s*(업데이트|추정)\s*[:：]\s*(.*)", s)
            if mm:
                cur = mm.group(1)
                if cur == "업데이트":
                    upd = mm.group(2).strip()
                else:
                    est = mm.group(2).strip()
            elif cur == "업데이트" and s:
                upd += " " + s
            elif cur == "추정" and s:
                est += " " + s
        return {"업데이트": upd.strip().strip("'\"")[:400], "추정": est.strip().strip("'\"")[:200]}
    except Exception:
        return {"업데이트": "", "추정": ""}


def load_debt_ranks():
    """latest_kospi_quality.csv(전종목 부채비율+섹터, 매일갱신) → code별 섹터내 부채 순위.
    반환 {code: {debt, rank(1=섹터최저부채), n, band(저부채/중간/고부채 그룹)}}. 절대수치 판정 없이 동종 상대위치."""
    try:
        d = pd.read_csv(os.path.join(_DIR, "latest_kospi_quality.csv"), dtype={"종목코드": str})
    except Exception:
        return {}
    if "부채비율(%)" not in d.columns or "섹터" not in d.columns:
        return {}
    d = d.dropna(subset=["부채비율(%)", "섹터"]).copy()
    d["종목코드"] = d["종목코드"].str.zfill(6)
    out = {}
    for sec, g in d.groupby("섹터"):
        nn = len(g)
        if nn < 4:
            continue
        g = g.assign(rk=g["부채비율(%)"].rank(method="min"))  # 1=섹터 내 최저 부채
        for _, x in g.iterrows():
            pos = x["rk"] / nn
            band = "저부채 그룹" if pos <= 1/3 else ("고부채 그룹" if pos > 2/3 else "중간")
            out[x["종목코드"]] = {"debt": float(x["부채비율(%)"]), "rank": int(x["rk"]), "n": nn, "band": band}
    return out


# 프로즈 전체 재분석 주기(일). 만료가 실제로 작동하게 고친 뒤(2026-08-25) 사용자가 5일로 결정 —
# 종목당 주 1.4회 전체분석. 비용이 부담되면 늘리면 된다.
ANALYSIS_TTL_DAYS = int(os.environ.get("ANALYSIS_TTL_DAYS", "5"))


def gemini_analyze(top10, flows, roes, cache, ebitdas=None, dranks=None):
    """캐시 인식: 재등장(TTL 내) 종목은 어제 8섹션 재사용 + 오늘 업데이트만 호출. 신규/묵은건 전체분석.

    신선도는 'date'(=마지막 실행일) 가 아니라 **'full'(=마지막 전체분석일)** 로 판정한다.
    date 는 매 실행마다 오늘로 갱신되므로 그걸로 재면 days<=7 이 영원히 참이 되어
    프로즈가 한 번 쓰이면 절대 재생성되지 않았다 — 수급분석이 13일째 그대로였던 원인(2026-08-25).
    """
    from google import genai
    ebitdas = ebitdas or {}; dranks = dranks or {}
    c = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    today = datetime.now(KST); out = {}
    for _, r in top10.iterrows():
        code = r["code"]; fl = flows.get(code) or {}; roe = roes.get(code)
        cached = cache.get(code)
        fresh = False
        base = (cached or {}).get("full") or (cached or {}).get("date")   # full 없는 옛 캐시는 date 로 1회 판정
        if cached and base:
            try:
                fresh = (today - datetime.strptime(base, "%Y-%m-%d").replace(tzinfo=KST)).days <= ANALYSIS_TTL_DAYS
            except Exception:
                fresh = False
        if not cached or not cached.get("한줄"):
            fresh = False          # 줄글 시절 캐시는 새 구조가 없다 → 재분석해서 채운다
        try:
            if fresh:
                a = {k: cached.get(k, "") for k in SEC_KEYS}        # 어제 8섹션 재사용
                a["full"] = base                                    # 전체분석 시점은 그대로 물려받는다
                u = _gemini_update(c, r["종목명"], code, cached.get("한줄", ""))
                a["업데이트"], a["추정"] = u["업데이트"], u["추정"]
                log(f"  {r['종목명']}: 캐시 재사용 + 업데이트")
            else:
                a = _gemini_full(c, r, fl, roe, ebitdas.get(code), dranks.get(code)); a["업데이트"] = ""
                a["full"] = today.strftime("%Y-%m-%d")
                log(f"  {r['종목명']}: 전체 분석")
        except Exception as e:
            log(f"  Gemini {code} 실패: {str(e)[:80]}")
            a = (cached or {k: "" for k in SEC_KEYS}); a.setdefault("업데이트", "")
        a["date"] = today.strftime("%Y-%m-%d")
        a.setdefault("full", a["date"])
        out[code] = a; cache[code] = a
    return out


_CUTPCT = int(round(MOM_CUT * 100))
DISCLAIMER = (f"📊 선정: 거래대금 100억↑  →  모멘텀 상위 {_CUTPCT}%  →  저변동성 10개 (과열 꼭지 제거)\n"
              + ("📈 백테스트(2023~26): 상승추세장 우위·비추세장 약 (롱온리·생존편향 — 절대수익 과대)\n"
                 "🎯 제안 청산: +15% 익절 / −10% 손절 · 추세 이탈(200·120일선 아래) 시 전량 현금·신규진입 중단\n"
                 if MOM_GATE else
                 "📈 성격(2023~26): 고변동 발굴형 — 60일내 고점 +50% 19%·+100% 6%(게이트판 2배), 단 −30% 폭락도 18%(게이트판 3배)\n"
                 "🎯 제안: 급등 후보 스캐너로 활용 · 평균은 폭락에 상쇄돼 기계적보유 부적합 · 손절 −10% 타이트하게\n")
              + "ℹ️ 수급·재무 미반영 · 종목 '이슈'는 AI 검색 추정(확정 아님) · 투자판단 보조용")

METHOD = (
    "🧮 이 리포트는 어떻게 만들어지나 — 3단계 선정 + 모멘텀 점수\n\n"
    "[1단계] 거래대금 필터\n"
    "최근 5거래일 평균 거래대금이 100억원 미만인 종목은 제외합니다. 거래가 적어 소수 세력에 휘둘리거나 사고팔기 어려운 잡주를 1차로 걸러내는 관문입니다.\n\n"
    f"[2단계] 모멘텀 점수 상위 {_CUTPCT}%\n"
    f"1단계를 통과한 종목을 아래 '모멘텀 점수'로 줄세워 상위 {_CUTPCT}%(주도주군)만 남깁니다.\n"
    "　모멘텀 점수 = ① 60일 고점 근접도 + ② 20일 이동평균 이격도 + ③ 5일 수익률\n"
    "　· ① 현재가가 최근 60일 최고가에 얼마나 가까운가 (1.0이면 60일 신고가)\n"
    "　· ② 현재가가 20일 이동평균선보다 얼마나 위에 있는가\n"
    "　· ③ 최근 5거래일 상승률\n"
    "　세 지표를 그날 전 종목과 비교해 표준화(Z-score)한 뒤 합산합니다. 단순 20일 수익률이 아니라 '추세의 강도와 지속성'을 측정합니다.\n\n"
    "[3단계] 저변동성 10개 압축\n"
    "2단계 주도주군 중에서 '20일 변동성(일간 수익률의 표준편차 = 매일 얼마나 출렁이는지)'이 낮은 순으로 10개만 추립니다. 변동성이 낮다는 것은 급등락 없이 꾸준히·완만하게 오른 종목이라는 뜻입니다. 같은 강세라도 급등락이 심한 과열 종목은 반전(폭락) 위험이 크기 때문에, 덜 흔들리며 추세를 유지한 '건강한 주도주'를 고르는 단계입니다. (백테스트상 가장 뜨겁게 급등한 종목을 사는 것보다 이 방식이 더 좋았습니다.)\n\n"
    "🔁 종목이 며칠간 비슷한 이유: 모멘텀은 지속성이 있어 한 번 주도주가 되면 며칠씩 유지되는 게 정상입니다. 가격·점수·순위·수급은 매일 새로 갱신되며, 종목별 '전일 대비 변화'를 함께 표기합니다.")


def _rel(pct, n):
    """섹터 내 백분위 → 라벨. pct 낮음=쌈. 표본<4면 빈값."""
    if pd.isna(pct) or n < 4:
        return ""
    return "저평가" if pct <= 0.33 else ("고평가" if pct >= 0.67 else "평균")


def _t(txt, bold=False, color=None):
    a = {"bold": bold}
    if color:
        a["color"] = color
    return [{"type": "text", "text": {"content": str(txt)}, "annotations": a}]


def _bullet(rich):
    return {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": rich}}


def _metric_line(r, roe, eb, drank):
    """핵심 지표 한 줄 — 숫자는 전부 우리가 가진 값이라 AI 를 거치지 않는다(환각 차단)."""
    p = []
    if roe is not None:
        p.append(f"ROE {roe:.1f}%")
    if r.get("pbr", 0) and r["pbr"] > 0:
        p.append(f"PBR {r['pbr']:.2f}배")
    if r.get("per", 0) and r["per"] > 0:
        n = int(r.get("sec_n") or 0)
        rk = r.get("per_rank")
        p.append(f"PER {r['per']:.0f}배" + (f"({int(rk)}/{n}위)" if n >= 4 and pd.notna(rk) else ""))
    if drank:
        p.append(f"부채 {drank['debt']:.0f}%({drank['rank']}/{drank['n']}위·{drank['band']})")
    if (eb or {}).get("ev_ebitda") is not None:
        p.append(f"EV/EBITDA {eb['ev_ebitda']:.1f}배")
    return " · ".join(p)


def _sections(a, r=None, roe=None, eb=None, drank=None):
    """결론 → 근거 순서로 블록을 쌓는다. 없는 항목은 통째로 건너뛴다."""
    out = []
    metric = _metric_line(r, roe, eb, drank) if r is not None else ""
    if metric or a.get("밸류한줄"):
        rich = _t("💵 핵심 지표\n", True)
        if metric:
            rich += _t(metric + ("\n" if a.get("밸류한줄") else ""))
        if a.get("밸류한줄"):
            rich += _t("→ " + a["밸류한줄"], True, "blue")
        out.append({"object": "block", "type": "callout", "callout": {
            "icon": {"type": "emoji", "emoji": "💵"}, "color": "gray_background", "rich_text": rich}})

    if a.get("실적결론") or a.get("실적근거"):
        out.append(_para(_t("📊 사업·실적  ", True) + _t(a.get("실적결론", ""), True, "orange")))
        for it in parse_items(a.get("실적근거")):
            out.append(_bullet(_t(it[0] + ("  " if len(it) > 1 else ""), False, "gray")
                               + (_t(it[1]) if len(it) > 1 else [])))

    if a.get("촉매"):
        out.append(_para(_t("⚡ 상승 촉매", True)))
        for i, it in enumerate(parse_items(a["촉매"])[:5]):
            out.append(_bullet(_t("①②③④⑤"[i] + " " + it[0], True)
                               + (_t("  " + it[1], False, "gray") if len(it) > 1 else [])))

    for key, head, mark, col in (("강세론", "📈 강세론", "🟢", "green"),
                                 ("리스크", "⚠️ 리스크", "🔴", "red")):
        if a.get(key):
            out.append(_para(_t(head, True)))
            for it in parse_items(a[key])[:4]:
                out.append(_bullet(_t(f"{mark} {it[0]}", True, col)
                                   + (_t("  " + it[1], False, "gray") if len(it) > 1 else [])))

    if a.get("관전"):
        out.append(_para(_t("👀 관전 포인트", True)))
        for i, it in enumerate(parse_items(a["관전"])[:5]):
            w = 3 if i < 2 else (2 if i == 2 else 1)      # Gemini 가 중요한 순서로 냈다는 전제
            # 숫자가 있다고 다 날짜가 아니다("12~15% 달성 여부") → 시점 꼴일 때만 📅
            tail = it[1] if len(it) > 1 else ""
            when = tail if re.search(r"\d{1,2}\s*[/월]\s*\d{0,2}|\d{4}\s*년?|상반기|하반기|[1-4]Q|분기", tail) else ""
            rich = _t("🔥" * w + " ") + _t(it[0], True)
            if when:
                rich += _t(f"   📅 {when}", True, "orange")
            elif tail:
                rich += _t(f"  {tail}", False, "gray")
            out.append(_bullet(rich))

    if a.get("수급분석"):
        out.append(_para(_t("🔁 수급 해석  ", True) + _t(_strip_won(a["수급분석"]), False, "gray")))
    return out


def _para(rich):
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich}}


def _delta_line(dl):
    """전일 대비 변화 한 줄."""
    if not dl:
        return ""
    if dl.get("new"):
        return "📌 전일대비: 신규 진입 (어제 10위권 밖)"
    parts = []
    rp = dl.get("rank_prev")
    parts.append(f"순위 {rp}위 유지" if rp == dl.get("_rank") else f"어제 {rp}위")
    if dl.get("price_chg") is not None:
        parts.append(f"주가 {dl['price_chg']*100:+.1f}%")
    if dl.get("score_prev") is not None and dl.get("_score") is not None:
        parts.append(f"점수 {dl['score_prev']:.1f}→{dl['_score']:.1f}")
    return "📌 전일대비: " + " · ".join(parts)


def _strip_won(txt):
    """수급분석 prose의 금액(억/조/만/원) 표기 제거 — 정확한 수치는 막대가 담당.
    Gemini가 google_search로 다른 기간 순매수액을 지어내 막대와 불일치하던 문제 차단(2026-07-13)."""
    if not txt:
        return txt
    txt = re.sub(r"\s*약?\s*[\d,]+\s*(?:조|억|만)+\s*원?(?:어치)?\s*"
                 r"(?:을|를|의|가|이|은|는|씩|만큼|가량|이상|이하)?", " ", txt)
    return re.sub(r"\s{2,}", " ", txt).strip()


def _stock_toggle(rank, r, a, fl, roe, dl=None, ebitda=None, drank=None):
    """종목 1개 = 접이식 토글. 제목줄=요약지표, 펼치면 결론(태그·한줄·핵심지표) → 근거(불릿)."""
    icon = {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, "📈")
    sec = r["섹터"] if pd.notna(r["섹터"]) else "-"
    secname = sec if sec != "-" else "업종"
    n = int(r.get("sec_n", 0))
    per_s = f"{r['per']:.0f}" if r.get('per', 0) and r['per'] > 0 else "—"
    pbr_s = f"{r['pbr']:.1f}" if r.get('pbr', 0) and r['pbr'] > 0 else "—"
    pl = _rel(r.get("per_pct"), n); bl = _rel(r.get("pbr_pct"), n)
    per_rk = f" ({secname} {int(r['per_rank'])}/{n}위·{pl})" if (pl and not pd.isna(r.get('per_rank'))) else ""
    pbr_rk = f" ({int(r['pbr_rank'])}/{n}위·{bl})" if (bl and not pd.isna(r.get('pbr_rank'))) else ""
    roe_s = f" · ROE {roe:.1f}%" if roe is not None else ""
    hi = "60일 신고가" if r.get("hi60", 0) >= 0.999 else f"60일 고점대비 {(r['hi60']-1)*100:.0f}%"

    def gray(t): return {"type": "text", "text": {"content": t}, "annotations": {"color": "gray"}}
    chg = r.get("chg", 0) or 0
    chg_color = "red" if chg > 0 else ("blue" if chg < 0 else "gray")   # 상승 빨강·하락 파랑(국내 관습)
    title_rich = [
        {"type": "text", "text": {"content": f"{icon} {rank}. {r['종목명']} "}, "annotations": {"bold": True}},
        {"type": "text", "text": {"content": f"{chg*100:+.1f}% "}, "annotations": {"bold": True, "color": chg_color}},
        gray(f"({r['code']}) · {sec}  |  모멘텀 {r['score']:.1f} · 20일 {r['ret20']*100:+.1f}% · {hi} · {int(r['price']):,}원")]

    kids = []
    # 결론 먼저 — 태그 한 줄과 투자판단 한 줄이 3초 안에 방향을 잡아준다(줄글 피로도 대응)
    if a.get("태그"):
        kids.append(_para(_t("   ".join(x.strip() for x in a["태그"].split("·") if x.strip()), True)))
    if a.get("한줄"):
        kids.append(_para(_t(a["한줄"])))
    # 전일 대비 변화 (별도 강조 줄)
    if dl:
        dl = {**dl, "_rank": rank, "_score": r["score"]}
        dtxt = _delta_line(dl)
        if dtxt:
            kids.append(_para([{"type": "text", "text": {"content": dtxt},
                                "annotations": {"bold": True, "color": "blue"}}]))
    # 오늘 업데이트 (재등장 종목)
    if a.get("업데이트"):
        kids.append(_para([
            {"type": "text", "text": {"content": "🆕 오늘 업데이트  "}, "annotations": {"bold": True, "color": "green"}},
            {"type": "text", "text": {"content": a["업데이트"]}}]))
    # 수급 5일 막대 (시각화)
    sb = _supply_bars(fl)
    if sb:
        kids.append(sb)
    # 밸류 + 이슈
    dline = f"🏢 PER {per_s}{per_rk} · PBR {pbr_s}{pbr_rk}{roe_s}"
    if a.get("issue"):
        dline += f"\n📰 이슈 — {a['issue']}"
    kids.append(_para([gray(dline)]))
    kids += _sections(a, r, roe, ebitda, drank)
    return {"object": "block", "type": "toggle", "toggle": {
        "rich_text": title_rich,
        "color": "blue_background" if rank <= 3 else "default",
        "children": kids}}


def _supply_bars(fl):
    """수급 5일 순매수 막대 (외인/기관/개인). 순매수=초록 / 순매도=빨강."""
    if not fl:
        return None
    items = [("외국인", fl.get("frgn5", 0)), ("기관 ", fl.get("orgn5", 0)), ("개인 ", fl.get("prsn5", 0))]
    mx = max((abs(v) for _, v in items), default=0) or 1
    rich = [{"type": "text", "text": {"content": "💰 수급 5일 순매수(억)\n"}, "annotations": {"bold": True}}]
    for name, v in items:
        nbar = max(1, round(abs(v) / mx * 12))
        rich.append({"type": "text", "text": {"content": f"{name}  "}, "annotations": {"color": "gray"}})
        rich.append({"type": "text", "text": {"content": "▇" * nbar},
                     "annotations": {"color": ("green" if v >= 0 else "red")}})
        rich.append({"type": "text", "text": {"content": f"  {v:+,.0f}\n"}, "annotations": {"color": "gray"}})
    return _para(rich)


def _quarter_table(income):
    """분기 실적 표 (분기/매출억/영업이익억). income=단일분기 리스트. 없으면 None."""
    if not income:
        return None
    cell = lambda t: [{"type": "text", "text": {"content": str(t)}}]
    def q(yymm): return f"{yymm[2:4]}.{yymm[4:6]}"   # 202603 → 26.03
    rows = [{"type": "table_row", "table_row": {"cells": [cell(c) for c in ["분기", "매출(억)", "영업이익(억)"]]}}]
    for x in income:
        rows.append({"type": "table_row", "table_row": {"cells": [
            cell(q(x["q"])), cell(f"{x['sale']:,.0f}"), cell(f"{x['op']:,.0f}")]}})
    return {"object": "block", "type": "table", "table": {
        "table_width": 3, "has_column_header": True, "has_row_header": False, "children": rows}}


def upload_notion(top, analysis=None, trend=None, flows=None, roes=None, deltas=None, newly=None, dropped=None, incomes=None, asof=None, cash=False, ebitdas=None, dranks=None):
    """Notion 업로드: 헤더 생성 → 종목별 토글 append → 토글 안에 분기표·추정 append."""
    analysis = analysis or {}; flows = flows or {}; roes = roes or {}; deltas = deltas or {}
    incomes = incomes or {}; ebitdas = ebitdas or {}; dranks = dranks or {}
    import 수급 as sg
    headers = {"Authorization": f"Bearer {os.environ['NOTION_API_KEY']}",
               "Content-Type": "application/json", "Notion-Version": "2022-06-28"}
    cal_today = datetime.now(KST).strftime("%Y-%m-%d")
    asof = asof or cal_today
    parent = os.environ.get("NOTION_PARENT_PAGE_ID", "3324a00632f880fbb014d766d87a1079")
    title = f"🚀 {asof} KOSPI 30일 모멘텀 추천{MOM_LABEL}"

    header = []
    if asof != cal_today:
        header.append({"object": "block", "type": "callout", "callout": {
            "rich_text": [{"type": "text", "text": {"content": f"데이터 기준일: {asof} (장 마감 전 실행 — 최신 거래일 종가 기준)"},
                           "annotations": {"bold": True}}],
            "icon": {"type": "emoji", "emoji": "⚠️"}, "color": "orange_background"}})
    if trend:
        header.append({"object": "block", "type": "callout", "callout": {
            "rich_text": [{"type": "text", "text": {"content": f"KOSPI 추세: {trend['text']}"},
                           "annotations": {"bold": True}}],
            "icon": {"type": "emoji", "emoji": trend["emoji"]}, "color": trend["color"]}})
    header += [
        {"object": "block", "type": "callout", "callout": {
            "rich_text": [{"type": "text", "text": {"content": DISCLAIMER}}],
            "icon": {"type": "emoji", "emoji": "📐"}, "color": "yellow_background"}},
        {"object": "block", "type": "toggle", "toggle": {
            "rich_text": [{"type": "text", "text": {"content": "🧮 산출 방법 — 펼쳐 보기 (3단계 선정 + 모멘텀 점수)"},
                           "annotations": {"bold": True, "color": "gray"}}],
            "children": [{"object": "block", "type": "paragraph", "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": METHOD}, "annotations": {"color": "gray"}}]}}]}},
    ]
    if cash:     # 게이트 발동: 추세 이탈 → 추천 비우고 현금 메시지
        header.append({"object": "block", "type": "callout", "callout": {
            "rich_text": [{"type": "text", "text": {"content":
                "🛑 추세 이탈 — 신규 진입 중단·전량 현금 권장. KOSPI 지수가 200·120일선 아래로 내려와 "
                "비강세 구간입니다. 이 모멘텀 전략은 백테스트상 상승추세에서만 수익이 났고 비추세장에선 약했습니다. "
                "추세가 200·120일선 위로 회복될 때까지 신규 매수를 멈추고 현금 비중을 높이세요. (오늘 추천 종목 없음)"},
                "annotations": {"bold": True}}],
            "icon": {"type": "emoji", "emoji": "🛑"}, "color": "red_background"}})
    else:
        if newly or dropped:
            chg = []
            if newly:
                chg.append(f"🆕 신규 진입: {', '.join(newly)}")
            if dropped:
                chg.append(f"📉 이탈: {', '.join(dropped)}")
            header.append({"object": "block", "type": "callout", "callout": {
                "rich_text": [{"type": "text", "text": {"content": "어제 대비  " + "   ·   ".join(chg)}}],
                "icon": {"type": "emoji", "emoji": "📌"}, "color": "blue_background"}})
        header.append({"object": "block", "type": "heading_3",
                       "heading_3": {"rich_text": [{"type": "text", "text": {"content": "🏆 상위 10 — 종목을 펼치면 상세 분석"}}]}})

    def _item(rank, r):
        """종목 1개의 (토글, 2차블록) — 페이지 모드·대시보드 모드 공통. 2차블록은 table 등 3단계 불가분."""
        tog = _stock_toggle(rank, r, analysis.get(r["code"], {}), flows.get(r["code"]) or {},
                            roes.get(r["code"]), deltas.get(r["code"]),
                            ebitdas.get(r["code"]), dranks.get(r["code"]))
        a = analysis.get(r["code"], {})
        extra = []
        qt = _quarter_table(incomes.get(r["code"]))
        if qt:
            extra.append({"object": "block", "type": "paragraph", "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": "📊 분기 실적 추이 (단일분기)"},
                               "annotations": {"bold": True}}]}})
            extra.append(qt)
        if a.get("추정"):
            extra.append({"object": "block", "type": "paragraph", "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": "📈 다음 분기 컨센서스  "},
                               "annotations": {"bold": True}},
                              {"type": "text", "text": {"content": a["추정"]}}]}})
        return tog, extra

    if MOM_TARGET == "dashboard":
        # 통합 대시보드에 토글 1개로 붙인다 (날짜별 페이지 생성 안 함)
        import dashboard
        items = [] if cash else [_item(rank, r) for rank, (_, r) in
                                 enumerate(top.head(10).iterrows(), 1)]
        tid = dashboard.add_report(title, header, items)
        log(f"✅ 대시보드 리포트 추가{'(현금/추세이탈)' if cash else ''}: {dashboard.url()}"
            if tid else "❌ 대시보드 리포트 추가 실패")
        return

    date_parent = sg._get_or_create_date_page(asof, headers, parent)
    sg._archive_same_title_pages(title, headers, date_parent)
    r = None
    for attempt in range(3):     # 페이지 생성도 일시 timeout 재시도
        try:
            r = requests.post("https://api.notion.com/v1/pages", headers=headers, timeout=30,
                              json={"parent": {"page_id": date_parent},
                                    "properties": {"title": {"title": [{"text": {"content": title}}]}},
                                    "children": header})
            if r.status_code == 200:
                break
            log(f"  ⚠️ 페이지 생성 {r.status_code} ({attempt+1}/3): {r.text[:150]}")
        except Exception as e:
            log(f"  ⚠️ 페이지 생성 예외({attempt+1}/3): {str(e)[:120]}")
        time.sleep(2 * (attempt + 1))
    if r is None or r.status_code != 200:
        log("❌ Notion 페이지 생성 최종 실패"); return
    page_id = r.json()["id"]
    page_url = r.json().get("url", "")
    if cash:     # 현금 모드: 종목 토글 없이 종료
        log(f"✅ Notion 업로드 완료(현금/추세이탈): {page_url}")
        return

    def append(block_id, blocks):
        # Notion API 일시 지연(ReadTimeout)·5xx·429 재시도 — 1종목 실패가 리포트 전체 중단 막음
        for attempt in range(3):
            try:
                rr = requests.patch(f"https://api.notion.com/v1/blocks/{block_id}/children",
                                    headers=headers, json={"children": blocks}, timeout=30)
                time.sleep(0.35)
                if rr.status_code == 200:
                    return rr.json().get("results", [])
                log(f"  ⚠️ append {rr.status_code} ({attempt+1}/3): {rr.text[:120]}")
                if rr.status_code < 500 and rr.status_code != 429:
                    return None      # 4xx(429제외)는 재시도 무의미
            except Exception as e:
                log(f"  ⚠️ append 예외({attempt+1}/3): {str(e)[:120]}")
            time.sleep(2 * (attempt + 1))
        return None

    # 종목별: 토글 append → 그 토글 안에 분기 실적 표 별도 append (중첩 한도 회피)
    for rank, (_, r) in enumerate(top.head(10).iterrows(), 1):
        tog, extra = _item(rank, r)
        res = append(page_id, [tog])
        if not res:
            continue
        if extra:
            append(res[0]["id"], extra)

    log(f"✅ Notion 업로드 완료: {page_url}")


if __name__ == "__main__":
    main()
