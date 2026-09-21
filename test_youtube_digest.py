"""Unified YouTube publication/cost boundaries; no paid generation or network."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import youtube_digest as y

DAY = '2026-09-21'
REPORT = '''## 전체 요약
AI 투자 이야기가 나왔다.
가격을 확인하자는 의견도 있었다.
물가 해석에는 차이가 있었다.

### 1. AI 투자 확대를 긍정적으로 봤다
메모리 수요를 이유로 들었다. 공급 계획의 진행 여부를 관찰한다.

### 2. 좋은 산업도 진입 가격은 별도로 봤다
**급등 뒤 추격**을 경계했다. 매수세가 이어지는지를 관찰한다.

### 3. 물가 전망은 다른 근거에 무게를 뒀다
장기금리 안정과 유가 상승을 각각 강조했다. 무엇이 실제 물가에 영향을 줄지 본다.

## 그래서 한마디로?
산업 전망과 진입 시점은 별개다. 기대가 실제 진행으로 이어지는지를 본다.
'''


def cache(day=DAY):
    return {day: {'a': [{'video_id': 'one', 'title': 'AI 이야기', 'analysis': '산업은 긍정적, 가격은 주의.'}],
                  'b': [{'video_id': 'two', 'title': '물가 이야기', 'analysis': '유가 상승과 장기금리 안정의 대립.'}]}}


class DigestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.llm = Mock(return_value=REPORT)
        p = patch('requests.sessions.Session.request', side_effect=AssertionError('network forbidden'))
        p.start(); self.addCleanup(p.stop)

    def generate(self, source=None):
        return y.generate(source or cache(), DAY, root=self.root, llm=self.llm,
                          write=lambda p, v: p.write_text(json.dumps(v)))

    def test_next_day_activation(self):
        self.assertFalse(y.enabled('2026-09-20'))
        self.assertTrue(y.enabled(DAY))

    def test_current_sources_only_and_deduplication(self):
        data = {**cache('2026-09-20'), **cache(), **cache('2026-09-22')}
        data[DAY]['c'] = data[DAY]['a']
        rows = y.material(data, DAY)
        self.assertEqual(len(rows), 2)
        self.assertEqual({r['date'] for r in rows}, {DAY})
        with self.assertRaises(ValueError): y.material(cache('2026-09-20'), DAY)

    def test_holiday_window_does_not_relabel_old_or_future_videos(self):
        data = {**cache('2026-09-17'), **cache('2026-09-20'), **cache('2026-09-22')}
        self.assertEqual({r['date'] for r in y.material(data, DAY, light=True)}, {'2026-09-20'})
        with self.assertRaises(ValueError): y.material(cache('2026-09-17'), DAY, light=True)

    def test_cached_success_does_not_spend_again(self):
        first = self.generate()
        second = self.generate()
        self.assertEqual(first, second)
        self.llm.assert_called_once()
        prompt = self.llm.call_args.args[0]
        self.assertIn('3~5분', prompt)
        self.assertIn('2,200~2,700자', prompt)
        self.assertIn('원문에 없는 이유를 추측해서 만들지는 않는다', prompt)

    def test_changed_input_invalidates_cache(self):
        self.generate()
        changed = cache(); changed[DAY]['b'][0]['analysis'] += ' 새 발언.'
        self.generate(changed)
        self.assertEqual(self.llm.call_count, 2)

    def test_provider_failure_and_bad_response_are_not_retried(self):
        for error in [RuntimeError('fake timeout'), None]:
            with self.subTest(error=error):
                for p in self.root.rglob('*.json'): p.unlink()
                self.llm.reset_mock()
                self.llm.side_effect = error
                self.llm.return_value = 'unfinished'
                with self.assertRaises((ValueError, RuntimeError)): self.generate()
                with self.assertRaises(ValueError): self.generate()
                self.llm.assert_called_once()

    def test_input_cap_blocks_before_paid_call(self):
        data = cache(); data[DAY]['a'][0]['analysis'] = '가' * 100001
        with self.assertRaises(ValueError): self.generate(data)
        self.llm.assert_not_called()

    def test_link_table_missing_summary_or_closing_rejected(self):
        for bad in [REPORT + '\nhttps://example.com', REPORT + '\n| table |',
                    REPORT.replace('## 전체 요약', '요약'), REPORT.replace('## 그래서 한마디로?', ''),
                    REPORT.replace('### 3.', '### 4.')]:
            with self.subTest(bad=bad), self.assertRaises(ValueError): y.validate(bad)

    def test_publication_readback_and_retry_reuse(self):
        record = self.generate()
        d = Mock()
        d._layout.return_value = ([], None, None, [], [])
        d.add_report.return_value = 'notion-root'
        d.children.return_value = y.render(REPORT)
        self.assertEqual(y.publish(record, d), 'notion-root')
        title = f'📺 {DAY} 유튜브 통합 레포트'
        d._base_title.side_effect = lambda s: s
        d._layout.return_value = ([], None, None, [], [{'id': 'notion-root', 'type': 'toggle',
            'toggle': {'rich_text': [{'plain_text': title}]}}])
        self.assertEqual(y.publish(record, d), 'notion-root')
        d.add_report.assert_called_once()
        self.llm.assert_called_once()
        self.assertNotIn('toggle', [b['type'] for b in y.render(REPORT)])

    def test_incomplete_publication_fails_without_regeneration(self):
        record = self.generate()
        d = Mock(); d._layout.return_value = ([], None, None, [], [])
        d.add_report.return_value = 'notion-root'; d.children.return_value = []
        with self.assertRaises(ValueError): y.publish(record, d)
        self.generate()
        self.llm.assert_called_once()

    def test_weekend_uses_one_digest_without_second_llm_or_duplicate_body(self):
        import daily_archive as a
        with patch.object(a, '_llm') as llm, patch.object(a, 'dashboard_copy', return_value=['single-report']):
            data = {'youtube_digest': REPORT}
            self.assertEqual(a.synthesize_weekend(data), REPORT)
            self.assertEqual(a.build_weekend_blocks(data, REPORT), ['single-report'])
            llm.assert_not_called()

    def test_watcher_waits_for_current_input(self):
        import archive_watch as w
        with patch.object(w, 'ROOT', self.root):
            with self.assertRaises(w.InputsPending): w.verify_youtube_inputs(DAY)
            (self.root / 'latest_youtube_analysis.json').write_text(json.dumps(cache()))
            w.verify_youtube_inputs(DAY)

    def test_producer_saves_inputs_without_building_or_uploading_channel_reports(self):
        with patch.dict('os.environ', {'GEMINI_API_KEY':'isolated-fixture','NOTION_API_KEY':'isolated-fixture'}):
            import youtube_report as producer
        video = {'id': 'one', 'published': DAY, 'title': 'sample'}
        with patch.object(producer, 'get_channel_videos', return_value=[video]), \
             patch.object(producer, 'load_processed', return_value=set()), \
             patch.object(producer, 'load_failed', return_value={}), \
             patch.object(producer, 'save_processed') as saved, \
             patch.object(producer, 'save_failed'), patch.object(producer, '_quota', return_value=0), \
             patch.object(producer, '_quota_add'), patch.object(producer, 'get_transcript', return_value='text'), \
             patch.object(producer, 'analyze_with_gemini', return_value='analysis'), \
             patch.object(producer, '_save_analysis_cache') as stored, \
             patch.object(producer, 'build_video_blocks') as built, \
             patch.object(producer, 'get_or_create_channel_toggle') as uploaded:
            producer._process_channel({'name': 'test', 'channel_id': 'channel', 'slug': 'test'}, DAY)
            stored.assert_called_once()
            saved.assert_called_with({'one'})
            built.assert_not_called(); uploaded.assert_not_called()


if __name__ == '__main__':
    unittest.main()
