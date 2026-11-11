"""Generate one system YAML from a scenario file (see scenario.yaml).

    python generate_system.py scenario.yaml --out generated.yaml [--seed N]

What is drawn (all from the scenario's seed, so the result is reproducible):
  topology     given in the scenario (links + node roles: the same for every seed),
               or a line of switches -1, -2, ..., -N (N = 1 is a star) built from
               topology.seed, with nodes connected by `placement`
  WGs          per publisher a number of WGs; per WG a period, v variables with
               types, Delta DSMs (variables split over them)
  WCETs        base + slope * size, base and slope drawn per task from ranges
  classes      round(st_share * WGs) ST WGs, picked with weight T^-bias; the others
               AVB (A or B is assigned later, in step 5)
  priorities   global: all ST WGs above all AVB WGs, then shorter period first,
               then smaller WriterGroupId; rho = rank
  subscribers  every WG to one subscriber (each subscriber gets at least one);
               Delta' and v' equal the WG's
  best effort  flows from the BE sources to random subscribers
A draw is kept only if it passes the loader's checks (MTU, link load, ...) and
every publisher and subscriber has sum W/T below limits.max_cpu_utilization;
otherwise the next draw is tried, up to limits.attempts.
"""
from __future__ import annotations

import argparse
import copy
import random
import sys
from collections import Counter
from fractions import Fraction

import yaml

import uadp
from system import ConfigError, _parse_system


class ScenarioError(ValueError):
    pass


# ----------------------------------------------------------------- helpers

def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _irange(rng: random.Random, r, what: str) -> int:
    if isinstance(r, int):
        return r
    lo, hi = r
    if lo > hi or lo < 0:
        raise ScenarioError(f"{what}: bad range {r}")
    return rng.randint(lo, hi)


def _frange(rng: random.Random, r) -> float:
    if isinstance(r, (int, float)):
        return float(r)
    lo, hi = r
    return rng.uniform(lo, hi)


def _us(x: float) -> float:
    """WCETs rounded to the ns, at least 1 ns."""
    return max(0.001, round(x, 3))


def _cost(rng: random.Random, spec: dict, size: float, slope_key: str | None) -> float:
    base = _frange(rng, spec["base"])
    slope = _frange(rng, spec[slope_key]) if slope_key else 0.0
    return _us(base + slope * size)


def _weighted_sample(rng: random.Random, items: list, weights: list[float], k: int) -> list:
    items, weights, out = list(items), list(weights), []
    for _ in range(k):
        i = rng.choices(range(len(items)), weights=weights)[0]
        out.append(items.pop(i))
        weights.pop(i)
    return out


def _split(rng: random.Random, v: int, delta: int) -> list[int]:
    """v variables over delta DSMs, each DSM at least one: sizes of the groups."""
    cuts = sorted(rng.sample(range(1, v), delta - 1))
    bounds = [0] + cuts + [v]
    return [b - a for a, b in zip(bounds, bounds[1:])]


# ------------------------------------------------------------------ drawing

class Topology:
    """End nodes by role and the links, fixed for all draws of one generation."""

    def __init__(self, links, pub_nodes, sub_nodes, be_nodes):
        self.links, self.pub_nodes, self.sub_nodes, self.be_nodes = links, pub_nodes, sub_nodes, be_nodes

    @property
    def switches(self) -> int:
        return len({x for l in self.links for x in l if x < 0})


def _node_list(t: dict, key: str) -> list[int]:
    v = t.get(key, [])
    if not isinstance(v, list) or not all(isinstance(x, int) and not isinstance(x, bool) and x > 0
                                          for x in v):
        raise ScenarioError(f"topology.{key}: expected a list of end-node numbers (positive)")
    return v


