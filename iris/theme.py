"""Visual identity: palette, bundled Inter fonts, rcParams, figure factory.

Everything renders through new_figure()/style_axis() so all charts share one
look and nothing gets restyled per-command. Uses matplotlib Figure objects
directly (no pyplot) so rendering is safe from worker threads.
"""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")

from matplotlib import font_manager, rcParams
from matplotlib.figure import Figure

# Warm dark palette.
BG = "#262624"         # figure + axes background
SURFACE = "#30302E"    # card panels
ACCENT = "#D97757"     # series 1: chat
SECONDARY = "#8B9DAF"  # series 2: voice (dusty blue — sage #A3A380 failed
                       # colour-vision-deficiency separation against the accent)
TEXT = "#EDEAE3"       # warm off-white, never pure white
MUTED = "#A8A29E"      # labels
GRID = "#3A3A38"       # gridlines / softened spines

DPI = 180

_FONTS_DIR = Path(__file__).with_name("fonts")


def _register_fonts() -> str:
    registered = False
    for path in sorted(_FONTS_DIR.glob("*.ttf")) + sorted(_FONTS_DIR.glob("*.otf")):
        try:
            font_manager.fontManager.addfont(str(path))
            registered = True
        except Exception:
            pass
    return "Inter" if registered else "DejaVu Sans"


FONT_FAMILY = _register_fonts()

rcParams.update(
    {
        "figure.facecolor": BG,
        "axes.facecolor": BG,
        "savefig.facecolor": BG,
        "figure.dpi": DPI,
        "savefig.dpi": DPI,
        "font.family": FONT_FAMILY,
        "text.color": TEXT,
        "axes.labelcolor": MUTED,
        "axes.edgecolor": GRID,
        "xtick.color": MUTED,
        "ytick.color": MUTED,
        "xtick.labelsize": 10,
        "ytick.labelsize": 10,
        "xtick.major.size": 0,
        "ytick.major.size": 0,
        "xtick.major.pad": 6,
        "ytick.major.pad": 6,
        "axes.titlelocation": "left",
        "axes.titlesize": 12.5,
        "axes.titleweight": "medium",
        "axes.titlecolor": MUTED,
        "axes.titlepad": 14,
        "axes.axisbelow": True,
        "grid.color": GRID,
        "grid.linewidth": 1.0,
        "grid.alpha": 0.9,
    }
)


def new_figure(width: float, height: float) -> Figure:
    fig = Figure(figsize=(width, height), dpi=DPI)
    fig.set_facecolor(BG)
    return fig


def style_axis(ax) -> None:
    """No box, no vertical grid: a thin bottom spine and soft horizontal lines."""
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.spines["bottom"].set_linewidth(1.0)
    ax.tick_params(length=0)
    ax.grid(axis="y", color=GRID, linewidth=1.0, alpha=0.9)
    ax.grid(visible=False, axis="x")


def header(fig: Figure, title: str, subtitle: str, x: float = 0.07) -> None:
    """Left-aligned figure header: name in primary text, context line in muted.

    Positions are computed in inches so the header reads the same on figures
    of any height.
    """
    h = fig.get_figheight()
    fig.text(x, 1 - 0.34 / h, title, fontsize=19, fontweight="semibold", color=TEXT, va="top")
    fig.text(x, 1 - 0.76 / h, subtitle, fontsize=10.5, color=MUTED, va="top")
