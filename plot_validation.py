"""Plots and a summary table for validate.py (safety and tightness of the analysis).

    python plot_validation.py Experiment6                 one experiment (validation.csv)
    python plot_validation.py sweep_runs/wgs_per_publisher   a sweep (validation_all.csv)

Writes into the same folder, per level L (A, B):
    validation_scatter_L.png   largest observed value against the bound, one point per WG
                               (and run), four panels: publisher, network, subscriber,
                               end-to-end; above the diagonal = violation
    validation_ratio_L.png     share of WGs with observed / bound <= x, one curve per mode;
                               1 = bound reached
    validation_e2e_L.png       (one experiment only) per WG: random-mode end-to-end delay
                               (median to 99th percentile, line to the maximum), the
                               adversarial maxima and the bound
    validation_summary.csv     per level, mode and part: WGs, violations, median / 90th
                               percentile / largest ratio, share of WGs at the bound
"""
from __future__ import annotations

import argparse
import csv
import os
import statistics
import sys

import plot_style as ps
from plot_style import plt

MODES = [("random-arbitrary", "Random, any phase"), ("random-part14", "Random, Part 14 phase"),
         ("adv-arbitrary", "Adversarial, any phase"), ("adv-part14", "Adversarial, Part 14 phase")]
COLOR = dict(zip([m for m, _ in MODES], ps.SLOT))
LAYER = {"random-arbitrary": 3, "random-part14": 4, "adv-part14": 5, "adv-arbitrary": 6}   # key series on top
PARTS = [("pub", "Publisher: f - r vs R$^p$"),
         ("net", "Network: a - f vs R$^{max}$ (ST: a - r vs A$_{max}$)"),
         ("sub", "Subscriber: fs - nominal arrival vs R$^s$"),
         ("e2e", "End-to-end: fs - r vs R")]
TIGHT = 0.999


def read(folder: str) -> tuple[list[dict], bool]:
    for name, sweep in (("validation_all.csv", True), ("validation.csv", False)):
        path = os.path.join(folder, name)
        if os.path.isfile(path):
            with open(path, newline="", encoding="utf-8") as f:
                rows = [r for r in csv.DictReader(f) if r.get("wg")]
            for r in rows:
                for k, v in r.items():
                    if k not in ("level", "mode", "class", "worst_e2e_scenario", "sweep", "parameter",
                                 "value") and v not in ("", None):
                        try:
                            r[k] = float(v)
                        except ValueError:
                            pass
                    elif v == "":
                        r[k] = None
            return rows, sweep
    raise SystemExit(f"no validation.csv or validation_all.csv in {folder} (run validate.py first)")


