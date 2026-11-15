"""Comparison with Denzler et al.: their upper bound, our bound and their measurement, end to end,
split into publisher, network and subscriber. One WG, no contention, point-to-point.

    python denzler.py experiments/denzler.yaml [--out results/denzler] [--tools-dir .] [--runner ...]
    python denzler.py experiments/denzler.yaml --plot-only

(1) static2021: one Int32 value, no VLAN tag (Static Timing Analysis of OPC UA PubSub, 2021)
(2) sizes2022:  1 to 4 uint32 values, VLAN tag (Timing Analysis of TSN-Enabled OPC UA PubSub, 2022)
Each case is one run_all.py run (both exes) of a system with the paper's configuration (link,
data type, number of values, period, VLAN tag, UADP payload header; one WG, AVB, no gates),
and WCETs from YOUR model: the `wcet:` section of your scenario (wcet_model in the spec),
read at the midpoints of its ranges. The minimum and maximum of the ranges give a range
for our bound (the network results are reused: they do not depend on the WCETs).

Our bound, split as in the analysis: R = R^p + R^max + (R^s - J'), here W + R^N + W'
(no contention), with W and W' from your WCET model.
Their bound: PublishCallback + network + SubscribeCallback, where the network part has no
bound in the papers ("HW-dependent") and is filled with their measured value.

Writes into --out (default results/denzler):
    denzler_results.csv      per case: frame, our R^p, R^min, R^N, J', R^s, R and their values
    denzler_static2021.png   three stacked bars: their bound, our bound, their measurement
    denzler_sizes.png        the same per message size (their measurement: medians)
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import json
import os
import sys

import yaml

from fractions import Fraction

import generate_system as gen
import plot_style as ps
import run_all
import uadp
from network_results import apply_network_results
from plot_style import plt
from subscriber_rta import analyse_all
from system import _parse_system

PARTS = [("pub", "Publisher"), ("net", "Network"), ("sub", "Subscriber")]
COLOR = {"pub": ps.SLOT[0], "net": ps.SLOT[1], "sub": ps.SLOT[2]}
SOURCES = [("bound", "Upper bound [Denzler]\n(WCET analysis)", "///"), ("ours", "Our bound\n(our WCET model)", None),
           ("measured", "Measured [Denzler]", "...")]


# --------------------------------------------------------------------- model

def wcet_model(sp: dict) -> dict:
    """The WCET model of your scenario (wcet: publisher / subscriber), path relative to the spec."""
    with open(sp["_wcet_model_path"], encoding="utf-8") as f:
        return yaml.safe_load(f)["wcet"]


def _v(x, pick: str) -> Fraction:
    """A model value: a number, or a [min, max] range read at min, mid or max."""
    if isinstance(x, list):
        lo, hi = Fraction(str(x[0])), Fraction(str(x[1]))
        return {"min": lo, "mid": (lo + hi) / 2, "max": hi}[pick]
    return Fraction(str(x))


def _per_type(spec: dict, typ: str, pick: str) -> Fraction:
    if "per_type" in spec:
        return Fraction(str(spec["per_type"][typ]))
    return _v(spec["base"], pick) + _v(spec["per_byte"], pick) * uadp.field_size(typ)


def build(sp: dict, study: dict, v: int, model: dict, pick: str = "mid") -> dict:
    """The system of one case: Denzler's configuration, WCETs from your model (read at `pick`)."""
    typ, pid, ph = study["type"], "UInt16", bool(sp.get("payload_header", True))
    nm = uadp.nm_size([typ] * v, 1, pid, ph)
    P, S = model["publisher"], model["subscriber"]
    s = lambda x: str(x)
    wg = {"n": 1, "writer_group_id": 1, "T": study["T"], "rho": 1, "types": [typ] * v,
          "psi": [s(_per_type(P["collect"], typ, pick))] * v,
          "phi": [s(_v(P["dsw"]["base"], pick) + _v(P["dsw"]["per_field"], pick) * v)],
          "C3": s(_v(P["wg"]["base"], pick) + _v(P["wg"]["per_dsm"], pick)),
          "C4": s(_v(P["transport"]["base"], pick) + _v(P["transport"]["per_byte"], pick) * nm)}
    tx = {"m": 1, "writer_group_id": 1,
          "C1": s(_v(S["reception"]["base"], pick) + _v(S["reception"]["per_byte"], pick) * nm),
          "C2": s(_v(S["filter"]["base"], pick)),
          "phi": [s(_v(S["decode"]["base"], pick) + _v(S["decode"]["per_field"], pick) * v)],
          "psi": [s(_per_type(S["dispatch"], typ, pick))] * v}
    net = dict(sp["network"])
    net.update(full_duplex=True, publisher_id_type=pid, vlan_tag=bool(study["vlan_tag"]), preamble_ifg=True,
               payload_header=ph, messages=[{"writer_group_id": 1, "hard_real_time": False}])
    return {"name": sp["name"], "publishers": [{"p": 1, "node": 1, "writer_groups": [wg]}],
            "subscribers": [{"k": 1, "node": 2, "transactions": [tx]}], "network": net}


