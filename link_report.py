"""Network utilization per link.

For every directed link (row of `architecture`, numbered as in STHS_input.txt,
Schedule.txt and AVB_Input.txt), the messages whose path uses it and

    utilization = sum over those messages of (length / period) / bandwidth

with length in bytes as given to the network tools (frame incl. preamble/SFD and
IFG when preamble_ifg is true), period in us (best-effort flows: period or
min_inter_arr_time) and bandwidth in bytes/us. Split by ST, AVB_A, AVB_B and BE.

Writes, into the experiment folder:
    link_utilization.csv            one row per link
    link_utilization_messages.csv   one row per (link, message)

Usage on its own:
    python link_report.py example_system.yaml --system ex1 [--out FOLDER]
(run_all.py writes it at the end of every run.)
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from fractions import Fraction

from system import System, load_systems

CLASSES = ("ST", "AVB_A", "AVB_B", "BE")


def flows(system: System) -> list[dict]:
    """Every message on the network with its path, length and period."""
    wgs = system.writer_groups()
    out = [{"kind": m.traffic_class, "id": str(g), "length": m.length, "period": wgs[g][1].T,
            "path": m.path} for g, m in sorted(system.network.items())]
    out += [{"kind": "BE", "id": b.name, "length": b.length,
             "period": b.period + b.min_inter_arr_time, "path": b.path} for b in system.best_effort]
    return out


def link_utilization(system: System) -> list[dict]:
    """One entry per row of `architecture`, in row order."""
    cfg = system.network_config
    if cfg is None:
        return []
    links = [{"link_id": i, "from": a, "to": b, "messages": [],
              "bytes_per_us": Fraction(0), **{c: Fraction(0) for c in CLASSES}}
             for i, (a, b) in enumerate(cfg.architecture, start=1)]
    row = {(l["from"], l["to"]): l for l in links}
    for f in flows(system):
        rate = Fraction(f["length"]) / f["period"]            # bytes/us
        for hop in zip(f["path"], f["path"][1:]):
            l = row[hop]
            l["messages"].append({**f, "rate": rate})
            l["bytes_per_us"] += rate
            l[f["kind"]] += rate
    for l in links:
        l["utilization"] = l["bytes_per_us"] / cfg.bandwidth
        for c in CLASSES:
            l[c + "_util"] = l[c] / cfg.bandwidth
    return links


def _pct(x: Fraction) -> float:
    return round(float(100 * x), 4)


def write_report(system: System, folder: str) -> list[dict]:
    links = link_utilization(system)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "link_utilization.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["link_id", "from", "to", "messages", "bytes_per_us", "bandwidth_bytes_per_us"]
                   + [f"{c}_percent" for c in CLASSES] + ["total_percent"])
        for l in links:
            w.writerow([l["link_id"], l["from"], l["to"], len(l["messages"]),
                        round(float(l["bytes_per_us"]), 6), float(system.network_config.bandwidth)]
                       + [_pct(l[c + "_util"]) for c in CLASSES] + [_pct(l["utilization"])])
    with open(os.path.join(folder, "link_utilization_messages.csv"), "w", newline="",
              encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["link_id", "from", "to", "class", "writer_group_id_or_name", "length_bytes",
                    "period_us", "bytes_per_us", "percent_of_link"])
        for l in links:
            for m in l["messages"]:
                w.writerow([l["link_id"], l["from"], l["to"], m["kind"], m["id"], m["length"],
                            float(m["period"]), round(float(m["rate"]), 6),
                            _pct(m["rate"] / system.network_config.bandwidth)])
    return links


def print_report(links: list[dict], bandwidth) -> None:
    if not links:
        return
    print(f"\n  Link utilization  (bandwidth {float(bandwidth):g} bytes/us; % of the link)")
    print(f"    {'link':>4} {'from':>5} {'to':>5} {'msgs':>4} " + " ".join(f"{c:>7}" for c in CLASSES)
          + f" {'total':>8}")
    for l in links:
        print(f"    {l['link_id']:>4} {l['from']:>5} {l['to']:>5} {len(l['messages']):>4} "
              + " ".join(f"{_pct(l[c + '_util']):>7.3f}" for c in CLASSES)
              + f" {_pct(l['utilization']):>7.3f}%")
    busiest = max(links, key=lambda l: l["utilization"])
    print(f"    busiest: link {busiest['link_id']} ({busiest['from']} -> {busiest['to']}), "
          f"{_pct(busiest['utilization']):.3f}%")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Network utilization per link")
    ap.add_argument("file", help="YAML/JSON file with the systems")
    ap.add_argument("--system", required=True, help="name of the system")
    ap.add_argument("--out", default=".", help="folder for the CSV files (default: current folder)")
    args = ap.parse_args(argv)
    systems = {s.name: s for s in load_systems(args.file)}
    if args.system not in systems:
        ap.error(f"no system named {args.system!r}")
    s = systems[args.system]
    if s.network_config is None:
        ap.error(f"system {args.system!r} has no network section")
    print_report(write_report(s, args.out), s.network_config.bandwidth)
    return 0


if __name__ == "__main__":
    sys.exit(main())
