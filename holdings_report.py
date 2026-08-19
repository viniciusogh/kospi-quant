"""보유종목 심층분석 → 통합 대시보드 토글 (📌 내 보유종목).

momentum_daily 의 8섹션 Gemini 분석·수급막대·분기표를 그대로 재사용한다. 다른 점은 제목줄:
모멘텀 리포트는 '모멘텀 점수·신고가'를, 여기서는 **보유 관점(수량·평단→현재가·수익률·평가금액)** 을 보여준다.

토큰: momentum_analysis.json 캐시를 공유하므로 오늘 모멘텀 추천에 든 보유종목은 재분석하지 않는다.
(오늘 날짜 캐시면 Gemini 호출 0회. 그 외는 momentum 과 같은 규칙 — 7일내 재등장은 가벼운 업데이트만.)

실행: python holdings_report.py   (.env 필요. portfolio.py 가 만든 portfolio.json 을 읽음)
"""
import os, json
from datetime import datetime

import numpy as np
import pandas as pd

import momentum_daily as M
import dashboard as D
from momentum_backtest import token, KST

_DIR = os.path.dirname(os.path.abspath(__file__))
PORTFOLIO = os.path.join(_DIR, "portfolio.json")
QUALITY = os.path.join(_DIR, "latest_kospi_quality.csv")
CACHE = os.path.join(_DIR, "momentum_analysis.json")
TITLE = "📌 내 보유종목 심층분석"


def _sector_table():
    """전종목 PER/PBR 섹터 순위 (모멘텀 리포트와 동일 기준) + 섹터·ROE."""
    if not os.path.exists(QUALITY):
        return pd.DataFrame()
    d = pd.read_csv(QUALITY, dtype={"종목코드": str})
    d["code"] = d["종목코드"].str.zfill(6)
    d["_per"] = pd.to_numeric(d.get("PER"), errors="coerce").where(lambda s: s > 0)
    d["_pbr"] = pd.to_numeric(d.get("PBR"), errors="coerce").where(lambda s: s > 0)
    g = d.groupby("섹터")
    d["per_pct"] = g["_per"].rank(pct=True)
    d["pbr_pct"] = g["_pbr"].rank(pct=True)
    d["per_rank"] = g["_per"].rank()
    d["pbr_rank"] = g["_pbr"].rank()
    d["sec_n"] = g["_per"].transform("count")
    return d.set_index("code")


def _rows(positions, tok):
    """보유종목별 지표 행 — momentum 의 score_today 로 뽑고 섹터·밸류순위는 quality CSV 에서."""
    q = _sector_table()
    out = []
    for p in positions:
        code = p["code"]
        s = M.score_today(code, tok)
        if s is None:
            M.log(f"  ⚠️ {p['name']}({code}) 시세 조회 실패 — 건너뜀")
            continue
        qq = q.loc[code] if len(q) and code in q.index else None
        row = {**s, "종목명": p["name"] or (qq["종목명"] if qq is not None else code),
               "섹터": (qq["섹터"] if qq is not None and pd.notna(qq["섹터"]) else "-"),
               "per_pct": qq["per_pct"] if qq is not None else np.nan,
               "pbr_pct": qq["pbr_pct"] if qq is not None else np.nan,
               "per_rank": qq["per_rank"] if qq is not None else np.nan,
               "pbr_rank": qq["pbr_rank"] if qq is not None else np.nan,
               "sec_n": int(qq["sec_n"]) if qq is not None and pd.notna(qq["sec_n"]) else 0,
               "score": 0.0}         # 모멘텀 점수는 보유 리포트에서 의미 없음(제목줄에 안 씀)
        out.append((row, p))
    return out


def _data_asof():
    """최신 데이터 기준일 = momentum_history.csv 의 마지막 거래일. 없으면 오늘."""
    h = os.path.join(_DIR, "momentum_history.csv")
    try:
        d = pd.read_csv(h, encoding="utf-8-sig")
        return str(d["date"].max())
    except Exception:
        return datetime.now(KST).strftime("%Y-%m-%d")


