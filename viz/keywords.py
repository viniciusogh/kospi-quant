"""유튜브 분석에서 많이 나온 단어 → 트리맵 PNG (크기=언급 횟수, 색=종류).

세는 방식: **아는 단어만 센다.** 형태소 분석기 없이 자유 토큰을 뽑으면 조사·불용어가 섞여
의미가 없다. 종목명은 실제 유니버스 CSV(863개)에서, 섹터는 KIS 업종명에서 가져오고
매크로·테마 용어만 큐레이션한다. 긴 단어 우선 매칭 + 구간 마스킹으로 중복 집계를 막는다
("HD현대중공업" 을 세고 나면 그 구간에서 "HD현대" 를 다시 세지 않는다).

색: 카테고리(종목/테마·섹터/매크로) = 명목형 → 기준 팔레트 고정 순서 1·2·3 의 다크 모드 값.
검증기 통과 확인 (다크 표면 #2b2f3b): 명도대역·채도·CVD·정상시야·표면대비 전부 PASS.
크기가 빈도를 담으므로 색에 빈도를 다시 싣지 않는다.
"""
import os, re, collections
import numpy as np
import pandas as pd

from treemap import (squarify, contrast, hex_to_oklab, SURFACE, SUBINK, _font)  # noqa: F401

CAT_COLORS = {"종목": "#3987e5", "테마·섹터": "#d95926", "매크로": "#199e70"}

MACRO = ["금리", "환율", "인플레이션", "관세", "유가", "FOMC", "연준", "CPI", "실적", "수급",
         "외국인", "기관", "개미", "달러", "국채", "경기침체", "버블", "순환매", "주도주",
         "밸류업", "배당", "자사주", "공매도", "코스피", "코스닥", "나스닥", "환헤지"]
THEME = ["AI", "HBM", "반도체", "메모리", "전력", "원전", "방산", "조선", "2차전지", "바이오",
         "로봇", "우주항공", "클라우드", "데이터센터", "전기차", "자율주행", "화장품", "엔터",
         "은행", "증권", "보험", "건설", "통신", "철강", "정유", "해운"]


# 국내 유니버스에 없지만 자주 언급되는 해외 종목·기업 (국내 유튜브 분석 특성)
FOREIGN = ["엔비디아", "테슬라", "애플", "마이크로소프트", "구글", "아마존", "메타", "브로드컴",
           "마이크론", "TSMC", "팔란티어", "AMD", "인텔", "넷플릭스", "코인베이스", "오픈AI",
           "슈퍼마이크로", "코어위브", "일라이릴리", "버크셔"]


def _load_text(path, days=2):
    import json
    if not os.path.exists(path):
        return "", 0
    d = json.load(open(path))
    ks = sorted(d.keys())[-days:]
    txt, n = "", 0
    for k in ks:
        for vs in d[k].values():
            for v in vs:
                txt += v.get("analysis", "") + "\n"
                n += 1
    return txt, n


def count_keywords(txt, universe_csv, top=40):
    """긴 단어 우선 + 구간 마스킹으로 중복 없이 센다. 반환 [(단어, 횟수, 카테고리)]."""
    voc = {}
    try:
        sup = pd.read_csv(universe_csv, dtype={"종목코드": str})
        for n in sup["종목명"].dropna().unique():
            n = str(n)
            if len(n) >= 3 and not re.search(r"(ETF|ETN|레버리지|인버스|선물|스팩)", n):
                voc[n] = "종목"
        for s in sup["섹터"].dropna().unique():
            s = str(s)
            if "ETF" not in s and "ETN" not in s and len(s) >= 2:
                voc.setdefault(s, "테마·섹터")
    except Exception:
        pass
    for n in FOREIGN:
        voc[n] = "종목"
    for t in THEME:
        voc[t] = "테마·섹터"
    for m in MACRO:
        voc.setdefault(m, "매크로")

    mask = bytearray(len(txt))
    cnt = collections.Counter()
    cat = {}
    for w in sorted(voc, key=len, reverse=True):        # 긴 단어 먼저
        for mt in re.finditer(re.escape(w), txt):
            a, b = mt.start(), mt.end()
            if any(mask[a:b]):                          # 이미 더 긴 단어가 차지한 구간
                continue
            for i in range(a, b):
                mask[i] = 1
            cnt[w] += 1
            cat[w] = voc[w]
    return [(w, c, cat[w]) for w, c in cnt.most_common(top) if c >= 2]


def render(items, asof, out_path, n_videos=0, w=1400, h=760):
    """items: [(단어, 횟수, 카테고리)] — 크기=횟수, 색=카테고리."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    f = _font()
    if f:
        plt.rcParams["font.family"] = f
    plt.rcParams["axes.unicode_minus"] = False

    fig = plt.figure(figsize=(w / 100, h / 100), dpi=100, facecolor=SURFACE)
    ax = fig.add_axes([0.008, 0.008, 0.984, 0.855])
    ax.set_xlim(0, 100); ax.set_ylim(0, 100); ax.axis("off")

    fig.text(0.008, 0.945, f"많이 나온 단어  {asof}", fontsize=19, fontweight="bold", color="#ffffff")
    fig.text(0.008, 0.902,
             f"크기 = 언급 횟수   ·   색 = 종류   ·   영상 {n_videos}개 분석 기준"
             f"   ·   사전에 있는 단어만 집계(조사·불용어 제외)",
             fontsize=10.5, color=SUBINK)
    # 범례 (색이 2종 이상이므로 필수)
    lx = 0.62
    for i, (name, col) in enumerate(CAT_COLORS.items()):
        fig.patches.append(plt.Rectangle((lx + i * 0.13, 0.938), 0.018, 0.022, facecolor=col,
                                        transform=fig.transFigure, figure=fig))
        fig.text(lx + i * 0.13 + 0.023, 0.945, name, fontsize=11, color="#dfe3ea")

    rects = squarify([c for _, c, _ in items], 0, 0, 100, 100)
    GAP = 0.4
    for (word, c, cate), (x, y, rw, rh) in zip(items, rects):
        col = CAT_COLORS.get(cate, "#3987e5")
        tw, th = max(rw - GAP, 0.1), max(rh - GAP, 0.1)
        ax.add_patch(Rectangle((x, y), tw, th, facecolor=col, edgecolor=SURFACE, lw=1.0))
        if tw < 4 or th < 3:
            continue
        ink = "#ffffff" if contrast("#ffffff", col) >= contrast("#111111", col) else "#111111"
        area = tw * th
        fs = 21 if area > 260 else (16 if area > 130 else (12 if area > 55 else 9.5))
        ax.text(x + tw / 2, y + th / 2 + min(th * 0.12, 1.8), word, ha="center", va="center",
                fontsize=fs, color=ink, fontweight="bold")
        if th > 5.5 and tw > 6:
            ax.text(x + tw / 2, y + th / 2 - min(th * 0.20, 2.6), f"{c}회", ha="center",
                    va="center", fontsize=fs * 0.62, color=ink)

    fig.savefig(out_path, facecolor=SURFACE)
    plt.close(fig)
    return out_path
