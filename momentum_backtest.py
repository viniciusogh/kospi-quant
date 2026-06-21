"""30일 forward-return 예측 백테스트 — 1단계: 가격 feature only (수급 미반영).

목적: '오르기 전' 가격 feature(모멘텀/이격도/변동성)가 이후 30거래일 수익률을
예측하는지 IC(순위상관)로 검증. in-sample vs holdout 분리로 과적합 체크.
신호 확인되면 2단계에서 수급(FHPTJ04160001) 추가.

실행: python momentum_backtest.py   (.env 에 APP_KEY/APP_SECRET 필요)
"""
import os, time, random, requests, _env
import pandas as pd, numpy as np
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed


def spearman(a, b):
    """순위상관 (scipy 없이). 표본<3 이면 nan."""
    if len(a) < 3:
        return np.nan
    return pd.Series(a).rank().corr(pd.Series(b).rank())

KST = timezone(timedelta(hours=9))
APP_KEY, APP_SECRET = os.environ["APP_KEY"], os.environ["APP_SECRET"]
BASE = "https://openapi.koreainvestment.com:9443"
_DIR = os.path.dirname(os.path.abspath(__file__))

UNIVERSE_CSV = os.path.join(_DIR, "latest_kospi_supply.csv")
CACHE_DIR    = os.path.join(_DIR, "price_cache")   # 종목별 일봉 캐시 (재실행 초단위)
TOP_N        = None    # None=전 종목 (시총컷 해제, 생존편향 완화). point-in-time 유동성컷으로 노이즈 제거
FWD          = 30      # forward 수익률 일수 (거래일)
ANCHOR_STEP  = 5       # anchor 간격 (거래일)
LIQ_PCT      = 0.30    # 거래대금 하위 30% 컷 (노이즈/저유동성 제외)
EXTREME_RET  = 0.40    # anchor 직전 5일 |수익률| 이 값 넘으면 극단노이즈로 제외
WORKERS      = 4


def log(m): print(f"[{datetime.now(KST):%H:%M:%S}] {m}")


def token():
    """발급 토큰 디스크 캐시 (KIS 발급 1분당 1회 제한 회피, 유효 6h 재사용)."""
    import json
    cache = os.path.join(_DIR, ".kis_token.json")
    if os.path.exists(cache):
        try:
            c = json.load(open(cache))
            if time.time() - c["ts"] < 6 * 3600:
                return c["token"]
        except Exception:
            pass
    # 연결 타임아웃(GHA↔KIS 간헐 불통)에 강건하게: 예외도 재시도+백오프, timeout 확대 (2026-06-21)
    last = None
    for attempt in range(4):
        try:
            r = requests.post(f"{BASE}/oauth2/tokenP", json={
                "grant_type": "client_credentials", "appkey": APP_KEY, "appsecret": APP_SECRET}, timeout=15)
            if r.status_code == 200:
                tok = r.json()["access_token"]
                json.dump({"token": tok, "ts": time.time()}, open(cache, "w"))
                return tok
            last = f"status {r.status_code}: {r.text[:120]}"
            time.sleep(61)              # 비200 = 대개 발급제한(1분당 1회) → 1분 대기
        except Exception as e:
            last = repr(e)
            time.sleep(5 * (attempt + 1))   # 연결오류 → 짧게 백오프 후 재시도
    raise RuntimeError(f"토큰 발급 실패(4회): {last}")


def _get(url, headers, params, retry=3):
    for a in range(retry):
        try:
            r = requests.get(url, headers=headers, params=params, timeout=5)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        time.sleep(0.4 * (a + 1))
    return None


