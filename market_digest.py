"""One market narrative from video analyses and measured sector data."""
import csv
import json
import math
from pathlib import Path

SINCE = '2026-09-22'


def sectors(root, day, *, light=False):
    with (Path(root) / 'latest_sector_close.csv').open(encoding='utf-8-sig') as f:
        rows = list(csv.DictReader(f))
    dates = {r['date'] for r in rows}
    if len(dates) != 1 or (not light and dates != {day}) or any(d > day for d in dates):
        raise ValueError('시장 동향 섹터 기준일 불일치')
    out = []
    for r in rows:
        if r.get('coverage_complete', '').lower() != 'true':
            raise ValueError('시장 동향 섹터 수집 불완전')
        values = {k: float(r[k]) for k in ('오늘', 'd5', 'd20', '순매수')}
        if not all(math.isfinite(v) for v in values.values()):
            raise ValueError('시장 동향 섹터 수치 누락')
        out.append({'업종': r['섹터'], '당일방향': '상승' if values['오늘'] > 0 else '하락' if values['오늘'] < 0 else '보합',
                    '당일수급방향': '순매수' if values['순매수'] > 0 else '순매도' if values['순매수'] < 0 else '중립',
                    '당일등락률_pct': values['오늘'] * 100,
                    '5일등락률_pct': values['d5'] * 100, '20일등락률_pct': values['d20'] * 100,
                    '외국인기관순매수_억원': values['순매수'] / 100, '주도주': r.get('주도주', ''),
                    'chart_capital': float(r['시가총액']) if r.get('시가총액') else None,
                    'chart_leaders': json.loads(r['주도종목_json']) if r.get('주도종목_json') else None})
    return {'data_date': next(iter(dates)), 'source': 'KRX daily close',
            'method': '수집 유니버스의 섹터 내 등가중, 업종지수 자체는 아님', 'rows': out}
