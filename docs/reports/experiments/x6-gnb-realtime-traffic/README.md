# X6 — gNB real time under two-UE uplink load (lower-PHY thread profile)

> Dated evidence, preserved from `docs/plans/x6-gnb-realtime-traffic.md` at `58d3156`. Results apply only to the recorded revisions, hardware and configurations; see [current status](../../../getting_started/status.md).


Status: 2026-10-01, done. Findings, two knobs, three measured runs (one wedge caught in gdb); results at the bottom.

## Why

CROSSTALK_MILESTONES.md "트래픽 하 중계 정지" (Track D): with two srsUEs each
running iperf3 UL at 20 Mbit/s through the broker, the OCUDU gNB stopped
transmitting after ~33 s of UL (runs `ocudu-multi-ue/20261001T104716Z`,
`…T105444Z`) and the lock-step relay wedged. The gNB runs its lower PHY in
"sequential baseband" mode: lower-PHY TX/RX and the whole upper PHY on one
`phy_worker`. Goal here: make the gNB keep real time under that load, then
re-measure the wire levels under traffic (`TX_POWER_UL` check).

## What the OCUDU source (a1916edc) actually offers

`apps/units/flexible_o_du/split_8/helpers/ru_sdr_config.h` — `lower_phy_thread_profile`:

| profile | threads (per cell, `apps/services/worker_manager/worker_manager.cpp` `create_lower_phy_executors`) |
|---|---|
| `blocking` → worker-manager `sequential` | none of its own: lower PHY runs on `phy_exec` (= the single `phy_worker`, no RT priority, which also runs the whole upper PHY — `create_du_low_executors`, `!rt_mode`); only the `radio` worker (ZMQ sockets) is separate |
| `single` | `lower_phy#N` (RT max, `ru` affinity) + `radio`; upper PHY in the RT worker pools |
| `dual` | `lower_phy_tx#N` (RT max) + `lower_phy_rx#N` (RT max-1) + `radio` |
| `triple` | `lower_phy_tx#N` (max) + `lower_phy_rx#N` (max-2) + `lower_phy_ul#N` (max-1, demodulation) + `radio` |

YAML key (`ru_sdr_config_yaml_writer.cpp`, CLI `--execution_profile single|dual|triple`):

```yaml
expert_execution:
  threads:
    lower_phy:
      execution_profile: dual
  cell_affinities:          # optional, per cell
    - ru_cpus: 5-9          # mask for radio + lower-PHY workers
      ru_pinning: mask
```

Default without the key: by host CPU count (`<4` single, `<8` dual, else triple).

**The catch — ZMQ forces sequential, three times over:**

1. `ru_sdr_config_cli11_schema.cpp:299` `autoderive_ru_sdr_parameters_after_parsing`:
   `if (device_driver == "zmq") execution_profile = blocking;` — runs after the
   YAML/CLI are parsed, so a configured profile is silently discarded.
2. `ru_sdr_config_translator.cpp:254` `fill_sdr_worker_manager_config`: zmq →
   `worker_manager_config::ru_sdr_config::lower_phy_thread_profile::sequential`
   regardless of the parsed value.
3. `split_8_o_du_application_unit_impl.cpp:87` `is_blocking_mode_enable = (device_driver == "zmq")`
   → `du_low_cfg.is_sequential_mode_active`, which also puts the upper PHY on
   the single `phy_worker` and sizes the DU-high workers for blocking mode.

So no YAML/CLI/env on a1916edc changes the lower-PHY threading of a ZMQ gNB.
The design intent (srsRAN heritage) is that a ZMQ radio has no clock: the PHY
is paced by sample arrival, and one thread guarantees TX never races RX.
Changing it is a source patch (three one-line sites above plus checking the
ZMQ radio under concurrent TX/RX: `radio_zmq_rx_stream.cpp` aligns RX to the
TX sample count with `RECEIVE_TS_ALIGN_TIMEOUT = 100 ms` then pads zeros), to
be built as a separate OCUDU build dir — outside this track's edit scope, so
it is reported, not done.

