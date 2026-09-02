"""섹터 장세(순환매) 대시보드 → 통합 대시보드 토글 (슬롯3).

목적: "지금 돈이 어느 섹터로 도는가" 를 한 화면에서 본다. finviz 트리맵의 정보량을 표로 옮긴 형태
(노션은 트리맵을 못 그리고, 사용자가 차트보다 표를 선호한다 — index_ticker 때 확인된 선호).

기준(표에도 명시): 시가총액 상위 UNIVERSE_N 개 개별주 · **섹터 등가중** 수익률.
등가중을 쓰는 이유는 시총가중이 소수 대형주에 지배되기 때문(백테스트 교훈 #4).
ETF/ETN/우선주/스팩 제외. 가격은 KIS 일봉(정규장 종가) — 다른 리포트와 기준 통일.

실행: python sector_dashboard.py
"""
import os, re
import numpy as np
import pandas as pd

import momentum_daily as M
import dashboard as D
from momentum_backtest import token, KST, BASE, APP_KEY, APP_SECRET
from datetime import datetime

_DIR = os.path.dirname(os.path.abspath(__file__))
UNI = os.path.join(_DIR, "latest_kospi_supply.csv")
UNIVERSE_N = int(os.environ.get("SECTOR_UNIVERSE", "300"))
BASELINE = os.path.join(_DIR, ".sector_baseline.json")
TOP_PER_SECTOR = int(os.environ.get("SECTOR_TOP_N", "3"))
MIN_STOCKS = 2          # 섹터당 이 미만이면 표본 부족으로 제외


# KIS 업종분류(bstp_kor_isnm, 29종)는 순환매를 못 잡는다. 실측 예:
#   화장품 7종목이 "화학" 안에 석유화학·타이어와 섞여 평균돼 강세가 지워진다.
#   조선은 운송장비·부품 / 금융(HD한국조선해양) / 기계·장비 로 흩어진다.
#   방산은 운송장비·부품 / 금속(LIG넥스원) / 전기·전자(한화시스템) 로 흩어진다.
# → 테마 단위로 재분류한다. 종목코드 대신 **종목명**으로 적고 실행 시 유니버스에서 해석해,
#   이름이 안 맞으면 조용히 오분류되지 않고 경고로 드러나게 한다. 새 테마는 여기에 추가.
THEMES = {
    "화장품": ["코스맥스", "한국콜마", "LG생활건강", "아모레퍼시픽", "아모레퍼시픽홀딩스",
              "에이피알", "달바글로벌", "애경산업", "한국화장품"],
    "타이어": ["한국타이어앤테크놀로지", "금호타이어", "넥센타이어"],
    "조선":   ["HD현대중공업", "한화오션", "삼성중공업", "HD한국조선해양", "HD현대미포", "STX엔진"],
    "방산":   ["한화에어로스페이스", "한국항공우주", "LIG넥스원", "현대로템", "한화시스템", "풍산"],
    "원전":   ["두산에너빌리티", "한전기술", "한전KPS"],
}
_NAME2THEME = {n: t for t, lst in THEMES.items() for n in lst}


def _excluded(name, sector):
    s, n = str(sector), str(name)
    return ("ETF" in s or "ETN" in s or "스팩" in n or bool(re.search(r"우[BC]?$", n)))


def universe():
    d = pd.read_csv(UNI, dtype={"종목코드": str})
    d["code"] = d["종목코드"].str.zfill(6)
    d = d[~d.apply(lambda r: _excluded(r["종목명"], r["섹터"]), axis=1)].copy()
    d["시가총액"] = pd.to_numeric(d["시가총액"], errors="coerce")
    d["순매수"] = pd.to_numeric(d.get("외국인+기관_순매수대금(백만원)"), errors="coerce").fillna(0)
    d = d.dropna(subset=["시가총액"])
    # 유니버스 = 시총 상위 N ∪ 테마 구성종목 (소형 테마주도 빠지지 않게)
    top = d.nlargest(UNIVERSE_N, "시가총액")
    theme_rows = d[d["종목명"].isin(_NAME2THEME)]
    uni = pd.concat([top, theme_rows]).drop_duplicates(subset=["code"]).copy()
    # 테마 오버라이드: 업종분류를 테마로 덮어쓴다
    uni["섹터"] = uni.apply(
        lambda r: _NAME2THEME.get(r["종목명"], r["섹터"]), axis=1)
    uni = uni.dropna(subset=["섹터"])
    resolved = set(uni[uni["종목명"].isin(_NAME2THEME)]["종목명"])
    missing = [n for n in _NAME2THEME if n not in resolved]
    if missing:
        M.log(f"  ⚠️ 테마 종목명 미해석 {len(missing)}개(유니버스에 없음): {', '.join(missing[:6])}")
    return uni.reset_index(drop=True)


