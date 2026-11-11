"""Tests for the subscriber priority-queue RTA with jitter and the e2e delay.

Cross-checks:
  * with J' = 0 the subscriber analysis equals the publisher analysis
    (which is itself exact against the simulator);
  * R^s equals the busy-period form of Davis et al. (2007) with jitter;
  * soundness: a non-preemptive priority-queue simulator with jittered NM
    arrivals never exceeds R^s.
Run with:  python -m pytest -q
"""
import heapq
import random
import textwrap

import pytest

from publisher_rta import analyse_wg, ceil_div, fixed_point
from subscriber_rta import analyse_all, analyse_subscriber, priority_key
from system import ConfigError, Publisher, Subscriber, SubTransaction, WriterGroup, load_systems


def st(m, T, rho, W, wgid=None):
    return SubTransaction(m=m, writer_group_id=wgid or m, C1=W, C2=0, phi=(0,), psi=(0,),
                          T=T, rho=rho, p=1)


# ------------------------------------------------------- hand-calculated cases

def test_jitter_demo_by_hand():
    s = {x.name: x for x in load_systems("example_system.yaml")}["jitter_demo"]
    pub, sub, e2e = analyse_all(s)
    rows = {r.writer_group_id: r for r in sub[1]}
    assert (rows[1].J, rows[1].B, rows[1].omega, rows[1].R) == (10, 6, [6], 20)
    assert (rows[2].J, rows[2].B, rows[2].omega, rows[2].R) == (0, 6, [14], 17)
    assert (rows[3].J, rows[3].B, rows[3].omega, rows[3].R) == (0, 0, [7], 13)
    assert {e.writer_group_id: e.R for e in e2e} == {1: 35, 2: 24, 3: 24}


def test_jitter_adds_second_interfering_nm():
    hi, lo = st(1, 20, 2, 4), st(2, 100, 1, 6)
    sub = Subscriber(k=1, transactions=(hi, lo))
    no_j = {r.m: r.R for r in analyse_subscriber(sub, {1: 0, 2: 0})}
    with_j = {r.m: r.R for r in analyse_subscriber(sub, {1: 16, 2: 0})}
    assert no_j[2] == 4 + 6
    assert with_j[2] == 8 + 6          # floor((4 + 16) / 20) + 1 = 2 NMs of WG 1


def test_own_jitter_adds_to_response():
    sub = Subscriber(k=1, transactions=(st(1, 50, 1, 5),))
    (r,) = analyse_subscriber(sub, {1: 12})
    assert (r.q_max, r.R) == (1, 12 + 5)


def test_overload():
    sub = Subscriber(k=1, transactions=(st(1, 10, 2, 6), st(2, 10, 1, 4)))
    r = analyse_subscriber(sub, {1: 0, 2: 0})[1]
    assert r.status == "overload" and r.R is None


def test_tie_break_by_writer_group_id():
    a, b = st(1, 100, 5, 10, wgid=20), st(2, 100, 5, 30, wgid=7)
    ra, rb = analyse_subscriber(Subscriber(k=1, transactions=(a, b)), {20: 0, 7: 0})
    assert rb.B == 10 and ra.B == 0     # WGId 7 is higher priority


# ------------------------------------------------------------------ loader

BASE = """
time_unit: us
systems:
- name: x
  publishers:
  - p: 1
    node: 1
    writer_groups:
    - {n: 1, writer_group_id: 1, T: 100, rho: 1, types: [TYPE1], psi: [1], phi: [1], C3: 1, C4: 1}
  - p: 2
    node: 2
    writer_groups:
    - {n: 1, writer_group_id: WG2, T: 100, rho: 1, types: [Boolean], psi: [1], phi: [1], C3: 1, C4: 1}
  subscribers:
  - k: 1
    node: 3
    transactions:
    - {m: 1, writer_group_id: 1, C1: 1, C2: 1, phi: [1], psi: [1]}
  network:
    links: [[1, -1], [2, -1], [3, -1]]
    bandwidth: 91
    epsilon: 1
    max_link_load: LOAD
    messages:
    - MSG1
"""
# A Boolean-only NM is 21 bytes, 91 bytes on the wire, so C_N = 1 us at 91 bytes/us.


