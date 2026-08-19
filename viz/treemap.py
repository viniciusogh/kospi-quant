"""섹터/테마 트리맵 PNG 생성 (크기=시총, 색=5일 수익률).

색 규칙: 다이버징(상승/하락 = 극성) → 두 색상 + 중립 회색 중간점, **팔당 동일 단계**.
- 파랑 램프는 기준 팔레트 값을 그대로 사용, 빨강 팔은 같은 OKLab 명도에 빨강 색상만 입혀
  양쪽 팔의 명도를 일치시킨다(다이버징 검증 기준은 명도 단조성이다).
- 국내 관습에 맞춰 **상승=빨강 / 하락=파랑** (기존 리포트 표기와 통일).
- 라벨 색은 눈대중하지 않고 타일과의 대비를 계산해 흰색/검정 중 고른다.
"""
import numpy as np

# 기준 팔레트: 파랑 시퀀셜 램프 (명도 단조 감소) — 이 값들의 OKLab L 을 빨강 팔에 미러링
BLUE_STEPS = ["#9ec5f4", "#5598e7", "#256abf", "#104281"]   # 200 / 350 / 500 / 650
RED_ANCHOR = "#e34948"                                       # 기준 팔레트 categorical red
NEUTRAL    = "#f0efec"                                       # 기준 팔레트 다이버징 중간점(light)
SURFACE    = "#fcfcfb"                                       # 기준 팔레트 light chart surface
INK        = "#1a1a19"

# 팔 경계 (5일 수익률 %) — 팔당 4단계, 좌우 대칭
BANDS = [0.75, 2.0, 4.0, 7.0]
SECTOR_AREA_EXP = 0.5      # 섹터 면적 = 시총^0.5 (대형주 지배 완화, 순서는 보존)


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


def _red_arm():
    """빨강 팔 = 파랑 팔의 **L·C 를 그대로** 쓰고 색상(hue)만 빨강 앵커 것으로.
    다이버징은 '팔당 동일 단계' 가 규칙이므로 명도·채도를 맞춘다.
    채도를 최대화하면 색역 경계에서 8비트 반올림 때문에 L 이 밀린다(실측 ΔL 0.15) →
    파랑과 같은 채도에서 시작해 왕복 L 오차가 허용치 안에 들 때까지만 낮춘다."""
    ra = hex_to_oklab(RED_ANCHOR)
    hue = np.arctan2(ra[2], ra[1])
    out = []
    for b in BLUE_STEPS:
        lab = hex_to_oklab(b)
        L, C = lab[0], np.hypot(lab[1], lab[2])
        for _ in range(60):
            h = oklab_to_hex(np.array([L, C * np.cos(hue), C * np.sin(hue)]))
            if abs(hex_to_oklab(h)[0] - L) < 0.004:      # 왕복 후에도 L 유지되면 채택
                break
            C *= 0.95
        out.append(h)
    return out


RED_STEPS = _red_arm()


def color_for(pct):
    """5일 수익률(%) → 타일 색. 0 근처는 중립 회색."""
    v = abs(pct)
    if v < BANDS[0]:
        return NEUTRAL
    arm = RED_STEPS if pct > 0 else BLUE_STEPS
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
    """라벨 색: 대비를 계산해 흰색/먹색 중 더 잘 보이는 쪽."""
    return "#ffffff" if contrast("#ffffff", bg) >= contrast(INK, bg) else INK


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