def build_baseline(df, tok):
    """오늘 봉을 제외한 종가로 기준가를 만든다 — {code: [전일종가, 5일전, 20일전]}.
    하루 한 번만 필요하다(장중엔 현재가만 갱신하면 되므로). 일봉 조회는 종목당 1콜.
    클라우드/로컬이 캐시를 공유하지 않으므로, 낡으면 각자 알아서 다시 만든다."""
    import json as _j
    today = datetime.now(KST).strftime("%Y%m%d")
    out = {}
    for i, r in df.iterrows():
        got = M.fetch_recent(r["code"], tok)
        if got is None:
            continue
        c, _v, _per, _pbr, last = got[0], got[1], got[2], got[3], got[4]
        if str(last) == today:          # 오늘 봉은 제외 (장중엔 미완성, 마감후엔 현재가와 중복)
            c = c[:-1]
        if len(c) < 21:
            continue
        out[r["code"]] = [float(c[-1]), float(c[-5]), float(c[-20])]
        if (i + 1) % 100 == 0:
            M.log(f"  기준가 {i+1}/{len(df)}")
    _j.dump({"built_for": datetime.now(KST).strftime("%Y-%m-%d"), "base": out},
            open(BASELINE, "w"))
    M.log(f"  기준가 캐시 생성 {len(out)}종목")
    return out


def load_baseline(df, tok):
    """캐시가 오늘자면 재사용, 아니면 새로 만든다."""
    import json as _j
    today = datetime.now(KST).strftime("%Y-%m-%d")
    if os.path.exists(BASELINE):
        try:
            d = _j.load(open(BASELINE))
            if d.get("built_for") == today and d.get("base"):
                M.log(f"  기준가 캐시 재사용 ({len(d['base'])}종목)")
                return d["base"]
        except Exception:
            pass
    M.log("  기준가 캐시 없음/낡음 → 생성 (일봉 조회, 첫 실행만 느림)")
    return build_baseline(df, tok)


