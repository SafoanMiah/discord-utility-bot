"""matplotlib rendering: bucketed data in, PNG bytes out.

No aggregation, no timezone logic, no database. Callers (bot.py) convert
analysis output to display strings/series and pass them in. Uses Figure
objects directly so it can run under asyncio.to_thread safely.
"""
from __future__ import annotations

import io
from typing import Sequence

from matplotlib.patches import FancyBboxPatch
from matplotlib.ticker import FuncFormatter, MaxNLocator

from . import theme
from .analysis import WEEKDAYS

_HOUR_TICKS = list(range(0, 24, 3))
_WEEKDAY_ABBR = [d[:3] for d in WEEKDAYS]


# -- formatting helpers (pure string, shared with bot.py) --------------------

def fmt_count(n: int) -> str:
    return f"{n:,}"


def fmt_duration(seconds: float) -> str:
    """3725 -> '1h 2m'; 180 -> '3m'; 30 -> '<1m'; 0 -> '0m'."""
    minutes = int(seconds // 60)
    if seconds > 0 and minutes == 0:
        return "<1m"
    hours, minutes = divmod(minutes, 60)
    if hours == 0:
        return f"{minutes}m"
    if minutes == 0:
        return f"{hours}h"
    return f"{hours}h {minutes}m"


def fmt_hour_range(hour: int) -> str:
    return f"{hour:02d}:00–{(hour + 1) % 24:02d}:00"


def _compact(value: float) -> str:
    if value >= 10_000:
        return f"{value / 1000:.0f}k"
    if value >= 1_000:
        return f"{value / 1000:.1f}k"
    return f"{value:,.0f}"


def _fmt_hours(hours: float) -> str:
    return f"{hours:.1f}".rstrip("0").rstrip(".") + "h"


def _fmt_minutes(minutes: float) -> str:
    return f"{round(minutes)}m"


def _voice_series(values: Sequence[float]) -> tuple[list[float], str, object]:
    """Voice minutes scale badly past a few hours: switch the whole series to
    hours (unit is named in the panel title, so ticks stay unit-consistent)."""
    if max(values, default=0) >= 180:
        return [v / 60 for v in values], "hours", _fmt_hours
    return list(values), "minutes", _fmt_minutes


# -- panel builders -----------------------------------------------------------

def _empty_panel(ax, title: str, note: str) -> None:
    ax.set_title(title)
    ax.grid(visible=False)
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.text(0.5, 0.45, note, transform=ax.transAxes, ha="center", va="center",
            color=theme.MUTED, fontsize=10.5)


def _bar_panel(ax, values: Sequence[float], color: str, title: str,
               kind: str, peak_fmt, empty_note: str) -> None:
    """One single-series bar panel. kind is 'hour' (24 bars) or 'weekday' (7)."""
    if not any(values):
        _empty_panel(ax, title, empty_note)
        return

    theme.style_axis(ax)
    ax.set_title(title)
    positions = range(len(values))
    ax.bar(positions, values, width=0.72, color=color, zorder=3)

    if kind == "hour":
        ax.set_xticks(_HOUR_TICKS, [f"{h:02d}" for h in _HOUR_TICKS])
        ax.set_xlim(-0.7, 23.7)
    else:
        ax.set_xticks(range(7), _WEEKDAY_ABBR)
        ax.set_xlim(-0.7, 6.7)

    peak = max(values)
    ax.set_ylim(0, peak * 1.22)
    ax.yaxis.set_major_locator(MaxNLocator(nbins=4, integer=True))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: _compact(v) if v else "0"))

    # Selective direct label: the peak bar only.
    peak_idx = max(positions, key=values.__getitem__)
    ax.annotate(peak_fmt(peak), (peak_idx, peak), xytext=(0, 5),
                textcoords="offset points", ha="center",
                color=theme.TEXT, fontsize=9.5, fontweight="medium", zorder=4)


def _to_png(fig) -> io.BytesIO:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=theme.BG)
    buf.seek(0)
    return buf


# -- public renderers ---------------------------------------------------------

