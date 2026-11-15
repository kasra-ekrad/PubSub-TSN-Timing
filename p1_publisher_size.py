"""P1: how the size of one WG changes the response times on its own publisher.

    python p1_publisher_size.py experiments/p1.yaml [--out results/p1]

One publisher (publisher analysis only: no network, no subscriber, no exes). The WG under
analysis grows one variable at a time until its NetworkMessage reaches the MTU; all other
WGs stay as given in the spec. For every size it runs publisher_rta.analyse_system and
records, per WG, W, B, the interference I = R - B - W (for the worst instance q*), R
and R/T.

Writes into --out (default results/<name>):
    p1_results.csv   one row per (v, WG)
    p1_lines.png     R of every WG and W of the target against the target's NM size
    p1_bars.png      R of every WG at the smallest and the largest size
What the analysis predicts (non-preemptive, chain = one job):
    target        R grows with W (its own execution time), B and I stay constant
    lower WGs     the target is in their interference: R grows by (instances of the
                  target in their busy window) x dW, i.e. with slope 1 while one instance fits
    higher WGs    the target is only in their blocking B = max W over lower WGs: R stays
                  flat until W_target becomes the largest lower WG, then grows with slope 1
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from fractions import Fraction

import yaml

import plot_style as ps
import uadp
from plot_style import plt
from publisher_rta import analyse_system
from system import _parse_system


# --------------------------------------------------------------------- model

def _f(x) -> Fraction:
    return Fraction(str(x))


def split(v: int, dsms: int) -> list[int]:
    """v variables over `dsms` DSMs, as evenly as possible (the first ones get one more)."""
    dsms = max(1, min(dsms, v))
    return [v // dsms + (1 if i < v % dsms else 0) for i in range(dsms)]


def wg_dict(spec: dict, wcet: dict, pid: str, n: int, rho: int) -> dict:
    """A WG of the system YAML from a size spec, with WCETs from the WCET model."""
    v, typ = int(spec["v"]), spec["type"]
    groups = split(v, int(spec.get("dsms", 1)))
    types = [typ] * v
    nm = uadp.nm_size(types, len(groups), pid)
    uadp.frame_size(nm)                                     # raises if above the MTU
    c, d, g, t = (wcet[k] for k in ("collect", "dsw", "wg", "transport"))
    psi = _f(c["base"]) + _f(c["per_byte"]) * uadp.field_size(typ)
    return {"n": n, "writer_group_id": int(spec["writer_group_id"]), "T": spec["T"], "rho": rho,
            "types": types, "psi": [str(psi)] * v,
            "phi": [str(_f(d["base"]) + _f(d["per_field"]) * k) for k in groups],
            "C3": str(_f(g["base"]) + _f(g["per_dsm"]) * len(groups)),
            "C4": str(_f(t["base"]) + _f(t["per_byte"]) * nm), "_nm": nm}


def max_variables(target: dict, pid: str) -> int:
    """The largest v whose NM still fits the MTU (IP packet <= 1500 bytes)."""
    v = 1
    while True:
        groups = split(v + 1, int(target.get("dsms", 1)))
        nm = uadp.nm_size([target["type"]] * (v + 1), len(groups), pid)
        if uadp.IPV4_HEADER + uadp.UDP_HEADER + nm > uadp.MTU:
            return v
        v += 1


def build(spec: dict, v: int) -> tuple[dict, dict[int, str], dict[int, int]]:
    """System dict for target size v; role and NM bytes per WG."""
    pid, wcet = spec.get("publisher_id_type", "UInt16"), spec["wcet"]
    order = [(w, "higher") for w in spec.get("higher", [])] + [({**spec["target"], "v": v}, "target")] \
        + [(w, "lower") for w in spec.get("lower", [])]
    wgs, role, nm = [], {}, {}
    for i, (w, r) in enumerate(order):
        d = wg_dict(w, wcet, pid, n=i + 1, rho=len(order) - i)
        nm[d["writer_group_id"]] = d.pop("_nm")
        role[d["writer_group_id"]] = r
        wgs.append(d)
    return {"name": spec["name"], "publishers": [{"p": 1, "writer_groups": wgs}]}, role, nm


def crossover(spec: dict) -> int | None:
    """Smallest v at which the target's W exceeds the W of every lower-priority WG."""
    pid, wcet, t = spec.get("publisher_id_type", "UInt16"), spec["wcet"], spec["target"]
    lower = [sum(_f(x) for x in d["psi"] + d["phi"] + [d["C3"], d["C4"]])
             for d in (wg_dict(w, wcet, pid, 0, 0) for w in spec.get("lower", []))]
    if not lower:
        return None
    for v in range(1, max_variables(t, pid) + 1):
        d = wg_dict({**t, "v": v}, wcet, pid, 0, 0)
        if sum(_f(x) for x in d["psi"] + d["phi"] + [d["C3"], d["C4"]]) > max(lower):
            return v
    return None


# ------------------------------------------------------------------ analysis

