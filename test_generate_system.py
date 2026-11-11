"""Tests for generate_system.py."""
import copy

import pytest

import generate_system as gen
from system import load_systems


@pytest.fixture
def sc():
    return gen.load_scenario("scenario.yaml")


def wgs_of(system):
    return [w for p in system.publishers for w in p.wgs]


def test_default_scenario_is_valid(sc, tmp_path):
    d, system, _ = gen.generate(sc)
    gen.write_yaml(d, str(tmp_path / "g.yaml"), ["test"])
    (loaded,) = load_systems(str(tmp_path / "g.yaml"))          # the written file loads
    assert loaded.name == "gen" and not loaded.warnings          # Delta', v' equal the WG's
    pub, sub = gen.utilizations(loaded)
    assert max(pub.values()) < 1 and max(sub.values()) < 1
    assert all(s.transactions for s in loaded.subscribers)     # every subscriber receives something


def test_global_priorities_st_first(sc):
    _, system, _ = gen.generate(sc)
    wgs = wgs_of(system)
    rhos = sorted(w.rho for w in wgs)
    assert rhos == list(range(1, len(wgs) + 1))                 # unique, global rank
    st = [w for w in wgs if w.st]
    avb = [w for w in wgs if not w.st]
    assert st and min(w.rho for w in st) > max(w.rho for w in avb)
    by_rho = sorted(avb, key=lambda w: -w.rho)                   # shorter period -> higher rho
    assert all(a.T <= b.T for a, b in zip(by_rho, by_rho[1:]))


def test_no_avb_class_before_step_5(sc):
    d, system, _ = gen.generate(sc)
    assert all("avb_class" not in m for m in d["network"]["messages"])
    assert {m.traffic_class for m in system.network.values()} <= {"ST", "AVB"}


def test_same_seed_same_system(sc):
    assert gen.generate(sc)[0] == gen.generate(sc)[0]
    assert gen.generate(sc, seed=2)[0] != gen.generate(sc)[0]


@pytest.mark.parametrize("seed", range(30))
def test_many_seeds_valid(sc, seed):
    _, system, _ = gen.generate(sc, seed)
    assert len(system.network) == len(wgs_of(system))


LINE = {"switches": 2, "publishers": 3, "subscribers": 2, "best_effort_sources": 1, "placement": "random"}


def test_given_topology_same_for_every_seed(sc):
    links = {str(gen.generate(sc, seed)[0]["network"]["links"]) for seed in range(5)}
    assert links == {str(sc["topology"]["links"])}
    d, _, _ = gen.generate(sc)
    assert [p["node"] for p in d["publishers"]] == [1, 2]
    assert [x["node"] for x in d["subscribers"]] == [3, 4]
    assert {f["source"] for f in d["network"]["best_effort"]} == {5}


def test_line_topology_with_own_seed(sc):
    s = copy.deepcopy(sc)
    s["topology"] = dict(LINE, switches=4, seed=7)
    links = {str(gen.generate(s, seed)[0]["network"]["links"]) for seed in range(5)}
    assert len(links) == 1                                     # fixed by topology.seed
    s["topology"].pop("seed")                                  # follows the scenario seed instead
    links = {str(gen.generate(s, seed)[0]["network"]["links"]) for seed in range(5)}
    assert len(links) > 1


def test_star_topology(sc):
    s = copy.deepcopy(sc)
    s["topology"] = dict(LINE, switches=1)
    _, system, _ = gen.generate(s)
    assert all(b == -1 for a, b in system.network_config.links)


def test_split_placement(sc):
    s = copy.deepcopy(sc)
    s["topology"] = dict(LINE, switches=4, placement="split")
    d, _, _ = gen.generate(s)
    attach = {a: b for a, b in d["network"]["links"] if a > 0}
    assert all(attach[p["node"]] in (-1, -2) for p in d["publishers"])
    assert all(attach[x["node"]] in (-3, -4) for x in d["subscribers"])


@pytest.mark.parametrize("share,expected", [(0, 0), (1, None)])
def test_st_share_extremes(sc, share, expected):
    s = copy.deepcopy(sc)
    s["traffic"]["st_share"] = share
    _, system, _ = gen.generate(s)
    n_st = sum(m.hard_real_time for m in system.network.values())
    assert n_st == (len(system.network) if expected is None else expected)


def test_infeasible_scenario_reports_why(sc):
    s = copy.deepcopy(sc)
    s["wcet"]["publisher"]["transport"]["base"] = [20000, 30000]   # > every period
    s["limits"]["attempts"] = 20
    with pytest.raises(gen.ScenarioError, match="publisher utilization"):
        gen.generate(s)


def test_bad_scenarios(sc):
    s = copy.deepcopy(sc)
    s["periods"] = [500.5]
    with pytest.raises(gen.ScenarioError, match="whole microseconds"):
        gen.generate(s)
    s = copy.deepcopy(sc)
    s["topology"]["best_effort_sources"] = []
    with pytest.raises(gen.ScenarioError, match="best_effort source"):
        gen.generate(s)
    s = copy.deepcopy(sc)
    s["topology"]["subscribers"] = [3, 1]
    with pytest.raises(gen.ScenarioError, match="more than one role"):
        gen.generate(s)
    s = copy.deepcopy(sc)
    s["topology"]["subscribers"] = [3, 9]
    with pytest.raises(gen.ScenarioError, match="not in topology.links"):
        gen.generate(s)
