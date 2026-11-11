"""Run the whole flow for one system:

    1  publisher response times          (ST WGs before all other WGs)
    2  network generator                 Messages.mat, Routes.mat, Architecture.mat, message_map.csv
    3  mapping tool                      TT/AVB/BE_Messages.mat, STHS_input.txt
    4  TSN_HeuristicScheduler.exe        Schedule.txt
    5  AVB analysis input generator      AVB_Input.txt, AVB_Input_map.csv
    6  AVB_analysis.exe                  final_new_results.txt
    7  subscriber response times         with R^max and jitter from steps 4 and 6
    8  end-to-end delays                 printed and saved to Experiment<ID>/results.json
       + utilization per link            link_utilization.csv, link_utilization_messages.csv

The exes are run like the .bat does: from the work directory (the folder that
holds Experiment<ID>), with the experiment folder as first argument:
    TSN_HeuristicScheduler.exe Experiment<ID>\\ STHS_input.txt <num_queues>
    AVB_analysis.exe Experiment<ID>\\
Both are waited for. On macOS/Linux they are started through `wine`.

Usage (Windows, exes in the current folder):
    python run_all.py example_system.yaml --system ex1 --exp-id 1
Options: --work-dir, --tools-dir, --num-queues, --folder-arg, --with-be,
--reuse-network-results (skip steps 4 and 6 and read existing outputs),
--instances, --runner.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict

import avb_input_generator
import link_report
import mapping_tool
from analyse import network_json, print_e2e, print_network, print_subscribers
from network_generator import write_experiment
from network_results import apply_network_results
from publisher_rta import analyse_system, json_default, print_report
from subscriber_rta import analyse_e2e, analyse_subscribers
from system import load_systems

SCHEDULER = "TSN_HeuristicScheduler.exe"
AVB_TOOL = "AVB_analysis.exe"
SCHEDULE = "Schedule.txt"
AVB_RESULTS = "final_new_results.txt"


def step(n: int, text: str) -> None:
    print(f"\n=== [{n}/8] {text} " + "=" * max(0, 60 - len(text)))


def run_exe(exe: str, args: list[str], cwd: str, runner: list[str]) -> None:
    if not os.path.isfile(exe):
        raise SystemExit(f"{exe} not found; put the exes in the work directory or pass --tools-dir")
    cmd = runner + [exe] + args
    print("  > " + " ".join(cmd) + f"    (in {cwd})")
    result = subprocess.run(cmd, cwd=cwd)
    if result.returncode != 0:
        print(f"  warning: {os.path.basename(exe)} exited with code {result.returncode}", file=sys.stderr)


def collect_output(name: str, folder: str, work_dir: str, exe: str) -> str:
    """The tool output in the experiment folder; one written to the work dir is moved there."""
    target = os.path.join(folder, name)
    if os.path.isfile(target):
        return target
    stray = os.path.join(work_dir, name)
    if os.path.isfile(stray):
        shutil.move(stray, target)
        print(f"  moved {name} from the work directory into {folder}")
        return target
    raise SystemExit(f"{os.path.basename(exe)} did not write {name} (looked in {folder} and {work_dir}). "
                     f"If it could not find its input, try another --folder-arg, "
                     f"e.g. \"Experiment{{id}}\" or an absolute path.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Publisher -> network tools -> subscriber -> end-to-end")
    ap.add_argument("file", help="YAML/JSON file with the systems")
    ap.add_argument("--system", required=True, help="name of the system to run")
    ap.add_argument("--exp-id", type=int, required=True, help="experiment number (folder Experiment<ID>)")
    ap.add_argument("--work-dir", default=".", help="folder in which Experiment<ID> is created and the "
                                                    "exes are started (default: current folder)")
    ap.add_argument("--tools-dir", help="folder with the two exes (default: the work dir)")
    ap.add_argument("--num-queues", type=int, default=4, help="TSN queues for the scheduler (default 4)")
    ap.add_argument("--folder-arg", default="Experiment{id}\\",
                    help="folder argument given to the exes, {id} = experiment number "
                         "(default: Experiment{id}\\)")
    ap.add_argument("--with-be", action="store_true", help="pass best-effort flows to the AVB analysis")
    ap.add_argument("--reuse-network-results", action="store_true",
                    help="skip the exes and use Schedule.txt / final_new_results.txt already in the folder")
    ap.add_argument("--runner", default=None,
                    help="program that starts the exes (default: none on Windows, 'wine' elsewhere)")
    ap.add_argument("--instances", action="store_true", help="print per-instance values")
    args = ap.parse_args(argv)

    work_dir = os.path.abspath(args.work_dir)
    tools_dir = os.path.abspath(args.tools_dir or work_dir)
    folder = os.path.join(work_dir, f"Experiment{args.exp_id}")
    folder_arg = str(args.exp_id) #args.folder_arg.format(id=args.exp_id)
    runner = (args.runner.split() if args.runner is not None
              else ([] if os.name == "nt" else ["wine"]))

    systems = {s.name: s for s in load_systems(args.file)}
    if args.system not in systems:
        ap.error(f"no system named {args.system!r}")
    system = systems[args.system]
    for w in system.warnings:
        print(f"warning: {w}", file=sys.stderr)

    step(1, "Publisher response times")
    pub = analyse_system(system)
    print_report(system, pub, args.instances)

    step(2, "Network generator")
    write_experiment(system, args.exp_id, work_dir)
    print(f"  {folder}: {len(system.network)} WG messages, {len(system.best_effort)} best-effort flows")

    step(3, "Mapping tool")
    groups = mapping_tool.run(args.exp_id, work_dir)
    print(f"  {len(groups['TT'])} TT, {len(groups['AVB'])} AVB, {len(groups['BE'])} BE")

    step(4, "ST scheduler")
    scheduler = os.path.join(tools_dir, SCHEDULER)
    if args.reuse_network_results:
        print(f"  skipped; using the existing {SCHEDULE}")
    else:
        if os.path.isfile(os.path.join(folder, SCHEDULE)):
            os.remove(os.path.join(folder, SCHEDULE))            # never read a stale schedule
        run_exe(scheduler, [folder_arg, "STHS_input.txt", str(args.num_queues)], work_dir, runner)
    schedule = collect_output(SCHEDULE, folder, work_dir, scheduler)

    step(5, "AVB analysis input generator")
    counts = avb_input_generator.run(args.exp_id, work_dir, with_be=args.with_be)
    print(f"  AVB_Input.txt: {counts['ST blocks']} ST blocks, {counts['AVB']} AVB, {counts['BE']} BE")

    step(6, "AVB analysis")
    avb_tool = os.path.join(tools_dir, AVB_TOOL)
    if args.reuse_network_results:
        print(f"  skipped; using the existing {AVB_RESULTS}")
    else:
        if os.path.isfile(os.path.join(folder, AVB_RESULTS)):
            os.remove(os.path.join(folder, AVB_RESULTS))
        run_exe(avb_tool, [folder_arg], work_dir, runner)
    results = collect_output(AVB_RESULTS, folder, work_dir, avb_tool) if counts["AVB"] else None

    step(7, "Subscriber response times (network results applied)")
    has_st = any(m.hard_real_time for m in system.network.values())
    system = apply_network_results(system, folder, schedule if has_st else None, results)
    print_network(system)
    sub = analyse_subscribers(system)
    print_subscribers(sub, args.instances)

    step(8, "End-to-end delays")
    e2e = analyse_e2e(system, pub, sub)
    print_e2e(e2e)
    links = link_report.write_report(system, folder)
    link_report.print_report(links, system.network_config.bandwidth)
    out = {
        "system": system.name, "experiment": args.exp_id, "time_unit": "us",
        "publishers": {str(p): [asdict(r) for r in rows] for p, rows in pub.items()},
        "network": network_json(system),
        "subscribers": {str(k): [asdict(r) for r in rows] for k, rows in sub.items()},
        "e2e": [asdict(e) for e in e2e],
        "link_utilization": [
            {"link_id": l["link_id"], "from": l["from"], "to": l["to"],
             "messages": [m["id"] for m in l["messages"]], "bytes_per_us": l["bytes_per_us"],
             "utilization": l["utilization"],
             **{f"{c}_utilization": l[c + "_util"] for c in link_report.CLASSES}}
            for l in links],
    }
    with open(os.path.join(folder, "results.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, default=json_default)
    print(f"\n  saved {os.path.join(folder, 'results.json')}")

    bad = [e.writer_group_id for e in e2e
           if (e.pub_status, e.sub_status) != ("ok", "ok") or e.net_status not in ("", "ok")]
    bad += [r.writer_group_id for rows in pub.values() for r in rows if r.status != "ok"]
    if bad:
        print(f"  not schedulable / not valid: WG {sorted(set(bad))}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
