"""Python replacement for AVB_analysis_input_generator.m.

Reads, from Experiment<ID>/:
    TT_Messages.mat, AVB_Messages.mat, BE_Messages.mat   (from mapping_tool.py)
    Routes.mat, Architecture.mat                         (from network_generator.py)
    Schedule.txt                                         (from TSN_HeuristicScheduler.exe)
    message_map.csv                                      (AVB class A/B from the YAML)
and writes Experiment<ID>/AVB_Input.txt, plus AVB_Input_map.csv: the block number
used in the AVB analysis results (0-based position in AVB_Input.txt) -> WG.

ST part (only if TT messages exist and Schedule.txt exists), as STschedule_to_file:
    Schedule.txt columns used: 1 message index (0-based, order of STHS_input.txt),
    2 link (row of `architecture`), 4 start [ns], 5 end [ns].
    For TT message i and each link j on its path:
        P      = period + min_inter_arr_time
        n_j    = ceil((largest end on link j / 1000) / P)   (instances in the schedule)
        offset = start times of message i on link j, in file order
        one block per instance k = 1..n_j:
            message / length / period = deadline = P*n_j / priority = ST /
            linkNbr = 1 / link = j / jitterIn = 0 / initOffset = 0 /
            offset = offset(k)/1000 (%f)
AVB part, as struct_to_file:
    message / length / period = deadline = period + min_inter_arr_time /
    priority = ClassA or ClassB / linkNbr / link per hop / jitterIn / initOffset = 0
    The class is assigned here, with the original rule: odd positions ClassA,
    even positions ClassB (avb_priorities). The AVB analysis uses it as given, and
    AVB_Input_map.csv records it per block.
BE part (commented out in the original): only with --with-be, as struct_to_file
with priority NRT.

Usage:
    python avb_input_generator.py --exp-id 1 [--dir DIR] [--with-be]
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import re
import sys

import numpy as np
from scipy.io import loadmat

from mapping_tool import (FIELDS, MappingError, _i, _value, link_ids, load_architecture,
                          load_routes, route_path)


def load_group(path: str, var: str) -> list[dict]:
    """A struct array saved by the mapping tool, or [] where it saved the scalar 0."""
    arr = loadmat(path)[var]
    if arr.dtype.names is None:
        return []
    return [{f: _value(arr[0, i][f]) for f in FIELDS} for i in range(arr.shape[1])]


# ----------------------------------------------------------------- schedule

_SPLIT = re.compile(r"[,;\s]+")


def read_schedule(path: str) -> np.ndarray:
    """Numeric rows of Schedule.txt (at least 5 columns)."""
    return read_numeric(path, 5)


def read_numeric(path: str, min_cols: int) -> np.ndarray:
    """Numeric rows of a tool output file, like readmatrix: delimiter detected (comma,
    semicolon, tab or space), lines that are not numbers (headers) skipped."""
    rows = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            tokens = [t for t in _SPLIT.split(line.strip()) if t]
            if not tokens:
                continue
            try:
                rows.append([float(t) for t in tokens])
            except ValueError:
                continue
    if not rows:
        raise MappingError(f"{path}: no numeric rows")
    width = max(len(r) for r in rows)
    if width < min_cols:
        raise MappingError(f"{path}: expected at least {min_cols} columns, found {width}")
    return np.array([r + [math.nan] * (width - len(r)) for r in rows])


# ------------------------------------------------------------------- blocks

def st_blocks(tt: list[dict], route, architecture, schedule: np.ndarray) -> tuple[str, list[dict]]:
    """Text of the ST blocks and, per block, which TT message, link and instance it is."""
    out, info = [], []
    for i, m in enumerate(tt, start=1):
        P = m["period"] + m["min_inter_arr_time"]
        links = link_ids(route_path(route, m), architecture)
        instances = []
        for link in links:
            ends = schedule[schedule[:, 1] == link, 4]
            if ends.size == 0:
                raise MappingError(f"Schedule.txt has no entry for link {link} (TT message {i})")
            instances.append(math.ceil((ends.max() / 1000) / P))
        for link, n in zip(links, instances):
            offsets = schedule[(schedule[:, 1] == link) & (schedule[:, 0] == i - 1), 3]
            if offsets.size < n:
                raise MappingError(f"Schedule.txt has {offsets.size} start times for TT message {i} "
                                   f"on link {link}, {n} needed")
            for k in range(n):
                out.append("\n".join([
                    "message",
                    f"length = {_i(m['length'], 'length')}",
                    f"period = {_i(P * n, 'period')}",
                    f"deadline = {_i(P * n, 'deadline')}",
                    "priority = ST",
                    "linkNbr = 1",
                    f"link = {link}",
                    "jitterIn = 0",
                    "initOffset = 0",
                    f"offset = {offsets[k] / 1000:f}",
                ]) + "\n\n")
                info.append({"kind": "ST", "group_index": i - 1, "priority": "ST",
                             "link": link, "instance": k})
    return "".join(out), info


def struct_blocks(msgs: list[dict], priorities: list[str], route, architecture) -> str:
    out = []
    for m, prio in zip(msgs, priorities):
        P = m["period"] + m["min_inter_arr_time"]
        path = route_path(route, m)
        lines = ["message",
                 f"length = {_i(m['length'], 'length')}",
                 f"period = {_i(P, 'period')}",
                 f"deadline = {_i(P, 'deadline')}",
                 f"priority = {prio}",
                 f"linkNbr = {len(path) - 1}"]
        lines += [f"link = {x}" for x in link_ids(path, architecture)]
        lines += [f"jitterIn = {_i(m['jitter_transmission'], 'jitterIn')}", "initOffset = 0"]
        out.append("\n".join(lines) + "\n\n")
    return "".join(out)


def avb_priorities(n: int) -> list[str]:
    """The AVB class of each AVB message, in the order of AVB_Messages.mat. This is the
    rule of the original AVB_analysis_input_generator.m: odd positions ClassA, even
    positions ClassB. Change it here to use another rule."""
    return ["ClassB" if i % 2 == 0 else "ClassA" for i in range(1, n + 1)]


def run(exp_id: int, base_dir: str = ".", with_be: bool = False) -> dict[str, int]:
    folder = os.path.join(base_dir, f"Experiment{exp_id}")
    tt = load_group(os.path.join(folder, "TT_Messages.mat"), "message_TT")
    avb = load_group(os.path.join(folder, "AVB_Messages.mat"), "message_AVB")
    be = load_group(os.path.join(folder, "BE_Messages.mat"), "message_BE")
    route = load_routes(os.path.join(folder, "Routes.mat"))
    architecture = load_architecture(os.path.join(folder, "Architecture.mat"))

    text, counts = "", {"ST blocks": 0, "AVB": len(avb), "BE": 0}
    blocks: list[dict] = []
    schedule_path = os.path.join(folder, "Schedule.txt")
    if tt and os.path.isfile(schedule_path):
        st, info = st_blocks(tt, route, architecture, read_schedule(schedule_path))
        counts["ST blocks"] = len(info)
        text += st
        blocks += info
    elif tt:
        print(f"warning: {schedule_path} not found; AVB_Input.txt has no ST traffic", file=sys.stderr)
    if avb:
        prios = avb_priorities(len(avb))
        text += struct_blocks(avb, prios, route, architecture)
        blocks += [{"kind": "AVB", "group_index": j, "priority": p} for j, p in enumerate(prios)]
    if with_be and be:
        text += struct_blocks(be, ["NRT"] * len(be), route, architecture)
        blocks += [{"kind": "BE", "group_index": j, "priority": "NRT"} for j in range(len(be))]
        counts["BE"] = len(be)

    with open(os.path.join(folder, "AVB_Input.txt"), "w", encoding="ascii") as f:
        f.write(text)
    write_block_map(folder, blocks)
    counts["blocks"] = blocks
    return counts


def write_block_map(folder: str, blocks: list[dict]) -> None:
    """AVB_Input_map.csv: block number as used by the AVB analysis (0-based position in
    AVB_Input.txt) -> kind and WriterGroupId / best-effort name (from message_map.csv)."""
    ids: dict[str, list[tuple[str, str]]] = {"ST": [], "AVB": [], "BE": []}
    path = os.path.join(folder, "message_map.csv")
    if os.path.isfile(path):
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                kind = {"TT": "ST", "BE": "BE"}.get(r["class"], "AVB")
                ids[kind].append((r["writer_group_id"], r["name"]))
    with open(os.path.join(folder, "AVB_Input_map.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["block_id", "kind", "writer_group_id", "name", "priority", "link", "instance"])
        for b_id, b in enumerate(blocks):
            group = ids[b["kind"]]
            wgid, name = group[b["group_index"]] if b["group_index"] < len(group) else ("", "placeholder")
            b.update(writer_group_id=wgid, name=name)
            w.writerow([b_id, b["kind"], wgid, name, b["priority"], b.get("link", ""), b.get("instance", "")])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Write AVB_Input.txt for the AVB analysis")
    ap.add_argument("--exp-id", type=int, required=True, help="experiment number (folder Experiment<ID>)")
    ap.add_argument("--dir", default=".", help="directory that contains Experiment<ID>")
    ap.add_argument("--with-be", action="store_true",
                    help="also write the best-effort messages (priority NRT)")
    args = ap.parse_args(argv)
    c = run(args.exp_id, args.dir, args.with_be)
    print(f"wrote Experiment{args.exp_id}/AVB_Input.txt: {c['ST blocks']} ST blocks, "
          f"{c['AVB']} AVB, {c['BE']} BE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
