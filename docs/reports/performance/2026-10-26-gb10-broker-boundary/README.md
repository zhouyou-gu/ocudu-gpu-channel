# Broker boundary on a DGX Spark GB10 with synthetic radios, 26 October 2026

Where the shipped broker stops when nothing on the far side of its ZMQ sockets is slow. The gNB and the UEs are replaced by synthetic radios that send and request IQ exactly as a lock-step radio does; the broker, its CUDA channel and its pacing run unchanged. Real-time ratio = IQ samples the receiving radios obtained / (sample rate × measurement window).

| Cell | UEs (receiving nodes) | Ratio, shipped (`OCG_ZMQ_IO_THREADS=2`) | Ratio, `OCG_ZMQ_IO_THREADS=4` | Broker stages per slot, mean µs (room / data / channel call) |
|---|---:|---:|---:|---|
| 20 MHz, 15 kHz, 1 ms slot | 1–8 (2–9) | 1.000 | — | 46 / 0.4 / 56 (8 UEs) |
| 20 MHz | 16 (17) | **1.000**, 0.994 | 1.000 | 140 / 2 / 76 |
| 20 MHz | 20 (21) | 0.86 | **0.99** | 288 / 10 / 113 (4 threads) |
| 20 MHz | 24 (25) | 0.70, 0.75 | **0.87**, 0.87 | 978 / 329 / 104 → 864 / 51 / 117 |
| 20 MHz | 28 (29) | — | 0.70 | 1,124 / 100 / 118 |
| 20 MHz | 32 (33) | 0.46 ¹ | 0.60 | 1,449 / 118 / 138 |
| 50 MHz, 15 kHz, 1 ms slot | 4 (5) | **1.000** | — | 695 / 1 / 82 |
| 50 MHz | 8 (9) | 0.88–0.93 | 0.89 | 569 / 243 / 118 → 808 / 147 / 127 |
| 50 MHz | 12 (13) | 0.56–0.70 | 0.59–0.60 | 903 / 363 / 125 |
| 100 MHz, 30 kHz, 0.5 ms slot | 2 (3) | **1.000** | — | 178 / 10 / 72 |
| 100 MHz | 4 (5) | 0.75–0.81 | **0.91**, 0.91 | 358 / 158 / 97 → 332 / 88 / 99 |
| 100 MHz | 6 (7) | 0.58–0.63 | 0.58 | 391 / 265 / 121 |

¹ Measured with the synthetic radios on the A725 cores (see "Radio placement"); the other rows have them on X925 cores.

