import unittest
from unittest.mock import patch
import pandas as pd
import momentum_daily as m

class GateDisplayTests(unittest.TestCase):
    def test_failed_gate_still_displays_candidate_details(self):
        top=pd.DataFrame([{'code':f'{i:06d}'} for i in range(10)])
        block={'object':'block','type':'toggle','toggle':{'rich_text':[],'children':[]}}
        with patch.object(m,'MOM_TARGET','dashboard'),patch.object(m,'_stock_toggle',return_value=block),patch('dashboard.add_report',return_value='id') as add,patch('dashboard.url',return_value='test'):
            m.upload_notion(top,trend={'text':'약세','emoji':'x','color':'gray_background','reason':'조건 미충족'},cash=True)
        self.assertEqual(len(add.call_args.args[2]),10)
        text=str(add.call_args.args[1])
        self.assertIn('시장 게이트 미충족',text)
        self.assertNotIn('오늘 추천 종목 없음',text)
