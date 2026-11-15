"""P1: one WG grows to the MTU; response times on its publisher."""
import yaml

import p1_publisher_size as p1


def test_p1_shapes(tmp_path):
    spec = yaml.safe_load(open("experiments/p1.yaml"))
    spec["target"]["v_step"] = 1
    rows, _ = p1.run(spec)
    at = {(r["v"], r["wg"]): r for r in rows}
    vs = sorted({r["v"] for r in rows})
    assert vs[0] == 1 and vs[-1] == 363 and at[(363, 3)]["target_nm_bytes"] == 1472   # IP packet 1500
    d = lambda g, v: at[(v, g)]["R"] - at[(v - 1, g)]["R"]
    dW = lambda v: at[(v, 3)]["W"] - at[(v - 1, 3)]["W"]
    largest_lower = max(at[(1, g)]["W"] for g in (4, 5))
    for v in [v for v in vs[1:] if v - 1 in vs]:
        assert abs(d(3, v) - dW(v)) < 1e-9                 # target: R grows with its own W
        for g in (4, 5):
            assert abs(d(g, v) - dW(v)) < 1e-9             # lower: one instance of the target
        for g in (1, 2):                                   # higher: only through blocking (a max)
            if at[(v, 3)]["W"] <= largest_lower:
                assert d(g, v) == 0
            elif at[(v - 1, 3)]["W"] >= largest_lower:
                assert abs(d(g, v) - dW(v)) < 1e-9
    assert all(r["status"] == "ok" for r in rows)
    assert p1.main(["experiments/p1.yaml", "--out", str(tmp_path)]) == 0
    for f in ("p1_results.csv", "p1_lines.png", "p1_bars.png"):
        assert (tmp_path / f).is_file()


def test_p1_size_budget(tmp_path):
    """WCETs x 5: WG 3's blocking makes WG 1 (T 500) miss its deadline from NM 1052 B."""
    spec = yaml.safe_load(open("experiments/p1_budget.yaml"))
    rows, _ = p1.run(spec)
    b = p1.size_budget(rows)
    assert b[1]["first_miss_nm_bytes"] == 1052 and b[1]["last_ok_nm_bytes"] == 1048
    assert all(b[g]["first_miss_v"] is None for g in (2, 3, 4, 5))
    assert p1.main(["experiments/p1_budget.yaml", "--out", str(tmp_path)]) == 0
    assert (tmp_path / "p1_budget.csv").is_file()
