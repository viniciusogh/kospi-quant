"""Deterministic market summary. Dated KIS inputs only; no news or stock selection."""
import io
import json
import math
from datetime import datetime
from pathlib import Path
from statistics import mean, median

VERSION = 1
NEUTRAL = .003


def number(value):
    value = float(value)
    if not math.isfinite(value):
        raise ValueError('시장 흐름 숫자 누락')
    return value


def dates(values):
    result = [str(v).replace('-', '') for v in values]
    for value in result:
        if len(value) != 8 or datetime.strptime(value, '%Y%m%d').strftime('%Y%m%d') != value:
            raise ValueError('시장 흐름 날짜 오류')
    if result != sorted(set(result)):
        raise ValueError('시장 흐름 날짜 중복/역순')
    return result


def encode_investors(rows):
    """Preserve the source dates and both amounts, without filling missing data."""
    try:
        items = sorted([{'date': r['stck_bsop_date'],
                         'foreign': number(r['frgn_ntby_tr_pbmn']),
                         'institution': number(r['orgn_ntby_tr_pbmn'])} for r in rows],
                       key=lambda r: r['date'])
        dates([r['date'] for r in items])
        return json.dumps(items, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError, KeyError):
        return ''


def stock(prices, price_dates, encoded_flow):
    px = [number(p) for p in prices]
    ds = dates(price_dates)
    if len(ds) != len(px) or len(px) < 21 or min(px) <= 0:
        raise ValueError('시장 흐름 가격 이력 부족')
    flow = None
    try:
        records = json.loads(encoded_flow)
        fd = dates([r['date'] for r in records])
        lookup = {d: r for d, r in zip(fd, records)}
        # Same five KRX sessions for every constituent; no partial-sum/zero fallback.
        flow = [(number(lookup[d]['foreign']) + number(lookup[d]['institution'])) / 100
                for d in ds[-5:]]
        if max(fd) != ds[-1]:
            flow = None
    except (ValueError, TypeError, KeyError):
        pass
    return {'dates': ds[-21:], 'd5': px[-1] / px[-6] - 1,
            'previous5': px[-6] / px[-11] - 1, 'd20': px[-1] / px[-21] - 1,
            'flow': flow}


def sector(name, members, observed_at):
    if len(members) < 2 or len({m['code'] for m in members}) != len(members):
        raise ValueError('시장 흐름 구성종목 부족/중복')
    common = members[0]['dates']
    if any(m['dates'] != common for m in members):
        raise ValueError('시장 흐름 구성종목 거래일 불일치')
    complete = all(m['flow'] is not None for m in members)
    return {'version': VERSION, 'sector': str(name), 'dates': common[-5:],
            'asof': common[-1], 'observed_at': observed_at, 'n': len(members),
            'd5': mean(m['d5'] for m in members), 'd20': mean(m['d20'] for m in members),
            'median5': median(m['d5'] for m in members),
            'breadth5': mean(m['d5'] > 0 for m in members),
            'breadth_previous5': mean(m['previous5'] > 0 for m in members),
            'flow': [sum(m['flow'][i] for m in members) for i in range(5)] if complete else None,
            'members': members, 'source': 'KIS FHKST03010100 / FHKST01010900'}


def validate(record, day):
    if record['version'] != VERSION or record['asof'] != day.replace('-', ''):
        raise ValueError('시장 흐름 기준일/버전 불일치')
    # Recompute against stored per-company inputs, including breadth and every daily sum.
    rebuilt = sector(record['sector'], record['members'], record['observed_at'])
    if rebuilt != record or len(dates(record['dates'])) != 5:
        raise ValueError('시장 흐름 합계/구성종목 불일치')
    for m in record['members']:
        if len(dates(m['dates'])) != 21:
            raise ValueError('시장 흐름 거래일 부족')
        for key in ('d5', 'd20', 'previous5'):
            number(m[key])
        if m['flow'] is not None:
            if len(m['flow']) != 5:
                raise ValueError('시장 흐름 수급일 부족')
            for amount in m['flow']:
                number(amount)
    return record


def read(root, day):
    import pandas as pd
    frame = pd.read_csv(Path(root) / 'latest_sector_close.csv')
    if 'market_flow_json' not in frame:
        return {'day': day, 'sectors': [], 'status': 'dated_history_unavailable'}
    if set(frame['date']) != {day} or frame['섹터'].duplicated().any():
        raise ValueError('시장 흐름 CSV 기준일/업종 중복')
    result = []
    for _, row in frame.iterrows():
        record = validate(json.loads(row['market_flow_json']), day)
        if record['sector'] != row['섹터'] or record['n'] != int(row['n']):
            raise ValueError('시장 흐름 CSV 구성 불일치')
        if any(not math.isclose(number(row[k]), record[k], abs_tol=1e-12) for k in ('d5', 'd20')):
            raise ValueError('시장 흐름 CSV 수익률 불일치')
        result.append(record)
    if not result or len({tuple(r['members'][0]['dates']) for r in result}) != 1:
        raise ValueError('시장 흐름 업종별 거래일 불일치')
    return {'day': day, 'sectors': result, 'status': 'verified'}


def selected(records):
    ranked = sorted(records, key=lambda r: (-r['d5'], r['sector']))
    positive = [r for r in ranked if r['d5'] >= NEUTRAL]
    negative = [r for r in reversed(ranked) if r['d5'] <= -NEUTRAL]
    if positive and negative:
        return [positive[0], negative[0]]
    return (positive or negative)[:2]