def run(spec: dict) -> tuple[list[dict], dict[int, str]]:
    t = spec["target"]
    pid = spec.get("publisher_id_type", "UInt16")
    v_to = max_variables(t, pid) if str(t.get("v_to", "max")) == "max" else int(t["v_to"])
    vs = list(range(int(t.get("v_from", 1)), v_to + 1, int(t.get("v_step", 1))))
    if vs[-1] != v_to:
        vs.append(v_to)
    rows, role = [], {}
    for v in vs:
        d, role, nm = build(spec, v)
        system = _parse_system(d, spec["name"])
        res = {r.writer_group_id: r for r in analyse_system(system)[1]}
        for g, r in res.items():
            I = None if r.R is None else r.R - r.B - r.W
            rows.append({"v": v, "target_nm_bytes": nm[int(t["writer_group_id"])],
                         "wg": g, "role": role[g], "T": float(r.T), "W": float(r.W), "B": float(r.B),
                         "I": None if I is None else float(I), "R": None if r.R is None else float(r.R),
                         "R_over_T": None if r.R is None else round(float(r.R / r.T), 6),
                         "worst_q": r.worst_q, "status": r.status})
    return rows, role


def write_csv(rows: list[dict], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


# --------------------------------------------------------------------- plots

COL = {"W": ps.SLOT[3], "target": ps.SLOT[0], "higher": ps.SLOT[1], "lower": ps.SLOT[2]}
STYLES = ["-", (0, (5, 2)), (0, (1, 1.5)), (0, (6, 2, 1, 2))]


def lines(rows: list[dict], spec: dict, path: str) -> str:
    tg = int(spec["target"]["writer_group_id"])
    wgs = sorted({r["wg"] for r in rows}, key=lambda g: next(r["role"] for r in rows if r["wg"] == g))
    by = {}
    for r in rows:
        by.setdefault(r["wg"], []).append(r)
    order = [g for role in ("higher", "target", "lower") for g in sorted(by) if by[g][0]["role"] == role]
    fig, ax = plt.subplots(figsize=(7.4, 4.4))
    count = {"higher": 0, "lower": 0}
    labels = []
    t_rows = by[tg]
    xs = [r["target_nm_bytes"] for r in t_rows]
    ax.plot(xs, [r["W"] for r in t_rows], color=COL["W"], linewidth=2, linestyle=(0, (5, 2)),
            label=f"W of WG {tg} (under analysis)")
    labels.append((xs[-1], t_rows[-1]["W"], f"W, WG {tg}", COL["W"]))
    for g in order:
        rs = [r for r in by[g] if r["R"] is not None]
        role = rs[0]["role"] if rs else by[g][0]["role"]
        if not rs:
            continue
        if role == "target":
            style, name = "-", f"R of WG {g} (under analysis)"
        else:
            style = STYLES[count[role] % len(STYLES)]
            count[role] += 1
            name = f"R of WG {g} ({'higher' if role == 'higher' else 'lower'} priority, T {rs[0]['T']:g})"
        ax.plot([r["target_nm_bytes"] for r in rs], [r["R"] for r in rs], color=COL[role], linewidth=2, linestyle=style,
                label=name)
        labels.append((rs[-1]["target_nm_bytes"], rs[-1]["R"], f"WG {g}", COL[role]))
        miss = [r for r in rs if r["status"] != "ok"]
        if miss:
            ax.scatter([r["target_nm_bytes"] for r in miss], [r["R"] for r in miss], s=14, color=COL[role], zorder=4)
    # where the target becomes the largest lower-priority WG of the WG just above it
    cross = crossover(spec)
    if cross is not None and spec.get("higher"):
        xb = _nm_of(spec, cross)
        ax.axvline(xb, color=ps.TEXT_2, linewidth=0.8, linestyle=(0, (2, 3)))
        ax.text(xb, 0.98, f"  W of WG {tg} > every lower WG (NM {xb} B, {cross} variables):\n  "
                             f"higher WGs start to see it as blocking",
                transform=ax.get_xaxis_transform(), fontsize=7, color=ps.TEXT_2, va="top")
    # direct labels at the line ends: lines ending at the same value share one label
    merged = []
    for x, y, text, color in sorted(labels, key=lambda x: x[1]):
        if merged and abs(merged[-1][1] - y) < 1e-9:
            merged[-1] = (x, y, merged[-1][2] + " = " + text.replace("WG ", "") + " (same R)", color)
        else:
            merged.append((x, y, text, color))
    labels = merged
    ax.axvline(xs[-1], color=ps.TEXT_2, linewidth=0.8)
    ax.text(xs[-1], 0.02, "MTU ", transform=ax.get_xaxis_transform(), fontsize=7, color=ps.TEXT_2,
            ha="right", va="bottom")
    span = ax.get_ylim()[1] - ax.get_ylim()[0]
    last = None
    for x, y, text, color in labels:
        y2 = y if last is None or y - last > 0.04 * span else last + 0.04 * span
        ax.text(x * 1.01 + 1, y2, text, fontsize=7, color=ps.TEXT, va="center")
        last = y2
    ax.set_xlim(0, xs[-1] * 1.2)
    ax.set_ylim(0, None)
    ax.set_xlabel(f"NetworkMessage size of WG {tg}, the WG under analysis (bytes; "
                  f"{spec['target']['type']} variables added, {spec['target'].get('dsms', 1)} "
                  f"DSM{'s' if int(spec['target'].get('dsms', 1)) > 1 else ''})")
    ax.set_ylabel("µs")
    ax.set_title(f"P1: one WG grows to the MTU; response times on its publisher", loc="left")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.16), ncols=2)
    return ps.save(fig, path)


