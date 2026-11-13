"""Tests for the evaluation tools: sweeps (scenarios 2 and 5), plots, case study (scenario 1)."""
import copy
import csv
import sys

import pytest

import generate_system as gen
import plot_case_study
import plot_sweep
import run_all
import run_sweep
import test_run_all
from system import load_systems


@pytest.fixture
def tools(tmp_path):
    d = tmp_path / "tools"
    d.mkdir()
    (d / run_all.SCHEDULER).write_text(test_run_all.SCHEDULER)
    (d / run_all.AVB_TOOL).write_text(test_run_all.AVB)
    return d


def sweep(tmp_path, tools, name, parameter, values, base="scenario.yaml", extra=""):
    f = tmp_path / f"{name}.yaml"
    f.write_text(f"name: {name}\nscenario: {base}\nparameter: {parameter}\nvalues: {values}\n"
                 f"seeds: [1, 2]\n{extra}")
    import os
    import shutil
    for scen in ("scenario.yaml", "scenario_line.yaml"):
        shutil.copy(scen, tmp_path / scen)
    work = tmp_path / "runs"
    assert run_sweep.main([str(f), "--work-dir", str(work), "--tools-dir", str(tools),
                           "--runner", sys.executable]) == 0
    return work / name


def rows(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def test_hops_sweep_and_plots(tmp_path, tools):
    out = sweep(tmp_path, tools, "hops", "topology.switches", "[1, 3]", base="scenario_line.yaml")
    runs = rows(out / "results_runs.csv")
    assert len(runs) == 4 and all(r["status"] == "ok" for r in runs)
    wg = rows(out / "results_wg.csv")
    for r in wg:                                         # the parts add up to R
        total = sum(float(r[k]) for k in ("part_publisher", "part_network", "part_jitter", "part_subscriber"))
        assert total == pytest.approx(float(r["R"]))
    assert {r["value"] for r in wg} == {"1", "3"}
    assert plot_sweep.main([str(out)]) == 0
    for f in ("e2e_boxplot.png", "breakdown.png", "schedulable.png", "summary.csv"):
        assert (out / f).is_file()


def test_wcet_scale_sweep_is_paired(tmp_path, tools):
    out = sweep(tmp_path, tools, "wcet_scale", "wcet_scale", "[1.0, 2.0]")
    wg = rows(out / "results_wg.csv")
    by = {(r["value"], r["seed"], r["wg"]): r for r in wg}
    for (v, seed, g), r in by.items():
        if v == "1":
            twice = by[("2", seed, g)]
            assert twice["class"].startswith(r["class"][:2]) and twice["T"] == r["T"]
            assert float(twice["R_p"]) == pytest.approx(2 * float(r["R_p"]), rel=1e-3)


def test_wcet_scale_keeps_the_system(tmp_path):
    sc = gen.load_scenario("scenario.yaml")
    d1, _, _ = gen.generate(sc, 4)
    s2 = copy.deepcopy(sc)
    s2["wcet_scale"] = 3.0
    d3, _, _ = gen.generate(s2, 4)
    w1, w3 = d1["publishers"][0]["writer_groups"][0], d3["publishers"][0]["writer_groups"][0]
    assert (w1["T"], w1["types"], w1["rho"]) == (w3["T"], w3["types"], w3["rho"])
    assert w3["C4"] == pytest.approx(3 * w1["C4"], abs=0.002)
    assert d1["network"] == d3["network"]


def test_failed_generation_is_recorded(tmp_path, tools):
    out = sweep(tmp_path, tools, "mtu", "types", "[{'String(400)': 1.0}]",
                extra="set: {variables_per_wg: [5, 5], limits.attempts: 3}\n")
    runs = rows(out / "results_runs.csv")
    assert all(r["status"] == "failed" and "MTU" in r["error"] for r in runs)


def test_case_study(tmp_path, tools):
    (s,) = load_systems("case_study.yaml")
    assert len(s.publishers) == 3 and len(s.subscribers) == 2 and not s.warnings
    work = tmp_path / "work"
    work.mkdir()
    run_all.main(["case_study.yaml", "--system", "case_study", "--exp-id", "1", "--work-dir", str(work),
                  "--tools-dir", str(tools), "--runner", sys.executable, "--with-be"])
    assert plot_case_study.main([str(work / "Experiment1")]) == 0
    table = rows(work / "Experiment1" / "case_study_table.csv")
    assert [r["wg"] for r in table] == [str(i) for i in range(1, 8)]
    for f in ("case_study_breakdown.png", "case_study_links.png"):
        assert (work / "Experiment1" / f).is_file()
