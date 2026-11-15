"""Tests for the simulation-based validation (sim_core.py, sim_tsn.py, validate.py, plot_validation.py)."""
import argparse
import csv
import sys
from fractions import Fraction

import pytest

import plot_validation
import run_all
import sim_core as sc
import test_evaluation
import test_run_all
import validate
from sim_tsn import TSN, Windows

US = sc.PS


def args(**kw):
    a = dict(seed=1, runs=3, hyperperiods=3, bcet=0.5, search=1, modes=list(validate.MODES),
             levels=None, idle_slope="srp", idle_slope_factor=1.0, be="auto", out=None, resume=False)
    a.update(kw)
    return argparse.Namespace(**a)


def observe(model, mode, level="A", **kw):
    return validate.run_mode(model, level, mode, args(**kw))


# ------------------------------------------------------------------- scheduler

def test_np_fp_is_non_preemptive_fixed_priority():
    jobs = [(0, 1, 5, "low"), (1, 0, 3, "high"), (2, 2, 1, "lowest"), (1, 0, 2, "high2")]
    out = sc.np_fp(jobs)
    assert out["low"] == (0, 5)            # started first, not preempted
    assert out["high"] == (5, 8)           # same rank: release order (then input order)
    assert out["high2"] == (8, 10)
    assert out["lowest"] == (10, 11)


def test_releases():
    assert sc.releases(-1, 10, 0, 35) == [9, 19, 29]
    assert sc.releases(0, 10, -20, 0) == [-20, -10, 0]


# ---------------------------------------------------------- tightness, Level A

PUB_E1 = """
time_unit: us
systems:
  - name: e1
    publishers:
      - p: 1
        writer_groups:
          - {n: 1, writer_group_id: 5, T: 500,  rho: 10, psi: [8.967],  phi: [0], C3: 0, C4: 0}
          - {n: 2, writer_group_id: 2, T: 1000, rho: 9,  psi: [11.826], phi: [0], C3: 0, C4: 0}
          - {n: 3, writer_group_id: 1, T: 2000, rho: 4,  psi: [11.706], phi: [0], C3: 0, C4: 0}
          - {n: 4, writer_group_id: 4, T: 2000, rho: 3,  psi: [14.9],   phi: [0], C3: 0, C4: 0}
          - {n: 5, writer_group_id: 3, T: 5000, rho: 2,  psi: [13.829], phi: [0], C3: 0, C4: 0}
"""


@pytest.fixture
def e1(tmp_path):
    p = tmp_path / "e1.yaml"
    p.write_text(PUB_E1)
    return validate.load(str(p), "e1", None)


def test_publisher_bound_reached_with_arbitrary_phases(e1):
    """The critical instant (blocker 1 ps before, higher priority released together) reaches
    R^p, minus the 1 ps the blocker must start earlier. WG 4: 61.228 us (run_6 example)."""
    o = observe(e1, "adv-arbitrary")
    assert e1.tasks[4].Rp == 61_228_000
    for g, t in e1.tasks.items():
        got = o.obs[g].max["pub"][0]
        assert got <= t.Rp
        lower = any(x.rank > t.rank for x in e1.tasks.values())
        assert got == t.Rp - (sc.EPS if lower else 0), g


def test_part14_phasing_cannot_block(e1):
    """OPC 10000-14 6.3.1.1.1: cycles start at multiples of T on the node clock. WG 3 is always
    done before WG 4 is released, so WG 4 never sees blocking: 47.399 us, 23 % below R^p."""
    o = observe(e1, "adv-part14")
    assert o.obs[4].max["pub"][0] == 47_399_000
    r = observe(e1, "random-part14")
    assert r.obs[4].max["pub"][0] <= 47_399_000


def test_subscriber_and_e2e_bound_reached():
    """jitter_demo: WG 1 (AVB, J' = 10) reaches R^s = 20 and R = 25 (minus 1 ps of blocking),
    WG 3 reaches R^s = 13 exactly; nothing above any bound in any mode."""
    m = validate.load("example_system.yaml", "jitter_demo", None)
    adv = observe(m, "adv-arbitrary")
    assert adv.obs[1].max["sub"][0] == 20 * US - sc.EPS
    assert adv.obs[1].max["e2e"][0] == 25 * US - sc.EPS
    assert adv.obs[3].max["sub"][0] == 13 * US
    for mode in validate.MODES:
        o = observe(m, mode)
        assert all(not any(x.viol.values()) for x in o.obs.values()), mode


def test_random_mode_stays_within_the_delay_box():
    m = validate.load("example_system.yaml", "jitter_demo", None)
    o = observe(m, "random-arbitrary", runs=5, hyperperiods=20)
    s = m.streams[1]
    assert s.net_lo <= o.obs[1].net_min and o.obs[1].max["net"][0] <= s.net_hi
    assert len(o.obs[1].samples["e2e"]) == o.obs[1].n


# ------------------------------------------------------------ Level B mechanics

@pytest.fixture(scope="module")
def ex1(tmp_path_factory):
    """ex1 through run_all.py with the stand-in exes (ST WG 1, AVB WGs, best effort)."""
    work = tmp_path_factory.mktemp("ex1")
    tools = work / "tools"
    tools.mkdir()
    (tools / run_all.SCHEDULER).write_text(test_run_all.SCHEDULER)
    (tools / run_all.AVB_TOOL).write_text(test_run_all.AVB)
    assert run_all.main(["example_system.yaml", "--system", "ex1", "--exp-id", "1", "--work-dir", str(work),
                         "--tools-dir", str(tools), "--runner", sys.executable, "--with-be"]) == 0
    folder = str(work / "Experiment1")
    return validate.load("example_system.yaml", "ex1", folder), folder, work


