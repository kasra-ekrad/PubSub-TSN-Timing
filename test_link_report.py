"""Tests for the per-link utilization report."""
from fractions import Fraction

import link_report
from system import load_systems


def ex1():
    return {s.name: s for s in load_systems("example_system.yaml")}["ex1"]


def test_shared_link_by_hand():
    links = link_report.link_utilization(ex1())
    l = links[6]                                        # row 7 of architecture: -1 -> -2
    assert (l["from"], l["to"]) == (-1, -2)
    # every message crosses it: WG 1 (ST), 2, 10, 11, 12, 13 (AVB), camera, logging (BE)
    rate = (Fraction(93, 500) + Fraction(175, 1000) + Fraction(123, 1000) + Fraction(132, 2000)
            + Fraction(95, 2000) + Fraction(241, 10000) + Fraction(1542, 1000) + Fraction(600, 5000))
    assert len(l["messages"]) == 8
    assert l["bytes_per_us"] == rate
    assert l["utilization"] == rate / Fraction(25, 2)
    assert l["ST"] == Fraction(93, 500) and l["BE"] == Fraction(1542, 1000) + Fraction(600, 5000)
    assert sum(l[c] for c in link_report.CLASSES) == rate


def test_every_architecture_row_listed():
    s = ex1()
    links = link_report.link_utilization(s)
    assert [(l["from"], l["to"]) for l in links] == s.network_config.architecture
    assert links[1]["messages"] == [] and links[1]["utilization"] == 0     # -1 -> 1 carries nothing


def test_csv_files(tmp_path):
    link_report.write_report(ex1(), str(tmp_path))
    rows = (tmp_path / "link_utilization.csv").read_text().splitlines()
    assert len(rows) == 1 + 14
    assert rows[7].startswith("7,-1,-2,8,") and rows[7].endswith(",18.2688")
    detail = (tmp_path / "link_utilization_messages.csv").read_text().splitlines()
    assert sum(1 for r in detail[1:] if r.startswith("7,")) == 8