def _analyze(rows, tok):
    """8섹션 분석. 오늘 날짜 캐시가 있으면 Gemini 호출 없이 그대로 재사용(토큰 절약)."""
    cache = {}
    if os.path.exists(CACHE):
        try:
            cache = json.load(open(CACHE))
        except Exception:
            cache = {}
    asof = _data_asof()          # 달력 날짜가 아니라 '최신 거래일' 과 비교해야 한다.
    reuse, need = {}, []          # 휴장일엔 캐시 date(=마지막 거래일)가 오늘과 다르지만 최신이다.
    for row, _ in rows:
        c = cache.get(row["code"])
        if c and c.get("date") == asof and c.get("요약"):
            reuse[row["code"]] = c
        else:
            need.append(row)
    if reuse:
        M.log(f"  캐시 그대로 재사용({asof} 기준) {len(reuse)}종목 — Gemini 호출 0")

    flows, roes, ebitdas, incomes = {}, {}, {}, {}
    for row, _ in rows:                      # 수급 막대·분기표는 재사용분도 필요
        code = row["code"]
        flows[code] = M.investor_flows(code, tok) or {}
        roes[code] = M.roe_latest(code, tok)
        ebitdas[code] = M.fetch_ebitda(code, tok)
        incomes[code] = M.fetch_income(code, tok)

    out = dict(reuse)
    if need:
        M.log(f"  Gemini 분석 필요 {len(need)}종목")
        df = pd.DataFrame(need)
        got = M.gemini_analyze(df, flows, roes, cache, ebitdas, M.load_debt_ranks())
        out.update(got)
        json.dump(cache, open(CACHE, "w"), ensure_ascii=False)
    return out, flows, roes, incomes


def _holding_toggle(row, pos, a, fl, roe):
    """보유 1종목 토글. 제목줄 = 수익률·평단→현재가·수량·평가금액 (모멘텀 지표 대신 보유 관점)."""
    ret = pos["ret"]
    col = "red" if ret > 0 else ("blue" if ret < 0 else "gray")
    n = int(row.get("sec_n", 0))
    per_s = f"{row['per']:.0f}" if row.get("per", 0) and row["per"] > 0 else "—"
    pbr_s = f"{row['pbr']:.1f}" if row.get("pbr", 0) and row["pbr"] > 0 else "—"
    pl, bl = M._rel(row.get("per_pct"), n), M._rel(row.get("pbr_pct"), n)
    secname = row["섹터"] if row["섹터"] != "-" else "업종"
    per_rk = (f" ({secname} {int(row['per_rank'])}/{n}위·{pl})"
              if pl and pd.notna(row.get("per_rank")) else "")
    pbr_rk = (f" ({int(row['pbr_rank'])}/{n}위·{bl})"
              if bl and pd.notna(row.get("pbr_rank")) else "")
    roe_s = f" · ROE {roe:.1f}%" if roe is not None else ""

    def gray(t):
        return {"type": "text", "text": {"content": t}, "annotations": {"color": "gray"}}

    title = [
        {"type": "text", "text": {"content": f"📌 {row['종목명']} "}, "annotations": {"bold": True}},
        {"type": "text", "text": {"content": f"{ret*100:+.1f}% "},
         "annotations": {"bold": True, "color": col}},
        gray(f"({row['code']}) · {pos['broker']}  |  {pos['qty']:,.0f}주 · "
             f"{pos['avg']:,.0f} → {pos['price']:,.0f}원 · 평가 {pos['eval']:,.0f}원")]

    kids = []
    sig = ", ".join(pos.get("signal") or [])
    head = (f"💰 손익 {pos['pl']:+,.0f}원 · 매입 {pos['cost']:,.0f}원"
            + (f"\n⭐ 오늘 모멘텀 추천: {sig}" if sig else ""))
    kids.append(M._para([{"type": "text", "text": {"content": head},
                          "annotations": {"bold": True}}]))
    if row.get("ret20") is not None:
        hi = ("60일 신고가" if row.get("hi60", 0) >= 0.999
              else f"60일 고점대비 {(row['hi60']-1)*100:.0f}%")
        kids.append(M._para([gray(f"📉 20일 {row['ret20']*100:+.1f}% · 당일 {row['chg']*100:+.1f}% · {hi}")]))
    if a.get("업데이트"):
        kids.append(M._para([
            {"type": "text", "text": {"content": "🆕 오늘 업데이트  "},
             "annotations": {"bold": True, "color": "green"}},
            {"type": "text", "text": {"content": a["업데이트"]}}]))
    sb = M._supply_bars(fl)
    if sb:
        kids.append(sb)
    dline = f"🏢 PER {per_s}{per_rk} · PBR {pbr_s}{pbr_rk}{roe_s}"
    if a.get("issue"):
        dline += f"\n📰 이슈 — {a['issue']}"
    kids.append(M._para([gray(dline)]))
    for label, key in M.SECTIONS:
        v = (a.get(key) or "").strip()
        if key == "수급분석":
            v = M._strip_won(v)
        if v:
            kids.append(M._para([
                {"type": "text", "text": {"content": f"{label}\n"}, "annotations": {"bold": True}},
                {"type": "text", "text": {"content": v[:1900]}}]))
    return {"object": "block", "type": "toggle",
            "toggle": {"rich_text": title, "color": "default", "children": kids}}


