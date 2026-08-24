"""섹터/테마 트리맵 PNG (finviz 풍 다크 테마). 크기=시총, 색=수익률.

색 규칙: 다이버징(극성) → 두 색상 + 중립 회색 중간점, **팔당 동일 단계**.
- finviz 관습을 따라 **상승 초록 / 하락 빨강** (국내 리포트 표기와 반대임을 문서 표기로 보완).
- 두 팔의 OKLab L·C 를 일치시켜 같은 크기의 변화가 같은 강도로 보이게 한다.
- 라벨은 흰색 고정이므로 모든 단계에서 흰색 대비 4.5 이상을 계산해 확인한다.
"""
import numpy as np

# finviz 풍 다크 팔레트 (앵커에서 램프를 계산해 만든다 — 눈대중 금지)
SURFACE     = "#2b2f3b"     # 배경
NEUTRAL     = "#464a58"     # 다이버징 중립 중간점 (0% 근처)
GREEN_ANCHOR = "#35a34e"    # 상승 팔 색상
RED_ANCHOR   = "#b04a4a"    # 하락 팔 색상
INK          = "#ffffff"    # 라벨 (다크 타일이므로 흰색 고정)
SUBINK       = "#9aa0ae"    # 보조 텍스트

# 팔당 4단계 명도 (다크 배경에서 흰 라벨 대비를 확보하는 범위)
# 초록은 같은 OKLab L 에서도 휘도가 높아 흰 라벨 대비가 빨강보다 낮다.
# 상한 0.55 는 **초록**이 흰 라벨 대비 4.5 를 넘기는 경계(실측). 양팔 L 은 동일하게 유지.
ARM_L = [0.395, 0.447, 0.499, 0.551]
# 당일 등락 기준 구간. 5일 기준(±7%)을 쓰면 당일 변동(보통 ±2%)이 전부 회색으로 뭉친다.
BANDS = [0.3, 1.0, 2.0, 3.5]
SECTOR_AREA_EXP = 0.5      # 섹터 면적 = 시총^0.5 (대형주 지배 완화, 순서 보존)


def _srgb_to_lin(c):
    c = c / 255.0
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def _lin_to_srgb(c):
    c = np.clip(c, 0, 1)
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * c ** (1 / 2.4) - 0.055) * 255.0


_M1 = np.array([[0.4122214708, 0.5363325363, 0.0514459929],
                [0.2119034982, 0.6806995451, 0.1073969566],
                [0.0883024619, 0.2817188376, 0.6299787005]])
_M2 = np.array([[0.2104542553, 0.7936177850, -0.0040720468],
                [1.9779984951, -2.4285922050, 0.4505937099],
                [0.0259040371, 0.7827717662, -0.8086757660]])


def hex_to_oklab(h):
    rgb = np.array([int(h[i:i + 2], 16) for i in (1, 3, 5)], float)
    lms = _M1 @ _srgb_to_lin(rgb)
    return _M2 @ np.cbrt(lms)


def oklab_to_hex(lab):
    lms = np.linalg.inv(_M2) @ lab
    rgb = _lin_to_srgb(np.linalg.inv(_M1) @ (lms ** 3))
    return "#%02x%02x%02x" % tuple(int(round(v)) for v in np.clip(rgb, 0, 255))


def _arm(anchor_hex):
    """앵커 색상(hue)에 ARM_L 명도를 입힌 4단계. 채도는 색역 안에서 최대,
    단 왕복 후 L 오차가 커지면(8비트 반올림) 채도를 낮춰 L 을 보존한다."""
    a = hex_to_oklab(anchor_hex)
    hue = np.arctan2(a[2], a[1])
    c0 = np.hypot(a[1], a[2])
    out = []
    for L in ARM_L:
        C = c0            # 앵커 채도 그대로 (1.25배는 형광색으로 떠서 폐기)
        for _ in range(80):
            h = oklab_to_hex(np.array([L, C * np.cos(hue), C * np.sin(hue)]))
            if abs(hex_to_oklab(h)[0] - L) < 0.004:
                break
            C *= 0.95
        out.append(h)
    return out


UP_STEPS   = _arm(GREEN_ANCHOR)     # 약 → 강 (명도 상승)
DOWN_STEPS = _arm(RED_ANCHOR)