def scatter(rows, level, folder) -> str:
    fig, axes = plt.subplots(2, 2, figsize=(7.2, 6.6))
    for ax, (part, title) in zip(axes.flat, PARTS):
        top = 0
        for mode, name in MODES:
            pts = [(r[f"bound_{part}"], r[f"max_{part}"], r["class"]) for r in rows
                   if r["mode"] == mode and r[f"bound_{part}"] is not None and r[f"max_{part}"] is not None]
            for cls, marker in (("AVB", "o"), ("ST", "s")):
                sel = [(x, y) for x, y, c in pts if (c == "ST") == (cls == "ST")]
                if sel:
                    ax.scatter([x for x, _ in sel], [y for _, y in sel], s=22, marker=marker,
                               color=COLOR[mode], edgecolor=ps.SURFACE, linewidth=0.6, zorder=LAYER[mode],
                               label=name if cls == "AVB" or not any(c != "ST" for *_, c in pts) else None)
            top = max([top] + [max(x, y) for x, y, _ in pts])
        vals = [v for r in rows if r["mode"] in COLOR
                for v in (r[f"bound_{part}"], r[f"max_{part}"]) if v is not None]
        log = bool(vals) and min(vals) > 0 and max(vals) / min(vals) > 20     # ST and AVB differ a lot
        top = top * (1.3 if log else 1.06) or 1
        lo = min(vals) / 1.3 if log else 0
        ax.plot([lo, top], [lo, top], color=ps.TEXT_2, linewidth=1, linestyle=(0, (4, 3)), zorder=2)
        ax.fill_between([lo, top], [lo, top], [top, top], color="#d6402b", alpha=0.06, linewidth=0, zorder=1)
        if log:
            ax.set_xscale("log")
            ax.set_yscale("log")
        ax.text(0.04, 0.93, "violation", fontsize=7, color="#a33a2a", va="top", transform=ax.transAxes)
        ax.set_xlim(lo, top)
        ax.set_ylim(lo if log else min(0, ax.get_ylim()[0]), top)
        ax.set_title(title, loc="left", fontsize=8.5)
        ax.set_xlabel("bound (µs" + (", log)" if log else ")"))
        ax.set_ylabel("largest observed (µs" + (", log)" if log else ")"))
    handles, labels = [], []
    for ax in axes.flat:
        for h, l in zip(*ax.get_legend_handles_labels()):
            if l not in labels:
                handles.append(h)
                labels.append(l)
    fig.legend(handles, labels, loc="upper center", ncols=2, bbox_to_anchor=(0.5, 0.005))
    fig.suptitle(f"Level {level}: observed vs analytical bound per WG (circle AVB, square ST)",
                 x=0.02, ha="left", fontsize=10)
    fig.tight_layout()
    return ps.save(fig, os.path.join(folder, f"validation_scatter_{level}.png"))


def ratio_cdf(rows, level, folder) -> str:
    fig, axes = plt.subplots(2, 2, figsize=(7.2, 5.8), sharey=True)
    for ax, (part, title) in zip(axes.flat, PARTS):
        hi = 1.05
        for mode, name in MODES:
            xs = sorted(r[f"ratio_{part}"] for r in rows if r["mode"] == mode and r[f"ratio_{part}"] is not None)
            if not xs:
                continue
            ys = [(i + 1) / len(xs) for i in range(len(xs))]
            ax.step([xs[0]] + xs, [0] + ys, where="post", color=COLOR[mode], linewidth=2, label=name,
                    zorder=LAYER[mode])
            hi = max(hi, xs[-1] * 1.03)
        ax.axvline(1, color=ps.TEXT_2, linewidth=1, linestyle=(0, (4, 3)))
        ax.text(1, 1.02, " bound", fontsize=7, color=ps.TEXT_2, va="bottom", ha="left",
                transform=ax.get_xaxis_transform())
        ax.set_xlim(min(0, ax.get_xlim()[0]), hi)
        ax.set_ylim(0, 1.0)
        ax.set_title(title, loc="left", fontsize=8.5)
        ax.set_xlabel("largest observed / bound")
    for ax in axes[:, 0]:
        ax.set_ylabel("share of WGs")
    handles, labels = axes.flat[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncols=2, bbox_to_anchor=(0.5, 0.005))
    fig.suptitle(f"Level {level}: how close the simulation gets to the bound", x=0.02, ha="left", fontsize=10)
    fig.tight_layout()
    return ps.save(fig, os.path.join(folder, f"validation_ratio_{level}.png"))


