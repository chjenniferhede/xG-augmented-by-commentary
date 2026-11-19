"""Slide figures for the goals-only results.

    make goals          runs the model, then this script
    python scripts/goals_figures.py

Reads data/out/goals.csv and data/out/goals_summary.json (both written by `npm run goals`) and
writes one PNG per figure to data/out/figures/. No number is typed in here: everything shown
comes from those two files, so the slides always match the code.

Sized for a 16:9 slide (1920x1080 at full width) with text large enough to read from the back of
a classroom. Colours: one blue for a single series; blue = raised and orange = lowered where
direction matters, a pair checked for colour-blind separation and contrast on the background.
"""
import csv
import json
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from paths import DATA  # noqa: E402

OUT = DATA / "out"
FIG = OUT / "figures"

BG = "#FBF8F3"
INK = "#17140F"
INK_2 = "#4A4F57"
MUTED = "#8A867E"
GRID = "#E6E0D6"
BLUE = "#1F7AA8"    # raised / single series
ORANGE = "#C2571A"  # lowered / emphasis
NAVY = "#16324A"
GREY = "#C9C3B8"
PALE = "#BCD3DD"   # the "all shots" square: context, not data emphasis

plt.rcParams.update({
    "font.family": "sans-serif",
    "font.sans-serif": ["Figtree", "Helvetica Neue", "Helvetica", "Arial", "DejaVu Sans"],
    "font.size": 18,
    "axes.titlesize": 26, "axes.titleweight": "bold", "axes.titlelocation": "left",
    "axes.titlepad": 18, "axes.titlecolor": INK,
    "axes.labelsize": 18, "axes.labelcolor": INK_2,
    "xtick.labelsize": 16, "ytick.labelsize": 17,
    "xtick.color": INK_2, "ytick.color": INK,
    "axes.edgecolor": GRID, "axes.facecolor": BG, "figure.facecolor": BG,
    "savefig.facecolor": BG,
})
SIZE = (12, 6.75)  # inches; x160 dpi = 1920x1080
DPI = 160


def clean(ax, keep_bottom=True):
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_visible(keep_bottom)
    ax.tick_params(length=0)


def save(fig, name):
    path = FIG / f"{name}.png"
    fig.savefig(path, dpi=DPI)
    plt.close(fig)
    print(f"  {path}")


def heading(fig, title, sub):
    """Title and subtitle pinned to the figure's left edge, so every chart lines up the same."""
    fig.text(0.03, 0.95, title, fontsize=28, fontweight="bold", color=INK, va="top")
    fig.text(0.03, 0.865, sub, fontsize=18, color=INK_2, va="top")


def finish(fig):
    fig.tight_layout(rect=(0.01, 0.01, 0.99, 0.80))


def hbars(ax, labels, values, colours, xmax, fmt="{:,}"):
    """Horizontal bars, top to bottom in the order given, value printed at the end."""
    y = list(range(len(labels)))[::-1]
    ax.barh(y, values, color=colours, height=0.62)
    for yi, v in zip(y, values):
        ax.text(v + xmax * 0.012, yi, fmt.format(v), va="center", fontsize=20, fontweight="bold", color=INK)
    ax.set_yticks(y, labels)
    ax.set_xlim(0, xmax * 1.1)
    ax.set_xticks([])
    clean(ax, keep_bottom=False)


def fig_funnel(s):
    """Nested squares, area proportional to the count."""
    fig, ax = plt.subplots(figsize=SIZE)
    heading(fig, "From every shot to the goals the commentary moved",
            "Area of each square is proportional to the count")
    big = 1.0
    sides = [big * (n / s["shots"]) ** 0.5 for n in (s["shots"], s["goals"], s["moved_with_replay"])]
    x0, y0 = 0.0, 0.0
    ax.add_patch(Rectangle((x0, y0), sides[0], sides[0], color=PALE, lw=0))
    ax.add_patch(Rectangle((x0 + 0.04, y0 + 0.04), sides[1], sides[1], color=BLUE, lw=0))
    ax.add_patch(Rectangle((x0 + 0.04, y0 + 0.04), sides[2], sides[2], color=NAVY, lw=0))
    ax.set_xlim(0, 2.25)
    ax.set_ylim(0, 1)
    ax.set_aspect("equal")
    ax.axis("off")
    rows = [
        (f"{s['shots']:,}", f"shots matched to commentary\nin {s['games']} games", PALE),
        (f"{s['goals']:,}", "of them were goals", BLUE),
        (f"{s['moved_with_replay']:,}", "goals the commentary moved", NAVY),
    ]
    for i, (num, text, colour) in enumerate(rows):
        yy = 0.82 - i * 0.3
        ax.add_patch(Rectangle((1.12, yy - 0.035), 0.07, 0.07, color=colour, lw=0))
        ax.text(1.25, yy, num, fontsize=34, fontweight="bold", color=INK, va="center")
        ax.text(1.62, yy, text, fontsize=19, color=INK_2, va="center")
    finish(fig)
    save(fig, "1_funnel")