def color_for(pct):
    """수익률(%) → 타일 색. 0 근처는 중립 회색. 상승 초록 / 하락 빨강(finviz 관습)."""
    v = abs(pct)
    if v < BANDS[0]:
        return NEUTRAL
    arm = UP_STEPS if pct > 0 else DOWN_STEPS
    for i, b in enumerate(BANDS[1:], 1):
        if v < b:
            return arm[i - 1]
    return arm[-1]


def _lum(h):
    rgb = np.array([int(h[i:i + 2], 16) for i in (1, 3, 5)], float)
    lin = _srgb_to_lin(rgb)
    return float(0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2])


def contrast(a, b):
    la, lb = _lum(a), _lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def ink_on(bg):
    """다크 테마이므로 라벨은 흰색 고정. 대비는 검증에서 확인한다."""
    return INK


def squarify(vals, x, y, w, h):
    """squarified treemap — 타일 종횡비를 1에 가깝게 (얇고 긴 타일 방지)."""
    if not len(vals):
        return []
    vals = np.asarray(vals, float)
    total = vals.sum()
    if total <= 0:
        return []
    scaled = vals / total * (w * h)
    rects, i = [], 0
    while i < len(scaled):
        rest = scaled[i:]
        short = min(w, h)
        best, n = None, 1
        for k in range(1, len(rest) + 1):
            s = rest[:k].sum()
            side = s / short
            worst = max(max(side / (v / side), (v / side) / side) for v in rest[:k]) if s > 0 else 1e9
            if best is None or worst <= best:
                best, n = worst, k
            else:
                break
        s = rest[:n].sum()
        if w >= h:
            cw = s / h
            oy = y
            for v in rest[:n]:
                ch = v / cw if cw else 0
                rects.append((x, oy, cw, ch)); oy += ch
            x += cw; w -= cw
        else:
            ch = s / w
            ox = x
            for v in rest[:n]:
                cw = v / ch if ch else 0
                rects.append((ox, y, cw, ch)); ox += cw
            y += ch; h -= ch
        i += n
    return rects


# ── 렌더링 ──────────────────────────────────────────────────────────
def _font():
    """한글 폰트. 로컬(macOS)·클라우드(Ubuntu+fonts-nanum) 양쪽에서 되는 후보를 순서대로."""
    import matplotlib.font_manager as fm
    have = {f.name for f in fm.fontManager.ttflist}
    for c in ("Apple SD Gothic Neo", "AppleGothic", "NanumGothic", "Nanum Gothic",
              "Noto Sans CJK KR", "Noto Sans KR", "Malgun Gothic", "DejaVu Sans"):
        if c in have:
            return c
    return None


