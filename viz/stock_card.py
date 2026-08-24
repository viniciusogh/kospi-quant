"""종목 심층분석 카드 PNG — 프로즈를 '숫자 게이지 + 촉매/리스크 대조'로 압축.

줄글을 그대로 이미지로 옮기면 스크린샷일 뿐이라 이득이 없다. 이득은 성격이 다른 것을
분해할 때 나온다: 순위는 게이지, 수급은 막대, 판단은 3줄 대조. 원문 8섹션은 카드 아래
토글에 그대로 남으므로 잃는 건 없다.

수급은 KIS 원본 숫자를 직접 그린다 — 프로즈는 캐시 재사용으로 며칠 묵으면 실제 수급과
어긋날 수 있지만(2026-08-25 GS건설 사례) 막대는 매 실행 새로 계산돼 틀릴 수가 없다.
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, Circle

from treemap import _font, SURFACE, INK, SUBINK

GOOD, MID, BAD = "#2f9e5e", "#8a8f9e", "#c0554f"
CARD, LINE = "#333846", "#3d4252"
UP, DOWN = "#e05252", "#4d8ce0"          # 등락은 국내 관습(상승 빨강) — 다른 보유 표와 통일


def render(d, out, w=860):
    """d: name·code·ret(보유수익률)·chg(당일)·sector·qty·avg·price·eval·pl·oneline·gauges·flow·bull·bear·target"""
    f = _font()
    if f:
        plt.rcParams["font.family"] = f
    plt.rcParams["axes.unicode_minus"] = False

    g, nb = d.get("gauges") or [], max(len(d.get("bull") or []), len(d.get("bear") or []))
    gh = 44 + 50 * len(g)
    bh = 40 + 34 * nb
    h = 166 + gh + 14 + 176 + 14 + bh + 40
    fig = plt.figure(figsize=(w / 100, h / 100), dpi=100, facecolor=SURFACE)
    ax = fig.add_axes([0, 0, 1, 1]); ax.set_xlim(0, w); ax.set_ylim(0, h); ax.axis("off")
    P = 30

    def panel(y0, y1, title=None):
        ax.add_patch(FancyBboxPatch((P, y0), w - 2 * P, y1 - y0,
                                    boxstyle="round,pad=0,rounding_size=10",
                                    facecolor=CARD, edgecolor="none"))
        if title:
            ax.text(P + 22, y1 - 27, title, fontsize=15, fontweight="bold", color=SUBINK, va="center")

    # 헤더
    ax.text(P, h - 46, d["name"], fontsize=30, fontweight="bold", color=INK, va="center")
    ax.text(P + len(d["name"]) * 31 + 12, h - 49, d["code"], fontsize=15, color=SUBINK, va="center")
    # 큰 숫자는 **보유 수익률** — 바로 아래 토글 제목과 같은 값이어야 헷갈리지 않는다.
    # 당일 등락은 오른쪽 보조줄로 뺀다(둘 다 큰 글씨면 어느 게 오늘인지 모른다).
    ax.text(w - P, h - 46, f"{d['ret']*100:+.1f}%", fontsize=30, fontweight="bold",
            color=(UP if d["ret"] > 0 else DOWN), va="center", ha="right")
    ax.text(P, h - 80, f"{d['sector']} · 보유 {d['qty']:,.0f}주 · {d['avg']:,.0f} → {d['price']:,.0f}원",
            fontsize=15, color=SUBINK, va="center")
    ax.text(w - P, h - 80, f"평가 {d['eval']:,.0f}원 · {d['pl']:+,.0f}원 · 오늘 {d['chg']*100:+.1f}%",
            fontsize=15, color=SUBINK, va="center", ha="right")

    if d.get("oneline"):
        panel(h - 152, h - 104)
        ax.text(P + 22, h - 128, d["oneline"][:46], fontsize=16, color=INK, va="center")

    # 섹터 내 위치 — 왼쪽이 좋음. PER/PBR/부채는 낮을수록, ROE 는 높을수록 1위.
    top = h - 166
    if g:
        panel(top - gh, top, f"섹터 내 위치  ({d['sector']} {d['sec_n']}개)")
        ax.text(w - P - 22, top - 26, "◀ 좋음     나쁨 ▶", fontsize=12, color=SUBINK,
                va="center", ha="right")
        for i, (lab, val, rank, n, note) in enumerate(g):
            yy = top - 60 - i * 50
            ax.text(P + 22, yy, lab, fontsize=16, fontweight="bold", color=INK, va="center")
            ax.text(P + 200, yy, val, fontsize=16, color=INK, va="center", ha="right")
            x0, tw = P + 215, 290
            ax.plot([x0, x0 + tw], [yy] * 2, color=LINE, lw=7, solid_capstyle="round")
            rr = (rank - 0.5) / n
            col = GOOD if rr < 1 / 3 else (MID if rr < 2 / 3 else BAD)
            ax.add_patch(Circle((x0 + tw * rr, yy), 9, facecolor=col, edgecolor=CARD, lw=2.5, zorder=3))
            ax.text(x0 + tw + 20, yy, f"{rank}/{n}위", fontsize=14, color=SUBINK, va="center")
            ax.text(x0 + tw + 110, yy, note, fontsize=14, fontweight="bold", color=col, va="center")

    # 수급 — 순매수 초록·순매도 빨강(국내 등락 관습과 별개로 매수/매도 방향 표기)
    top -= gh + 14
    panel(top - 176, top, "최근 5일 순매수 (억원)")
    fl = d.get("flow") or []
    mx = max((abs(v) for _, v in fl), default=0) or 1
    for i, (lab, v) in enumerate(fl):
        yy = top - 62 - i * 44
        ax.text(P + 22, yy, lab, fontsize=16, color=INK, va="center")
        bw = abs(v) / mx * 330
        ax.add_patch(FancyBboxPatch((P + 120, yy - 11), bw, 22,
                                    boxstyle="round,pad=0,rounding_size=4",
                                    facecolor=(GOOD if v >= 0 else BAD), edgecolor="none"))
        ax.text(P + 132 + bw, yy, f"{v:+,.0f}", fontsize=15, fontweight="bold", color=INK, va="center")

    # 촉매 / 리스크
    top -= 176 + 14
    panel(top - bh, top)
    ax.text(P + 22, top - 28, "▲ 상승 촉매", fontsize=15, fontweight="bold", color=GOOD, va="center")
    ax.text(w / 2 + 8, top - 28, "▼ 리스크", fontsize=15, fontweight="bold", color=BAD, va="center")
    for i, t in enumerate(d.get("bull") or []):
        ax.text(P + 22, top - 64 - i * 34, "· " + t[:26], fontsize=14, color="#d2d6df", va="center")
    for i, t in enumerate(d.get("bear") or []):
        ax.text(w / 2 + 8, top - 64 - i * 34, "· " + t[:26], fontsize=14, color="#d2d6df", va="center")

    if d.get("target"):
        t = d["target"]                      # 한글은 글자폭이 넓어 46자가 w=860 의 한계
        ax.text(P, 16, t[:46] + ("…" if len(t) > 46 else ""), fontsize=14, color=SUBINK, va="bottom")
    fig.savefig(out, facecolor=SURFACE, dpi=100)
    plt.close(fig)
    return out
