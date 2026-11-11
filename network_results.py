"""Read the network tools' outputs back into the system model (step 7 of the flow).

AVB, from final_new_results.txt (written by AVB_analysis.exe):
    columns: block ID (0-based position in AVB_Input.txt), priority (2 = class A,
    3 = class B), deadline [ps], worst-case response time [ps]
    -> R^max_g = WCRT, the worst-case network delay from the hand-off; the AVB class
       (A/B) as assigned in step 5 and used by the analysis;
       J' = R^max_g - R^min_g with R^min_g = h C_N + (h - 1) epsilon.
    The block ID is mapped to the WG with AVB_Input_map.csv.

ST, from Schedule.txt (written by TSN_HeuristicScheduler.exe):
    columns: message index (0-based, order of STHS_input.txt), link, queue,
    gate opening time [ns], gate closing time [ns]
    The scheduler repeats each link with its own cycle (hyperperiod of the ST messages
    on that link), so the number of windows of a message can differ per link. Each
    window covers one period of the message; a window opening at o belongs to the
    period starting at k T with k = floor(o / T). Relative to that period start:
        s = opening on the first link - k T     (transmission starts)
        a = closing on the last link  - k T     (arrival at the subscriber)
    -> A_max = max a, A_min = min a (over the last link's windows), J' = A_max - A_min,
       s_min = min s (over the first link's windows).
    The end-to-end delay of an ST message is A_max + R^s; it requires R^p <= s_min
    (the NM is handed over before its first gate opens).
"""
from __future__ import annotations

import csv
import math
import os
from dataclasses import replace
from fractions import Fraction

from avb_input_generator import read_numeric
from mapping_tool import MappingError, link_ids
from system import System, check_message

PS_PER_US = 10 ** 6
NS_PER_US = 10 ** 3
PRIORITY_CLASS = {2: "ClassA", 3: "ClassB"}


def _exact(x: float) -> Fraction:
    return Fraction(repr(float(x)))


def _csv(path: str) -> list[dict]:
    if not os.path.isfile(path):
        raise MappingError(f"{path} not found")
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def read_avb_results(folder: str, results_path: str) -> dict[int, dict]:
    """WriterGroupId -> {"R_max", "deadline", "priority"} [us]."""
    blocks = _csv(os.path.join(folder, "AVB_Input_map.csv"))
    out: dict[int, dict] = {}
    for row in read_numeric(results_path, 4):
        b_id, prio, deadline, wcrt = int(row[0]), int(row[1]), row[2], row[3]
        if not 0 <= b_id < len(blocks):
            raise MappingError(f"{results_path}: message ID {b_id} is not a block of AVB_Input.txt")
        b = blocks[b_id]
        if b["kind"] != "AVB":
            continue                                   # BE (NRT) results are not used
        if not b["writer_group_id"]:
            raise MappingError(f"AVB_Input_map.csv: block {b_id} has no WriterGroupId")
        expected = b["priority"]
        if PRIORITY_CLASS.get(prio) != expected:
            raise MappingError(f"{results_path}: message ID {b_id} (WG {b['writer_group_id']}) has "
                               f"priority {prio}, but AVB_Input.txt gives it {expected}")
        out[int(b["writer_group_id"])] = {"R_max": _exact(wcrt) / PS_PER_US,
                                          "deadline": _exact(deadline) / PS_PER_US,
                                          "avb_class": expected.removeprefix("Class")}
    return out


def st_window_stats(sched, i: int, links: list[int], T: Fraction) -> dict:
    """Arrival and start times of TT message i, relative to the start of its period [us].

    The scheduler repeats each link with its own cycle (the hyperperiod of the ST
    messages on that link), so a message can have a different number of windows on
    different links: every window covers one period of the message. A window opening
    at `o` belongs to the period starting at floor(o / T) * T.
        arrivals a = gate close on the LAST link  - floor(open / T) * T
        starts   s = gate open  on the FIRST link - floor(open / T) * T
    """
    def windows(link):
        rows = sched[(sched[:, 0] == i) & (sched[:, 1] == link)]
        return [(_exact(o) / NS_PER_US, _exact(c) / NS_PER_US) for o, c in rows[:, 3:5]]

    first, last = windows(links[0]), windows(links[-1])
    for name, w, link in (("first", first, links[0]), ("last", last, links[-1])):
        if not w:
            raise MappingError(f"Schedule.txt has no window for message {i} on its {name} link {link}")
    start = lambda o: math.floor(o / T) * T
    a = [c - start(o) for o, c in last]
    s = [o - start(o) for o, _ in first]
    return {"A_max": max(a), "A_min": min(a), "s_min": min(s), "instances": len(last)}


def read_st_arrivals(folder: str, schedule_path: str, system: System) -> dict[int, dict]:
    """WriterGroupId -> {"A_max", "A_min", "s_min", "instances"} [us], relative to the period start."""
    tt_rows = [r for r in _csv(os.path.join(folder, "message_map.csv")) if r["class"] == "TT"]
    sched = read_numeric(schedule_path, 5)
    arch = system.network_config.architecture
    wgs = system.writer_groups()
    out: dict[int, dict] = {}
    for i, r in enumerate(tt_rows):
        wgid = int(r["writer_group_id"])
        links = link_ids(list(system.network[wgid].path), arch)
        try:
            out[wgid] = st_window_stats(sched, i, links, wgs[wgid][1].T)
        except MappingError as e:
            raise MappingError(f"{schedule_path}: WG {wgid}: {e}") from None
    return out


def apply_network_results(system: System, folder: str, schedule_path: str | None,
                          results_path: str | None) -> System:
    """The system with R^max (AVB) and the ST arrival times taken from the tool outputs."""
    avb = read_avb_results(folder, results_path) if results_path else {}
    st = read_st_arrivals(folder, schedule_path, system) if schedule_path else {}
    wgs = system.writer_groups()
    network = {}
    missing = []
    for wgid, m in system.network.items():
        if m.hard_real_time:
            if wgid not in st:
                missing.append(f"ST WG {wgid} (Schedule.txt)")
                continue
            x = st[wgid]
            m = replace(m, st_arrival_max=x["A_max"], st_arrival_min=x["A_min"],
                        st_start_min=x["s_min"], st_instances=x["instances"])
        else:
            if wgid not in avb:
                missing.append(f"AVB WG {wgid} (final_new_results.txt)")
                continue
            m = replace(m, R_max=avb[wgid]["R_max"], network_deadline=avb[wgid]["deadline"],
                        avb_class=avb[wgid]["avb_class"])
        check_message(m, wgs[wgid][1].T, f"WG {wgid}")
        network[wgid] = m
    if missing:
        raise MappingError("no network result for " + ", ".join(missing))
    return replace(system, network=network)
