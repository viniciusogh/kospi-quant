"""공시 원문 실제 표본과 실패/캐시 경계. 네트워크·LLM·게시·주문 없음."""
from copy import deepcopy
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import pandas as pd

os.environ.setdefault("APP_KEY", "test")
os.environ.setdefault("APP_SECRET", "test")
import dart_earnings as dart
import report_earnings as report
import momentum_daily as momentum

FIX = Path(__file__).parent / "tests/fixtures/earnings"
ASOF = "2026-09-26"
FILING = {"corp_code": "00415646", "rcept_no": "20260814004101", "rcept_dt": "20260814",
          "report_nm": "반기보고서 (2026.06)"}
PROVISIONAL = {"corp_code": "00126380", "rcept_no": "20260730800103", "rcept_dt": "20260730",
               "report_nm": "[기재정정]연결재무제표기준영업(잠정)실적(공정공시)"}


def raw():
    return (FIX / "dreamtech-half-income.html").read_bytes()


def api():
    return json.loads((FIX / "dreamtech-half-api.json").read_text())


def verified():
    return {**dart.parse_regular(api(), raw(), FILING, "CFS"), "status": "verified", "asof": ASOF,
            "code": "192650", "receipt": FILING["rcept_no"], "published_on": "2026-08-14",
            "checked_at": "2026-09-26T17:55:12+09:00", "corrected": False,
            "sources": [{"receipt": FILING["rcept_no"], "sha256": "fixture"}]}


