"""Comparison with Denzler et al. 2021/2022 (stand-in exes)."""
import sys

import denzler
import run_all
import test_run_all


def test_denzler(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / run_all.SCHEDULER).write_text(test_run_all.SCHEDULER)
    (tools / run_all.AVB_TOOL).write_text(test_run_all.AVB)
    out = tmp_path / "d"
    assert denzler.main(["experiments/denzler.yaml", "--out", str(out), "--tools-dir", str(tools),
                         "--runner", sys.executable]) == 0
    rows = {r["case"]: r for r in denzler.read_rows(str(out / "denzler_results.csv"))}
    assert set(rows) == {"static2021", "sizes2022_v1", "sizes2022_v2", "sizes2022_v3", "sizes2022_v4"}
    a = rows["static2021"]
    assert a["frame_bytes"] == 73                         # one Int32, no VLAN tag, payload header
    assert a["bound_e2e"] == 232.9 + 15.4 + 5544.29 and a["measured_e2e"] == 642.32
    for r in rows.values():                               # no contention: R^p = W, R^s - J' = W'
        assert r["ours_pub"] == r["bound_pub"] and abs(r["ours_sub"] - r["bound_sub"]) < 1e-6
        assert abs(r["ours_e2e"] - (r["ours_pub"] + r["ours_net"] + r["ours_sub"])) < 1e-6
    assert [rows[f"sizes2022_v{v}"]["frame_bytes"] for v in range(1, 5)] == [77, 81, 85, 89]          # 77 B for 32 bit, as in the paper
    for f in ("denzler_static2021.png", "denzler_sizes.png"):
        assert (out / f).is_file()