def cases(sp: dict) -> list[dict]:
    a, b = sp["static2021"], sp["sizes2022"]
    out = [{"case": "static2021", "study": "static2021", "values": int(a["values"]),
            "bound": dict(a["bound"]), "measured": dict(a["measured"])}]
    for v in b["values"]:
        m = b["measured"]
        out.append({"case": f"sizes2022_v{v}", "study": "sizes2022", "values": int(v),
                    "bound": {"publish": b["bound_publish"][v], "network": m["network"][v],
                              "subscribe": b["bound_subscribe"]},
                    "measured": {"publish": m["publish"][v], "network": m["network"][v],
                                 "subscribe": m["subscribe"][v], "e2e": m["e2e"][v]}})
    for i, c in enumerate(out, start=1):
        c["exp_id"] = i
    return out


# ------------------------------------------------------------------ running

def run_case(sp: dict, c: dict, out: str, args) -> str:
    path = os.path.join(out, "systems", f"{c['case']}.yaml")
    gen.write_yaml(build(sp, sp[c["study"]], c["values"], wcet_model(sp)), path,
                   [f"Denzler et al. configuration ({c['case']}); WCETs: your model at the midpoints"])
    argv = [path, "--system", sp["name"], "--exp-id", str(c["exp_id"]), "--work-dir", out,
            "--tools-dir", args.tools_dir, "--num-queues", str(args.num_queues), "--folder-arg", args.folder_arg]
    if args.runner is not None:
        argv += ["--runner", args.runner]
    with open(os.path.join(out, "logs", f"{c['case']}.log"), "w", encoding="utf-8") as f, \
            contextlib.redirect_stdout(f), contextlib.redirect_stderr(f):
        try:
            run_all.main(argv)
        except BaseException as e:                       # SystemExit included
            return f"failed: {e}"
    return "ok" if os.path.isfile(os.path.join(out, f"Experiment{c['exp_id']}", "results.json")) else "failed"


def _analyse(sp: dict, c: dict, folder: str, pick: str):
    """Your analysis of the case with the WCET model read at `pick`, network results of the run."""
    d = build(sp, sp[c["study"]], c["values"], wcet_model(sp), pick)
    system = _parse_system(d, sp["name"])
    system = apply_network_results(system, folder, None, os.path.join(folder, "final_new_results.txt"))
    return analyse_all(system)


