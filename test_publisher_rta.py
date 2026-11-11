"""Tests for the publisher RTA.

Besides hand-calculated cases, the analysis is cross-checked against a
small non-preemptive fixed-priority simulator:
  * exactness: in the critical-instant scenario (blocker started at 0^-,
    hp and n released at 0), the simulated worst response over the
    instances released in [0, L) equals R_{p,n};
  * soundness: with random release offsets for all WGs, no simulated job
    exceeds its R_{p,n}.
Run with:  python -m pytest -q
"""
import heapq
import random

import pytest

from publisher_rta import analyse_publisher, analyse_wg, priority_key
from system import ConfigError, Publisher, WriterGroup, load_systems


def wg(n, T, rho, W, wgid=None):
    # Split W over the four tasks; only the sum matters for the analysis.
    return WriterGroup(n=n, writer_group_id=wgid or n, T=T, rho=rho,
                       psi=(W,), phi=(0,), C3=0, C4=0)


# ---------------------------------------------------------------- simulator

def simulate(wgs, offsets, horizon, start=0):
    """Non-preemptive FP on one core. Releases with time < horizon.
    The core is busy until `start` (a blocker that began at 0^-).
    Returns {n: [(release, response), ...]}."""
    releases = [(offsets[g.n] + k * g.T, g)
                for g in wgs
                for k in range((horizon - offsets[g.n] + g.T - 1) // g.T)]
    releases.sort(key=lambda x: x[0])
    out = {g.n: [] for g in wgs}
    ready, i, t = [], 0, start
    while i < len(releases) or ready:
        while i < len(releases) and releases[i][0] <= t:
            r, g = releases[i]
            key = priority_key(g)
            heapq.heappush(ready, (tuple(-x for x in key), r, g.n, g))
            i += 1
        if not ready:
            t = releases[i][0]
            continue
        *_, r, _, g = heapq.heappop(ready)
        t += g.W
        out[g.n].append((r, t - r))
    return out


# ------------------------------------------------------- hand-calculated cases

DAVIS = Publisher(p=1, wgs=(wg(1, 25, 3, 10), wg(2, 35, 2, 10), wg(3, 35, 1, 10)))


def test_davis_highest():
    r = analyse_wg(DAVIS, DAVIS.wgs[0])
    assert (r.B, r.L, r.Q, r.R) == (10, 20, 1, 20)


def test_davis_middle():
    r = analyse_wg(DAVIS, DAVIS.wgs[1])
    assert (r.B, r.L, r.Q, r.w, r.R_q, r.R) == (10, 50, 2, [20, 40], [30, 15], 30)


def test_davis_lowest_worst_case_at_second_instance():
    r = analyse_wg(DAVIS, DAVIS.wgs[2])
    assert r.B == 0                      # lp(p,n) is empty
    assert (r.L, r.Q, r.w, r.R_q) == (70, 2, [20, 60], [30, 35])
    assert r.R == 35 and r.worst_q == 1
    assert r.status == "ok"              # R == T counts as schedulable


def test_W_is_sum_of_all_four_tasks():
    g = WriterGroup(n=1, writer_group_id=1, T=100, rho=1,
                    psi=(1, 2, 3), phi=(4, 5), C3=6, C4=7)
    assert (g.v, g.Delta, g.C1, g.C2, g.W) == (3, 2, 6, 9, 28)


def test_tie_break_smaller_writer_group_id_wins():
    a, b = wg(1, 100, 5, 10, wgid=20), wg(2, 100, 5, 30, wgid=7)
    pub = Publisher(p=1, wgs=(a, b))
    ra, rb = analyse_publisher(pub)
    assert rb.B == 10 and rb.R == 40     # b (WGId 7) is higher: blocked by a
    assert ra.B == 0 and ra.R == 40      # a waits for b


def test_overload():
    pub = Publisher(p=1, wgs=(wg(1, 10, 2, 6), wg(2, 10, 1, 4)))
    r = analyse_wg(pub, pub.wgs[1])
    assert r.status == "overload" and r.R is None and r.U == "1"


def test_deadline_miss():
    pub = Publisher(p=1, wgs=(wg(1, 10, 2, 2), wg(2, 100, 1, 9)))
    r = analyse_wg(pub, pub.wgs[0])      # blocked 9, then runs 2
    assert r.R == 11 and r.status == "deadline_miss"


def test_example_file_loads():
    systems = load_systems("example_system.yaml")
    assert [s.name for s in systems] == ["ex1", "davis", "jitter_demo"]


def test_decimals_are_exact(tmp_path):
    from fractions import Fraction
    f = tmp_path / "dec.yaml"
    f.write_text("systems:\n- name: x\n  publishers:\n  - p: 1\n    writer_groups:\n"
                 "    - {n: 1, writer_group_id: 1, T: 0.3, rho: 1, psi: [0.1, 0.2], phi: [0], C3: 0, C4: 0}\n")
    (s,) = load_systems(str(f))
    wg = s.publishers[0].wgs[0]
    assert wg.W == Fraction(3, 10) == wg.T          # 0.1 + 0.2 == 0.3 exactly (not in floats)
    r = analyse_wg(s.publishers[0], wg)
    assert r.U == "1" and r.status == "overload"    # boundary U = 1 detected exactly


def test_non_number_rejected(tmp_path):
    f = tmp_path / "bad.yaml"
    f.write_text("systems:\n- name: x\n  publishers:\n  - p: 1\n    writer_groups:\n"
                 "    - {n: 1, writer_group_id: 1, T: fast, rho: 1, psi: [1], phi: [1], C3: 1, C4: 1}\n")
    with pytest.raises(ConfigError, match="T"):
        load_systems(str(f))


# ------------------------------------------------------ simulator cross-checks

def random_publisher(rng):
    k = rng.randint(1, 6)
    periods = [rng.choice([20, 30, 40, 50, 60, 75, 100, 120, 150]) for _ in range(k)]
    target_u = rng.uniform(0.3, 0.95)
    wgs = []
    for i, T in enumerate(periods, start=1):
        W = max(1, int(T * target_u / k * rng.uniform(0.3, 1.7)))
        wgs.append(wg(i, T, rng.randint(1, 3), W, wgid=rng.randint(1, 1000) * 10 + i))
    return Publisher(p=1, wgs=tuple(wgs))


@pytest.mark.parametrize("seed", range(400))
def test_exact_in_critical_instant_scenario(seed):
    rng = random.Random(seed)
    pub = random_publisher(rng)
    for g in pub.wgs:
        r = analyse_wg(pub, g)
        if r.status == "overload":
            continue
        level = [m for m in pub.wgs if priority_key(m) >= priority_key(g)]
        sim = simulate(level, {m.n: 0 for m in level}, horizon=r.L, start=r.B)
        assert len(sim[g.n]) == r.Q
        assert [resp for _, resp in sim[g.n]] == r.R_q
        assert max(resp for _, resp in sim[g.n]) == r.R


@pytest.mark.parametrize("seed", range(200))
def test_sound_with_random_offsets(seed):
    rng = random.Random(10_000 + seed)
    pub = random_publisher(rng)
    results = {r.n: r for r in analyse_publisher(pub)}
    if any(r.status == "overload" for r in results.values()):
        return
    offsets = {g.n: rng.randrange(g.T) for g in pub.wgs}
    sim = simulate(pub.wgs, offsets, horizon=40 * max(g.T for g in pub.wgs))
    for n, jobs in sim.items():
        assert max(resp for _, resp in jobs) <= results[n].R
