"""Full analysis: publishers, network jitter, subscribers and end-to-end delay.

Usage:
    python analyse.py example_system.yaml
    python analyse.py example_system.yaml --system jitter_demo --instances --json out.json
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from publisher_rta import _fmt, json_default, print_report
from subscriber_rta import analyse_all
from system import System, load_systems


def print_network(system: System) -> None:
    if not system.network:
        return
    print("\n  Network messages m_g  (NM and length in bytes)")
    print(f"    {'WGId':>5} {'class':>5} {'path':>18} {'h':>2} {'NM':>5} {'length':>6} {'C_N':>9} "
          f"{'R_max':>10} {'R_min':>10} {'J':>10}")
    for wgid, m in sorted(system.network.items()):
        path = " ".join(str(x) for x in m.path)
        R_max, R_min = (None, None) if m.from_schedule else (m.R_max, m.R_min)
        print(f"    {wgid:>5} {m.traffic_class:>5} {path:>18} {m.h:>2} {m.size.nm:>5} {m.length:>6} "
              f"{_fmt(m.C_N):>9} {_fmt(R_max):>10} {_fmt(R_min):>10} {_fmt(m.J):>10}")
    st = [(g, m) for g, m in sorted(system.network.items()) if m.from_schedule]
    if st:
        print("\n  ST messages from the schedule (relative to the period start)")
        print(f"    {'WGId':>5} {'instances':>9} {'first gate':>11} {'A_min':>10} {'A_max':>10} {'J':>10}")
        for g, m in st:
            print(f"    {g:>5} {m.st_instances:>9} {_fmt(m.st_start_min):>11} {_fmt(m.st_arrival_min):>10} "
                  f"{_fmt(m.st_arrival_max):>10} {_fmt(m.J):>10}")


def network_json(system: System) -> dict:
    out = {}
    for g, m in sorted(system.network.items()):
        d = {"class": m.traffic_class, "hard_real_time": m.hard_real_time,
             "source": m.source, "destination": m.destination, "path": list(m.path),
             "route_index": m.route_index, "h": m.h, "nm_bytes": m.size.nm,
             "frame_bytes": m.size.frame, "wire_bytes": m.size.wire, "length": m.length,
             "C_N": m.C_N, "J": m.J}
        if m.from_schedule:
            d.update(st_instances=m.st_instances, st_first_gate_min=m.st_start_min,
                     st_arrival_min=m.st_arrival_min, st_arrival_max=m.st_arrival_max)
        else:
            d.update(R_max=m.R_max, R_min=m.R_min, network_deadline=m.network_deadline)
        out[str(g)] = d
    return out


def require_R_max(system: System) -> None:
    """analyse.py alone takes the network delays from the YAML."""
    missing = [g for g, m in system.network.items() if m.R_max is None and not m.from_schedule]
    if missing:
        raise SystemExit(f"system {system.name!r}: no R_max in the YAML for WG {sorted(missing)}; "
                         f"add it, or use run_all.py to take it from the network tools")


def print_subscribers(sub_results, instances: bool) -> None:
    for k, rows in sub_results.items():
        print(f"\n  Subscriber E'_{k}")
        print(f"    {'m':>3} {'WGId':>5} {'from':>4} {'rho':>4} {'T':>12} {'J':>10} {'W':>10} "
              f"{'B':>10} {'qmax':>4} {'R_s':>12} {'R/T':>6} {'q*':>3}  status")
        for r in sorted(rows, key=lambda r: (-r.rho, r.writer_group_id)):
            ratio = "-" if r.R is None else f"{float(r.R / r.T):.3f}"
            print(f"    {r.m:>3} {r.writer_group_id:>5} {'E' + str(r.p):>4} {r.rho:>4} {_fmt(r.T):>12} "
                  f"{_fmt(r.J):>10} {_fmt(r.W):>10} {_fmt(r.B):>10} {_fmt(r.q_max):>4} "
                  f"{_fmt(r.R):>12} {ratio:>6} {_fmt(r.worst_q):>3}  {r.status}")
            if instances and r.omega is not None:
                for q, (w, Rq) in enumerate(zip(r.omega, r.R_q), start=1):
                    print(f"          q={q}: omega={_fmt(w)}  R(q)={_fmt(Rq)}")


def print_e2e(e2e) -> None:
    if not e2e:
        return
    print("\n  End-to-end  R = R^p + R^max + R^s - J'   (J' is in both R^max and R^s; "
          "ST from the schedule: R^max = A_max - R^p)")
    print(f"    {'WGId':>5} {'path':>10} {'class':>5} {'R^p':>12} {'R^max':>12} {'R^s':>12} "
          f"{'J':>10} {'R':>12}  pub / net / sub status")
    for e in sorted(e2e, key=lambda e: e.writer_group_id):
        path = f"E{e.p}->E'{e.k}"
        print(f"    {e.writer_group_id:>5} {path:>10} {e.traffic_class:>5} {_fmt(e.R_p):>12} "
              f"{_fmt(e.R_max):>12} {_fmt(e.R_s):>12} {_fmt(e.J):>10} {_fmt(e.R):>12}  "
              f"{e.pub_status} / {e.net_status or '-'} / {e.sub_status}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Publisher, subscriber and end-to-end analysis")
    ap.add_argument("file", help="YAML/JSON file with one or more systems")
    ap.add_argument("--system", help="analyse only the system with this name")
    ap.add_argument("--instances", action="store_true", help="print per-instance values")
    ap.add_argument("--json", metavar="OUT", help="write all results to a JSON file")
    args = ap.parse_args(argv)

    systems = load_systems(args.file)
    if args.system:
        systems = [s for s in systems if s.name == args.system]
        if not systems:
            ap.error(f"no system named {args.system!r}")

    out, all_ok = {}, True
    for s in systems:
        require_R_max(s)
        pub, sub, e2e = analyse_all(s)
        print_report(s, pub, args.instances)
        print_network(s)
        print_subscribers(sub, args.instances)
        print_e2e(e2e)
        for w in s.warnings:
            print(f"  warning: {w}", file=sys.stderr)
        out[s.name] = {
            "publishers": {str(p): [asdict(r) for r in rows] for p, rows in pub.items()},
            "network": network_json(s),
            "subscribers": {str(k): [asdict(r) for r in rows] for k, rows in sub.items()},
            "e2e": [asdict(e) for e in e2e],
        }
        all_ok &= all(r.status == "ok" for rows in pub.values() for r in rows)
        all_ok &= all(r.status == "ok" for rows in sub.values() for r in rows)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"time_unit": "us", "systems": out}, f, indent=2, default=json_default)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
