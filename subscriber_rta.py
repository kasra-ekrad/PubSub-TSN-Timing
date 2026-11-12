"""Subscriber response-time analysis (priority queue, non-preemptive) with jitter,
and the end-to-end delay.

Per subscriber transaction Gamma'_{k,m} (instances q = 1, ..., q_max):
    W'_{k,m}     sum of the four task WCETs
    B'_{k,m}     max W' over lp(k,m), 0 if lp(k,m) is empty
    U            utilization of hp(k,m) and m; U >= 1 -> overload
    omega'(q)    B' + sum_hp (floor((omega' + J_l) / T_l) + 1) W'_l + (q - 1) W'
                 iterated from omega' = 0
    q_max        smallest q >= 1 with
                 B' + sum_hp ceil((q T - J + J_l) / T_l) W'_l + q W' <= q T - J
    R'(q)        J + omega'(q) - (q - 1) T + W'
    R^s          max over 1 <= q <= q_max
Jitter J = R^max_g - R^min_g of the NM received (0 for ST).
Schedulable iff R^s <= T'.

End-to-end delay:
    R_{i,j} = R^p_{i,j} + R^max_g + R^s_{k,m} - J'_{k,m}
R^s is measured from the earliest possible arrival and so contains J'; R^max is
the latest arrival (= R^min + J'). Subtracting J' once counts the jitter once:
    AVB, or ST with R^max from the YAML:   R = R^p + R^min + R^s
    ST with the schedule (run_all.py):     R^max = A_max - R^p, J' = A_max - A_min,
                                           so R = A_min + R^s
        (A_max/A_min = latest/earliest arrival relative to the period start; valid
        if R^p <= the first gate opening, otherwise net_status = "st_not_ready")

Priority order: larger rho is higher priority (inherited from the sending WG);
equal rho is broken by the smaller WriterGroupId (unique in the system).
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from publisher_rta import MAX_ITER, WGResult, analyse_system, ceil_div, fixed_point
from system import Subscriber, SubTransaction, System


def priority_key(t: SubTransaction) -> tuple[int, int]:
    """A larger key means higher priority."""
    return (t.rho, -t.writer_group_id)


@dataclass
class SubResult:
    k: int
    m: int
    writer_group_id: int
    p: int
    rho: int
    T: int
    J: int
    W: int
    B: int
    U: str                    # utilization of hp(k,m) and m, exact fraction
    status: str               # "ok" | "deadline_miss" | "overload"
    q_max: int | None = None
    omega: list[int] | None = None    # omega'(q), q = 1..q_max (list index q - 1)
    R_q: list[int] | None = None      # R'(q),     q = 1..q_max (list index q - 1)
    R: int | None = None              # R^s_{k,m}
    worst_q: int | None = None        # 1-based, as in the paper


@dataclass
class E2EResult:
    writer_group_id: int
    p: int
    k: int
    m: int
    traffic_class: str
    R_p: int | None
    R_max: int | None         # network part; for ST from the schedule: A_max - R^p
    R_s: int | None
    R: int | None             # R^p + R^max + R^s - J
    pub_status: str
    sub_status: str
    net_status: str = ""      # "ok", "deadline_miss" (AVB analysis), "st_not_ready" (R^p > first gate)
    J: int | None = None      # J' of the NM, counted once in R


def analyse_transaction(sub: Subscriber, t: SubTransaction, J: dict[int, int]) -> SubResult:
    key = priority_key(t)
    higher = [x for x in sub.transactions if priority_key(x) > key]
    B = max((x.W for x in sub.transactions if priority_key(x) < key), default=0)
    U = sum((Fraction(x.W, x.T) for x in higher + [t]), Fraction(0))
    Jm = J[t.writer_group_id]

    res = SubResult(k=sub.k, m=t.m, writer_group_id=t.writer_group_id, p=t.p, rho=t.rho,
                    T=t.T, J=Jm, W=t.W, B=B, U=str(U), status="overload")
    if U >= 1:
        return res

    def omega(q: int) -> int:
        base = B + (q - 1) * t.W
        return fixed_point(
            lambda w: base + sum(((w + J[x.writer_group_id]) // x.T + 1) * x.W for x in higher), 0)

    def busy_period_ends(q: int) -> bool:
        lhs = B + sum(ceil_div(q * t.T - Jm + J[x.writer_group_id], x.T) * x.W for x in higher) \
              + q * t.W
        return lhs <= q * t.T - Jm

    q_max = 1
    while not busy_period_ends(q_max):
        q_max += 1
        if q_max > MAX_ITER:
            raise RuntimeError("q_max search did not terminate")

    om = [omega(q) for q in range(1, q_max + 1)]
    R_q = [Jm + om[q - 1] - (q - 1) * t.T + t.W for q in range(1, q_max + 1)]
    R = max(R_q)
    res.q_max, res.omega, res.R_q, res.R = q_max, om, R_q, R
    res.worst_q = R_q.index(R) + 1
    res.status = "ok" if R <= t.T else "deadline_miss"
    return res


def analyse_subscriber(sub: Subscriber, J: dict[int, int]) -> list[SubResult]:
    return [analyse_transaction(sub, t, J) for t in sub.transactions]


def analyse_subscribers(system: System) -> dict[int, list[SubResult]]:
    J = {wgid: msg.J for wgid, msg in system.network.items()}
    return {sub.k: analyse_subscriber(sub, J) for sub in system.subscribers}


def analyse_e2e(system: System, pub_results: dict[int, list[WGResult]],
                sub_results: dict[int, list[SubResult]]) -> list[E2EResult]:
    by_wg = {r.writer_group_id: r for rows in pub_results.values() for r in rows}
    out = []
    for rows in sub_results.values():
        for s in rows:
            pr = by_wg[s.writer_group_id]
            msg = system.network[s.writer_group_id]
            known = pr.R is not None and s.R is not None
            J = msg.J
            if msg.hard_real_time and msg.from_schedule:
                # A_max is measured from the period start: written as R^max = A_max - R^p
                # so that R = R^p + R^max + R^s - J = A_min + R^s.
                R_net = None if pr.R is None else msg.st_arrival_max - pr.R
                net = "ok" if pr.R is None or pr.R <= msg.st_start_min else "st_not_ready"
            else:
                R_net = msg.R_max
                net = ("" if msg.network_deadline is None else
                       "ok" if msg.R_max <= msg.network_deadline else "deadline_miss")
            R = pr.R + R_net + s.R - J if known else None
            out.append(E2EResult(writer_group_id=s.writer_group_id, p=s.p, k=s.k, m=s.m,
                                 traffic_class=msg.traffic_class, R_p=pr.R, R_max=R_net,
                                 R_s=s.R, R=R, pub_status=pr.status, sub_status=s.status,
                                 net_status=net, J=J))
    return out


def analyse_all(system: System):
    pub = analyse_system(system)
    sub = analyse_subscribers(system)
    return pub, sub, analyse_e2e(system, pub, sub)
