"""Python replacement for Mapping_tool.m.

Reads Experiment<ID>/Messages.mat, Routes.mat and Architecture.mat and writes,
into the same folder:
    TT_Messages.mat    `message_TT`  (struct array)
    AVB_Messages.mat   `message_AVB` (struct array, or 0 if there is none)
    BE_Messages.mat    `message_BE`  (struct array, or 0 if there is none)
    STHS_input.txt     input of the ST scheduler: one block per TT message

Classification, from the fields the generator sets from the YAML (the original
used jitter_reception and deadline instead):
    hard_real_time = true   -> TT (ST)
    deadline = 0            -> BE
    otherwise               -> AVB
As in the original, if there is no TT message a placeholder TT message is built
from the extremes of all messages (largest period and deadline, smallest length,
...), so the ST scheduler always gets an input. A warning is printed when this
happens.

STHS_input.txt block per TT message (same format as the original):
    message
    length = <bytes>
    period = <period + min_inter_arr_time>
    deadline = <deadline, or period if deadline is 0>
    linkNbr = <links on the path>
    link = <row of the link in `architecture`>      (one line per link)
    initOffset = <offset>
    <empty line>
The values are written as integers (%i); a non-integer value is an error.

Usage:
    python mapping_tool.py --exp-id 1 [--dir DIR]
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
from scipy.io import loadmat, savemat

FIELDS = ("period", "offset", "jitter_transmission", "jitter_reception",
          "min_inter_arr_time", "length", "deadline", "hard_real_time",
          "source", "destination", "path")


class MappingError(ValueError):
    pass


# ------------------------------------------------------------------- loading

def _value(x):
    """A MATLAB scalar field as a Python bool (logical) or float (double).

    scipy reads MATLAB logicals back as uint8, so uint8 is taken as logical too;
    the generator writes every other field as double.
    """
    a = np.asarray(x)
    if a.size != 1:
        raise MappingError(f"expected a scalar field, got shape {a.shape}")
    v = a.reshape(-1)[0]
    return bool(v) if a.dtype in (np.bool_, np.uint8) else float(v)


def load_messages(path: str) -> list[dict]:
    arr = loadmat(path)["message"]
    return [{f: _value(arr[0, i][f]) for f in FIELDS} for i in range(arr.shape[1])]


def load_routes(path: str):
    return loadmat(path)["route"]


def load_architecture(path: str) -> list[tuple[int, int]]:
    a = loadmat(path)["architecture"]
    return [(int(r[0]), int(r[1])) for r in a]


def route_path(route, m: dict) -> list[int]:
    """route{destination, source}{path}"""
    d, s, k = int(m["destination"]), int(m["source"]), int(m["path"])
    cell = route[d - 1, s - 1]
    if k < 1 or k > cell.shape[1]:
        raise MappingError(f"route{{{d},{s}}} has no path {k}")
    return [int(x) for x in np.asarray(cell[0, k - 1]).reshape(-1)]


# ------------------------------------------------------------ classification

def classify(m: dict) -> str:
    if m["hard_real_time"]:
        return "TT"
    if m["deadline"] == 0:
        return "BE"
    return "AVB"


def placeholder_tt(messages: list[dict]) -> dict:
    """Same rule as Mapping_tool.m when no TT message exists."""
    col = lambda f: [m[f] for m in messages]
    return {
        "period": max(col("period")),
        "offset": min(col("offset")),
        "jitter_transmission": min(col("jitter_transmission")),
        "jitter_reception": 1.0,
        "min_inter_arr_time": min(col("min_inter_arr_time")),
        "length": min(col("length")),
        "deadline": max(col("deadline")),
        "hard_real_time": 1.0,            # double 1, as in the original
        "source": min(col("source")),
        "destination": max(col("destination")),
        "path": min(col("path")),
    }


# ------------------------------------------------------------------- writing

def struct_array(messages: list[dict]):
    """1xN MATLAB struct array, or the scalar 0 when empty (as in the original)."""
    if not messages:
        return 0.0
    arr = np.empty((1, len(messages)), dtype=[(f, object) for f in FIELDS])
    for i, m in enumerate(messages):
        for f in FIELDS:
            v = m[f]
            arr[0, i][f] = np.array([[v]], dtype=bool if isinstance(v, bool) else float)
    return arr


def _i(value: float, what: str) -> int:
    if float(value) != int(value):
        raise MappingError(f"{what} = {value} is not an integer; STHS_input.txt takes integers "
                           f"(use whole microseconds for ST messages)")
    return int(value)


def link_ids(path: list[int], architecture: list[tuple[int, int]]) -> list[int]:
    """Row of each hop of the path in `architecture` (1-based), as find(...) in the original."""
    ids = []
    for a, b in zip(path, path[1:]):
        rows = [r for r, link in enumerate(architecture, start=1) if link == (a, b)]
        if len(rows) != 1:
            raise MappingError(f"link {a}->{b} appears {len(rows)} times in the architecture")
        ids.append(rows[0])
    return ids


def sths_block(m: dict, route, architecture: list[tuple[int, int]]) -> str:
    path = route_path(route, m)
    lines = ["message",
             f"length = {_i(m['length'], 'length')}",
             f"period = {_i(m['period'] + m['min_inter_arr_time'], 'period')}",
             f"deadline = {_i(m['deadline'] if m['deadline'] > 0 else m['period'], 'deadline')}",
             f"linkNbr = {len(path) - 1}"]
    lines += [f"link = {x}" for x in link_ids(path, architecture)]
    lines.append(f"initOffset = {_i(m['offset'], 'offset')}")
    return "\n".join(lines) + "\n\n"


def run(exp_id: int, base_dir: str = ".") -> dict[str, list[dict]]:
    folder = os.path.join(base_dir, f"Experiment{exp_id}")
    messages = load_messages(os.path.join(folder, "Messages.mat"))
    route = load_routes(os.path.join(folder, "Routes.mat"))
    architecture = load_architecture(os.path.join(folder, "Architecture.mat"))

    groups: dict[str, list[dict]] = {"TT": [], "AVB": [], "BE": []}
    for m in messages:
        groups[classify(m)].append(m)
    if not groups["TT"]:
        groups["TT"].append(placeholder_tt(messages))
        print("warning: no ST message; added the placeholder TT message of the original "
              "Mapping_tool to TT_Messages.mat and STHS_input.txt", file=sys.stderr)

    text = "".join(sths_block(m, route, architecture) for m in groups["TT"])
    with open(os.path.join(folder, "STHS_input.txt"), "w", encoding="ascii") as f:
        f.write(text)
    savemat(os.path.join(folder, "TT_Messages.mat"), {"message_TT": struct_array(groups["TT"])})
    savemat(os.path.join(folder, "AVB_Messages.mat"), {"message_AVB": struct_array(groups["AVB"])})
    savemat(os.path.join(folder, "BE_Messages.mat"), {"message_BE": struct_array(groups["BE"])})
    return groups


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Split the messages of an experiment into TT, AVB and BE "
                                             "and write the ST scheduler input")
    ap.add_argument("--exp-id", type=int, required=True, help="experiment number (folder Experiment<ID>)")
    ap.add_argument("--dir", default=".", help="directory that contains Experiment<ID>")
    args = ap.parse_args(argv)
    groups = run(args.exp_id, args.dir)
    print(f"mapped Experiment{args.exp_id}: {len(groups['TT'])} TT, {len(groups['AVB'])} AVB, "
          f"{len(groups['BE'])} BE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