def render(groups, asof, out_path, w=1600, h=1000):
    """groups: [(섹터명, 섹터5일%, 시총합, [(종목명, 5일%, 시총)…])] — 시총합 내림차순."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    f = _font()
    if f:
        plt.rcParams["font.family"] = f
    plt.rcParams["axes.unicode_minus"] = False

    fig = plt.figure(figsize=(w / 100, h / 100), dpi=100, facecolor=SURFACE)
    ax = fig.add_axes([0.008, 0.008, 0.984, 0.90])
    ax.set_xlim(0, 100); ax.set_ylim(0, 100); ax.axis("off")
    ax.set_facecolor(SURFACE)

    fig.text(0.008, 0.965, f"섹터·테마 장세  {asof}", fontsize=19, fontweight="bold", color=INK)
    fig.text(0.008, 0.933,
             "타일 크기 = 시가총액(√ 압축 — 대형주 지배 완화)   ·   색 = 5일 수익률"
             "(상승 빨강 / 하락 파랑)   ·   섹터/테마 등가중",
             fontsize=11, color="#6b6b68")

    # 범례 (다이버징: 두 팔 + 중립 중간점)
    lx, ly, bw = 0.62, 0.936, 0.026
    order = [(-BANDS[3], BLUE_STEPS[3]), (-BANDS[2], BLUE_STEPS[2]), (-BANDS[1], BLUE_STEPS[1]),
             (-BANDS[0], BLUE_STEPS[0]), (0, NEUTRAL),
             (BANDS[0], RED_STEPS[0]), (BANDS[1], RED_STEPS[1]),
             (BANDS[2], RED_STEPS[2]), (BANDS[3], RED_STEPS[3])]
    for i, (_, c) in enumerate(order):
        fig.patches.append(plt.Rectangle((lx + i * bw, ly), bw - 0.003, 0.022,
                                        facecolor=c, edgecolor=SURFACE, lw=1.2,
                                        transform=fig.transFigure, figure=fig))
    fig.text(lx - 0.008, ly + 0.006, "-7%", fontsize=9, color="#6b6b68", ha="right")
    fig.text(lx + 9 * bw + 0.004, ly + 0.006, "+7%", fontsize=9, color="#6b6b68")

    # 시총을 그대로 쓰면 삼성전자·SK하이닉스가 화면 60%를 먹어 테마가 안 보인다(실측).
    # 순환매 파악이 목적이므로 제곱근으로 압축한다 — 순서는 보존, 격차만 줄인다.
    caps = [float(g[2]) ** SECTOR_AREA_EXP for g in groups]
    rects = squarify(caps, 0, 0, 100, 100)
    GAP = 0.45                                  # 타일 사이 표면 간격 (마크 규격)
    for (sec, s5, cap, stocks), (x, y, rw, rh) in zip(groups, rects):
        x, y, rw, rh = x + GAP, y + GAP, max(rw - 2 * GAP, 0.1), max(rh - 2 * GAP, 0.1)
        hdr = min(3.2, rh * 0.30)               # 섹터 머리말 띠
        # 종목 sub-tile
        sub = squarify([s[2] for s in stocks], x, y, rw, max(rh - hdr, 0.1)) if stocks else []
        for (nm, p5, _), (sx, sy, sw, sh) in zip(stocks, sub):
            c = color_for(p5)
            ax.add_patch(Rectangle((sx, sy), max(sw - GAP, 0.05), max(sh - GAP, 0.05),
                                   facecolor=c, edgecolor=SURFACE, lw=0.8))
            if sw > 6.0 and sh > 3.4:           # 글자가 들어갈 만큼 넓을 때만 라벨
                ink = ink_on(c)
                fs = 10.5 if sw > 11 else 8.5
                ax.text(sx + sw / 2, sy + sh / 2 + sh * 0.10, nm[:9], ha="center", va="center",
                        fontsize=fs, color=ink, fontweight="bold")
                ax.text(sx + sw / 2, sy + sh / 2 - sh * 0.22, f"{p5:+.1f}%", ha="center",
                        va="center", fontsize=fs - 1.5, color=ink)
        # 섹터 머리말
        ax.add_patch(Rectangle((x, y + rh - hdr), rw, hdr, facecolor="#e8e7e4",
                               edgecolor=SURFACE, lw=0.8))
        if rw > 3.2:
            nm_fs = 11 if rw > 12 else (9.5 if rw > 7 else 8)
            # 이름과 % 가 겹치지 않을 만큼 넓을 때만 % 를 오른쪽에 (실측: 좁은 타일에서 붙어버림)
            room = rw > (len(sec) * (nm_fs * 0.075) + 4.2)
            ax.text(x + 0.45, y + rh - hdr / 2, sec, ha="left", va="center",
                    fontsize=nm_fs, color=INK, fontweight="bold")
            if room:
                ax.text(x + rw - 0.45, y + rh - hdr / 2, f"{s5:+.1f}%", ha="right", va="center",
                        fontsize=nm_fs - 1,
                        color="#b13f3c" if s5 > 0 else ("#256abf" if s5 < 0 else "#6b6b68"),
                        fontweight="bold")

    fig.savefig(out_path, facecolor=SURFACE, bbox_inches=None)
    plt.close(fig)
    return out_path
