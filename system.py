"""System descriptions A = <X, Y, N>, loaded from one YAML/JSON file.

One file holds a list of systems. Each system has publishers, subscribers
and the network: its topology and the messages m_g between them.

All times are in microseconds. Values may be decimals (e.g. 0.4); they are
read as exact fractions, so the floor/ceil operations of the analysis at
multiples of T stay exact. Sizes are in bytes, bandwidth in bytes/us.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from fractions import Fraction
from typing import Any

import yaml

import topology
import uadp


# ------------------------------------------------------------------ publisher

@dataclass(frozen=True)
class WriterGroup:
    """Gamma_{i,j}: one WG transaction of publisher E_i."""

    n: int                       # transaction index within the publisher
    writer_group_id: int         # spec WriterGroupId; unique in the whole system
    T: Fraction                  # publishing interval T_{i,j} [us]
    rho: int                     # priority rho_{i,j}; larger value = higher priority
    psi: tuple[Fraction, ...]    # psi_{i,j,k}: collection time of variable k (length v)
    phi: tuple[Fraction, ...]    # phi_{i,j,u}: build/encode time of DSM u (length Delta)
    C3: Fraction                 # WCET of the WG task (NM assembly)
    C4: Fraction                 # WCET of the transport task (hand-off to the NIC)
    types: tuple[str, ...] = ()  # data type of variable k, for the NM size
    st: bool = False             # its NM is ST (hard_real_time); ST WGs have priority over all others

    @property
    def v(self) -> int:
        return len(self.psi)

    @property
    def Delta(self) -> int:
        return len(self.phi)

    @property
    def C1(self):                # Eq. (pubWcet1)
        return sum(self.psi)

    @property
    def C2(self):                # Eq. (pubWcet2)
        return sum(self.phi)

    @property
    def W(self):                 # Eq. (demand): cumulative WCET of gamma_{i,j}
        return self.C1 + self.C2 + self.C3 + self.C4


@dataclass(frozen=True)
class Publisher:
    """E_i: a publisher node hosting a set of WG transactions."""

    p: int
    wgs: tuple[WriterGroup, ...]
    node: int | None = None      # end-node number in the network architecture


# ----------------------------------------------------------------- subscriber

@dataclass(frozen=True)
class SubTransaction:
    """Gamma'_{k,m}: processing of the NMs of one publisher WG at subscriber E'_k.

    T, rho and p are taken from the publisher WG it receives.
    """

    m: int                       # transaction index within the subscriber
    writer_group_id: int         # the publisher WG whose NMs this transaction receives
    C1: Fraction                 # reception: copy of the NM from the receive buffer
    C2: Fraction                 # filtering
    phi: tuple[Fraction, ...]    # decoding time of DSM u (length Delta')
    psi: tuple[Fraction, ...]    # dispatch time of field x (length v')
    T: Fraction                  # T'_{k,m} = T_{i,j} of the sending WG
    rho: int                     # rho'_{k,m} = rho_{i,j} of the sending WG
    p: int                       # publisher of the sending WG

    @property
    def v(self) -> int:
        return len(self.psi)

    @property
    def Delta(self) -> int:
        return len(self.phi)

    @property
    def C3(self):                # decoding
        return sum(self.phi)

    @property
    def C4(self):                # dispatching
        return sum(self.psi)

    @property
    def W(self):                 # cumulative WCET of gamma'_{k,m}
        return self.C1 + self.C2 + self.C3 + self.C4


@dataclass(frozen=True)
class Subscriber:
    """E'_k: a subscriber node."""

    k: int
    transactions: tuple[SubTransaction, ...]
    node: int | None = None


# -------------------------------------------------------------------- network

@dataclass(frozen=True)
class NetworkConfig:
    links: tuple[tuple[int, int], ...]
    full_duplex: bool
    bandwidth: Fraction          # bytes/us
    max_link_load: Fraction      # percent
    epsilon: Fraction            # switching-fabric delay of one switch [us]
    publisher_id_type: str       # UInt16 or UInt64 (Table A.1)
    vlan_tag: bool               # 802.1Q tag in the frame
    preamble_ifg: bool           # `length` includes preamble/SFD and IFG

    @property
    def architecture(self) -> list[tuple[int, int]]:
        return topology.build_architecture(list(self.links), self.full_duplex)