def load(tmp_path, wg2="2", msg1="{writer_group_id: 1, hard_real_time: false, avb_class: A, R_max: 20}",
         type1="Boolean", load_pct="100"):
    f = tmp_path / "s.yaml"
    f.write_text(BASE.replace("WG2", wg2).replace("MSG1", msg1).replace("TYPE1", type1)
                 .replace("LOAD", load_pct))
    return load_systems(str(f))


def test_loader_ok_and_R_min(tmp_path):
    (s,) = load(tmp_path)
    m = s.network[1]
    assert (m.path, m.h, m.length, m.C_N) == ((1, -1, 3), 2, 91, 1)
    assert (m.R_min, m.J) == (2 * 1 + 1 * 1, 20 - 3)


def test_writer_group_id_unique_across_publishers(tmp_path):
    with pytest.raises(ConfigError, match="duplicate writer_group_id"):
        load(tmp_path, wg2="1")


def test_R_max_below_R_min(tmp_path):
    with pytest.raises(ConfigError, match="R_max"):
        load(tmp_path, msg1="{writer_group_id: 1, hard_real_time: false, avb_class: A, R_max: 2}")


def test_jitter_above_period(tmp_path):
    with pytest.raises(ConfigError, match="exceeds T"):
        load(tmp_path, msg1="{writer_group_id: 1, hard_real_time: false, avb_class: A, R_max: 200}")


def test_st_has_zero_jitter(tmp_path):
    (s,) = load(tmp_path, msg1="{writer_group_id: 1, hard_real_time: true, R_max: 50}")
    assert s.network[1].J == 0 and s.network[1].traffic_class == "ST"


def test_message_for_wg_without_subscriber(tmp_path):
    with pytest.raises(ConfigError, match="not received by any subscriber"):
        load(tmp_path, msg1="{writer_group_id: 2, hard_real_time: true, R_max: 50}")


def test_hard_real_time_must_be_boolean(tmp_path):
    with pytest.raises(ConfigError, match="true or false"):
        load(tmp_path, msg1="{writer_group_id: 1, hard_real_time: 1, R_max: 50}")


def test_path_not_a_route(tmp_path):
    with pytest.raises(ConfigError, match="not a route"):
        load(tmp_path, msg1="{writer_group_id: 1, hard_real_time: true, R_max: 50, path: [1, -1, 2]}")


def test_link_overload(tmp_path):
    # 91 bytes per 100 us = 0.91 bytes/us = 1 % of 91 bytes/us
    load(tmp_path, load_pct="1")
    with pytest.raises(ConfigError, match="max_link_load"):
        load(tmp_path, load_pct="0.99")


def test_mtu_exceeded(tmp_path):
    with pytest.raises(ConfigError, match="MTU"):
        load(tmp_path, type1="String(1500)")


def test_wg_received_twice(tmp_path):
    f = tmp_path / "s.yaml"
    text = BASE.replace("WG2", "2").replace("TYPE1", "Boolean").replace("LOAD", "100").replace(
        "MSG1", "{writer_group_id: 1, hard_real_time: true, R_max: 5}")
    text = text.replace("  network:", "  - k: 2\n    node: 2\n    transactions:\n"
                        "    - {m: 1, writer_group_id: 1, C1: 1, C2: 1, phi: [1], psi: [1]}\n  network:")
    f.write_text(text)
    with pytest.raises(ConfigError, match="receiving transaction"):
        load_systems(str(f))


# ------------------------------------------------------ random cross-checks

