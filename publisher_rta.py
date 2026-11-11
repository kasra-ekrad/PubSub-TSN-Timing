"""Publisher response-time analysis (fixed-priority non-preemptive, A8).

Implements, per WG transaction Gamma_{p,n} of a publisher E_p:
    W_{p,n}      Eq. (demand)
    B_{p,n}      Eq. (blocking)     0 if lp(p,n) is empty
    L_{p,n}      Eq. (busy)         ceil form, start value B + W
    Q_{p,n}      Eq. (instances)
    w_{p,n}(q)   Eq. (waiting)      floor + 1 (closed window), start value B + qW
    R_{p,n}(q)   Eq. (respq)
    R_{p,n}      Eq. (resp)
Schedulable iff R_{p,n} <= T_{p,n}.

Priority order: WGs whose NM is ST come before all other WGs of the publisher;
within each group, larger rho is higher priority, and for equal rho the
smaller WriterGroupId is higher priority (A13).

Usage:
    python publisher_rta.py example_system.yaml
    python publisher_rta.py example_system.yaml --system ex1 --instances --json out.json
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from fractions import Fraction
from typing import Callable

from system import Publisher, System, WriterGroup, load_systems

MAX_ITER = 1_000_000


def ceil_div(a: int, b: int) -> int:
    return -(-a // b)


def priority_key(wg: WriterGroup) -> tuple[bool, int, int]:
    """Total order: ST WGs first, then rho, then smaller WriterGroupId (A13).
    A larger key means higher priority."""
    return (wg.st, wg.rho, -wg.writer_group_id)


def hp(pub: Publisher, wg: WriterGroup) -> list[WriterGroup]:
    return [m for m in pub.wgs if priority_key(m) > priority_key(wg)]


def lp(pub: Publisher, wg: WriterGroup) -> list[WriterGroup]:
    return [m for m in pub.wgs if priority_key(m) < priority_key(wg)]


def fixed_point(f: Callable[[int], int], start: int) -> int:
    """Smallest x >= start with x = f(x); f must be monotone non-decreasing."""
    x = start
    for _ in range(MAX_ITER):
        nxt = f(x)
        if nxt == x:
            return x
        x = nxt
    raise RuntimeError("fixed-point iteration did not converge")


@dataclass
class WGResult:
    p: int
    n: int
    writer_group_id: int
    rho: int
    T: int
    W: int
    B: int
    U: str                    # utilization of hp(p,n) and n, exact fraction
    status: str               # "ok" | "deadline_miss" | "overload"
    L: int | None = None
    Q: int | None = None
    w: list[int] | None = None        # w_{p,n}(q), q = 0..Q-1
    R_q: list[int] | None = None      # R_{p,n}(q)
    R: int | None = None
    worst_q: int | None = None
    st: bool = False          # NM is ST: priority over all non-ST WGs


def analyse_wg(pub: Publisher, wg: WriterGroup) -> WGResult:
    higher = hp(pub, wg)
    B = max((m.W for m in lp(pub, wg)), default=0)               # Eq. (blocking)
    level = higher + [wg]
    U = sum((Fraction(m.W, m.T) for m in level), Fraction(0))

    res = WGResult(p=pub.p, n=wg.n, writer_group_id=wg.writer_group_id, rho=wg.rho,
                   T=wg.T, W=wg.W, B=B, U=str(U), status="overload", st=wg.st)
    if U >= 1:
        # Eq. (busy) has no finite solution; the WG is not schedulable.
        return res

    L = fixed_point(lambda L: B + sum(ceil_div(L, m.T) * m.W for m in level),
                    B + wg.W)                                      # Eq. (busy)
    Q = ceil_div(L, wg.T)                                          # Eq. (instances)

    w_list, R_list = [], []
    for q in range(Q):
        base = B + q * wg.W
        w = fixed_point(lambda w: base + sum((w // m.T + 1) * m.W for m in higher),
                        base)                                      # Eq. (waiting)
        w_list.append(w)
        R_list.append(w - q * wg.T + wg.W)                         # Eq. (respq)

    R = max(R_list)                                                # Eq. (resp)
    res.L, res.Q, res.w, res.R_q, res.R = L, Q, w_list, R_list, R
    res.worst_q = R_list.index(R)
    res.status = "ok" if R <= wg.T else "deadline_miss"
    return res


def analyse_publisher(pub: Publisher) -> list[WGResult]:
    return [analyse_wg(pub, wg) for wg in pub.wgs]


def analyse_system(system: System) -> dict[int, list[WGResult]]:
    return {pub.p: analyse_publisher(pub) for pub in system.publishers}


def _fmt(x) -> str:
    """Integers as is, exact fractions as decimals (up to 6 places)."""
    if x is None:
        return "-"
    x = Fraction(x)
    if x.denominator == 1:
        return f"{int(x):,}".replace(",", " ")
    return f"{float(x):,.6f}".rstrip("0").replace(",", " ")


def json_default(o):
    """json.dump hook: exact fractions become int or float."""
    if isinstance(o, Fraction):
        return int(o) if o.denominator == 1 else float(o)
    raise TypeError(f"not JSON serializable: {type(o).__name__}")


def print_report(system: System, results: dict[int, list[WGResult]], instances: bool) -> None:
    print(f"\nSystem '{system.name}'  (times in us)")
    for p, rows in results.items():
        print(f"\n  Publisher E_{p}")
        hdr = f"    {'n':>3} {'WGId':>5} {'ST':>3} {'rho':>4} {'T':>12} {'W':>10} {'B':>10} " \
              f"{'L':>12} {'Q':>3} {'R':>12} {'R/T':>6} {'q*':>3}  status"
        print(hdr)
        for r in sorted(rows, key=lambda r: (not r.st, -r.rho, r.writer_group_id)):
            ratio = "-" if r.R is None else f"{float(r.R / r.T):.3f}"
            st = "ST" if r.st else ""
            print(f"    {r.n:>3} {r.writer_group_id:>5} {st:>3} {r.rho:>4} {_fmt(r.T):>12} "
                  f"{_fmt(r.W):>10} {_fmt(r.B):>10} {_fmt(r.L):>12} {_fmt(r.Q):>3} "
                  f"{_fmt(r.R):>12} {ratio:>6} {_fmt(r.worst_q):>3}  {r.status}")
            if instances and r.w is not None:
                for q, (w, Rq) in enumerate(zip(r.w, r.R_q)):
                    print(f"          q={q}: w={_fmt(w)}  R(q)={_fmt(Rq)}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Publisher response-time analysis")
    ap.add_argument("file", help="YAML/JSON file with one or more systems")
    ap.add_argument("--system", help="analyse only the system with this name")
    ap.add_argument("--instances", action="store_true", help="print w(q) and R(q) per instance")
    ap.add_argument("--json", metavar="OUT", help="write all results to a JSON file")
    args = ap.parse_args(argv)

    systems = load_systems(args.file)
    if args.system:
        systems = [s for s in systems if s.name == args.system]
        if not systems:
            ap.error(f"no system named {args.system!r}")

    out = {}
    all_ok = True
    for s in systems:
        results = analyse_system(s)
        print_report(s, results, args.instances)
        out[s.name] = {str(p): [asdict(r) for r in rows] for p, rows in results.items()}
        all_ok &= all(r.status == "ok" for rows in results.values() for r in rows)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"time_unit": "us", "systems": out}, f, indent=2, default=json_default)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
