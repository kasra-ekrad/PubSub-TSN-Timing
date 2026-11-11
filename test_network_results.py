"""Tests for reading the ST schedule (network_results.st_window_stats)."""
from fractions import Fraction

import numpy as np
import pytest

from mapping_tool import MappingError
from network_results import st_window_stats


def schedule(rows):
    """rows: (message, link, open_ns, close_ns) -> Schedule.txt matrix (queue column 0)."""
    return np.array([[m, l, 0, o, c] for m, l, o, c in rows], dtype=float)


def test_different_window_counts_per_link():
    # Message 0, T = 100 us, path over links 3 -> 7 -> 10. Link 3 is shared with a
    # T = 5000 us message, so its cycle is 5000 us (50 windows); links 7 and 10 only
    # carry message 0 (cycle 100 us, 1 window). Same situation as WG 6 in generated.yaml.
    rows = [(0, 3, k * 100_000 + 62_080, k * 100_000 + 74_640) for k in range(50)]
    rows += [(0, 7, 74_640, 87_200), (0, 10, 87_200, 100_000)]
    st = st_window_stats(schedule(rows), 0, [3, 7, 10], Fraction(100))
    assert st == {"A_max": 100, "A_min": 100, "s_min": Fraction("62.08"), "instances": 1}


def test_arrivals_vary_between_periods():
    # Last link: two windows in a 1000 us cycle, at different places in their periods
    rows = [(0, 3, 400_000, 410_000), (0, 3, 900_000, 910_000),
            (0, 6, 470_000, 480_000), (0, 6, 990_000, 1_000_000)]
    st = st_window_stats(schedule(rows), 0, [3, 6], Fraction(500))
    assert (st["A_min"], st["A_max"], st["s_min"]) == (480, 500, 400)   # J' = 20 us


def test_window_order_in_file_does_not_matter():
    rows = [(0, 6, 990_000, 1_000_000), (0, 3, 900_000, 910_000),
            (0, 6, 470_000, 480_000), (0, 3, 400_000, 410_000)]
    st = st_window_stats(schedule(rows), 0, [3, 6], Fraction(500))
    assert (st["A_min"], st["A_max"]) == (480, 500)


def test_missing_link():
    with pytest.raises(MappingError, match="last link 6"):
        st_window_stats(schedule([(0, 3, 0, 10_000)]), 0, [3, 6], Fraction(500))
