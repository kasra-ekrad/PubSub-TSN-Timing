"""Shared figure style for the paper plots (light surface, validated categorical order)."""
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

TEXT = "#0b0b0b"
TEXT_2 = "#52514e"
GRID = "#e4e3df"
SURFACE = "#ffffff"
# Categorical slots in fixed order (validated: adjacent CVD dE >= 9.1, normal-vision >= 19.6)
SLOT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]

# End-to-end delay = publisher + network + jitter + subscriber (R^p + (R^max - J') + J' + (R^s - J'))
PARTS = [("part_publisher", "Publisher R$^p$"),
         ("part_network", "Network (to earliest arrival)"),
         ("part_jitter", "Network jitter J'"),
         ("part_subscriber", "Subscriber (after arrival)")]
CLASS_COLOR = {"AVB": SLOT[0], "ST": SLOT[1]}


def setup():
    plt.rcParams.update({
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
        "font.size": 9, "axes.titlesize": 10, "axes.labelsize": 9, "legend.fontsize": 8,
        "text.color": TEXT, "axes.labelcolor": TEXT_2, "xtick.color": TEXT_2, "ytick.color": TEXT_2,
        "axes.edgecolor": TEXT_2, "axes.linewidth": 0.6,
        "axes.spines.top": False, "axes.spines.right": False,
        "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6,
        "legend.frameon": False, "savefig.dpi": 200, "savefig.bbox": "tight",
    })


def save(fig, path):
    fig.savefig(path)
    plt.close(fig)
    return path