def headline(r):
    if r['d5'] >= NEUTRAL:
        if r['median5'] > 0 and r['breadth5'] >= .6 and r['breadth5'] - r['breadth_previous5'] >= .2 - 1e-12:
            text = '상승이 여러 종목으로 퍼지고 있습니다'
        elif r['median5'] <= 0:
            text = '일부 종목에 상승이 집중됐습니다'
        else:
            text = '최근 오름세를 보이고 있습니다'
    else:
        text = ('최근 상승 흐름이 꺾였습니다' if r['d20'] > 0 else
                '최근 약세가 이어지고 있습니다' if r['d20'] < 0 else '최근 하락세를 보이고 있습니다')
    return f"{r['sector']}, {text}"


def interpretation(r):
    up, month_up = r['d5'] > 0, r['d20'] > 0
    first = ('최근 한 달의 상승 흐름이 최근 5거래일에도 이어졌습니다.' if up and month_up else
             '최근 한 달의 하락 속에서 최근 5거래일은 반등했습니다.' if up else
             '최근 한 달 수익률은 플러스지만 최근 5거래일은 하락했습니다.' if month_up else
             '최근 한 달과 최근 5거래일 모두 하락했습니다.')
    if r['d20'] == 0:
        first = '최근 한 달 수익률은 보합이며 최근 5거래일은 ' + ('상승했습니다.' if up else '하락했습니다.')
    flow = r['flow']
    if flow is None:
        return first + ' 외국인·기관 수급은 일부 자료가 빠져 합산을 보류했습니다.'
    total = sum(flow)
    if total == 0:
        second = '외국인·기관의 합산 순매수는 매수와 매도가 균형을 이뤘습니다.'
    elif up and total > 0:
        second = '주가 상승과 외국인·기관의 합산 순매수가 함께 나타났습니다.'
    elif not up and total < 0:
        second = '주가 하락과 외국인·기관의 합산 순매도가 함께 나타났습니다.'
    elif up:
        second = '주가는 올랐지만 외국인·기관 합산은 순매도였습니다.'
    else:
        second = '외국인·기관 합산은 순매수였지만 주가는 하락했습니다.'
    return first + ' ' + second


def chart_png(r):
    """Compact plot with an explicit zero and symmetric axis; no price line."""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    font = next((name for name in ('Apple SD Gothic Neo', 'NanumGothic', 'Noto Sans CJK KR')
                 if any(f.name == name for f in font_manager.fontManager.ttflist)), 'DejaVu Sans')
    if r['flow'] is None:
        raise ValueError('미확인 수급은 그래프 생성 금지')
    with plt.rc_context({'font.family': font, 'axes.unicode_minus': False}):
        fig = plt.figure(figsize=(8, 1.2), dpi=160, facecolor='white')
        ax = fig.add_axes([.43, .23, .53, .68])
        values = r['flow']; limit = max(max(abs(v) for v in values) * 1.15, 1)
        ax.bar(range(5), values, width=.4, color=['#c85c60' if v > 0 else '#5483ad' for v in values])
        ax.axhline(0, color='#acb3bc', linewidth=.7)
        ax.set_ylim(-limit, limit)
        ax.set_xticks(range(5), [f'{d[4:6]}/{d[6:]}' for d in r['dates']], fontsize=9, color='#727c89')
        ax.set_yticks([0], ['0'], fontsize=9, color='#727c89')
        ax.tick_params(length=0)
        for spine in ax.spines.values(): spine.set_visible(False)
        fig.text(.015, .62, '외국인 + 기관', fontsize=12, weight='bold', color='#252a30')
        total = sum(values)
        fig.text(.015, .30, f'5일 합계 {total:+,.0f}억원', fontsize=11,
                 color='#bf4549' if total > 0 else '#4277ad' if total < 0 else '#727c89')
        output = io.BytesIO(); fig.savefig(output, format='png'); plt.close(fig)
        return output.getvalue()


def block(text, kind='paragraph'):
    return {'object': 'block', 'type': kind, kind: {'rich_text': [
        {'type': 'text', 'text': {'content': text}}]}}


def blocks(data, upload):
    result = [block('시장 흐름', 'heading_2')]
    if data['status'] != 'verified':
        return result + [block('날짜가 확인된 최근 5거래일 자료를 준비 중입니다.')]
    records = selected(data['sectors'])
    result.append(block(f"{data['day']} 마감 · KIS 시세·수급 · 시총 상위 300개와 지정 테마 · 수익률은 업종 내 종목 평균"))
    if not records:
        return result + [block('최근 5거래일 업종 평균 수익률은 모두 보합권입니다.')]
    for r in records:
        result.extend([block(headline(r), 'heading_3'),
                       block(f"최근 5일 {r['d5']:+.1%}     20일 {r['d20']:+.1%}")])
        if r['flow'] is not None:
            fid = upload(chart_png(r), 'market-flow.png')
            if not fid:
                raise ValueError('시장 흐름 그래프 업로드 실패')
            result.append({'object': 'block', 'type': 'image', 'image': {
                'type': 'file_upload', 'file_upload': {'id': fid},
                'caption': [{'type': 'text', 'text': {'content':
                    '외국인·기관 일별 순매수 합산 · 위: 순매수 / 아래: 순매도 · 업종별 축 크기는 다름'}}]}})
        result.append(block(interpretation(r)))
    observed = max(r['observed_at'] for r in records)
    result.append(block(f'자료 조회: {observed} · KIS API → 종목별 날짜 대조 → 업종별 합산'))
    return result
