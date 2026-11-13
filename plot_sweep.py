"""Plots and a summary table for a sweep (scenarios 2 and 5).

    python plot_sweep.py sweep_runs/<name>

Reads results_wg.csv and results_runs.csv from the sweep folder and writes there:
    e2e_boxplot.png    end-to-end delay R per value, AVB and ST side by side
                       (box = quartiles, line = median, whiskers = min..max)
    breakdown.png      mean R split into publisher / network / jitter / subscriber,
                       per value, one panel for AVB and one for ST
    schedulable.png    share of the runs in which every WG is schedulable
    summary.csv        the numbers behind the plots (n, mean, median, p95, max, parts)
"""
from __future__ import annotations

import argparse
import csv
import os
import statistics
import sys

import plot_style as ps
from plot_style import plt

NAMES = {
    "wgs_per_publisher": "WGs per publisher",
    "topology.publishers": "Publishers sending to the subscriber",
    "variables_per_wg": "Variables per WG",
    "topology.switches": "Switches in the line",
    "traffic.st_share": "Share of ST WGs",
    "best_effort.flows": "Best-effort flows",
    "wcet_scale": "WCET scale factor",
}


def read(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def group(rows: list[dict]) -> tuple[list[str], dict]:
    """Values in sweep order, and rows per (value, class) with class AVB or ST."""
    order = {}
    for r in rows:
        order.setdefault(r["value"], int(r["value_order"]))
    values = sorted(order, key=order.get)
    by = {}
    for r in rows:
        if r.get("R") in ("", None):
            continue
        cls = "ST" if r["class"] == "ST" else "AVB"
        by.setdefault((r["value"], cls), []).append(r)
    return values, by


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def summary(values, by, runs, out: str) -> str:
    path = os.path.join(out, "summary.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["value", "class", "n_wgs", "R_mean", "R_median", "R_p95", "R_max", "R_over_T_max"]
                   + [f"{k}_mean" for k, _ in ps.PARTS] + ["runs", "runs_schedulable", "runs_failed"])
        for v in values:
            rv = [r for r in runs if r["value"] == v]
            sched = sum(r.get("schedulable") == "1" for r in rv)
            failed = sum(r["status"] in ("failed", "missing") for r in rv)
            for cls in ("AVB", "ST"):
                rows = by.get((v, cls), [])
                if not rows:
                    continue
                R = [float(r["R"]) for r in rows]
                w.writerow([v, cls, len(rows), round(statistics.mean(R), 3), round(statistics.median(R), 3),
                            round(pct(R, 0.95), 3), round(max(R), 3),
                            round(max(float(r["R_over_T"]) for r in rows), 4)]
                           + [round(statistics.mean(float(r[k]) for r in rows), 3) for k, _ in ps.PARTS]
                           + [len(rv), sched, failed])
    return path


def boxplot(values, by, xlabel, out: str) -> str:
    """One panel per class with its own axis: ST and AVB delays differ by orders of magnitude."""
    present = [c for c in ("AVB", "ST") if any((v, c) in by for v in values)]
    fig, axes = plt.subplots(1, len(present), figsize=(3.4 * len(present) + 0.6, 3.2), squeeze=False)
    for ax, cls in zip(axes[0], present):
        data, pos = [], []
        for i, v in enumerate(values):
            if (v, cls) in by:
                data.append([float(r["R"]) for r in by[(v, cls)]])
                pos.append(i)
        color = ps.CLASS_COLOR[cls]
        ax.boxplot(data, positions=pos, widths=0.5, whis=(0, 100), patch_artist=True,
                   boxprops=dict(facecolor=color, edgecolor=color, linewidth=1),
                   medianprops=dict(color=ps.SURFACE, linewidth=1.6),
                   whiskerprops=dict(color=color, linewidth=1), capprops=dict(color=color, linewidth=1),
                   flierprops=dict(marker="", linewidth=0))
        ax.set_xticks(range(len(values)), values)
        ax.set_xlim(-0.6, len(values) - 0.4)
        ax.set_xlabel(xlabel)
        ax.set_title(f"{cls}: end-to-end delay R", loc="left")
        ax.grid(axis="x", visible=False)
        flat = [x for d in data for x in d]
        if flat and min(flat) > 0 and max(flat) / min(flat) > 30:
            ax.set_yscale("log")
    axes[0][0].set_ylabel("µs")
    fig.text(0.01, -0.10, "Box: quartiles, line: median, whiskers: min-max over all WGs of all runs.",
             fontsize=7, color=ps.TEXT_2)
    return ps.save(fig, os.path.join(out, "e2e_boxplot.png"))


def breakdown(values, by, xlabel, out: str) -> str:
    present = [c for c in ("AVB", "ST") if any((v, c) in by for v in values)]
    fig, axes = plt.subplots(1, len(present), figsize=(3.4 * len(present) + 0.6, 3.4), squeeze=False)
    for ax, cls in zip(axes[0], present):
        bottoms = [0.0] * len(values)
        for (key, name), color in zip(ps.PARTS, ps.SLOT):
            heights = [statistics.mean(float(r[key]) for r in by[(v, cls)]) if (v, cls) in by else 0
                       for v in values]
            ax.bar(range(len(values)), heights, bottom=bottoms, width=0.62, color=color,
                   edgecolor=ps.SURFACE, linewidth=1, label=name)
            bottoms = [b + h for b, h in zip(bottoms, heights)]
        for i, total in enumerate(bottoms):
            if total:
                ax.text(i, total, f"{total:.0f}", ha="center", va="bottom", fontsize=7, color=ps.TEXT_2)
        ax.set_xticks(range(len(values)), values)
        ax.set_xlabel(xlabel)
        ax.set_title(f"{cls}: mean end-to-end delay by part", loc="left")
        ax.grid(axis="x", visible=False)
    axes[0][0].set_ylabel("µs")
    handles, labels = axes[0][0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncols=4, bbox_to_anchor=(0.5, 0.0))
    return ps.save(fig, os.path.join(out, "breakdown.png"))


def schedulable(values, runs, xlabel, out: str) -> str:
    fig, ax = plt.subplots(figsize=(6.4, 2.8))
    shares, ns = [], []
    for v in values:
        rv = [r for r in runs if r["value"] == v and r["status"] not in ("failed", "missing")]
        ns.append(len(rv))
        shares.append(100 * sum(r.get("schedulable") == "1" for r in rv) / len(rv) if rv else 0)
    ax.bar(range(len(values)), shares, width=0.62, color=ps.SLOT[0], label="schedulable runs")
    for i, (s, n) in enumerate(zip(shares, ns)):
        ax.text(i, s, f"{s:.0f}% (n={n})", ha="center", va="bottom", fontsize=7, color=ps.TEXT_2)
    ax.set_xticks(range(len(values)), values)
    ax.set_ylim(0, 112)
    ax.set_xlabel(xlabel)
    ax.set_ylabel("% of runs")
    ax.set_title("Runs in which every WG is schedulable", loc="left")
    ax.grid(axis="x", visible=False)
    return ps.save(fig, os.path.join(out, "schedulable.png"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Plot a sweep")
    ap.add_argument("folder", help="sweep folder, e.g. sweep_runs/hops")
    args = ap.parse_args(argv)
    ps.setup()
    wg = read(os.path.join(args.folder, "results_wg.csv"))
    runs = read(os.path.join(args.folder, "results_runs.csv"))
    if not wg:
        raise SystemExit("no results in results_wg.csv (did the runs fail? see results_runs.csv)")
    xlabel = NAMES.get(wg[0]["parameter"], wg[0]["parameter"])
    values, by = group(wg)
    for path in (boxplot(values, by, xlabel, args.folder), breakdown(values, by, xlabel, args.folder),
                 schedulable(values, runs, xlabel, args.folder), summary(values, by, runs, args.folder)):
        print("wrote", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
