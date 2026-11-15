"""Simulation of an analysed system, to check the analysis against observed behaviour.

Inside the simulator, times are integer picoseconds (ps); inputs and outputs are in us.

What is simulated: the same model the analysis uses.
  publisher   each WG chain is one job of length W (its four tasks run back to back),
              released every T; one core per node, non-preemptive fixed priority in the
              analysis' order (ST first, then larger rho, then smaller WriterGroupId);
              jobs of one WG run in release order
  network     Level A (here): a delay box. The frame of an AVB instance arrives at
              a = f + d with d in [R^min, R^max]; frames of one stream keep their order.
              ST: the frame arrives at the gate-close time on its last link (relative to
              the start of its period), if it was handed over before its first gate opened.
              Level B (sim_tsn.py): ports with gates, credit-based shapers and queues.
  subscriber  the arrival releases the receiving chain (one job of length W'); one core,
              non-preemptive, with the priority of the sending WG

Per instance it records r (release), f (hand-over to the NIC), a (arrival at the
subscriber) and fs (subscriber chain done), and checks them against the analysis:
  publisher   f - r      <= R^p
  network     AVB: R^min <= a - f <= R^max;   ST: A_min <= a - r <= A_max
  subscriber  fs - nu    <= R^s, with nu = r + R^p + R^min (AVB) or r + A_min (ST): the
              nominal arrival from which the analysis measures R^s (which contains J')
  end-to-end  fs - r     <= R = (nu - r) + R^s
It also records the arrival spread max(a - r) - min(a - r), to compare with J'.
"""
from __future__ import annotations

import heapq
import math
import os
import random
from dataclasses import dataclass, field
from fractions import Fraction

from network_results import _csv, _exact
from avb_input_generator import read_numeric
from mapping_tool import link_ids
from publisher_rta import priority_key as pub_key
from subscriber_rta import priority_key as sub_key

PS = 10 ** 6          # ps per us
EPS = 1               # 1 ps: "just before"
MAX_H_PERIODS = 20    # a hyperperiod longer than 20 x the longest period is cut there


