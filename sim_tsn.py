"""Level B: a TSN network simulation, to replace the delay box of Level A.

Every directed link has an output port (at the end node's NIC or at a switch) with
    ST     frames sent in their own gate windows from Schedule.txt (802.1Qbv)
    AVB A  credit-based shaper (802.1Qav), strict priority over B
    AVB B  credit-based shaper
    BE     strict lowest priority (only if the AVB analysis included best effort, or --be on)
Rules:
  - gates: AVB and BE may not transmit in any ST window of the link; a frame starts only if
    it ends before the next ST window opens (guard band). The ST windows that close the
    gates are the ST blocks of AVB_Input.txt, i.e. exactly what the AVB analysis was given
    (including the placeholder ST message when the system has no ST traffic).
  - credit-based shaper per class and port: while frames wait (gate open) the credit grows
    at idleSlope; while the class transmits it falls at sendSlope = idleSlope - port rate;
    a frame may start only with credit >= 0; with an empty queue a positive credit is reset
    to 0 and a negative one grows back to 0; while the gate is closed the credit is frozen.
  - store and forward: a frame received completely at a switch enters the next port
    epsilon later; it arrives at the subscriber when its last bit is received.
  - transmission time on a link = length / bandwidth (length as given to the tools).
idleSlope (fraction of the port rate), per port and class, from the streams of that class
that use the port:
    srp     (default) 802.1Qat reservation: sum of C * ceil(CMI / T) / CMI with the class
            measurement interval CMI = 125 us (A) and 250 us (B)
    load    sum of C / T, times --idle-slope-factor
    A=x,B=y the same fixed fractions on every port
The idleSlope the AVB analysis tool assumes is not documented here; choose the same one,
otherwise Level B and the tool's R^max describe different networks.
"""
from __future__ import annotations

import bisect
import heapq
import math
import os
import re
from collections import deque
from fractions import Fraction

from sim_core import Model, ps, tx_time

CMI = {"A": 125 * 10 ** 6, "B": 250 * 10 ** 6}      # ps
ORDER = {"txend": 0, "arrive": 1, "st": 2, "wake": 3}


class SimError(ValueError):
    pass


# -------------------------------------------------------------------- windows

