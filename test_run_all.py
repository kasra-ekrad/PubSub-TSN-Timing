"""End-to-end test of run_all.py with stand-in exes.

The stand-ins are small Python programs named like the real exes. They take the
same arguments (experiment folder, STHS_input.txt, number of queues / folder)
and write Schedule.txt and final_new_results.txt in the real formats: the
scheduler places every ST frame as late as possible in its period, like the real
one; the AVB stand-in reports WCRT = links * length * 80 ns + 50 us.
"""
import json
import math
import sys
import textwrap

import pytest

import run_all

SCHEDULER = r'''
import math, os, sys
folder = sys.argv[1].replace("\\", "/")
blocks = [b for b in open(os.path.join(folder, sys.argv[2])).read().split("\n\n") if b.strip()]
msgs = []
for b in blocks:
    kv = [l.split(" = ") for l in b.splitlines()[1:]]
    msgs.append({"length": int(kv[0][1]), "period": int(kv[1][1]),
                 "links": [int(v) for k, v in kv if k == "link"]})
H = math.lcm(*[m["period"] for m in msgs])
with open(os.path.join(folder, "Schedule.txt"), "w") as f:
    for i, m in enumerate(msgs):
        d = m["length"] * 80                        # ns per frame at 100 Mbit/s
        for k in range(H // m["period"]):
            close = (k + 1) * m["period"] * 1000
            for j, link in reversed(list(enumerate(m["links"]))):
                f.write(f"{i};{link};0;{close - d};{close};\n")
                close -= d
'''

AVB = r'''
import os, sys
folder = sys.argv[1].replace("\\", "/")
blocks = [b for b in open(os.path.join(folder, "AVB_Input.txt")).read().split("\n\n") if b.strip()]
with open(os.path.join(folder, "final_new_results.txt"), "w") as f:
    for i, b in enumerate(blocks):
        kv = dict(l.split(" = ") for l in b.splitlines()[1:] if " = " in l)
        if kv["priority"] in ("ClassA", "ClassB"):
            prio = 2 if kv["priority"] == "ClassA" else 3
            wcrt_ns = int(kv["linkNbr"]) * int(kv["length"]) * 80 + 50_000
            f.write(f"{i};{prio};{int(kv['period']) * 10**6};{wcrt_ns * 1000};\n")
'''


@pytest.fixture
def tools(tmp_path):
    d = tmp_path / "tools"
    d.mkdir()
    (d / run_all.SCHEDULER).write_text(SCHEDULER)
    (d / run_all.AVB_TOOL).write_text(AVB)
    return d


def run(tmp_path, tools, *extra):
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    code = run_all.main(["example_system.yaml", "--system", "ex1", "--exp-id", "1",
                         "--work-dir", str(work), "--tools-dir", str(tools),
                         "--runner", sys.executable, *extra])
    return code, work / "Experiment1"


def test_full_flow(tmp_path, tools):
    code, folder = run(tmp_path, tools)
    assert code == 0
    for f in ("Messages.mat", "STHS_input.txt", "Schedule.txt", "AVB_Input.txt",
              "AVB_Input_map.csv", "final_new_results.txt", "results.json"):
        assert (folder / f).is_file(), f
    res = json.loads((folder / "results.json").read_text())
    net = res["network"]
    # WG 1 (ST, T = 500): last window closes at the end of the period
    assert net["1"]["st_arrival_max"] == 500 and net["1"]["J"] == 0
    assert net["1"]["st_first_gate_min"] == 500 - 3 * 7.44
    # WG 10 (AVB, 123 bytes, 3 links): 3 * 123 * 80 ns + 50 us
    assert net["10"]["R_max"] == pytest.approx(3 * 123 * 0.08 + 50)
    assert net["10"]["J"] == pytest.approx(net["10"]["R_max"] - net["10"]["R_min"])
    e2e = {e["writer_group_id"]: e for e in res["e2e"]}
    assert e2e[1]["R"] == pytest.approx(500 + e2e[1]["R_s"])                 # ST: A_max + R^s
    assert e2e[10]["R"] == pytest.approx(e2e[10]["R_p"] + e2e[10]["R_max"] + e2e[10]["R_s"])
    assert all(e["net_status"] == "ok" for e in res["e2e"])


def test_stale_outputs_are_not_reused(tmp_path, tools):
    run(tmp_path, tools)
    (tools / run_all.AVB_TOOL).write_text("import sys\n")                    # writes nothing
    with pytest.raises(SystemExit, match="did not write final_new_results.txt"):
        run(tmp_path, tools)


def test_reuse_network_results(tmp_path, tools):
    run(tmp_path, tools)
    for f in (run_all.SCHEDULER, run_all.AVB_TOOL):
        (tools / f).unlink()                                                  # exes not needed
    code, folder = run(tmp_path, tools, "--reuse-network-results")
    assert code == 0 and (folder / "results.json").is_file()


def test_missing_exe(tmp_path, tools):
    (tools / run_all.SCHEDULER).unlink()
    with pytest.raises(SystemExit, match="TSN_HeuristicScheduler.exe not found"):
        run(tmp_path, tools)


def test_st_window_before_publisher_hand_off(tmp_path, tools):
    # A schedule whose ST window opens at 10 us, before R^p = 42.1 us of WG 1
    early = SCHEDULER.replace("close = (k + 1) * m[\"period\"] * 1000",
                              "close = k * m[\"period\"] * 1000 + 10_000 + len(m[\"links\"]) * d")
    (tools / run_all.SCHEDULER).write_text(early)
    code, folder = run(tmp_path, tools)
    res = json.loads((folder / "results.json").read_text())
    st = [e for e in res["e2e"] if e["writer_group_id"] == 1][0]
    assert st["net_status"] == "st_not_ready" and code == 1