def test_level_b_single_frame_takes_h_times_C(ex1):
    m, folder, _ = ex1
    net = TSN(m, folder)
    for g, s in m.streams.items():
        if s.cls == "ST":
            continue
        arr, late = net.run({g: [(0, 10 * US)]}, {})
        assert arr[g][0] - 10 * US == s.dmin == len(s.links) * s.C   # epsilon = 0 in ex1


def test_level_b_credit_based_shaper(ex1):
    """Two class-A frames of WG 2 (14 us each, 3 hops) handed over together: the second waits
    on link 3 until the credit, -C (1 - a), is back at 0 at idleSlope a = C / 125 us (SRP),
    i.e. it starts 125 us after the first."""
    m, folder, _ = ex1
    net = TSN(m, folder)
    s = m.streams[2]
    assert s.cls == "AVB_A" and s.C == 14 * US
    assert net.alpha[s.links[0]]["A"] == Fraction(14, 125)
    arr, _ = net.run({2: [(0, 0), (0, 0)]}, {})
    assert arr[2] == [42 * US, 125 * US + 42 * US]


def test_level_b_guard_band(ex1):
    """WG 13 (19.28 us) reaches link 7 at 479.28 us; it would run into the ST window
    [485.12, 492.56), so it waits until the window closes."""
    m, folder, _ = ex1
    net = TSN(m, folder)
    s = m.streams[13]
    assert s.links[1] == 7 and s.C == 19_280_000
    arr, _ = net.run({13: [(0, 460 * US)]}, {})
    assert arr[13][0] == 492_560_000 + 2 * s.C


def test_level_b_st_window(ex1):
    m, folder, _ = ex1
    net = TSN(m, folder)
    arr, late = net.run({1: [(0, 10 * US)]}, {})       # in time: arrives when its last window closes
    assert arr[1] == [500 * US] and late[1] == [False]
    arr, late = net.run({1: [(0, 478 * US)]}, {})      # after its first gate opened: next period
    assert arr[1] == [1000 * US] and late[1] == [True]


def test_windows_open_time_and_wrap():
    w = Windows([(90, 110)], 100)                      # wraps: [90, 100) and [0, 10)
    assert w.end_if_inside(95) == 110 and w.end_if_inside(105) == 110 and w.end_if_inside(50) is None
    assert w.next_start(50) == 90 and w.next_start(91) == 190
    assert w.open_time(0, 100) == 80 and w.open_time(50, 250) == 160


# --------------------------------------------------------- command line, plots

def test_validate_experiment_and_plots(ex1):
    _, folder, work = ex1
    code = validate.main(["example_system.yaml", "--system", "ex1", "--exp-id", "1", "--work-dir", str(work),
                          "--runs", "2", "--hyperperiods", "2", "--search", "1"])
    rows = list(csv.DictReader(open(f"{folder}/validation.csv")))
    assert {r["level"] for r in rows} == {"A", "B"} and {r["mode"] for r in rows} == set(validate.MODES)
    level_a = [r for r in rows if r["level"] == "A"]
    assert all(int(r[f"viol_{k}"]) == 0 for r in level_a for k in ("pub", "net", "sub", "e2e", "net_min"))
    # the stand-in AVB tool ignores best effort, so Level B finds its R^max too small
    assert code == 1
    assert plot_validation.main([folder]) == 0
    for f in ("validation_scatter_A.png", "validation_ratio_B.png", "validation_e2e_A.png",
              "validation_summary.csv"):
        assert (work / "Experiment1" / f).is_file()


def test_validate_sweep(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / run_all.SCHEDULER).write_text(test_run_all.SCHEDULER)
    (tools / run_all.AVB_TOOL).write_text(test_run_all.AVB)
    out = test_evaluation.sweep(tmp_path, tools, "hops", "topology.switches", "[1, 2]", base="scenario_line.yaml")
    assert validate.main(["--sweep", str(out), "--levels", "A", "--runs", "1", "--hyperperiods", "1"]) == 0
    rows = list(csv.DictReader(open(out / "validation_all.csv")))
    assert {r["value"] for r in rows} == {"1", "2"} and {r["seed"] for r in rows} == {"1", "2"}
    assert all(int(r["viol_e2e"]) == 0 for r in rows)
    assert plot_validation.main([str(out)]) == 0
    assert (out / "validation_scatter_A.png").is_file()


def test_publisher_output_jitter_counterexample():
    """The subscriber analysis takes J' = R^max - R^min (no publisher jitter). When a WG's
    hand-over time varies (here between W = 1 and R^p = 96 us), its frames can arrive closer
    than T - J', and the bound is exceeded: the "jit" adversarial pattern finds it."""
    m = validate.load("publisher_jitter_example.yaml", "pubjit", None)
    assert m.streams[20].Rs == 20 * US and m.streams[20].R == 23 * US
    o = observe(m, "adv-arbitrary")
    assert o.obs[20].max["sub"][0] == 30 * US
    assert o.obs[20].viol["sub"] and o.obs[20].viol["e2e"]
    assert "jit" in o.obs[20].max["sub"][1]
