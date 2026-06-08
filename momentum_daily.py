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
OUT_CSV  = os.path.join(_DIR, "latest_momentum_reco.csv")
UNIV_TOP  = int(os.environ.get("UNIV_TOP", "0"))   # 0=전종목, n=시총상위 n (테스트용)
TOP_N     = 10                    # 최종 추천 종목 수
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
    return c, v, per, pbr


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
    if c >= ma200 and c >= ma120:
        txt, emo, color = "상승추세 (200·120일선 위) — 진입 우호", "📈", "green_background"
    elif c >= ma200:
        txt, emo, color = "상승추세 (200일선 위·단기 조정) — 보통", "📊", "yellow_background"
    else:
        txt, emo, color = "하락추세 (200일선 아래) — 진입 주의", "📉", "orange_background"
    return {"text": f"{txt}  ·  지수 {c:,.0f} / 200일선 {ma200:,.0f}", "emoji": emo, "color": color}


def score_today(code, tok):
    r = fetch_recent(code, tok)
    if r is None:
        return None
    c, v, per, pbr = r
    px = c[-1]
    dr = np.diff(c[-21:]) / c[-21:-1]
    return {"code": code, "price": px,
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
    decile = df.nlargest(max(10, int(len(df) * 0.10)), "score")  # 2단계: 모멘텀 top10%
    final = decile.nsmallest(TOP_N, "vol20").copy()              # 3단계: 저변동성 10
    final.sort_values("score", ascending=False, inplace=True)
    final.reset_index(drop=True, inplace=True)
    final["rank"] = np.arange(1, len(final) + 1)

    final["종목명"] = final["code"].map(lambda c: meta.get(c, {}).get("종목명", ""))
    final["섹터"]  = final["code"].map(lambda c: meta.get(c, {}).get("섹터", ""))
    out = final[["rank", "code", "종목명", "섹터", "price", "score", "ret20", "ret5", "vol20",
                 "per", "pbr", "per_pct", "pbr_pct", "per_rank", "pbr_rank", "sec_n", "hi60", "liq5"]]
    out.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    log(f"저장 {OUT_CSV} (최종 {len(out)}종목)")

    print(f"\n===== 오늘 건강 모멘텀 (저변동성 압축) TOP {TOP_N} =====")
    show = out.head(TOP_N).copy()
    show["price"] = show["price"].map(lambda x: f"{int(x):,}")
    show["ret20%"] = (show["ret20"] * 100).round(1)
    print(show[["rank", "code", "종목명", "섹터", "price", "score", "ret20%"]].to_string(index=False))

    top10 = out.head(TOP_N).copy()
    # 상위10 enrichment: 일별 수급 + ROE (라이브)
    log("상위10 수급·ROE 수집")
    flows, roes = {}, {}
    for code in top10["code"]:
        flows[code] = investor_flows(code, tok)
        roes[code] = roe_latest(code, tok)
    trend = kospi_trend(tok)
    if trend:
        log(f"KOSPI 추세: {trend['emoji']} {trend['text']}")
    analysis = gemini_analyze(top10, flows, roes) if os.environ.get("GEMINI_API_KEY") else {}
    if os.environ.get("NOTION_API_KEY"):
        upload_notion(top10, analysis, trend, flows, roes)
    else:
        log("NOTION_API_KEY 없음 → Notion 업로드 생략 (로컬). Actions 에선 업로드됨")
        for _, r in top10.iterrows():
            a = analysis.get(r["code"], {})
            log(f"  {r['종목명']}: 이슈={a.get('issue','')} | 종합={a.get('verdict','')}")


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


def gemini_analyze(top10, flows, roes):
    """상위10: (1)급등 이슈 키워드 + (2)모멘텀·수급·퀄리티 종합한 한줄 코멘트. Gemini+검색."""
    from google import genai
    c = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    out = {}
    for _, r in top10.iterrows():
        code = r["code"]; fl = flows.get(code) or {}; roe = roes.get(code)
        hi = "신고가" if r.get("hi60", 0) >= 0.999 else f"고점대비 {(r['hi60']-1)*100:.0f}%"
        sup = (f"최근5일 수급 외인 {fl.get('frgn5',0):+.0f}억·기관 {fl.get('orgn5',0):+.0f}억·"
               f"개인 {fl.get('prsn5',0):+.0f}억" if fl else "수급 데이터 없음")
        n = r.get("sec_n", 0)
        per_str = (f"PER {r['per']:.0f}(섹터 {int(r['per_rank'])}/{int(n)}위)"
                   if (r.get('per', 0) > 0 and not pd.isna(r.get('per_rank')) and n) else "PER 적자/-")
        roe_str = f"·ROE {roe:.1f}%" if roe is not None else ""
        stat = (f"{r['종목명']}({code}, {r['섹터']}): 20일 {r['ret20']*100:+.0f}%·{hi}, "
                f"{per_str}·PBR {r['pbr']:.1f}{roe_str}, {sup}.")
        prompt = (f"국내주식 데이터: {stat}\n"
                  "아래 두 줄을 정확히 이 형식으로만 출력(다른 말 금지):\n"
                  "이슈: <급등 핵심 이슈 키워드 2~3개, ·로 연결, 35자내, 따옴표·서술문 금지>\n"
                  "종합: <위 데이터 종합 한줄평 45자내, 강점과 위험을 균형있게. 예: 기관 주도 밸류업 급등이나 신고가 과열·실적 약>")
        try:
            resp = c.models.generate_content(model="gemini-2.5-flash", contents=prompt,
                config={"tools": [{"google_search": {}}], "thinking_config": {"thinking_budget": 0},
                        "max_output_tokens": 2500})
            issue, verdict = "", ""
            for line in resp.text.splitlines():
                line = line.strip()
                if line.startswith("이슈"):
                    issue = _trim_phrase(line.split(":", 1)[-1] if ":" in line else line)
                elif line.startswith("종합"):
                    v = (line.split(":", 1)[-1] if ":" in line else line).strip().strip("'\"")
                    if len(v) > 58:                       # 단어 경계서 절단 (중간 잘림 방지)
                        cut = v[:58]; b = max(cut.rfind(" "), cut.rfind("·"), cut.rfind(","))
                        v = (cut[:b] if b > 30 else cut).rstrip(" ,·") + "…"
                    verdict = v
            out[code] = {"issue": issue if _is_clean(issue) else "", "verdict": verdict}
        except Exception as e:
            log(f"  Gemini {code} 실패: {str(e)[:80]}")
            out[code] = {"issue": "", "verdict": ""}
    return out


DISCLAIMER = ("📊 선정: 거래대금 100억↑  →  모멘텀 상위 10%  →  저변동성 10개 (과열 꼭지 제거)\n"
              "📈 백테스트(2023~26): 건당 +1.3% / 30일 · 승률 42% · 7개 반기 중 5개 +  (하락장 약·롱온리)\n"
              "🎯 제안 청산: +20% 익절 / −10% 손절\n"
              "ℹ️ 수급·재무 미반영 · 종목 '이슈'는 AI 검색 추정(확정 아님) · 투자판단 보조용")


def _rel(pct, n):
    """섹터 내 백분위 → 라벨. pct 낮음=쌈. 표본<4면 빈값."""
    if pd.isna(pct) or n < 4:
        return ""
    return "저평가" if pct <= 0.33 else ("고평가" if pct >= 0.67 else "평균")


def upload_notion(top, analysis=None, trend=None, flows=None, roes=None):
    """Notion 업로드. 수급.py 의 날짜페이지/중복정리 헬퍼 재활용."""
    analysis = analysis or {}; flows = flows or {}; roes = roes or {}
    import 수급 as sg
    headers = {"Authorization": f"Bearer {os.environ['NOTION_API_KEY']}",
               "Content-Type": "application/json", "Notion-Version": "2022-06-28"}
    today = datetime.now(KST).strftime("%Y-%m-%d")
    parent = os.environ.get("NOTION_PARENT_PAGE_ID", "3324a00632f880fbb014d766d87a1079")
    title = f"🚀 {today} KOSPI 30일 모멘텀 추천"

    cols = ["랭킹", "종목코드", "종목명", "섹터", "주가", "모멘텀점수", "20일등락%", "PER(섹터)", "PBR(섹터)"]
    cell = lambda t: [{"type": "text", "text": {"content": str(t)}}]
    rows = [{"type": "table_row", "table_row": {"cells": [cell(c) for c in cols]}}]
    short = {"저평가": "저", "고평가": "고", "평균": "평", "": ""}
    for _, r in top.iterrows():
        per_s = f"{r['per']:.0f}" if r.get('per', 0) and r['per'] > 0 else "—"
        pbr_s = f"{r['pbr']:.1f}" if r.get('pbr', 0) and r['pbr'] > 0 else "—"
        pl = short[_rel(r.get("per_pct"), r.get("sec_n", 0))]
        bl = short[_rel(r.get("pbr_pct"), r.get("sec_n", 0))]
        rows.append({"type": "table_row", "table_row": {"cells": [
            cell(int(r["rank"])), cell(r["code"]), cell(r["종목명"]),
            cell(r["섹터"] if pd.notna(r["섹터"]) else "-"), cell(f"{int(r['price']):,}"),
            cell(f"{r['score']:.2f}"), cell(f"{r['ret20']*100:.1f}"),
            cell(f"{per_s} {f'({pl})' if pl else ''}".strip()),
            cell(f"{pbr_s} {f'({bl})' if bl else ''}".strip()),
        ]}})

    children = []
    if trend:
        children.append({"object": "block", "type": "callout", "callout": {
            "rich_text": [{"type": "text", "text": {"content": f"KOSPI 추세: {trend['text']}"},
                           "annotations": {"bold": True}}],
            "icon": {"type": "emoji", "emoji": trend["emoji"]}, "color": trend["color"]}})
    children += [
        {"object": "block", "type": "callout", "callout": {
            "rich_text": [{"type": "text", "text": {"content": DISCLAIMER}}],
            "icon": {"type": "emoji", "emoji": "📐"}, "color": "yellow_background"}},
        {"object": "block", "type": "heading_3",
         "heading_3": {"rich_text": [{"type": "text", "text": {"content": "🏆 상위 10"}}]}},
    ]
    def gray(t): return {"type": "text", "text": {"content": t}, "annotations": {"color": "gray"}}
    for rank, (_, r) in enumerate(top.head(10).iterrows(), 1):
        icon = {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, "📈")
        sec = r["섹터"] if pd.notna(r["섹터"]) else "-"
        secname = sec if sec != "-" else "업종"
        code = r["code"]; fl = flows.get(code) or {}; roe = roes.get(code); a = analysis.get(code, {})
        n = int(r.get("sec_n", 0))
        per_s = f"{r['per']:.0f}" if r.get('per', 0) and r['per'] > 0 else "—"
        pbr_s = f"{r['pbr']:.1f}" if r.get('pbr', 0) and r['pbr'] > 0 else "—"
        pl = _rel(r.get("per_pct"), n); bl = _rel(r.get("pbr_pct"), n)
        per_rk = f" ({secname} {int(r['per_rank'])}/{n}위·{pl})" if (pl and not pd.isna(r.get('per_rank'))) else ""
        pbr_rk = f" ({int(r['pbr_rank'])}/{n}위·{bl})" if (bl and not pd.isna(r.get('pbr_rank'))) else ""
        roe_s = f"  ·  ROE {roe:.1f}%" if roe is not None else ""
        hi = "신고가" if r.get("hi60", 0) >= 0.999 else f"고점대비 {(r['hi60']-1)*100:.0f}%"
        # 줄1: 이름  줄2: 모멘텀  줄3: 수급  줄4: 퀄리티  줄5: 이슈  줄6: 종합
        rich = [
            {"type": "text", "text": {"content": f"{r['종목명']} "}, "annotations": {"bold": True}},
            {"type": "text", "text": {"content": f"({code}) · {sec}\n"}},
            gray(f"📈 모멘텀 {r['score']:.1f} · 20일 {r['ret20']*100:+.1f}% · {hi} · {int(r['price']):,}원\n")]
        if fl:
            rich.append(gray(f"💰 수급5일: 외인 {fl['frgn5']:+.0f}억 · 기관 {fl['orgn5']:+.0f}억 · 개인 {fl['prsn5']:+.0f}억\n"))
        rich.append(gray(f"🏢 PER {per_s}{per_rk} · PBR {pbr_s}{pbr_rk}{roe_s}"))
        if a.get("issue"):
            rich.append(gray(f"\n📰 이슈: {a['issue']}"))
        if a.get("verdict"):
            rich.append({"type": "text", "text": {"content": f"\n🔎 종합: {a['verdict']}"},
                         "annotations": {"color": "default"}})
        children.append({"object": "block", "type": "callout", "callout": {
            "rich_text": rich, "icon": {"type": "emoji", "emoji": icon},
            "color": "blue_background" if rank <= 3 else "gray_background"}})
    children += [
        {"object": "block", "type": "divider", "divider": {}},
        {"object": "block", "type": "heading_3",
         "heading_3": {"rich_text": [{"type": "text", "text": {"content": f"📋 전체 TOP{len(top)}"}}]}},
        {"object": "block", "type": "table", "table": {
            "table_width": len(cols), "has_column_header": True, "has_row_header": False, "children": rows}},
    ]

    date_parent = sg._get_or_create_date_page(today, headers, parent)
    sg._archive_same_title_pages(title, headers, date_parent)
    body = {"parent": {"page_id": date_parent},
            "properties": {"title": {"title": [{"text": {"content": title}}]}},
            "children": children}
    r = requests.post("https://api.notion.com/v1/pages", headers=headers, json=body, timeout=15)
    if r.status_code == 200:
        log(f"✅ Notion 업로드 완료: {r.json().get('url', '')}")
    else:
        log(f"❌ Notion 업로드 실패 {r.status_code}: {r.text[:200]}")


if __name__ == "__main__":
    main()
