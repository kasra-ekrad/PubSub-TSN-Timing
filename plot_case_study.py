"""Plots and a table for one analysed system (scenario 1, the case study).

    python run_all.py case_study.yaml --system case_study --exp-id 1
    python plot_case_study.py Experiment1

Reads Experiment<ID>/results.json and writes into the same folder:
    case_study_table.csv       per WG: class, T, R^p, R^max, J', R^s, R, R/T, statuses
    case_study_breakdown.png   per WG, R split into publisher / network / jitter / subscriber
    case_study_links.png       utilization of every link that carries traffic, by class
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys

import plot_style as ps
from plot_style import plt

LINK_CLASSES = [("ST", "ST"), ("AVB_A", "AVB class A"), ("AVB_B", "AVB class B"), ("BE", "Best effort")]


def parts(e: dict) -> dict:
    J = e.get("J") or 0
    return {"part_publisher": e["R_p"], "part_network": e["R_max"] - J,
            "part_jitter": J, "part_subscriber": e["R_s"] - J}


def table(res: dict, folder: str) -> str:
    path = os.path.join(folder, "case_study_table.csv")
    T = {int(r["writer_group_id"]): r["T"] for rows in res["publishers"].values() for r in rows}
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["wg", "class", "T_us", "R_p", "R_max", "J", "R_s", "R", "R_over_T",
                    "publisher", "network", "jitter", "subscriber", "pub_status", "net_status", "sub_status"])
        for e in sorted(res["e2e"], key=lambda e: e["writer_group_id"]):
            t = T[e["writer_group_id"]]
            p = parts(e) if e["R"] is not None else {}
            w.writerow([e["writer_group_id"], e["traffic_class"], t, e["R_p"], e["R_max"], e.get("J"),
                        e["R_s"], e["R"], round(e["R"] / t, 4) if e["R"] is not None else ""]
                       + [round(p[k], 3) if p else "" for k, _ in ps.PARTS]
                       + [e["pub_status"], e["net_status"], e["sub_status"]])
    return path


def breakdown(res: dict, folder: str) -> str:
    T = {int(r["writer_group_id"]): r["T"] for rows in res["publishers"].values() for r in rows}
    rows = [e for e in sorted(res["e2e"], key=lambda e: e["writer_group_id"]) if e["R"] is not None]
    fig, ax = plt.subplots(figsize=(6.6, 0.42 * len(rows) + 1.2))
    y = list(range(len(rows)))[::-1]
    left = [0.0] * len(rows)
    for (key, name), color in zip(ps.PARTS, ps.SLOT):
        widths = [parts(e)[key] for e in rows]
        ax.barh(y, widths, left=left, height=0.62, color=color, edgecolor=ps.SURFACE, linewidth=1, label=name)
        left = [a + b for a, b in zip(left, widths)]
    for yi, e, total in zip(y, rows, left):
        ax.text(total, yi, f"  {total:.1f} µs", va="center", fontsize=7, color=ps.TEXT_2)
    ax.set_yticks(y, [f"WG {e['writer_group_id']} · {e['traffic_class']} · T {T[e['writer_group_id']]:g}"
                      for e in rows])
    ax.set_xlabel("End-to-end delay R (µs)")
    ax.set_xlim(0, max(left) * 1.18)
    ax.grid(axis="y", visible=False)
    ax.set_title("End-to-end delay per WG, by part", loc="left")
    ax.legend(loc="upper center", bbox_to_anchor=(0.45, -0.18 - 0.5 / len(rows)), ncols=2)
    return ps.save(fig, os.path.join(folder, "case_study_breakdown.png"))


def links(res: dict, folder: str) -> str:
    rows = [l for l in res.get("link_utilization", []) if l["utilization"] > 0]
    fig, ax = plt.subplots(figsize=(6.6, 0.38 * len(rows) + 1.2))
    y = list(range(len(rows)))[::-1]
    left = [0.0] * len(rows)
    for (key, name), color in zip(LINK_CLASSES, ps.SLOT):
        widths = [100 * (l.get(f"{key}_utilization") or 0) for l in rows]
        ax.barh(y, widths, left=left, height=0.62, color=color, edgecolor=ps.SURFACE, linewidth=1, label=name)
        left = [a + b for a, b in zip(left, widths)]
    for yi, total in zip(y, left):
        ax.text(total, yi, f"  {total:.1f}%", va="center", fontsize=7, color=ps.TEXT_2)
    ax.set_yticks(y, [f"link {l['link_id']}: {l['from']} → {l['to']}" for l in rows])
    ax.set_xlabel("Utilization (% of the link)")
    ax.set_xlim(0, max(left) * 1.2 if left else 1)
    ax.grid(axis="y", visible=False)
    ax.set_title("Link utilization (links with traffic), by class", loc="left")
    ax.legend(loc="upper center", bbox_to_anchor=(0.45, -0.2 - 0.5 / max(len(rows), 1)), ncols=4)
    return ps.save(fig, os.path.join(folder, "case_study_links.png"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Plot one analysed system")
    ap.add_argument("experiment", help="experiment folder (with results.json), e.g. Experiment1")
    args = ap.parse_args(argv)
    path = args.experiment if args.experiment.endswith(".json") else os.path.join(args.experiment, "results.json")
    folder = os.path.dirname(os.path.abspath(path))
    with open(path, encoding="utf-8") as f:
        res = json.load(f)
    ps.setup()
    for out in (table(res, folder), breakdown(res, folder), links(res, folder)):
        print("wrote", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