def render(sectors, asof, out_path, w=1600, h=1180, strip=None):
    """sectors: [(섹터명, 오늘%, 시총합)] — 섹터 단위 타일만 그린다(개별 종목 없음).

    표시는 **오늘 기준으로 통일**한다. 예전엔 큰 숫자가 5일 수익률, 작은 줄이 "오늘 ..." 이라
    큰 숫자를 오늘로 오해하기 쉬웠다(사용자 지적). 기간별 비교는 아래 표가 담당한다.

    finviz 관습: 다크 배경 · 상승 초록 / 하락 빨강 · 타일 안에 이름과 % 를 크게.
    크기는 시총^0.5 (그대로 쓰면 삼성전자·SK하이닉스가 화면 60%를 먹어 테마가 안 보인다).
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    f = _font()
    if f:
        plt.rcParams["font.family"] = f
    plt.rcParams["axes.unicode_minus"] = False

    fig = plt.figure(figsize=(w / 100, h / 100), dpi=100, facecolor=SURFACE)
    STRIP_H = 0.185 if strip else 0.0        # 하단 요약 스트립 높이(도형 좌표 비율)
    ax = fig.add_axes([0.006, 0.006 + STRIP_H, 0.988, 0.885 - STRIP_H])
    ax.set_xlim(0, 100); ax.set_ylim(0, 100); ax.axis("off")
    ax.set_facecolor(SURFACE)

    fig.text(0.006, 0.955, f"섹터·테마 장세  {asof}", fontsize=20, fontweight="bold", color=INK)
    fig.text(0.006, 0.918,
             "타일 크기 = 시가총액(√ 압축)   ·   색·숫자 = 당일 등락률(상승 초록 / 하락 빨강)"
             "   ·   섹터/테마 등가중   ·   기간별 비교는 아래 표",
             fontsize=11, color=SUBINK)

    # 범례: 두 팔 + 중립 중간점
    lx, ly, bw = 0.655, 0.922, 0.0255
    order = [(-BANDS[3], DOWN_STEPS[3]), (-BANDS[2], DOWN_STEPS[2]), (-BANDS[1], DOWN_STEPS[1]),
             (-BANDS[0], DOWN_STEPS[0]), (0, NEUTRAL),
             (BANDS[0], UP_STEPS[0]), (BANDS[1], UP_STEPS[1]),
             (BANDS[2], UP_STEPS[2]), (BANDS[3], UP_STEPS[3])]
    for i, (_, c) in enumerate(order):
        fig.patches.append(plt.Rectangle((lx + i * bw, ly), bw - 0.003, 0.023,
                                        facecolor=c, edgecolor=SURFACE, lw=1.2,
                                        transform=fig.transFigure, figure=fig))
    fig.text(lx - 0.008, ly + 0.007, f"-{BANDS[-1]:.1f}%", fontsize=9.5, color=SUBINK, ha="right")
    fig.text(lx + 9 * bw + 0.004, ly + 0.007, f"+{BANDS[-1]:.1f}%", fontsize=9.5, color=SUBINK)

    caps = [float(c) ** SECTOR_AREA_EXP for (_, _, c) in sectors]
    rects = squarify(caps, 0, 0, 100, 100)
    GAP = 0.42                                   # 타일 사이 표면 간격
    for (name, chg, _cap), (x, y, rw, rh) in zip(sectors, rects):
        tw, th = max(rw - GAP, 0.1), max(rh - GAP, 0.1)
        col = color_for(chg)
        ax.add_patch(Rectangle((x, y), tw, th, facecolor=col, edgecolor=SURFACE, lw=1.0))
        cx, cy = x + tw / 2, y + th / 2
        # 타일 크기에 맞춰 글자 크기 결정 (작으면 이름만, 더 작으면 생략)
        area = tw * th
        nm_fs = 20 if area > 200 else (15 if area > 90 else (11.5 if area > 38 else 9))
        if tw < 4.2 or th < 2.6:
            continue
        show_pct = th > 4.6 and tw > 6
        # 간격을 타일 높이 비례로만 두면 큰 타일에서 글자가 위아래로 흩어진다(실측: 전기·전자).
        # 비례값에 절대 상한을 씌워 텍스트 덩어리를 중앙에 모은다.
        d1 = min(th * 0.13, 2.1)
        ax.text(cx, cy + (d1 if show_pct else 0), name, ha="center", va="center",
                fontsize=nm_fs, color=INK, fontweight="bold")
        if show_pct:
            ax.text(cx, cy - min(th * 0.19, 3.0), f"{chg:+.1f}%", ha="center", va="center",
                    fontsize=nm_fs * 0.88, color=INK)

    # ── 하단 요약 스트립 (강세/약세 + 대표 종목) ──────────────────────
    # 표·목록은 모바일에서 잘리고 여백이 뜬다(사용자 지적) → 이미지 안에 넣으면 안 깨진다.
    if strip:
        sa = fig.add_axes([0.006, 0.008, 0.988, STRIP_H - 0.012])
        sa.set_xlim(0, 100); sa.set_ylim(0, 100); sa.axis("off")
        sa.add_patch(plt.Rectangle((0, 0), 100, 100, facecolor="#22252e",
                                   edgecolor="#3a3f4d", lw=1.2))
        for ci, (title, rows, accent) in enumerate(
                [("▲ 오늘 강세", strip.get("hot", []), UP_STEPS[-1]),
                 ("▼ 오늘 약세", strip.get("cold", []), DOWN_STEPS[-1])]):
            x0 = 2.5 + ci * 49.5
            sa.text(x0, 84, title, fontsize=13, fontweight="bold", color=INK)
            for ri, (sec, pct, stocks) in enumerate(rows[:3]):
                y = 62 - ri * 21
                sa.add_patch(plt.Rectangle((x0, y - 5), 1.0, 15, facecolor=accent))
                sa.text(x0 + 2.2, y + 4.5, sec, fontsize=12, fontweight="bold", color=INK)
                sa.text(x0 + 14.5, y + 4.5, f"{pct:+.1f}%", fontsize=12, fontweight="bold",
                        color=accent)
                if stocks:
                    txt = "   ".join(f"{n} {c*100:+.1f}%" for n, c in stocks[:3])
                    sa.text(x0 + 2.2, y - 3.5, txt[:52], fontsize=9.5, color="#aab0bd")

    fig.savefig(out_path, facecolor=SURFACE)
    plt.close(fig)
    return out_path