@dataclass(frozen=True)
class NetworkMessage:
    """m_g: the NM sent by WG `writer_group_id` from `source` to `destination`.

    hard_real_time selects ST (True) or AVB (False).
    AVB: R_max = worst-case network delay from the hand-off, from the AVB analysis
         (or the YAML); R_min = h C_N + (h - 1) epsilon; J = R_max - R_min.
    ST:  with a schedule, the arrival times at the subscriber relative to the start
         of the period: J = A_max - A_min, and the end-to-end delay uses A_max.
         Without a schedule (YAML only), R_max is the delay from the hand-off and J = 0.
    """

    writer_group_id: int
    hard_real_time: bool
    R_max: Fraction | None       # worst-case network delay R^max_g [us]; None until known
    source: int                  # end node of the publisher
    destination: int             # end node of the subscriber
    path: tuple[int, ...]        # nodes from source to destination
    route_index: int             # 1-based index into route{destination, source}
    size: uadp.FrameSize
    length: int                  # bytes given to the network tool
    C_N: Fraction                # transmission time on one link = length / bandwidth [us]
    epsilon: Fraction
    avb_class: str | None = None  # "A"/"B": set from the AVB analysis results (step 5 decides)
    # From the ST schedule, relative to the start of the period [us]:
    st_arrival_max: Fraction | None = None   # A_max: latest gate close on the last link
    st_arrival_min: Fraction | None = None   # A_min: earliest gate close on the last link
    st_start_min: Fraction | None = None     # earliest gate open on the first link
    st_instances: int | None = None
    # From the AVB analysis:
    network_deadline: Fraction | None = None  # deadline reported by the AVB analysis [us]

    @property
    def traffic_class(self) -> str:
        if self.hard_real_time:
            return "ST"
        return f"AVB_{self.avb_class}" if self.avb_class else "AVB"

    @property
    def from_schedule(self) -> bool:
        return self.st_arrival_max is not None

    @property
    def h(self) -> int:          # number of links on the path
        return len(self.path) - 1

    @property
    def R_min(self):
        if self.hard_real_time:
            return self.R_max
        return self.h * self.C_N + (self.h - 1) * self.epsilon

    @property
    def J(self):                 # Eq. (jitter): J'_{k,m} = R^max_g - R^min_g
        if self.hard_real_time:
            return self.st_arrival_max - self.st_arrival_min if self.from_schedule else 0
        if self.R_max is None:
            raise ValueError(f"WG {self.writer_group_id}: R_max is not known yet")
        return self.R_max - self.R_min


def check_message(m: NetworkMessage, T, where: str) -> None:
    """R_max >= R_min (AVB) and J <= T, the assumption of the subscriber analysis."""
    if not m.hard_real_time and m.R_max is not None and m.R_max < m.R_min:
        raise ConfigError(f"{where}: R_max ({float(m.R_max)}) < R_min = h*C_N + (h-1)*epsilon "
                          f"({float(m.R_min)}); do bandwidth and epsilon match the network tools?")
    if (m.R_max is not None or m.from_schedule) and m.J > T:
        raise ConfigError(f"{where}: jitter J' = {float(m.J)} exceeds T = {float(T)} "
                          f"(the analysis assumes J' <= T')")


@dataclass(frozen=True)
class BestEffortFlow:
    """Background best-effort traffic: on the network, not part of the PubSub analysis."""

    name: str
    source: int
    destination: int
    length: int                  # bytes given to the network tool
    period: Fraction             # 0 if sporadic
    min_inter_arr_time: Fraction  # 0 if periodic
    path: tuple[int, ...]
    route_index: int


# --------------------------------------------------------------------- system

@dataclass(frozen=True)
class System:
    """A = <X, Y, N>."""

    name: str
    publishers: tuple[Publisher, ...]
    subscribers: tuple[Subscriber, ...] = ()
    network: dict[int, NetworkMessage] = field(default_factory=dict, compare=False)
    network_config: NetworkConfig | None = None
    best_effort: tuple[BestEffortFlow, ...] = ()
    routes: dict = field(default_factory=dict, compare=False)
    warnings: tuple[str, ...] = ()

    def writer_groups(self) -> dict[int, tuple[Publisher, WriterGroup]]:
        return {wg.writer_group_id: (pub, wg) for pub in self.publishers for wg in pub.wgs}


