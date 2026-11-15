"""Where to spend CPU speed: scale only the publisher's WCETs by k, then only the subscriber's,
then both, and compare the response times and the critical scaling factor of each side.

    python cpu_scale.py experiments/cpu_scale.yaml [--out results/cpu_scale] [--tools-dir .] [--runner ...]
    python cpu_scale.py experiments/cpu_scale.yaml --plot-only

The network tools run once (k = 1, in <out>/Experiment1): their inputs (lengths, periods,
paths) do not depend on any WCET, so R^N, R^min and J' are the same for every k, and only
the publisher and subscriber analyses are repeated.

Critical scaling factor of a side: the largest k for which every WG is still schedulable,
i.e. R^p <= T and R^s <= T for all WGs (found by bisection; the bounds grow monotonically
with the WCETs). The WG and the check that fail first are reported.

Writes into --out (default results/<name>):
    cpu_scale_results.csv   per side, k and WG: R^p, R^N, R^s, R and the statuses
    cpu_scale_critical.csv  per side: critical k, the WG and the check (publisher/subscriber) that limits it
    cpu_scale_lines.png     end-to-end R of every WG against k: publisher scaled | subscriber scaled
    cpu_scale_bars.png      critical scaling factor per side
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import csv
import math
import os
import sys
from fractions import Fraction

import yaml

import generate_system as gen
import plot_style as ps
import run_all
from network_results import apply_network_results
from p1_publisher_size import _f, scaled_wcet, wg_dict, write_csv
from p2_priority import sub_transaction
from plot_style import plt
from subscriber_rta import analyse_all
from system import _parse_system

SIDES = [("publisher", "publisher WCETs × k"), ("subscriber", "subscriber WCETs × k"),
         ("both", "both × k")]


# --------------------------------------------------------------------- model

def build(spec: dict, kp: Fraction, ks: Fraction) -> dict:
    pid = spec.get("publisher_id_type", "UInt16")
    wcet = scaled_wcet({**spec, "wcet_scale": kp})
    sub_wcet = {t: {key: str(_f(x) * ks) for key, x in vals.items()} for t, vals in spec["sub_wcet"].items()}
    order = spec["wgs"]
    wgs, nm = [], {}
    for i, w in enumerate(order):
        d = wg_dict(w, wcet, pid, n=i + 1, rho=len(order) - i)
        nm[d["writer_group_id"]] = d.pop("_nm")
        wgs.append(d)
    by_id = sorted(order, key=lambda w: int(w["writer_group_id"]))
    subs = [sub_transaction(w, nm[int(w["writer_group_id"])], sub_wcet, m) for m, w in enumerate(by_id, start=1)]
    net = dict(spec["network"])
    net.update(full_duplex=True, publisher_id_type=pid, vlan_tag=True, preamble_ifg=True,
               messages=[{"writer_group_id": int(w["writer_group_id"]), "hard_real_time": False} for w in by_id])
    return {"name": spec["name"], "publishers": [{"p": 1, "node": 1, "writer_groups": wgs}],
            "subscribers": [{"k": 1, "node": 2, "transactions": subs}], "network": net}


def scales(side: str, k: Fraction) -> tuple[Fraction, Fraction]:
    return {"publisher": (k, Fraction(1)), "subscriber": (Fraction(1), k), "both": (k, k)}[side]


def analyse(spec: dict, folder: str, side: str, k: Fraction):
    kp, ks = scales(side, k)
    system = _parse_system(build(spec, kp, ks), spec["name"])
    sched = os.path.join(folder, "Schedule.txt")
    res = os.path.join(folder, "final_new_results.txt")
    has_st = any(m.hard_real_time for m in system.network.values())
    system = apply_network_results(system, folder, sched if has_st else None, res)
    return analyse_all(system)


def schedulable(spec, folder, side, k) -> tuple[bool, str]:
    pub, sub, _ = analyse(spec, folder, side, k)
    for rows, part in ((pub, "publisher"), (sub, "subscriber")):
        for r in (x for xs in rows.values() for x in xs):
            if r.status != "ok":
                return False, f"WG {r.writer_group_id} {part} ({r.status})"
    return True, ""


def critical(spec, folder, side, hi=Fraction(64), digits=4) -> dict:
    """Largest k with every WG schedulable (bisection to 10^-digits)."""
    lo = Fraction(0)
    ok, _ = schedulable(spec, folder, side, hi)
    if ok:
        return {"side": side, "critical_k": f">{float(hi):g}", "limited_by": ""}
    while hi - lo > Fraction(1, 10 ** digits):
        mid = (lo + hi) / 2
        if schedulable(spec, folder, side, mid)[0]:
            lo = mid
        else:
            hi = mid
    _, why = schedulable(spec, folder, side, hi)
    return {"side": side, "critical_k": round(float(lo), digits), "limited_by": why}


def k_grid(spec: dict) -> list[Fraction]:
    a, b, n = float(spec["k_min"]), float(spec["k_max"]), int(spec["points"])
    ks = {Fraction(round(a * (b / a) ** (i / (n - 1)), 4)).limit_denominator(10000) for i in range(n)}
    return sorted(ks | {Fraction(1)})


# ------------------------------------------------------------------ running

def run_baseline(spec: dict, out: str, args) -> None:
    folder = os.path.join(out, "Experiment1")
    if args.resume and os.path.isfile(os.path.join(folder, "results.json")):
        print("  network: reusing Experiment1")
        return
    os.makedirs(os.path.join(out, "systems"), exist_ok=True)
    path = os.path.join(out, "systems", "baseline.yaml")
    gen.write_yaml(build(spec, Fraction(1), Fraction(1)), path, ["CPU scale: baseline, k = 1 on both sides"])
    argv = [path, "--system", spec["name"], "--exp-id", "1", "--work-dir", out, "--tools-dir", args.tools_dir,
            "--num-queues", str(args.num_queues), "--folder-arg", args.folder_arg]
    if args.runner is not None:
        argv += ["--runner", args.runner]
    with open(os.path.join(out, "run_baseline.log"), "w", encoding="utf-8") as f, \
            contextlib.redirect_stdout(f), contextlib.redirect_stderr(f):
        run_all.main(argv)
    if not os.path.isfile(os.path.join(folder, "results.json")):
        raise SystemExit(f"the baseline run failed; see {os.path.join(out, 'run_baseline.log')}")
    print("  network: ran the exes once (Experiment1)")


def collect(spec: dict, folder: str) -> list[dict]:
    rows = []
    T = {int(w["writer_group_id"]): w["T"] for w in spec["wgs"]}
    for side, _ in SIDES:
        for k in k_grid(spec):
            _, _, e2e = analyse(spec, folder, side, k)
            for e in sorted(e2e, key=lambda e: e.writer_group_id):
                f = lambda x: None if x is None else float(x)
                rows.append({"side": side, "k": float(k), "wg": e.writer_group_id, "T": T[e.writer_group_id],
                             "R_p": f(e.R_p), "R_N": f(e.R_max), "J": f(e.J), "R_s": f(e.R_s), "R": f(e.R),
                             "pub_status": e.pub_status, "sub_status": e.sub_status})
    return rows


def read_csv(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["k"], r["wg"], r["T"] = float(r["k"]), int(r["wg"]), float(r["T"])
        for key in ("R_p", "R_N", "J", "R_s", "R"):
            r[key] = float(r[key]) if r[key] else None
    return rows


# --------------------------------------------------------------------- plots

STYLES = ["-", (0, (5, 2)), (0, (1, 1.5)), (0, (6, 2, 1, 2)), (0, (3, 1, 1, 1, 1, 1))]


def lines(rows: list[dict], crit: dict, spec: dict, path: str) -> str:
    wgs = [int(w["writer_group_id"]) for w in spec["wgs"]]
    color = {g: (ps.SLOT + [ps.TEXT])[i % 5] for i, g in enumerate(wgs)}
    fig, axes = plt.subplots(1, 2, figsize=(7.4, 3.6), sharey=True)
    for ax, (side, title) in zip(axes, SIDES[:2]):
        for i, g in enumerate(wgs):
            pts = [(r["k"], r["R"]) for r in rows if r["side"] == side and r["wg"] == g and r["R"] is not None]
            ax.plot([x for x, _ in pts], [y for _, y in pts], color=color[g], linestyle=STYLES[i % 5],
                    linewidth=2, label=f"WG {g} (T {spec['wgs'][i]['T']:g})")
        c = crit[side]["critical_k"]
        if isinstance(c, float):
            ax.axvline(c, color=ps.TEXT_2, linewidth=0.9, linestyle=(0, (2, 3)))
            ax.axvspan(c, float(spec["k_max"]) * 1.1, color="#d6402b", alpha=0.06, linewidth=0)
            ax.text(c, 0.98, f"critical k = {c:.2f} \n{crit[side]['limited_by'].split(' (')[0]} ",
                    transform=ax.get_xaxis_transform(), fontsize=7, color=ps.TEXT_2, va="top", ha="right")
        ax.axvline(1, color=ps.GRID, linewidth=1)
        ax.set_xscale("log", base=2)
        ax.set_xlim(float(spec["k_min"]) / 1.1, float(spec["k_max"]) * 1.1)
        ax.set_xticks([0.25, 0.5, 1, 2, 4, 8, 16], ["¼", "½", "1", "2", "4", "8", "16"])
        ax.set_xlabel(f"k ({title})")
        ax.set_title(f"{side.capitalize()} scaled", loc="left", fontsize=9)
    axes[0].set_ylabel("end-to-end R (µs)")
    axes[0].set_ylim(0, None)
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", bbox_to_anchor=(0.5, 0.0), ncols=5)
    fig.suptitle("Where to spend CPU speed: end-to-end R when one side's WCETs are × k",
                 x=0.02, ha="left", fontsize=10)
    fig.tight_layout()
    return ps.save(fig, path)


def bars(crit: dict, path: str) -> str:
    fig, ax = plt.subplots(figsize=(4.6, 2.6))
    names = [s for s, _ in SIDES]
    vals = [crit[s]["critical_k"] if isinstance(crit[s]["critical_k"], float) else float("nan") for s in names]
    ax.barh(range(len(names))[::-1], vals, height=0.55, color=ps.SLOT[:3])
    for y, v, s in zip(range(len(names))[::-1], vals, names):
        ax.text(v, y, f"  {v:.2f}" if v == v else "  > range", va="center", fontsize=8, color=ps.TEXT)
    ax.set_yticks(range(len(names))[::-1], ["publisher only", "subscriber only", "both"])
    ax.set_xlabel("critical scaling factor k (all WGs schedulable up to k)")
    ax.grid(axis="y", visible=False)
    ax.set_xlim(0, max(v for v in vals if v == v) * 1.25 if any(v == v for v in vals) else 1)
    ax.set_title("How much slower can each side get?", loc="left", fontsize=9)
    return ps.save(fig, path)


# --------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Where to spend CPU speed: publisher vs subscriber WCET scaling")
    ap.add_argument("spec", help="experiment spec, e.g. experiments/cpu_scale.yaml")
    ap.add_argument("--out", help="output folder (default results/<name>)")
    ap.add_argument("--tools-dir", default=".", help="folder with the two exes (default: current folder)")
    ap.add_argument("--num-queues", type=int, default=4)
    ap.add_argument("--folder-arg", default="Experiment{id}\\")
    ap.add_argument("--runner", default=None, help="see run_all.py (default: none on Windows, wine elsewhere)")
    ap.add_argument("--resume", action="store_true", help="reuse Experiment1 if it exists")
    ap.add_argument("--plot-only", action="store_true", help="only redraw from the CSV files")
    args = ap.parse_args(argv)
    args.tools_dir = os.path.abspath(args.tools_dir)
    with open(args.spec, encoding="utf-8") as f:
        spec = yaml.safe_load(f)
    out = os.path.abspath(args.out or os.path.join("results", spec["name"]))
    folder = os.path.join(out, "Experiment1")
    res_csv, crit_csv = os.path.join(out, "cpu_scale_results.csv"), os.path.join(out, "cpu_scale_critical.csv")

    if not args.plot_only:
        os.makedirs(out, exist_ok=True)
        run_baseline(spec, out, args)
        rows = collect(spec, folder)
        write_csv(rows, res_csv)
        write_csv([critical(spec, folder, s) for s, _ in SIDES], crit_csv)
    rows = read_csv(res_csv)
    with open(crit_csv, newline="", encoding="utf-8") as f:
        crit = {}
        for r in csv.DictReader(f):
            try:
                r["critical_k"] = float(r["critical_k"])
            except ValueError:
                pass
            crit[r["side"]] = r
    ps.setup()
    figs = [lines(rows, crit, spec, os.path.join(out, "cpu_scale_lines.png")),
            bars(crit, os.path.join(out, "cpu_scale_bars.png"))]

    print(f"\nCPU scale {spec['name']}: critical scaling factor (every WG with R^p <= T and R^s <= T)")
    for s, _ in SIDES:
        c = crit[s]
        print(f"  {s:>10}: k = {c['critical_k']}" + (f"   limited by {c['limited_by']}" if c["limited_by"] else ""))
    print(f"\n  end-to-end R (us) at k = 1 and k = 2:")
    at = {(r["side"], r["k"], r["wg"]): r for r in rows}
    print(f"  {'WG':>4} {'k=1':>9} {'pub x2':>9} {'sub x2':>9} {'both x2':>9}")
    for w in spec["wgs"]:
        g = int(w["writer_group_id"])
        cells = [at.get(("publisher", 1.0, g)), at.get(("publisher", 2.0, g)),
                 at.get(("subscriber", 2.0, g)), at.get(("both", 2.0, g))]
        print(f"  {g:>4} " + " ".join(f"{(c['R'] if c and c['R'] is not None else float('nan')):9.2f}" for c in cells))
    for p in [res_csv, crit_csv] + figs:
        print("wrote", p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
