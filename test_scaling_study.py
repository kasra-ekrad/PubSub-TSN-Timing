"""Scaling study (stand-in exes, few seeds and steps)."""
import copy
import os
import sys

import yaml

import generate_system as gen
import run_all
import scaling_study as ss
import test_run_all
from system import _parse_system


def _tools(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / run_all.SCHEDULER).write_text(test_run_all.SCHEDULER)
    (tools / run_all.AVB_TOOL).write_text(test_run_all.AVB)
    return tools


def _spec(tmp_path, name, **over):
    sp = yaml.safe_load(open(f"experiments/{name}.yaml"))
    sp["scenario"] = os.path.abspath("scenario.yaml")
    sp.update(over)
    path = tmp_path / f"{name}.yaml"
    path.write_text(yaml.safe_dump(sp))
    return str(path)


def test_count_mode_round_robin_and_same_data():
    sp = ss.load_spec("experiments/scaling_count.yaml")
    dr = ss.draw(sp, 1)
    d = ss.system_dict(sp, dr, 9)
    pubs = {p["node"]: [w["writer_group_id"] for w in p["writer_groups"]] for p in d["publishers"]}
    assert pubs == {1: [1, 8], 2: [2, 9], 3: [3], 4: [4], 5: [5], 6: [6], 7: [7]}
    subs = {s["node"]: [t["writer_group_id"] for t in s["transactions"]] for s in d["subscribers"]}
    assert subs == {8: [1, 3, 5, 7, 9], 9: [2, 4, 6, 8]}
    assert all(w["types"] == ["Float"] * 8 for p in d["publishers"] for w in p["writer_groups"])
    # the first transactions are the same at every step
    d2 = ss.system_dict(sp, dr, 12)
    assert d2["publishers"][0]["writer_groups"][0]["C4"] == d["publishers"][0]["writer_groups"][0]["C4"]


def test_data_mode_grows_every_wg_and_dict_utilization_matches():
    sp = ss.load_spec("experiments/scaling_data.yaml")
    dr = ss.draw(sp, 3)
    for v in (1, 5):
        d = ss.system_dict(sp, dr, v)
        assert all(len(w["types"]) == v for p in d["publishers"] for w in p["writer_groups"])
        system = _parse_system(copy.deepcopy(d), "t")
        pu, su = gen.utilizations(system)
        a, b = ss.dict_utilizations(d)
        assert sorted(pu.values()) == sorted(a.values()) and sorted(su.values()) == sorted(b.values())
    subs = [t["writer_group_id"] for s in ss.system_dict(sp, dr, 1)["subscribers"] for t in s["transactions"]]
    assert len(subs) == len(dr["wgs"])                                     # every WG reaches a subscriber


def test_scaling_runs(tmp_path):
    tools = _tools(tmp_path)
    for name, over in (("scaling_count", {"max_wgs": 60}), ("scaling_data", {"max_variables": 40})):
        out = tmp_path / name
        assert ss.main([_spec(tmp_path, name, **over), "--seeds", "1", "2", "--out", str(out),
                        "--tools-dir", str(tools), "--runner", sys.executable]) == 0
        res = ss.read_seeds(str(out))
        assert [r["seed"] for r in res] == [1, 2]
        for r in res:
            xs = [s["x"] for s in r["steps"]]
            assert xs == list(range(xs[0], xs[0] + len(xs)))                 # no step skipped
            d = r["limits"]["deadline"]
            if d is not None:                                                # exes only up to the miss
                assert all(s["network"] == (1 if s["x"] <= d else 0) for s in r["steps"])
                assert max(w["x"] for w in r["wgs"]) == d
            for k in ("pub_util", "sub_util"):                               # limits match the steps
                key = {"pub_util": "U_pub_max", "sub_util": "U_sub_max"}[k]
                first = next((s["x"] for s in r["steps"] if s[key] >= 1), None)
                assert r["limits"][k] == first
        for f in ("limits.csv", "steps.csv", "wgs.csv", "nodes.csv", "survival.pdf", "growth.pdf", "limits.pdf"):
            assert (out / f"{name}_{f}").is_file()
        assert not any(n.startswith("Experiment") for n in os.listdir(out))   # folders removed