class ConfigError(ValueError):
    pass


# --------------------------------------------------------------- field parsing

def _int(value: Any, where: str, minimum: int | None = None) -> int:
    # bool is a subclass of int in Python; reject it explicitly.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where}: expected an integer, got {value!r}")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{where}: must be >= {minimum}, got {value}")
    return value


def _num(value: Any, where: str, minimum: int = 0, positive: bool = False) -> Fraction:
    """A time or rate: integer or decimal, read exactly."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ConfigError(f"{where}: expected a number, got {value!r}")
    try:
        x = Fraction(str(value))     # str() keeps the decimal as written, e.g. 0.1 -> 1/10
    except (ValueError, ZeroDivisionError):
        raise ConfigError(f"{where}: expected a number, got {value!r}") from None
    if x < minimum or (positive and x == 0):
        raise ConfigError(f"{where}: must be {'>' if positive else '>='} {minimum}, got {value}")
    return x


def _num_list(value: Any, where: str) -> tuple[Fraction, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{where}: expected a non-empty list of numbers")
    return tuple(_num(x, f"{where}[{i}]") for i, x in enumerate(value))


def _bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{where}: expected true or false, got {value!r}")
    return value


def _require(d: Any, key: str, where: str) -> Any:
    if not isinstance(d, dict) or key not in d:
        raise ConfigError(f"{where}: missing '{key}'")
    return d[key]


def _list(d: dict, key: str, where: str, required: bool) -> list:
    value = _require(d, key, where) if required else d.get(key, [])
    if value is None:
        value = []
    if not isinstance(value, list) or (required and not value):
        raise ConfigError(f"{where}.{key}: expected a{' non-empty' if required else ''} list")
    return value


def _unique(values: list, what: str, where: str) -> None:
    seen = set()
    for x in values:
        if x in seen:
            raise ConfigError(f"{where}: duplicate {what} {x}")
        seen.add(x)


def _node(d: dict, where: str) -> int | None:
    if "node" not in d:
        return None
    return _int(d["node"], f"{where}.node", 1)


# ----------------------------------------------------------- section parsing

def _parse_wg(d: dict, where: str) -> WriterGroup:
    psi = _num_list(_require(d, "psi", where), f"{where}.psi")
    types = d.get("types")
    if types is None:
        types = ()
    elif not isinstance(types, list) or len(types) != len(psi):
        raise ConfigError(f"{where}.types: expected a list with one type per variable ({len(psi)})")
    else:
        for i, t in enumerate(types):
            try:
                uadp.field_size(t)
            except uadp.SizeError as e:
                raise ConfigError(f"{where}.types[{i}]: {e}") from None
    wg = WriterGroup(
        n=_int(_require(d, "n", where), f"{where}.n", 1),
        writer_group_id=_int(_require(d, "writer_group_id", where), f"{where}.writer_group_id", 1),
        T=_num(_require(d, "T", where), f"{where}.T", positive=True),
        rho=_int(_require(d, "rho", where), f"{where}.rho", 0),
        psi=psi,
        phi=_num_list(_require(d, "phi", where), f"{where}.phi"),
        C3=_num(_require(d, "C3", where), f"{where}.C3"),
        C4=_num(_require(d, "C4", where), f"{where}.C4"),
        types=tuple(str(t) for t in types),
    )
    if wg.W == 0:
        raise ConfigError(f"{where}: cumulative WCET W is 0")
    return wg


def _parse_publisher(d: dict, where: str) -> Publisher:
    p = _int(_require(d, "p", where), f"{where}.p", 1)
    raw = _list(d, "writer_groups", where, required=True)
    wgs = tuple(_parse_wg(w, f"{where}.writer_groups[{i}]") for i, w in enumerate(raw))
    _unique([w.n for w in wgs], "n", where)
    return Publisher(p=p, wgs=wgs, node=_node(d, where))


def _parse_sub_transaction(d: dict, where: str, wgs: dict) -> SubTransaction:
    wgid = _int(_require(d, "writer_group_id", where), f"{where}.writer_group_id", 1)
    if wgid not in wgs:
        raise ConfigError(f"{where}: no publisher WG with writer_group_id {wgid}")
    pub, wg = wgs[wgid]
    t = SubTransaction(
        m=_int(_require(d, "m", where), f"{where}.m", 1),
        writer_group_id=wgid,
        C1=_num(_require(d, "C1", where), f"{where}.C1"),
        C2=_num(_require(d, "C2", where), f"{where}.C2"),
        phi=_num_list(_require(d, "phi", where), f"{where}.phi"),
        psi=_num_list(_require(d, "psi", where), f"{where}.psi"),
        T=wg.T, rho=wg.rho, p=pub.p,
    )
    if t.W == 0:
        raise ConfigError(f"{where}: cumulative WCET W' is 0")
    return t


def _parse_network_config(net: dict, where: str) -> NetworkConfig:
    raw_links = _list(net, "links", where, required=True)
    links = []
    for i, l in enumerate(raw_links):
        w = f"{where}.links[{i}]"
        if not isinstance(l, list) or len(l) != 2:
            raise ConfigError(f"{w}: expected [node_a, node_b]")
        a, b = (_int(x, w) for x in l)
        if 0 in (a, b) or a == b:
            raise ConfigError(f"{w}: node numbers must be non-zero and different")
        links.append((a, b))
    full_duplex = _bool(net.get("full_duplex", True), f"{where}.full_duplex")
    # Each directed link must be one row of `architecture` (Mapping_tool looks links up by row).
    _unique(topology.build_architecture(links, full_duplex), "link", f"{where}.links")
    if max(max(l) for l in links) < 1:
        raise ConfigError(f"{where}.links: no end node (positive number)")
    pid = net.get("publisher_id_type", "UInt16")
    if pid not in uadp.PUBLISHER_ID_BYTES:
        raise ConfigError(f"{where}.publisher_id_type: must be UInt16 or UInt64")
    return NetworkConfig(
        links=tuple(links),
        full_duplex=full_duplex,
        bandwidth=_num(_require(net, "bandwidth", where), f"{where}.bandwidth", positive=True),
        max_link_load=_num(net.get("max_link_load", 100), f"{where}.max_link_load", positive=True),
        epsilon=_num(_require(net, "epsilon", where), f"{where}.epsilon"),
        publisher_id_type=pid,
        vlan_tag=_bool(net.get("vlan_tag", True), f"{where}.vlan_tag"),
        preamble_ifg=_bool(net.get("preamble_ifg", True), f"{where}.preamble_ifg"),
    )


def _route(d: dict, where: str, routes: dict, source: int, destination: int) -> tuple[int, list[int]]:
    """Index into route{destination, source} and the path: the given `path`, else the shortest."""
    candidates = routes.get((destination, source), [])
    explicit = d.get("path")
    if explicit is not None and (not isinstance(explicit, list)
                                 or not all(isinstance(x, int) and not isinstance(x, bool) for x in explicit)):
        raise ConfigError(f"{where}.path: expected a list of node numbers")
    try:
        idx = topology.select_path(candidates, explicit)
    except ValueError as e:
        raise ConfigError(f"{where}: {e} from node {source} to node {destination}") from None
    return idx, candidates[idx - 1]


def _parse_message(d: dict, where: str, cfg: NetworkConfig, routes: dict,
                   pub: Publisher, wg: WriterGroup, sub: Subscriber) -> NetworkMessage:
    hrt = _bool(_require(d, "hard_real_time", where), f"{where}.hard_real_time")
    # Optional: run_all.py takes R_max from the network tools; analyse.py needs it here.
    R_max = _num(d["R_max"], f"{where}.R_max") if d.get("R_max") is not None else None
    for role, node in (("publisher", pub.node), ("subscriber", sub.node)):
        if node is None:
            raise ConfigError(f"{where}: the {role} of WG {wg.writer_group_id} has no 'node'")
    if not wg.types:
        raise ConfigError(f"{where}: WG {wg.writer_group_id} needs 'types' to size its NM")
    idx, path = _route(d, where, routes, pub.node, sub.node)

    try:
        size = uadp.frame_size(uadp.nm_size(list(wg.types), wg.Delta, cfg.publisher_id_type),
                               cfg.vlan_tag)
    except uadp.SizeError as e:
        raise ConfigError(f"{where}: {e}") from None
    length = size.length(cfg.preamble_ifg)
    msg = NetworkMessage(
        writer_group_id=wg.writer_group_id, hard_real_time=hrt, R_max=R_max,
        source=pub.node, destination=sub.node, path=tuple(path), route_index=idx,
        size=size, length=length, C_N=Fraction(length) / cfg.bandwidth, epsilon=cfg.epsilon)
    check_message(msg, wg.T, where)
    return msg


def _parse_best_effort(d: dict, where: str, routes: dict, n_end: int) -> BestEffortFlow:
    name = str(_require(d, "name", where))
    src = _int(_require(d, "source", where), f"{where}.source", 1)
    dst = _int(_require(d, "destination", where), f"{where}.destination", 1)
    for role, node in (("source", src), ("destination", dst)):
        if node > n_end:
            raise ConfigError(f"{where}.{role}: node {node} is not an end node of network.links")
    if src == dst:
        raise ConfigError(f"{where}: source and destination are the same node")
    if ("period" in d) == ("min_inter_arr_time" in d):
        raise ConfigError(f"{where}: give exactly one of 'period' or 'min_inter_arr_time'")
    period = _num(d.get("period", 0), f"{where}.period")
    mit = _num(d.get("min_inter_arr_time", 0), f"{where}.min_inter_arr_time")
    if period + mit == 0:
        raise ConfigError(f"{where}: period / min_inter_arr_time must be > 0")
    idx, path = _route(d, where, routes, src, dst)
    return BestEffortFlow(name=name, source=src, destination=dst,
                          length=_int(_require(d, "length", where), f"{where}.length", 1),
                          period=period, min_inter_arr_time=mit, path=tuple(path), route_index=idx)


def _check_link_load(cfg: NetworkConfig, flows: list[tuple[str, tuple, int, Fraction]], where: str) -> None:
    """Load per directed link: sum of length / (period + min_inter_arr_time) in bytes/us,
    as in the original generator. flows: (label, path, length, interval)."""
    load: dict[tuple[int, int], Fraction] = {}
    users: dict[tuple[int, int], list[str]] = {}
    arch = set(cfg.architecture)
    for label, path, length, interval in flows:
        for a, b in zip(path, path[1:]):
            if (a, b) not in arch:
                raise ConfigError(f"{where}: {label} uses link {a}->{b}, "
                                  f"which is not in the architecture (full_duplex: false?)")
            load[(a, b)] = load.get((a, b), Fraction(0)) + Fraction(length) / interval
            users.setdefault((a, b), []).append(label)
    limit = cfg.bandwidth * cfg.max_link_load / 100
    for link, x in load.items():
        if x > limit:
            raise ConfigError(
                f"{where}: link {link[0]}->{link[1]} is loaded {float(100 * x / cfg.bandwidth):.2f}% "
                f"> max_link_load {float(cfg.max_link_load)}% ({', '.join(users[link])})")


def _parse_system(s: dict, where: str) -> System:
    name = str(_require(s, "name", where))
    raw_pubs = _list(s, "publishers", where, required=True)
    pubs = tuple(_parse_publisher(p, f"{where}.publishers[{j}]") for j, p in enumerate(raw_pubs))
    _unique([p.p for p in pubs], "publisher id p", where)
    _unique([w.writer_group_id for p in pubs for w in p.wgs], "writer_group_id", where)
    wgs = {w.writer_group_id: (p, w) for p in pubs for w in p.wgs}

    subs = []
    for k_i, raw in enumerate(_list(s, "subscribers", where, required=False)):
        w = f"{where}.subscribers[{k_i}]"
        k = _int(_require(raw, "k", w), f"{w}.k", 1)
        raw_ts = _list(raw, "transactions", w, required=True)
        ts = tuple(_parse_sub_transaction(t, f"{w}.transactions[{i}]", wgs)
                   for i, t in enumerate(raw_ts))
        _unique([t.m for t in ts], "m", w)
        subs.append(Subscriber(k=k, transactions=ts, node=_node(raw, w)))
    _unique([x.k for x in subs], "subscriber id k", where)
    # Unicast: the NM of a WG goes to exactly one subscriber transaction.
    _unique([t.writer_group_id for x in subs for t in x.transactions],
            "receiving transaction for writer_group_id", where)
    # A1: each node is either one publisher or one subscriber.
    _unique([x.node for x in list(pubs) + subs if x.node is not None], "node", where)
    receiver = {t.writer_group_id: x for x in subs for t in x.transactions}

    warnings = []
    for x in subs:
        for t in x.transactions:
            wg = wgs[t.writer_group_id][1]
            if (t.Delta, t.v) != (wg.Delta, wg.v):
                warnings.append(f"subscriber {x.k}, m={t.m}: Delta'={t.Delta}, v'={t.v} differ from "
                                f"WG {t.writer_group_id} (Delta={wg.Delta}, v={wg.v})")

    net = s.get("network") or {}
    if not isinstance(net, dict):
        raise ConfigError(f"{where}.network: expected a mapping")
    cfg, routes, network, best_effort = None, {}, {}, []
    raw_msgs = _list(net, "messages", f"{where}.network", required=False)
    raw_be = _list(net, "best_effort", f"{where}.network", required=False)
    if raw_msgs or raw_be or "links" in net:
        cfg = _parse_network_config(net, f"{where}.network")
        routes = topology.all_routes(cfg.architecture)
        n_end = topology.num_end_nodes(cfg.architecture)
        for x in list(pubs) + subs:
            if x.node is not None and x.node > n_end:
                raise ConfigError(f"{where}: node {x.node} is not an end node of network.links")
        msgs = []
        for i, d in enumerate(raw_msgs):
            w = f"{where}.network.messages[{i}]"
            wgid = _int(_require(d, "writer_group_id", w), f"{w}.writer_group_id", 1)
            if wgid not in wgs:
                raise ConfigError(f"{w}: no publisher WG with writer_group_id {wgid}")
            if wgid not in receiver:
                raise ConfigError(f"{w}: WG {wgid} is not received by any subscriber")
            pub, wg = wgs[wgid]
            msgs.append(_parse_message(d, w, cfg, routes, pub, wg, receiver[wgid]))
            if "avb_class" in d:
                warnings.append(f"{w}: avb_class is ignored; the AVB class is assigned in step 5 "
                                f"(AVB_Input.txt)")
        _unique([m.writer_group_id for m in msgs], "writer_group_id", f"{where}.network.messages")
        best_effort = [_parse_best_effort(d, f"{where}.network.best_effort[{i}]", routes, n_end)
                       for i, d in enumerate(raw_be)]
        _unique([b.name for b in best_effort], "name", f"{where}.network.best_effort")
        flows = [(f"WG {m.writer_group_id}", m.path, m.length, wgs[m.writer_group_id][1].T) for m in msgs]
        flows += [(f"BE {b.name}", b.path, b.length, b.period + b.min_inter_arr_time) for b in best_effort]
        _check_link_load(cfg, flows, f"{where}.network")
        network = {m.writer_group_id: m for m in msgs}

    for x in subs:
        for i, t in enumerate(x.transactions):
            if t.writer_group_id not in network:
                raise ConfigError(f"{where}: WG {t.writer_group_id} (subscriber {x.k}) has no "
                                  f"entry in network.messages")

    # Step 1 of the flow: WGs whose NM is ST get priority over all other WGs of their publisher.
    st_ids = {g for g, m in network.items() if m.hard_real_time}
    pubs = tuple(replace(p, wgs=tuple(replace(w, st=w.writer_group_id in st_ids) for w in p.wgs))
                 for p in pubs)

    return System(name=name, publishers=pubs, subscribers=tuple(subs), network=network,
                  network_config=cfg, best_effort=tuple(best_effort), routes=routes,
                  warnings=tuple(warnings))


def load_systems(path: str) -> list[System]:
    """Read every system in a YAML or JSON file (JSON is valid YAML)."""
    with open(path, encoding="utf-8") as f:
        doc = yaml.safe_load(f)

    unit = doc.get("time_unit", "us") if isinstance(doc, dict) else None
    if unit != "us":
        raise ConfigError(f"time_unit must be 'us', got {unit!r}")

    raw_systems = _list(doc, "systems", "file", required=True)
    systems = [_parse_system(s, f"systems[{i}]") for i, s in enumerate(raw_systems)]
    _unique([s.name for s in systems], "system name", "file")
    return systems