def fetch_prices(code, tok):
    """FHKST03010100 윈도 체이닝 → 2023~현재 일봉 [date, close, value] 오름차순. 디스크 캐시."""
    cache = os.path.join(CACHE_DIR, f"{code}.pkl")
    if os.path.exists(cache):
        df = pd.read_pickle(cache)
        return df if df is not None and len(df) >= FWD + 65 else None
    url = f"{BASE}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
    hdr = {"authorization": f"Bearer {tok}", "appkey": APP_KEY, "appsecret": APP_SECRET,
           "tr_id": "FHKST03010100", "custtype": "P"}
    today = datetime.now(KST)
    start = datetime(2023, 1, 1, tzinfo=KST)
    wins, d2 = [], today
    while d2 > start:                       # 150일(거래일~100) 윈도로 2023~현재 체이닝
        d1 = d2 - timedelta(days=150)
        wins.append((max(d1, start), d2))
        d2 = d1 - timedelta(days=1)
    rows = []
    for d1, d2 in wins:
        j = _get(url, hdr, {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
                            "FID_INPUT_DATE_1": d1.strftime("%Y%m%d"),
                            "FID_INPUT_DATE_2": d2.strftime("%Y%m%d"),
                            "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0"})
        time.sleep(random.uniform(0.2, 0.35))
        if not j or j.get("rt_cd") != "0":
            continue
        for r in j.get("output2", []) or []:
            c = r.get("stck_clpr")
            if c and c != "0":
                rows.append({"date": r["stck_bsop_date"], "close": float(c),
                             "value": float(r.get("acml_tr_pbmn", 0) or 0)})
    df = pd.DataFrame(rows).drop_duplicates("date").sort_values("date").reset_index(drop=True) if rows else None
    os.makedirs(CACHE_DIR, exist_ok=True)
    pd.to_pickle(df, cache)
    return df if df is not None and len(df) >= FWD + 65 else None


def features_at(df, i):
    """df 의 i번째 행을 anchor T 로 보고 feature 계산 (i 이전 데이터만 사용)."""
    c = df["close"].values
    if i < 60 or i + FWD >= len(c):
        return None
    px = c[i]
    prev5 = c[i - 5]
    # 극단 노이즈 컷: 직전 5일 급등락
    if abs(px / prev5 - 1) > EXTREME_RET:
        return None
    ma20 = c[i - 19:i + 1].mean()
    ret20 = c[i] / c[i - 20] - 1
    ret60 = c[i] / c[i - 60] - 1
    ret5 = px / prev5 - 1
    daily = np.diff(c[i - 20:i + 1]) / c[i - 20:i]
    vol20 = daily.std()
    hi60 = px / c[i - 60:i + 1].max()
    liq = df["value"].values[i - 19:i + 1].mean()
    fwd = c[i + FWD] / px - 1
    return {"ret5": ret5, "ret20": ret20, "ret60": ret60, "disp20": px / ma20 - 1,
            "vol20": vol20, "hi60": hi60, "liq": liq, "fwd": fwd}


FEATS = ["ret5", "ret20", "ret60", "disp20", "vol20", "hi60"]


