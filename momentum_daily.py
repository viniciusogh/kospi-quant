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
    return c, v


def score_today(code, tok):
    r = fetch_recent(code, tok)
    if r is None:
        return None
    c, v = r
    px = c[-1]
    dr = np.diff(c[-21:]) / c[-21:-1]
    return {"code": code, "price": px,
            "hi60": px / c[-61:].max(),
            "disp20": px / c[-20:].mean() - 1,
            "ret5": px / c[-6] - 1,
            "ret20": px / c[-21] - 1,
            "vol20": dr.std(),
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
    out = final[["rank", "code", "종목명", "섹터", "price", "score", "ret20", "ret5", "vol20", "hi60", "liq5"]]
    out.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    log(f"저장 {OUT_CSV} (최종 {len(out)}종목)")

    print(f"\n===== 오늘 건강 모멘텀 (저변동성 압축) TOP {TOP_N} =====")
    show = out.head(TOP_N).copy()
    show["price"] = show["price"].map(lambda x: f"{int(x):,}")
    show["ret20%"] = (show["ret20"] * 100).round(1)
    print(show[["rank", "code", "종목명", "섹터", "price", "score", "ret20%"]].to_string(index=False))

    reasons = gemini_reasons(out.head(10)) if os.environ.get("GEMINI_API_KEY") else {}
    if os.environ.get("NOTION_API_KEY"):
        upload_notion(out.head(TOP_N), reasons)
    else:
        log("NOTION_API_KEY 없음 → Notion 업로드 생략 (로컬). Actions 에선 업로드됨")


def _trim_phrase(t):
    """키워드구만 남김. 따옴표·서두·서술꼬리 제거, 쉼표→·, 길면 절단."""
    t = t.strip().replace("\n", " ").replace("'", "").replace('"', "").replace("`", "")
    # 서두 군더더기 제거
    t = re.sub(r"^.*?(이슈는|요인은|배경은|원인은|이유는|다음과\s*같습니다\.?|요약하면|핵심은)\s*", "", t)
    # 서술 꼬리(문장) 잘라내기 — 첫 서술어/요약 표현 앞까지만
    m = re.search(r"(요약|습니다|입니다|된다|했다|이다|있다|봅니다|됐다|때문)", t)
    if m:
        t = t[:m.start()]
    t = t.replace(", ", "·").replace(",", "·")
    t = re.sub(r"\s*(으로|등으로|등)\s*$", "", t.strip())   # 끝의 '으로/등' 꼬리
    t = t.strip(" ·.")
    if len(t) <= 46:
        return t
    cut = t[:46]
    return cut[:cut.rfind("·")].strip(" ·") if "·" in cut else cut.strip()


def _is_clean(t):
    return bool(t) and "촉매 미확인" not in t and "특이" not in t


def gemini_reasons(top10):
    """상위10 급등 배경 키워드구 (Gemini + 검색 그라운딩). 근거없으면 빈값."""
    from google import genai
    c = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    out = {}
    for _, r in top10.iterrows():
        prompt = (f"{r['종목명']}({r['code']}) 주가가 최근 한 달 {r['ret20']*100:.0f}% 급등. "
                  "급등 핵심 이슈를 키워드 명사구 2~3개로만, ·(가운뎃점)으로 연결. "
                  "반드시 지킬 것: 따옴표 금지, 서술문 금지, '다음과 같습니다'·'요약'·'때문' 등 군더더기 금지, 35자 이내. "
                  "예시 형식: 엔비디아 협력 기대·북미 수주 확대·실적 개선. "
                  "확실한 공개 근거 없으면 '미확인'만 출력.")
        try:
            resp = c.models.generate_content(model="gemini-2.5-flash", contents=prompt,
                config={"tools": [{"google_search": {}}], "thinking_config": {"thinking_budget": 0},
                        "max_output_tokens": 2000})
            txt = _trim_phrase(resp.text)
            out[r["code"]] = txt if _is_clean(txt) else ""
        except Exception as e:
            log(f"  Gemini {r['code']} 실패: {str(e)[:80]}")
            out[r["code"]] = ""
    return out


DISCLAIMER = ("📊 선정: 거래대금 100억↑  →  모멘텀 상위 10%  →  저변동성 10개 (과열 꼭지 제거)\n"
              "📈 백테스트(2023~26): 건당 +1.3% / 30일 · 승률 42% · 7개 반기 중 5개 +  (하락장 약·롱온리)\n"
              "🎯 제안 청산: +20% 익절 / −10% 손절\n"
              "ℹ️ 수급·재무 미반영 · 종목 '이슈'는 AI 검색 추정(확정 아님) · 투자판단 보조용")


def upload_notion(top, reasons=None):
    """Notion 업로드. 수급.py 의 날짜페이지/중복정리 헬퍼 재활용."""
    reasons = reasons or {}
    import 수급 as sg
    headers = {"Authorization": f"Bearer {os.environ['NOTION_API_KEY']}",
               "Content-Type": "application/json", "Notion-Version": "2022-06-28"}
    today = datetime.now(KST).strftime("%Y-%m-%d")
    parent = os.environ.get("NOTION_PARENT_PAGE_ID", "3324a00632f880fbb014d766d87a1079")
    title = f"🚀 {today} KOSPI 30일 모멘텀 추천"

    cols = ["랭킹", "종목코드", "종목명", "섹터", "주가", "모멘텀점수", "20일등락%", "5일등락%"]
    cell = lambda t: [{"type": "text", "text": {"content": str(t)}}]
    rows = [{"type": "table_row", "table_row": {"cells": [cell(c) for c in cols]}}]
    for _, r in top.iterrows():
        rows.append({"type": "table_row", "table_row": {"cells": [
            cell(int(r["rank"])), cell(r["code"]), cell(r["종목명"]),
            cell(r["섹터"] if pd.notna(r["섹터"]) else "-"), cell(f"{int(r['price']):,}"),
            cell(f"{r['score']:.2f}"), cell(f"{r['ret20']*100:.1f}"), cell(f"{r['ret5']*100:.1f}"),
        ]}})

    children = [
        {"object": "block", "type": "callout", "callout": {
            "rich_text": [{"type": "text", "text": {"content": DISCLAIMER}}],
            "icon": {"type": "emoji", "emoji": "📐"}, "color": "yellow_background"}},
        {"object": "block", "type": "heading_3",
         "heading_3": {"rich_text": [{"type": "text", "text": {"content": "🏆 상위 10"}}]}},
    ]
    for rank, (_, r) in enumerate(top.head(10).iterrows(), 1):
        icon = {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, "📈")
        sec = r["섹터"] if pd.notna(r["섹터"]) else "-"
        rich = [
            {"type": "text", "text": {"content": f"{r['종목명']} "}, "annotations": {"bold": True}},
            {"type": "text", "text": {"content": f"({r['code']}) · {sec}\n"}},
            {"type": "text", "text": {"content":
                f"모멘텀 {r['score']:.1f}   ·   20일 {r['ret20']*100:+.1f}%   ·   {int(r['price']):,}원"},
             "annotations": {"color": "gray"}}]
        why = reasons.get(r["code"])
        if why:
            rich.append({"type": "text", "text": {"content": f"\n이슈  {why}"},
                         "annotations": {"color": "gray"}})
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