def fig_replay(s):
    fig, ax = plt.subplots(figsize=SIZE)
    heading(fig, "The replay is where the detail is",
            f"Goals moved by their commentary, out of all {s['goals']} goals (light bar)")
    labels = ["Pre-shot commentary only", "Pre-shot + replay commentary"]
    values = [s["moved_pre_only"], s["moved_with_replay"]]
    ax.barh([1, 0], [s["goals"]] * 2, color="#EFEAE2", height=0.5)  # track = all goals
    hbars(ax, labels, values, [MUTED, BLUE], s["goals"])
    for b in ax.patches[2:]:
        b.set_height(0.5)
        b.set_y(b.get_y() + 0.06)
    finish(fig)
    save(fig, "2_replay")


def fig_features(s, top=8):
    feats = s["feature_counts"][:top]
    fig, ax = plt.subplots(figsize=SIZE)
    heading(fig, "What the commentary picks up on goals",
            "Number of goals each detail moved, most common first")
    hbars(ax, [f["label"] for f in feats], [f["goals"] for f in feats], BLUE,
          max(f["goals"] for f in feats))
    finish(fig)
    save(fig, "3_features")


# Goals named on the scatter, with where each label sits (axis units, placed in empty space).
# For a player with several goals, the one the commentary raised most is labelled.
CALLOUTS = {"Max Pacioretty": (0.03, 0.45), "Reilly Smith": (0.26, 0.51)}


def fig_scatter(rows, s):
    fig, ax = plt.subplots(figsize=SIZE)
    heading(fig, "Every goal: MoneyPuck vs. after the commentary",
            f"Above the line = raised. Total stays {s['total_xg']:.1f}, so grey dots dip slightly.")
    lim = 0.95
    ax.plot([0, lim], [0, lim], color=MUTED, lw=1.5, ls="--", zorder=1)
    ax.text(0.47, 0.40, "no change", color=MUTED, fontsize=16, ha="left")
    groups = [
        ("Commentary raised it", lambda r: r["shift"] > 0, BLUE),
        ("Commentary lowered it", lambda r: r["shift"] < 0, ORANGE),
        ("No usable commentary", lambda r: r["shift"] == 0, GREY),
    ]
    for label, keep, colour in groups:
        pts = [r for r in rows if keep(r)]
        ax.scatter([r["mp"] for r in pts], [r["ref"] for r in pts], s=80, color=colour,
                   edgecolor=BG, linewidth=1.5, label=f"{label} ({len(pts)})", zorder=3)
    for name, (tx, ty) in CALLOUTS.items():
        theirs = [r for r in rows if r["shooter"] == name]
        if not theirs:
            continue
        r = max(theirs, key=lambda r: r["ref"] - r["mp"])
        ax.annotate(f"{r['shooter']}\n{r['mp']:.0%} to {r['ref']:.0%}", (r["mp"], r["ref"]),
                    xytext=(tx, ty), fontsize=16, color=INK, va="center",
                    arrowprops=dict(arrowstyle="-", color=INK_2, lw=1.2, shrinkB=6))
        ax.scatter([r["mp"]], [r["ref"]], s=150, facecolor="none", edgecolor=INK, linewidth=1.8, zorder=4)
    ax.set_xlim(0, lim)
    ax.set_ylim(0, lim)
    ax.set_xlabel("MoneyPuck chance of scoring")
    ax.set_ylabel("After the commentary")
    ax.xaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0, decimals=0))
    ax.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0, decimals=0))
    ax.grid(color=GRID, lw=1)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.tick_params(length=0)
    ax.legend(loc="lower right", frameon=False, fontsize=16)
    finish(fig)
    save(fig, "4_every_goal")


def fig_long_shots(s):
    cut = f"{s['long_shot_cutoff']:.0%}"
    fig, ax = plt.subplots(figsize=SIZE)
    heading(fig, "Long-shot goals the commentary explains",
            f"Long shot = a goal MoneyPuck gave under a {cut} chance")
    labels = ["Long-shot goals", "Raised by the commentary", f"Now above {cut}"]
    values = [s["long_shots"], s["long_shots_raised"], s["long_shots_now_above_cutoff"]]
    hbars(ax, labels, values, [MUTED, BLUE, NAVY], s["long_shots"])
    finish(fig)
    save(fig, "5_long_shots")


def main():
    summary_path, csv_path = OUT / "goals_summary.json", OUT / "goals.csv"
    if not summary_path.exists() or not csv_path.exists():
        sys.exit("Run `npm --prefix annotate run goals` (or `make goals`) first.")
    s = json.loads(summary_path.read_text())
    with csv_path.open() as f:
        rows = [{"shooter": r["shooter"], "mp": float(r["xg_moneypuck"]),
                 "ref": float(r["xg_refined"]), "shift": float(r["feature_shift"])}
                for r in csv.DictReader(f)]
    FIG.mkdir(parents=True, exist_ok=True)
    print("Figures:")
    fig_funnel(s)
    fig_replay(s)
    fig_features(s)
    fig_scatter(rows, s)
    fig_long_shots(s)


if __name__ == "__main__":
    main()