def random_subscriber(rng, jitter=True):
    k = rng.randint(1, 6)
    u = rng.uniform(0.3, 0.97)
    ts, J = [], {}
    for i in range(1, k + 1):
        T = rng.choice([20, 24, 30, 40, 50, 60, 75, 100, 120])
        W = max(1, int(T * u / k * rng.uniform(0.3, 1.7)))
        wgid = rng.randint(1, 1000) * 10 + i
        ts.append(st(i, T, rng.randint(1, 3), W, wgid=wgid))
        J[wgid] = rng.choice([0, T, rng.randint(0, T)]) if jitter else 0
    return Subscriber(k=1, transactions=tuple(ts)), J


def davis_R(sub, t, J):
    """Davis et al. (2007), busy-period form with jitter, 0-based q."""
    key = priority_key(t)
    H = [x for x in sub.transactions if priority_key(x) > key]
    B = max((x.W for x in sub.transactions if priority_key(x) < key), default=0)
    lvl = H + [t]
    Jx = lambda x: J[x.writer_group_id]
    L = fixed_point(lambda L: B + sum(ceil_div(L + Jx(x), x.T) * x.W for x in lvl), B + t.W)
    Q = ceil_div(L + Jx(t), t.T)
    R = 0
    for q in range(Q):
        w = fixed_point(lambda w: B + q * t.W + sum(((w + Jx(x)) // x.T + 1) * x.W for x in H),
                        B + q * t.W)
        R = max(R, Jx(t) + w - q * t.T + t.W)
    return R


@pytest.mark.parametrize("seed", range(300))
def test_zero_jitter_equals_publisher_analysis(seed):
    sub, J = random_subscriber(random.Random(seed), jitter=False)
    pub = Publisher(p=1, wgs=tuple(
        WriterGroup(n=t.m, writer_group_id=t.writer_group_id, T=t.T, rho=t.rho,
                    psi=(t.W,), phi=(0,), C3=0, C4=0) for t in sub.transactions))
    for s, wg in zip(analyse_subscriber(sub, J), pub.wgs):
        p = analyse_wg(pub, wg)
        assert (s.status, s.R, s.B) == (p.status, p.R, p.B)


@pytest.mark.parametrize("seed", range(300))
def test_equals_davis_busy_period_form(seed):
    sub, J = random_subscriber(random.Random(1000 + seed))
    for r, t in zip(analyse_subscriber(sub, J), sub.transactions):
        if r.status != "overload":
            assert r.R == davis_R(sub, t, J)


def simulate(sub, J, offsets, jit, horizon):
    """Non-preemptive priority queue. NM k of t arrives at offset + k T + jit, jit in [0, J].
    Response is measured from the jitter-free arrival offset + k T."""
    arr = []
    for t in sub.transactions:
        k = 0
        while offsets[t.m] + k * t.T < horizon:
            nom = offsets[t.m] + k * t.T
            arr.append((nom + jit(J[t.writer_group_id]), t.m, nom, t))
            k += 1
    arr.sort(key=lambda a: (a[0], a[1]))
    worst = {t.m: 0 for t in sub.transactions}
    ready, i, now = [], 0, 0
    while i < len(arr) or ready:
        while i < len(arr) and arr[i][0] <= now:
            _, m, nom, t = arr[i]
            key = priority_key(t)
            heapq.heappush(ready, (-key[0], -key[1], nom, m, t))
            i += 1
        if not ready:
            now = arr[i][0]
            continue
        *_, nom, m, t = heapq.heappop(ready)
        now += t.W
        worst[m] = max(worst[m], now - nom)
    return worst


@pytest.mark.parametrize("seed", range(300))
def test_sound_against_simulator(seed):
    rng = random.Random(5000 + seed)
    sub, J = random_subscriber(rng)
    res = {r.m: r for r in analyse_subscriber(sub, J)}
    if any(r.status == "overload" for r in res.values()):
        return
    horizon = 30 * max(t.T for t in sub.transactions)
    for _ in range(10):
        offsets = {t.m: rng.randrange(t.T) for t in sub.transactions}
        extreme = rng.random() < 0.5
        jit = (lambda j: rng.choice([0, j])) if extreme else (lambda j: rng.randint(0, j))
        for m, w in simulate(sub, J, offsets, jit, horizon).items():
            assert w <= res[m].R
