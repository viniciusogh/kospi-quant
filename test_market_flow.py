"""Date alignment, missing data, breadth, aggregation, and archive preservation."""
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

import pandas as pd
import market_flow as F

DS = pd.bdate_range('2026-08-26', periods=21).strftime('%Y%m%d').tolist()
DAY = '2026-09-23'


def member(code='000001', up=True):
    px = [100 + i * (1 if up else -.5) for i in range(21)]
    rows = [{'stck_bsop_date': d, 'frgn_ntby_tr_pbmn': '100', 'orgn_ntby_tr_pbmn': '-30'} for d in DS]
    return dict(F.stock(px, DS, F.encode_investors(rows)), code=code)


def record(name='테스트', up=True):
    return F.sector(name, [member(up=up), member('000002', up)], '2026-09-23T17:40:00+09:00')


class FlowTests(unittest.TestCase):
    def test_representatives_rank_turnover_not_returns_and_keep_negative_return(self):
        members = []
        for code, amount, up in [('000001', 100, True), ('000002', 500, False),
                                 ('000003', 300, True), ('000004', 200, True)]:
            m = member(code, up)
            m.update(F.turnover_detail('종목' + code, [amount] * 21, DS))
            members.append(m)
        r = F.sector('테스트', members, 'now')
        self.assertEqual([m['code'] for m in F.representatives(r)], ['000002', '000003', '000004'])
        text = F.representative_block(r)['paragraph']['rich_text'][0]['text']['content']
        self.assertIn('종목000002 -', text)
        self.assertEqual(F.validate(r, DAY), r)

    def test_representatives_require_complete_same_period_turnover(self):
        r = record()
        for m in r['members']:
            m.update(F.turnover_detail('종목', [100] * 21, DS))
        self.assertEqual(len(F.representatives(r)), 2)
        r['members'][0].update(F.turnover_detail('종목', [float('nan')] * 21, DS))
        self.assertEqual(F.representatives(r), [])
        r['members'][0]['turnover_dates'][-1] = '20260924'
        with self.assertRaises(ValueError): F.validate(r, DAY)

    def test_supply_preserves_raw_dates_through_feature_export(self):
        with patch.dict(os.environ, {'APP_KEY': 'test', 'APP_SECRET': 'test', 'NOTION_API_KEY': 'test'}):
            import 수급 as supply
        raw = [{'stck_bsop_date': d, 'frgn_ntby_tr_pbmn': str(100 + i),
                'orgn_ntby_tr_pbmn': str(-30 + i)} for i, d in enumerate(DS)]
        response = Mock(); response.json.return_value = {'rt_cd': '0', 'output': list(reversed(raw))}
        with patch.object(supply, 'safe_request_get', return_value=response), patch.object(supply, 'polite_sleep'):
            frame = supply.get_netflow_history('000001', 'fake')
        feature = supply.compute_strength_score('000001', '테스트', frame)
        self.assertIsNotNone(feature)
        encoded = feature['flow_daily_json']
        self.assertEqual([r['date'] for r in json.loads(encoded)], DS)
        parsed = F.stock(range(100, 121), DS, encoded)
        self.assertAlmostEqual(sum(parsed['flow']), sum(70 + i * 2 for i in range(16, 21)) / 100)

    def test_exact_five_dates_units_and_same_members(self):
        r = record()
        self.assertEqual(r['flow'], [1.4] * 5)
        self.assertAlmostEqual(sum(r['flow']), 7)
        self.assertEqual(F.validate(r, DAY), r)
        self.assertEqual(r['dates'], DS[-5:])

    def test_missing_amount_is_not_zero(self):
        rows = [{'stck_bsop_date': d, 'frgn_ntby_tr_pbmn': '10', 'orgn_ntby_tr_pbmn': ''} for d in DS]
        self.assertEqual(F.encode_investors(rows), '')
        m = member(); m['flow'] = None
        self.assertIsNone(F.sector('test', [member('000002'), m], 'now')['flow'])

    def test_duplicate_dates_are_not_summed(self):
        rows = [{'stck_bsop_date': DS[-1], 'frgn_ntby_tr_pbmn': '1', 'orgn_ntby_tr_pbmn': '2'}] * 2
        self.assertEqual(F.encode_investors(rows), '')

    def test_flow_wrong_dates_or_future_are_unavailable(self):
        for flow_dates in (DS[:-1], DS + ['20260924'], DS[:-3] + DS[-2:]):
            rows = [{'stck_bsop_date': d, 'frgn_ntby_tr_pbmn': '1', 'orgn_ntby_tr_pbmn': '2'} for d in flow_dates]
            self.assertIsNone(F.stock(range(100, 121), DS, F.encode_investors(rows))['flow'])

    def test_price_calendar_mismatch_rejected(self):
        m = member(); m['dates'] = ['20260825'] + m['dates'][1:]
        with self.assertRaises(ValueError): F.sector('test', [m, member('000002')], 'now')
        with self.assertRaises(ValueError): F.stock(range(21), DS, '')
        with self.assertRaises(ValueError): F.stock(range(100, 121), DS[:-1] + [DS[-2]], '')

    def test_tampered_sum_and_wrong_day_rejected(self):
        r = record(); r['flow'][0] += 1
        with self.assertRaises(ValueError): F.validate(r, DAY)
        with self.assertRaises(ValueError): F.validate(record(), '2026-09-24')

    def test_nonfinite_rejected(self):
        for value in ('nan', 'inf', '-inf'):
            with self.assertRaises(ValueError): F.number(value)

    def test_all_down_never_called_strong(self):
        picks = F.selected([record('A', False), record('B', False)])
        self.assertEqual(len(picks), 2)
        self.assertTrue(all('약세' in F.headline(r) for r in picks))

    def test_two_sided_selection_and_stable_order(self):
        picks = F.selected([record('B'), record('C', False), record('A')])
        self.assertEqual([r['sector'] for r in picks], ['A', 'C'])

    def test_spread_requires_change_and_majority_not_mean_only(self):
        r = record(); r.update(breadth5=.7, breadth_previous5=.4, median5=.01)
        self.assertIn('퍼지고', F.headline(r))
        r['breadth_previous5'] = .7
        self.assertNotIn('퍼지고', F.headline(r))
        r['median5'] = -.01
        self.assertIn('일부', F.headline(r))

    def test_commentary_combined_flow_no_individual_inference(self):
        r = record(); r['flow'] = [-1] * 5
        self.assertIn('올랐지만', F.interpretation(r))
        r['flow'] = None
        self.assertIn('보류', F.interpretation(r))
        r['flow'] = [0] * 5
        self.assertIn('균형', F.interpretation(r))

    def test_csv_reconciles_summary_and_legacy_not_fabricated(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'latest_sector_close.csv'
            r = record()
            row = {'date': DAY, '섹터': r['sector'], 'n': r['n'], 'd5': r['d5'], 'd20': r['d20'],
                   'market_flow_json': json.dumps(r)}
            pd.DataFrame([row]).to_csv(path, index=False)
            self.assertEqual(F.read(td, DAY)['status'], 'verified')
            row['d5'] += .1
            pd.DataFrame([row]).to_csv(path, index=False)
            with self.assertRaises(ValueError): F.read(td, DAY)
            del row['market_flow_json']
            pd.DataFrame([row]).to_csv(path, index=False)
            self.assertEqual(F.read(td, DAY)['status'], 'dated_history_unavailable')

    def test_plot_and_notion_share_same_values(self):
        r = record()
        png = F.chart_png(r)
        self.assertTrue(png.startswith(b'\x89PNG'))
        with patch.object(F, 'chart_png', return_value=png) as chart:
            blocks = F.blocks({'status': 'verified', 'day': DAY, 'sectors': [r]}, lambda *a: 'upload-id')
            chart.assert_called_once_with(r)
        self.assertEqual(sum(b['type'] == 'image' for b in blocks), 1)
        r['flow'] = None
        with self.assertRaises(ValueError): F.chart_png(r)



if __name__ == '__main__':
    unittest.main()
