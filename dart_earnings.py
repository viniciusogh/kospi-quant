"""공식 공시 기준 실적 수집. 실패는 이전 실적/한투 값으로 대체하지 않는다.

DART 목록 → 접수번호 → 정기 재무 API+원문 대조 또는 잠정 공시 원문.
실행별 새 목록 조회, 원본과 가공 근거는 private audit에 보존한다.
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
import os
from pathlib import Path
import re
import zipfile
from lxml import html
import requests

KST = timezone(timedelta(hours=9))
VERSION = 1
ROOT = Path(__file__).resolve().parent
ACCOUNTS = {"sale": "ifrs-full_Revenue", "op": "dart_OperatingIncomeLoss"}
REGULAR = re.compile(r"(?:\[[^]]+\])*(사업|반기|분기)보고서\s*\((\d{4})\.(\d{2})\)")


class EarningsError(ValueError):
    pass


def now():
    return datetime.now(KST).isoformat(timespec="seconds")


def number(text):
    s = str(text).strip().replace(",", "").replace("−", "-")
    if re.fullmatch(r"\([\d.]+\)", s):
        s = "-" + s[1:-1]
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", s):
        raise EarningsError("실적 숫자 미확인")
    try:
        v = Decimal(s)
    except InvalidOperation:
        raise EarningsError("실적 숫자 형식 오류") from None
    if not v.is_finite():
        raise EarningsError("실적 숫자 비유한 값")
    return v


def document_tree(raw):
    # 거래소 원문은 UTF-8 바이트인데 meta에 euc-kr이라고 적힌 경우가 있다.
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("euc-kr")
    text = re.sub(r"<\?xml[^>]*\?>", "", text)
    return html.fromstring(text)


def text_of(node):
    return " ".join(" ".join(node.itertext()).split())


def row(q, sale, op):
    if sale < 0:
        raise EarningsError("매출 음수")
    return {"q": q, "sale": float(sale / Decimal(100000000)),
            "op": float(op / Decimal(100000000)),
            "sale_krw": str(sale), "op_krw": str(op)}


def regular_period(filing):
    m = REGULAR.fullmatch(filing["report_nm"].strip())
    if not m or m[3] not in ("03", "06", "09", "12"):
        raise EarningsError("정기보고서 기간 미확인")
    return m[2] + m[3]


def parse_regular(api, raw, filing, fs_div, *, cumulative=False):
    """DART 3개월 금액을 그대로 사용. 누적에서 임의로 차감하지 않는다."""
    q = regular_period(filing)
    month = q[4:]
    annual = month == "12"
    if annual and not cumulative:
        raise EarningsError("연간 실적을 단일분기로 사용 불가")
    data = api.get("list") or []
    if api.get("status") != "000" or not data:
        raise EarningsError("정기 재무 응답 미확인")
    expected_report = {"03": "11013", "06": "11012", "09": "11014", "12": "11011"}[month]
    if any(x.get("rcept_no") != filing["rcept_no"] or x.get("reprt_code") != expected_report
           or x.get("corp_code") != filing["corp_code"] for x in data):
        raise EarningsError("재무 API와 선택 공시 불일치")
    tree = document_tree(raw)
    axis = ("_ifrs-full_ConsolidatedAndSeparateFinancialStatementsAxis_ifrs-full_"
            + ("ConsolidatedMember" if fs_div == "CFS" else "SeparateMember"))
    suffix = {"03": "dFQA" if cumulative else "dFQQ", "06": "dHYA" if cumulative else "dHYQ",
              "09": "dTQA" if cumulative else "dTQQ", "12": "dFY"}[month]
    # 원문 컨텍스트 네이밍을 확인할 수 없는 양식은 실패로 남긴다.
    cols = [("current", ("thstrm_amount" if annual or not cumulative else "thstrm_add_amount"), "CFY" + q[:4]),
            ("prior", ("frmtrm_amount" if annual else "frmtrm_add_amount" if cumulative else "frmtrm_q_amount"), "PFY" + str(int(q[:4]) - 1))]
    amounts = {"current": {}, "prior": {}}
    evidence = []
    for field, account in ACCOUNTS.items():
        rows = [x for x in data if x.get("account_id") == account and x.get("sj_div") in ("IS", "CIS")]
        if len(rows) != 1 or rows[0].get("currency") != "KRW":
            raise EarningsError("재무 계정/통화 미확인")
        entry = rows[0]
        for side, key, prefix in cols:
            value = number(entry.get(key, ""))
            context = prefix + suffix + axis
            candidates = tree.xpath("//te[@acode=$account and @acontext=$context]", account=account, context=context)
            matched = []
            for cell in candidates:
                table = cell.xpath("ancestor::table[1]")
                if not table:
                    continue
                # 바로 앞의 표제만 사용한다. 앞 재무표 너머의 단위를
                # 주석 표에 상속하면 같은 컨텍스트의 천원 값을 오독한다.
                previous = table[0].getprevious()
                if previous is None or previous.xpath(".//te[@acode]"):
                    continue
                caption = text_of(previous)
                unit = re.search(r"단위\s*:\s*(원|천원|백만원)\s*\)", caption)
                if not unit or not re.search("손익계산서", caption):
                    continue
                if ("연결" in caption) != (fs_div == "CFS"):
                    continue
                scale = {"원": 1, "천원": 1000, "백만원": 1000000}[unit[1]]
                if number(text_of(cell)) * scale != value:
                    raise EarningsError("재무 API와 원문 숫자 충돌")
                matched.append({"source_value": text_of(cell), "source_unit": unit[1], "scale_to_krw": scale})
            if not matched:
                raise EarningsError("공시 원문 계정/기간/단위 대조 실패")
            amounts[side][field] = value
            evidence.append({"field": field, "side": side, "account": account, "api_field": key,
                             "context": context, "value_krw": str(value), "unit": "원", "cells": matched})
    previous = str(int(q[:4]) - 1) + month
    return {"period": q, "rows": [row(previous, **amounts["prior"]), row(q, **amounts["current"])],
            "basis": "연결" if fs_div == "CFS" else "별도", "provisional": False,
            "evidence": evidence, "cumulative": cumulative}


def parse_provisional(raw, filing):
    """거래소 표준 잠정 공시: 정정 전/후 요약표 대신 본문 실적 표만 읽는다."""
    tree = document_tree(raw)
    trs = [[text_of(c) for c in tr.xpath("./td|./th")] for tr in tree.xpath("//tr")]
    periods = {}
    for label in ("당기실적", "전년동기실적"):
        matches = [r for r in trs if len(r) == 4 and r[0] == label and r[2] == "~"]
        if len(matches) != 1:
            raise EarningsError("잠정 실적기간 미확인")
        begin, end = date.fromisoformat(matches[0][1]), date.fromisoformat(matches[0][3])
        if not (begin.day == 1 and (end - begin).days in range(89, 93)
                and begin.month in (1, 4, 7, 10) and end.month == begin.month + 2
                and (end + timedelta(days=1)).day == 1):
            raise EarningsError("잠정 단일분기 기간 미확인")
        periods[label] = (begin, end)
    current, prior = periods["당기실적"], periods["전년동기실적"]
    if (current[0].year - prior[0].year != 1 or current[0].month != prior[0].month
            or current[1].month != prior[1].month):
        raise EarningsError("잠정 전년 동기 불일치")
    units = [r[1] for r in trs if len(r) == 2 and re.fullmatch(r"1\.\s*(?:연결)?실적내용", r[0])]
    if len(units) != 1:
        raise EarningsError("잠정 실적 단위 미확인")
    m = re.fullmatch(r"(?:구분\s*)?\(?단위\s*:\s*(원|천원|백만원|억원|조원)\s*,\s*%\)?", units[0])
    if not m:
        raise EarningsError("잠정 실적 통화/단위 미확인")
    scale = {"원": 1, "천원": 1000, "백만원": 1000000, "억원": 100000000, "조원": 1000000000000}[m[1]]
    values = {"current": {}, "prior": {}}
    evidence = []
    # 헤더에서 현재/전년 위치를 검증. 흑적 전환 열이 있는 표준 양식만 허용.
    header = [r for r in trs if r == ["구분", "당기실적", "전기실적", "전기대비", "전년동기실적", "전년동기대비"]]
    if len(header) != 1:
        raise EarningsError("잠정 실적 비교 열 미확인")
    for field, label in (("sale", "매출액"), ("op", "영업이익")):
        found = [r for r in trs if len(r) == 9 and r[:2] == [label, "당해실적"]]
        if len(found) != 1:
            raise EarningsError("잠정 실적 수치 미확인")
        r = found[0]
        for side, index in (("current", 2), ("prior", 6)):
            value = number(r[index]) * scale
            values[side][field] = value
            evidence.append({"field": field, "side": side, "row": r, "column": index,
                             "unit": m[1], "scale_to_krw": scale, "value_krw": str(value)})
    q, oldq = current[1].strftime("%Y%m"), prior[1].strftime("%Y%m")
    return {"period": q, "rows": [row(oldq, **values["prior"]), row(q, **values["current"])],
            "basis": "연결" if "연결" in filing["report_nm"] else "별도",
            "provisional": True, "cumulative": False, "evidence": evidence}


def missing(code, asof, reason):
    return {"version": VERSION, "status": "unavailable", "code": code, "asof": asof,
            "checked_at": now(), "reason": reason, "rows": [], "sources": []}


class Client:
    def __init__(self, api_key=None, *, audit_root=None, session=None):
        self.key = api_key if api_key is not None else os.environ.get("DART_API_KEY", "")
        self.http = session or requests.Session()
        self.audit_root = Path(audit_root or ROOT / ".recommendation_audits" / "earnings")
        self.corps = None
        self.documents = {}
        self.financial_responses = {}

    def request(self, endpoint, params):
        if not self.key:
            raise EarningsError("공시 연결 설정 미확인")
        try:
            r = self.http.get("https://opendart.fss.or.kr/api/" + endpoint,
                              params={"crtfc_key": self.key, **params}, timeout=(5, 20))
            r.raise_for_status()
            if len(r.content) > 30_000_000:
                raise EarningsError("공시 응답 크기 초과")
            return r
        except requests.RequestException:
            # 예외/URL에는 crtfc_key가 들어 있으므로 그대로 출력·저장하지 않는다.
            raise EarningsError("공시 서비스 통신 실패") from None

    def api(self, endpoint, params, *, empty_ok=False):
        try:
            data = self.request(endpoint, params).json()
        except (ValueError, TypeError):
            raise EarningsError("공시 응답 형식 오류") from None
        if data.get("status") == "013" and empty_ok:
            return data
        if data.get("status") != "000":
            raise EarningsError("공시 서비스 응답 오류 " + str(data.get("status", "미확인"))[:3])
        return data

    def unzip(self, raw, *, filename=None, limit=60_000_000):
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                names = [x for x in archive.infolist() if x.filename.lower().endswith(".xml")]
                if filename:
                    names = [x for x in names if x.filename == filename]
                if len(names) != 1 or names[0].file_size > limit:
                    raise EarningsError("공시 원문 파일 범위 미확인")
                return archive.read(names[0])
        except zipfile.BadZipFile:
            raise EarningsError("공시 원문 다운로드 실패") from None

    def corporation(self, code):
        if self.corps is None:
            raw = self.unzip(self.request("corpCode.xml", {}).content)
            from xml.etree import ElementTree
            root = ElementTree.fromstring(raw)
            self.corps = {}
            for item in root:
                stock = (item.findtext("stock_code") or "").strip()
                if stock:
                    self.corps.setdefault(stock, []).append(item.findtext("corp_code"))
        values = self.corps.get(code, [])
        if len(values) != 1:
            raise EarningsError("공시 기업 식별 미확인")
        return values[0]

    def listings(self, corp, asof):
        # 정기/거래소 공시는 실적 발표와 정정을 포함한다. 대량 지분공시가
        # 있는 대기업에서도 이 두 범위의 모든 페이지를 끝까지 확인한다.
        rows = self._listings_type(corp, asof, "A") + self._listings_type(corp, asof, "I")
        if len({x["rcept_no"] for x in rows}) != len(rows):
            raise EarningsError("공시 유형 간 접수번호 중복")
        return rows

    def _listings_type(self, corp, asof, kind):
        day = date.fromisoformat(asof)
        params = {"corp_code": corp, "bgn_de": (day - timedelta(days=550)).strftime("%Y%m%d"),
                  "end_de": day.strftime("%Y%m%d"), "page_count": 100, "last_reprt_at": "N",
                  "pblntf_ty": kind}
        rows, total = [], None
        for page in range(1, 11):
            data = self.api("list.json", {**params, "page_no": page}, empty_ok=True)
            if data.get("status") == "013":
                if page != 1:
                    raise EarningsError("공시 목록 중간 페이지 누락")
                return []
            if total is not None and total != int(data["total_count"]):
                raise EarningsError("조회 중 공시 목록 변경")
            total = int(data["total_count"])
            batch = data.get("list", [])
            if not batch or int(data["page_no"]) != page:
                raise EarningsError("공시 목록 불완전")
            rows.extend(batch)
            if page == int(data["total_page"]):
                if len(rows) != total or len({x["rcept_no"] for x in rows}) != total:
                    raise EarningsError("공시 목록 누락/중복")
                if any(x.get("corp_code") != corp or x["rcept_dt"] > params["end_de"] for x in rows):
                    raise EarningsError("공시 기업/조회일 불일치")
                return rows
        raise EarningsError("공시 목록 조회 범위 초과")

    def document(self, receipt):
        if receipt not in self.documents:
            self.documents[receipt] = self.unzip(self.request("document.xml", {"rcept_no": receipt}).content,
                                                 filename=receipt + ".xml")
        return self.documents[receipt]

    def regular(self, filing, *, cumulative=False):
        q = regular_period(filing)
        params = {"corp_code": filing["corp_code"], "bsns_year": q[:4],
                  "reprt_code": {"03": "11013", "06": "11012", "09": "11014", "12": "11011"}[q[4:]]}
        data = self.api("fnlttSinglAcntAll.json", {**params, "fs_div": "CFS"}, empty_ok=True)
        basis = "CFS"
        # 연결 자료 없음(013)일 때만 별도를 조사한다. 통신 실패를 기준 변경으로 숨기지 않는다.
        if data.get("status") == "013":
            data = self.api("fnlttSinglAcntAll.json", {**params, "fs_div": "OFS"})
            basis = "OFS"
        self.financial_responses[filing["rcept_no"]] = data
        raw = self.document(filing["rcept_no"])
        result = parse_regular(data, raw, filing, basis, cumulative=cumulative)
        return result, data

    def collect(self, code, asof):
        """실행마다 목록을 새로 확인. 새 실적 공시를 못 읽으면 이전 값으로 대체 금지."""
        audit = {"code": code, "asof": asof, "started_at": now()}
        selected = set()
        try:
            date.fromisoformat(asof)
            if not re.fullmatch(r"\d{6}", code):
                raise EarningsError("종목코드 형식 오류")
            corp = self.corporation(code)
            listings = self.listings(corp, asof)
            audit["listings"] = listings
            regulars = [x for x in listings if REGULAR.fullmatch(x["report_nm"].strip())]
            latest = max(regulars, key=lambda x: (regular_period(x), x["rcept_no"]), default=None)
            # 같은 날 정정은 접수번호순. 정기보고서보다 새 실적 발표를 우선 확인.
            announcements = [x for x in listings if ("잠정" in x["report_nm"] or "영업실적" in x["report_nm"])
                             and "전망" not in x["report_nm"]
                             and (latest is None or x["rcept_no"] > latest["rcept_no"])]
            if announcements:
                regular_q = regular_period(latest) if latest else None
                latest = max(announcements, key=lambda x: x["rcept_no"])
                selected.add(latest["rcept_no"])
                raw = self.document(latest["rcept_no"])
                result = parse_provisional(raw, latest)
                if regular_q and result["period"] < regular_q:
                    raise EarningsError("새 잠정 공시와 최신 정기보고서 기간 역전")
                audit["financial_api"] = None
            elif latest:
                selected.add(latest["rcept_no"])
                q = regular_period(latest)
                if q.endswith("12"):
                    annual, api = self.regular(latest, cumulative=True)
                    third = [x for x in regulars if regular_period(x) == q[:4] + "09"]
                    if not third:
                        raise EarningsError("4분기 계산용 3분기 누적 미확인")
                    prior_filing = max(third, key=lambda x: x["rcept_no"])
                    selected.add(prior_filing["rcept_no"])
                    nine, prior_api = self.regular(prior_filing, cumulative=True)
                    if annual["basis"] != nine["basis"]:
                        raise EarningsError("연간/3분기 연결 기준 불일치")
                    result = dict(annual)
                    result["rows"] = [row(a["q"], number(a["sale_krw"]) - number(b["sale_krw"]),
                                          number(a["op_krw"]) - number(b["op_krw"]))
                                      for a, b in zip(annual["rows"], nine["rows"])]
                    result["cumulative"] = False
                    result["evidence"] = annual["evidence"] + nine["evidence"]
                    result["calculation"] = "연간 누적 - 같은 연도 3분기 누적"
                    audit["financial_api"] = [api, prior_api]
                else:
                    result, audit["financial_api"] = self.regular(latest)
            else:
                raise EarningsError("최신 실적 공시 미확인")
            if result["period"] >= asof.replace("-", "")[:6]:
                raise EarningsError("아직 끝나지 않은 실적 기간")
            receipt = latest["rcept_no"]
            result.update({"version": VERSION, "status": "verified", "code": code, "asof": asof,
                           "checked_at": now(), "published_on": datetime.strptime(latest["rcept_dt"], "%Y%m%d").date().isoformat(),
                           "receipt": receipt, "report_name": latest["report_nm"].strip(),
                           "corrected": "정정" in latest["report_nm"],
                           "sources": [{"receipt": r, "url": "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + r,
                                        "sha256": hashlib.sha256(b).hexdigest()}
                                       for r, b in self.documents.items() if r in selected],
                           "coverage": "DART 정기·거래소 공시 목록·정정 확인"})
        except Exception as exc:
            reason = str(exc) if isinstance(exc, EarningsError) else "공시 자료 검증 실패"
            result = missing(code, asof, reason)
        audit["result"] = result
        audit["selected_receipts"] = sorted(selected)
        audit["raw_financial_responses"] = {r: self.financial_responses[r] for r in selected
                                            if r in self.financial_responses}
        # 조회·가공 근거 저장 실패도 검증 완료로 내보내지 않는다.
        try:
            folder = self.audit_root / (datetime.now(KST).strftime("%Y%m%dT%H%M%S%f") + "-" + code)
            folder.mkdir(parents=True, mode=0o700)
            for receipt, raw in self.documents.items():
                if receipt in selected:
                    (folder / (receipt + ".xml")).write_bytes(raw)
            (folder / "audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            return missing(code, asof, "공시 검증 기록 저장 실패")
        return result