class EarningsTests(unittest.TestCase):
    def setUp(self):
        p = patch("requests.sessions.Session.request", side_effect=AssertionError("network prohibited"))
        p.start(); self.addCleanup(p.stop)

    def test_actual_dreamtech_period_and_yoy_replace_old_growth(self):
        data = verified(); snapshot = report.snapshot(data, ASOF)
        self.assertAlmostEqual(data["rows"][-1]["sale"], 2573.36437897)
        self.assertAlmostEqual(data["rows"][-1]["op"], 1.04648418)
        section = report.sections(snapshot)
        self.assertIn("2026년 2분기", section["실적결론"])
        self.assertIn("매출 감소", section["실적결론"])
        self.assertIn("-11.7%", section["실적근거"])
        self.assertIn("-98.8%", section["실적근거"])
        self.assertIn("0.04%", section["실적근거"])
        self.assertNotIn("+20%", str(section))

    def test_cumulative_amount_cannot_replace_quarter(self):
        bad = api(); bad["list"][0]["thstrm_amount"] = bad["list"][0]["thstrm_add_amount"]
        with self.assertRaisesRegex(dart.EarningsError, "숫자 충돌"):
            dart.parse_regular(bad, raw(), FILING, "CFS")

    def test_actual_q1_and_annual_minus_nine_months(self):
        def load(q):
            prefix = FIX / ('dreamtech-' + q)
            return (json.loads(Path(str(prefix) + '-api.json').read_text()),
                    Path(str(prefix) + '-income.html').read_bytes(),
                    json.loads(Path(str(prefix) + '-filing.json').read_text()))
        q1 = dart.parse_regular(*load('202603'), 'CFS')
        self.assertEqual(q1['period'], '202603')
        annual = dart.parse_regular(*load('202512'), 'CFS', cumulative=True)
        nine = dart.parse_regular(*load('202509'), 'CFS', cumulative=True)
        with tempfile.TemporaryDirectory() as tmp:
            c = dart.Client('test', audit_root=tmp)
            filings = [load(q)[2] for q in ('202512', '202509')]
            with patch.object(c, 'corporation', return_value='00415646'), patch.object(c, 'listings', return_value=filings), patch.object(c, 'regular', side_effect=[(annual, {}), (nine, {})]):
                result = c.collect('192650', '2026-03-31')
            self.assertEqual(result['status'], 'verified')
            self.assertFalse(result['cumulative'])
            self.assertAlmostEqual(result['rows'][-1]['sale'], annual['rows'][-1]['sale'] - nine['rows'][-1]['sale'])
            self.assertEqual(result['rows'][-1]['q'], '202512')

    def test_document_zip_chooses_main_receipt_not_audit_attachment(self):
        import io, zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as archive:
            archive.writestr('20260318001215_00761.xml', 'attachment')
            archive.writestr('20260318001215.xml', 'main')
        self.assertEqual(dart.Client('test').unzip(buf.getvalue(), filename='20260318001215.xml'), b'main')
        with self.assertRaises(dart.EarningsError):
            dart.Client('test').unzip(buf.getvalue(), filename='missing.xml')

    def test_table_and_prose_share_amount_precision(self):
        e = report.snapshot(verified(), ASOF)
        self.assertIn('1.05억원', report.sections(e)['실적근거'])
        self.assertIn('1.05', json.dumps(momentum._quarter_table(e['rows'])))

    def test_actual_thousand_and_million_won_statements(self):
        for code, unit in (('103590', '천원'), ('000150', '백만원')):
            with self.subTest(code=code):
                data = json.loads((FIX / (code + '-api.json')).read_text())
                filing = json.loads((FIX / (code + '-filing.json')).read_text())
                source = (FIX / (code + '-income.html')).read_bytes()
                result = dart.parse_regular(data, source, filing, 'CFS')
                self.assertEqual(result['evidence'][0]['cells'][0]['source_unit'], unit)
                revenue = next(x for x in data['list'] if x['account_id'] == 'ifrs-full_Revenue')
                self.assertEqual(result['rows'][-1]['sale_krw'], revenue['thstrm_amount'])
                wrong_unit = source.replace(('단위 : ' + unit).encode(), '단위 : 원'.encode())
                self.assertNotEqual(wrong_unit, source)
                with self.assertRaises(dart.EarningsError):
                    dart.parse_regular(data, wrong_unit, filing, 'CFS')

    def test_receipt_currency_and_unit_conflicts_fail(self):
        for field, value in (("rcept_no", "20260927000001"), ("currency", "USD"), ("corp_code", "00126380")):
            bad = api(); bad["list"][0][field] = value
            with self.subTest(field=field), self.assertRaises(dart.EarningsError):
                dart.parse_regular(bad, raw(), FILING, "CFS")
        with self.assertRaises(dart.EarningsError):
            dart.parse_regular(api(), raw().replace("단위 : 원".encode(), "단위 : 천원".encode()), FILING, "CFS")

    def test_same_context_in_notes_does_not_override_income_table(self):
        note = b'<table><tr><te acode="ifrs-full_Revenue" acontext="CFY2026dHYQ_ifrs-full_ConsolidatedAndSeparateFinancialStatementsAxis_ifrs-full_ConsolidatedMember">257,336,438</te></tr></table>'
        result = dart.parse_regular(api(), raw() + note, FILING, "CFS")
        self.assertEqual(result["rows"][-1]["sale_krw"], "257336437897")

    def test_actual_corrected_provisional_and_unit_conversion(self):
        result = dart.parse_provisional((FIX / "samsung-provisional.html").read_bytes(), PROVISIONAL)
        self.assertTrue(result["provisional"])
        self.assertEqual(result["period"], "202606")
        self.assertEqual(result["basis"], "연결")
        self.assertEqual(result["rows"][-1]["sale_krw"], "171500000000000.00")
        self.assertEqual(result["rows"][-1]["op_krw"], "89490000000000.00")
        self.assertEqual(result["rows"][0]["sale_krw"], "74570000000000.00")

    def test_provisional_missing_amount_and_wrong_period_fail(self):
        s = (FIX / "samsung-provisional.html").read_text()
        for bad in (s.replace("171.50", "-"), s.replace("2026-04-01", "2026-01-01"), s.replace("전년동기실적", "기준 미상")):
            with self.assertRaises(dart.EarningsError):
                dart.parse_provisional(bad.encode(), PROVISIONAL)

    def test_provenance_always_visible_and_provisional_label(self):
        data = verified();data["provisional"] = True;data["corrected"] = True
        e = report.snapshot(data, ASOF)
        line = report.provenance(e)
        for expected in ("회사 공시 기준", "연결", "잠정", "정정 반영", "2026-08-14", "2026-09-26", "17:55"):
            self.assertIn(expected, line)
        blocks = momentum._sections({**report.sections(e), "earnings": e})
        paragraphs = [b for b in blocks if b["type"] == "paragraph"]
        self.assertTrue(any("회사 공시 기준" in json.dumps(b, ensure_ascii=False) for b in paragraphs))
        self.assertFalse(any(b["type"] == "toggle" for b in blocks))

    def test_unknown_data_no_stale_actuals(self):
        data = {"status": "unavailable", "rows": [{"q": "202503", "sale": 300, "op": 20}]}
        e = report.snapshot(data, ASOF)
        self.assertEqual(e["rows"], [])
        self.assertEqual(report.sections(e), {"실적결론": "최신 실적 확인 중", "실적근거": ""})

    def test_snapshot_rejects_future_and_inconsistent_proof(self):
        for key, value in (("asof", "2026-09-27"), ("published_on", "2026-09-27"), ("cumulative", True), ("sources", [])):
            d = verified();d[key] = value
            with self.assertRaises(ValueError):
                report.snapshot(d, ASOF)

    def test_income_change_and_correction_invalidate_cache_even_same_quarter(self):
        data = verified();e = report.snapshot(data, ASOF)
        cached = {"fmt": report.ANALYSIS_FORMAT, "analysis_status": "ok", **report.metadata(e)}
        self.assertTrue(report.matches(cached, e))
        data["checked_at"] = "2026-09-26T18:00:00+09:00"
        self.assertTrue(report.matches(cached, report.snapshot(data, ASOF)))
        data["rows"][-1]["sale"] += 1
        self.assertFalse(report.matches(cached, report.snapshot(data, ASOF)))
        data = verified();data["receipt"] = "20260925000001"
        self.assertFalse(report.matches(cached, report.snapshot(data, ASOF)))
        self.assertFalse(report.matches({**cached, "fmt": 2}, e))

    def test_loss_transitions_are_not_misleading_growth_percentages(self):
        self.assertEqual(report._change(10, -10, profit=True), "흑자전환")
        self.assertEqual(report._change(-5, -10, profit=True), "적자 축소")
        self.assertEqual(report._change(-10, 10, profit=True), "적자전환")

    def test_pruning_keeps_earnings_proof(self):
        e = report.snapshot(verified(), ASOF)
        cached = {"date": datetime.now(momentum.KST).date().isoformat(), "earnings": e, **report.metadata(e)}
        self.assertEqual(momentum.prune_cache({"192650": cached})["192650"], cached)

    def test_generation_failure_does_not_revive_legacy_growth(self):
        frame = pd.DataFrame([{"code": "192650", "종목명": "드림텍"}])
        old = {"fmt": 2, "한줄": "매출 성장", "실적결론": "매출 증가", "실적근거": "2025년 1분기 +20%", "full": ASOF}
        cache = {"192650": deepcopy(old)}
        with patch.dict(os.environ, {"GEMINI_API_KEY": ""}):
            output = momentum.gemini_analyze(frame, {}, {}, cache, earnings={"192650": verified()}, asof=ASOF)["192650"]
        self.assertNotIn("+20%", str(output));self.assertEqual(output["한줄"], "")
        self.assertIn("매출 감소", output["실적결론"])
        self.assertNotIn("full", output)

    def test_missing_official_data_still_renders_verified_flows_and_price(self):
        frame = pd.DataFrame([{"code": "192650", "종목명": "드림텍"}])
        with patch.dict(os.environ, {"GEMINI_API_KEY": ""}):
            a = momentum.gemini_analyze(frame, {}, {}, {}, asof=ASOF)["192650"]
        self.assertEqual(a["실적결론"], "최신 실적 확인 중")
        self.assertIsNotNone(momentum._supply_bars({"frgn5": -70, "orgn5": 226, "prsn5": -155}))

    def test_model_receives_current_actuals_and_wrong_basis_is_rejected(self):
        client = Mock();client.models.generate_content.return_value.text = "실적기준분기: 202503\n한줄: 매출 성장"
        r = {"code": "192650", "종목명": "드림텍", "섹터": "전기·전자", "hi60": 1,
             "per": 8450, "pbr": 1.5, "ret20": .61}
        e = report.snapshot(verified(), ASOF)
        with self.assertRaisesRegex(ValueError, "분기 불일치"):
            momentum._gemini_full(client, r, {}, -2.6, earnings=e)
        prompt = client.models.generate_content.call_args.kwargs["contents"]
        self.assertIn("202606", prompt);self.assertIn("-11.7%", prompt)
        client.models.generate_content.return_value.text = "실적기준분기: 202606\n실적결론: 매출 증가\n실적근거: +20%\n한줄: 신사업 전망 점검"
        result = momentum._gemini_full(client, r, {}, -2.6, earnings=e)
        self.assertIn("매출 감소", result["실적결론"]);self.assertNotIn("+20%", result["실적근거"])

    def test_new_unreadable_announcement_blocks_old_regular(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = dart.Client("test", audit_root=tmp)
            newer = {**FILING, "rcept_no": "20260925000001", "rcept_dt": "20260925", "report_nm": "영업(잠정)실적(공정공시)"}
            with patch.object(c, "corporation", return_value="00415646"), patch.object(c, "listings", return_value=[FILING, newer]), patch.object(c, "document", return_value=b'<html>no data</html>'), patch.object(c, "regular") as fallback:
                result = c.collect("192650", ASOF)
            self.assertEqual(result["status"], "unavailable");self.assertEqual(result["rows"], [])
            fallback.assert_not_called()

    def test_real_client_path_uses_selected_receipt_and_preserves_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = dart.Client("test", audit_root=tmp)
            with patch.object(c, "corporation", return_value="00415646"), patch.object(c, "listings", return_value=[FILING]), patch.object(c, "api", return_value=api()), patch.object(c, "request") as req:
                c.documents[FILING["rcept_no"]] = raw()
                result = c.collect("192650", ASOF)
            self.assertEqual(result["status"], "verified")
            req.assert_not_called()
            self.assertEqual(len(list(Path(tmp).glob('*/audit.json'))), 1)
            self.assertEqual(len(list(Path(tmp).glob('*/20260814004101.xml'))), 1)

    def test_pagination_change_and_request_key_redacted(self):
        c = dart.Client("SECRET", session=Mock())
        c.http.get.side_effect = __import__('requests').Timeout('crtfc_key=SECRET')
        with self.assertRaises(dart.EarningsError) as caught:c.request("list.json", {})
        self.assertNotIn("SECRET", str(caught.exception))
        pages = [{"status": "000", "list": [FILING], "total_count": 2, "page_no": 1, "total_page": 2},
                 {"status": "000", "list": [FILING], "total_count": 3, "page_no": 2, "total_page": 2}]
        with patch.object(c, "api", side_effect=pages), self.assertRaisesRegex(dart.EarningsError, "목록 변경"):
            c.listings("00415646", ASOF)


if __name__ == "__main__":
    unittest.main()
