"""Python replacement for Network_generator.m, driven by the system YAML.

Writes, for one system, the same files as the MATLAB generator:
    Experiment<ID>/Messages.mat      1xN struct `message`
    Experiment<ID>/Routes.mat        num_nodes x num_nodes cell `route`, route{d,s}
    Experiment<ID>/Architecture.mat  `architecture` (each link twice if full duplex)
plus Experiment<ID>/message_map.csv, which maps the message index i in
`message(i)` to the WriterGroupId it comes from.

Nothing is random. Per message (one per received WG, ordered by WriterGroupId):
    period              T of the WG [us]
    offset              0
    jitter_transmission 0
    jitter_reception    0
    min_inter_arr_time  0 (always periodic)
    length              Ethernet frame size from the UADP NM size [bytes] (see uadp.py)
    deadline            T
    hard_real_time      from the YAML (true = ST, false = AVB; the AVB class A/B is
                        assigned later, in avb_input_generator.py)
    source/destination  end node of the publisher / subscriber
    path                index into route{destination, source}: the path given in
                        the YAML, else the shortest route (first on ties)
Best-effort flows from network.best_effort follow the WG messages, with their
own length, period or min_inter_arr_time, deadline 0 and hard_real_time false.
A link loaded above max_link_load is an error; nothing is changed or dropped.

Usage:
    python network_generator.py example_system.yaml --system ex1 --exp-id 1 [--out DIR] [--map]
(--map also runs mapping_tool.py on the new experiment.)
"""
from __future__ import annotations

import argparse
import csv
import os
import sys

import numpy as np
from scipy.io import savemat

import topology
from system import System, load_systems

MESSAGE_FIELDS = ("period", "offset", "jitter_transmission", "jitter_reception",
                  "min_inter_arr_time", "length", "deadline", "hard_real_time",
                  "source", "destination", "path")


def _scalar(x) -> np.ndarray:
    return np.array([[float(x)]])


def message_list(system: System) -> list[dict]:
    """WG messages ordered by WriterGroupId, then the best-effort flows in YAML order."""
    wgs = system.writer_groups()
    out = []
    for wgid in sorted(system.network):
        m = system.network[wgid]
        T = wgs[wgid][1].T
        out.append({
            "class": "TT" if m.hard_real_time else "AVB", "writer_group_id": wgid, "name": "",
            "period": T, "offset": 0, "jitter_transmission": 0, "jitter_reception": 0,
            "min_inter_arr_time": 0, "length": m.length, "deadline": T,
            "hard_real_time": m.hard_real_time,
            "source": m.source, "destination": m.destination, "path": m.route_index,
            "path_nodes": m.path, "nm_bytes": m.size.nm,
        })
    for b in system.best_effort:
        out.append({
            "class": "BE", "writer_group_id": "", "name": b.name,
            "period": b.period, "offset": 0, "jitter_transmission": 0, "jitter_reception": 0,
            "min_inter_arr_time": b.min_inter_arr_time, "length": b.length, "deadline": 0,
            "hard_real_time": False,
            "source": b.source, "destination": b.destination, "path": b.route_index,
            "path_nodes": b.path, "nm_bytes": "",
        })
    return out


def messages_struct(messages: list[dict]) -> np.ndarray:
    arr = np.empty((1, len(messages)), dtype=[(f, object) for f in MESSAGE_FIELDS])
    for i, msg in enumerate(messages):
        for f in MESSAGE_FIELDS:
            if f == "hard_real_time":
                arr[0, i][f] = np.array([[bool(msg[f])]])        # MATLAB logical
            else:
                arr[0, i][f] = _scalar(msg[f])
    return arr


def routes_cell(routes: dict, n: int) -> np.ndarray:
    """route{d,s}: 1xP cell of paths; a path is a column vector, s == d gives the scalar s."""
    cell = np.empty((n, n), dtype=object)
    for (d, s), paths in routes.items():
        c = np.empty((1, len(paths)), dtype=object)
        for i, p in enumerate(paths):
            c[0, i] = np.array([[float(s)]]) if s == d else np.array(p, dtype=float).reshape(-1, 1)
        cell[d - 1, s - 1] = c
    return cell


def architecture_matrix(system: System) -> np.ndarray:
    return np.array(system.network_config.architecture, dtype=float)


def write_experiment(system: System, exp_id: int, out_dir: str = ".") -> str:
    if system.network_config is None:
        raise ValueError(f"system {system.name!r} has no network section")
    if not system.network and not system.best_effort:
        raise ValueError(f"system {system.name!r} has no network messages")
    folder = os.path.join(out_dir, f"Experiment{exp_id}")
    os.makedirs(folder, exist_ok=True)

    arch = system.network_config.architecture
    n = topology.num_end_nodes(arch)
    messages = message_list(system)
    savemat(os.path.join(folder, "Messages.mat"), {"message": messages_struct(messages)})
    savemat(os.path.join(folder, "Routes.mat"), {"route": routes_cell(system.routes, n)})
    savemat(os.path.join(folder, "Architecture.mat"), {"architecture": architecture_matrix(system)})

    with open(os.path.join(folder, "message_map.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["message_index", "class", "writer_group_id", "name", "hard_real_time", "period_us",
                    "min_inter_arr_time_us", "deadline_us", "length_bytes", "nm_bytes",
                    "source", "destination", "path_index", "path_nodes"])
        for i, m in enumerate(messages, start=1):
            w.writerow([i, m["class"], m["writer_group_id"], m["name"], m["hard_real_time"],
                        float(m["period"]), float(m["min_inter_arr_time"]), float(m["deadline"]),
                        m["length"], m["nm_bytes"], m["source"], m["destination"], m["path"],
                        " ".join(str(x) for x in m["path_nodes"])])
    return folder


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate the network tool's MAT files from a system YAML")
    ap.add_argument("file", help="YAML/JSON file with one or more systems")
    ap.add_argument("--system", required=True, help="name of the system to export")
    ap.add_argument("--exp-id", type=int, required=True, help="experiment number (folder Experiment<ID>)")
    ap.add_argument("--out", default=".", help="directory in which Experiment<ID> is created")
    ap.add_argument("--map", action="store_true", help="also run the mapping tool on the result")
    args = ap.parse_args(argv)

    systems = {s.name: s for s in load_systems(args.file)}
    if args.system not in systems:
        ap.error(f"no system named {args.system!r}")
    s = systems[args.system]
    folder = write_experiment(s, args.exp_id, args.out)
    print(f"wrote {folder}: {len(s.network)} WG messages, {len(s.best_effort)} best-effort flows")
    if args.map:
        import mapping_tool
        mapping_tool.main(["--exp-id", str(args.exp_id), "--dir", args.out])
    return 0


if __name__ == "__main__":
    sys.exit(main())
