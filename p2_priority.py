"""P2: how the priority position of one WG changes publisher, network and subscriber response times.

    python p2_priority.py experiments/p2.yaml [--out results/p2] [--tools-dir .] [--runner ...]
    python p2_priority.py experiments/p2.yaml --plot-only        (redraw from p2_results.csv)

One publisher with five WGs and one subscriber that receives all of them. The WG under
analysis takes every priority position, from the highest to the lowest; all sizes stay
fixed. Each position is one full run_all.py run (both exes), in <out>/Experiment<pos>.
Per position and WG it records R^p, the network's R^max (R^N), R^min, J', R^s and the
end-to-end R.

Writes into --out (default results/<name>):
    systems/p2_pos<k>.yaml   the system of position k
    logs/run_<k>.log         run_all.py output
    p2_results.csv           one row per (position, WG)
    p2_lines.png             R^p, R^N and R^s of every WG against the position of the target
What the analysis predicts:
    publisher   a WG above the target has it only in its blocking (a max); below it, the
                target is in its interference (a sum): moving the target down lowers the
                R^p of each WG it passes, and raises its own
    network     WG priorities are not given to the network tools (the AVB class is assigned
                by the tool's own rule), so R^N does not change
    subscriber  the subscriber inherits the priority order: the same pattern as the publisher,
                with W' instead of W, plus the jitter J' of the higher-priority streams
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import json
import os
import sys
from fractions import Fraction

import yaml

import generate_system as gen
import plot_style as ps
import run_all
import uadp
from p1_publisher_size import _f, scaled_wcet, split, wg_dict, write_csv
from plot_style import plt


# --------------------------------------------------------------------- model

def sub_transaction(w: dict, nm: int, sub_wcet: dict, m: int) -> dict:
    """Receiving transaction of WG w at the subscriber (reads every DSM and variable)."""
    r, f, d, x = (sub_wcet[k] for k in ("reception", "filter", "decode", "dispatch"))
    groups = split(int(w["v"]), int(w.get("dsms", 1)))
    psi = _f(x["base"]) + _f(x["per_byte"]) * uadp.field_size(w["type"])
    return {"m": m, "writer_group_id": int(w["writer_group_id"]),
            "C1": str(_f(r["base"]) + _f(r["per_byte"]) * nm), "C2": str(_f(f["base"])),
            "phi": [str(_f(d["base"]) + _f(d["per_field"]) * k) for k in groups],
            "psi": [str(psi)] * int(w["v"])}


def order_at(spec: dict, pos: int) -> list[dict]:
    """The WGs in priority order with the target at position pos (1 = highest)."""
    others = list(spec["others"])
    return others[:pos - 1] + [spec["target"]] + others[pos - 1:]


def build(spec: dict, pos: int) -> dict:
    pid = spec.get("publisher_id_type", "UInt16")
    wcet = scaled_wcet(spec)
    order = order_at(spec, pos)
    wgs, nm = [], {}
    for i, w in enumerate(order):
        d = wg_dict(w, wcet, pid, n=i + 1, rho=len(order) - i)
        nm[d["writer_group_id"]] = d.pop("_nm")
        wgs.append(d)
    by_id = sorted(order, key=lambda w: int(w["writer_group_id"]))   # same message order in every run
    subs = [sub_transaction(w, nm[int(w["writer_group_id"])], spec["sub_wcet"], m)
            for m, w in enumerate(by_id, start=1)]
    net = dict(spec["network"])
    net.update(full_duplex=True, publisher_id_type=pid, vlan_tag=True, preamble_ifg=True,
               messages=[{"writer_group_id": int(w["writer_group_id"]), "hard_real_time": False}
                         for w in by_id])
    return {"name": spec["name"], "publishers": [{"p": 1, "node": 1, "writer_groups": wgs}],
            "subscribers": [{"k": 1, "node": 2, "transactions": subs}], "network": net}


# ------------------------------------------------------------------ running

def run_position(spec: dict, pos: int, out: str, args) -> tuple[str, str]:
    path = os.path.join(out, "systems", f"p2_pos{pos}.yaml")
    gen.write_yaml(build(spec, pos), path,
                   [f"P2: WG {spec['target']['writer_group_id']} at priority position {pos}"])
    argv = [path, "--system", spec["name"], "--exp-id", str(pos), "--work-dir", out,
            "--tools-dir", args.tools_dir, "--num-queues", str(args.num_queues),
            "--folder-arg", args.folder_arg]
    if args.runner is not None:
        argv += ["--runner", args.runner]
    log = os.path.join(out, "logs", f"run_{pos}.log")
    with open(log, "w", encoding="utf-8") as f, contextlib.redirect_stdout(f), contextlib.redirect_stderr(f):
        try:
            code = run_all.main(argv)
        except SystemExit as e:
            return "failed", str(e)
        except Exception as e:
            return "failed", f"{type(e).__name__}: {e}"
    return ("ok" if code == 0 else "not schedulable"), ""


def collect(spec: dict, out: str, positions: list[int]) -> list[dict]:
    tg = int(spec["target"]["writer_group_id"])
    rows = []
    for pos in positions:
        path = os.path.join(out, f"Experiment{pos}", "results.json")
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as f:
            res = json.load(f)
        prio = {int(w["writer_group_id"]): i + 1 for i, w in enumerate(order_at(spec, pos))}
        pub = {r["writer_group_id"]: r for rows_ in res["publishers"].values() for r in rows_}
        sub = {r["writer_group_id"]: r for rows_ in res["subscribers"].values() for r in rows_}
        for e in sorted(res["e2e"], key=lambda e: e["writer_group_id"]):
            g = e["writer_group_id"]
            n = res["network"][str(g)]
            rows.append({"position": pos, "wg": g, "role": "target" if g == tg else "other",
                         "priority_rank": prio[g], "T": pub[g]["T"], "class": e["traffic_class"],
                         "W": pub[g]["W"], "R_p": e["R_p"], "R_N": e["R_max"], "R_min": n.get("R_min"),
                         "J": e["J"], "W_s": sub[g]["W"], "R_s": e["R_s"], "R": e["R"],
                         "pub_status": e["pub_status"], "net_status": e["net_status"],
                         "sub_status": e["sub_status"]})
    return rows


def read_rows(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for k in ("position", "wg", "priority_rank"):
            r[k] = int(r[k])
        for k in ("T", "W", "R_p", "R_N", "R_min", "J", "W_s", "R_s", "R"):
            r[k] = float(r[k]) if r[k] not in ("", None) else None
    return rows


# --------------------------------------------------------------------- plot

STYLES = ["-", (0, (5, 2)), (0, (1, 1.5)), (0, (6, 2, 1, 2))]
PANELS = [("R_p", "Publisher $R^p$"), ("R_N", "Network $R^N$ ($R^{max}$)"), ("R_s", "Subscriber $R^s$")]


def lines(rows: list[dict], spec: dict, path: str) -> str:
    tg = int(spec["target"]["writer_group_id"])
    positions = sorted({r["position"] for r in rows})
    n_pos = len(spec["others"]) + 1
    wgs = sorted({r["wg"] for r in rows})
    others = [g for g in wgs if g != tg]
    color = {g: ps.SLOT[i % len(ps.SLOT)] for i, g in enumerate(others)}
    color[tg] = ps.TEXT
    style = {g: STYLES[i % len(STYLES)] for i, g in enumerate(others)}
    style[tg] = "-"
    at = {(r["position"], r["wg"]): r for r in rows}
    T = {r["wg"]: r["T"] for r in rows}
    fig, axes = plt.subplots(len(PANELS), 1, figsize=(6.4, 7.4), sharex=True,
                             gridspec_kw={"hspace": 0.3})
    for ax, (key, title) in zip(axes, PANELS):
        ends = []
        for g in others + [tg]:
            xs = [p for p in positions if at.get((p, g)) and at[(p, g)][key] is not None]
            ys = [at[(p, g)][key] for p in xs]
            if not xs:
                continue
            ax.plot(xs, ys, color=color[g], linestyle=style[g], linewidth=2.6 if g == tg else 1.8,
                    zorder=4 if g == tg else 3,
                    label=f"WG {g} (under analysis, T {T[g]:g})" if g == tg else f"WG {g} (T {T[g]:g})")
            ends.append((xs[-1], ys[-1], f"WG {g}"))
        ax.set_ylim(0, None)
        merged = []                                   # equal end values share one label
        for x, y, text in sorted(ends, key=lambda e: e[1]):
            if merged and abs(merged[-1][1] - y) < 1e-6:
                merged[-1] = (x, y, merged[-1][2] + " = " + text.replace("WG ", ""))
            else:
                merged.append((x, y, text))
        span = ax.get_ylim()[1] - ax.get_ylim()[0]
        last = None
        for x, y, text in merged:                     # direct labels, nudged apart
            y2 = y if last is None or y - last > 0.08 * span else last + 0.08 * span
            ax.text(x + 0.08, y2, text, fontsize=7, color=ps.TEXT, va="center")
            last = y2
        ax.set_title(title, loc="left", fontsize=9)
        ax.set_ylabel("µs")
        ax.grid(axis="x", visible=False)
    axes[-1].set_xticks(range(1, n_pos + 1),
                        [f"{k}" + (" (highest)" if k == 1 else " (lowest)" if k == n_pos else "")
                         for k in range(1, n_pos + 1)])
    axes[-1].set_xlim(0.8, n_pos + 0.55)
    axes[-1].set_xlabel(f"priority position of WG {tg}, the WG under analysis")
    fig.suptitle(f"P2: priority position of WG {tg}; one publisher, one subscriber",
                 x=0.02, ha="left", fontsize=10)
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", bbox_to_anchor=(0.5, 0.03), ncols=3)
    return ps.save(fig, path)


# --------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P2: priority position of one WG")
    ap.add_argument("spec", help="experiment spec, e.g. experiments/p2.yaml")
    ap.add_argument("--out", help="output folder (default results/<name>)")
    ap.add_argument("--tools-dir", default=".", help="folder with the two exes (default: current folder)")
    ap.add_argument("--num-queues", type=int, default=4)
    ap.add_argument("--folder-arg", default="Experiment{id}\\")
    ap.add_argument("--runner", default=None, help="see run_all.py (default: none on Windows, wine elsewhere)")
    ap.add_argument("--resume", action="store_true", help="skip positions that already have results.json")
    ap.add_argument("--plot-only", action="store_true", help="only redraw from p2_results.csv")
    args = ap.parse_args(argv)
    args.tools_dir = os.path.abspath(args.tools_dir)
    with open(args.spec, encoding="utf-8") as f:
        spec = yaml.safe_load(f)
    out = os.path.abspath(args.out or os.path.join("results", spec["name"]))
    csv_path = os.path.join(out, "p2_results.csv")
    positions = list(range(1, len(spec["others"]) + 2))
    tg = int(spec["target"]["writer_group_id"])

    if not args.plot_only:
        for sub in ("systems", "logs"):
            os.makedirs(os.path.join(out, sub), exist_ok=True)
        for pos in positions:
            if args.resume and os.path.isfile(os.path.join(out, f"Experiment{pos}", "results.json")):
                print(f"  position {pos}: already done")
                continue
            status, err = run_position(spec, pos, out, args)
            print(f"  position {pos}: {status}{' - ' + err[:150] if err else ''}  (log: logs/run_{pos}.log)")
        rows = collect(spec, out, positions)
        if not rows:
            raise SystemExit("no results (see the logs)")
        write_csv(rows, csv_path)
    rows = read_rows(csv_path)
    ps.setup()
    fig = lines(rows, spec, os.path.join(out, "p2_lines.png"))

    at = {(r["position"], r["wg"]): r for r in rows}
    print(f"\nP2 {spec['name']}: WG {tg} at priority positions {positions[0]}..{positions[-1]}  (us)")
    head = f"  {'WG':>4} {'T':>6}  " + "  ".join(f"{'pos ' + str(p):>26}" for p in positions)
    print(head)
    print(f"  {'':>4} {'':>6}  " + "  ".join(f"{'R^p':>8} {'R^N':>8} {'R^s':>8}" for _ in positions))
    for g in sorted({r["wg"] for r in rows}):
        cells = []
        for p in positions:
            r = at.get((p, g))
            cells.append("   (no result)            " if r is None else
                         " ".join(f"{(r[k] if r[k] is not None else float('nan')):8.2f}" for k in ("R_p", "R_N", "R_s")))
        print(f"  {g:>4} {at[(positions[0], g)]['T']:>6g}  " + "  ".join(cells)
              + ("   <- under analysis" if g == tg else ""))
    print(f"wrote {csv_path}\nwrote {fig}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