def _nm_of(spec, v):
    t = spec["target"]
    per = uadp.field_size(t["type"])
    dsm = uadp.DSM_HEADER * int(t.get("dsms", 1))
    head = uadp.NM_HEADER_FIXED + uadp.PUBLISHER_ID_BYTES[spec.get("publisher_id_type", "UInt16")]
    return head + dsm + per * v


def bars(rows: list[dict], spec: dict, path: str) -> str:
    vs = sorted({r["v"] for r in rows})
    lo, hi = vs[0], vs[-1]
    at = {(r["v"], r["wg"]): r for r in rows}
    order = [g for role in ("higher", "target", "lower")
             for g in sorted({r["wg"] for r in rows if r["role"] == role})]
    fig, ax = plt.subplots(figsize=(6.6, 3.4))
    w = 0.38
    for i, (v, color) in enumerate(((lo, ps.SLOT[0]), (hi, ps.SLOT[1]))):
        xs = [k + (i - 0.5) * w for k in range(len(order))]
        ys = [at[(v, g)]["R"] or 0 for g in order]
        ax.bar(xs, ys, width=w, color=color, edgecolor=ps.SURFACE, linewidth=1,
               label=f"WG {spec['target']['writer_group_id']} with {v} variable{'s' if v > 1 else ''} "
                     f"(NM {at[(v, order[0])]['target_nm_bytes']} B)")
        for x, y in zip(xs, ys):
            ax.text(x, y, f"{y:.1f}", ha="center", va="bottom", fontsize=7, color=ps.TEXT_2)
    role = {g: at[(lo, g)]["role"] for g in order}
    ax.set_xticks(range(len(order)), [f"WG {g}\n{'under analysis' if role[g] == 'target' else role[g]}"
                                      for g in order])
    ax.set_ylabel("response time R (µs)")
    ax.grid(axis="x", visible=False)
    ax.set_title("P1: response times at the smallest and the largest size", loc="left")
    ax.legend(loc="upper left")
    return ps.save(fig, path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P1: one WG grows to the MTU; publisher response times")
    ap.add_argument("spec", help="experiment spec, e.g. experiments/p1.yaml")
    ap.add_argument("--out", help="output folder (default results/<name>)")
    ap.add_argument("--step", type=int, help="variables between two computed points (overrides v_step)")
    args = ap.parse_args(argv)
    with open(args.spec, encoding="utf-8") as f:
        spec = yaml.safe_load(f)
    out = args.out or os.path.join("results", spec["name"])
    os.makedirs(out, exist_ok=True)
    if args.step:
        spec["target"]["v_step"] = args.step
    rows, _ = run(spec)
    ps.setup()
    paths = [os.path.join(out, "p1_results.csv")]
    write_csv(rows, paths[0])
    paths.append(lines(rows, spec, os.path.join(out, "p1_lines.png")))
    paths.append(bars(rows, spec, os.path.join(out, "p1_bars.png")))

    tg = int(spec["target"]["writer_group_id"])
    vs = sorted({r["v"] for r in rows})
    at = {(r["v"], r["wg"]): r for r in rows}
    print(f"P1 {spec['name']}: WG {tg} from {vs[0]} to {vs[-1]} variables "
          f"(NM {at[(vs[0], tg)]['target_nm_bytes']} -> {at[(vs[-1], tg)]['target_nm_bytes']} bytes)")
    print(f"  {'WG':>4} {'role':>8} {'T':>7} {'R first':>10} {'R last':>10} {'change':>9}  status at the end")
    for g in sorted({r["wg"] for r in rows}, key=lambda g: (["higher", "target", "lower"].index(at[(vs[0], g)]["role"]), g)):
        a, b = at[(vs[0], g)], at[(vs[-1], g)]
        ch = "" if a["R"] is None or b["R"] is None else f"{b['R'] - a['R']:+.3f}"
        print(f"  {g:>4} {a['role']:>8} {a['T']:>7g} {a['R'] or '-':>10} {b['R'] or '-':>10} {ch:>9}  {b['status']}")
    print(f"  W of WG {tg}: {at[(vs[0], tg)]['W']:.3f} -> {at[(vs[-1], tg)]['W']:.3f} us")
    for p in paths:
        print("wrote", p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
