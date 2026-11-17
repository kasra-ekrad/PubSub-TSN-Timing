"""Scaling study on one fixed topology, over many seeds (automated).

    python scaling_study.py experiments/scaling_data.yaml    [--seeds 1 5] [--jobs 4] [--resume]
    python scaling_study.py experiments/scaling_count.yaml   [--tools-dir .] [--runner ...]
    python scaling_study.py experiments/scaling_count.yaml --plot-only

Two modes (`mode:` in the spec):

  data   Every seed draws a system on the topology (WGs per publisher, periods, data types and
         WCETs from the scenario's ranges; every WG goes to one of the subscribers, in turn).
         Then every WG of every publisher gets one more variable per step, from
         `start_variables` until the NetworkMessage of a WG no longer fits the MTU.
         x = variables per WG.
  count  Transactions (WGs) with the same data are added one at a time, round robin over
         the publishers (WG 1 on publisher 1, WG 2 on publisher 2, ...) and in turn over the
         subscribers. Periods and WCETs are drawn per WG from the scenario's ranges.
         x = number of transactions.

At every step: the utilization of every publisher and subscriber (sum W/T), the publisher
response times, and, through run_all.py and the exes, R^N, R^s and the end-to-end R of every
transaction. Three limits are tracked per seed, each at the first step it is crossed:
    pub_util   a publisher with sum W/T >= max_cpu_utilization
    sub_util   a subscriber with sum W'/T >= max_cpu_utilization
    deadline   a transaction misses its deadline: R^p > T, the AVB analysis reports a miss,
               J' > T, R^s > T, or the end-to-end R > deadline_factor * T
After the first deadline miss the exes are no longer run (no later step can be schedulable);
only the utilizations are computed, until both utilization limits are crossed or the last
step is reached. The response times of every transaction are kept for every step up to and
including the first deadline miss. All WGs are AVB; no ST, no best-effort traffic.

Writes into results/<name>/:
    seeds/seed_<n>.json          everything of one seed (used by --resume and --plot-only)
    logs/                        run_all.py output per step
    <name>_limits.csv            one row per seed: where each limit is crossed
    <name>_steps.csv             one row per seed and step: max utilizations, response times
    <name>_wgs.csv               one row per seed, step and transaction: W, W', R^p, R^N, J', R^s, R
    <name>_nodes.csv             one row per seed, step and node: utilization
    <name>_survival.pdf          share of systems within each limit, against x
    <name>_growth.pdf            utilizations and response times against x (median, 25-75 %)
    <name>_limits.pdf            where each limit is crossed (all seeds) and which comes first
    <name>_size.pdf              (data mode) the limit against the number of WGs in the system
"""
from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import copy
import csv
import json
import os
import random
import shutil
import statistics
import sys
from dataclasses import replace
from fractions import Fraction

import yaml

import generate_system as gen
import link_report
import plot_style as ps
import run_all
import uadp
from network_results import apply_network_results
from plot_style import plt
from publisher_rta import analyse_system
from run_sweep import seed_list
from subscriber_rta import analyse_all
from system import ConfigError, _parse_system

LIMITS = ("pub_util", "sub_util", "deadline")
LIMIT_NAME = {"pub_util": "Publisher utilization < 1", "sub_util": "Subscriber utilization < 1",
              "deadline": "All deadlines met", "all": "All three"}
V_CAP = 2000          # longest list of variable types drawn per WG (data mode)


# ------------------------------------------------------------------ spec

