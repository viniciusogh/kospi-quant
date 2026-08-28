"""산출물 검증 게이트 — 파이프라인이 success 로 끝났는데 결과가 없거나 낡은 것을 잡는다.

무음 실패 이력: 자막 5일간 0개(프록시 402)·NameError 8일 정지 모두 워크플로가 success 로
떠서 며칠 뒤 대시보드 경고로야 발견됐다. 실패는 그날 빨간불이 되어야 한다.
스텝 성공여부로 판정하지 않는다 — 스텝이 성공하고도 쓰레기를 내놓는 게 무음 실패의 본질이다.

사용: python sanity_check.py [supply quality momentum]  (인자 없으면 전부)
      --date YYYY-MM-DD 로 기대 기준일 지정 (기본: 오늘 KST)
치명 실패 → 종료코드 1. 경고만 → 0.
"""
import os, sys, argparse
from datetime import datetime, timezone, timedelta
import numpy as np
import pandas as pd

KST = timezone(timedelta(hours=9))
_DIR = os.path.dirname(os.path.abspath(__file__))

# (그룹, 파일, 최소행, 최대행, 날짜출처파일:컬럼, 필수컬럼, 전부NaN이면안되는컬럼)
# 날짜출처가 self 인 것은 자기 기준일 컬럼, 아닌 것은 짝 히스토리의 date 최댓값으로 신선도를 본다
# (latest_*_reco.csv 와 quality CSV 에는 날짜 컬럼이 없다).
SPEC = [
    ("supply",   "latest_kospi_supply.csv",       2000, 3200, "self:기준일",
     ["종목코드", "종목명", "기준일"], "외국인+기관_순매수대금(백만원)"),
    ("supply",   "latest_수급_reco.csv",            100,  100, "self:기준일",
     ["종목코드", "종목명"], "수급합(5일)"),
    ("quality",  "latest_kospi_quality.csv",      2000, 3200, "quality_score_history.csv:date",
     ["종목코드", "종목명", "퀄리티점수"], "퀄리티점수"),
    ("quality",  "latest_quality_reco.csv",        100,  100, "quality_score_history.csv:date",
     ["종목코드", "종목명"], "퀄리티점수"),
    ("momentum", "latest_momentum_reco.csv",         10,   10, "momentum_history.csv:date",
     ["rank", "code", "종목명", "price", "score"], "score"),
    ("momentum", "latest_momentum_reco_v20g.csv",    10,   10, "momentum_history_v20g.csv:date",
     ["rank", "code", "종목명", "price", "score"], "score"),
]

def read(name):
    return pd.read_csv(os.path.join(_DIR, name), encoding="utf-8-sig")

def busgap(d, today):
    """d 이후 지난 영업일 수. 주말·공휴일 오탐을 막으려고 달력일이 아니라 영업일로 센다."""
    try:
        return int(np.busday_count(str(d)[:10], today))
    except Exception:
        return 99

def check(spec, today):
    _, name, lo, hi, dsrc, cols, numcol = spec
    p = os.path.join(_DIR, name)
    if not os.path.exists(p):
        return [f"{name}: 파일이 없다 — 생성 단계가 산출물을 못 만들었다"]
    try:
        d = read(name)
    except Exception as e:
        return [f"{name}: 읽기 실패 ({type(e).__name__}: {e})"]

    bad = []
    if not (lo <= len(d) <= hi):
        bad.append(f"{name}: 행수 {len(d)} 가 기대범위 {lo}~{hi} 밖 — 수집이 잘렸거나 필터가 깨졌다")
    missing = [c for c in cols if c not in d.columns]
    if missing:
        bad.append(f"{name}: 필수 컬럼 없음 {missing} — 컬럼명 변경이 하위 소비자를 깨뜨린다")
    if numcol in d.columns:
        s = pd.to_numeric(d[numcol], errors="coerce")
        if s.isna().all():
            bad.append(f"{name}: {numcol} 이 전부 NaN — 계산 단계가 조용히 실패했다")
        elif (s.fillna(0) == 0).all():
            bad.append(f"{name}: {numcol} 이 전부 0 — 당일등락 0.0% 버그와 같은 부류다")
    code = "종목코드" if "종목코드" in d.columns else ("code" if "code" in d.columns else None)
    if code and d[code].duplicated().any():
        dup = d[code][d[code].duplicated()].astype(str).tolist()[:5]
        bad.append(f"{name}: 종목코드 중복 {dup} — 병합이 행을 불렸다")

    src, col = dsrc.split(":")
    last = None
    if src == "self":
        if col in d.columns:
            last = str(d[col].astype(str).max())[:10]
    else:
        try:
            last = str(read(src)[col].astype(str).max())[:10]
        except Exception:
            bad.append(f"{name}: 신선도 판정 불가 — {src} 를 읽을 수 없다")
    if last:
        g = busgap(last, today)
        if g >= 1:
            bad.append(f"{name}: 기준일 {last} 이 영업일 {g}일 낡음 (기대 {today}) — 갱신 안 된 산출물이 그대로 배포된다")
    return bad

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("groups", nargs="*", default=[])
    ap.add_argument("--date", default=None, help="기대 기준일 YYYY-MM-DD (기본 오늘 KST)")
    a = ap.parse_args()
    today = a.date or datetime.now(KST).strftime("%Y-%m-%d")
    groups = set(a.groups) or {g for g, *_ in SPEC}

    print(f"▶ 산출물 검증 (기준일 {today}, 그룹 {sorted(groups)})")
    fatal = []
    for spec in SPEC:
        if spec[0] not in groups:
            continue
        name = spec[1]
        bad = check(spec, today)
        if not bad:
            print(f"  ✅ {name}")
        else:
            for b in bad:
                fatal.append(b)
                print(f"  ❌ {b}")

    if fatal:
        print(f"\n❌ 치명 {len(fatal)}건 — 이 실행은 실패다. 산출물을 신뢰하지 말 것.")
        return 1
    print("\n✅ 검증 통과")
    return 0

if __name__ == "__main__":
    sys.exit(main())
