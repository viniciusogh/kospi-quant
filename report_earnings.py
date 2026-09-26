"""본문과 분기표가 함께 사용하는 실적 기준. 네트워크/모델 호출 없음.

입력은 dart_earnings의 공식 자료 대조 결과다. 이전 분기/한투 데이터로
실패를 대체하지 않으며, 서로 다른 기간의 실제 실적과 전망을 섞지 않는다.
"""
from datetime import date
import hashlib
import json
import math
import re

VERSION = 1
ANALYSIS_FORMAT = 3
META_KEYS = {"earnings_version", "earnings_fingerprint", "earnings_period", "analysis_status", "earnings"}


def normalize(rows):
    if not rows:
        return []
    out, seen = [], set()
    for row in rows:
        q = str(row["q"])
        if not re.fullmatch(r"\d{4}(03|06|09|12)", q) or q in seen:
            raise ValueError("실적 분기 형식/중복 오류")
        date(int(q[:4]), int(q[4:]), 1)
        seen.add(q)
        values = {}
        for key in ("sale", "op"):
            if isinstance(row[key], bool):
                raise ValueError("실적 숫자 형식 오류")
            value = float(row[key])
            if not math.isfinite(value):
                raise ValueError("실적 숫자 누락/비유한 값")
            values[key] = value
        if values["sale"] < 0:
            raise ValueError("실적 매출 음수")
        out.append({"q": q, **values})
    return sorted(out, key=lambda x: x["q"])


def snapshot(data, asof):
    """실적 출처/분기/정정 값까지 캐시 지문에 포함. 조회일은 생성일과 분리."""
    day = date.fromisoformat(str(asof)[:10])
    data = data or {}
    status = "missing"
    rows, period = [], "미확인"
    if data.get("status") == "verified":
        if (data.get("asof") != day.isoformat() or not data.get("sources")
                or not data.get("receipt") or not data.get("checked_at")
                or data.get("basis") not in ("연결", "별도")
                or type(data.get("provisional")) is not bool
                or data.get("cumulative") is not False):
            raise ValueError("실적 출처/기준일/구분 미확인")
        rows = normalize(data["rows"])
        period = data["period"]
        if not rows or rows[-1]["q"] != period or period >= day.strftime("%Y%m"):
            raise ValueError("실적 기간 미확인")
        if date.fromisoformat(data["published_on"]) > day:
            raise ValueError("기준일 이후 공시")
        status = "current"
    identity = {k: data.get(k) for k in ("receipt", "basis", "provisional", "corrected", "sources")}
    fingerprint = hashlib.sha256(json.dumps(
        {"version": VERSION, "rows": rows, "status": status, **identity},
        sort_keys=True, allow_nan=False).encode()).hexdigest()
    return {"rows": rows, "period": period, "status": status,
            "asof": day.isoformat(), "fingerprint": fingerprint,
            "source": {k: data.get(k) for k in ("basis", "provisional", "corrected", "receipt", "published_on",
                                                "checked_at", "coverage", "sources", "reason")}}


def metadata(e):
    return {"earnings_version": VERSION, "earnings_fingerprint": e["fingerprint"],
            "earnings_period": e["period"]}


def matches(cached, e):
    return bool(cached and cached.get("fmt") == ANALYSIS_FORMAT
                and cached.get("analysis_status") == "ok"
                and e["status"] == "current"
                and all(cached.get(k) == v for k, v in metadata(e).items()))


def _change(current, previous, *, profit=False):
    if profit:
        if previous <= 0 < current:
            return "흑자전환" if previous < 0 else "흑자 발생"
        if previous >= 0 > current:
            return "적자전환"
        if previous < 0 and current < 0:
            return "적자 축소" if current > previous else ("적자 확대" if current < previous else "적자 유지")
    if previous <= 0:
        return "증감률 산출 불가"
    return f"{(current / previous - 1) * 100:+.1f}%"


def format_amount(value):
    return f"{value:,.2f}" if abs(value) < 10 else f"{value:,.0f}"


def sections(e):
    if e["status"] != "current":
        return {"실적결론": "최신 실적 확인 중", "실적근거": ""}
    latest = e["rows"][-1]
    q = latest["q"]
    label = f"{q[:4]}년 {int(q[4:]) // 3}분기"
    prior_q = str(int(q[:4]) - 1) + q[4:]
    prior = next((x for x in e["rows"] if x["q"] == prior_q), None)
    parts = []
    for key, name in (("sale", "매출"), ("op", "영업이익")):
        change = ("전년 동기 대비 " + _change(latest[key], prior[key], profit=key == "op")
                  if prior else "전년 동기 자료 미확인")
        amount = format_amount(latest[key])
        parts.append(f"{name} :: {amount}억원 · {change}")
    if latest["sale"] > 0:
        margin = latest["op"] / latest["sale"] * 100
        detail = f"{margin:.2f}%"
        if prior and prior["sale"] > 0:
            prior_margin = prior["op"] / prior["sale"] * 100
            detail += f" · 전년 동기 대비 {margin - prior_margin:+.2f}%p"
        parts.append(f"영업이익률 :: {detail}")
    # 결론에도 기간을 강제하고 방향은 동일 표의 동기 값에서만 계산한다.
    conclusion = label + (" 잠정" if e["source"]["provisional"] else "")
    if prior:
        sales = "증가" if latest["sale"] > prior["sale"] else ("감소" if latest["sale"] < prior["sale"] else "유지")
        profit = _change(latest["op"], prior["op"], profit=True)
        if profit.endswith("%"):
            profit = "증가" if latest["op"] > prior["op"] else ("감소" if latest["op"] < prior["op"] else "유지")
        conclusion += f" · 전년 동기 대비 매출 {sales} / 영업이익 {profit}"
    return {"실적결론": conclusion, "실적근거": " || ".join(parts)}


def provenance(e):
    if e["status"] != "current":
        return ""
    s = e["source"]
    q = e["period"]
    kind = "잠정" if s["provisional"] else "정기보고서"
    if s.get("corrected"):
        kind += " · 정정 반영"
    checked = str(s["checked_at"]).replace("T", " ")[:16]
    return (f"회사 공시 기준 · {q[:4]}년 {int(q[4:]) // 3}분기 {s['basis']} · {kind}\n"
            f"{s['published_on']} 공시 · {e['asof']}까지 DART 공시·정정 확인 · 조회 {checked} KST\n"
            "공시 원문 수치 → 전년 같은 분기 비교·이익률 계산")


def prompt_context(e):
    return (f"보고서 기준일: {e['asof']}\n"
            f"실적 기준 분기: {e['period']} (상태: {e['status']})\n"
            "분기표와 동일한 단일분기 실적(억원): "
            + json.dumps(e["rows"], ensure_ascii=False, allow_nan=False)
            + "\n계산된 실적 설명: " + json.dumps(sections(e), ensure_ascii=False)
            + "\n출처·기준: " + provenance(e)
            + "\n최근 실적 판단은 이 기준 분기와 전년 같은 분기만 비교한다. "
            "검색한 과거 분기의 성장률을 최근 흐름으로 쓰지 않는다. "
            "누적/단일분기, 연결/별도, 실제/전망의 기준을 혼합하지 않는다. "
            "검색 자료가 제공 표와 충돌하면 원인을 확인하기 전 수치를 대체하지 않는다. "
            "미확인/오래된 실적이면 최근 매출·이익의 증가나 감소를 단정하지 않는다. "
            "촉매·위험 해석은 유지하되 전망은 대상 기간과 전망임을 명시한다.\n")