What the rendered `gnb.yaml` had (`configs/ocudu-multi-ue-native/20261001T105444Z/gnb.yaml`):
no `expert_execution` section at all; console: `Lower PHY in executor sequential baseband mode.`
The CUDA renderer (`scripts/cuda/render-cuda-1x1-configs.py`) never touches
`expert_execution` either; it appends `ru_sdr.expert_cfg` + top-level
`expert_phy` and refuses a yaml that already carries those — the new block uses
a different top-level key, so the two compose.

## The knob (edited files)

| File | Change |
|---|---|
| `scripts/native/render-multi-ue-configs.py` | `OCUDU_NATIVE_GNB_LOWER_PHY_PROFILE` = `single|dual|triple` appends the `expert_execution.threads.lower_phy.execution_profile` block to `gnb.yaml`; unset/empty = fixture untouched (byte-identical render verified); other values fail the render. The Sionna renderer inherits it (`legacy.render_gnb`). |
| `scripts/native/run-ocudu-multi-ue.sh` | Validates the value, records `gnb_lower_phy_profile` in `run-parameters.json` and the `native_multi_ue_gate_parameters` line. |
| `scripts/native/run-ocudu-multi-ue-inner.sh` | `attach-summary.json` gets `gnb_lower_phy_mode` = the `Lower PHY in … mode.` line from `gnb-console.log`, so what the gNB really ran is on record (requested ≠ effective, see above). |
| `scripts/native/platform-profiles.json` | `gnb_lower_phy_note` on `spark-gb10`: placement unchanged; `gnb_env` exports `OCUDU_NATIVE_GNB_MAIN_POOL_THREADS=5` (see Run 3). |
| `scripts/native/platform-profile.py` | Generic `gnb_env`: profile-provided `OCUDU_NATIVE_GNB_*` defaults printed as `export` lines (env wins), recorded in `--json`. |
| `scripts/native/render-sionna-multi-ue-configs.py` | Comment only: traffic-regime wire levels next to `TX_POWER_*` (constants unchanged, see bottom). |

