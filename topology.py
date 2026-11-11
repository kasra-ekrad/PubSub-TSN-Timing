"""Architecture and route enumeration, ported from Network_generator.m.

Node numbering as in the original: end nodes are positive (1..num_nodes),
switches negative. 0 is reserved (the algorithm uses it as an end marker).
"""
from __future__ import annotations


def build_architecture(links: list[tuple[int, int]], full_duplex: bool) -> list[tuple[int, int]]:
    """Rows of `architecture`. Full duplex: row 2i-1 = link i, row 2i = link i reversed."""
    arch = []
    for a, b in links:
        arch.append((a, b))
        if full_duplex:
            arch.append((b, a))
    return arch


def num_end_nodes(arch: list[tuple[int, int]]) -> int:
    return max(max(a, b) for a, b in arch)


def routes_between(arch: list[tuple[int, int]], s: int, d: int) -> list[list[int]]:
    """All loop-free paths from s to d, in the same order as the MATLAB code."""
    if s == d:
        return [[s]]
    route = [[s]]               # route{d,s}{path}
    link_node = [s]             # last node of each path in the frontier
    path = prev_path = link_num = 1
    finished = False
    while not finished:
        finished = True
        for path_num in range(1, path + 1):         # range fixed at loop entry, as in MATLAB
            current = route[path_num - 1]
            if link_node[path_num - 1] in (d, 0):   # reached d, or already finished
                path += 1
                link_node.append(0)
                route.append(current + [0])
            else:
                for a, b in arch:
                    if a == link_node[path_num - 1] and b not in current:
                        path += 1
                        link_node.append(b)
                        route.append(current + [b])
                finished = False
        link_node = link_node[prev_path:]
        route = route[prev_path:]
        link_num += 1
        path -= prev_path
        prev_path = path
    return [[x for x in r if x != 0] for r in route]


def all_routes(arch: list[tuple[int, int]]) -> dict[tuple[int, int], list[list[int]]]:
    """route{d,s} for all end nodes, keyed (d, s) like the MATLAB cell array."""
    n = num_end_nodes(arch)
    return {(d, s): routes_between(arch, s, d)
            for s in range(1, n + 1) for d in range(1, n + 1)}


def select_path(routes: list[list[int]], explicit: list[int] | None = None) -> int:
    """1-based index into route{d,s}: the given path, else the shortest (first on ties)."""
    if not routes:
        raise ValueError("no route")
    if explicit is not None:
        for i, r in enumerate(routes, start=1):
            if r == list(explicit):
                return i
        raise ValueError(f"path {list(explicit)} is not a route of the architecture")
    best = min(len(r) for r in routes)
    return next(i for i, r in enumerate(routes, start=1) if len(r) == best)