def main():
    # 휴장일엔 시세가 안 바뀌므로 Gemini 호출을 아낀다 (portfolio.py 와 같은 판정 재사용)
    if os.environ.get("SKIP_MARKET_CHECK") != "1":
        try:
            import portfolio as _P
            if not _P.market_open_today():
                M.log("휴장일 — 보유종목 리포트 생략 (강제: SKIP_MARKET_CHECK=1)")
                return
        except Exception as e:
            M.log(f"  장운영 확인 실패(계속 진행): {str(e)[:60]}")

    if not os.path.exists(PORTFOLIO):
        M.log("portfolio.json 없음 — portfolio.py 를 먼저 실행하세요")
        return
    data = json.load(open(PORTFOLIO))
    positions = data.get("positions") or []
    if not positions:
        M.log("보유 종목 없음 — 리포트 생략")
        return

    tok = token()
    M.log(f"▶ 보유종목 {len(positions)}개 심층분석")
    rows = _rows(positions, tok)
    if not rows:
        M.log("시세 조회 가능한 보유종목이 없음 — 중단")
        return
    analysis, flows, roes, incomes = _analyze(rows, tok)

    t = data["total"]
    col = "red_background" if t["pl"] > 0 else ("blue_background" if t["pl"] < 0 else "gray_background")
    header = [{"object": "block", "type": "callout", "callout": {
        "icon": {"type": "emoji", "emoji": "💰"}, "color": col,
        "rich_text": [
            {"type": "text", "text": {"content": f"보유 {len(rows)}종목 · 총평가 {t['eval']:,.0f}원 · "
                                                 f"손익 {t['pl']:+,.0f}원 ({t['ret']*100:+.2f}%)\n"},
             "annotations": {"bold": True}},
            {"type": "text", "text": {"content": f"기준 {data.get('asof','')} · 분석은 AI 검색 추정으로 "
                                                 f"확정 사실이 아님 · 투자판단 보조용"},
             "annotations": {"color": "gray"}}]}}]

    items = []
    for row, pos in sorted(rows, key=lambda x: -x[1]["eval"]):
        code = row["code"]
        tog = _holding_toggle(row, pos, analysis.get(code, {}), flows.get(code) or {}, roes.get(code))
        extra = []
        qt = M._quarter_table(incomes.get(code))
        if qt:
            extra.append(M._para([{"type": "text", "text": {"content": "📊 분기 실적 추이 (단일분기)"},
                                   "annotations": {"bold": True}}]))
            extra.append(qt)
        if (analysis.get(code) or {}).get("추정"):
            extra.append(M._para([
                {"type": "text", "text": {"content": "📈 다음 분기 컨센서스  "}, "annotations": {"bold": True}},
                {"type": "text", "text": {"content": analysis[code]["추정"]}}]))
        items.append((tog, extra))

    tid = D.add_report(TITLE, header, items, color="default")
    M.log(f"✅ 대시보드에 보유종목 리포트 추가: {D.url()}" if tid else "❌ 보유종목 리포트 추가 실패")


if __name__ == "__main__":
    main()