def collect(sp: dict, out: str) -> list[dict]:
    rows = []
    for c in cases(sp):
        folder = os.path.join(out, f"Experiment{c['exp_id']}")
        if not os.path.isfile(os.path.join(folder, "results.json")):
            continue
        with open(os.path.join(folder, "results.json"), encoding="utf-8") as f:
            res = json.load(f)
        n = res["network"]["1"]
        b, m = c["bound"], c["measured"]
        row = {"case": c["case"], "study": c["study"], "values": c["values"],
               "bits": 32 * c["values"], "frame_bytes": n["frame_bytes"], "wire_bytes": n["length"]}
        for pick in ("mid", "min", "max"):
            pub, sub, e2e = _analyse(sp, c, folder, pick)
            e, pw, sw = e2e[0], pub[1][0], sub[1][0]
            tag = "" if pick == "mid" else f"_{pick}"
            row.update({f"ours_W{tag}": float(pw.W), f"ours_Ws{tag}": float(sw.W), f"ours_pub{tag}": float(e.R_p),
                        f"ours_net{tag}": float(e.R_max), f"ours_sub{tag}": float(e.R_s - e.J),
                        f"ours_e2e{tag}": float(e.R)})
            if pick == "mid":
                row.update(R_min=float(n.get("R_min")), J=float(e.J), R_s=float(e.R_s))
        row.update({"bound_pub": b["publish"], "bound_net": b["network"], "bound_sub": b["subscribe"],
                    "bound_e2e": b["publish"] + b["network"] + b["subscribe"],
                    "measured_pub": m["publish"], "measured_net": m["network"], "measured_sub": m["subscribe"],
                    "measured_sum": m["publish"] + m["network"] + m["subscribe"], "measured_e2e": m["e2e"]})
        rows.append(row)
    return rows