def ps(x) -> int:
    """us -> ps, rounded up (bounds round up, so rounding never makes a false violation)."""
    v = Fraction(x) * PS
    return -(-v.numerator // v.denominator)


def ps_floor(x) -> int:
    v = Fraction(x) * PS
    return v.numerator // v.denominator


def us(x: int | None) -> float | None:
    return None if x is None else x / PS


# --------------------------------------------------------------------- model

@dataclass
class Task:
    """One publisher WG: a job of length W every T."""
    g: int
    p: int            # publisher (Publisher.p)
    node: int
    T: int
    W: int
    st: bool
    rank: int         # 0 = highest priority on its publisher
    Rp: int | None = None   # the analysis' R^p


@dataclass
class Stream:
    """The NM of one WG through the network to its subscriber, with the analysis' bounds [ps]."""
    g: int
    p: int
    k: int            # subscriber (Subscriber.k)
    sub_node: int
    T: int
    Ws: int           # subscriber chain WCET W'
    sub_rank: int     # 0 = highest priority on its subscriber
    cls: str          # ST, AVB_A, AVB_B, AVB
    st_sched: bool    # ST with times from Schedule.txt
    length: int
    C: int            # transmission time on one link
    links: tuple[int, ...]
    dmin: int         # AVB: delay box [dmin, dmax] from the hand-off
    dmax: int
    # bounds (None if the analysis gives none)
    Rp: int | None
    Rs: int | None
    R: int | None
    J: int
    net_lo: int       # AVB: R^min (floor); ST: A_min, relative to the release
    net_hi: int       # AVB: R^max;         ST: A_max
    nu: int | None    # nominal arrival, relative to the release
    st_arr: list[int] = field(default_factory=list)    # ST: arrival per instance (j mod n)
    st_start: list[int] = field(default_factory=list)  # ST: first gate opening per instance
    st_windows: dict = field(default_factory=dict)     # ST: link -> [(open, close)] over n*T


@dataclass
class BEFlow:
    name: str
    node: int
    T: int
    length: int
    links: tuple[int, ...]


@dataclass
class Model:
    system: object
    tasks: dict[int, Task]
    streams: dict[int, Stream]
    pub_of: dict[int, list[int]]       # publisher -> WGs
    sub_of: dict[int, list[int]]       # subscriber -> WGs
    st_pubs: set[int]                  # publishers with an ST WG (synchronised to the schedule)
    be: list[BEFlow]
    H: int
    Tmax: int
    bandwidth: Fraction
    eps: int                           # switch delay


def tx_time(length: int, bandwidth: Fraction) -> int:
    return ps(Fraction(length) / bandwidth)


def st_schedule(folder: str, system) -> dict[int, dict]:
    """Per ST WG: arrival and first-gate offsets per instance, and its windows per link [ps].

    A window opening at o belongs to the period k = floor(o / T); relative to that period:
    arrival = close on the last link - kT, start = open on the first link - kT.
    The windows of a link repeat every n T (n = number of windows of the message on it)."""
    tt = [r for r in _csv(os.path.join(folder, "message_map.csv")) if r["class"] == "TT"]
    sched = read_numeric(os.path.join(folder, "Schedule.txt"), 5)
    arch = system.network_config.architecture
    wgs = system.writer_groups()
    out = {}
    for i, r in enumerate(tt):
        g = int(r["writer_group_id"])
        T = ps(wgs[g][1].T)
        links = link_ids(list(system.network[g].path), arch)
        windows = {}
        for link in links:
            rows = sched[(sched[:, 0] == i) & (sched[:, 1] == link)]
            windows[link] = sorted((int(_exact(o) * 1000), int(_exact(c) * 1000)) for o, c in rows[:, 3:5])
            if not windows[link]:
                raise ValueError(f"Schedule.txt: no window for WG {g} on link {link}")
        arr = [c - (o // T) * T for o, c in windows[links[-1]]]
        start = [o - (o // T) * T for o, c in windows[links[0]]]
        out[g] = {"arr": arr, "start": start, "windows": windows}
    return out


def build_model(system, pub, sub, e2e, folder: str | None = None) -> Model:
    """The simulation model, with the analysis results (publisher_rta / subscriber_rta) as bounds."""
    cfg = system.network_config
    tasks, pub_of, st_pubs = {}, {}, set()
    for P in system.publishers:
        order = sorted(P.wgs, key=pub_key, reverse=True)
        pub_of[P.p] = [w.writer_group_id for w in order]
        for rank, w in enumerate(order):
            tasks[w.writer_group_id] = Task(w.writer_group_id, P.p, P.node, ps(w.T), ps(w.W), w.st, rank)
            if w.st:
                st_pubs.add(P.p)
    pres = {r.writer_group_id: r for rows in pub.values() for r in rows}
    for g, t in tasks.items():
        t.Rp = None if pres[g].R is None else ps(pres[g].R)
    sres = {r.writer_group_id: r for rows in sub.values() for r in rows}
    eres = {e.writer_group_id: e for e in e2e}
    has_st_sched = any(m.from_schedule for m in system.network.values())
    st = st_schedule(folder, system) if folder and has_st_sched else {}
    arch = cfg.architecture if cfg else []

    streams, sub_of = {}, {}
    for S in system.subscribers:
        order = sorted(S.transactions, key=sub_key, reverse=True)
        sub_of[S.k] = [t.writer_group_id for t in order]
        for rank, t in enumerate(order):
            g = t.writer_group_id
            m = system.network[g]
            T = ps(t.T)
            Rp = None if pres[g].R is None else ps(pres[g].R)
            Rs = None if sres[g].R is None else ps(sres[g].R)
            R = None if eres[g].R is None else ps(eres[g].R)
            s = Stream(g=g, p=t.p, k=S.k, sub_node=S.node, T=T, Ws=ps(t.W), sub_rank=rank,
                       cls=m.traffic_class, st_sched=m.from_schedule, length=m.length,
                       C=tx_time(m.length, cfg.bandwidth), links=tuple(link_ids(list(m.path), arch)),
                       dmin=0, dmax=0, Rp=Rp, Rs=Rs, R=R, J=ps(m.J), net_lo=0, net_hi=0, nu=None)
            if m.from_schedule:
                x = st[g]
                s.st_arr, s.st_start, s.st_windows = x["arr"], x["start"], x["windows"]
                s.net_lo, s.net_hi = ps_floor(m.st_arrival_min), ps(m.st_arrival_max)
                s.nu = ps_floor(m.st_arrival_min)
            else:   # AVB, or ST with R_max given in the YAML (fixed delay)
                s.dmin, s.dmax = ps(m.R_min), ps_floor(m.R_max)
                s.dmax = max(s.dmax, s.dmin)
                s.net_lo, s.net_hi = ps_floor(m.R_min), ps(m.R_max)
                s.nu = None if Rp is None else Rp + ps_floor(m.R_min)
            streams[g] = s

    be = []
    for b in system.best_effort:
        be.append(BEFlow(b.name, b.source, ps(b.period + b.min_inter_arr_time), b.length,
                         tuple(link_ids(list(b.path), arch))))
    periods = [t.T for t in tasks.values()] + [b.T for b in be]
    Tmax = max(t.T for t in tasks.values())
    H = 1
    for T in periods:
        H = math.lcm(H, T)
        if H > MAX_H_PERIODS * Tmax:
            H = MAX_H_PERIODS * Tmax
            break
    return Model(system=system, tasks=tasks, streams=streams, pub_of=pub_of, sub_of=sub_of,
                 st_pubs=st_pubs, be=be, H=H, Tmax=Tmax,
                 bandwidth=cfg.bandwidth if cfg else Fraction(1),
                 eps=ps(cfg.epsilon) if cfg else 0)


# ----------------------------------------------------------------- scheduling

def np_fp(jobs: list[tuple[int, int, int, object]]) -> dict:
    """Non-preemptive fixed priority on one core.
    jobs: (release, rank, execution, tag); rank 0 = highest; equal rank in release order.
    Returns tag -> (start, finish)."""
    jobs = sorted(jobs, key=lambda j: (j[0], j[1]))
    out, heap = {}, []
    i, n, t = 0, len(jobs), None
    while i < n or heap:
        if not heap and (t is None or t < jobs[i][0]):
            t = jobs[i][0]
        while i < n and jobs[i][0] <= t:
            r, rank, e, tag = jobs[i]
            heapq.heappush(heap, (rank, r, i, e, tag))
            i += 1
        rank, r, _, e, tag = heapq.heappop(heap)
        out[tag] = (t, t + e)
        t += e
    return out


def releases(anchor: int, T: int, t_from: int, t_to: int) -> list[int]:
    """anchor + kT for every k with t_from <= anchor + kT <= t_to."""
    k0 = -(-(t_from - anchor) // T)
    return list(range(anchor + k0 * T, t_to + 1, T))


@dataclass
class Inst:
    r: int
    f: int
    a: int | None = None
    fs: int | None = None
    late: bool = False        # ST: handed over after its first gate opened


def run_publishers(model: Model, rel: dict[int, list[int]], exe) -> dict[int, list[int]]:
    """Finish times f per WG and instance. exe(g, j) is the execution time of instance j."""
    fin = {}
    for p, gs in model.pub_of.items():
        jobs = [(r, model.tasks[g].rank, exe(g, j), (g, j)) for g in gs for j, r in enumerate(rel[g])]
        res = np_fp(jobs)
        for g in gs:
            fin[g] = [res[(g, j)][1] for j in range(len(rel[g]))]
    return fin


def run_subscribers(model: Model, arr: dict[int, list[int]], exe) -> dict[int, list[int]]:
    out = {}
    for k, gs in model.sub_of.items():
        jobs = [(a, model.streams[g].sub_rank, exe(g, j), (g, j))
                for g in gs for j, a in enumerate(arr[g])]
        res = np_fp(jobs)
        for g in gs:
            out[g] = [res[(g, j)][1] for j in range(len(arr[g]))]
    return out


def simulate(model: Model, rel: dict[int, list[int]], network, pub_exe, sub_exe) -> dict[int, list[Inst]]:
    """Publishers -> network -> subscribers. network(frames) with frames = {g: [(r, f)]}
    returns ({g: [a]}, {g: [late]})."""
    fin = run_publishers(model, rel, pub_exe)
    frames = {g: list(zip(rel[g], fin[g])) for g in model.streams}
    arr, late = network(frames)
    fs = run_subscribers(model, arr, sub_exe)
    out = {g: [Inst(r, f) for r, f in zip(rel[g], fin[g])] for g in model.tasks}
    for g in model.streams:
        for j, x in enumerate(out[g]):
            x.a, x.fs, x.late = arr[g][j], fs[g][j], late[g][j]
    return out


# ---------------------------------------------------------- Level A: delay box

def st_arrival(s: Stream, r: int, f: int) -> tuple[int, bool]:
    """ST: arrival at its window on the last link; late if handed over after its first gate
    opened (then it takes the next period's window)."""
    j = r // s.T
    if f - r <= s.st_start[j % len(s.st_start)]:
        return r + s.st_arr[j % len(s.st_arr)], False
    return r + s.T + s.st_arr[(j + 1) % len(s.st_arr)], True


def _ordered(xs: list[int]) -> list[int]:
    """Frames of one stream keep their order."""
    out, prev = [], None
    for a in xs:
        a = a if prev is None or a >= prev else prev
        out.append(a)
        prev = a
    return out


def delay_box(model: Model, choose):
    """A Level A network: choose(stream, r, f, lo, hi) -> arrival in [lo, hi] for AVB."""
    def net(frames):
        arr, late = {}, {}
        for g, fr in frames.items():
            s = model.streams[g]
            if s.st_sched:
                res = [st_arrival(s, r, f) for r, f in fr]
                arr[g], late[g] = [a for a, _ in res], [x for _, x in res]
            else:
                arr[g] = _ordered([choose(s, r, f, f + s.dmin, f + s.dmax) for r, f in fr])
                late[g] = [False] * len(fr)
        return arr, late
    return net


def random_delays(model: Model, rng: random.Random):
    return delay_box(model, lambda s, r, f, lo, hi: rng.randint(lo, hi))


def planned_delays(model: Model, plan: dict):
    """Adversarial delays. plan[g]:
        ("align", tau)        arrive at tau if possible; earlier instances as late as possible,
                              later ones as early as possible (jitter bunching around tau)
        ("block", tau)        the instance that can arrive at tau does; the others early
        ("avoid", lo, hi)     keep out of [lo, hi)
        missing               as late as possible (R^max)"""
    def choose(s, r, f, lo, hi):
        kind = plan.get(s.g, ("late",))
        if kind[0] == "align":
            tau = kind[1]
            return hi if hi < tau else (tau if lo <= tau else lo)
        if kind[0] == "block":
            tau = kind[1]
            return tau if lo <= tau <= hi else lo
        if kind[0] == "avoid":
            wlo, whi = kind[1], kind[2]
            if lo < wlo or lo >= whi:
                return lo
            return whi if whi <= hi else lo
        return hi
    return delay_box(model, choose)


# ------------------------------------------------------- adversarial scenarios

@dataclass
class Scenario:
    label: str
    anchors: dict[int, int]       # WG -> a release time (others at anchor + kT)
    t_from: dict[int, int]        # WG -> first release no earlier than this
    t_end: int
    plan: dict                    # Level A delays (planned_delays)
    target: int = 0               # the WG whose bounds the scenario aims at

    def releases(self, model: Model) -> dict[int, list[int]]:
        return {g: releases(self.anchors[g], model.tasks[g].T, self.t_from[g], self.t_end)
                for g in model.tasks}


def _publisher_finish(model: Model, p: int, anchors: dict, t_from: dict, t_end: int) -> dict:
    rel = {g: releases(anchors[g], model.tasks[g].T, t_from[g], t_end) for g in model.pub_of[p]}
    jobs = [(r, model.tasks[g].rank, model.tasks[g].W, (g, j))
            for g in model.pub_of[p] for j, r in enumerate(rel[g])]
    res = np_fp(jobs)
    return {g: [(r, res[(g, j)][1]) for j, r in enumerate(rel[g])] for g in model.pub_of[p]}


def _target_arrival(model: Model, g: int, fr: list[tuple[int, int]], limit: int) -> int | None:
    """The arrival time tau of the target's worst instance (released before `limit`):
    AVB: the instance with the largest publisher response, delayed by R^max;
    ST: the instance whose window gives the latest arrival A_max."""
    s = model.streams[g]
    cand = [(r, f) for r, f in fr if r < limit] or fr[:1]
    if s.st_sched:
        best = max(cand, key=lambda rf: (st_arrival(s, rf[0], rf[1])[0] - rf[0], -rf[0]))
        return st_arrival(s, best[0], best[1])[0]
    r, f = max(cand, key=lambda rf: (rf[1] - rf[0], -rf[0]))
    return f + s.dmax


def _send_time(model: Model, x: int, tau: int) -> int:
    """Release of x so that, without interference at its publisher, its frame can arrive at
    tau with the largest delay: tau - R^max - W."""
    return tau - model.streams[x].dmax - model.tasks[x].W


def _hits(model: Model, x: int, fr, tau: int) -> bool:
    s = model.streams[x]
    if s.st_sched:
        return any(st_arrival(s, r, f)[0] == tau for r, f in fr)
    return any(f + s.dmin <= tau <= f + s.dmax for r, f in fr)


def adversarial(model: Model, g: int, phasing: str, variant: str) -> Scenario:
    """A release pattern (and, for Level A, the network delays) built to reach the bounds of WG g.

    phasing "arbitrary" (any release phase, as the analysis assumes), variant
      "pub"  the publisher critical instant of g: g and every higher-priority WG released at
             0, the largest lower-priority AVB WG at 0 - 1 ps (blocking)
      "sub"  g released at 0; every WG that interferes with g at its subscriber released so
             that its frame can arrive together with g's (also on g's own publisher)
      "jit"  as "sub", and on each other publisher the interfering stream with the largest W'
             leaves at its own critical instant, so its frames bunch by its publisher's
             output jitter too (see _publisher_jitter)
    phasing "part14": OPC 10000-14 6.3.1.1.1, a WG starts its cycles at multiples of its
      PublishingInterval on its node's clock; a node with an ST WG is synchronised (offset 0),
      the others get the offset that best lines up their frames with g's.
    In all of them, the other publishers' WGs that interfere with g at its subscriber are
    placed so their frames can arrive together with g's worst instance (higher priority) or
    1 ps before it (the largest lower-priority one: blocking), and frames bunch around it.
    Execution times are the WCETs."""
    tk = model.tasks[g]
    P, Tm = tk.p, model.Tmax
    anchors = {x: 0 for x in model.tasks}
    t_from = {}
    span = model.H if phasing == "part14" else 2 * Tm      # where g's worst instance is looked for
    for x in model.pub_of[P]:
        t_from[x] = 0
    if phasing == "arbitrary" and variant == "pub":
        lower = [x for x in model.pub_of[P] if model.tasks[x].rank > tk.rank and not model.tasks[x].st]
        if lower:
            b = max(lower, key=lambda x: (model.tasks[x].W, -model.tasks[x].rank))
            anchors[b] = t_from[b] = -EPS
    label = f"{phasing}:{variant}:WG{g}"
    t_end_p = span + 3 * Tm
    s = model.streams.get(g)
    if s is None:                                   # no frame: only the publisher matters
        for x in model.tasks:
            t_from.setdefault(x, anchors[x] - 2 * Tm)
        return Scenario(label, anchors, t_from, t_end_p, {}, g)

    k = s.k
    hp = [x for x in model.sub_of[k] if model.streams[x].sub_rank < s.sub_rank]
    lp = [x for x in model.sub_of[k] if model.streams[x].sub_rank > s.sub_rank]
    lp_avb = [x for x in lp if not model.streams[x].st_sched]
    blocker = max(lp_avb or lp, key=lambda x: (model.streams[x].Ws, -model.streams[x].sub_rank),
                  default=None)
    tau_of = {x: None for x in model.tasks}

    fr = _publisher_finish(model, P, anchors, t_from, t_end_p)
    tau = _target_arrival(model, g, fr[g], span)
    if phasing == "arbitrary" and variant in ("sub", "jit"):
        # line up the interfering WGs of g's own publisher too (moving them changes g's
        # hand-over time, so iterate)
        own = [x for x in model.pub_of[P] if x != g and not model.tasks[x].st
               and (x in hp or x == blocker)]
        for _ in range(8):
            changed = False
            for x in own:
                want = _send_time(model, x, tau - (EPS if x == blocker else 0))
                if anchors[x] != want:
                    anchors[x] = t_from[x] = want
                    changed = True
            if not changed:
                break
            fr = _publisher_finish(model, P, anchors, t_from, t_end_p)
            tau = _target_arrival(model, g, fr[g], span)
    t_end = tau + 3 * Tm
    for x in hp:
        tau_of[x] = tau
    if blocker is not None:
        tau_of[blocker] = tau - EPS

    # the other publishers
    for q, gs in model.pub_of.items():
        if q == P:
            continue
        relevant = [x for x in gs if tau_of[x] is not None and not model.streams[x].st_sched]
        if phasing == "arbitrary":
            for x in gs:
                anchors[x] = _send_time(model, x, tau_of[x]) if x in relevant else 0
            if variant == "jit":
                _publisher_jitter(model, q, gs, relevant, tau_of, anchors)
        elif q not in model.st_pubs and relevant:
            best, best_score = 0, -1
            cands = [0] + [_send_time(model, x, tau_of[x]) % model.tasks[x].T for x in relevant]
            for o in cands:
                anc = {x: o for x in gs}
                tf = {x: o - 2 * Tm for x in gs}
                frq = _publisher_finish(model, q, anc, tf, t_end)
                score = sum(model.streams[x].Ws for x in relevant if _hits(model, x, frq[x], tau_of[x]))
                if score > best_score:
                    best, best_score = o, score
            for x in gs:
                anchors[x] = best
        for x in gs:
            t_from[x] = anchors[x] - 2 * Tm

    plan = {g: ("align", tau)}
    for x in hp:
        plan[x] = ("align", tau)
    if blocker is not None:
        plan[blocker] = ("block", tau - EPS)
        for x in lp:
            if x != blocker:
                plan[x] = ("avoid", tau - EPS - model.streams[blocker].Ws, tau)
    return Scenario(label, anchors, t_from, t_end, plan, g)


def _publisher_jitter(model: Model, q: int, gs: list[int], relevant: list[int], tau_of: dict,
                      anchors: dict) -> None:
    """Variant "jit": on publisher q, the interfering stream x with the largest W' gets the
    critical instant of its own publisher (higher-priority WGs released with it, the largest
    lower-priority AVB WG 1 ps before), timed so that this instance, which leaves as late as
    R^p, arrives with delay R^max. Its next instance usually leaves early (about W after its
    release) and arrives early, so two of its frames arrive closer than T - J': the
    publisher's output jitter R^p - W, which the analysis does not include in J'."""
    cand = [x for x in relevant if model.tasks[x].Rp is not None]
    if not cand:
        return
    x = max(cand, key=lambda y: (model.streams[y].Ws, -model.streams[y].sub_rank))
    tx = model.tasks[x]
    r = tau_of[x] - model.streams[x].dmax - tx.Rp
    lower = [y for y in gs if model.tasks[y].rank > tx.rank and not model.tasks[y].st]
    blocker = max(lower, key=lambda y: (model.tasks[y].W, -model.tasks[y].rank), default=None)
    for y in gs:
        t = model.tasks[y]
        if t.st:
            continue
        if t.rank < tx.rank or y == x:
            anchors[y] = r
        elif y == blocker:
            anchors[y] = r - EPS


def scenarios(model: Model, phasing: str) -> list[Scenario]:
    """All adversarial scenarios of one phasing: per WG, "pub", "sub" and "jit" (arbitrary)
    or "p14" (Part 14)."""
    out = []
    for g in sorted(model.tasks):
        if phasing == "arbitrary":
            out.append(adversarial(model, g, "arbitrary", "pub"))
            if g in model.streams:
                out.append(adversarial(model, g, "arbitrary", "sub"))
                out.append(adversarial(model, g, "arbitrary", "jit"))
        else:
            out.append(adversarial(model, g, "part14", "p14"))
    return out


def random_releases(model: Model, rng: random.Random, phasing: str, t_end: int) -> dict[int, list[int]]:
    """Random phases: arbitrary = each AVB WG its own phase in [0, T); part14 = one clock offset
    per node in [0, Tmax) (0 for nodes with ST: they are synchronised to the schedule).
    ST WGs always start their periods at multiples of T (the schedule's time base)."""
    rel = {}
    offset = {p: (0 if p in model.st_pubs else rng.randrange(model.Tmax)) for p in model.pub_of}
    for g, t in model.tasks.items():
        if t.st:
            anchor = 0
        elif phasing == "arbitrary":
            anchor = rng.randrange(t.T)
        else:
            anchor = offset[t.p]
        rel[g] = releases(anchor, t.T, 0, t_end)
    return rel


def random_exec(rng: random.Random, wcet: dict[int, int], bcet: float):
    lo = {g: max(1, math.ceil(bcet * w)) for g, w in wcet.items()}
    return lambda g, j: rng.randint(lo[g], wcet[g])


# ------------------------------------------------------------------ observing

METRICS = ("pub", "net", "sub", "e2e")


@dataclass
class Obs:
    n: int = 0
    max: dict = field(default_factory=dict)        # metric -> (value, scenario, release)
    net_min: int | None = None
    arr_min: int | None = None                     # min / max of a - r
    arr_max: int | None = None
    after_max: int | None = None                   # fs - a
    late: int = 0
    viol: dict = field(default_factory=lambda: {m: 0 for m in METRICS + ("net_min",)})
    samples: dict = field(default_factory=lambda: {m: [] for m in METRICS})


class Observer:
    """Collects, per WG, the largest observed values, violations and (optionally) samples."""

    def __init__(self, model: Model, keep_samples: bool = False, max_traces: int = 5):
        self.model, self.keep, self.max_traces = model, keep_samples, max_traces
        self.obs = {g: Obs() for g in model.tasks}
        self.traces: list[dict] = []
        self._traced: dict = {}

    def _note(self, g, metric, value, bound, label, x: Inst):
        o = self.obs[g]
        cur = o.max.get(metric)
        if cur is None or value > cur[0]:
            o.max[metric] = (value, label, x.r)
        if self.keep and metric in o.samples:
            o.samples[metric].append(value)
        if bound is not None and value > bound:
            o.viol[metric] += 1
            key = (g, metric)
            if self._traced.get(key, 0) < self.max_traces:
                self._traced[key] = self._traced.get(key, 0) + 1
                self.traces.append({"wg": g, "metric": metric, "observed": us(value), "bound": us(bound),
                                    "scenario": label, "r": us(x.r), "f": us(x.f), "a": us(x.a),
                                    "fs": us(x.fs), "late": x.late})

    def add(self, insts: dict[int, list[Inst]], label: str) -> None:
        for g, lst in insts.items():
            s = self.model.streams.get(g)
            o = self.obs[g]
            for x in lst:
                o.n += 1
                self._note(g, "pub", x.f - x.r, self.model.tasks[g].Rp, label, x)
                if s is None or x.a is None:
                    continue
                net = x.a - (x.r if s.st_sched else x.f)
                self._note(g, "net", net, s.net_hi, label, x)
                o.net_min = net if o.net_min is None else min(o.net_min, net)
                if net < s.net_lo:
                    o.viol["net_min"] += 1
                rel = x.a - x.r
                o.arr_min = rel if o.arr_min is None else min(o.arr_min, rel)
                o.arr_max = rel if o.arr_max is None else max(o.arr_max, rel)
                o.after_max = max(o.after_max or 0, x.fs - x.a)
                o.late += x.late
                if s.nu is not None:
                    self._note(g, "sub", x.fs - (x.r + s.nu), s.Rs, label, x)
                self._note(g, "e2e", x.fs - x.r, s.R, label, x)
