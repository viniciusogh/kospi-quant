"""공개 생산자 마감 경계 단위 테스트. 인증/API 호출 없음."""
from datetime import datetime
import os
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd
import report_ready as ready


class ReadyBoundaryTests(unittest.TestCase):
    def test_collection_must_follow_actual_close_not_fixed_1530(self):
        manifest = {'version': 1, 'data_date': '2026-09-09',
                    'collection_started_at': '2026-09-09T15:31:00+09:00',
                    'ready_at': '2026-09-09T16:00:00+09:00',
                    'sha256': {name: 'a' * 64 for name in ready.FILES}}
        now = ready.aware('2026-09-09T17:00:00+09:00')
        self.assertEqual(ready.validate_manifest(manifest, '2026-09-09', now=now,
                         closed_at=ready.aware('2026-09-09T15:30:00+09:00')), manifest)
        with self.assertRaises(ValueError):
            ready.validate_manifest(manifest, '2026-09-09', now=now,
                                    closed_at=ready.aware('2026-09-09T16:30:00+09:00'))

    def test_sector_close_reuses_one_daily_bar_response(self):
        with patch.dict(os.environ, {'APP_KEY': 'test', 'APP_SECRET': 'test'}):
            import sector_dashboard as sector
        frame = pd.DataFrame([{'code': '000001', '종목명': '테스트', '섹터': '건설',
                               '시가총액': 10, '순매수': 3, '기준일': '2026-09-09'}])
        prices = np.arange(100., 170.)
        fixed = unittest.mock.Mock(wraps=datetime)
        fixed.now.return_value = ready.aware('2026-09-09T16:00:00+09:00')
        with patch.object(sector, 'datetime', fixed), \
                patch.object(sector.M, 'fetch_recent', return_value=(prices, [], 0, 0, '20260909')) as fetch:
            result = sector.metrics_closed(frame, 'test', '2026-09-09').iloc[0]
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(result['d5'], prices[-1] / prices[-6] - 1)
        self.assertEqual(result['d20'], prices[-1] / prices[-21] - 1)


if __name__ == '__main__':
    unittest.main()