def read_rows(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for k, v in r.items():
            if k not in ("case", "study"):
                r[k] = float(v) if v not in ("", None) else None
    return rows


def write_rows(rows: list[dict], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


# --------------------------------------------------------------------- plots

def _stack(ax, x, r, src, hatch, width, label_parts=False):
    bottom = 0.0
    for key, name in PARTS:
        h = r[f"{src}_{key}"]
        ax.bar(x, h, bottom=bottom, width=width, color=COLOR[key], edgecolor=ps.SURFACE, linewidth=0.8,
               hatch=hatch, label=name if label_parts else None)
        bottom += h
    if src == "ours" and r.get("ours_e2e_min") is not None:     # range of your WCET model
        ax.errorbar(x, bottom, yerr=[[bottom - r["ours_e2e_min"]], [r["ours_e2e_max"] - bottom]],
                    color=ps.TEXT, capsize=3, linewidth=1, zorder=5)
    return bottom


def static2021(rows: list[dict], path: str) -> str:
    r = next(x for x in rows if x["study"] == "static2021")
    fig, ax = plt.subplots(figsize=(6.4, 3.8))
    plt.rcParams["hatch.color"] = ps.SURFACE
    top = 0
    for i, (src, name, hatch) in enumerate(SOURCES):
        total = _stack(ax, i, r, src, hatch, 0.5, label_parts=(i == 1))
        top = max(top, total)
        parts = "\n".join(f"{n}: {r[f'{src}_{k}']:.2f}" for k, n in PARTS)
        rng = (f"\n(model range {r['ours_e2e_min']:,.2f}–{r['ours_e2e_max']:,.2f})" if src == "ours" else "")
        ax.text(i + 0.28, total, f"{total:,.2f} µs{rng}\n" + parts, fontsize=7, color=ps.TEXT, va="bottom")
    m = r["measured_e2e"]
    ax.plot([1.72, 2.28], [m, m], color=ps.TEXT, linewidth=1.4)
    ax.annotate(f"measured end-to-end max: {m:.2f}\n(the parts do not peak together)", (1.8, m),
                xytext=(-30, 40), textcoords="offset points", ha="right", fontsize=7, color=ps.TEXT,
                arrowprops=dict(arrowstyle="-", color=ps.TEXT_2, linewidth=0.6))
    ax.set_xticks(range(3), [n for _, n, _ in SOURCES])
    ax.set_xlim(-0.5, 2.95)
    ax.set_ylim(0, top * 1.3)
    ax.set_ylabel("end-to-end (µs)")
    ax.grid(axis="x", visible=False)
    ax.set_title("One Int32 value, point-to-point, no contention [Denzler et al. 2021]", loc="left", fontsize=9)
    ax.legend(loc="upper center", ncols=3, fontsize=7, bbox_to_anchor=(0.5, -0.1))
    return ps.save(fig, path)


def sizes(rows: list[dict], path: str) -> str | None:
    rs = sorted((x for x in rows if x["study"] == "sizes2022"), key=lambda x: x["values"])
    if not rs:
        return None
    fig, ax = plt.subplots(figsize=(7.2, 3.8))
    plt.rcParams["hatch.color"] = ps.SURFACE
    w = 0.26
    for g, r in enumerate(rs):
        for i, (src, name, hatch) in enumerate(SOURCES):
            x = g + (i - 1) * (w + 0.02)
            total = _stack(ax, x, r, src, hatch, w, label_parts=(g == 0 and i == 1))
            ax.text(x, total, f"{total:,.0f}", ha="center", va="bottom", fontsize=6.5, color=ps.TEXT_2)
        m = r["measured_e2e"]
        x = g + (w + 0.02)
        ax.plot([x - w / 2, x + w / 2], [m, m], color=ps.TEXT, linewidth=1.2)
    for _, name, hatch in SOURCES:                      # legend entries for the three sources
        ax.bar(0, 0, color=ps.TEXT_2, hatch=hatch, edgecolor=ps.SURFACE, label=name.replace("\n", " "))
    ax.plot([], [], color=ps.TEXT, linewidth=1.2, label="measured end-to-end (median)")
    ax.set_xticks(range(len(rs)), [f"{int(r['bits'])} bit\n(frame {int(r['frame_bytes'])} B)" for r in rs])
    ax.set_ylabel("end-to-end (µs)")
    ax.grid(axis="x", visible=False)
    ax.set_title("Message size, point-to-point, no contention [Denzler et al. 2022; measured: medians]",
                 loc="left", fontsize=9)
    ax.legend(loc="upper center", ncols=4, fontsize=7, bbox_to_anchor=(0.5, -0.17))
    return ps.save(fig, path)


# --------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Comparison with Denzler et al.")
    ap.add_argument("spec", help="experiments/denzler.yaml")
    ap.add_argument("--out", help="output folder (default results/<name>)")
    ap.add_argument("--tools-dir", default=".", help="folder with the two exes (default: current folder)")
    ap.add_argument("--num-queues", type=int, default=4)
    ap.add_argument("--folder-arg", default="Experiment{id}\\")
    ap.add_argument("--runner", default=None, help="see run_all.py (default: none on Windows, wine elsewhere)")
    ap.add_argument("--plot-only", action="store_true", help="only redraw from denzler_results.csv")
    args = ap.parse_args(argv)
    args.tools_dir = os.path.abspath(args.tools_dir)
    with open(args.spec, encoding="utf-8") as f:
        sp = yaml.safe_load(f)
    sp["_wcet_model_path"] = os.path.join(os.path.dirname(os.path.abspath(args.spec)),
                                          sp.get("wcet_model", "../scenario.yaml"))
    out = os.path.abspath(args.out or os.path.join("results", sp["name"]))
    csv_path = os.path.join(out, "denzler_results.csv")
    if not args.plot_only:
        for sub in ("systems", "logs"):
            os.makedirs(os.path.join(out, sub), exist_ok=True)
        for c in cases(sp):
            print(f"  {c['case']}: {run_case(sp, c, out, args)}")
        rows = collect(sp, out)
        if not rows:
            raise SystemExit("no results (see the logs)")
        write_rows(rows, csv_path)
    rows = read_rows(csv_path)
    ps.setup()
    figs = [static2021(rows, os.path.join(out, "denzler_static2021.png")),
            sizes(rows, os.path.join(out, "denzler_sizes.png"))]

    ours_head = "our bound (R^p + R^N + W')"
    print(f"\n  {'case':>14} {'bits':>5} {'frame':>5}   {'their bound (P + N + S)':>30}   "
          f"{ours_head:>30}   {'measured (P + N + S | e2e)':>34}")
    for r in rows:
        print(f"  {r['case']:>14} {int(r['bits']):>5} {int(r['frame_bytes']):>5}   "
              f"{r['bound_pub']:7.2f}+{r['bound_net']:6.2f}+{r['bound_sub']:9.2f}={r['bound_e2e']:9.2f}   "
              f"{r['ours_pub']:7.2f}+{r['ours_net']:6.2f}+{r['ours_sub']:9.2f}={r['ours_e2e']:9.2f}   "
              f"{r['measured_pub']:7.2f}+{r['measured_net']:6.2f}+{r['measured_sub']:7.2f}={r['measured_sum']:8.2f} | "
              f"{r['measured_e2e']:8.2f}")
    for p in [csv_path] + [f for f in figs if f]:
        print("wrote", p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