def build_topology(sc: dict, seed: int) -> Topology:
    t = sc["topology"]
    if "links" in t:                                    # given in the scenario: fixed
        links = t["links"]
        pubs, subs = _node_list(t, "publishers"), _node_list(t, "subscribers")
        bes = _node_list(t, "best_effort_sources")
        roles = pubs + subs + bes
        if len(set(roles)) != len(roles):
            raise ScenarioError("topology: a node is listed in more than one role")
        ends = {x for l in links for x in l if x > 0}
        missing = [x for x in roles if x not in ends]
        if missing:
            raise ScenarioError(f"topology: node(s) {missing} are not in topology.links")
        if not pubs or not subs:
            raise ScenarioError("topology: at least one publisher and one subscriber are needed")
        return Topology([list(l) for l in links], pubs, subs, bes)

    # generated line of switches, from its own seed
    rng = random.Random(f"{t.get('seed', seed)}:topology")
    P, S, B = t["publishers"], t["subscribers"], t.get("best_effort_sources", 0)
    n_switch = t["switches"]
    if not all(isinstance(x, int) for x in (P, S, B, n_switch)):
        raise ScenarioError("topology: switches, publishers, subscribers and best_effort_sources "
                            "are counts here (or give links and node lists)")
    if P < 1 or S < 1 or n_switch < 1:
        raise ScenarioError("publishers, subscribers and switches must be at least 1")
    pub_nodes = list(range(1, P + 1))
    sub_nodes = list(range(P + 1, P + S + 1))
    be_nodes = list(range(P + S + 1, P + S + B + 1))
    switches = [-(i + 1) for i in range(n_switch)]
    placement = t.get("placement", "random")
    attach = {}
    for idx, node in enumerate(pub_nodes + sub_nodes + be_nodes):
        if placement == "round_robin":
            attach[node] = switches[idx % n_switch]
        elif placement == "split":
            half = max(1, n_switch // 2)
            side = switches[:half] if node in pub_nodes else switches[n_switch - half:]
            attach[node] = rng.choice(side)
        elif placement == "random":
            attach[node] = rng.choice(switches)
        else:
            raise ScenarioError(f"topology.placement: random, round_robin or split, got {placement!r}")
    links = [[node, sw] for node, sw in attach.items()]
    links += [[switches[i], switches[i + 1]] for i in range(n_switch - 1)]
    return Topology(links, pub_nodes, sub_nodes, be_nodes)


def draw(rng: random.Random, sc: dict, topo: Topology) -> dict:
    be = sc["best_effort"]
    if be["flows"] and not topo.be_nodes:
        raise ScenarioError("best_effort.flows needs at least one best_effort source in the topology")
    pid_type = sc["network"]["publisher_id_type"]
    pub_nodes, sub_nodes, be_nodes, links = topo.pub_nodes, topo.sub_nodes, topo.be_nodes, topo.links
    P, S = len(pub_nodes), len(sub_nodes)

    # ---- WGs
    periods, pweights = sc["periods"], sc.get("period_weights")
    if not all(isinstance(t, int) and t > 0 for t in periods):
        raise ScenarioError("periods must be whole microseconds")
    types, tweights = list(sc["types"]), list(sc["types"].values())
    W = sc["wcet"]["publisher"]
    wgs = []                                   # dicts with the drawn structure
    wgid = 0
    for p, node in zip(range(1, P + 1), pub_nodes):
        for n in range(1, _irange(rng, sc["wgs_per_publisher"], "wgs_per_publisher") + 1):
            wgid += 1
            v = _irange(rng, sc["variables_per_wg"], "variables_per_wg")
            lo, hi = sc["dsms_per_wg"]
            delta = rng.randint(min(lo, v), min(hi, v))
            tps = rng.choices(types, weights=tweights, k=v)
            groups = _split(rng, v, delta)
            nm = uadp.nm_size(tps, delta, pid_type)
            wgs.append({
                "p": p, "n": n, "writer_group_id": wgid,
                "T": rng.choices(periods, weights=pweights)[0] if pweights else rng.choice(periods),
                "types": tps, "groups": groups, "nm": nm,
                "psi": [_cost(rng, W["collect"], uadp.field_size(t), "per_byte") for t in tps],
                "phi": [_cost(rng, W["dsw"], g, "per_field") for g in groups],
                "C3": _cost(rng, W["wg"], delta, "per_dsm"),
                "C4": _cost(rng, W["transport"], nm, "per_byte"),
            })
    N = len(wgs)
    if N < S:
        raise ScenarioError(f"{N} WGs cannot cover {S} subscribers; raise wgs_per_publisher")

    # ---- classes and global priorities
    tr = sc["traffic"]
    n_st = round(tr["st_share"] * N)
    st = _weighted_sample(rng, wgs, [w["T"] ** -tr["st_short_period_bias"] for w in wgs], n_st)
    st_ids = {w["writer_group_id"] for w in st}
    avb = sorted((w for w in wgs if w["writer_group_id"] not in st_ids),
                 key=lambda w: (w["T"], w["writer_group_id"]))
    for w in avb:
        w["class"] = "AVB"
    for w in st:
        w["class"] = "ST"
    order = sorted(st, key=lambda w: (w["T"], w["writer_group_id"])) + avb
    for rank, w in enumerate(order):
        w["rho"] = N - rank

    # ---- subscribers: each gets at least one WG
    shuffled = wgs[:]
    rng.shuffle(shuffled)
    owner = {w["writer_group_id"]: sub_nodes[i] if i < S else rng.choice(sub_nodes)
             for i, w in enumerate(shuffled)}
    Ws = sc["wcet"]["subscriber"]
    subscribers = []
    for k, node in zip(range(1, S + 1), sub_nodes):
        txs = []
        for w in wgs:
            if owner[w["writer_group_id"]] != node:
                continue
            txs.append({
                "m": len(txs) + 1, "writer_group_id": w["writer_group_id"],
                "C1": _cost(rng, Ws["reception"], w["nm"], "per_byte"),
                "C2": _cost(rng, Ws["filter"], 0, None),
                "phi": [_cost(rng, Ws["decode"], g, "per_field") for g in w["groups"]],
                "psi": [_cost(rng, Ws["dispatch"], uadp.field_size(t), "per_byte") for t in w["types"]],
            })
        subscribers.append({"k": k, "node": node, "transactions": txs})

    # ---- network
    messages = []
    for w in wgs:
        messages.append({"writer_group_id": w["writer_group_id"], "hard_real_time": w["class"] == "ST"})
    flows = [{"name": f"be{i + 1}", "source": rng.choice(be_nodes), "destination": rng.choice(sub_nodes),
              "length": _irange(rng, be["length"], "best_effort.length"),
              "period": rng.choice(be["periods"])} for i in range(be["flows"])]
    net = dict(sc["network"])
    network = {"links": links, "full_duplex": True, **net, "messages": messages}
    if flows:
        network["best_effort"] = flows

    publishers = []
    for p, node in zip(range(1, P + 1), pub_nodes):
        publishers.append({"p": p, "node": node, "writer_groups": [
            {"n": w["n"], "writer_group_id": w["writer_group_id"], "T": w["T"], "rho": w["rho"],
             "types": w["types"], "psi": w["psi"], "phi": w["phi"], "C3": w["C3"], "C4": w["C4"]}
            for w in wgs if w["p"] == p]})
    return {"name": sc["name"], "publishers": publishers, "subscribers": subscribers, "network": network}


# ------------------------------------------------------------------ checking

def utilizations(system) -> tuple[dict, dict]:
    pub = {p.p: sum((Fraction(w.W) / w.T for w in p.wgs), Fraction(0)) for p in system.publishers}
    sub = {s.k: sum((Fraction(t.W) / t.T for t in s.transactions), Fraction(0)) for s in system.subscribers}
    return pub, sub


def generate(sc: dict, seed: int | None = None):
    """(system dict, loaded System, number of draws)."""
    seed = sc["seed"] if seed is None else seed
    rng = random.Random(seed)
    topo = build_topology(sc, seed)
    limit = Fraction(str(sc["limits"]["max_cpu_utilization"]))
    reasons: Counter = Counter()
    for attempt in range(1, sc["limits"]["attempts"] + 1):
        try:
            d = draw(rng, sc, topo)
        except uadp.SizeError as e:
            reasons[f"NM size: {e}".split(" exceeds")[0]] += 1
            continue
        try:
            system = _parse_system(copy.deepcopy(d), "generated")
        except ConfigError as e:
            msg = str(e)
            reasons["link load above max_link_load" if "max_link_load" in msg
                    else "MTU exceeded" if "MTU" in msg else msg] += 1
            continue
        pub, sub = utilizations(system)
        if max(pub.values()) >= limit:
            reasons["publisher utilization >= limit"] += 1
            continue
        if max(sub.values()) >= limit:
            reasons["subscriber utilization >= limit"] += 1
            continue
        return d, system, attempt
    detail = ", ".join(f"{r}: {n}" for r, n in reasons.most_common())
    raise ScenarioError(f"no valid system in {sc['limits']['attempts']} draws ({detail}); "
                        f"use fewer WGs/variables, longer periods or smaller WCET ranges")


def load_scenario(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        sc = yaml.safe_load(f)
    required = ("name", "seed", "topology", "wgs_per_publisher", "periods",
                "variables_per_wg", "dsms_per_wg", "types", "traffic", "best_effort", "network",
                "limits", "wcet")
    missing = [k for k in required if k not in sc]
    if missing:
        raise ScenarioError(f"{path}: missing {', '.join(missing)}")
    return sc


def write_yaml(d: dict, path: str, header: list[str]) -> None:
    class Dumper(yaml.SafeDumper):
        pass

    def short_lists(dumper, data):           # numbers and node pairs on one line
        flow = all(not isinstance(x, (dict, list)) for x in data) or \
               all(isinstance(x, list) and len(x) == 2 for x in data)
        return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=flow)

    Dumper.add_representer(list, short_lists)
    with open(path, "w", encoding="utf-8") as f:
        f.write("".join(f"# {h}\n" for h in header))
        yaml.dump({"time_unit": "us", "systems": [d]}, f, Dumper=Dumper, sort_keys=False, width=110)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate a system YAML from a scenario")
    ap.add_argument("scenario", help="scenario YAML (see scenario.yaml)")
    ap.add_argument("--out", default="generated.yaml", help="output file (default generated.yaml)")
    ap.add_argument("--seed", type=int, help="override the scenario's seed")
    args = ap.parse_args(argv)

    sc = load_scenario(args.scenario)
    seed = sc["seed"] if args.seed is None else args.seed
    d, system, attempts = generate(sc, seed)
    pub, sub = utilizations(system)
    wgs = [w for p in system.publishers for w in p.wgs]
    classes = Counter(m.traffic_class for m in system.network.values())
    summary = [
        f"Generated by generate_system.py from {args.scenario}, seed {seed} (draw {attempts}).",
        f"{len(system.publishers)} publishers, {len(system.subscribers)} subscribers, "
        f"{len({x for l in system.network_config.links for x in l if x < 0})} switch(es), {len(wgs)} WGs "
        f"(ST {classes['ST']}, AVB {classes['AVB']}), "
        f"{len(system.best_effort)} best-effort flows.",
        "CPU utilization: publishers " + ", ".join(f"E{p} {float(u):.3f}" for p, u in pub.items())
        + "; subscribers " + ", ".join(f"E'{k} {float(u):.3f}" for k, u in sub.items()) + ".",
    ]
    write_yaml(d, args.out, summary)
    print("\n".join(summary))
    print(f"wrote {args.out}; run it with:\n  python run_all.py {args.out} --system {sc['name']} --exp-id 1")
    return 0


if __name__ == "__main__":
    sys.exit(main())
