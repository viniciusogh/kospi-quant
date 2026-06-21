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
    """FHKST66430500 기타주요비율 → 최신 EBITDA(억)·EV/EBITDA(0이면 무효). 1콜. 캐시."""
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
        r0 = o[0]
        def _f(k):
            try:
                return float(r0.get(k))
            except (TypeError, ValueError):
                return None
        eveb = _f("ev_ebitda")
        res = {"q": r0.get("stac_yymm"), "ebitda": _f("ebitda"),
               "ev_ebitda": eveb if (eveb and eveb > 0) else None}
    os.makedirs(EBITDA_CACHE, exist_ok=True); pd.to_pickle(res, cache)
    return res


def roe_latest(code, tok):
    """최근 분기 ROE (FHKST66430300 캐시 재활용)."""
    try:
        from value_increment import fetch_fin
        fd = fetch_fin(code, tok)
        if fd is not None and len(fd):
            return float(fd.iloc[-1].get("roe"))
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
    analysis = {} if cash_mode else (gemini_analyze(top10, flows, roes, cache, ebitdas) if os.environ.get("GEMINI_API_KEY") else {})
    if analysis:                                  # 캐시 저장 (등장한 종목만 유지 + 오래된건 정리)
        json.dump({k: v for k, v in cache.items()}, open(CACHE_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    if os.environ.get("NOTION_API_KEY"):
        upload_notion(top10, analysis, trend, flows, roes, deltas, newly, dropped, incomes, today_str, cash=cash_mode)
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


SEC_KEYS = ["issue", "요약", "사업실적", "촉매", "수급분석", "밸류", "강세론", "리스크", "관전"]


def _gemini_full(c, r, fl, roe, ebitda=None):
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
    # 부채(부채비율) + 현금흐름(EBITDA·EV/EBITDA) — 밸류 파트 핵심 데이터
    debt_str = f"부채비율 {lblt:.0f}%" if (lblt is not None and not pd.isna(lblt)) else "부채비율 미제공"
    eb = ebitda or {}
    cf_parts = []
    if eb.get("ebitda") is not None:
        cf_parts.append(f"EBITDA {eb['ebitda']:,.0f}억")
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
        "- ⚠️ 부채비율 해석은 섹터 맥락 필수: 은행·보험·증권·지주 등 금융업은 예수금·보험준비금이 회계상 부채라 부채비율이 구조적으로 수백~수천%가 정상이다. 금융주는 부채비율 절대수치로 위험을 단정하지 말고 '업종 특성상 정상' 또는 자본적정성 관점으로 서술하라. 비금융 제조·서비스업만 부채비율 100~200% 초과를 레버리지 부담으로 해석.\n"
        "- 문체: 자연스럽게 풀어 쓴 완결 문장. 개조식·전보문·가운뎃점 나열 금지.\n"
        "- ⚠️ 분기별 ROE/EPS/매출 수치와 수급 금액(억)은 리포트에 표·막대로 따로 보여주므로 prose에 숫자를 나열하지 말 것. 그 수치들의 '의미·방향·해석'만 서술하라.\n"
        "- 각 섹션 4~7문장, 실제 사실·뉴스·공시·증권사 리포트 근거로. 모르면 '확인 안 됨'.\n"
        "아래 라벨 형식으로만 출력:\n"
        "이슈: <촉매 키워드 3개 ·로 연결, 40자내>\n"
        "요약: <핵심 투자포인트 thesis 2~3문장>\n"
        "사업실적: <사업 개요 + 최근 실적의 의미·방향 해석(숫자 나열 금지) 4~6문장>\n"
        "촉매: <주가를 끌어올린 요인 — 테마·정책·공시·뉴스를 시간순으로 4~6문장>\n"
        "수급분석: <외인/기관/개인 흐름의 해석(금액 나열 말고 누가 주도/이탈하며 의미가 뭔지) 2~4문장>\n"
        "밸류: <재무 건전성·현금흐름 중심으로 서술. ① 부채비율로 재무 레버리지/안정성(금융주는 섹터 특성 반영) ② EBITDA로 영업현금 창출 규모·추세, EV/EBITDA 있으면 현금흐름 대비 밸류 ③ ROE로 수익성. PER/PBR은 보조로 한 줄. 피어/업종 대비 적정성까지 4~6문장>\n"
        "강세론: <상승 지속 시나리오와 근거 3~4문장>\n"
        "리스크: <구체적 하방 리스크와 영향 3~5문장>\n"
        "관전: <향후 트리거·실적발표일·정책·지표 체크포인트 3~5문장>\n"
        "추정: <다음 분기 매출·영업이익 증권사 컨센서스 방향을 ▲상향/▼하향/→유지 중 하나로 시작하고 근거 1문장. 컨센서스 못 찾으면 '컨센서스 미확인'>")
    resp = c.models.generate_content(model="gemini-2.5-flash", contents=prompt,
        config={"tools": [{"google_search": {}}], "thinking_config": {"thinking_budget": 0}, "max_output_tokens": 14000})
    d = {k: "" for k in SEC_KEYS}; d["추정"] = ""; cur = None
    for line in resp.text.splitlines():
        s = line.strip()
        m = re.match(r"^\**\s*(이슈|요약|사업실적|촉매|수급분석|밸류|강세론|리스크|관전|추정)\s*[:：]\s*(.*)", s)
        if m:
            cur = "issue" if m.group(1) == "이슈" else m.group(1); d[cur] = m.group(2).strip()
        elif cur and s:
            d[cur] += " " + s
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


def gemini_analyze(top10, flows, roes, cache, ebitdas=None):
    """캐시 인식: 재등장(7일내) 종목은 어제 8섹션 재사용 + 오늘 업데이트만 호출. 신규/묵은건 전체분석."""
    from google import genai
    ebitdas = ebitdas or {}
    c = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    today = datetime.now(KST); out = {}
    for _, r in top10.iterrows():
        code = r["code"]; fl = flows.get(code) or {}; roe = roes.get(code)
        cached = cache.get(code)
        fresh = False
        if cached and cached.get("date"):
            try:
                fresh = (today - datetime.strptime(cached["date"], "%Y-%m-%d").replace(tzinfo=KST)).days <= 7
            except Exception:
                fresh = False
        try:
            if fresh:
                a = {k: cached.get(k, "") for k in SEC_KEYS}        # 어제 8섹션 재사용
                u = _gemini_update(c, r["종목명"], code, cached.get("요약", ""))
                a["업데이트"], a["추정"] = u["업데이트"], u["추정"]
                log(f"  {r['종목명']}: 캐시 재사용 + 업데이트")
            else:
                a = _gemini_full(c, r, fl, roe, ebitdas.get(code)); a["업데이트"] = ""
                log(f"  {r['종목명']}: 전체 분석")
        except Exception as e:
            log(f"  Gemini {code} 실패: {str(e)[:80]}")
            a = (cached or {k: "" for k in SEC_KEYS}); a.setdefault("업데이트", "")
        a["date"] = today.strftime("%Y-%m-%d")
        out[code] = a; cache[code] = a
    return out


_CUTPCT = int(round(MOM_CUT * 100))
DISCLAIMER = (f"📊 선정: 거래대금 100억↑  →  모멘텀 상위 {_CUTPCT}%  →  저변동성 10개 (과열 꼭지 제거)\n"
              + ("📈 백테스트(2023~26): 상승추세장 우위·비추세장 약 (롱온리·생존편향 — 절대수익 과대)\n"
                 "🎯 제안 청산: +20% 익절 / −10% 손절 · 추세 이탈(200·120일선 아래) 시 전량 현금·신규진입 중단\n"
                 if MOM_GATE else
                 "📈 백테스트(2023~26): 건당 +1.3% / 30일 · 승률 42% · 7개 반기 중 5개 +  (하락장 약·롱온리)\n"
                 "🎯 제안 청산: +20% 익절 / −10% 손절\n")
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


SECTIONS = [("💡 요약", "요약"), ("📊 사업·실적", "사업실적"), ("⚡ 상승 촉매", "촉매"),
            ("🔁 수급 분석", "수급분석"), ("💵 밸류에이션", "밸류"), ("📈 강세론", "강세론"),
            ("⚠️ 리스크", "리스크"), ("👀 관전 포인트", "관전")]


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


def _stock_toggle(rank, r, a, fl, roe, dl=None):
    """종목 1개 = 접이식 토글. 제목줄=요약지표, 펼치면 8개 섹션 문단."""
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
    # 8개 섹션
    for label, key in SECTIONS:
        v = (a.get(key) or "").strip()
        if v:
            kids.append(_para([
                {"type": "text", "text": {"content": f"{label}\n"}, "annotations": {"bold": True}},
                {"type": "text", "text": {"content": v[:1900]}}]))
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


def upload_notion(top, analysis=None, trend=None, flows=None, roes=None, deltas=None, newly=None, dropped=None, incomes=None, asof=None, cash=False):
    """Notion 업로드: 헤더 생성 → 종목별 토글 append → 토글 안에 분기표·추정 append."""
    analysis = analysis or {}; flows = flows or {}; roes = roes or {}; deltas = deltas or {}; incomes = incomes or {}
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

    date_parent = sg._get_or_create_date_page(asof, headers, parent)
    sg._archive_same_title_pages(title, headers, date_parent)
    r = requests.post("https://api.notion.com/v1/pages", headers=headers, timeout=20,
                      json={"parent": {"page_id": date_parent},
                            "properties": {"title": {"title": [{"text": {"content": title}}]}},
                            "children": header})
    if r.status_code != 200:
        log(f"❌ Notion 페이지 생성 실패 {r.status_code}: {r.text[:200]}"); return
    page_id = r.json()["id"]
    page_url = r.json().get("url", "")
    if cash:     # 현금 모드: 종목 토글 없이 종료
        log(f"✅ Notion 업로드 완료(현금/추세이탈): {page_url}")
        return

    def append(block_id, blocks):
        rr = requests.patch(f"https://api.notion.com/v1/blocks/{block_id}/children",
                            headers=headers, json={"children": blocks}, timeout=20)
        time.sleep(0.35)
        if rr.status_code != 200:
            log(f"  ⚠️ append 실패 {rr.status_code}: {rr.text[:150]}"); return None
        return rr.json().get("results", [])

    # 종목별: 토글 append → 그 토글 안에 분기 실적 표 별도 append (중첩 한도 회피)
    for rank, (_, r) in enumerate(top.head(10).iterrows(), 1):
        tog = _stock_toggle(rank, r, analysis.get(r["code"], {}), flows.get(r["code"]) or {},
                            roes.get(r["code"]), deltas.get(r["code"]))
        res = append(page_id, [tog])
        if not res:
            continue
        tid = res[0]["id"]
        a = analysis.get(r["code"], {})
        extra = []
        qt = _quarter_table(incomes.get(r["code"]))
        if qt:
            extra.append({"object": "block", "type": "paragraph", "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": "📊 분기 실적 추이 (단일분기)"}, "annotations": {"bold": True}}]}})
            extra.append(qt)
        if a.get("추정"):
            extra.append({"object": "block", "type": "paragraph", "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": "📈 다음 분기 컨센서스  "}, "annotations": {"bold": True}},
                              {"type": "text", "text": {"content": a["추정"]}}]}})
        if extra:
            append(tid, extra)

    log(f"✅ Notion 업로드 완료: {page_url}")


if __name__ == "__main__":
    main()