def render_activity(name: str, subtitle: str,
                    msg_hours: Sequence[float], vc_hours: Sequence[float],
                    msg_weekdays: Sequence[float], vc_weekdays: Sequence[float]) -> io.BytesIO:
    """/activity composite: hour-of-day chat, hour-of-day voice, and a
    day-of-week row split into two single-series mini-panels (counts and
    minutes are different units, so they never share an axis)."""
    fig = theme.new_figure(9.2, 10.6)
    gs = fig.add_gridspec(3, 2, left=0.07, right=0.955, top=0.855, bottom=0.055,
                          hspace=0.52, wspace=0.24)
    theme.header(fig, name, subtitle)

    vc_h, h_unit, h_fmt = _voice_series(vc_hours)
    vc_w, w_unit, w_fmt = _voice_series(vc_weekdays)
    _bar_panel(fig.add_subplot(gs[0, :]), msg_hours, theme.ACCENT,
               "Messages · by hour of day", "hour", _compact, "No messages yet")
    _bar_panel(fig.add_subplot(gs[1, :]), vc_h, theme.SECONDARY,
               f"Voice · {h_unit} by hour of day", "hour", h_fmt, "No voice activity yet")
    _bar_panel(fig.add_subplot(gs[2, 0]), msg_weekdays, theme.ACCENT,
               "Messages · by day", "weekday", _compact, "No messages yet")
    _bar_panel(fig.add_subplot(gs[2, 1]), vc_w, theme.SECONDARY,
               f"Voice · {w_unit} by day", "weekday", w_fmt, "No voice activity yet")
    return _to_png(fig)


def render_activity_day(name: str, subtitle: str,
                        msg_hours: Sequence[float], vc_hours: Sequence[float]) -> io.BytesIO:
    """/activity with a weekday filter: chat and voice hour-of-day panels."""
    fig = theme.new_figure(9.2, 7.4)
    gs = fig.add_gridspec(2, 1, left=0.07, right=0.955, top=0.78, bottom=0.075,
                          hspace=0.5)
    theme.header(fig, name, subtitle)

    vc_h, h_unit, h_fmt = _voice_series(vc_hours)
    _bar_panel(fig.add_subplot(gs[0]), msg_hours, theme.ACCENT,
               "Messages · by hour of day", "hour", _compact, "No messages on this day")
    _bar_panel(fig.add_subplot(gs[1]), vc_h, theme.SECONDARY,
               f"Voice · {h_unit} by hour of day", "hour", h_fmt,
               "No voice activity on this day")
    return _to_png(fig)


def render_stats_card(name: str, subtitle: str,
                      hero: Sequence[tuple[str, str]],
                      details: Sequence[tuple[str, str]]) -> io.BytesIO:
    """/stats card: three hero tiles + a 4x2 grid of label/value pairs.
    All values arrive pre-formatted; layout coordinates are in inches."""
    W, H = 9.6, 5.75
    fig = theme.new_figure(W, H)
    theme.header(fig, name, subtitle)

    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, W)
    ax.set_ylim(0, H)
    ax.axis("off")

    margin, gap = 0.66, 0.28
    tile_h = 1.5
    tile_top = H - 1.32
    tile_w = (W - 2 * margin - 2 * gap) / 3

    for i, (label, value) in enumerate(hero[:3]):
        x = margin + i * (tile_w + gap)
        ax.add_patch(FancyBboxPatch(
            (x, tile_top - tile_h), tile_w, tile_h,
            boxstyle="round,pad=0,rounding_size=0.14",
            facecolor=theme.SURFACE, edgecolor="none", zorder=2))
        ax.text(x + 0.26, tile_top - 0.42, label, color=theme.MUTED,
                fontsize=10, zorder=3)
        ax.text(x + 0.26, tile_top - 1.02, value, color=theme.TEXT,
                fontsize=20, fontweight="semibold", zorder=3)

    grid_top = tile_top - tile_h - 0.78
    row_h = 1.06
    col_w = (W - 2 * margin) / 4
    for i, (label, value) in enumerate(details[:8]):
        row, col = divmod(i, 4)
        x = margin + col * col_w
        y = grid_top - row * row_h
        ax.text(x, y, label, color=theme.MUTED, fontsize=9.5)
        ax.text(x, y - 0.34, value, color=theme.TEXT, fontsize=13,
                fontweight="medium")

    return _to_png(fig)