CPU placement decision (GB10): all ten big cores are allocated (gNB 5-9, broker
15-17, UE 18,19). `dual` adds 2 and `triple` 3 RT threads, which fit in the
gNB's five cores next to the upper-PHY pool, `radio`, DU-high and IO threads;
taking a core from the broker (S11: three needed for p99 65 us) or the UEs
(S11: one core falls below real time) would cost more than it gives, and little
cores are excluded on purpose (the profile's `why`). `OCUDU_NATIVE_GNB_CPUS`
overrides per run. Today the profile is inert anyway (above), so nothing
measurable depends on this yet. What the pin DOES change is the gNB's main
worker pool size (see Run 2): that is why the profile now exports
`OCUDU_NATIVE_GNB_MAIN_POOL_THREADS=5`.

## Run (DGX Spark, xtree + these files, build `gpuch-xt-release`)

Sionna multi-UE gate, `use_cases/configs/sionna/scenarios/robot_ring/robot-ring-walk.json`, 200 s, Track A's
iperf hooks (two iperf3 servers on 5201/5202; per UE 90 s UL at 20 Mbit/s then
30 s DL), wire capture 4,000,000 samples after SKIP_SECONDS=20 (UL starts at
run-second ~9-11, the old wedge came at ~42, so 20 s is inside the UL window),
`OCUDU_NATIVE_GNB_LOWER_PHY_PROFILE=dual`. A watcher (`/workspace/gpuch/xt/x6-watch.sh`)
dumps `thread apply all bt` of the gNB (and both srsUEs) the moment the broker
logs `event=node_stall` after UL has started.

Note: xtree is a snapshot of 442513f + X0–X2 (commit 0b3a980), not 54492f7;
its broker has no `OCG_BROKER_WEDGE_TIMEOUT_MS`, so a wedge runs to the end as
before. Only the four files above were copied in.

## Results

### Run 1 — `ocudu-multi-ue/20261001T114501Z`, `OCUDU_NATIVE_GNB_LOWER_PHY_PROFILE=dual`

- gNB console: `Lower PHY in executor sequential baseband mode.` — the knob is
  **inert** on a1916edc, exactly as the source says (the dry run accepts the key).
- No wedge this time: verdict `passed` (strict counters 0/0/0), rx_starvations
  3154 (vs 1038 in the wedged 105444Z, 45 in the idle 100228Z), no
  `node_stall` after attach. ue0 UL **20.0 Mbit/s for the full 90 s** (215 MB,
  118 retr); ue1 UL 1.95 Mbit/s over 190 s, 0 B received.
- ue1 was not a relay problem: at 11:45:54 the gNB MAC logged
  `RLF detected. Cause: 100 consecutive HARQ-ACK KOs` for rnti 0x4602 and at
  11:46:03 released it (`UE Context Release`, srsUE prints `Received RRC
  Release`). ue1 is the ring-walk UE that alternates LOS (-3 dB) and the pillar
  shadow (-27 dB) every ~10 s; at 20 Mbit/s UL with MCS 28 the shadow phases
  produce PUSCH/HARQ KO streaks (242 `crc=KO` for 0x4602). UE cores 18,19 sat
  at 89-96 % during UL (both srsUEs on two cores), so the UE side is also at
  its limit. The DL iperf phases never ran (ue0's server connection died
  after its UL test; ue1 was released).
- Wire levels under traffic (`wire-capture-power.py`, 4,000,000 samples at
  skip 20 s): gNB tx_in active mean **3.40e-3 (-24.7 dB)**, peak 0.47; ue0
  tx_in **9.72e3 (39.9 dB)**, peak 313, active 99 %; ue1 tx_in 3.25e4 (45.1 dB)
  but only 0.7 % active (control bursts, it was in shadow in the window).

### Run 2 — `ocudu-multi-ue/20261001T115125Z`, default profile (same gNB)

- **Wedged at UL t≈32 s** (iperf 11:51:44, last gNB PHY line 11:52:16.9, then
  `zmq:tx` and `zmq:rx` both `Waiting for data` once per second), as in the
  two Track-D runs — so the wedge is probabilistic (1 of 2 here, 2 of 2 there),
  and the lower-PHY profile has nothing to do with it.
- `gdb -p <gnb> thread apply all bt` while wedged
  (`/workspace/gpuch/xt/gnb-threads-20261001T115426Z.txt`, wchan in
  `gnb-wchan-…`): the gNB has **9 threads**: `gnb` (main: nanosleep), logger,
  `radio` (zmq_recv in `radio_zmq_rx_channel::receive_response`, i.e. idle
  polling), `io_broker_epoll`, two ZMQ bg threads, and the three that matter:

  ```
  phy_worker   pthread_cond_wait <- sync_task_executor::execute
               <- mac_cell_processor::handle_slot_indication <- fapi slot_indication
               <- upper_phy on_tti_boundary <- lower_phy downlink_processor_baseband_impl::process
  main_pool#0  pthread_cond_wait <- sync_task_executor::defer
               <- rlc_tx_am_entity::handle_changed_buffer_state <- rlc_tx_am_entity::handle_sdu
               <- f1u_bearer_impl::handle_pdu (DL NR-U PDU from the CU-UP, i.e. TCP ACKs for the UL flow)
  main_pool#1  pthread_cond_wait <- sync_task_executor::defer
               <- timer_manager::trigger_timeout_handling <- timer_manager::tick (io timer)
  ```

  **Cause: the DU-high executor pool starves itself.** In ZMQ ("blocking")
  mode `du_high_executor_mapper.cpp` decorates every cell executor
  (`slot_ind_exec`, `mac_cell_exec`, `rlc_lower_exec` — strands over the main
  pool) with `sync` (`is_sync = !rt_mode_enabled`), so a caller blocks until
  its task has run on a main-pool worker. The main pool is sized by
  `worker_manager.cpp get_default_nof_workers` as
  `min(cells*(dl_ant+ul_ant+1)+2, avail_cpus-3)` = `min(5, 5-3)` = **2 workers
  under the GB10 pin to cores 5-9** (unpinned: 5). With two UEs in traffic,
  the moment both workers are inside a sync wait (an RLC buffer-state update
  and a timer handler) nobody is left to run the cell strand they, and the
  phy_worker's slot indication, are waiting for. Lower PHY stops producing
  DL samples -> broker `gnb0 wait_data` -> both UEs `wait_data` -> relay
  wedged. The srsUEs are innocent bystanders (both parked in
  `rf_zmq_rx_baseband` / `zmq_recv`). This is the same mechanism Track D
  inferred from the heartbeat (gNB TX stops first), and the same family as
  M5.4.
- The gate still said `passed`: xtree's broker predates the fail-fast, and
  the summary gates only overflows/gaps/zmq_errors. UL ue0 3.18 Mbit/s avg
  (20 Mbit/s until the wedge), rx_starvations 1204. Wire levels in the
  (partial, 0.78-0.85 M samples) capture: gNB 3.70e-3, ue0 9.90e3, ue1
  3.25e4 (0.8 % active) — same as run 1.

### Mitigation in scope: `OCUDU_NATIVE_GNB_MAIN_POOL_THREADS`

`expert_execution.threads.main_pool.nof_threads` is a real knob
(`worker_manager_cli11_schema.cpp`), so the renderer now also writes it from
`OCUDU_NATIVE_GNB_MAIN_POOL_THREADS` (unset = untouched; recorded in
`run-parameters.json`). Run 3 (below) uses 5, the value an unpinned host gets.
The true fix is upstream: a sync executor must not be dispatched from a worker
of the pool it waits on (or blocking mode must size/structure the pool so it
cannot be exhausted).

### Run 3 — `ocudu-multi-ue/20261001T120308Z`, default profile, `OCUDU_NATIVE_GNB_MAIN_POOL_THREADS=5`

- **Held real time for the whole 200 s**: no `node_stall` after attach (6 =
  the attach-time ones), gNB PHY logging to the last second, strict counters
  0/0/0, rx_starvations 3211 (same band as run 1's 3154; the wedged run 2
  had 1204 because it stopped early), gNB and both srsUEs alive at broker
  stop. ue0 UL **20.0 Mbit/s for the full 90 s** (215 MB, 115 retr), past the
  UL+32 s mark where run 2 and both Track-D runs died.
- Gate verdict `failed` — for ue1 only: `attach_clean=0` because the gNB
  declared RLF (`RLC max ReTxs reached`, 12:04:02, UL+23 s) and released it,
  as in run 1 (there: HARQ-KO streak). ue1 UL 519 kbit/s. This is the
  shadowed ring-walk UE under full-buffer UL (and two srsUEs sharing cores
  18,19 at ~95 %), a link/UE-side problem independent of the relay; worth
  its own track (lower UL rate for the far UE, or MCS cap).
- Wire levels (4,000,000 samples at skip 20 s): gNB tx_in 4.06e-3
  (-23.9 dB), ue0 1.58e4 (42.0 dB, 75 % active — a mix of PUSCH slots and
  control), ue1 3.25e4 (control-only, 0.7 % active). Same picture as runs 1/2.

Tally: default pool (2 workers) wedged 3 of 4 runs under this load (Track D
2/2, here 1/2); pool of 5 held 1 of 1. One run is not statistics, but the
mechanism is pinned by the backtrace and the sizing formula, so spark-gb10
now exports `OCUDU_NATIVE_GNB_MAIN_POOL_THREADS=5` by default
(`platform-profiles.json` `gnb_env`, resolved by `platform-profile.py` as
`export …` lines the gates `eval`; a caller's own value wins). Only the
srsUE multi-UE renderers read the variable, so other gates' renders are
unchanged. Repeating the default-pool run a few more times would firm up
the rate; the fail-fast broker (54492f7) makes a wedge a hard failure.

### TX_POWER constants

Under 20 Mbit/s traffic both wire levels sit ~5 dB below the constants
(gNB -24.7 vs -19.5 dB; ue0 39.9 vs 44.8 dB) while the **ratio** — the only
thing `ue_tx_scale_db` uses — is 64.6 dB vs 64.3 dB, within 0.3 dB. The
idle-ish UE (ue1, control bursts) measures 3.25e4, i.e. the current
`TX_POWER_UL` is right for the regime it documents (PUCCH/SRS/small PUSCH);
full-rate PUSCH is simply ~5 dB quieter per sample, and the gNB's PDSCH
slots are ~5 dB quieter than its SSB/PDCCH-dominated idle slots. Changing
`TX_POWER_UL` alone to 9.7e3 would move the UE scale by +4.9 dB (UEs louder
than the -7 dB offset intends under traffic); changing both would move the
absolute noise floor by 5 dB and invalidate the multi-UE gate's SNR calibration. So the
constants are left alone and the comment records the traffic-regime numbers
(coordinator's call whether the floor should reference loaded slots).