Read in one line: on one GB10 the broker as shipped holds real time to **16 UEs at 20 MHz, 4 UEs at 50 MHz and 2 UEs at 100 MHz**; the channel call on the GPU stays at 70–140 µs per receiver per slot at every point, and what grows at the edge is the time the broker spends waiting on its ZMQ transport (`room`: the receiving radio has not yet drained the previous slot's rows; `data`: input IQ has not yet arrived). Four ZMQ I/O threads instead of two, an environment knob, move the 20 MHz edge from 20 to 24 UEs at 0.87 and 100 MHz / 4 UEs from 0.75 to 0.91 — when the broker has the cores for them (see "Live stack").

## What limits it

The broker prints one `event=cpu_stage_timings` line per receiving node per second with the mean per-slot time of each producer stage ([`src/broker.cpp`](../../../../src/broker.cpp)). `tools/stagestat.py` averages them over the measurement window; `data/broker-stages.csv` has every run.

- **The channel call is flat.** `process` (the fused CUDA call for one receiving node, all its incoming links) is 56–76 µs at 20 MHz, 82–127 µs at 50 MHz and 72–121 µs at 100 MHz, from 2 to 33 nodes, with two or four I/O threads, blocking or spinning. It is not what stops the broker at any point in the table.
- **`room` grows first.** At 20 MHz / 24 UEs the producer waits 0.9–1.3 ms per slot for space in the receive ring, i.e. for the radio's request/reply to take the previous slot's rows away; at 50 and 100 MHz `data` (waiting for input IQ to arrive through the pullers) grows as well. Both are ZMQ transport: 184 KB (20 MHz) or 491 KB (50 and 100 MHz) per port per slot in each direction, 9 MB per millisecond at 24 UEs, all through the broker's ZMQ I/O threads.
- **ZMQ I/O threads, not cores or spinning.** At 20 MHz / 24 UEs with everything else fixed, `OCG_ZMQ_IO_THREADS` 1 / 2 / 4 / 8 gives **0.47 / 0.70 / 0.87 / 0.88** (`data/phase4.csv`, B rows); at 100 MHz / 4 UEs 0.81 / 0.91 / 0.89; at 50 MHz / 12 UEs 0.70 / 0.60 / 0.66 (no gain). With four threads at 24 UEs, 15 broker cores including the ten A725 cores are worse than ten (0.78 against 0.87), spinning (`OCG_BROKER_SPIN=2`) is worse still (0.65 at 974 % CPU), and at 24 UEs with two threads blocking at 451 % CPU equals spinning at 973 % (0.62 each, `data/phase2b.csv`). The residual with four threads is the per-port request/reply round trip, not a shortage of cores.
- **`ipc://` instead of TCP loopback** gives 0.80 (two threads) and 0.82 (four) at 24 UEs and 0.90 at 100 MHz / 4 UEs (`data/phase4.csv`, C rows): about what four threads over TCP give, not more.
- **Channel updates at the live rate cost nothing.** One `matrix_profile_swap` per link at 10 Hz through the control endpoint (`batch_begin` / swap / `batch_commit`, 320–480 batches per second at 16–24 UEs; `tools/updater.py`): 16 UEs 0.994 → 1.000, 20 UEs 0.86 → 0.87, 24 UEs 0.75 → 0.72, 50 MHz / 8 UEs 0.88 → 0.87, 100 MHz / 4 UEs 0.75 → 0.81 (`data/phase5.csv`). All batches accepted; commit round trip p50 0.3–0.6 ms, p99 1.1–2.0 ms. The hop trace (`OCG_HOP_TRACE_DIR`) and the control endpoint themselves cost nothing either (16 UEs: 1.000 with and without each, `data/phase4.csv`, A rows).
- **Taps per link do not change the count.** The generated links carry 8 taps (live Sionna links carry 2–8); 1, 2, 24 or 32 taps per link at 16 UEs change the per-call time (55 → 142 µs) but not the ratio; 32 is the device-side ceiling (`kDeviceMaxTaps`) and a 64-tap profile is rejected at start.

## Brokers per GPU

Cells run as separate broker processes, i.e. separate CUDA contexts. 4-UE cells at 20 MHz, one broker each, all brokers on the same ten cores: **2 brokers 0.995, 3 brokers 0.93, 4 brokers 0.89, 5 brokers 0.68**; two 8-UE brokers 0.93 against 1.000 for one 16-UE broker. The channel call grows with the number of contexts: 70 µs with one broker, 154 / 236 / 451 / 544 µs per call with 2 / 3 / 4 / 5 brokers (`data/broker-stages.csv`, `p3-M-*` rows), which is the GPU time-slicing between contexts rather than the call itself. At 100 MHz two 2-UE brokers run at 0.67 against 0.95–1.00 for one. Two cells on one GB10 cost little; from three brokers up the context switching is a real part of the slot budget (MPS is the known remedy and was not used here).

## Live stack

The same broker build runs the live single-cell scheduler benchmark of `ehs0/ocudu-gpu-channel` (`RAN_benchmark`, OCUDU gNB + srsUE, Sionna channel, traffic), where it is pinned to two X925 cores because the gNB takes five and the UEs three. There the knob reverses: `OCG_ZMQ_IO_THREADS=4` takes 8 UEs from 0.93–0.94 to 0.65 / 0.68 (every broker stage slows: channel call 49 → 128 µs, room 328 → 794 µs, data 120 → 310 µs, as four I/O threads and 27 broker threads share two cores), leaves 16 UEs at 0.45 / 0.46 (the `data` stage, waiting for the gNB's IQ, is 10–12 ms per slot with either setting: the gNB bounds that case, not the broker) and leaves two cells at 863 / 878 against 877 / 884 slots per second. The knob is worth four when a broker has four or more cores of its own.

## Setup

- **Hardware and software.** NVIDIA DGX Spark, GB10 (10 Cortex-X925 cores 5–9 and 15–19, 10 Cortex-A725 cores 0–4 and 10–14, 128 GB unified memory, Blackwell GPU), driver 580.178.04, CUDA 13.0, GCC 13.3.0, libzmq 4.3.5, kernel 7.0.0-1019-nvidia, Ubuntu 24.04 container. The GPU was shared with no other process during every retained run (checked before and during each run; one earlier series with a foreign GPU process was discarded and repeated).
- **Revision.** Broker, sources and sinks built Release, `OCUDU_GPU_CHANNEL_CUDA_ARCHITECTURES=120`, from `ehs0/ocudu-gpu-channel` `RAN_benchmark` at `364391bccd3abd5fb73946e50c3a4c95ce649551`, whose `src/`, `apps/` and `include/` equal upstream `main` at `5f11fd2669aff92de87dc0bd26744c59c030de84` except for the opt-in sample-indexed control (`take_effect_at_sample`, receiver-noise epoch; `src/broker.cpp` +41, `src/cuda_backend.cu` +14, `src/control_server.cpp` +86, `include/ocudu_gpu_channel/runtime_control.h` +87 lines), which is inert unless a control client names a target sample. `apps/ocudu_zmq_source.cpp`, `apps/ocudu_zmq_sink.cpp` and `src/config.cpp` are identical. SHA256 (first 16): `ocudu-gpu-channel` `f948c3950cfe49f0`, `ocudu-zmq-source` `b2cbd436cfa22494`, `ocudu-zmq-sink` `37e8d94ce5da4d2c`.
- **Topology.** One gNB and N UEs, one port per node, N uplink and N downlink links, each an 8-tap TDL (delays spread over 2.5 µs, gains −3 to −20 dB), one AWGN per receiving node, `batch_samples` = one slot (23,040 at 23.04 MS/s; 61,440 at 61.44 MS/s and at 122.88 MS/s with 30 kHz SCS), `queue_samples` = 32 slots. Generated by `tools/gen.py`; `data/topology-*.yaml` are three of the files used.
- **Radios.** One `ocudu-zmq-source` per port (pushes a constant batch as fast as the broker accepts it; the broker's pacing sets the rate) and one `ocudu-zmq-sink` per port (one outstanding request, re-issued once per slot, `--request-interval-us` = slot). Sources for 26 s, broker for 24 s, sinks for 20 s starting 3 s after the sources; the ratio is over the sinks' 20 s. One run per point unless a repeat is shown; repeats at the edge agree within ±0.05 (24 UEs with four threads: 0.87 and 0.87; 100 MHz / 4 UEs: 0.91 and 0.91) and across days within about ±0.08 (24 UEs with two threads: 0.62 to 0.75 over placements).
- **Placement.** Broker on cores 5–14 (five X925, five A725) with `OCG_BROKER_SPIN=0` and `OCG_ZMQ_IO_THREADS=2` unless stated; radios on cores 15–19. Rows marked ¹ and the `phase2`/`phase2b` series had the radios on the A725 cores and the broker on the ten X925 cores with `OCG_BROKER_SPIN=2`.
- **Radio placement matters.** The synthetic radios are not free: on the A725 cores they cannot feed 25 ports at 20 MHz (0.60 with the `data` stage at 1.1 ms, `data/phase6.csv` `srcLittle`) nor one port at 100 MHz (0.55 against 0.999 on an X925). Every retained 50 and 100 MHz row has the radios on X925 cores.
- **What this does not measure.** No MAC, HARQ or traffic: the IQ is a constant synthetic batch, so this bounds the channel path alone. 20 s windows, one seed per topology. Ratios below 1 here are the broker's; in a live system the radios' own turnaround adds to the same lock-step cycle.

## The Sionna side: not in this measurement, and where it stops

The channel updates above are synthetic swaps sent at Sionna's rate; the Sionna RT solver itself was not running during any retained run, and the GPU held the broker alone. (The live single-cell benchmark also pre-computes its trace before the radios start, so its ratios do not contain the solver either; the two-cell benchmark runs the solver live.) The solver has its own limit, visible in the recorded `channel-manifest.json` timing of 102 single-cell benchmark sessions on this GB10 (Sionna RT `PathSolver`, Mitsuba `cuda_ad_mono_polarized`, Dr.Jit 1.3.1, SUTD test scene, max depth 3, 200,000 samples per source, LOS + specular reflection, one solve per 100 ms step for all links of the cell; `data/sionna-solve-times.csv`):

| UEs (links) | Sessions | Solve per step, mean (median over sessions) | Solve per step, max (median over sessions) |
|---:|---:|---:|---:|
| 1 (2) | 8 | 26 ms | 79 ms |
| 2 (4) | 3 | 30 ms | 103 ms |
| 4 (8) | 39 | 32 ms | 135 ms |
| 8 (16) | 43 | 45 ms | 187 ms |
| 16 (32) | 9 | 88 ms | 484–916 ms |

At the 10 Hz update rate the budget is 100 ms per step: 16 UEs use 88 % of it on average and the tails exceed it several times over, so a solver running live at that rate stops near 16–18 UEs per scene on this machine, before or alongside the broker's 16–24. Running it on the same GPU as the broker adds the context-switching cost of the previous section unless it is an MPS client (measured for the robot-ring demo in September: broker p99 1.5 ms with the solver in its own context, 0.21 ms under MPS); that combination was not re-measured here.

## A note on `ocudu-gpu-channel-bench`

The bench reports per-call latency correctly, but it is not a throughput tool for many concurrent nodes: with `OCG_BENCH_THREADS=1` and `OCG_BENCH_PACE=1` it saturates at about 24,500 calls per second for 23,040-sample slots and 10,900 for 61,440-sample slots whatever the node count, because each call's output-power accounting (a sum over every output sample) runs under one global mutex ([`apps/ocudu_gpu_channel_bench.cpp`](../../../../apps/ocudu_gpu_channel_bench.cpp), `record_mutex`). The call count it reaches under pacing therefore measures that loop, not the GPU, and no number from it is used above; the broker's own stage timings are.

## Reproduction

```sh
# one point: 24 UEs, 20 MHz, broker on 5-14 blocking, radios on 15-19, four I/O threads
cd tools
echo "B-24-io4 24 23040000 15 4 tcp 0 0 0" | BD=<build dir> BIG=5-14 LITTLE=15-19 bash phase4.sh out.csv phase4
# stage breakdown of the broker log the run wrote
python3 stagestat.py p4-B-24-io4/broker.log
# K brokers with Sionna-rate updates (needs pyzmq for updater.py)
echo "M-20-4ue-b3 4 23040000 15 8 3 10" | BD=<build dir> PY=python3 bash phase3.sh out.csv phase3
```

`phase2.sh` is the plain single-broker driver (columns: label, UEs, sample rate, SCS, taps, gNB ports, UE ports, cells); `phase4.sh` adds the I/O-thread, transport (`tcp`/`ipc`), hop-trace, control-endpoint and spin columns and retries a broker that fails to start; `phase3.sh` runs K brokers with an optional update rate. The drivers pin processes with `taskset`; adjust `BIG`/`LITTLE` to the host.

Data: `data/phase2*.csv` (shipped broker, first sweeps), `data/phase4.csv` (I/O threads, transport, trace/control), `data/phase5.csv` (updates, brokers per GPU), `data/phase6.csv` (edge with four threads, placement variants), `data/phase3.csv` (an earlier run of the phase 5 list with a foreign GPU tenant present; kept for the record, not used), `data/broker-stages.csv` (per-run stage means), `data/broker-stop-lines.txt` (each broker's final counters: pulls, requests, starvations, control updates applied), `data/sionna-solve-times.csv` (solver timing per recorded benchmark session).

One operational note: after a day of runs the container held about 96 GB of page cache and the driver intermittently failed to create a GPU context (`cudaSetDevice failed: out of memory`; kernel log `kgrctxAllocCtxBuffers: NV_ERR_NO_MEMORY`) with 9 GB free; a temporary 48 GB anonymous allocation inside the container reclaimed the cache and the failures stopped. `phase4.sh` retries a broker start for that reason.

## Appendix: local patches and knobs in the measured stack

**Broker (the subject of this report): no patch.** The broker, `ocudu-zmq-source` and `ocudu-zmq-sink` are built unmodified from `ehs0/ocudu-gpu-channel` `RAN_benchmark` at `364391b`; relative to upstream `main` `5f11fd2` that tree carries only the opt-in sample-indexed control named under "Revision" (inert here except in the update runs, where `batch_commit` without `take_effect_at_sample` applies at the next call boundary as upstream does). Every broker setting used is an existing environment knob or flag: `OCG_ZMQ_IO_THREADS`, `OCG_BROKER_SPIN`, `OCG_HOP_TRACE_DIR`, `--control-endpoint`, `--duration`; `runtime.pacing` and `runtime.cuda_host_memory` at their defaults (pacing on; zero-copy on the GB10). The files under `tools/` are measurement drivers around the shipped binaries and change nothing in them.

**Live stack used for the cross-check** (section "Live stack"; none of these touch the broker):

| Patch or knob | Where | What it does and why it was in the stack |
|---|---|---|
| `integrations/srsran/patches/srsue-ldpc-neon.patch` | `ehs0` `RAN_benchmark` `364391b` (srsUE patch lock; not on upstream `main`) | Native NEON LDPC decoder for srsRAN 4G on aarch64 (a port of its AVX2 decoder, selected by default on aarch64). Without it srsUE decodes PDSCH with the scalar decoder (1.6–2.9 ms per 20 MHz slot) and the live cell is UE-bound at 0.40–0.57, which would hide the broker; with it 8 UEs run at 0.93–0.94 and the broker becomes visible. |
| `integrations/ocudu/patches/ocudu-zmq-lower-phy-profile.patch` | upstream `main` and `ehs0` (applied in the `ocudu-lphy-release` gNB build) | Adds `OCUDU_LPHY_RX_TO_TX_MAX_MS` to OCUDU's lower PHY; run with `3` so the gNB keeps a two-block TX lead instead of answering each UL block with lead 0. `OCUDU_LPHY_DL_WAIT_MS` and the threaded profiles (`OCUDU_ZMQ_LOWER_PHY_PROFILE=single|dual`) of the same patch were not used (the profiles stall the gNB with srsUE). |
| `srsue-ra-contention.patch`, `srsue-rar-window.patch`, `srsue-zmq-rx-sleep.patch` | `ehs0` srsUE patch lock (pre-existing) | Multi-UE RACH preamble selection, RAR window, and the `rx_sleep` switch (run with `false`). Needed for 8–16 srsUEs to attach on one lock-step broker; unrelated to the channel path. |
| `use_cases/ran_benchmark/run-single-cell-policy.sh` knobs | `ehs0` `364391b` | Per-process CPU pins (`SINGLE_CELL_CPUS_GNB=5-12`, `_BROKER=15-16`, `_UES=17-19`, `_AUX=0-4`, `_UE<i>`), `SINGLE_CELL_UE_RX_SLEEP=false`, `SINGLE_CELL_QUIET_LOGS=1`, `SINGLE_CELL_PCAP=off`, `SINGLE_CELL_GNB_BINARY=<lphy build>`; broker env `OCG_BROKER_SPIN=2` with `OCG_ZMQ_IO_THREADS=2` (recipe) or `4` (the cross-check). |
| gNB binary for the two-cell runs | isolated native root | The two-cell runner has no gNB-binary knob, so the root's `builds/ocudu-zmq-release` link was pointed at the `ocudu-lphy-release` build for the runs and restored afterwards. |

Sessions: single-cell `tagged-io4-8ue-20261026T101316Z-g107`, `tagged-io4-16ue-20261026T101824Z-g108` (compared with `tagged-lock-8ue-20261026T055841Z-g104` and the 16-UE recipe session of 2026-10-24), two-cell report `20261026T102631Z` (compared with `20261026T074139Z`), all under the isolated native root `/workspace/ocudu-spark-ranb/results/` on the Spark.