def e2e_per_wg(rows, level, folder) -> str | None:
    """One experiment: where the observed end-to-end delays sit relative to the bound."""
    wgs = sorted({int(r["wg"]) for r in rows if r["bound_e2e"] is not None})
    if not wgs:
        return None
    by = {(r["mode"], int(r["wg"])): r for r in rows}
    fig, ax = plt.subplots(figsize=(6.8, 0.5 * len(wgs) + 1.4))
    y = {g: i for i, g in enumerate(reversed(wgs))}
    off = {"random-arbitrary": 0.16, "random-part14": -0.16}
    top = 0
    for mode, name in MODES[:2]:
        for g in wgs:
            r = by.get((mode, g))
            if not r or r.get("e2e_p50") is None:
                continue
            yy = y[g] + off[mode]
            ax.plot([r["e2e_p50"], r["max_e2e"]], [yy, yy], color=COLOR[mode], linewidth=1, zorder=2)
            ax.plot([r["e2e_p50"], r["e2e_p99"]], [yy, yy], color=COLOR[mode], linewidth=5,
                    solid_capstyle="butt", zorder=3, label=name if g == wgs[0] else None)
            top = max(top, r["max_e2e"])
    for mode, name in MODES[2:]:
        xs = [(by[(mode, g)]["max_e2e"], y[g]) for g in wgs if (mode, g) in by and by[(mode, g)]["max_e2e"]]
        if xs:
            ax.scatter([x for x, _ in xs], [v for _, v in xs], marker="D", s=30, color=COLOR[mode],
                       edgecolor=ps.SURFACE, linewidth=0.6, zorder=4, label=f"{name} (max)")
            top = max([top] + [x for x, _ in xs])
    for g in wgs:
        b = by[(MODES[0][0], g)]["bound_e2e"] if (MODES[0][0], g) in by else None
        b = b if b is not None else next(r["bound_e2e"] for r in rows if int(r["wg"]) == g)
        ax.plot([b, b], [y[g] - 0.34, y[g] + 0.34], color=ps.TEXT, linewidth=1.6, zorder=5,
                label="bound R" if g == wgs[0] else None)
        top = max(top, b)
    cls = {int(r["wg"]): r["class"] for r in rows}
    ax.set_yticks([y[g] for g in wgs], [f"WG {g} · {cls[g]}" for g in wgs])
    ax.set_xlim(0, top * 1.08)
    ax.set_xlabel("end-to-end delay (µs)")
    ax.grid(axis="y", visible=False)
    ax.set_title(f"Level {level}: end-to-end delay per WG (bar: median to p99, line: to the maximum)",
                 loc="left", fontsize=9)
    ax.legend(loc="upper center", bbox_to_anchor=(0.45, -0.12 - 0.3 / len(wgs)), ncols=3)
    return ps.save(fig, os.path.join(folder, f"validation_e2e_{level}.png"))


def summary(rows, folder) -> str:
    path = os.path.join(folder, "validation_summary.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["level", "mode", "part", "wgs", "violations", "wgs_with_violation",
                    "ratio_median", "ratio_p90", "ratio_max", "share_at_bound", "share_above_bound"])
        for level in sorted({r["level"] for r in rows}):
            for mode, _ in MODES:
                sel = [r for r in rows if r["level"] == level and r["mode"] == mode]
                if not sel:
                    continue
                for part, _ in PARTS:
                    xs = sorted(r[f"ratio_{part}"] for r in sel if r[f"ratio_{part}"] is not None)
                    v = [r[f"viol_{part}"] or 0 for r in sel]
                    if not xs:
                        continue
                    w.writerow([level, mode, part, len(xs), int(sum(v)), sum(1 for x in v if x),
                                round(statistics.median(xs), 4), round(xs[int(0.9 * (len(xs) - 1))], 4),
                                round(xs[-1], 4), round(sum(TIGHT <= x <= 1 for x in xs) / len(xs), 4),
                                round(sum(x > 1 for x in xs) / len(xs), 4)])
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Plot validate.py results")
    ap.add_argument("folder", help="experiment folder (validation.csv) or sweep folder (validation_all.csv)")
    args = ap.parse_args(argv)
    ps.setup()
    rows, sweep = read(args.folder)
    if not rows:
        raise SystemExit("the validation file has no rows")
    out = [summary(rows, args.folder)]
    for level in sorted({r["level"] for r in rows}):
        sel = [r for r in rows if r["level"] == level]
        out += [scatter(sel, level, args.folder), ratio_cdf(sel, level, args.folder)]
        if not sweep:
            out.append(e2e_per_wg(sel, level, args.folder))
    for p in out:
        if p:
            print("wrote", p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
