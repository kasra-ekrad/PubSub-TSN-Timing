# PubSub timing analysis

## Run everything (Windows, VS Code terminal)

Put `TSN_HeuristicScheduler.exe` and `AVB_analysis.exe` in this folder, then:

```
python -m pip install pyyaml numpy scipy
python run_all.py example_system.yaml --system ex1 --exp-id 1
```

Steps: 1 publisher RTA (ST WGs first) → 2 network generator → 3 mapping → 4 `TSN_HeuristicScheduler.exe Experiment1\ STHS_input.txt 4` → 5 AVB input generator → 6 `AVB_analysis.exe Experiment1\` → 7 subscriber RTA with the network results → 8 end-to-end. Everything ends up in `Experiment1/`, the final numbers in `Experiment1/results.json`. At the end, the utilization of every directed link is printed and saved to `link_utilization.csv` (per link, split ST / AVB_A / AVB_B / BE; without step 5, AVB messages are counted as `AVB`) and `link_utilization_messages.csv` (per link and message): sum of length / period over the messages on the link, divided by the bandwidth. `python link_report.py example_system.yaml --system ex1` gives the same report without running the tools.

Options: `--num-queues` (default 4), `--with-be`, `--tools-dir` (exes elsewhere), `--work-dir`, `--folder-arg` (default `Experiment{id}\`), `--reuse-network-results` (skip the exes, reuse their outputs), `--instances`. On macOS/Linux the exes are started through `wine`.

Reading the network outputs: AVB `final_new_results.txt` (block ID; priority 2 = A, 3 = B; deadline ps; WCRT ps) → R^max per WG via `AVB_Input_map.csv`. ST `Schedule.txt` (message; link; queue; gate open ns; gate close ns) → per instance k: arrival a_k = last-link close − kT, start s_k = first-link open − kT; A_max, J = A_max − A_min. ST end-to-end = A_min + R^s (= R^p + R^max + R^s − J' with R^max = A_max − R^p), valid only if R^p ≤ min s_k (else net status `st_not_ready`).

Python 3.10+, needs `pyyaml`, `numpy`, `scipy` and `matplotlib` (and `pytest` for the tests): `python -m pip install -r requirements.txt`.

| File | Content |
|---|---|
| `system.py` | Loads one YAML/JSON file holding a list of systems A = ⟨X, Y, N⟩: publishers, subscribers, network topology and messages |
| `publisher_rta.py` | Publisher analysis (non-preemptive fixed priority); also runs on its own |
| `subscriber_rta.py` | Subscriber analysis (priority queue, non-preemptive) with jitter, and the end-to-end delay |
| `analyse.py` | Runs everything: publishers, network messages, subscribers, end-to-end |
| `uadp.py` | NetworkMessage and Ethernet frame size (Part 14 Annex A.2.1, UADP-Periodic-Fixed) |
| `topology.py` | Architecture and route enumeration, ported from `Network_generator.m` |
| `network_generator.py` | Writes `Messages.mat`, `Routes.mat`, `Architecture.mat` for the network tool, from the YAML |
| `mapping_tool.py` | Port of `Mapping_tool.m`: splits the messages into TT/AVB/BE and writes `STHS_input.txt` |
| `network_results.py` | Reads `Schedule.txt` and `final_new_results.txt` back into R^max, ST arrival times and jitter per WG |
| `run_all.py` | Runs steps 1–8 in order, including the two exes |
| `generate_system.py`, `scenario.yaml` | Generate a system YAML from a scenario (counts, ranges, shares, WCET model) |
| `run_sweep.py`, `plot_sweep.py`, `sweeps/` | Parameter sweeps (scenarios 2 and 5) and their plots |
| `case_study.yaml`, `plot_case_study.py` | Case study (scenario 1) and its plots |
| `link_report.py` | Utilization per link: messages, lengths and periods on each link |
| `avb_input_generator.py` | Port of `AVB_analysis_input_generator.m`: writes `AVB_Input.txt` from the schedule and the AVB/BE messages |
| `example_system.yaml` | `ex1` (placeholder WCETs, ring topology), `davis` (publisher only), `jitter_demo` (hand-checkable) |
| `test_*.py` | Hand-calculated cases, loader checks, simulator cross-checks, MAT round trip |

```
python analyse.py example_system.yaml                                   # all systems
python analyse.py example_system.yaml --system jitter_demo --instances
python analyse.py example_system.yaml --json results.json
python network_generator.py example_system.yaml --system ex1 --exp-id 1 [--out DIR] [--map]
python mapping_tool.py --exp-id 1 [--dir DIR]                           # or --map above
#   ... run TSN_HeuristicScheduler.exe on STHS_input.txt -> Schedule.txt ...
python avb_input_generator.py --exp-id 1 [--dir DIR] [--with-be]
python publisher_rta.py example_system.yaml                             # publishers only
python -m pytest -q
```

## Evaluation (scenarios 1, 2 and 5)

**Scenario 1, case study.** `case_study.yaml` is a robot cell (3 publishers, 2 subscribers, 2 switches, 7 WGs of which 2 ST, best-effort traffic from a maintenance PC). WCETs come from the midpoints of the scenario.yaml ranges.

```
python run_all.py case_study.yaml --system case_study --exp-id 1 --with-be
python plot_case_study.py Experiment1
```

Writes `case_study_table.csv`, `case_study_breakdown.png` (R per WG split into publisher / network / jitter / subscriber) and `case_study_links.png` (link utilization by class) into `Experiment1`.

**Scenario 2, sweeps** and **scenario 5, WCET sensitivity.** A sweep file in `sweeps/` varies one scenario parameter over a list of values, with the same seeds for every value:

```
python run_sweep.py sweeps/hops.yaml            # runs every value x seed through all 8 steps
python plot_sweep.py sweep_runs/hops            # e2e_boxplot.png, breakdown.png, schedulable.png, summary.csv
```

Sweep files: `wgs_per_publisher`, `fan_in`, `message_size`, `hops`, `st_share`, `best_effort` (run with `--with-be`), `wcet_scale` (scenario 5). Each run's files stay in `sweep_runs/<name>/Experiment<ID>/`, its printed output in `logs/`, and all numbers in `results_wg.csv` (per WG) and `results_runs.csv` (per run). `--seeds 1 5` overrides the seeds, `--resume` skips finished runs, `--collect-only` rebuilds the CSV files.

The end-to-end delay is split as R = R^p + (R^max − J') + J' + (R^s − J'): publisher, network up to the earliest arrival, network jitter, subscriber after arrival.

## Generating a system

Instead of writing the YAML by hand, describe it in a scenario (`scenario.yaml`) and generate it:

```
python generate_system.py scenario.yaml --out generated.yaml [--seed N]
python run_all.py generated.yaml --system gen --exp-id 1
```

The scenario gives the topology (links and which end nodes are publishers, subscribers and best-effort sources: the same for every seed; or a generated line of switches, 1 = star, with its own `topology.seed`) and counts and ranges: WGs per publisher, periods, variables and DSMs per WG, data types, ST and AVB A shares (placeholders), best-effort flows and network settings. WCET = base + slope × size, with base and slope drawn per task from ranges (rough estimates, scaled from Kirchberger, TU Wien 2022); ψ (collection) and ψ' (dispatch) have one value per data type for the whole system, or are given per type with `per_type`. Priorities are global: every ST WG above every AVB WG, then shorter period first; rho = rank. A draw is kept only if it passes the loader's checks (MTU, link load) and every publisher and subscriber has ΣW/T below `limits.max_cpu_utilization`; otherwise the next draw is tried. Same scenario + seed = same system.

## Conventions

- Times in µs. Decimals are allowed and read exactly (0.1 + 0.2 = 0.3), so floor/ceil in the analysis stays exact. Sizes in bytes, bandwidth in bytes/µs.
- Larger ρ = higher priority. Equal ρ is broken by the smaller WriterGroupId, which must be unique in the whole system.
- A subscriber transaction inherits T' and ρ' from the publisher WG it receives (`writer_group_id`). With unicast, each WG is received by at most one subscriber transaction.
- Each publisher and subscriber has a `node`: its end-node number in `network.links`. A node hosts one publisher or one subscriber (A1).
- v and Δ are the lengths of the `psi` and `phi` lists; `types` gives the data type of each variable (same order as `psi`).

## Network

- `links`, `full_duplex`, `bandwidth`, `max_link_load` as in the original generator (end nodes positive, switches negative); `epsilon` is the switching-fabric delay per switch.
- Per message: `hard_real_time` (true = ST, false = AVB; the AVB class A/B is not in the YAML, it is assigned in step 5), `R_max` (parameter from the network analysis), optional `path` (node list). Without `path`, the shortest route is used (first on ties).
- h = links on the path; C_N = length / bandwidth; AVB: R_min = h·C_N + (h−1)·ε, J' = R_max − R_min; ST: J' = 0.
- `best_effort`: background flows that are not PubSub (`name`, `source`, `destination` end nodes, `length` in bytes, `period` or `min_inter_arr_time`, optional `path`). They go into `Messages.mat` with deadline 0 and count in the link load, but not in the timing analysis.
- Each directed link may appear only once (the mapping tool looks links up by their row in `architecture`).
- Errors (nothing is adjusted or dropped): link load above `max_link_load`, IP packet above the 1500-byte MTU, R_max < R_min, J' > T, no route.

### NM and frame size (`uadp.py`)

- NetworkMessage header: 13 + PublisherId (2 for UInt16, 8 for UInt64) bytes; no PayloadHeader, security, timestamp.
- DataSetMessage header: 5 bytes (DataSetFlags1, SequenceNumber, Status).
- Fields, RawData: Boolean/SByte/Byte 1, (U)Int16 2, (U)Int32/Float/StatusCode 4, (U)Int64/Double/DateTime 8, Guid 16, String(n)/ByteString(n) 4 + n, arrays Type[n] 4 + n·size.
- Frame: IPv4 20 + UDP 8 + NM, Ethernet 14 + 802.1Q 4 (`vlan_tag`) + FCS 4, at least 64; `length` adds preamble/SFD 8 and IFG 12 when `preamble_ifg` is true.

### Generator output (`Experiment<ID>/`)

- `Messages.mat`: 1×N struct `message`, one per received WG, ordered by WriterGroupId. period = deadline = T, offset = jitters = min_inter_arr_time = 0, length from the frame size, hard_real_time from the YAML, source/destination nodes, path = index into route{destination, source}.
- `Routes.mat`, `Architecture.mat`: same content as the MATLAB generator (verified in Octave on 41 topologies).
- `message_map.csv`: message index i ↔ class and WriterGroupId (or best-effort name), to read the network tool's results back.

### Mapping output (same folder)

- Classification: `hard_real_time` true → TT, deadline 0 → BE, otherwise AVB. (The original used jitter_reception ≠ 0 for TT, which the generator now always sets to 0.)
- `TT_Messages.mat`, `AVB_Messages.mat`, `BE_Messages.mat`: struct arrays, or the scalar 0 when a class is empty.
- `STHS_input.txt`: one block per TT message (length, period, deadline, linkNbr, link rows, initOffset); values must be whole µs.
- Without any ST message, the original's placeholder TT message is added, with a warning.
- Verified against `Mapping_tool.m` in Octave: identical STHS file and MAT contents (values, classes, field order).

### AVB analysis input (same folder)

- ST: one block per TT message, link and schedule instance, from `Schedule.txt` (columns: message index 0-based, link row, -, start ns, end ns); period = P·n where n = ceil(largest end on the link / 1000 / P); offset = start / 1000.
- AVB: priority `ClassA`/`ClassB` assigned here with the original rule: odd positions A, even positions B (function `avb_priorities`). The class used is recorded in `AVB_Input_map.csv` and, after step 7, shown in the tables (before that: `AVB`).
- BE: only with `--with-be` (priority NRT); the original had this block commented out.
- Verified against `AVB_analysis_input_generator.m` in Octave: byte-identical `AVB_Input.txt`, also with the BE block enabled; with the real tool outputs, identical to the `AVB_Input.txt` the AVB analysis used.

## Output

- Status per WG / transaction: `ok` (R ≤ T), `deadline_miss` (R > T), `overload` (utilization of hp ∪ {self} ≥ 1).
- End-to-end: R = R^p + R^max + R^s − J' (= R^p + R^min + R^s): R^s is measured from the earliest arrival and R^max is the latest, so the jitter is counted once.
- Instance numbering: publisher q is 0-based (q = 0..Q−1); subscriber q is 1-based (q = 1..q_max).
- Exit code is 0 only if every publisher WG and subscriber transaction is `ok`.
