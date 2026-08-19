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
from momentum_backtest import token, KST
from datetime import datetime

_DIR = os.path.dirname(os.path.abspath(__file__))
UNI = os.path.join(_DIR, "latest_kospi_supply.csv")
UNIVERSE_N = int(os.environ.get("SECTOR_UNIVERSE", "300"))
TOP_PER_SECTOR = int(os.environ.get("SECTOR_TOP_N", "3"))
MIN_STOCKS = 2          # 섹터당 이 미만이면 표본 부족으로 제외


def _excluded(name, sector):
    s, n = str(sector), str(name)
    return ("ETF" in s or "ETN" in s or "스팩" in n or bool(re.search(r"우[BC]?$", n)))


def universe():
    d = pd.read_csv(UNI, dtype={"종목코드": str})
    d["code"] = d["종목코드"].str.zfill(6)
    d = d[~d.apply(lambda r: _excluded(r["종목명"], r["섹터"]), axis=1)].copy()
    d["시가총액"] = pd.to_numeric(d["시가총액"], errors="coerce")
    d["순매수"] = pd.to_numeric(d.get("외국인+기관_순매수대금(백만원)"), errors="coerce").fillna(0)
    d = d.dropna(subset=["시가총액", "섹터"])
    return d.nlargest(UNIVERSE_N, "시가총액").reset_index(drop=True)


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
    agg = agg[agg["n"] >= MIN_STOCKS].sort_values("d5", ascending=False)
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


def blocks(agg, tops, asof):
    head = [{"object": "block", "type": "table_row", "table_row": {"cells": [
        [{"type": "text", "text": {"content": h}, "annotations": {"bold": True}}]
        for h in ["섹터 (종목수)", "오늘", "5일", "20일", "외국인+기관 순매수", "주요 종목 (시총순·오늘)"]]}}]
    rows = []
    for _, r in agg.iterrows():
        stocks = tops.get(r["섹터"], [])
        cell = []
        for i, (nm, ch) in enumerate(stocks):
            if i:
                cell.append({"type": "text", "text": {"content": "\n"}})
            cell.append({"type": "text", "text": {"content": f"{nm} "}})
            cell.append(_pct(ch))
        rows.append({"object": "block", "type": "table_row", "table_row": {"cells": [
            [{"type": "text", "text": {"content": f"{r['섹터']} ({int(r['n'])})"},
              "annotations": {"bold": True}}],
            [_pct(r["오늘"])], [_pct(r["d5"], bold=True)], [_pct(r["d20"])],
            [{"type": "text", "text": {"content": _flow(r["순매수"])},
              "annotations": {"color": "red" if r["순매수"] > 0 else "blue"}}],
            cell or [{"type": "text", "text": {"content": "—"}}]]}})

    hot = " · ".join(agg.head(3)["섹터"].tolist())
    cold = " · ".join(agg.tail(3)["섹터"].tolist()[::-1])
    flow = agg.nlargest(3, "순매수")["섹터"].tolist()
    header = [{"object": "block", "type": "callout", "callout": {
        "icon": {"type": "emoji", "emoji": "🔄"}, "color": "gray_background",
        "rich_text": [
            {"type": "text", "text": {"content": f"🔥 5일 강세: {hot}\n"},
             "annotations": {"bold": True}},
            {"type": "text", "text": {"content": f"🧊 5일 약세: {cold}\n"}},
            {"type": "text", "text": {"content": f"💰 순매수 유입 상위: {' · '.join(flow)}\n"}},
            {"type": "text", "text": {"content":
                f"기준: {asof} 정규장 종가 · 시총 상위 {UNIVERSE_N} 개별주 · 섹터 등가중 "
                f"(시총가중은 소수 대형주에 지배돼 체감과 어긋남) · ETF/ETN/우선주 제외"},
             "annotations": {"color": "gray"}}]}}]
    table = {"object": "block", "type": "table", "table": {
        "table_width": 6, "has_column_header": True, "has_row_header": False,
        "children": head + rows}}
    return header, table


def main():
    tok = token()
    df = universe()
    M.log(f"▶ 섹터 장세: 시총 상위 {len(df)}개 수집 시작")
    m = metrics(df, tok)
    if m.empty:
        M.log("❌ 수집 실패 — 중단")
        return
    asof_raw = str(m["asof"].max())
    asof = f"{asof_raw[:4]}-{asof_raw[4:6]}-{asof_raw[6:]}"
    agg, tops = aggregate(m)
    M.log(f"  섹터 {len(agg)}개 집계 (종목 {len(m)}개)")
    header, table = blocks(agg, tops, asof)
    tid = D.add_report(f"🔄 {asof} 섹터 장세 (순환매)", header)
    if not tid:
        M.log("❌ 대시보드 토글 생성 실패")
        return
    D.append_blocks(tid, [table], chunk=1)
    M.log(f"✅ 대시보드에 섹터 장세 추가: {D.url()}")
    print(agg.assign(**{c: (agg[c] * 100).round(1) for c in ["오늘", "d5", "d20"]})
          .to_string(index=False))


if __name__ == "__main__":
    main()
