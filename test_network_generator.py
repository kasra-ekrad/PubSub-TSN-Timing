"""Tests for the NM size formula, the route port and the MAT export.

Route enumeration was also compared with the original Network_generator.m
(run in Octave) on 41 topologies: identical paths, order and orientation.
"""
import pytest
from scipy.io import loadmat

import topology
import uadp
from network_generator import message_list, write_experiment
from system import load_systems


# ----------------------------------------------------------------- NM size

def test_field_sizes():
    assert uadp.field_size("Boolean") == 1
    assert uadp.field_size("Int32") == 4
    assert uadp.field_size("Double") == 8
    assert uadp.field_size("Guid") == 16
    assert uadp.field_size("String(32)") == 4 + 32
    assert uadp.field_size("Float[8]") == 4 + 8 * 4
    assert uadp.field_size("String(16)[4]") == 4 + 4 * (4 + 16)


@pytest.mark.parametrize("bad", ["String", "Int32(4)", "Variant", "NodeId", "Float[]", ""])
def test_unsupported_types(bad):
    with pytest.raises(uadp.SizeError):
        uadp.field_size(bad)


def test_nm_size_by_hand():
    # Header 1+1+2+1+2+4+2+2 = 15, two DSM headers 2*5, fields 8+8+4+4+4 = 28
    assert uadp.nm_size(["Double", "Double", "Float", "Float", "Int32"], 2) == 15 + 10 + 28
    assert uadp.nm_size(["Boolean"], 1, "UInt64") == 21 + 5 + 1


def test_frame_size():
    s = uadp.frame_size(53)                      # 14 + 4 + 20 + 8 + 53 + 4
    assert (s.ip_packet, s.frame, s.wire) == (81, 103, 123)
    small = uadp.frame_size(21)                  # padded to the 64-byte minimum
    assert (small.frame, small.wire) == (71, 91)
    assert uadp.frame_size(5, vlan_tag=False).frame == 64
    assert uadp.frame_size(1472).ip_packet == 1500
    with pytest.raises(uadp.SizeError, match="MTU"):
        uadp.frame_size(1473)


# ------------------------------------------------------------------ routes

ARCH = topology.build_architecture(
    [(1, -1), (2, -2), (3, -1), (4, -2), (-1, -2), (-1, -3), (-2, -3)], full_duplex=True)


def test_architecture_full_duplex_order():
    assert ARCH[:4] == [(1, -1), (-1, 1), (2, -2), (-2, 2)]


def test_routes_match_octave_reference():
    r = topology.all_routes(ARCH)
    assert r[(3, 1)] == [[1, -1, 3]]
    assert r[(4, 3)] == [[3, -1, -2, 4], [3, -1, -3, -2, 4]]
    assert r[(1, 1)] == [[1]]


def test_unreachable_pair():
    arch = topology.build_architecture([(1, -1), (2, -2)], full_duplex=True)
    assert topology.routes_between(arch, 1, 2) == []


def test_select_path():
    routes = [[3, -1, -3, -2, 4], [3, -1, -2, 4], [3, -2, -1, 4]]
    assert topology.select_path(routes) == 2                      # shortest, first on ties
    assert topology.select_path(routes, [3, -2, -1, 4]) == 3
    with pytest.raises(ValueError):
        topology.select_path(routes, [3, 4])


# --------------------------------------------------------------- MAT export

def test_mat_files_round_trip(tmp_path):
    s = {x.name: x for x in load_systems("example_system.yaml")}["ex1"]
    folder = write_experiment(s, 3, str(tmp_path))
    msgs = loadmat(f"{folder}/Messages.mat")["message"]
    route = loadmat(f"{folder}/Routes.mat")["route"]
    arch = loadmat(f"{folder}/Architecture.mat")["architecture"]

    assert msgs.shape == (1, 8) and arch.shape == (14, 2) and route.shape == (6, 6)
    assert msgs.dtype.names == ("period", "offset", "jitter_transmission", "jitter_reception",
                                "min_inter_arr_time", "length", "deadline", "hard_real_time",
                                "source", "destination", "path")
    expected = message_list(s)
    for i, e in enumerate(expected):
        m = msgs[0, i]
        assert float(m["period"][0, 0]) == float(e["period"])
        assert float(m["deadline"][0, 0]) == (0 if e["class"] == "BE" else float(e["period"]))
        assert m["offset"][0, 0] == m["jitter_transmission"][0, 0] == m["jitter_reception"][0, 0] == 0
        assert int(m["length"][0, 0]) == e["length"]
        assert bool(m["hard_real_time"][0, 0]) == e["hard_real_time"]
        d, src, k = int(m["destination"][0, 0]), int(m["source"][0, 0]), int(m["path"][0, 0])
        path = route[d - 1, src - 1][0, k - 1].ravel().astype(int).tolist()
        assert path == list(e["path_nodes"])
    rows = open(f"{folder}/message_map.csv").read().splitlines()
    assert rows[1].startswith("1,TT,1,,True") and rows[7].startswith("7,BE,,camera,False")
    assert len(rows) == 9
