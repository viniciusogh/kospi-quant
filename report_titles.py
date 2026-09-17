"""사용자가 정한 두 모멘텀 후보 보고서의 제목."""
def momentum_title(day, *, expanded=False):
    suffix = '후보 범위를 넓힌 10개 + 시장 추세 점검' if expanded else '상승세가 강한 종목 중 10개'
    return f'{day} 모멘텀 후보 · {suffix}'
