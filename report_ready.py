"""검증된 당일 마감 산출물 묶음. GitHub 생산자와 로컬 게시자가 같은 파일 해시를 대조한다."""
import argparse
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent
MANIFEST = 'report_inputs_ready.json'
FILES = ('latest_kospi_supply.csv', 'latest_수급_reco.csv', 'latest_kospi_quality.csv',
         'latest_quality_reco.csv', 'quality_score_history.csv', 'latest_momentum_reco.csv',
         'latest_momentum_reco_v20g.csv', 'momentum_history.csv', 'momentum_history_v20g.csv',
         'momentum_analysis.json', 'latest_sector_close.csv')
VERSION = 1


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def aware(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('시간대 없는 생성 시각')
    return result.astimezone(KST)


def validate_data(day, root=ROOT):
    import pandas as pd
    import sanity_check as sanity
    # 기존 검증 게이트를 제거/완화하지 않는다.
    previous = sanity._DIR
    try:
        sanity._DIR = str(root)
        errors = [e for spec in sanity.SPEC for e in sanity.check(spec, day)]
    finally:
        sanity._DIR = previous
    if errors:
        raise ValueError(' / '.join(errors))
    def csv(name, **kwargs):
        return pd.read_csv(Path(root) / name, **kwargs)
    supply = csv('latest_kospi_supply.csv', dtype={'종목코드': str}).set_index('종목코드')
    for tag in ('', '_v20g'):
        reco = csv(f'latest_momentum_reco{tag}.csv', dtype={'code': str})
        if 'asof' not in reco or not reco['asof'].astype(str).eq(day.replace('-', '')).all():
            raise ValueError('모멘텀 후보별 실제 일봉 기준일이 당일이 아님')
        history = csv(f'momentum_history{tag}.csv', dtype={'code': str})
        today = history[history['date'].astype(str) == day].set_index('code')
        if len(today) != 10 or today.index.duplicated().any() or set(today.index) != set(reco['code']):
            raise ValueError('모멘텀 추천/당일 이력 코드 불일치')
        for _, row in reco.iterrows():
            code = row['code']
            if float(today.loc[code, 'price']) != float(row['price']):
                raise ValueError('모멘텀 추천/당일 이력 종가 불일치')
            if code not in supply.index or str(supply.loc[code, '기준일'])[:10] != day:
                raise ValueError('추천 후보의 실제 수급 기준일이 당일이 아님')
    for name in ('latest_수급_reco.csv', 'latest_kospi_quality.csv'):
        frame = csv(name)
        date_col = '기준일' if '기준일' in frame else '기준일자'
        if not frame[date_col].astype(str).str[:10].eq(day).all():
            raise ValueError(f'{name}: 당일/이전 날짜 혼합')
    sector = csv('latest_sector_close.csv')
    if (not 10 <= len(sector) <= 60 or sector['섹터'].duplicated().any()
            or not sector['date'].astype(str).eq(day).all()
            or not sector['source'].eq('KRX daily close').all()
            or not sector['coverage_complete'].eq(True).all()):
        raise ValueError('마감 섹터 날짜/완전성 검증 실패')
    for field in ('오늘', 'd5', 'd20', '순매수', 'n'):
        import numpy as np
        if not np.isfinite(pd.to_numeric(sector[field], errors='coerce')).all():
            raise ValueError('마감 섹터 필수 수치 누락')
    analysis = json.loads((Path(root) / 'momentum_analysis.json').read_text())
    if not isinstance(analysis, dict) or not analysis:
        raise ValueError('분석 JSON 없음/형식 오류')
    for row in analysis.values():
        if not isinstance(row, dict):
            raise ValueError('분석 JSON 행 형식 오류')
        for key in ('date', 'full'):
            if row.get(key) and datetime.strptime(row[key], '%Y-%m-%d').date().isoformat() > day:
                raise ValueError('분석 JSON 미래 날짜')


def validate_manifest(manifest, day, *, root=None, closed_at=None, now=None):
    now = now or datetime.now(KST)
    if manifest.get('version') != VERSION or manifest.get('data_date') != day:
        raise ValueError('당일 준비 완료 표식 없음')
    if set(manifest.get('sha256', {})) != set(FILES):
        raise ValueError('준비 완료 표식 파일 목록 오류')
    started, completed = aware(manifest['collection_started_at']), aware(manifest['ready_at'])
    if not started <= completed <= now or started.date().isoformat() != day:
        raise ValueError('준비 완료 표식 시간 오류')
    if closed_at and started <= closed_at:
        raise ValueError('실제 정규장 종료 전에 수집한 자료 — 마감본으로 사용 금지')
    for name, sha in manifest['sha256'].items():
        if not isinstance(sha, str) or len(sha) != 64 or any(c not in '0123456789abcdef' for c in sha):
            raise ValueError('파일 해시 형식 오류')
        if root is not None and digest(Path(root) / name) != sha:
            raise ValueError(f'{name}: 생산자 검증본과 로컬 파일이 다름')
    return manifest


def seal(started, root=ROOT):
    now = datetime.now(KST)
    day = now.date().isoformat()
    validate_data(day, root)
    manifest = {'version': VERSION, 'data_date': day, 'collection_started_at': started,
                'ready_at': now.isoformat(), 'run_id': os.environ.get('GITHUB_RUN_ID', ''),
                'sha256': {name: digest(Path(root) / name) for name in FILES}}
    validate_manifest(manifest, day, now=now)
    (Path(root) / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + '\n')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--seal', action='store_true', required=True)
    parser.add_argument('--started-at', required=True)
    args = parser.parse_args()
    result = seal(args.started_at)
    print(f"Report inputs ready: {result['data_date']} / {result['ready_at']}")