def metrics_live(df, tok, base):
    """현재가 1콜/종목으로 오늘·5일·20일 수익률. 기준가는 캐시에서."""
    url = f"{BASE}/uapi/domestic-stock/v1/quotations/inquire-price"
    hdr = {"authorization": f"Bearer {tok}", "appkey": APP_KEY, "appsecret": APP_SECRET,
           "tr_id": "FHKST01010100", "custtype": "P"}
    rows = []
    for i, r in df.iterrows():
        b = base.get(r["code"])
        if not b:
            continue
        j = M._get(url, hdr, {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": r["code"]})
        o = (j or {}).get("output") or {}
        try:
            px = float(o.get("stck_prpr") or 0)
        except Exception:
            px = 0
        if px <= 0:
            continue
        prev, b5, b20 = b
        rows.append({"code": r["code"], "종목명": r["종목명"], "섹터": r["섹터"],
                     "시가총액": r["시가총액"], "순매수": r["순매수"], "price": px,
                     "오늘": px / prev - 1 if prev else 0.0,
                     "d5": px / b5 - 1 if b5 else 0.0,
                     "d20": px / b20 - 1 if b20 else 0.0,
                     "asof": datetime.now(KST).strftime("%Y%m%d")})
        if (i + 1) % 100 == 0:
            M.log(f"  현재가 {i+1}/{len(df)}")
    return pd.DataFrame(rows)


def metrics(df, tok):
    """종목별 오늘/5일/20일 수익률 (KIS 일봉 종가 기준)."""
    rows = []
    for i, r in df.iterrows():
        got = M.fetch_recent(r["code"], tok)
        if got is None:
            continue
        c = got[0]
        if len(c) < 22 or not c[-2] or not c[-6] or not c[-21]:
            continue
        rows.append({"code": r["code"], "종목명": r["종목명"], "섹터": r["섹터"],
                     "시가총액": r["시가총액"], "순매수": r["순매수"],
                     "오늘": c[-1] / c[-2] - 1, "d5": c[-1] / c[-6] - 1,
                     "d20": c[-1] / c[-21] - 1, "price": c[-1], "asof": got[4]})
        if (i + 1) % 50 == 0:
            M.log(f"  {i+1}/{len(df)} 수집")
    return pd.DataFrame(rows)


def aggregate(m):
    g = m.groupby("섹터")
    agg = g.agg(오늘=("오늘", "mean"), d5=("d5", "mean"), d20=("d20", "mean"),
                순매수=("순매수", "sum"), n=("code", "count")).reset_index()
    # 정렬도 '오늘' 기준으로 통일 (트리맵과 어긋나면 같은 화면에서 순서가 달라 보인다)
    agg = agg[agg["n"] >= MIN_STOCKS].sort_values("오늘", ascending=False)
    tops = {}
    for sec, sub in g:
        t = sub.nlargest(TOP_PER_SECTOR, "시가총액")
        tops[sec] = [(r["종목명"], r["오늘"]) for _, r in t.iterrows()]
    return agg, tops


def _flow(v_mil):
    """순매수 표기. CSV 단위는 백만원(프로젝트 공통 규약 — 수급.py·momentum_daily 동일).
    1조 이상은 조, 그 미만은 억으로 읽기 쉽게."""
    eok = v_mil / 100.0                     # 백만원 → 억원
    if abs(eok) >= 10000:
        return f"{eok/10000:+,.2f}조"
    return f"{eok:+,.0f}억"


def _pct(v, bold=False):
    col = "red" if v > 0 else ("blue" if v < 0 else "gray")
    return {"type": "text", "text": {"content": f"{v*100:+.1f}%"},
            "annotations": {"color": col, "bold": bold}}


# 트리맵이 중립(회색)으로 칠하는 구간과 같은 기준. 부호만 보면 '+0.0%' 도 강세가 된다.
NEUTRAL_PCT = 0.003


def _rank_labels(agg):
    """상단/하단 라벨. 전 섹터가 빠진 날 -0.1% 를 '강세' 로, 보합인 +0.0% 를 '강세' 로
    부르면 오독한다(2026-08-31 실제 발생). 부호가 아니라 중립 구간(±0.3%)으로 판정한다."""
    top_v, bot_v = agg["오늘"].iloc[0], agg["오늘"].iloc[-1]
    hot = ("🔥 오늘 강세" if top_v >= NEUTRAL_PCT else
           "🔻 전 섹터 하락 — 덜 빠진 순" if top_v < 0 else
           "🔸 오늘 보합 — 상위 순")
    cold = ("🧊 오늘 약세" if bot_v <= -NEUTRAL_PCT else
            "🔺 전 섹터 상승 — 덜 오른 순" if bot_v > 0 else
            "🔹 오늘 보합 — 하위 순")
    return hot, cold


def blocks(agg, tops, asof):
    """요약 콜아웃 + 한 줄 목록. 6열 표는 모바일에서 열이 잘리고 행이 여러 줄로 늘어난다
    (사용자 지적) → 목록으로. 전체 섹터는 트리맵이 담당하고 여기선 강세·약세만 짚는다."""
    hot = " · ".join(agg.head(3)["섹터"].tolist())
    cold = " · ".join(agg.tail(3)["섹터"].tolist()[::-1])
    hot_lbl, cold_lbl = _rank_labels(agg)
    hot5 = " · ".join(agg.nlargest(3, "d5")["섹터"].tolist())
    flow = agg.nlargest(3, "순매수")["섹터"].tolist()
    header = [{"object": "block", "type": "callout", "callout": {
        "icon": {"type": "emoji", "emoji": "🔄"}, "color": "gray_background",
        "rich_text": [
            {"type": "text", "text": {"content": f"{hot_lbl}: {hot}\n"},
             "annotations": {"bold": True}},
            {"type": "text", "text": {"content": f"{cold_lbl}: {cold}\n"}},
            {"type": "text", "text": {"content": f"🔄 5일 기준 강세(순환매): {hot5}\n"}},
            {"type": "text", "text": {"content": f"💰 순매수 유입 상위: {' · '.join(flow)}\n"}},
            {"type": "text", "text": {"content":
                f"{asof} 정규장 종가 · 시총 상위 {UNIVERSE_N}+테마 · 섹터 등가중"},
             "annotations": {"color": "gray"}}]}}]

    # 표 4열 — 목록은 숫자 벽이라 안 읽혔고(사용자 지적) 6열은 모바일에서 잘린다.
    # 색은 바로 위 트리맵과 맞춰 상승 초록/하락 빨강(노션 다른 표의 한국식과 반대인 건
    # 같은 리포트 안에서 이미지와 표가 어긋나는 쪽이 더 헷갈려서다).
    def _rt(t, bold=False, color=None):
        a = {"bold": bold}
        if color:
            a["color"] = color
        return [{"type": "text", "text": {"content": str(t)}, "annotations": a}]

    def verdict(today, d20):
        if today > 0:
            return ("추세 지속", "green") if d20 > 0 else ("반등 시작", "green")
        return ("조정 중", "red") if d20 > 0 else ("약세 지속", "red")

    head = [{"object": "block", "type": "table_row", "table_row": {"cells": [
        _rt("섹터", True), _rt("오늘", True), _rt("판정 · 20일", True), _rt("주도주", True)]}}]
    body = []
    for _, r in list(agg.head(6).iterrows()) + list(agg.tail(4).iloc[::-1].iterrows()):
        sec, td, d20 = r["섹터"], r["오늘"], r["d20"]
        t = tops.get(sec) or []
        # 오르는 섹터는 끌어올린 종목, 빠지는 섹터는 끌어내린 종목이라야 이유가 보인다
        lead = (max(t, key=lambda x: x[1]) if td > 0 else min(t, key=lambda x: x[1])) if t else None
        vt, vc = verdict(td, d20)
        col = "green" if td > 0 else ("red" if td < 0 else "default")
        body.append({"object": "block", "type": "table_row", "table_row": {"cells": [
            _rt(sec, True),
            _rt(f"{td*100:+.1f}%", True, col),
            _rt(vt, True, vc) + _rt(f"\n20일 {d20*100:+.0f}%", color="gray"),
            (_rt(lead[0]) + _rt(f"\n{lead[1]*100:+.1f}%",
                                color="green" if lead[1] > 0 else "red") if lead else _rt("-"))]}})

    rows = [{"object": "block", "type": "table", "table": {
        "table_width": 4, "has_column_header": True, "has_row_header": False,
        "children": head + body}}]
    return header, rows


def main():
    # 15분마다 도는 작업이라 휴일에 헛돌면 낭비가 26배가 된다. 주말은 래퍼가, 대체공휴일까지는
    # 토스 장운영 API 가 잡는다(2026-08-17 실증). 장 마감 후 정산 1회는 통과시켜야 하므로
    # '장이 열린 날' 기준으로만 판정하고 시간대는 launchd 스케줄에 맡긴다.
    if os.environ.get("SKIP_MARKET_CHECK") != "1":
        try:
            import portfolio as _P
            if not _P.market_open_today():
                M.log("휴장일 — 섹터 장세 갱신 생략 (강제: SKIP_MARKET_CHECK=1)")
                return
        except Exception as e:
            M.log(f"  장운영 확인 실패(계속 진행): {str(e)[:60]}")

    tok = token()
    df = universe()
    M.log(f"▶ 섹터 장세: 시총 상위 {len(df)}개 수집 시작")
    base = load_baseline(df, tok)
    m = metrics_live(df, tok, base)
    if m.empty:
        M.log("❌ 수집 실패 — 중단")
        return
    asof_raw = str(m["asof"].max())
    asof = (f"{asof_raw[:4]}-{asof_raw[4:6]}-{asof_raw[6:]} "
            f"{datetime.now(KST).strftime('%H:%M')}")
    agg, tops = aggregate(m)
    M.log(f"  섹터 {len(agg)}개 집계 (종목 {len(m)}개)")
    # 섹터 집계를 이력으로 append — 매 실행 덮어써서 과거가 안 남던 문제(2026-09-01).
    # 일일 아카이브와 나중 백테스트의 재료다.
    try:
        hist = os.path.join(_DIR, "sector_history.csv")
        h = agg.copy()
        h.insert(0, "date", asof[:10])
        h["주도주"] = [((tops.get(sec) or [("", 0)])[0][0]) for sec in h["섹터"]]
        # 15분마다 실행되므로 append 만 하면 하루에 수백 행이 쌓인다(2026-09-02: 168행).
        # 같은 (날짜, 섹터) 는 최신 것만 남긴다.
        if os.path.exists(hist):
            old = pd.read_csv(hist, encoding="utf-8-sig")
            h = pd.concat([old, h], ignore_index=True)
        h = h.drop_duplicates(subset=["date", "섹터"], keep="last")
        h.to_csv(hist, index=False, encoding="utf-8-sig")
        M.log(f"  📁 섹터 이력 저장 (누적 {len(h)}행 · {h['date'].nunique()}일)")
    except Exception as e:
        M.log(f"  ⚠️ 섹터 이력 저장 실패: {str(e)[:70]}")

    header, rows = blocks(agg, tops, asof)
    tid = D.add_report(f"🔄 {asof} 섹터 장세 (순환매)", header)
    if not tid:
        M.log("❌ 대시보드 토글 생성 실패")
        return

    # 트리맵 이미지 (한눈에 보는 용도) → 표보다 위에
    try:
        import sys
        sys.path.insert(0, os.path.join(_DIR, "viz"))
        import treemap as T
        cap_by = m.groupby("섹터")["시가총액"].sum()
        groups = [(r["섹터"], r["오늘"] * 100, float(cap_by[r["섹터"]]))
                  for _, r in agg.iterrows()]      # 섹터 단위·오늘 기준
        groups.sort(key=lambda g: -g[2])
        png = os.path.join(_DIR, "latest_sector_treemap.png")
        strip = {
            "hot_label": _rank_labels(agg)[0].split(" — ")[0].replace("🔥", "▲").replace("🔻", "▼"),
            "cold_label": _rank_labels(agg)[1].split(" — ")[0].replace("🧊", "▼").replace("🔺", "▲"),
            "hot": [(r["섹터"], r["오늘"] * 100, tops.get(r["섹터"]) or [])
                    for _, r in agg.head(6).iterrows()],
            "cold": [(r["섹터"], r["오늘"] * 100, tops.get(r["섹터"]) or [])
                     for _, r in agg.tail(6).iloc[::-1].iterrows()],
        }
        T.render(groups, asof, png, strip=strip)
        with open(png, "rb") as f:
            ok = D.append_image(tid, f.read(), "sector_treemap.png")
        M.log("  🗺 트리맵 업로드 " + ("완료" if ok else "실패"))
    except Exception as e:
        M.log(f"  ⚠️ 트리맵 생략: {str(e)[:90]}")

    D.append_blocks(tid, rows, chunk=20)
    D.append_blocks(tid, [{"object": "block", "type": "paragraph", "paragraph": {
        "rich_text": [{"type": "text",
                       "text": {"content": f"… 전체 {len(agg)}개 섹터는 위 트리맵 참고"},
                       "annotations": {"color": "gray", "italic": True}}]}}])
    M.log(f"✅ 대시보드에 섹터 장세 추가: {D.url()}")
    print(agg.assign(**{c: (agg[c] * 100).round(1) for c in ["오늘", "d5", "d20"]})
          .to_string(index=False))


if __name__ == "__main__":
    main()
