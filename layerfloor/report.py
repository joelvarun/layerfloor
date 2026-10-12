"""Chart rendering for the measured results."""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SOFT = "#52514e"
GRID = "#e4e3df"
BASELINE = "#9ec5f4"
ACCENT = "#2a78d6"
ABLATION = "#cde2fb"

LABELS = {
    "monolith": "monolith" + chr(10) + "(COPY . .)",
    "manifest-first": "manifest-first" + chr(10) + "(guide default)",
    "churn-sort": "churn-sort" + chr(10) + "(frequency)",
    "layerfloor-seq": "seq-optimal" + chr(10) + "(no interleave)",
    "layerfloor": "layerfloor" + chr(10) + "(exact)",
}


def _panel(ax, order, values, colors, title, unit, fmt):
    xs = range(len(order))
    bars = ax.bar(xs, [values[k] for k in order], width=0.62, color=colors,
                  zorder=3, linewidth=0)
    top = max(values[k] for k in order)
    for rect, key in zip(bars, order):
        ax.annotate(fmt.format(values[key]),
                    (rect.get_x() + rect.get_width() / 2, rect.get_height()),
                    textcoords="offset points", xytext=(0, 5), ha="center",
                    fontsize=10.5, color=INK, fontweight="medium")
    ax.set_xticks(list(xs))
    ax.set_xticklabels([LABELS.get(k, k) for k in order], fontsize=9,
                       color=INK_SOFT)
    ax.set_ylabel(unit, fontsize=10, color=INK_SOFT)
    ax.set_title(title, fontsize=12, color=INK, pad=12, loc="left")
    ax.set_ylim(0, top * 1.18)
    ax.yaxis.grid(True, color=GRID, linewidth=1, zorder=0)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(axis="y", labelsize=9.5, colors=INK_SOFT, length=0)
    ax.tick_params(axis="x", length=0)


def before_after_chart(results: dict, meta: dict, out_path) -> None:
    """Two panels over the same five orderings: time, then bytes."""
    order = ["monolith", "churn-sort", "manifest-first", "layerfloor-seq",
             "layerfloor"]
    order = [k for k in order if k in results]
    colors = [ABLATION if k == "layerfloor-seq" else
              (ACCENT if k == "layerfloor" else BASELINE) for k in order]

    fig, axes = plt.subplots(1, 2, figsize=(13.2, 5.4), facecolor=SURFACE)
    for ax in axes:
        ax.set_facecolor(SURFACE)

    secs = {k: results[k]["seconds"] for k in order}
    mbs = {k: results[k]["bytes"] / 1e6 for k in order}
    _panel(axes[0], order, secs, colors, "Build commands re-run per commit",
           "seconds of RUN steps executed", "{:.2f}")
    _panel(axes[1], order, mbs, colors, "Registry upload per commit",
           "megabytes pushed", "{:.2f}")

    gm = secs["monolith"] / secs["layerfloor"]
    gf = secs["manifest-first"] / secs["layerfloor"]
    bm = mbs["monolith"] / mbs["layerfloor"]
    fig.suptitle("Where the RUN steps sit decides what a rebuild costs",
                 fontsize=15, color=INK, x=0.011, ha="left", y=0.975)
    fig.text(0.011, 0.918,
             "{repo}, {n} held-out commits, builds actually executed. "
             "Ordering fitted on the {fit} earlier commits only.".format(
                 repo=meta["repo"], n=meta["eval_commits"],
                 fit=meta["fit_commits"]),
             fontsize=10.5, color=INK_SOFT, ha="left")
    fig.text(0.011, 0.862,
             "layerfloor re-runs {:.2f}x less build work than COPY . . and "
             "{:.2f}x less than the manifest-first ordering every container "
             "guide recommends, and uploads {:.2f}x fewer bytes.".format(
                 gm, gf, bm),
             fontsize=10.5, color=INK, ha="left")
    fig.text(0.011, 0.055,
             "Lower is better. Same ten build steps in every column; only the "
             "order differs.", fontsize=9.5, color=INK_SOFT, ha="left")
    fig.text(0.011, 0.014,
             "Left panel, first three columns are equal by construction: "
             "with every COPY before every RUN, no ordering of the COPY "
             "groups can save a single re-execution.",
             fontsize=9.5, color=INK_SOFT, ha="left")
    fig.subplots_adjust(left=0.058, right=0.988, top=0.775, bottom=0.185,
                        wspace=0.20)
    fig.savefig(out_path, dpi=170, facecolor=SURFACE)
    plt.close(fig)