def load_spec(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        sp = yaml.safe_load(f)
    for key in ("name", "mode", "scenario", "seeds", "topology"):
        if key not in sp:
            raise SystemExit(f"{path}: missing '{key}'")
    if sp["mode"] not in ("data", "count"):
        raise SystemExit(f"{path}: mode must be 'data' or 'count'")
    sc = gen.load_scenario(os.path.join(os.path.dirname(os.path.abspath(path)), sp["scenario"]))
    sc["topology"] = sp["topology"]
    sc["traffic"] = {**sc.get("traffic", {}), "st_share": 0}
    sc["best_effort"] = {"flows": 0}
    for key in ("wgs_per_publisher", "dsms_per_wg", "periods", "period_weights", "types"):
        if key in sp:
            sc[key] = sp[key]
    sp["_scenario"] = sc
    return sp


# ------------------------------------------------------------------ drawing

def _coef(rng, spec: dict, slope_key: str | None) -> tuple[float, float]:
    return gen._frange(rng, spec["base"]), (gen._frange(rng, spec[slope_key]) if slope_key else 0.0)


def _cost(c: tuple[float, float], size: float) -> float:
    return gen._us(c[0] + c[1] * size)


def _draw_wg(rng, sc: dict, wgid: int, p: int, sub: int, types: list[str], delta: int) -> dict:
    """One WG with its period and the coefficients of all its WCETs (fixed for the seed)."""
    Wp, Ws = sc["wcet"]["publisher"], sc["wcet"]["subscriber"]
    periods, weights = sc["periods"], sc.get("period_weights")
    return {"id": wgid, "p": p, "sub": sub,
            "T": rng.choices(periods, weights=weights)[0] if weights else rng.choice(periods),
            "types": types, "delta": delta,
            "dsw": [_coef(rng, Wp["dsw"], "per_field") for _ in range(delta)],
            "wg": _coef(rng, Wp["wg"], "per_dsm"),
            "tr": _coef(rng, Wp["transport"], "per_byte"),
            "rec": _coef(rng, Ws["reception"], "per_byte"),
            "fil": _coef(rng, Ws["filter"], None),
            "dec": [_coef(rng, Ws["decode"], "per_field") for _ in range(delta)]}


def draw(sp: dict, seed: int) -> dict:
    """Everything random of one seed: the per-type costs and the WGs (data mode: the
    system; count mode: the stream of transactions, of which the first x are used)."""
    sc = sp["_scenario"]
    topo = gen.build_topology(sc, seed)
    P, S = len(topo.pub_nodes), len(topo.sub_nodes)
    trng = random.Random(f"{seed}:types")
    types = list(sc["types"])
    psi_pub = gen.type_costs(trng, sc["wcet"]["publisher"]["collect"], types, "publisher.collect")
    psi_sub = gen.type_costs(trng, sc["wcet"]["subscriber"]["dispatch"], types, "subscriber.dispatch")
    rng = random.Random(seed)
    wgs = []
    if sp["mode"] == "data":
        tw = list(sc["types"].values())
        for p in range(1, P + 1):
            for _ in range(gen._irange(rng, sc["wgs_per_publisher"], "wgs_per_publisher")):
                wgid = len(wgs) + 1
                delta = gen._irange(rng, sc["dsms_per_wg"], "dsms_per_wg")
                wgs.append(_draw_wg(rng, sc, wgid, p, (wgid - 1) % S, rng.choices(types, weights=tw, k=V_CAP),
                                    delta))
    else:
        w = sp["wg"]
        if w["type"] not in psi_pub:
            psi_pub[w["type"]] = gen.type_costs(random.Random(f"{seed}:types"),
                                                sc["wcet"]["publisher"]["collect"], [w["type"]], "collect")[w["type"]]
            psi_sub[w["type"]] = gen.type_costs(random.Random(f"{seed}:types"),
                                                sc["wcet"]["subscriber"]["dispatch"], [w["type"]], "dispatch")[w["type"]]
        for k in range(1, int(sp["max_wgs"]) + 1):
            wgs.append(_draw_wg(rng, sc, k, (k - 1) % P + 1, (k - 1) % S, [w["type"]] * int(w["v"]),
                                int(w.get("dsms", 1))))
    return {"seed": seed, "psi_pub": psi_pub, "psi_sub": psi_sub, "wgs": wgs, "topo": topo}


def _groups(v: int, delta: int) -> list[int]:
    d = min(delta, v)
    return [v // d + (1 if i < v % d else 0) for i in range(d)]


def system_dict(sp: dict, dr: dict, x: int) -> dict:
    """The system of one step: data mode, every WG with x variables; count mode, the first
    x transactions. Priorities: shorter period first (then smaller WG id), all AVB."""
    sc, topo = sp["_scenario"], dr["topo"]
    net = dict(sc["network"])
    pid = net["publisher_id_type"]
    if sp["mode"] == "data":
        wgs = [{**w, "v": x} for w in dr["wgs"]]
    else:
        wgs = [{**w, "v": len(w["types"])} for w in dr["wgs"][:x]]
    order = sorted(wgs, key=lambda w: (w["T"], w["id"]))
    rho = {w["id"]: len(wgs) - r for r, w in enumerate(order)}
    pubs, subs = {}, {}
    for w in wgs:
        types = w["types"][:w["v"]]
        groups = _groups(w["v"], w["delta"])
        nm = uadp.nm_size(types, len(groups), pid, net.get("payload_header", False))
        uadp.frame_size(nm, net.get("vlan_tag", True))                    # raises above the MTU
        pubs.setdefault(w["p"], []).append({
            "n": len(pubs.get(w["p"], [])) + 1, "writer_group_id": w["id"], "T": w["T"], "rho": rho[w["id"]],
            "types": types, "psi": [dr["psi_pub"][t] for t in types],
            "phi": [_cost(c, g) for c, g in zip(w["dsw"], groups)],
            "C3": _cost(w["wg"], len(groups)), "C4": _cost(w["tr"], nm)})
        subs.setdefault(w["sub"], []).append({
            "m": len(subs.get(w["sub"], [])) + 1, "writer_group_id": w["id"],
            "C1": _cost(w["rec"], nm), "C2": _cost(w["fil"], 0),
            "phi": [_cost(c, g) for c, g in zip(w["dec"], groups)],
            "psi": [dr["psi_sub"][t] for t in types]})
    return {"name": sp["name"],
            "publishers": [{"p": p, "node": topo.pub_nodes[p - 1], "writer_groups": pubs[p]} for p in sorted(pubs)],
            "subscribers": [{"k": k + 1, "node": topo.sub_nodes[k], "transactions": subs[k]} for k in sorted(subs)],
            "network": {"links": topo.links, "full_duplex": True, **net,
                        "messages": [{"writer_group_id": w["id"], "hard_real_time": False} for w in wgs]}}


def steps(sp: dict) -> list[int]:
    if sp["mode"] == "data":
        a, b = int(sp.get("start_variables", 1)), sp.get("max_variables", "max")
        b = V_CAP if b in (None, "max") else int(b)
    else:
        a, b = int(sp.get("start_wgs", 1)), int(sp["max_wgs"])
    return list(range(a, b + 1, int(sp.get("step", 1))))


# ------------------------------------------------------------------ one step

def _f(x, nd=3):
    return None if x is None else round(float(x), nd)


def evaluate(sp: dict, d: dict, system, folder: str | None):
    """Analysis of one step. With `folder` (network results there): everything; without:
    the publishers only. Returns (wg rows, deadline reasons)."""
    lim_d = Fraction(str(sp.get("deadline_factor", 1)))
    T = {w.writer_group_id: w.T for p in system.publishers for w in p.wgs}
    Ws = {t.writer_group_id: t.W for s in system.subscribers for t in s.transactions}
    sub_of = {t.writer_group_id: s.node for s in system.subscribers for t in s.transactions}
    pub_of = {w.writer_group_id: p.node for p in system.publishers for w in p.wgs}
    rows, reasons = {}, set()
    if folder is None:
        pub = analyse_system(system)
        for rs in pub.values():
            for r in rs:
                rows[r.writer_group_id] = {"Rp": r.R, "pub_status": r.status}
                if r.status != "ok":
                    reasons.add("publisher")
    else:
        try:
            network = apply_network_results(system, folder, None,
                                            os.path.join(folder, "final_new_results.txt")).network
        except (ConfigError, ValueError) as e:
            reasons.add("network (J' > T)" if "jitter" in str(e) else "network")
            return evaluate(sp, d, system, None)[0], reasons
        pub, sub, e2e = analyse_all(replace(system, network=network))
        for rs in pub.values():
            for r in rs:
                if r.status != "ok":
                    reasons.add("publisher")
        for e in e2e:
            rows[e.writer_group_id] = {"Rp": e.R_p, "RN": e.R_max, "J": e.J, "Rs": e.R_s, "R": e.R,
                                       "pub_status": e.pub_status, "net_status": e.net_status,
                                       "sub_status": e.sub_status}
            if e.net_status == "deadline_miss":
                reasons.add("network")
            if e.sub_status != "ok":
                reasons.add("subscriber")
            if e.R is not None and e.R > lim_d * T[e.writer_group_id]:
                reasons.add("end-to-end")
    out = []
    for g in sorted(T):
        r = rows.get(g, {})
        _, wg = system.writer_groups()[g]
        out.append({"wg": g, "publisher": pub_of[g], "subscriber": sub_of.get(g), "T": _f(T[g]),
                    "W": _f(wg.W), "Ws": _f(Ws.get(g)), "Rp": _f(r.get("Rp")), "RN": _f(r.get("RN")),
                    "J": _f(r.get("J")), "Rs": _f(r.get("Rs")), "R": _f(r.get("R")),
                    "pub_status": r.get("pub_status", ""), "net_status": r.get("net_status", ""),
                    "sub_status": r.get("sub_status", "")})
    return out, reasons


def dict_utilizations(d: dict) -> tuple[dict, dict]:
    """sum W/T per publisher node and sum W'/T per subscriber node, straight from the dict."""
    F = lambda x: Fraction(str(x))
    T = {}
    pu, su = {}, {}
    for p in d["publishers"]:
        for w in p["writer_groups"]:
            T[w["writer_group_id"]] = F(w["T"])
            W = sum(map(F, w["psi"])) + sum(map(F, w["phi"])) + F(w["C3"]) + F(w["C4"])
            pu[p["node"]] = pu.get(p["node"], Fraction(0)) + W / T[w["writer_group_id"]]
    for sb in d["subscribers"]:
        for t in sb["transactions"]:
            W = F(t["C1"]) + F(t["C2"]) + sum(map(F, t["phi"])) + sum(map(F, t["psi"]))
            su[sb["node"]] = su.get(sb["node"], Fraction(0)) + W / T[t["writer_group_id"]]
    return pu, su


def run_network(sp: dict, d: dict, out: str, exp_id: int, args) -> str | None:
    """run_all.py for one step (the exes run); the Experiment folder, or None on failure."""
    sys_path = os.path.join(out, "systems", f"exp{exp_id}.yaml")
    gen.write_yaml(d, sys_path, [f"{sp['name']}: experiment {exp_id}"])
    folder = os.path.join(out, f"Experiment{exp_id}")
    argv = [sys_path, "--system", sp["name"], "--exp-id", str(exp_id), "--work-dir", out,
            "--tools-dir", args.tools_dir, "--num-queues", str(args.num_queues), "--folder-arg", args.folder_arg]
    if args.runner is not None:
        argv += ["--runner", args.runner]
    with open(os.path.join(out, "logs", f"run_{exp_id}.log"), "w", encoding="utf-8") as f, \
            contextlib.redirect_stdout(f), contextlib.redirect_stderr(f):
        try:
            run_all.main(argv)
        except BaseException as e:                        # SystemExit included
            print(f"run_all: {type(e).__name__}: {e}")
    return folder if os.path.isfile(os.path.join(folder, "final_new_results.txt")) else None


# ------------------------------------------------------------------ one seed

def run_seed(sp: dict, seed: int, out: str, args) -> dict:
    lim = Fraction(str(sp.get("max_cpu_utilization", 1)))
    xs = steps(sp)
    dr, attempts = None, 0
    for attempts in range(1, int(sp.get("attempts", 200)) + 1):    # data mode: a valid start
        cand = draw(sp, seed * 1000 + attempts - 1 if attempts > 1 else seed)
        try:
            system = _parse_system(copy.deepcopy(system_dict(sp, cand, xs[0])), "start")
        except (ConfigError, uadp.SizeError):
            continue
        pu, su = gen.utilizations(system)
        if sp["mode"] == "count" or (max(pu.values()) < lim and max(su.values()) < lim):
            dr = cand
            break
    res = {"seed": seed, "attempts": attempts, "steps": [], "wgs": [], "nodes": [],
           "limits": {k: None for k in LIMITS}, "deadline_reason": "", "stop": ""}
    if dr is None:
        res["stop"] = "no valid start"
        return res
    res["n_wgs"] = len(dr["wgs"]) if sp["mode"] == "data" else None
    res["wgs_per_publisher"] = (" ".join(str(sum(w["p"] == p for w in dr["wgs"]))
                                         for p in range(1, len(dr["topo"].pub_nodes) + 1))
                                if sp["mode"] == "data" else "")
    net_on = True
    for i, x in enumerate(xs):
        try:
            d = system_dict(sp, dr, x)
            if not net_on:                    # after the first deadline miss: utilizations only
                pu, su = dict_utilizations(d)
                up, us = max(pu.values()), max(su.values())
                for key, u in (("pub_util", up), ("sub_util", us)):
                    if u >= lim and res["limits"][key] is None:
                        res["limits"][key] = x
                res["steps"].append({"seed": seed, "x": x, "n_wgs": sum(len(p["writer_groups"])
                                                                       for p in d["publishers"]),
                                     "network": 0, "U_pub_max": _f(up, 4), "U_sub_max": _f(us, 4),
                                     "pub_util_ok": int(up < lim), "sub_util_ok": int(us < lim)})
                res["nodes"] += [{"seed": seed, "x": x, "kind": kind, "node": node, "U": _f(u, 4)}
                                 for kind, uu in (("publisher", pu), ("subscriber", su)) for node, u in uu.items()]
                if all(res["limits"][k] is not None for k in LIMITS):
                    res["stop"] = "all limits crossed"
                    break
                continue
            system = _parse_system(copy.deepcopy(d), sp["name"])
        except uadp.SizeError:
            res["stop"] = "MTU reached"
            break
        except ConfigError as e:
            res["stop"] = "link load above max_link_load" if "max_link_load" in str(e) else f"config: {e}"
            break
        pu, su = gen.utilizations(system)
        up, us = max(pu.values()), max(su.values())
        if up >= lim and res["limits"]["pub_util"] is None:
            res["limits"]["pub_util"] = x
        if us >= lim and res["limits"]["sub_util"] is None:
            res["limits"]["sub_util"] = x
        folder = None
        if net_on:
            exp_id = seed * 10000 + i + 1
            folder = run_network(sp, d, out, exp_id, args)
            if folder is None:
                res["stop"] = f"network tools failed (logs/run_{exp_id}.log)"
                break
        rows, reasons = evaluate(sp, d, system, folder)
        if folder and not args.keep:
            shutil.rmtree(folder, ignore_errors=True)
            with contextlib.suppress(OSError):
                os.remove(os.path.join(out, "systems", f"exp{exp_id}.yaml"))
        if reasons and res["limits"]["deadline"] is None:
            res["limits"]["deadline"] = x
            res["deadline_reason"] = ", ".join(sorted(reasons))
        lu = max((l["utilization"] for l in link_report.link_utilization(system)), default=0)

        def stat(key, fn):
            vals = [r[key] for r in rows if r[key] is not None]
            return _f(fn(vals)) if vals else None

        def ratio(key):
            vals = [r[key] / r["T"] for r in rows if r[key] is not None]
            return _f(max(vals), 4) if vals else None

        res["steps"].append({
            "seed": seed, "x": x, "n_wgs": len(rows), "network": int(folder is not None),
            "U_pub_max": _f(up, 4), "U_sub_max": _f(us, 4), "link_util_max": _f(lu, 4),
            **{f"{k}_mean": stat(k, statistics.fmean) for k in ("Rp", "RN", "Rs", "R")},
            **{f"{k}_max": stat(k, max) for k in ("Rp", "RN", "Rs", "R")},
            **{f"{k}_T_max": ratio(k) for k in ("Rp", "Rs", "R")},
            "pub_util_ok": int(up < lim), "sub_util_ok": int(us < lim),
            "deadline_ok": int(not reasons) if folder is not None else None,
            "deadline_reason": ", ".join(sorted(reasons))})
        res["wgs"] += [{"seed": seed, "x": x, **r} for r in rows]
        res["nodes"] += [{"seed": seed, "x": x, "kind": "publisher", "node": p.node, "U": _f(pu[p.p], 4)}
                         for p in system.publishers]
        res["nodes"] += [{"seed": seed, "x": x, "kind": "subscriber", "node": sb.node, "U": _f(su[sb.k], 4)}
                         for sb in system.subscribers]
        if reasons:
            net_on = False
        if all(res["limits"][k] is not None for k in LIMITS):
            res["stop"] = "all limits crossed"
            break
    else:
        res["stop"] = res["stop"] or "last step"
    res["last_x"] = res["steps"][-1]["x"] if res["steps"] else None
    return res


def _seed_job(args_tuple):
    sp, seed, out, args = args_tuple
    res = run_seed(sp, seed, out, args)
    with open(os.path.join(out, "seeds", f"seed_{seed}.json"), "w", encoding="utf-8") as f:
        json.dump(res, f)
    return seed, res["stop"], res["limits"]


# ------------------------------------------------------------------ collecting

def read_seeds(out: str) -> list[dict]:
    folder = os.path.join(out, "seeds")
    res = []
    for name in sorted(os.listdir(folder), key=lambda n: int(n[5:-5]) if n[5:-5].isdigit() else 0):
        if name.startswith("seed_") and name.endswith(".json"):
            with open(os.path.join(folder, name), encoding="utf-8") as f:
                res.append(json.load(f))
    return res


def first_limit(r: dict) -> str:
    lims = {k: v for k, v in r["limits"].items() if v is not None}
    if not lims:
        return "none"
    m = min(lims.values())
    return " + ".join(k for k in LIMITS if lims.get(k) == m)


def last_ok(r: dict) -> dict | None:
    """The step before the first limit (every limit met), or None."""
    lims = [v for v in r["limits"].values() if v is not None]
    brk = min(lims) if lims else None
    ok = [s for s in r["steps"] if brk is None or s["x"] < brk]
    return ok[-1] if ok else None


def limit_rows(sp: dict, res: list[dict]) -> list[dict]:
    rows = []
    for r in res:
        lims = [v for v in r["limits"].values() if v is not None]
        brk = min(lims) if lims else None
        ok = last_ok(r)
        rows.append({"seed": r["seed"], "attempts": r["attempts"], "n_wgs": r.get("n_wgs"),
                     "wgs_per_publisher": r.get("wgs_per_publisher", ""),
                     "last_all_ok": ok["x"] if ok else None,
                     "U_pub_last_ok": ok["U_pub_max"] if ok else None,
                     "U_sub_last_ok": ok["U_sub_max"] if ok else None,
                     "R_T_last_ok": ok.get("R_T_max") if ok else None,
                     "first_break": brk, "first_limit": first_limit(r),
                     **{f"break_{k}": r["limits"][k] for k in LIMITS},
                     "deadline_reason": r["deadline_reason"], "last_x": r.get("last_x"), "stop": r["stop"]})
    return rows


def write_csv(rows: list[dict], path: str) -> None:
    if not rows:
        return
    fields = list(dict.fromkeys(k for r in rows for k in r))
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


# ------------------------------------------------------------------ plots

XLABEL = {"data": "Variables per WG", "count": "Number of transactions"}


def _xscale(sp: dict, ax) -> None:
    """`x_scale: log` in the spec: log x-axis (useful when the limits lie far apart)."""
    if sp.get("x_scale") == "log":
        from matplotlib.ticker import FixedLocator, FuncFormatter, NullFormatter
        ax.set_xscale("log")
        ax.xaxis.set_major_locator(FixedLocator([1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000]))
        ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
        ax.xaxis.set_minor_formatter(NullFormatter())
COLOR = {"pub_util": ps.SLOT[0], "sub_util": ps.SLOT[1], "deadline": ps.SLOT[2], "all": "#000000"}


def _ok_at(r: dict, crit: str, x: int):
    """True / False / None (not known: the seed stopped before x)."""
    lims = [r["limits"][k] for k in (LIMITS if crit == "all" else (crit,))]
    if any(v is not None and x >= v for v in lims):
        return False
    return True if r.get("last_x") is not None and x <= r["last_x"] else None


def survival(sp: dict, res: list[dict], path: str) -> str:
    xs = sorted({s["x"] for r in res for s in r["steps"]})
    fig, ax = plt.subplots(figsize=(3.5, 2.5))
    for crit in ("pub_util", "sub_util", "deadline", "all"):
        ys, xx = [], []
        for x in xs:
            known = [v for v in (_ok_at(r, crit, x) for r in res) if v is not None]
            if len(known) >= max(1, len(res) // 10):
                xx.append(x)
                ys.append(100 * sum(known) / len(known))
        ax.plot(xx, ys, color=COLOR[crit], linewidth=1.4 if crit == "all" else 1.2,
                linestyle=(0, (4, 2)) if crit == "all" else "-", label=LIMIT_NAME[crit])
    if sp["mode"] == "data":                              # seeds stop when a WG reaches the MTU
        xx = [x for x in xs]
        ax.plot(xx, [100 * sum(1 for r in res if r.get("last_x") is not None and r["last_x"] >= x) / len(res)
                     for x in xx], color="#888888", linewidth=1, linestyle=(0, (1, 1.5)),
                label="Every NM fits the MTU")
    brk = [min(v for v in r["limits"].values() if v is not None) for r in res
           if any(v is not None for v in r["limits"].values())]
    if brk:
        med = statistics.median(brk)
        ax.axvline(med, color="#555555", linewidth=0.6, linestyle=(0, (2, 2)))
        ax.text(med, 50, f" median {med:g}", fontsize=6.5, va="center")
    ax.set_ylim(0, 104)
    _xscale(sp, ax)
    ax.set_xlabel(XLABEL[sp["mode"]])
    ax.set_ylabel("Systems within the limit (%)")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.24), ncols=2, columnspacing=1.0, handlelength=2.2)
    return ps.save(fig, path)


def _band(ax, res, key, color, label, min_share=0.25, scale=1.0):
    xs = sorted({s["x"] for r in res for s in r["steps"]})
    xx, med, lo, hi = [], [], [], []
    for x in xs:
        vals = [s[key] * scale for r in res for s in r["steps"] if s["x"] == x and s.get(key) is not None]
        if len(vals) >= max(1, int(min_share * len(res))):
            q = statistics.quantiles(vals, n=4) if len(vals) > 1 else [vals[0]] * 3
            xx.append(x)
            med.append(statistics.median(vals))
            lo.append(q[0])
            hi.append(q[2])
    if xx:
        ax.fill_between(xx, lo, hi, color=color, alpha=0.18, linewidth=0)
        ax.plot(xx, med, color=color, linewidth=1.2, label=label)


def growth(sp: dict, res: list[dict], path: str) -> str:
    fig, (a1, a2, a3) = plt.subplots(3, 1, figsize=(3.5, 5.6), sharex=True,
                                     gridspec_kw={"hspace": 0.12, "height_ratios": [1, 1, 1.2]})
    _band(a1, res, "U_pub_max", ps.SLOT[0], "Publisher (highest)", min_share=0.1)
    _band(a1, res, "U_sub_max", ps.SLOT[1], "Subscriber (highest)", min_share=0.1)
    a1.axhline(1, color="#000000", linewidth=0.8)
    a1.set_ylim(0, 1.6)                                  # beyond: no longer schedulable
    a1.set_ylabel("Utilization")
    a1.legend(loc="upper left", fontsize=7)
    for key, col, name in (("Rp_T_max", ps.SLOT[0], "$R^p/T$"), ("Rs_T_max", ps.SLOT[1], "$R^s/T$"),
                           ("R_T_max", "#000000", "$R/T$ (end-to-end)")):
        _band(a2, res, key, col, name)
    a2.axhline(1, color="#000000", linewidth=0.8, linestyle=(0, (2, 2)))
    a2.set_ylabel("Highest $R/T$")
    a2.legend(loc="best", fontsize=7)
    for key, col, name in (("Rp_mean", ps.SLOT[0], "$R^p$"), ("RN_mean", ps.SLOT[3], "$R^N$"),
                           ("Rs_mean", ps.SLOT[1], "$R^s$"), ("R_mean", "#000000", "$R$ (end-to-end)")):
        _band(a3, res, key, col, name)
    a3.set_yscale("log")
    a3.set_ylabel("Mean over transactions (µs)")
    a3.set_xlabel(XLABEL[sp["mode"]])
    _xscale(sp, a3)
    a3.legend(loc="upper center", bbox_to_anchor=(0.5, -0.25), ncols=4, columnspacing=0.8, handlelength=1.6)
    for a in (a1, a2, a3):
        a.grid(axis="x", visible=False)
    return ps.save(fig, path)


def _horizontal() -> dict:
    import matplotlib
    major, minor = (int(x) for x in matplotlib.__version__.split(".")[:2])
    return {"orientation": "horizontal"} if (major, minor) >= (3, 10) else {"vert": False}


def limits_plot(sp: dict, res: list[dict], path: str) -> str:
    """(a) where each limit is crossed, (b) the highest utilizations at the last step with
    every limit met, (c) which limit comes first."""
    fig, (a1, a2, a3) = plt.subplots(3, 1, figsize=(3.5, 4.6),
                                     gridspec_kw={"height_ratios": [1.3, 0.9, 1], "hspace": 0.75})
    box = dict(widths=0.5, patch_artist=True, showfliers=True, **_horizontal(),
               medianprops={"color": "#000000", "linewidth": 1},
               flierprops={"marker": ".", "markersize": 3, "markeredgecolor": "#555555"})
    keys = ("pub_util", "sub_util", "deadline")
    data = [[r["limits"][k] for r in res if r["limits"][k] is not None] or [float("nan")] for k in keys]
    bp = a1.boxplot(data, **box)
    for patch, k in zip(bp["boxes"], keys):
        patch.set(facecolor=COLOR[k], alpha=0.6, linewidth=0.6)
    a1.set_yticks(range(1, 4), [f"{n} ({sum(r['limits'][k] is not None for r in res)}/{len(res)})"
                                for n, k in zip(("Pub. util.", "Sub. util.", "Deadline"), keys)])
    a1.set_xlabel(XLABEL[sp["mode"]] + " at the first crossing")
    _xscale(sp, a1)
    oks = [last_ok(r) for r in res]
    oks = [o for o in oks if o]
    bp = a2.boxplot([[o["U_pub_max"] for o in oks] or [float("nan")],
                     [o["U_sub_max"] for o in oks] or [float("nan")]], **box)
    for patch, k in zip(bp["boxes"], ("pub_util", "sub_util")):
        patch.set(facecolor=COLOR[k], alpha=0.6, linewidth=0.6)
    a2.set_yticks([1, 2], ["Publisher", "Subscriber"])
    a2.set_xlim(0, 1.02)
    a2.set_xlabel("Highest utilization at the last step with every limit met")
    counts = {}
    for r in res:
        k = first_limit(r)
        name = {"deadline": "Deadline miss first", "sub_util + deadline": "Sub. util. and deadline, same step",
                "pub_util": "Pub. util. first", "sub_util": "Sub. util. first", "none": "No limit reached"}.get(k, k)
        counts[name] = counts.get(name, 0) + 1
    items = sorted(counts.items(), key=lambda kv: -kv[1])
    a3.barh(range(len(items)), [v for _, v in items], color="#888888", height=0.55, linewidth=0)
    for i, (_, v) in enumerate(items):
        a3.text(v, i, f" {v}", va="center", fontsize=7)
    a3.set_yticks(range(len(items)), [k for k, _ in items])
    a3.invert_yaxis()
    a3.set_xlim(0, len(res) * 1.12)
    a3.set_xlabel("Systems (which limit is reached first)")
    for a in (a1, a2, a3):
        a.grid(axis="y", visible=False)
        a.grid(axis="x", visible=True)
    for a, tag in ((a1, "(a)"), (a2, "(b)"), (a3, "(c)")):
        a.text(-0.02, 1.04, tag, transform=a.transAxes, fontsize=8, ha="right", va="bottom")
    return ps.save(fig, path)


def size_plot(sp: dict, res: list[dict], path: str) -> str | None:
    pts = [(r.get("n_wgs"), min(v for v in r["limits"].values() if v is not None)) for r in res
           if r.get("n_wgs") and any(v is not None for v in r["limits"].values())]
    if not pts:
        return None
    fig, ax = plt.subplots(figsize=(3.5, 2.3))
    rng = random.Random(0)
    ax.scatter([n + rng.uniform(-0.25, 0.25) for n, _ in pts], [b for _, b in pts], s=10, color=ps.SLOT[0],
               alpha=0.7, linewidth=0)
    by = {}
    for n, b in pts:
        by.setdefault(n, []).append(b)
    ns = sorted(by)
    ax.plot(ns, [statistics.median(by[n]) for n in ns], color="#000000", linewidth=1, label="median")
    ax.set_xlabel("WGs in the system")
    ax.set_ylabel("Variables per WG\nat the first limit")
    ax.legend(loc="upper right")
    ax.grid(axis="x", visible=False)
    return ps.save(fig, path)


# ------------------------------------------------------------------ summary

def summary(sp: dict, res: list[dict]) -> None:
    unit = "variables per WG" if sp["mode"] == "data" else "transactions"
    print(f"\n{sp['name']}: {len(res)} seeds, x = {unit}")
    print(f"  {'limit':>10} {'crossed':>8} {'min':>6} {'p10':>6} {'median':>7} {'p90':>6} {'max':>6}")
    for k in LIMITS + ("first",):
        vals = sorted(r["limits"][k] for r in res if r["limits"][k] is not None) if k != "first" else sorted(
            min(v for v in r["limits"].values() if v is not None) for r in res
            if any(v is not None for v in r["limits"].values()))
        if not vals:
            print(f"  {k:>10} {0:>8}")
            continue
        q = statistics.quantiles(vals, n=10) if len(vals) > 1 else [vals[0]] * 9
        print(f"  {k:>10} {len(vals):>8} {vals[0]:>6g} {q[0]:>6.1f} {statistics.median(vals):>7.1f} "
              f"{q[8]:>6.1f} {vals[-1]:>6g}")
    oks = [o for o in (last_ok(r) for r in res) if o]
    for key, name in (("U_pub_max", "publisher"), ("U_sub_max", "subscriber")):
        vals = sorted(o[key] for o in oks)
        if vals:
            print(f"  highest {name} utilization at the last step with every limit met: "
                  f"min {vals[0]:.3f}, median {statistics.median(vals):.3f}, max {vals[-1]:.3f}")
    gaps = [r["limits"]["sub_util"] - r["limits"]["deadline"] for r in res
            if r["limits"]["sub_util"] is not None and r["limits"]["deadline"] is not None]
    if gaps:
        print(f"  steps between the first deadline miss and subscriber utilization 1: "
              f"median {statistics.median(gaps):g}, range {min(gaps)}..{max(gaps)}")
    counts = {}
    for r in res:
        counts[first_limit(r)] = counts.get(first_limit(r), 0) + 1
    print("  first limit reached: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])))
    stops = {}
    for r in res:
        stops[r["stop"]] = stops.get(r["stop"], 0) + 1
    print("  stopped: " + ", ".join(f"{k} {v}" for k, v in stops.items()))


# ------------------------------------------------------------------ main

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Scaling study on a fixed topology over many seeds")
    ap.add_argument("spec", help="experiments/scaling_data.yaml or experiments/scaling_count.yaml")
    ap.add_argument("--out", help="output folder (default results/<name>)")
    ap.add_argument("--seeds", type=int, nargs="+", help="override the seeds: a b (= a..b) or a list")
    ap.add_argument("--tools-dir", default=".", help="folder with the two exes (default: current folder)")
    ap.add_argument("--num-queues", type=int, default=4)
    ap.add_argument("--folder-arg", default="Experiment{id}\\")
    ap.add_argument("--runner", default=None, help="see run_all.py (default: none on Windows, wine elsewhere)")
    ap.add_argument("--jobs", type=int, default=1, help="seeds run in parallel (default 1)")
    ap.add_argument("--resume", action="store_true", help="skip seeds that are already finished")
    ap.add_argument("--keep", action="store_true", help="keep every Experiment folder (large)")
    ap.add_argument("--plot-only", action="store_true", help="only rebuild the CSV files and plots")
    args = ap.parse_args(argv)
    args.tools_dir = os.path.abspath(args.tools_dir)
    sp = load_spec(args.spec)
    out = os.path.abspath(args.out or os.path.join("results", sp["name"]))
    for sub in ("seeds", "logs", "systems"):
        os.makedirs(os.path.join(out, sub), exist_ok=True)
    seeds = seed_list(args.seeds if args.seeds else sp["seeds"])

    if not args.plot_only:
        todo = [s for s in seeds
                if not (args.resume and os.path.isfile(os.path.join(out, "seeds", f"seed_{s}.json")))]
        print(f"{sp['name']} ({sp['mode']}): {len(todo)} of {len(seeds)} seeds to run")
        jobs = [(sp, s, out, args) for s in todo]
        if args.jobs > 1:
            with concurrent.futures.ProcessPoolExecutor(max_workers=args.jobs) as ex:
                for seed, stop, lims in ex.map(_seed_job, jobs):
                    print(f"  seed {seed}: {stop}; limits {lims}", flush=True)
        else:
            for job in jobs:
                seed, stop, lims = _seed_job(job)
                print(f"  seed {seed}: {stop}; limits {lims}", flush=True)

    res = [r for r in read_seeds(out) if r["seed"] in seeds]
    if not res:
        raise SystemExit("no finished seeds")
    n = sp["name"]
    paths = [os.path.join(out, f"{n}_{k}.csv") for k in ("limits", "steps", "wgs", "nodes")]
    write_csv(limit_rows(sp, res), paths[0])
    write_csv([s for r in res for s in r["steps"]], paths[1])
    write_csv([w for r in res for w in r["wgs"]], paths[2])
    write_csv([x for r in res for x in r["nodes"]], paths[3])
    ps.paper()
    paths += [survival(sp, res, os.path.join(out, f"{n}_survival.pdf")),
              growth(sp, res, os.path.join(out, f"{n}_growth.pdf")),
              limits_plot(sp, res, os.path.join(out, f"{n}_limits.pdf"))]
    if sp["mode"] == "data":
        paths.append(size_plot(sp, res, os.path.join(out, f"{n}_size.pdf")))
    summary(sp, res)
    for p in paths:
        if p:
            print("wrote", p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