class Windows:
    """Time windows repeating with period P: [o, c) + kP. Windows that run past P wrap."""

    def __init__(self, wins: list[tuple[int, int]], period: int):
        self.P = period
        pieces = []
        for o, c in wins:
            o2, d = o % period, c - o
            if d <= 0:
                continue
            if d >= period:
                pieces = [(0, period)]
                break
            if o2 + d <= period:
                pieces.append((o2, o2 + d))
            else:
                pieces += [(o2, period), (0, o2 + d - period)]
        pieces.sort()
        merged = []
        for o, c in pieces:
            if merged and o <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], c))
            else:
                merged.append((o, c))
        self.starts = [o for o, _ in merged]
        self.ends = [c for _, c in merged]
        self.prefix = [0]
        for o, c in merged:
            self.prefix.append(self.prefix[-1] + c - o)
        # a piece at 0 that continues a window running to P is not a window start
        wraps = len(merged) > 1 and merged[0][0] == 0 and merged[-1][1] == period
        self.real_starts = self.starts[1:] if wraps else list(self.starts)

    def __bool__(self) -> bool:
        return bool(self.starts)

    def _piece_end(self, t: int) -> int | None:
        b = (t // self.P) * self.P
        i = bisect.bisect_right(self.starts, t - b) - 1
        if i >= 0 and t - b < self.ends[i]:
            return b + self.ends[i]
        return None

    def end_if_inside(self, t: int) -> int | None:
        """End of the window that contains t, or None."""
        e = self._piece_end(t)
        for _ in range(4):                     # a wrapped window continues in the next cycle
            if e is None:
                return None
            nxt = self._piece_end(e)
            if nxt is None:
                return e
            e = nxt
        return e

    def next_start(self, t: int) -> int | None:
        """First window start >= t."""
        s = self.real_starts
        if not s:
            return None
        b = (t // self.P) * self.P
        i = bisect.bisect_left(s, t - b)
        return b + s[i] if i < len(s) else b + self.P + s[0]

    def _closed_before(self, t: int) -> int:
        b, x = divmod(t, self.P)
        i = bisect.bisect_right(self.starts, x)
        part = self.prefix[i - 1] + min(self.ends[i - 1], x) - self.starts[i - 1] if i else 0
        return b * self.prefix[-1] + part

    def open_time(self, t0: int, t1: int) -> int:
        """Time in [t0, t1) outside the windows."""
        if not self.starts:
            return t1 - t0
        return (t1 - t0) - (self._closed_before(t1) - self._closed_before(t0))


def parse_avb_input_st(path: str, bandwidth: Fraction) -> dict[int, list[tuple[int, int, int]]]:
    """ST blocks of AVB_Input.txt: link -> [(open, close, period)] in ps."""
    if not os.path.isfile(path):
        raise SimError(f"{path} not found (Level B needs the files of a run_all.py run)")
    out: dict[int, list] = {}
    text = open(path, encoding="ascii", errors="replace").read()
    for block in re.split(r"\n\s*\n", text):
        kv = {}
        links = []
        for line in block.splitlines():
            if " = " in line:
                k, v = (x.strip() for x in line.split(" = ", 1))
                if k == "link":
                    links.append(int(v))
                else:
                    kv[k] = v
        if kv.get("priority") != "ST":
            continue
        o = ps(Fraction(kv["offset"]))
        c = o + tx_time(int(kv["length"]), bandwidth)
        P = ps(Fraction(kv["period"]))
        for link in links:
            out.setdefault(link, []).append((o, c, P))
    return out


def gate_windows(blocks: list[tuple[int, int, int]], cap: int = 20000) -> Windows:
    L = 1
    for _, _, P in blocks:
        L = math.lcm(L, P)
    wins = []
    for o, c, P in blocks:
        for k in range(L // P):
            wins.append((o + k * P, c + k * P))
            if len(wins) > cap:
                raise SimError("too many ST windows on one link")
    return Windows(wins, L)


# ---------------------------------------------------------------------- frames

class Frame:
    __slots__ = ("key", "cls", "links", "hop", "C", "r", "T", "late", "g")

    def __init__(self, key, cls, links, C, r, T, g=None):
        self.key, self.cls, self.links, self.C, self.r, self.T, self.g = key, cls, links, C, r, T, g
        self.hop, self.late = 0, False


class Port:
    def __init__(self, link: int, gates: Windows, alpha: dict[str, Fraction]):
        self.link, self.gates, self.alpha = link, gates, alpha
        self.q = {"A": deque(), "B": deque(), "BE": deque()}
        self.credit = {"A": Fraction(0), "B": Fraction(0)}
        self.last: int | None = None
        self.busy_until: int | None = None
        self.tx_cls: str | None = None
        self.st_waiting: list[Frame] = []       # ST frames whose window opened while busy
        self.version = 0
        self.min_credit = {"A": Fraction(0), "B": Fraction(0)}

    def advance(self, t: int) -> None:
        if self.last is None or t <= self.last:
            self.last = t if self.last is None else self.last
            return
        dt = t - self.last
        open_ = self.gates.open_time(self.last, t)
        for c in ("A", "B"):
            a = self.alpha.get(c, Fraction(0))
            if self.tx_cls == c:
                self.credit[c] -= (1 - a) * dt
                self.min_credit[c] = min(self.min_credit[c], self.credit[c])
            elif self.q[c]:
                self.credit[c] += a * open_
            elif self.credit[c] < 0:
                self.credit[c] = min(Fraction(0), self.credit[c] + a * open_)
        self.last = t


class TSN:
    def __init__(self, model: Model, folder: str, idle_slope: str = "srp", factor: float = 1.0,
                 be: str = "auto"):
        self.model = model
        sysm = model.system
        cfg = sysm.network_config
        st_blocks = parse_avb_input_st(os.path.join(folder, "AVB_Input.txt"), cfg.bandwidth)
        for s in model.streams.values():
            if s.cls == "ST" and not s.st_sched:
                raise SimError(f"WG {s.g} is ST without a schedule; Level B needs Schedule.txt")
        self.with_be = self._be_included(folder) if be == "auto" else be == "on"
        self.be = model.be if self.with_be else []
        self.idle_slope = idle_slope
        self.alpha = self._alphas(idle_slope, factor)
        links = {l for s in model.streams.values() for l in s.links} | {l for b in self.be for l in b.links}
        self.ports = {l: Port(l, gate_windows(st_blocks.get(l, [])) if st_blocks.get(l) else Windows([], 1),
                              self.alpha.get(l, {}))
                      for l in sorted(links)}
        self.own = {(s.g, l): (Windows(w, len(w) * s.T), s.T)
                    for s in model.streams.values() if s.st_sched for l, w in s.st_windows.items()}
        self.warnings = []
        for l, a in self.alpha.items():
            tot = sum(a.values())
            if tot > Fraction(3, 4):
                self.warnings.append(f"link {l}: idleSlope A+B = {float(tot):.0%} of the port rate "
                                     f"(802.1Q allows at most 75%)")

    @staticmethod
    def _be_included(folder: str) -> bool:
        path = os.path.join(folder, "AVB_Input_map.csv")
        if not os.path.isfile(path):
            return False
        return any(line.split(",")[1:2] == ["BE"] for line in open(path, encoding="utf-8"))

    def _alphas(self, mode: str, factor: float) -> dict[int, dict[str, Fraction]]:
        out: dict[int, dict[str, Fraction]] = {}
        fixed = None
        if re.fullmatch(r"A=[\d.]+,B=[\d.]+", mode or ""):
            fixed = {k: Fraction(v) for k, v in (x.split("=") for x in mode.split(","))}
        elif mode not in ("srp", "load"):
            raise SimError(f"--idle-slope: srp, load or A=x,B=y, got {mode!r}")
        for s in self.model.streams.values():
            c = self.cls(s)
            if c not in ("A", "B"):
                continue
            for l in s.links:
                d = out.setdefault(l, {})
                if fixed:
                    d[c] = fixed[c]
                elif mode == "srp":
                    d[c] = d.get(c, Fraction(0)) + Fraction(s.C * -(-CMI[c] // s.T), CMI[c])
                else:
                    d[c] = d.get(c, Fraction(0)) + Fraction(s.C, s.T) * Fraction(str(factor))
        for d in out.values():
            for c in d:
                d[c] = min(d[c], Fraction(1))
        return out

    @staticmethod
    def cls(s) -> str:
        return {"ST": "ST", "AVB_B": "B"}.get(s.cls, "A")

    # ------------------------------------------------------------------ running

    def run(self, frames: dict[int, list[tuple[int, int]]], be_rel: dict[str, list[int]] | None = None):
        """frames: WG -> [(release, hand-over)]. Returns ({g: [arrival]}, {g: [late]})."""
        for p in self.ports.values():
            p.__init__(p.link, p.gates, p.alpha)
        self.heap, self.seq = [], 0
        arr = {g: [None] * len(fr) for g, fr in frames.items()}
        late = {g: [False] * len(fr) for g, fr in frames.items()}
        self.out = (arr, late)
        for g, fr in frames.items():
            s = self.model.streams[g]
            for j, (r, f) in enumerate(fr):
                self._push(f, "arrive", Frame((g, j), self.cls(s), s.links, s.C, r, s.T, g))
        for b in self.be:
            C = tx_time(b.length, self.model.bandwidth)
            for j, r in enumerate((be_rel or {}).get(b.name, [])):
                self._push(r, "arrive", Frame((b.name, j), "BE", b.links, C, r, b.T))
        while self.heap:
            t = self.heap[0][0]
            dirty = {}
            while self.heap and self.heap[0][0] == t:
                _, _, _, kind, x, y = heapq.heappop(self.heap)
                if kind == "arrive":
                    port = self._enqueue(t, x)
                elif kind == "txend":
                    port = self._finish(t, x, y)
                elif kind == "st":
                    port = self._st_start(t, x, y)
                else:                                      # wake
                    port = x if y == x.version else None
                if port is not None:
                    dirty[port.link] = port
            for link in sorted(dirty):
                self._try_start(t, dirty[link])
        missing = [(g, j) for g, a in arr.items() for j, x in enumerate(a) if x is None]
        if missing:
            raise SimError(f"frames not delivered: {missing[:5]}")
        return arr, late

    def _push(self, t: int, kind: str, x, y=None) -> None:
        self.seq += 1
        heapq.heappush(self.heap, (t, ORDER[kind], self.seq, kind, x, y))

    def _enqueue(self, t: int, f: Frame) -> Port | None:
        port = self.ports[f.links[f.hop]]
        port.advance(t)
        if f.cls == "ST":
            w, T = self.own[(f.g, port.link)]
            o = w.next_start(t)
            if o // T != f.r // T:
                f.late = True                              # not in the window of its own period
            self._push(o, "st", port, f)
            return None
        port.q[f.cls].append(f)
        return port

    def _st_start(self, t: int, port: Port, f: Frame) -> Port | None:
        port.advance(t)
        if port.busy_until is not None and port.busy_until > t:
            f.late = True                                  # guard band failed (should not happen)
            port.st_waiting.append(f)
            return None
        self._start(t, port, f, "ST")
        return None

    def _start(self, t: int, port: Port, f: Frame, cls: str) -> None:
        port.busy_until, port.tx_cls = t + f.C, cls
        self._push(t + f.C, "txend", port, f)

    def _finish(self, t: int, port: Port, f: Frame) -> Port:
        port.advance(t)
        cls, port.tx_cls, port.busy_until = port.tx_cls, None, None
        if cls in ("A", "B") and not port.q[cls] and port.credit[cls] > 0:
            port.credit[cls] = Fraction(0)
        if f.hop + 1 < len(f.links):
            f.hop += 1
            self._push(t + self.model.eps, "arrive", f)
        elif f.g is not None:
            g, j = f.key
            self.out[0][g][j] = t
            self.out[1][g][j] = f.late
        return port

    def _try_start(self, t: int, port: Port) -> None:
        if port.busy_until is not None and port.busy_until > t:
            return
        port.advance(t)
        if port.st_waiting:
            self._start(t, port, port.st_waiting.pop(0), "ST")
            return
        if not any(port.q.values()):
            return
        close = port.gates.end_if_inside(t)
        if close is not None:                               # gate closed for AVB and BE
            self._wake(port, close)
            return
        nxt = port.gates.next_start(t)
        wakes = []
        for c in ("A", "B", "BE"):
            q = port.q[c]
            if not q:
                continue
            if c != "BE" and port.credit[c] < 0:
                a = port.alpha.get(c)
                if not a:
                    raise SimError(f"link {port.link}: class {c} frames but idleSlope 0")
                wakes.append(t + math.ceil(-port.credit[c] / a))
                continue
            if nxt is not None and t + q[0].C > nxt:      # guard band: would run into a window
                wakes.append(port.gates.end_if_inside(nxt))
                continue
            self._start(t, port, q.popleft(), c)
            return
        if wakes:
            self._wake(port, min(wakes))

    def _wake(self, port: Port, t: int) -> None:
        port.version += 1
        self._push(t, "wake", port, port.version)
