"""Tests for the mapping tool and the best-effort flows.

The port was also compared with the original Mapping_tool.m (run in Octave) on
three inputs: with ST, without ST (placeholder) and without BE (scalar 0).
STHS_input.txt was byte-identical and the TT/AVB/BE MAT files identical in
values, classes and field order.
"""
import textwrap

import pytest
from scipy.io import loadmat

import mapping_tool
from network_generator import write_experiment
from system import ConfigError, load_systems


def systems():
    return {s.name: s for s in load_systems("example_system.yaml")}


def test_ex1_mapping(tmp_path):
    write_experiment(systems()["ex1"], 1, str(tmp_path))
    groups = mapping_tool.run(1, str(tmp_path))
    assert [len(groups[c]) for c in ("TT", "AVB", "BE")] == [1, 5, 2]
    sths = (tmp_path / "Experiment1" / "STHS_input.txt").read_text()
    # WG 1: 93 bytes, T = 500, path 2 -> -1 -> -2 -> 3 = architecture rows 3, 7, 10;
    # identical to the STHS_input.txt the real tools were run with
    assert sths == ("message\nlength = 93\nperiod = 500\ndeadline = 500\nlinkNbr = 3\n"
                    "link = 3\nlink = 7\nlink = 10\ninitOffset = 0\n\n")
    tt = loadmat(tmp_path / "Experiment1" / "TT_Messages.mat")["message_TT"]
    be = loadmat(tmp_path / "Experiment1" / "BE_Messages.mat")["message_BE"]
    assert tt.shape == (1, 1) and be.shape == (1, 2)
    assert tt[0, 0]["hard_real_time"].dtype.kind in "bu"   # stays a logical


def test_empty_class_is_scalar_zero(tmp_path):
    write_experiment(systems()["jitter_demo"], 2, str(tmp_path))
    groups = mapping_tool.run(2, str(tmp_path))
    assert [len(groups[c]) for c in ("TT", "AVB", "BE")] == [2, 1, 0]
    be = loadmat(tmp_path / "Experiment2" / "BE_Messages.mat")["message_BE"]
    assert be.shape == (1, 1) and float(be[0, 0]) == 0


def test_placeholder_when_no_st(tmp_path):
    msgs = [
        {"period": 100.0, "offset": 0.0, "jitter_transmission": 0.0, "jitter_reception": 0.0,
         "min_inter_arr_time": 0.0, "length": 90.0, "deadline": 100.0, "hard_real_time": False,
         "source": 1.0, "destination": 3.0, "path": 1.0},
        {"period": 0.0, "offset": 0.0, "jitter_transmission": 0.0, "jitter_reception": 0.0,
         "min_inter_arr_time": 500.0, "length": 300.0, "deadline": 0.0, "hard_real_time": False,
         "source": 2.0, "destination": 4.0, "path": 2.0},
    ]
    p = mapping_tool.placeholder_tt(msgs)
    assert (p["period"], p["length"], p["deadline"], p["source"], p["destination"], p["path"]) == \
           (100.0, 90.0, 100.0, 1.0, 4.0, 1.0)
    assert p["jitter_reception"] == 1.0 and p["hard_real_time"] == 1.0


def test_non_integer_period_rejected():
    m = {"length": 93.0, "period": 500.5, "min_inter_arr_time": 0.0, "deadline": 500.5,
         "offset": 0.0, "destination": 2.0, "source": 1.0, "path": 1.0}
    import numpy as np
    cell = np.empty((1, 1), dtype=object); cell[0, 0] = np.array([[1.0], [-1.0], [2.0]])
    route = np.empty((2, 2), dtype=object); route[1, 0] = cell
    with pytest.raises(mapping_tool.MappingError, match="not an integer"):
        mapping_tool.sths_block(m, route, [(1, -1), (-1, 1), (2, -1), (-1, 2)])


# --------------------------------------------------------------- best effort

BASE = """
time_unit: us
systems:
- name: x
  publishers:
  - p: 1
    node: 1
    writer_groups:
    - {n: 1, writer_group_id: 1, T: 100, rho: 1, types: [Boolean], psi: [1], phi: [1], C3: 1, C4: 1}
  subscribers:
  - k: 1
    node: 2
    transactions:
    - {m: 1, writer_group_id: 1, C1: 1, C2: 1, phi: [1], psi: [1]}
  network:
    links: LINKS
    bandwidth: 125
    epsilon: 1
    messages:
    - {writer_group_id: 1, hard_real_time: true, R_max: 5}
    best_effort:
    - BE
"""


def load(tmp_path, be, links="[[1, -1], [2, -1], [3, -1]]"):
    f = tmp_path / "s.yaml"
    f.write_text(BASE.replace("BE", be).replace("LINKS", links))
    return load_systems(str(f))


def test_best_effort_ok(tmp_path):
    (s,) = load(tmp_path, "{name: bg, source: 3, destination: 2, length: 1542, period: 1000}")
    (b,) = s.best_effort
    assert (b.path, b.route_index, b.length, b.period, b.min_inter_arr_time) == ((3, -1, 2), 1, 1542, 1000, 0)


def test_best_effort_period_xor_mit(tmp_path):
    with pytest.raises(ConfigError, match="exactly one"):
        load(tmp_path, "{name: bg, source: 3, destination: 2, length: 100, period: 10, min_inter_arr_time: 5}")
    with pytest.raises(ConfigError, match="exactly one"):
        load(tmp_path, "{name: bg, source: 3, destination: 2, length: 100}")


def test_best_effort_counts_in_link_load(tmp_path):
    # link -1 -> 2: WG 1 (91 bytes / 100 us) + BE (11 400 bytes / 100 us) > 90 % of 125 bytes/us
    with pytest.raises(ConfigError, match="max_link_load"):
        f = tmp_path / "s.yaml"
        f.write_text(BASE.replace("BE", "{name: bg, source: 3, destination: 2, length: 11400, period: 100}")
                     .replace("LINKS", "[[1, -1], [2, -1], [3, -1]]")
                     .replace("    epsilon: 1", "    epsilon: 1\n    max_link_load: 90"))
        load_systems(str(f))


def test_best_effort_unknown_node(tmp_path):
    with pytest.raises(ConfigError, match="not an end node"):
        load(tmp_path, "{name: bg, source: 7, destination: 2, length: 100, period: 10}")


def test_duplicate_link(tmp_path):
    with pytest.raises(ConfigError, match="duplicate link"):
        load(tmp_path, "{name: bg, source: 3, destination: 2, length: 100, period: 10}",
             links="[[1, -1], [2, -1], [3, -1], [-1, 3]]")
