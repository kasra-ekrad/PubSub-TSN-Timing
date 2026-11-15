"""CPU-speed study over many generated systems (automated).

    python cpu_study.py experiments/cpu_study.yaml [--seeds 1 3] [--tools-dir .] [--runner ...]
    python cpu_study.py experiments/cpu_study.yaml --resume        (skip finished systems)
    python cpu_study.py experiments/cpu_study.yaml --plot-only

For every combination of the grid values (experiments/cpu_study.yaml) and every seed:
  1. generate a system from the base scenario (generate_system.py)
  2. run it through run_all.py: the exes run once
  3. per side, the critical scaling factor: the largest k such that, with that side's
     WCETs multiplied by k, every WG is still schedulable (R^p <= T, R^s <= T, and every
     ST NM handed over before its first gate). Sides: publisher, subscriber, both.
     The network results are reused: they do not depend on WCETs.
  4. per side, dR/dk at k = 1: the mean increase of the end-to-end R per unit of k
     (from k = 1 to 1 + dk), absolute (us) and relative (% of R)

Writes into results/<name>/:
    systems/, Experiment<ID>/, logs/     every generated system and its run_all.py files
    cpu_study_runs.csv                   one row per system: grid values, seed, sizes,
                                         k_pub, k_sub, k_both, what limits each, dR/dk
    cpu_study_heatmap.png                median k_sub / k_pub per grid cell (first two parameters)
    cpu_study_scatter.png                k_pub against k_sub, one point per system
    cpu_study_boxplots.png               k_pub and k_sub per value of each grid parameter
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import csv
import itertools
import math
import os
import statistics
import sys
from dataclasses import replace
from fractions import Fraction

import yaml

import generate_system as gen
import plot_style as ps
import run_all
from network_results import apply_network_results
from plot_style import plt
from run_sweep import seed_list, set_path
from subscriber_rta import analyse_all
from system import _parse_system

SIDES = ("publisher", "subscriber", "both")


# ------------------------------------------------------------------ the spec

def load_spec(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        sp = yaml.safe_load(f)
    for key in ("name", "scenario", "grid", "seeds"):
        if key not in sp:
            raise SystemExit(f"{path}: missing '{key}'")
    sp["scenario"] = os.path.join(os.path.dirname(os.path.abspath(path)), sp["scenario"])
    return sp


def apply(sc: dict, path: str, value) -> None:
    """Set a dotted path; a mapping value sets several keys of that section."""
    if isinstance(value, dict):
        for k, v in value.items():
            set_path(sc, f"{path}.{k}", v)
    else:
        set_path(sc, path, value)


def label(value) -> str:
    if isinstance(value, dict):
        if set(value) == {"publishers", "subscribers"}:
            return f"{value['publishers']}:{value['subscribers']}"
        return ",".join(f"{k}={v}" for k, v in value.items())
    if isinstance(value, list):
        return str(value[0]) if len(value) == 2 and value[0] == value[1] else "-".join(map(str, value))
    return f"{value:g}" if isinstance(value, float) else str(value)


def plan(sp: dict, seeds: list[int]) -> list[dict]:
    params = list(sp["grid"])
    runs, exp = [], 1
    for combo in itertools.product(*(list(enumerate(sp["grid"][p])) for p in params)):
        for seed in seeds:
            runs.append({"cell": {p: v for p, (_, v) in zip(params, combo)},
                         "order": {p: i for p, (i, _) in zip(params, combo)},
                         "labels": {p: label(v) for p, (_, v) in zip(params, combo)},
                         "seed": seed, "exp_id": exp})
            exp += 1
    return runs


# ------------------------------------------------------------- WCET scaling

def scale_side(d: dict, kp: Fraction, ks: Fraction) -> dict:
    """The system dict with every publisher WCET x kp and every subscriber WCET x ks (exact)."""
    d = copy.deepcopy(d)
    s = lambda x, k: str(Fraction(str(x)) * k)
    for p in d["publishers"]:
        for w in p["writer_groups"]:
            w["psi"] = [s(x, kp) for x in w["psi"]]
            w["phi"] = [s(x, kp) for x in w["phi"]]
            w["C3"], w["C4"] = s(w["C3"], kp), s(w["C4"], kp)
    for sub in d["subscribers"]:
        for t in sub["transactions"]:
            t["C1"], t["C2"] = s(t["C1"], ks), s(t["C2"], ks)
            t["phi"] = [s(x, ks) for x in t["phi"]]
            t["psi"] = [s(x, ks) for x in t["psi"]]
    return d


class Evaluator:
    """Analyses scaled copies of one system, reusing its network results."""

    def __init__(self, d: dict, folder: str):
        self.d = d
        base = _parse_system(copy.deepcopy(d), "cpu_study")
        has_st = any(m.hard_real_time for m in base.network.values())
        has_avb = any(not m.hard_real_time for m in base.network.values())
        self.network = apply_network_results(
            base, folder, os.path.join(folder, "Schedule.txt") if has_st else None,
            os.path.join(folder, "final_new_results.txt") if has_avb else None).network

    def analyse(self, side: str, k: Fraction):
        kp, ks = {"publisher": (k, Fraction(1)), "subscriber": (Fraction(1), k), "both": (k, k)}[side]
        system = _parse_system(scale_side(self.d, kp, ks), "cpu_study")
        return analyse_all(replace(system, network=self.network))

    def schedulable(self, side: str, k: Fraction) -> tuple[bool, str]:
        pub, sub, e2e = self.analyse(side, k)
        for r in (x for xs in pub.values() for x in xs):
            if r.status != "ok":
                return False, f"publisher WG {r.writer_group_id}"
        for e in e2e:
            if e.net_status == "st_not_ready":
                return False, f"ST hand-over WG {e.writer_group_id}"
        for r in (x for xs in sub.values() for x in xs):
            if r.status != "ok":
                return False, f"subscriber WG {r.writer_group_id}"
        return True, ""

    def critical(self, side: str, k_max: Fraction, digits: int = 3) -> tuple[float | None, str]:
        """Largest k with every WG schedulable (None if none up to k_max is limited)."""
        if self.schedulable(side, k_max)[0]:
            return None, f"none up to {float(k_max):g}"
        lo, hi = Fraction(0), k_max
        while hi - lo > Fraction(1, 10 ** digits):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if self.schedulable(side, mid)[0] else (lo, mid)
        return round(float(lo), digits), self.schedulable(side, hi)[1]

    def slope(self, side: str, dk: Fraction) -> tuple[float | None, float | None]:
        """Mean over WGs of dR/dk at k = 1 (us per unit k) and relative to R (% per unit k)."""
        r1 = {e.writer_group_id: e.R for e in self.analyse(side, Fraction(1))[2]}
        r2 = {e.writer_group_id: e.R for e in self.analyse(side, 1 + dk)[2]}
        d = [(r2[g] - r1[g]) / dk for g in r1 if r1[g] is not None and r2[g] is not None]
        rel = [(r2[g] - r1[g]) / dk / r1[g] for g in r1 if r1[g] and r2[g] is not None]
        if not d:
            return None, None
        return round(float(sum(d) / len(d)), 4), round(100 * float(sum(rel) / len(rel)), 3)


# ------------------------------------------------------------------ running

def run_one(sp: dict, base: dict, r: dict, out: str, args) -> dict:
    row = {**{f"grid_{p}": r["labels"][p] for p in r["labels"]},
           **{f"order_{p}": r["order"][p] for p in r["order"]}, "seed": r["seed"], "exp_id": r["exp_id"]}
    sc = copy.deepcopy(base)
    for path, v in (sp.get("set") or {}).items():
        apply(sc, path, v)
    for path, v in r["cell"].items():
        apply(sc, path, v)
    name = "_".join(r["labels"][p].replace(":", "x") for p in r["labels"]) + f"_s{r['seed']}"
    sys_path = os.path.join(out, "systems", f"{name}.yaml")
    folder = os.path.join(out, f"Experiment{r['exp_id']}")
    try:
        d, system, attempts = gen.generate(sc, r["seed"])
    except Exception as e:                                # no valid system for this cell
        return {**row, "status": "failed", "error": f"generator: {e}"}
    gen.write_yaml(d, sys_path, [f"cpu_study cell {r['labels']}, seed {r['seed']} (draw {attempts})"])
    done = os.path.isfile(os.path.join(folder, "results.json"))
    if not (args.resume and done):
        argv = [sys_path, "--system", sc["name"], "--exp-id", str(r["exp_id"]), "--work-dir", out,
                "--tools-dir", args.tools_dir, "--num-queues", str(args.num_queues),
                "--folder-arg", args.folder_arg]
        if args.runner is not None:
            argv += ["--runner", args.runner]
        if args.with_be:
            argv.append("--with-be")
        with open(os.path.join(out, "logs", f"run_{r['exp_id']}.log"), "w", encoding="utf-8") as f, \
                contextlib.redirect_stdout(f), contextlib.redirect_stderr(f):
            try:
                run_all.main(argv)
            except BaseException as e:                    # SystemExit included
                return {**row, "status": "failed", "error": f"run_all: {e}"}
        if not os.path.isfile(os.path.join(folder, "results.json")):
            return {**row, "status": "failed", "error": "run_all wrote no results (see the log)"}
    pub_u, sub_u = gen.utilizations(system)
    wgs = [w for p in system.publishers for w in p.wgs]
    received = [len(s.transactions) for s in system.subscribers]
    row.update(status="ok", error="", publishers=len(system.publishers), subscribers=len(system.subscribers),
               wgs=len(wgs), produced_max=max(len(p.wgs) for p in system.publishers),
               received_max=max(received), received_mean=round(sum(received) / len(received), 3),
               st=sum(w.st for w in wgs), max_pub_cpu=round(float(max(pub_u.values())), 4),
               max_sub_cpu=round(float(max(sub_u.values())), 4))
    try:
        ev = Evaluator(d, folder)
        k_max = Fraction(str(sp.get("k_max", 64)))
        dk = Fraction(str(sp.get("dk", 0.1)))
        for side, short in zip(SIDES, ("pub", "sub", "both")):
            k, why = ev.critical(side, k_max)
            a, rel = ev.slope(side, dk)
            row.update({f"k_{short}": k, f"limit_{short}": why, f"dRdk_{short}_us": a, f"dRdk_{short}_pct": rel})
    except Exception as e:                                 # e.g. J' > T in the network results
        row.update(status="failed", error=f"analysis: {type(e).__name__}: {e}")
    return row


def write_rows(rows: list[dict], path: str) -> None:
    fields = []
    for r in rows:
        fields += [k for k in r if k not in fields]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def read_rows(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for k in list(r):
            if k.startswith(("k_", "dRdk_", "order_")) and r[k] not in ("", None):
                r[k] = float(r[k])
            elif k.startswith(("k_", "dRdk_")):
                r[k] = None
    return rows


# --------------------------------------------------------------------- plots

NAMES = {"wgs_per_publisher": "WGs per publisher (produced)",
         "topology": "publishers : subscribers",
         "variables_per_wg": "variables per WG", "topology.switches": "switches in the line"}


def _ok(rows):
    return [r for r in rows if r["status"] == "ok" and r.get("k_pub") and r.get("k_sub")]


def heatmap(rows, sp, path) -> str | None:
    params = list(sp["grid"])
    if len(params) < 2:
        return None
    px, py = params[:2]
    xs = [label(v) for v in sp["grid"][px]]
    ys = [label(v) for v in sp["grid"][py]]
    from matplotlib.colors import LinearSegmentedColormap, TwoSlopeNorm
    cmap = LinearSegmentedColormap.from_list("div", [ps.SLOT[0], "#ecebe7", ps.SLOT[1]])
    grid = [[None] * len(xs) for _ in ys]
    ns = [[0] * len(xs) for _ in ys]
    for r in _ok(rows):
        i, j = ys.index(r[f"grid_{py}"]), xs.index(r[f"grid_{px}"])
        grid[i][j] = (grid[i][j] or []) + [math.log2(r["k_sub"] / r["k_pub"])]
        ns[i][j] += 1
    med = [[statistics.median(c) if c else float("nan") for c in row] for row in grid]
    lim = max([abs(v) for row in med for v in row if v == v] + [0.5])
    fig, ax = plt.subplots(figsize=(1.0 * len(xs) + 2.6, 0.62 * len(ys) + 1.6))
    im = ax.imshow(med, cmap=cmap, norm=TwoSlopeNorm(0, -lim, lim), aspect="auto")
    for i in range(len(ys)):
        for j in range(len(xs)):
            v = med[i][j]
            ax.text(j, i, "–" if v != v else f"{2 ** v:.2f}\nn={ns[i][j]}", ha="center", va="center",
                    fontsize=7, color=ps.TEXT)
    ax.set_xticks(range(len(xs)), xs)
    ax.set_yticks(range(len(ys)), ys)
    ax.set_xlabel(NAMES.get(px, px))
    ax.set_ylabel(NAMES.get(py, py))
    ax.grid(False)
    cb = fig.colorbar(im, ax=ax, fraction=0.05, pad=0.03)
    ticks = [t for t in (-3, -2, -1, 0, 1, 2, 3) if abs(t) <= lim]
    cb.set_ticks(ticks, labels=[f"{2 ** t:g}" for t in ticks])
    cb.set_label("median k_sub / k_pub\n< 1 subscriber limits · > 1 publisher limits", fontsize=7)
    ax.set_title("Which side limits the CPU slowdown (median over seeds)", loc="left", fontsize=9)
    return ps.save(fig, path)


def scatter(rows, sp, path) -> str:
    params = list(sp["grid"])
    py = params[1] if len(params) > 1 else params[0]
    groups = [label(v) for v in sp["grid"][py]]
    colors = ps.SLOT + [ps.TEXT, ps.TEXT_2]
    fig, ax = plt.subplots(figsize=(5.6, 4.4))
    ok = _ok(rows)
    for g, c in zip(groups, colors):
        pts = [(r["k_pub"], r["k_sub"]) for r in ok if r[f"grid_{py}"] == g]
        if pts:
            ax.scatter([x for x, _ in pts], [y for _, y in pts], s=20, color=c, edgecolor=ps.SURFACE,
                       linewidth=0.6, label=f"{g}", zorder=3)
    vals = [v for r in ok for v in (r["k_pub"], r["k_sub"])]
    if vals:
        lo, hi = min(vals) / 1.3, max(vals) * 1.3
        ax.plot([lo, hi], [lo, hi], color=ps.TEXT_2, linewidth=1, linestyle=(0, (4, 3)))
        ax.set_xlim(lo, hi)
        ax.set_ylim(lo, hi)
        ax.text(0.04, 0.96, "publisher limits", transform=ax.transAxes, fontsize=7, color=ps.TEXT_2, va="top")
        ax.text(0.96, 0.04, "subscriber limits", transform=ax.transAxes, fontsize=7, color=ps.TEXT_2,
                ha="right")
    ax.set_xscale("log", base=2)
    ax.set_yscale("log", base=2)
    ax.set_xlabel("k_pub (critical factor, publisher WCETs × k)")
    ax.set_ylabel("k_sub (critical factor, subscriber WCETs × k)")
    ax.legend(title=NAMES.get(py, py), loc="center left", bbox_to_anchor=(1.02, 0.5), fontsize=7,
              title_fontsize=7)
    ax.set_title("Critical scaling factor per system", loc="left", fontsize=9)
    return ps.save(fig, path)


def boxplots(rows, sp, path) -> str:
    params = list(sp["grid"])
    ok = _ok(rows)
    fig, axes = plt.subplots(1, len(params), figsize=(3.6 * len(params) + 0.4, 3.2), squeeze=False)
    for ax, p in zip(axes[0], params):
        vals = [label(v) for v in sp["grid"][p]]
        for side, c, off in (("k_pub", ps.SLOT[0], -0.18), ("k_sub", ps.SLOT[1], 0.18)):
            data, pos = [], []
            for i, v in enumerate(vals):
                xs = [r[side] for r in ok if r[f"grid_{p}"] == v]
                if xs:
                    data.append(xs)
                    pos.append(i + off)
            if data:
                ax.boxplot(data, positions=pos, widths=0.3, whis=(0, 100), patch_artist=True,
                           boxprops=dict(facecolor=c, edgecolor=c), medianprops=dict(color=ps.SURFACE, linewidth=1.5),
                           whiskerprops=dict(color=c), capprops=dict(color=c), flierprops=dict(marker=""))
        ax.plot([], [], color=ps.SLOT[0], linewidth=6, label="k_pub")
        ax.plot([], [], color=ps.SLOT[1], linewidth=6, label="k_sub")
        ax.set_xticks(range(len(vals)), vals)
        ax.set_xlabel(NAMES.get(p, p))
        ax.set_yscale("log", base=2)
        ax.grid(axis="x", visible=False)
    axes[0][0].set_ylabel("critical scaling factor k")
    axes[0][0].legend(loc="upper right")
    fig.suptitle("Critical scaling factor per grid value (box: quartiles, whiskers: min–max)",
                 x=0.02, ha="left", fontsize=9)
    fig.tight_layout()
    return ps.save(fig, path)


# --------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="CPU-speed study over many generated systems")
    ap.add_argument("spec", help="study spec, e.g. experiments/cpu_study.yaml")
    ap.add_argument("--out", help="output folder (default results/<name>)")
    ap.add_argument("--seeds", type=int, nargs=2, metavar=("FIRST", "LAST"), help="override the seeds")
    ap.add_argument("--tools-dir", default=".", help="folder with the two exes (default: current folder)")
    ap.add_argument("--num-queues", type=int, default=4)
    ap.add_argument("--folder-arg", default="Experiment{id}\\")
    ap.add_argument("--runner", default=None, help="see run_all.py (default: none on Windows, wine elsewhere)")
    ap.add_argument("--with-be", action="store_true", help="give best-effort flows to the AVB analysis")
    ap.add_argument("--resume", action="store_true", help="reuse finished run_all.py runs")
    ap.add_argument("--plot-only", action="store_true", help="only redraw from cpu_study_runs.csv")
    args = ap.parse_args(argv)
    args.tools_dir = os.path.abspath(args.tools_dir)
    sp = load_spec(args.spec)
    out = os.path.abspath(args.out or os.path.join("results", sp["name"]))
    csv_path = os.path.join(out, "cpu_study_runs.csv")

    if not args.plot_only:
        base = gen.load_scenario(sp["scenario"])
        seeds = list(range(args.seeds[0], args.seeds[1] + 1)) if args.seeds else seed_list(sp["seeds"])
        runs = plan(sp, seeds)
        for sub in ("systems", "logs"):
            os.makedirs(os.path.join(out, sub), exist_ok=True)
        print(f"cpu study {sp['name']}: {len(runs) // len(seeds)} grid cells x {len(seeds)} seeds "
              f"= {len(runs)} systems -> {out}")
        rows = []
        for i, r in enumerate(runs, start=1):
            row = run_one(sp, base, r, out, args)
            rows.append(row)
            cell = ", ".join(f"{p}={v}" for p, v in r["labels"].items())
            info = (f"k_pub {row.get('k_pub')}, k_sub {row.get('k_sub')}" if row["status"] == "ok"
                    else row["error"][:120])
            print(f"  [{i}/{len(runs)}] {cell}, seed {r['seed']}: {row['status']} - {info}")
            write_rows(rows, csv_path)                     # keep partial results
    rows = read_rows(csv_path)
    ps.setup()
    figs = [heatmap(rows, sp, os.path.join(out, "cpu_study_heatmap.png")),
            scatter(rows, sp, os.path.join(out, "cpu_study_scatter.png")),
            boxplots(rows, sp, os.path.join(out, "cpu_study_boxplots.png"))]
    ok = _ok(rows)
    print(f"\n{len(ok)} of {len(rows)} systems analysed; the subscriber limits in "
          f"{sum(r['k_sub'] < r['k_pub'] for r in ok)}, the publisher in {sum(r['k_pub'] < r['k_sub'] for r in ok)}")
    for p in [csv_path] + [f for f in figs if f]:
        print("wrote", p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
