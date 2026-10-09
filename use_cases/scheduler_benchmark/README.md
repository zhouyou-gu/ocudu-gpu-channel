# scheduler_benchmark — two OCUDU MAC schedulers under one channel

Native gate: `bash use_cases/scheduler_benchmark/run-ocudu-scheduler-benchmark.sh`
(from the repo root). It sources `scripts/native/env.sh` and reuses the
native renderers there; results land under `$OCUDU_NATIVE_ROOT`.

## Run the scheduler benchmark (two cells × two UEs, one channel)

`run-ocudu-scheduler-benchmark.sh` compares two OCUDU MAC scheduling
policies under the same radio conditions. Cell a (`gnb0` PCI 1 with `ue0`,
`ue1`) and cell b (`gnb1` PCI 2 with `ue2`, `ue3`) run side by side on two
broker processes. **One** Sionna bridge solves a seeded scenario whose radios
are `gnb0`, `ue0`, `ue1` and sends every batch to both brokers
(`run_bridge.py --fanout-control-endpoint`, cell b under the rename
`gnb0→gnb1, ue0→ue2, ue1→ue3`), so `ue2` carries exactly `ue0`'s channel and
`ue3` exactly `ue1`'s, at the same instant. The cells differ only in
`cell_cfg.scheduler.policy`: `rr` (`rr_sched`, `scheduler_time_rr.cpp`) or
`qos` (`qos_sched`, `scheduler_time_qos.cpp`, OCUDU's default). The policy is
bound at gNB start-up and the remote-control commands cannot change it, which
is why the two policies run in parallel rather than in alternating periods.

**Reproducibility.** `OCUDU_NATIVE_SB_SEED` fixes the UE random-waypoint
routes (`use_cases/scheduler_benchmark/make_scenario.py`, kept in line of sight
of the gNB against the scene's obstacle boxes), the PathSolver seed and every
packet schedule. The bridge runs `--timeline grid`: update k is solved at
scenario time k / update_hz, never at "elapsed wall time", and scenario time
0 is held (`--hold-until-file`) until all four UEs have attached. Within a
run both cells receive the same solved batch, so their channels are equal by
construction. Across runs with the same seed (live runs on the GB10,
2026-10-01: ten run pairs, seven seed 1 and two seed 2 runs, 701-1322 shared
grid points per pair) the UE positions and the tap structure (count, delays)
were identical at every grid point, and 89-98% of the batches were
bit-identical (SHA-256 per batch, `--profile-digest`); at the rest the GPU's
floating-point summation order differed, by at most 0.0038 dB in tap gain
and 0.0005 rad in phase.
`use_cases/scheduler_benchmark/campaign.py` recomputes this for any set of
runs. The radio stack itself (thread timing, HARQ, when a channel update
lands relative to the IQ stream) is not deterministic; that is what the
per-second pairing and the A/A control below measure.

**Sequence.** Core, two brokers, the bridge (scenario time 0 applied), two
gNBs; the gate then checks each gNB's non-default configuration dump
(`log.config_level: info`) for the requested policy and refuses to continue
on a mismatch (`policy-check.json`). Four srsUEs attach; then at one instant
the seeded UDP traffic (`use_cases/scheduler_benchmark/traffic.py`) starts and
the bridge's grid point 0 is released. After `OCUDU_NATIVE_SB_MEASURE_SECONDS`
and a drain, `use_cases/scheduler_benchmark/analyze.py` writes
`results/reports/ocudu-scheduler-benchmark/<ts>/benchmark-report.json`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `OCUDU_NATIVE_SB_SEED` | `1` | Scenario, solver and traffic seed. |
| `OCUDU_NATIVE_SB_SCHED_A` / `_B` | `rr` / `qos` | Policy of cell a / b. Run each seed twice with the policies swapped to cancel cell bias, and once with the same policy in both cells (A/A) to measure it. |
| `OCUDU_NATIVE_SB_PROFILE` | `mixed` | Traffic profile (`definitions.py`): `mixed` = UE A backlogged download, 5QI 9; UE B on/off video and 100 Hz control, 5QI 7. `full_buffer` = both backlogged, 5QI 9. `light` = Poisson well below capacity. The per-UE 5QI goes into the subscriber CSV's `qci` column. |
| `OCUDU_NATIVE_SB_TRAFFIC_OVERRIDES` | unset | `ue0.dl_bulk.rate_mbps=25,ue1.five_qi.value=9`, … |
| `OCUDU_NATIVE_SB_MEASURE_SECONDS` | `120` | Scenario seconds measured; the report pairs them second by second. |
| `OCUDU_NATIVE_SB_CPUS_*` | profile | On `spark-gb10`: both gNBs share 5-12, the four UEs share 15-19, brokers 13 / 14, everything else 0-4. A gNB must see at least five CPUs: OCUDU sizes its main worker pool as min(needed, CPUs − 3), and with one worker it stalls at MAC cell activation. |
| `OCUDU_NATIVE_SB_WEB_UI` / `_WEB_PORT` | `1` / `8090` | The benchmark UI (`use_cases/scheduler_benchmark/server.py --follow-latest`). |

**Metrics.** Every metric is computed per valid second (all four UEs in
service: present in their gNB's scheduler report with DL HARQ not failing
outright, and, when srsUE has written its metrics CSV, camped on its own PCI)
and reported per cell with a paired a − b difference and a batch-means 95%
CI. Application layer (UDP, one host clock): aggregate and per-UE
throughput, Jain's index, mean and p95/p99 one-way delay, PDB violation
rate against the UE's 5QI, drop rate. MAC layer (OCUDU `Slot decisions`
log, `lib/scheduler/log_reference.md`): PRB utilization, PRB shares,
spectral efficiency, DL wait while data is queued (`dl_bo > 0`) as the
starvation measure, and the scheduler's per-slot decision time. GBR
satisfaction is not measured: srsUE has one default non-GBR QoS flow.

**Radio speed.** The report and the UI state each cell's slots per second
(real time is 1000 at 15 kHz SCS). On the GB10 with this placement eight
`mixed` runs (2026-10-01) ran at 393-654 slots/s, varying from run to run
with no other busy process in the host-load samples (cause not identified);
within a run the two cells stayed within 4% of each other, so the comparison
stays paired, but wall-clock capacities and delays scale with this factor
and absolute numbers from different runs are not comparable.

**Cell bias is real; use swap pairs.** In the seed-1 A/A run (`rr` in both
cells, 20261001T083522Z) UE A's DL p99 delay was 244 ms higher in cell a
(95% CI 132-357 ms) and the aggregate DL 0.84 Mb/s lower (CI 0.40-1.28),
with the same policy, channel and traffic. A single run's paired CI therefore
does not separate the policy from the cell; the swap pair does.

**Known failure.** srsUE (srsRAN 4G 23.04 fork) can abort in RLC AM NR
(`rlc_am_nr.cc:1776`, `SO_start > SO_end` on a NACK segment) under the
backlogged 5QI 9 flow; one of eight runs lost UE A of cell a this way after
60 s. The lock-step broker then stalls the whole cell, the analyzer marks
those seconds invalid, and the gate fails on the crash (`per_ue.*.crashed`,
`srsue_alive_at_stop`).

In this container: `sudo env SB_SEED=1 SB_MEASURE=120 bash
/home/dev/ocudu-setup/run-isolated-scheduler-benchmark.sh`, or
`SB_CAMPAIGN="1:rr:qos 1:qos:rr 1:rr:rr"` for a swap pair plus an A/A run;
`use_cases/scheduler_benchmark/campaign.py <report dirs>` turns those into
cell-bias-free policy effects.
