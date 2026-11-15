"""Where to spend CPU speed: publisher vs subscriber WCET scaling (stand-in exes)."""
import sys
from fractions import Fraction

import yaml

import cpu_scale as cs
import run_all
import test_run_all


def test_cpu_scale(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / run_all.SCHEDULER).write_text(test_run_all.SCHEDULER)
    (tools / run_all.AVB_TOOL).write_text(test_run_all.AVB)
    out = tmp_path / "cpu"
    assert cs.main(["experiments/cpu_scale.yaml", "--out", str(out), "--tools-dir", str(tools),
                    "--runner", sys.executable]) == 0
    rows = cs.read_csv(str(out / "cpu_scale_results.csv"))
    at = {(r["side"], r["k"], r["wg"]): r for r in rows}
    for g in range(1, 6):
        base = at[("publisher", 1.0, g)]
        assert at[("subscriber", 1.0, g)]["R"] == base["R"] == at[("both", 1.0, g)]["R"]
        pub2, sub2 = at[("publisher", 2.0, g)], at[("subscriber", 2.0, g)]
        assert pub2["R_s"] == base["R_s"] and pub2["R_p"] > base["R_p"]     # one side only
        assert sub2["R_p"] == base["R_p"] and sub2["R_s"] > base["R_s"]
        assert pub2["R_N"] == sub2["R_N"] == base["R_N"]                    # network unchanged
    spec = yaml.safe_load(open("experiments/cpu_scale.yaml"))
    folder = str(out / "Experiment1")
    for side in ("publisher", "subscriber", "both"):
        k = cs.critical(spec, folder, side)["critical_k"]
        assert cs.schedulable(spec, folder, side, Fraction(str(k)))[0]
        assert not cs.schedulable(spec, folder, side, Fraction(str(k)) + Fraction(1, 1000))[0]
    for f in ("cpu_scale_lines.png", "cpu_scale_bars.png", "cpu_scale_critical.csv"):
        assert (out / f).is_file()
