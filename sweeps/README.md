# Sweep files

Each file varies one parameter of a base scenario over a list of values, with the same
seeds for every value. Run one with `python run_sweep.py sweeps/<file>.yaml`, then
`python plot_sweep.py sweep_runs/<name>`.

| File | Parameter | Base | What it shows |
|---|---|---|---|
| wgs_per_publisher.yaml | WGs per publisher | scenario.yaml | publisher blocking/interference -> R^p |
| fan_in.yaml | publishers sending to one subscriber | scenario_line.yaml | subscriber load -> R^s |
| message_size.yaml | variables per WG | scenario.yaml | NM size -> C^N, R^p, R^s |
| hops.yaml | switches in the line | scenario_line.yaml | path length -> R^min, R^max, J' |
| st_share.yaml | share of ST WGs | scenario.yaml | ST windows vs AVB, ST vs AVB end-to-end |
| best_effort.yaml | best-effort flows | scenario.yaml | BE blocking of AVB |
| wcet_scale.yaml | factor on all WCETs (scenario 5) | scenario.yaml | robustness to the WCET estimates |
