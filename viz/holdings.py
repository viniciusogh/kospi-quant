"""보유 현황 인포그래픽 PNG. 표가 모바일에서 잘리고 여백이 뜨는 문제를 이미지로 해결.

종목별 카드 = 수익률(큰 글씨) + 수량·평단→현재가 + 최근 5거래일 미니 막대.
색은 국내 관습(상승 빨강 / 하락 파랑) — 트리맵의 finviz 색과 다른 이유는
이 화면이 '내 손익'이고 나머지 리포트 표와 색을 맞춰야 하기 때문.
"""
import numpy as np
from treemap import SURFACE, SUBINK, contrast, _font

INK = "#ffffff"
UP = "#e05252"      # 상승 빨강 (국내 관습)
DOWN = "#4d8ce0"    # 하락 파랑
FLAT = "#7a8090"
CARD = "#22252e"
EDGE = "#3a3f4d"


def _c(v):
    return UP if v > 0 else (DOWN if v < 0 else FLAT)


def render(data, out_path, w=1200, h=None):
    """data: portfolio.json 구조 (total / positions[hist 포함])."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    pos = data.get("positions") or []
    n = len(pos)
    CARD_H, PAD, HEAD = 132, 14, 150
    h = h or (HEAD + n * (CARD_H + PAD) + 24)

    f = _font()
    if f:
        plt.rcParams["font.family"] = f
    plt.rcParams["axes.unicode_minus"] = False
    fig = plt.figure(figsize=(w / 100, h / 100), dpi=100, facecolor=SURFACE)
    ax = fig.add_axes([0, 0, 1, 1]); ax.set_xlim(0, w); ax.set_ylim(0, h); ax.axis("off")

    t = data.get("total") or {}
    ax.text(28, h - 46, "보유 현황", fontsize=23, fontweight="bold", color=INK)
    ax.text(28, h - 84, f"총평가 {t.get('eval',0):,.0f}원", fontsize=17, color=INK)
    pl = t.get("pl", 0)
    ax.text(232, h - 84, f"{pl:+,.0f}원 ({t.get('ret',0)*100:+.2f}%)",
            fontsize=17, fontweight="bold", color=_c(pl))
    px = data.get("px_date", "")
    basis = f"{px[:4]}-{px[4:6]}-{px[6:]} 정규장 종가" if px else "정규장 종가"
    ax.text(28, h - 114, f"{data.get('asof','')} 갱신 · 가격 {basis} 기준",
            fontsize=11, color=SUBINK)

    for i, p in enumerate(pos):
        y = h - HEAD - (i + 1) * CARD_H - i * PAD
        ax.add_patch(Rectangle((22, y), w - 44, CARD_H, facecolor=CARD, edgecolor=EDGE, lw=1.2))
        star = " ★" if p.get("signal") else ""
        ax.text(44, y + CARD_H - 40, f"{p['name']}{star}", fontsize=18,
                fontweight="bold", color=INK)
        ax.text(44, y + CARD_H - 72, f"{p['broker']} · {p['qty']:,.0f}주", fontsize=11, color=SUBINK)
        ax.text(44, y + 26, f"{p['avg']:,.0f} → {p['price']:,.0f}원   평가 {p['eval']:,.0f}원",
                fontsize=12.5, color="#c8cdd8")
        r = p.get("ret", 0)
        ax.text(w - 48, y + CARD_H - 46, f"{r*100:+.1f}%", fontsize=27, fontweight="bold",
                color=_c(r), ha="right")
        ax.text(w - 48, y + CARD_H - 76, f"{p.get('pl',0):+,.0f}원", fontsize=11.5,
                color=SUBINK, ha="right")

        hist = list(reversed(p.get("hist") or []))     # 오래된 → 최근
        if hist:
            bx, bw_, gap, base = w - 400, 44, 12, y + 34
            mx = max(abs(c) for _, c in hist) or 0.01
            for j, (d, chg) in enumerate(hist):
                bh = max(abs(chg) / mx * 30, 2)
                x0 = bx + j * (bw_ + gap)
                ax.add_patch(Rectangle((x0, base if chg >= 0 else base - bh), bw_, bh,
                                       facecolor=_c(chg)))
                ax.text(x0 + bw_ / 2, base + 40, f"{chg*100:+.1f}", fontsize=9,
                        color=_c(chg), ha="center")
                ax.text(x0 + bw_ / 2, base - 42, f"{int(d[4:6])}/{int(d[6:])}", fontsize=8.5,
                        color=SUBINK, ha="center")
            ax.text(bx - 12, base, "최근 5일", fontsize=10, color=SUBINK, ha="right", va="center")

    fig.savefig(out_path, facecolor=SURFACE)
    plt.close(fig)
    return out_path