def main():
    log("토큰 발급")
    tok = token()

    uni = pd.read_csv(UNIVERSE_CSV)
    code_col = uni.columns[0]  # BOM 종목코드
    uni[code_col] = uni[code_col].astype(str).str.zfill(6)
    uni = uni.sort_values("시가총액", ascending=False)
    if TOP_N:
        uni = uni.head(TOP_N)
    codes = uni[code_col].tolist()
    log(f"유니버스 {len(codes)}개 ({'전 종목' if not TOP_N else f'시총상위 {TOP_N}'})")

    log("가격 수집 시작")
    panels = {}
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(fetch_prices, c, tok): c for c in codes}
        for n, f in enumerate(as_completed(futs), 1):
            c = futs[f]
            df = f.result()
            if df is not None:
                panels[c] = df
            if n % 25 == 0:
                log(f"  {n}/{len(codes)} ({time.time()-t0:.0f}s)")
    log(f"가격 확보 {len(panels)}개")

    # anchor 별 obs 수집
    obs = []
    for c, df in panels.items():
        last_anchor = len(df) - FWD - 1
        for i in range(60, last_anchor, ANCHOR_STEP):
            fe = features_at(df, i)
            if fe:
                fe["code"], fe["anchor"] = c, df["date"].iloc[i]
                obs.append(fe)
    obs = pd.DataFrame(obs)
    log(f"raw obs {len(obs)}")

    # 노이즈/저유동성 컷: anchor 별 거래대금 하위 LIQ_PCT 제거
    obs = obs[obs.groupby("anchor")["liq"].transform(
        lambda s: s >= s.quantile(LIQ_PCT))].copy()
    log(f"유동성 컷 후 obs {len(obs)}  anchor 수 {obs['anchor'].nunique()}")

    # anchor → 반기 레짐 라벨
    obs["regime"] = obs["anchor"].str[:4] + np.where(obs["anchor"].str[4:6] <= "06", "H1", "H2")

    # anchor 별 IC 먼저 계산 → 레짐으로 집계
    ic = (obs.melt(id_vars=["anchor", "regime", "fwd"], value_vars=FEATS,
                   var_name="feature", value_name="fval")
             .groupby(["regime", "feature", "anchor"])
             .apply(lambda g: spearman(g["fval"].values, g["fwd"].values), include_groups=False)
             .rename("ic").reset_index())

    regimes = sorted(obs["regime"].unique())
    print("\n===== 레짐별 IC_mean (feature × 반기) =====")
    mat = ic.groupby(["feature", "regime"])["ic"].mean().unstack("regime").reindex(FEATS)
    print(mat[regimes].round(3).to_string())

    print("\n===== 레짐별 anchor 수 =====")
    print(obs.groupby("regime")["anchor"].nunique().reindex(regimes).to_string())

    print("\n===== 전체기간 feature IC 요약 =====")
    summ = (ic.groupby("feature")["ic"].agg(["mean", "std", "count"])
              .assign(IR=lambda d: d["mean"] / (d["std"] + 1e-9))
              .reindex(FEATS).sort_values("mean", key=abs, ascending=False))
    print(summ.round(3).to_string())

    print("\n[해석] 진짜 신호 = 레짐 바뀌어도 부호 안 뒤집힘. "
          "한 레짐만 크고 나머지 음수면 그 레짐 베타일 뿐.")

    # ===== 분위 스프레드: 안정 feature 합성점수 상위10% vs 하위10% 30일수익률 =====
    STABLE = ["hi60", "disp20", "ret5"]
    for f in STABLE:
        obs[f + "_z"] = obs.groupby("anchor")[f].transform(
            lambda s: (s - s.mean()) / (s.std() + 1e-9))
    obs["score"] = obs[[f + "_z" for f in STABLE]].sum(axis=1)

    def spread(g):
        if len(g) < 20:
            return None
        hi, lo = g["score"].quantile(0.9), g["score"].quantile(0.1)
        top = g[g["score"] >= hi]["fwd"].mean()
        bot = g[g["score"] <= lo]["fwd"].mean()
        return pd.Series({"top": top, "bot": bot, "spread": top - bot, "uni": g["fwd"].mean()})

    pa = (obs.groupby(["regime", "anchor"]).apply(spread, include_groups=False)
             .dropna().reset_index())

    print("\n===== 분위 스프레드 (합성=hi60+disp20+ret5, 30일수익률 %) =====")
    tbl = pa.groupby("regime")[["top", "bot", "spread", "uni"]].mean() * 100
    tbl["승률"] = pa.groupby("regime")["spread"].apply(lambda s: (s > 0).mean())
    print(tbl.round(2).to_string())
    g = pa[["top", "bot", "spread", "uni"]].mean() * 100
    print(f"\n전체: 상위10%={g['top']:.2f}%  하위10%={g['bot']:.2f}%  "
          f"스프레드={g['spread']:.2f}%p  유니버스평균={g['uni']:.2f}%  "
          f"스프레드>0 승률={(pa['spread']>0).mean():.0%}")
    print("[해석] 스프레드 = 상위-하위 30일수익률 차. 매 레짐 +이고 승률 높으면 실전 가치.")


if __name__ == "__main__":
    main()
