"""CPU-speed study over generated systems (stand-in exes, small grid)."""
import sys
from fractions import Fraction

import cpu_study as cs
import run_all
import test_run_all


def test_cpu_study(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / run_all.SCHEDULER).write_text(test_run_all.SCHEDULER)
    (tools / run_all.AVB_TOOL).write_text(test_run_all.AVB)
    spec = tmp_path / "study.yaml"
    spec.write_text(f"""name: t
scenario: {__import__('os').path.abspath('scenario_line.yaml')}
set: {{topology.switches: 2, topology.placement: split}}
grid:
  wgs_per_publisher: [[1, 1], [4, 4]]
  topology: [{{publishers: 2, subscribers: 1}}, {{publishers: 1, subscribers: 2}}]
seeds: [1, 2]
k_max: 1024
dk: 0.1
""")
    out = tmp_path / "out"
    assert cs.main([str(spec), "--out", str(out), "--tools-dir", str(tools), "--runner", sys.executable]) == 0
    rows = cs.read_rows(str(out / "cpu_study_runs.csv"))
    assert len(rows) == 8
    failed = [r for r in rows if r["status"] != "ok"]
    assert all("cannot cover" in r["error"] for r in failed)          # 1 WG cannot reach 2 subscribers
    for r in rows:
        if r["status"] == "ok" and r["k_pub"] and r["k_sub"]:
            assert r["k_both"] <= min(r["k_pub"], r["k_sub"]) + 1e-9   # both sides slower: never later
    for f in ("cpu_study_heatmap.png", "cpu_study_scatter.png", "cpu_study_boxplots.png"):
        assert (out / f).is_file()


def test_scale_side_scales_one_side_only():
    d = {"publishers": [{"writer_groups": [{"psi": ["1"], "phi": ["2"], "C3": "3", "C4": "4"}]}],
         "subscribers": [{"transactions": [{"C1": "1", "C2": "2", "phi": ["3"], "psi": ["4"]}]}]}
    s = cs.scale_side(d, Fraction(2), Fraction(1))
    assert s["publishers"][0]["writer_groups"][0]["C4"] == "8" and s["subscribers"][0]["transactions"][0]["C1"] == "1"
