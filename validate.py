"""Check the analysis against simulation: safety and tightness (evaluation test 1).

    python validate.py generated.yaml --exp-id 6            one finished run_all.py run
    python validate.py --sweep sweep_runs/wgs_per_publisher  every run of a sweep
    python validate.py example_system.yaml --system jitter_demo   YAML R_max only (Level A)
    python plot_validation.py Experiment6                    (or the sweep folder)

The bounds are recomputed from the same files run_all.py used (the YAML and, in
Experiment<ID>, Schedule.txt and final_new_results.txt). Then the system is simulated
(sim_core.py, sim_tsn.py) and every instance is checked against them:
    publisher   f - r   <= R^p
    network     AVB: R^min <= a - f <= R^max;   ST: A_min <= a - r <= A_max
    subscriber  fs - nu <= R^s   (nu = r + R^p + R^min for AVB, r + A_min for ST: the
                nominal arrival from which R^s, which contains J', is measured)
    end-to-end  fs - r  <= R
with r = release, f = hand-over to the NIC, a = arrival, fs = subscriber chain done.

Modes (--modes, default all four):
    random-arbitrary   random phase per WG, random execution times in [bcet x WCET, WCET],
                       random network delays; --runs runs of --hyperperiods hyperperiods.
                       Gives the distributions; rarely reaches the worst case.
    random-part14      the same with OPC 10000-14 6.3.1.1.1 phasing: every WG starts its
                       cycles at multiples of its PublishingInterval on its node's clock; one
                       random clock offset per node (0 for nodes with ST, which are
                       synchronised to the schedule).
    adv-arbitrary      per WG, release patterns built to reach its bounds, WCETs:
                       "pub" critical instant at the publisher with blocking; "sub" frames of
                       the interfering WGs arriving together with it at the subscriber, with
                       jitter bunching; "jit" the same plus the interfering streams' frames
                       bunching by their publisher's output jitter (sim_core.adversarial).
    adv-part14         the same, with Part 14 phasing.
Levels (--levels, default A and B with an experiment folder, A without):
    A   network = the analysis' delay box [R^min, R^max] (ST: the schedule's times):
        checks the publisher and subscriber analyses and their composition
    B   network = TSN simulation with gates, credit-based shapers and queues (sim_tsn.py):
        also checks the network tools' bounds. --idle-slope must match the AVB tool.
        Adversarial in Level B uses the Level A release patterns (+ --search random variants
        of the other publishers' phases); the network decides the arrivals.

Writes, in the experiment folder (sweep: also validation_all.csv in the sweep folder):
    validation.csv             per level, mode and WG: bounds, largest observed values,
                               observed / bound, violations, arrival spread vs J',
                               random-mode quantiles
    validation_violations.csv  observed values above their bound (first 5 per WG and
                               metric) with the instance's r, f, a, fs, to reproduce them
    validation.json            settings and totals
A simulation can only show violations, never prove a bound; an adversarial value equal to
the bound shows that the bound is reached (tight) for that WG.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import sys
import time

import sim_core as sc
from network_results import apply_network_results
from subscriber_rta import analyse_all
from system import load_systems

MODES = ("random-arbitrary", "random-part14", "adv-arbitrary", "adv-part14")
QUANTILES = {"p50": 0.5, "p90": 0.9, "p99": 0.99, "p999": 0.999}


# --------------------------------------------------------------------- loading

def load(yaml_path: str, system_name: str | None, folder: str | None):
    systems = {s.name: s for s in load_systems(yaml_path)}
    if system_name is None:
        if len(systems) != 1:
            raise SystemExit(f"{yaml_path} has {len(systems)} systems; choose one with --system")
        system_name = next(iter(systems))
    if system_name not in systems:
        raise SystemExit(f"no system named {system_name!r} in {yaml_path}")
    system = systems[system_name]
    if folder:
        has_st = any(m.hard_real_time for m in system.network.values())
        has_avb = any(not m.hard_real_time for m in system.network.values())
        sched = os.path.join(folder, "Schedule.txt")
        res = os.path.join(folder, "final_new_results.txt")
        system = apply_network_results(system, folder, sched if has_st else None,
                                       res if has_avb else None)
    pub, sub, e2e = analyse_all(system)
    return sc.build_model(system, pub, sub, e2e, folder)


# --------------------------------------------------------------------- running

def _wcet(model):
    return (lambda g, j: model.tasks[g].W), (lambda g, j: model.streams[g].Ws)


def run_mode(model, level: str, mode: str, args, tsn=None) -> sc.Observer:
    obs = sc.Observer(model, keep_samples=mode.startswith("random"))
    kind, phasing = mode.split("-")
    if kind == "random":
        t_end = args.hyperperiods * model.H
        for run in range(args.runs):
            rng = random.Random(f"{args.seed}:{level}:{mode}:{run}")
            rel = sc.random_releases(model, rng, phasing, t_end)
            pub_exe = sc.random_exec(rng, {g: t.W for g, t in model.tasks.items()}, args.bcet)
            sub_exe = sc.random_exec(rng, {g: s.Ws for g, s in model.streams.items()}, args.bcet)
            if level == "A":
                net = sc.random_delays(model, rng)
            else:
                be_rel = {b.name: sc.releases(rng.randrange(b.T), b.T, 0, t_end) for b in model.be}
                net = lambda fr, be_rel=be_rel: tsn.run(fr, be_rel)
            obs.add(sc.simulate(model, rel, net, pub_exe, sub_exe), f"{mode}:run{run}")
        return obs

    pub_exe, sub_exe = _wcet(model)
    for scn in sc.scenarios(model, phasing):
        variants = [scn]
        if level == "B" and args.search:
            rng = random.Random(f"{args.seed}:{scn.label}")
            variants += [_shuffled(model, scn, rng, phasing, i) for i in range(args.search)]
        for v in variants:
            rel = v.releases(model)
            if level == "A":
                net = sc.planned_delays(model, v.plan)
            else:
                be_rel = {b.name: sc.releases(0, b.T, -2 * model.Tmax, v.t_end) for b in model.be}
                net = lambda fr, be_rel=be_rel: tsn.run(fr, be_rel)
            obs.add(sc.simulate(model, rel, net, pub_exe, sub_exe), v.label)
    return obs


def _shuffled(model, scn: sc.Scenario, rng: random.Random, phasing: str, i: int) -> sc.Scenario:
    """Level B search: the scenario with random phases on the other publishers."""
    P = model.tasks[scn.target].p
    anchors, t_from = dict(scn.anchors), dict(scn.t_from)
    offset = {p: (0 if p in model.st_pubs else rng.randrange(model.Tmax)) for p in model.pub_of}
    for g, t in model.tasks.items():
        if t.p == P:
            continue
        anchors[g] = 0 if t.st else (rng.randrange(t.T) if phasing == "arbitrary" else offset[t.p])
        t_from[g] = anchors[g] - 2 * model.Tmax
    return sc.Scenario(f"{scn.label}:search{i}", anchors, t_from, scn.t_end, {}, scn.target)


# ------------------------------------------------------------------- reporting

def _q(xs: list[int], p: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return sc.us(xs[min(len(xs) - 1, int(p * (len(xs) - 1) + 0.5))])


def rows_of(model, obs: sc.Observer, level: str, mode: str) -> list[dict]:
    rows = []
    for g in sorted(model.tasks):
        t, s, o = model.tasks[g], model.streams.get(g), obs.obs[g]
        row = {"level": level, "mode": mode, "wg": g, "class": s.cls if s else "-",
               "publisher": t.p, "subscriber": s.k if s else "", "T": sc.us(t.T), "instances": o.n}
        bounds = {"pub": t.Rp, "net": s.net_hi if s else None, "sub": s.Rs if s else None,
                  "e2e": s.R if s else None}
        for m in sc.METRICS:
            mx = o.max.get(m)
            b = bounds[m]
            row[f"bound_{m}"] = sc.us(b)
            row[f"max_{m}"] = sc.us(mx[0]) if mx else None
            row[f"ratio_{m}"] = round(mx[0] / b, 6) if mx and b else None
            row[f"viol_{m}"] = o.viol[m]
        row.update({
            "bound_net_min": sc.us(s.net_lo) if s else None, "min_net": sc.us(o.net_min),
            "viol_net_min": o.viol["net_min"], "J": sc.us(s.J) if s else None,
            "arrival_spread": sc.us(o.arr_max - o.arr_min) if o.arr_min is not None else None,
            "max_after_arrival": sc.us(o.after_max), "st_late": o.late,
            "worst_e2e_scenario": o.max["e2e"][1] if "e2e" in o.max else "",
            "worst_e2e_release": sc.us(o.max["e2e"][2]) if "e2e" in o.max else None,
        })
        if mode.startswith("random"):
            for m in sc.METRICS:
                for name, p in QUANTILES.items():
                    row[f"{m}_{name}"] = _q(o.samples[m], p)
        rows.append(row)
    return rows


def write_csv(path: str, rows: list[dict]) -> None:
    fields = []
    for r in rows:
        fields += [k for k in r if k not in fields]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def print_table(rows: list[dict], level: str, modes: list[str]) -> None:
    by = {(r["mode"], r["wg"]): r for r in rows if r["level"] == level}
    wgs = sorted({r["wg"] for r in rows if r["level"] == level})
    short = {"random-arbitrary": "rand-arb", "random-part14": "rand-p14",
             "adv-arbitrary": "adv-arb", "adv-part14": "adv-p14"}
    print(f"\n  Level {level}: largest observed / bound   (pub, net, sub, e2e; 1.000 = bound reached)")
    head = f"    {'WG':>4} {'class':>6} {'R [us]':>10}"
    for m in modes:
        head += f"  {short[m]:>27}"
    print(head)
    for g in wgs:
        r0 = by[(modes[0], g)]
        line = f"    {g:>4} {r0['class']:>6} {_fmt(r0['bound_e2e']):>10}"
        for m in modes:
            r = by[(m, g)]
            cells = [("  -  " if r[f"ratio_{k}"] is None else f"{r[f'ratio_{k}']:.3f}") for k in sc.METRICS]
            line += "  " + " ".join(f"{c:>6}" for c in cells)
        print(line)
    viol: dict[int, dict[str, set]] = {}
    for r in rows:
        if r["level"] != level:
            continue
        for k in list(sc.METRICS) + ["net_min"]:
            if r[f"viol_{k}"]:
                viol.setdefault(r["wg"], {}).setdefault(k, set()).add(short[r["mode"]])
    print("    violations: " + ("; ".join(
        f"WG {g} " + ", ".join(f"{k} ({'/'.join(sorted(ms))})" for k, ms in d.items())
        for g, d in sorted(viol.items())) if viol else "none"))
    late = sorted({r["wg"] for r in rows if r["level"] == level and r["st_late"]})
    if late:
        print("    ST frames that missed their window: " + ", ".join(f"WG {g}" for g in late))
    wide = {}
    for r in rows:
        if r["level"] == level and r["J"] is not None and r["arrival_spread"] is not None \
                and r["arrival_spread"] > r["J"] + 1e-6:
            wide[r["wg"]] = max(wide.get(r["wg"], 0), r["arrival_spread"] - r["J"])
    if wide:
        print("    arrivals spread wider than J' (publisher output jitter): "
              + ", ".join(f"WG {g} +{d:.2f} us" for g, d in sorted(wide.items())))


def _fmt(x) -> str:
    return "-" if x is None else f"{x:.3f}"


def validate_experiment(yaml_path: str, system_name: str | None, folder: str | None, args,
                        quiet: bool = False) -> tuple[list[dict], list[dict], dict]:
    t0 = time.time()
    model = load(yaml_path, system_name, folder)
    levels = args.levels or (["A", "B"] if folder else ["A"])
    if "B" in levels and not folder:
        raise SystemExit("Level B needs an experiment folder (--exp-id)")
    tsn = None
    if "B" in levels:
        from sim_tsn import TSN
        tsn = TSN(model, folder, args.idle_slope, args.idle_slope_factor, args.be)
    rows, traces = [], []
    for level in levels:
        for mode in args.modes:
            obs = run_mode(model, level, mode, args, tsn)
            rows += rows_of(model, obs, level, mode)
            traces += [{"level": level, "mode": mode, **t} for t in obs.traces]
    summary = {
        "system": model.system.name, "yaml": os.path.abspath(yaml_path),
        "experiment": os.path.abspath(folder) if folder else None,
        "levels": levels, "modes": list(args.modes), "runs": args.runs,
        "hyperperiods": args.hyperperiods, "hyperperiod_us": sc.us(model.H), "bcet": args.bcet,
        "seed": args.seed, "search": args.search,
        "idle_slope": args.idle_slope if tsn else None,
        "best_effort_in_level_B": tsn.with_be if tsn else None,
        "warnings": tsn.warnings if tsn else [],
        "violations": sum(r[f"viol_{k}"] for r in rows for k in list(sc.METRICS) + ["net_min"]),
        "st_late": sum(r["st_late"] for r in rows),
        "seconds": round(time.time() - t0, 1),
    }
    if not quiet:
        print(f"system {model.system.name}: {len(model.tasks)} WGs, hyperperiod {sc.us(model.H):g} us"
              + (f"; Level B idleSlope {args.idle_slope}, best effort "
                 f"{'included' if tsn.with_be else 'not included'}" if tsn else ""))
        for w in summary["warnings"]:
            print(f"  warning: {w}")
        for level in levels:
            print_table(rows, level, list(args.modes))
    out = folder or os.path.dirname(os.path.abspath(yaml_path))
    if folder or args.out:
        out = args.out or folder
        write_csv(os.path.join(out, "validation.csv"), rows)
        write_csv(os.path.join(out, "validation_violations.csv"), traces or [{"wg": ""}])
        with open(os.path.join(out, "validation.json"), "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)
        if not quiet:
            print(f"\n  wrote {os.path.join(out, 'validation.csv')} ({summary['seconds']} s)")
    return rows, traces, summary


# ----------------------------------------------------------------------- sweep

def validate_sweep(folder: str, args) -> int:
    runs_csv = os.path.join(folder, "results_runs.csv")
    if not os.path.isfile(runs_csv):
        raise SystemExit(f"{runs_csv} not found (run run_sweep.py first)")
    with open(runs_csv, newline="", encoding="utf-8") as f:
        runs = list(csv.DictReader(f))
    all_rows, all_traces, failed = [], [], []
    for i, r in enumerate(runs, start=1):
        exp = os.path.join(folder, f"Experiment{r['exp_id']}")
        sys_path = os.path.join(folder, "systems", f"v{r['value']}_s{r['seed']}.yaml")
        tag = f"[{i}/{len(runs)}] value {r['value']}, seed {r['seed']}, Experiment{r['exp_id']}"
        if not os.path.isfile(os.path.join(exp, "results.json")) or not os.path.isfile(sys_path):
            print(f"  {tag}: no finished run ({r.get('status', '')}), skipped")
            continue
        base = {"sweep": r.get("sweep", ""), "parameter": r.get("parameter", ""), "value": r["value"],
                "value_order": r.get("value_order", ""), "seed": r["seed"], "exp_id": r["exp_id"]}
        done = os.path.join(exp, "validation.csv")
        try:
            if args.resume and os.path.isfile(done):
                with open(done, newline="", encoding="utf-8") as f:
                    rows = list(csv.DictReader(f))
                traces = []
                print(f"  {tag}: already validated")
            else:
                rows, traces, s = validate_experiment(sys_path, None, exp, args, quiet=True)
                per = []
                for level in s["levels"]:
                    lr = [x for x in rows if x["level"] == level]
                    wgs = {x["wg"] for x in lr for k in list(sc.METRICS) + ["net_min"] if x[f"viol_{k}"]}
                    per.append(f"Level {level}: {len(wgs)} WGs above a bound"
                               + (f", {sum(x['st_late'] for x in lr)} late ST frames"
                                  if any(x["st_late"] for x in lr) else ""))
                print(f"  {tag}: " + "; ".join(per) + f" ({s['seconds']} s)")
        except Exception as e:                      # one bad run must not stop the sweep
            failed.append((tag, str(e)))
            print(f"  {tag}: failed - {type(e).__name__}: {e}")
            continue
        all_rows += [{**base, **x} for x in rows]
        all_traces += [{**base, **x} for x in traces]
    write_csv(os.path.join(folder, "validation_all.csv"), all_rows or [{"wg": ""}])
    write_csv(os.path.join(folder, "validation_violations_all.csv"), all_traces or [{"wg": ""}])
    print(f"wrote {os.path.join(folder, 'validation_all.csv')}"
          + (f"; {len(failed)} runs failed" if failed else ""))
    print(f"plot with:  python plot_validation.py {folder}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Check the analysis against simulation")
    ap.add_argument("file", nargs="?", help="YAML with the system (as given to run_all.py)")
    ap.add_argument("--system", help="system name (default: the only one in the file)")
    ap.add_argument("--exp-id", type=int, help="experiment number: use Experiment<ID> of run_all.py")
    ap.add_argument("--work-dir", default=".", help="folder that holds Experiment<ID> (default: .)")
    ap.add_argument("--sweep", help="validate every run of a sweep folder (sweep_runs/<name>)")
    ap.add_argument("--levels", nargs="+", choices=["A", "B"], help="default: A B (A without --exp-id)")
    ap.add_argument("--modes", nargs="+", choices=MODES, default=list(MODES))
    ap.add_argument("--runs", type=int, default=10, help="random runs per mode (default 10)")
    ap.add_argument("--hyperperiods", type=int, default=10, help="hyperperiods per random run (default 10)")
    ap.add_argument("--bcet", type=float, default=0.5, help="random execution times in [bcet*WCET, WCET]")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--search", type=int, default=3,
                    help="Level B: random variants of the other publishers' phases per scenario")
    ap.add_argument("--idle-slope", default="srp", help="Level B: srp | load | A=x,B=y (default srp)")
    ap.add_argument("--idle-slope-factor", type=float, default=1.0, help="Level B with load: factor")
    ap.add_argument("--be", choices=["auto", "on", "off"], default="auto",
                    help="Level B best effort: auto = as in the AVB analysis input")
    ap.add_argument("--resume", action="store_true", help="sweep: keep existing validation.csv files")
    ap.add_argument("--out", help="output folder without --exp-id (default: none, print only)")
    args = ap.parse_args(argv)
    if args.sweep:
        return validate_sweep(args.sweep, args)
    if not args.file:
        ap.error("give the YAML file, or --sweep")
    folder = os.path.join(args.work_dir, f"Experiment{args.exp_id}") if args.exp_id is not None else None
    if folder and not os.path.isdir(folder):
        raise SystemExit(f"{folder} not found (run run_all.py first)")
    rows, traces, summary = validate_experiment(args.file, args.system, folder, args)
    return 1 if summary["violations"] else 0


if __name__ == "__main__":
    sys.exit(main())
