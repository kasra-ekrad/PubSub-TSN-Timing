"""Tests for avb_input_generator.py.

The port was also compared with the original AVB_analysis_input_generator.m
(run in Octave) on a system with two ST messages sharing a link, a shuffled
synthetic Schedule.txt and 4 AVB + 2 BE messages: AVB_Input.txt was
byte-identical (same odd/even AVB class rule), and with --with-be against
the original with its BE block uncommented.
"""
import pytest

import avb_input_generator as avb
import mapping_tool
from mapping_tool import MappingError, link_ids, load_architecture, load_routes, route_path
from network_generator import write_experiment
from system import load_systems


@pytest.fixture
def exp(tmp_path):
    """ex1 generated and mapped, with a Schedule.txt for its one ST message (WG 1, T = 500)."""
    s = {x.name: x for x in load_systems("example_system.yaml")}["ex1"]
    write_experiment(s, 1, str(tmp_path))
    mapping_tool.run(1, str(tmp_path))
    folder = tmp_path / "Experiment1"
    tt = avb.load_group(str(folder / "TT_Messages.mat"), "message_TT")
    route, arch = load_routes(str(folder / "Routes.mat")), load_architecture(str(folder / "Architecture.mat"))
    links = link_ids(route_path(route, tt[0]), arch)                    # [3, 10, 6]
    lines = ["msg,link,instance,start,end"]                             # header, skipped like readmatrix
    for hop, link in enumerate(links):
        for k in (1, 0):                                                # two instances, not in time order
            start = k * 500_000 + hop * 2000 + 250
            lines.append(f"0,{link},{k},{start},{start + 744}")
    (folder / "Schedule.txt").write_text("\n".join(lines) + "\n")
    return tmp_path, folder, links


def blocks(text):
    return [b for b in text.split("\n\n") if b.strip()]


def test_st_blocks_from_schedule(exp):
    base, folder, links = exp
    counts = avb.run(1, str(base))
    out = blocks((folder / "AVB_Input.txt").read_text())
    st = [b for b in out if "priority = ST" in b]
    # largest end on each link is ~500.25..504.99 us -> ceil(/500) = 2 instances per link
    assert counts["ST blocks"] == len(st) == 2 * len(links)
    assert st[0] == ("message\nlength = 93\nperiod = 1000\ndeadline = 1000\npriority = ST\n"
                     "linkNbr = 1\nlink = 3\njitterIn = 0\ninitOffset = 0\noffset = 500.250000")
    assert st[1].endswith("offset = 0.250000")                          # file order, as find(...)


def test_avb_classes_assigned_in_step_5(exp):
    base, folder, _ = exp
    avb.run(1, str(base))
    prios = [l.split()[-1] for l in (folder / "AVB_Input.txt").read_text().splitlines()
             if l.startswith("priority = Class")]
    # original rule: odd positions ClassA, even positions ClassB
    assert prios == ["ClassA", "ClassB", "ClassA", "ClassB", "ClassA"]
    rows = (folder / "AVB_Input_map.csv").read_text().splitlines()[1:]
    assert [r.split(",")[4] for r in rows if ",AVB," in r] == prios


def test_with_be(exp):
    base, folder, _ = exp
    assert avb.run(1, str(base))["BE"] == 0
    assert "NRT" not in (folder / "AVB_Input.txt").read_text()
    assert avb.run(1, str(base), with_be=True)["BE"] == 2
    be = [b for b in blocks((folder / "AVB_Input.txt").read_text()) if "priority = NRT" in b]
    assert be[1].startswith("message\nlength = 600\nperiod = 5000\ndeadline = 5000\n")   # sporadic: min_inter_arr_time


def test_missing_schedule_skips_st(exp, capsys):
    base, folder, _ = exp
    (folder / "Schedule.txt").unlink()
    assert avb.run(1, str(base))["ST blocks"] == 0
    assert "Schedule.txt not found" in capsys.readouterr().err


def test_too_few_start_times(exp):
    base, folder, links = exp
    text = (folder / "Schedule.txt").read_text().splitlines()
    (folder / "Schedule.txt").write_text("\n".join(l for l in text if not l.startswith(f"0,{links[0]},0,")))
    with pytest.raises(MappingError, match="start times"):
        avb.run(1, str(base))


def test_read_schedule_delimiters(tmp_path):
    f = tmp_path / "s.txt"
    f.write_text("Message Link Instance Start End\n0 3 0 250 994\n0;3;1;500250;500994\n\n0\t6\t0\t4250\t4994\n")
    m = avb.read_schedule(str(f))
    assert m.shape == (3, 5) and m[1, 3] == 500250 and m[2, 1] == 6
