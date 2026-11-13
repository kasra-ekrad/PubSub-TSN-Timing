"""Parameter sweep: many generated systems, each run through the whole flow (run_all.py).

A sweep file (see sweeps/*.yaml) names a base scenario, one parameter, its values
and the seeds:

    name: hops
    scenario: scenario_line.yaml        # base scenario (relative to the sweep file)
    set: {topology.placement: split}    # optional fixed changes to the base scenario
    parameter: topology.switches        # dotted path into the scenario
    values: [1, 2, 3, 4, 6]
    seeds: [1, 30]                      # seeds 1..30 (or a list: [3, 7, 11])

For every value and every seed, the scenario is generated with that seed (the same
seeds for every value, so value 2 / seed 5 and value 3 / seed 5 are drawn from the
same random stream) and run through all 8 steps, including the two exes. Results:

    <work-dir>/<name>/systems/v<value>_s<seed>.yaml   the generated systems
    <work-dir>/<name>/Experiment<ID>/                 every run's files, results.json
    <work-dir>/<name>/logs/run_<ID>.log               every run's printed output
    <work-dir>/<name>/results_wg.csv                  one row per WG and run
    <work-dir>/<name>/results_runs.csv                one row per run

Usage (Windows, exes in the current folder):
    python run_sweep.py sweeps/hops.yaml
    python plot_sweep.py sweep_runs/hops
Options: --seeds A B (override), --work-dir (default sweep_runs), --tools-dir,
--num-queues, --folder-arg, --runner, --with-be, --resume (skip runs that already
have results.json), --collect-only (only rebuild the CSV files).
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import csv
import json
import os
import sys
from fractions import Fraction

import yaml

import generate_system as gen
import run_all
from system import load_systems


# ------------------------------------------------------------------ the spec

def load_sweep(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        sp = yaml.safe_load(f)
    for key in ("name", "scenario", "parameter", "values", "seeds"):
        if key not in sp:
            raise SystemExit(f"{path}: missing '{key}'")
    sp["scenario"] = os.path.join(os.path.dirname(os.path.abspath(path)), sp["scenario"])
    return sp


def seed_list(seeds) -> list[int]:
    """[a, b] = a..b; a longer list = exactly those seeds."""
    if isinstance(seeds, list) and len(seeds) == 2 and seeds[0] <= seeds[1]:
        return list(range(seeds[0], seeds[1] + 1))
    return list(seeds)


def set_path(d: dict, path: str, value) -> None:
    keys = path.split(".")
    for k in keys[:-1]:
        if not isinstance(d.get(k), dict):
            raise SystemExit(f"parameter {path}: '{k}' is not a section of the scenario")
        d = d[k]
    d[keys[-1]] = value


def label(value) -> str:
    """Value as text: [3, 3] -> 3, [2, 6] -> 2-6."""
    if isinstance(value, list):
        return str(value[0]) if len(value) == 2 and value[0] == value[1] else "-".join(map(str, value))
    return f"{value:g}" if isinstance(value, float) else str(value)


def plan(sp: dict, seeds: list[int]) -> list[dict]:
    first = int(sp.get("first_exp_id", 1))
    runs = []
    for vi, value in enumerate(sp["values"]):
        for si, seed in enumerate(seeds):
            runs.append({"value": value, "label": label(value), "seed": seed, "order": vi,
                         "exp_id": first + vi * len(seeds) + si})
    return runs


# ------------------------------------------------------------------- running

def run_one(sp: dict, base: dict, r: dict, out: str, args) -> tuple[str, str]:
    """Generate and run one system. Returns (status, error)."""
    sc = copy.deepcopy(base)
    for path, v in (sp.get("set") or {}).items():
        set_path(sc, path, v)
    set_path(sc, sp["parameter"], r["value"])
    sys_path = os.path.join(out, "systems", f"v{r['label']}_s{r['seed']}.yaml")
    try:
        d, _, attempts = gen.generate(sc, r["seed"])
    except (gen.ScenarioError, Exception) as e:          # no valid system for this draw
        return "failed", f"generator: {e}"
    gen.write_yaml(d, sys_path, [f"sweep {sp['name']}: {sp['parameter']} = {r['label']}, "
                                 f"seed {r['seed']} (draw {attempts})"])
    argv = [sys_path, "--system", sc["name"], "--exp-id", str(r["exp_id"]), "--work-dir", out,
            "--tools-dir", args.tools_dir, "--num-queues", str(args.num_queues),
            "--folder-arg", args.folder_arg]
    if args.runner is not None:
        argv += ["--runner", args.runner]
    if args.with_be:
        argv.append("--with-be")
    log = os.path.join(out, "logs", f"run_{r['exp_id']}.log")
    with open(log, "w", encoding="utf-8") as f, contextlib.redirect_stdout(f), contextlib.redirect_stderr(f):
        try:
            code = run_all.main(argv)
        except SystemExit as e:
            return "failed", f"run_all: {e}"
        except Exception as e:
            return "failed", f"run_all: {type(e).__name__}: {e}"
    return ("ok" if code == 0 else "unschedulable"), ""


# ---------------------------------------------------------------- collecting

def _f(x):
    return "" if x is None else round(float(x), 6)


def collect(sp: dict, runs: list[dict], out: str, statuses: dict) -> tuple[str, str]:
    """Write results_wg.csv and results_runs.csv from the runs' results.json files."""
    wg_rows, run_rows = [], []
    for r in runs:
        folder = os.path.join(out, f"Experiment{r['exp_id']}")
        res_path = os.path.join(folder, "results.json")
        sys_path = os.path.join(out, "systems", f"v{r['label']}_s{r['seed']}.yaml")
        status, error = statuses.get(r["exp_id"], ("", ""))
        base = {"sweep": sp["name"], "parameter": sp["parameter"], "value": r["label"],
                "value_order": r["order"], "seed": r["seed"], "exp_id": r["exp_id"]}
        if not os.path.isfile(res_path):
            run_rows.append({**base, "status": status or "missing", "error": error})
            continue
        with open(res_path, encoding="utf-8") as f:
            res = json.load(f)
        system = load_systems(sys_path)[0]
        pub_u, sub_u = gen.utilizations(system)
        T = {w.writer_group_id: w.T for p in system.publishers for w in p.wgs}
        e2e = res["e2e"]
        for e in e2e:
            J = e.get("J") or 0
            row = {**base, "wg": e["writer_group_id"], "class": e["traffic_class"],
                   "T": float(T[e["writer_group_id"]]), "R_p": _f(e["R_p"]), "R_max": _f(e["R_max"]),
                   "J": _f(J), "R_s": _f(e["R_s"]), "R": _f(e["R"]),
                   "pub_status": e["pub_status"], "net_status": e["net_status"], "sub_status": e["sub_status"]}
            if e["R"] is not None:
                row.update(R_over_T=_f(e["R"] / float(T[e["writer_group_id"]])),
                           part_publisher=_f(e["R_p"]), part_network=_f(e["R_max"] - J),
                           part_jitter=_f(J), part_subscriber=_f(e["R_s"] - J))
            wg_rows.append(row)
        ok = all((e["pub_status"], e["sub_status"]) == ("ok", "ok") and e["net_status"] in ("", "ok")
                 for e in e2e)
        Rs = [e["R"] for e in e2e if e["R"] is not None]
        links = res.get("link_utilization", [])
        run_rows.append({**base, "status": status or ("ok" if ok else "unschedulable"), "error": error,
                         "schedulable": int(ok), "wgs": len(e2e),
                         "st": sum(e["traffic_class"] == "ST" for e in e2e),
                         "avb": sum(e["traffic_class"].startswith("AVB") for e in e2e),
                         "max_R": _f(max(Rs)) if Rs else "", "mean_R": _f(sum(Rs) / len(Rs)) if Rs else "",
                         "max_link_util": _f(max((l["utilization"] for l in links), default=0)),
                         "max_pub_cpu": _f(max(pub_u.values())), "max_sub_cpu": _f(max(sub_u.values()))})
    paths = []
    for name, rows in (("results_wg.csv", wg_rows), ("results_runs.csv", run_rows)):
        path = os.path.join(out, name)
        fields = []
        for row in rows:
            fields += [k for k in row if k not in fields]
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
        paths.append(path)
    return paths[0], paths[1]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run a parameter sweep through the whole flow")
    ap.add_argument("sweep", help="sweep file, e.g. sweeps/hops.yaml")
    ap.add_argument("--seeds", type=int, nargs=2, metavar=("FIRST", "LAST"), help="override the seeds")
    ap.add_argument("--work-dir", default="sweep_runs", help="folder for all sweeps (default sweep_runs)")
    ap.add_argument("--tools-dir", default=".", help="folder with the two exes (default: current folder)")
    ap.add_argument("--num-queues", type=int, default=4)
    ap.add_argument("--folder-arg", default="Experiment{id}\\")
    ap.add_argument("--runner", default=None, help="see run_all.py (default: none on Windows, wine elsewhere)")
    ap.add_argument("--with-be", action="store_true")
    ap.add_argument("--resume", action="store_true", help="skip runs that already have results.json")
    ap.add_argument("--collect-only", action="store_true", help="only rebuild the CSV files")
    args = ap.parse_args(argv)
    args.tools_dir = os.path.abspath(args.tools_dir)

    sp = load_sweep(args.sweep)
    base = gen.load_scenario(sp["scenario"])
    seeds = list(range(args.seeds[0], args.seeds[1] + 1)) if args.seeds else seed_list(sp["seeds"])
    runs = plan(sp, seeds)
    out = os.path.abspath(os.path.join(args.work_dir, sp["name"]))
    for sub in ("systems", "logs"):
        os.makedirs(os.path.join(out, sub), exist_ok=True)

    statuses = {}
    if not args.collect_only:
        print(f"sweep {sp['name']}: {sp['parameter']} = {[label(v) for v in sp['values']]}, "
              f"seeds {seeds[0]}..{seeds[-1]} ({len(runs)} runs) -> {out}")
        for i, r in enumerate(runs, start=1):
            done = os.path.isfile(os.path.join(out, f"Experiment{r['exp_id']}", "results.json"))
            if args.resume and done:
                print(f"  [{i}/{len(runs)}] value {r['label']}, seed {r['seed']}: already done")
                continue
            status, error = run_one(sp, base, r, out, args)
            statuses[r["exp_id"]] = (status, error)
            print(f"  [{i}/{len(runs)}] value {r['label']}, seed {r['seed']}, Experiment{r['exp_id']}: "
                  f"{status}{' - ' + error[:120] if error else ''}")
    wg_csv, runs_csv = collect(sp, runs, out, statuses)
    print(f"wrote {wg_csv}\n      {runs_csv}\nplot with:  python plot_sweep.py {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
