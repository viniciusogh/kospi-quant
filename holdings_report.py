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
    d["pbr_n"] = g["_pbr"].transform("count")
    d["_debt"] = pd.to_numeric(d.get("부채비율(%)"), errors="coerce")
    d["_roe"] = pd.to_numeric(d.get("ROE(%)"), errors="coerce")
    d["debt_rank"] = g["_debt"].rank()                    # 1위 = 부채 최저
    d["debt_n"] = g["_debt"].transform("count")
    d["roe_rank"] = g["_roe"].rank(ascending=False)       # 1위 = ROE 최고
    d["roe_n"] = g["_roe"].transform("count")
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
               "pbr_n": qq["pbr_n"] if qq is not None else np.nan,
               **({k: qq[k] for k in ("debt_rank", "debt_n", "roe_rank", "roe_n", "_debt", "_roe")}
                  if qq is not None else {}),
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
        if c and c.get("date") == asof and c.get("요약") and c.get("촉매3"):
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
        {"type": "text", "text": {"content": f"{ret*100:+.1f}%"},
         "annotations": {"bold": True, "color": col}},
        gray(f"  ·  {secname}")]        # 수량·평단·평가금액은 위 인포그래픽에 있다(중복 제거)

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


def _bullets(a, key3, key_prose):
    """카드용 3줄. 신규 분석엔 '촉매3/리스크3' 압축 필드가 있고, 아직 캐시에 없으면
    원문 프로즈를 문장 단위로 잘라 앞 3개를 쓴다(캐시가 7일 내 갱신되면 압축 필드로 바뀜)."""
    v = (a.get(key3) or "").strip()
    if v:
        return [x.strip(" ·") for x in v.split("·") if len(x.strip(" ·")) > 3][:3]
    import re as _re
    sents = [x.strip() for x in _re.split(r"(?<=다)\.\s*", a.get(key_prose) or "") if len(x.strip()) > 8]
    return [x[:26] for x in sents[:3]]


def _gauges(row):
    """섹터 내 순위 게이지 — 왼쪽이 좋음. PER/PBR/부채는 낮을수록, ROE 는 높을수록 1위."""
    out = []
    for lab, val, rk, n, labels in (
            ("PER", row.get("per"), row.get("per_rank"), row.get("sec_n"), ("쌈", "보통", "비쌈")),
            ("PBR", row.get("pbr"), row.get("pbr_rank"), row.get("pbr_n"), ("쌈", "보통", "비쌈")),
            ("부채비율", row.get("_debt"), row.get("debt_rank"), row.get("debt_n"), ("저부채", "보통", "고부채")),
            ("ROE", row.get("_roe"), row.get("roe_rank"), row.get("roe_n"), ("우수", "보통", "낮음"))):
        if val is None or pd.isna(val) or pd.isna(rk) or not n or n < 4:
            continue
        rk, n = int(rk), int(n)
        note = labels[0] if (rk - 0.5) / n < 1 / 3 else (labels[1] if (rk - 0.5) / n < 2 / 3 else labels[2])
        fmt = f"{val:,.0f}%" if lab == "부채비율" else (f"{val:.1f}%" if lab == "ROE"
              else (f"{val:.2f}배" if lab == "PBR" else f"{val:.1f}배"))
        out.append((lab, fmt, rk, n, note))
    return out


def _card(row, pos, a, fl):
    """종목 카드 PNG → image 블록. 실패하면 None (토글 원문은 그대로 살아있다)."""
    try:
        import sys
        sys.path.insert(0, os.path.join(_DIR, "viz"))
        import stock_card as SC
        one = (a.get("issue") or "").strip() or (a.get("요약") or "").split(".")[0]
        d = {"name": row["종목명"], "code": row["code"], "ret": pos["ret"],
             "chg": row.get("chg", 0) or 0,
             "sector": row["섹터"] if row["섹터"] != "-" else "업종", "sec_n": int(row.get("sec_n") or 0),
             "qty": pos["qty"], "avg": pos["avg"], "price": pos["price"],
             "eval": pos["eval"], "pl": pos["pl"], "oneline": one,
             "gauges": _gauges(row),
             "flow": [("외국인", fl.get("frgn5", 0)), ("기관", fl.get("orgn5", 0)),
                      ("개인", fl.get("prsn5", 0))] if fl else [],
             "bull": _bullets(a, "촉매3", "촉매"), "bear": _bullets(a, "리스크3", "리스크"),
             "target": ("컨센서스 " + (a.get("추정") or "").lstrip("→▲▼ ").strip()
                        if a.get("추정") else "")}
        png = os.path.join(_DIR, f"card_{row['code']}.png")
        SC.render(d, png)
        with open(png, "rb") as f:
            fid = D.upload_image(f.read(), f"card_{row['code']}.png")
        os.remove(png)
        return ({"object": "block", "type": "image",
                 "image": {"type": "file_upload", "file_upload": {"id": fid}}} if fid else None)
    except Exception as e:
        M.log(f"  ⚠️ {row['종목명']} 카드 생략: {str(e)[:80]}")
        return None


def _infographic(data):
    """보유 현황 인포그래픽 → image 블록. 표는 모바일에서 열이 잘려 이미지로 간다(사용자 요청).
    업로드는 신버전 API, 삽입은 구버전(after 지원) — dashboard 가 처리."""
    try:
        import sys
        sys.path.insert(0, os.path.join(_DIR, "viz"))
        import holdings as HV
        png = os.path.join(_DIR, "latest_holdings.png")
        HV.render(data, png)
        with open(png, "rb") as f:
            fid = D.upload_image(f.read(), "holdings.png")
        return ({"object": "block", "type": "image",
                 "image": {"type": "file_upload", "file_upload": {"id": fid}}} if fid else None)
    except Exception as e:
        M.log(f"  ⚠️ 인포그래픽 생략: {str(e)[:80]}")
        return None


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

    header = []
    img = _infographic(data)
    if img:
        header.append(img)
    header.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": [
        {"type": "text", "text": {"content": "종목을 펼치면 AI 심층분석 · 분기실적 · 수급이 나옵니다. "
                                             "분석은 AI 검색 추정으로 확정 사실이 아님 · 투자판단 보조용"},
         "annotations": {"color": "gray", "italic": True}}]}})

    items = []
    for row, pos in sorted(rows, key=lambda x: -x[1]["eval"]):
        code = row["code"]
        a = analysis.get(code, {})
        card = _card(row, pos, a, flows.get(code) or {})
        if card:
            items.append((card, []))          # 카드(요약) → 바로 아래 토글(원문 8섹션)
        tog = _holding_toggle(row, pos, a, flows.get(code) or {}, roes.get(code))
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
