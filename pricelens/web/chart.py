"""Price-over-time line chart as inline SVG (no JS, no charting library).

Design (from the dataviz method):
- one line per series; color follows the entity (store slot is fixed per store, not per rank)
- 2px lines, >=8px markers with a 2px surface ring, hairline solid gridlines
- legend always present for >= 2 series; direct end labels only if they don't collide
- text uses text colors, never series colors; colors come from CSS variables (light/dark)
- hover: native SVG <title> per point (no JS); the page also shows the full table view
"""
from __future__ import annotations

import html
import math
from dataclasses import dataclass
from datetime import date

from ..money import format_amount

MAX_SLOTS = 8  # validated categorical palette size; more series fold into the table only


@dataclass
class Series:
    name: str
    slot: int                       # 1..8, fixed per entity
    points: list[tuple[date, int]]  # (day, minor units), any order


def nice_ticks(lo: float, hi: float, target: int = 5) -> list[float]:
    """Round tick values covering [lo, hi] (1/2/2.5/5 x 10^n steps)."""
    if hi <= lo:
        hi = lo + 1
    raw = (hi - lo) / max(target - 1, 1)
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    start = math.floor(lo / step) * step
    ticks, v = [], start
    while v <= hi + step * 1e-9:
        ticks.append(round(v, 10))
        v += step
    if ticks[-1] < hi:
        ticks.append(round(ticks[-1] + step, 10))
    return ticks


def _date_ticks(d0: date, d1: date, target: int = 5) -> list[date]:
    span = max((d1 - d0).days, 1)
    step = max(1, round(span / (target - 1)))
    out = [date.fromordinal(d0.toordinal() + i) for i in range(0, span + 1, step)]
    if (d1 - out[-1]).days > step / 2:
        out.append(d1)
    return out


def line_chart_svg(series: list[Series], currency: str = "USD", title: str = "",
                   width: int = 960, height: int = 340) -> str:  # ~ the page's content width, so text isn't scaled up
    """Returns SVG markup. All text is HTML-escaped."""
    series = [s for s in series if s.points][:MAX_SLOTS]
    if not series:
        return ""
    e = html.escape
    pad_l, pad_r, pad_t, pad_b = 64, 132, 16, 36
    pw, ph = width - pad_l - pad_r, height - pad_t - pad_b

    days = [d for s in series for d, _ in s.points]
    vals = [v for s in series for _, v in s.points]
    d0, d1 = min(days), max(days)
    if d0 == d1:
        d0, d1 = date.fromordinal(d0.toordinal() - 3), date.fromordinal(d1.toordinal() + 3)
    lo, hi = min(vals), max(vals)
    margin = max((hi - lo) * 0.15, 25)
    yt = nice_ticks(max(lo - margin, 0), hi + margin)
    ylo, yhi = yt[0], yt[-1]

    def x(d: date) -> float:
        return pad_l + pw * (d.toordinal() - d0.toordinal()) / (d1.toordinal() - d0.toordinal())

    def y(v: float) -> float:
        return pad_t + ph * (1 - (v - ylo) / (yhi - ylo))

    out = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
           f'aria-label="{e(title or "Price over time")}" xmlns="http://www.w3.org/2000/svg">']
    # gridlines + y labels (recessive)
    for t in yt:
        yy = y(t)
        out.append(f'<line class="grid" x1="{pad_l}" x2="{pad_l + pw}" y1="{yy:.1f}" y2="{yy:.1f}"/>')
        out.append(f'<text class="axis" x="{pad_l - 8}" y="{yy + 4:.1f}" text-anchor="end">'
                   f'{e(format_amount(int(t), currency))}</text>')
    for d in _date_ticks(d0, d1):
        out.append(f'<text class="axis" x="{x(d):.1f}" y="{height - 12}" text-anchor="middle">'
                   f'{e(d.strftime("%b %d"))}</text>')

    ends = []
    for s in series:
        pts = sorted(s.points)
        coords = [(x(d), y(v)) for d, v in pts]
        if len(coords) > 1:
            path = " ".join(f"{'M' if i == 0 else 'L'}{cx:.1f},{cy:.1f}" for i, (cx, cy) in enumerate(coords))
            out.append(f'<path class="line s{s.slot}" d="{path}"/>')
        for (d, v), (cx, cy) in zip(pts, coords):
            out.append(f'<circle class="dot s{s.slot}" cx="{cx:.1f}" cy="{cy:.1f}" r="4">'
                       f'<title>{e(s.name)} · {d.isoformat()} · {e(format_amount(v, currency))}</title></circle>')
        ends.append((coords[-1], s, pts[-1][1]))

    # Direct end labels only if none collide (nudging would detach them; the legend covers it).
    ys = sorted(c[1] for c, _, _ in ends)
    if all(b - a >= 16 for a, b in zip(ys, ys[1:])):
        for (cx, cy), s, v in ends:
            out.append(f'<text class="label" x="{cx + 10:.1f}" y="{cy + 4:.1f}">'
                       f'{e(s.name)} {e(format_amount(v, currency))}</text>')
    out.append("</svg>")
    return "".join(out)


def legend_html(series: list[Series]) -> str:
    items = "".join(
        f'<li><svg width="18" height="8" aria-hidden="true"><line class="line s{s.slot}" x1="1" x2="17" y1="4" y2="4"/>'
        f'</svg>{html.escape(s.name)}</li>' for s in series[:MAX_SLOTS])
    return f'<ul class="legend">{items}</ul>' if len(series) >= 2 else ""
