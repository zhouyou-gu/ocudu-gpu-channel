# Rootless native gNB–UE live attach

This directory contains the Docker-free 1×1 live gate:

```text
OCUDU gNB (ZMQ) <-> current ocudu-gpu-channel broker <-> srsUE
                         |
                     Open5GS 5GC
```

It is intentionally a single-gNB, single-UE path. No multi-antenna engine is
required or enabled by this harness.

## Why it does not need host-wide privileges

The runner starts with the invoking user's normal permissions and creates a
user, network, and mount namespace with:

```bash
unshare --user --map-root-user --net --mount
```

`CAP_NET_ADMIN` and `CAP_SYS_ADMIN` exist only inside that disposable user
namespace. The runner creates `ogstun` and the nested `ue1` network namespace
there, then removes them during cleanup. It does not invoke `sudo`, Docker, or
modify the host network namespace.

The host must still permit unprivileged user namespaces and expose
`/dev/net/tun` to the user. Test the first requirement with:

```bash
unshare --user --map-root-user --net --mount --fork /bin/true
test -c /dev/net/tun
```

## Required native workspace

Set `OCUDU_NATIVE_ROOT` to the dedicated workspace containing the pinned OCUDU
gNB, srsUE, Open5GS, MongoDB, and user-space runtime libraries described by
`native-workspace.lock.json`. The default is
`/home/ubuntu/ocudu-native-workspace`.

Provision it without sudo or Docker from the repository root:

```bash
export OCUDU_NATIVE_ROOT="$HOME/ocudu-native-workspace"
./scripts/native/bootstrap-workspace.sh \
  --root "$OCUDU_NATIVE_ROOT" --jobs "$(nproc)"
```

The bootstrap downloads hash-locked inputs, extracts the user-space dependency
overlay, checks out the exact audited revisions, and builds every binary and
Open5GS module used by the live gate. To validate an existing workspace without
downloading or rebuilding it:

```bash
export OCUDU_NATIVE_ROOT="$HOME/ocudu-native-workspace"
./scripts/native/bootstrap-workspace.sh \
  --verify-only --root "$OCUDU_NATIVE_ROOT"
```

The relevant pinned binaries are:

- `builds/ocudu-zmq-release/apps/gnb/gnb`
- `builds/srsran4g-zmq-release/srsue/src/srsue`
- `builds/open5gs-v2.7.6/tests/app/5gc`
- `install/mongodb-6.0.29/bin/mongod`

The broker is not taken from a stale workspace build. The live gate configures
and builds the current checkout into
`builds/ocudu-gpu-channel-cuda-release`, probes that exact binary, and passes
the same path to the runtime namespace.

## Run the 1×1 attach gate

From the repository root:

```bash
export OCUDU_NATIVE_ROOT="$HOME/ocudu-native-workspace"
# This host's installed CUDA compiler. Override it if CUDA is elsewhere.
export CUDACXX="$(command -v nvcc)"
export OCUDU_NATIVE_GPU_DEVICE=0

./scripts/native/run-ocudu-legacy-1x1.sh
```

The gate performs these checks in order:

1. Verifies pinned source revisions, cached inputs, native binaries, ZMQ, SCTP,
   `/dev/net/tun`, and rootless namespace support.
2. Renders loopback-only gNB, broker, Open5GS, subscriber, and srsUE configs.
3. Builds and tests the current broker with CUDA enabled.
4. Probes CUDA and the new broker inside an isolated user/network/mount
   namespace.
5. Starts MongoDB, Open5GS, the broker, OCUDU gNB, and srsUE.
6. Requires RRC connection, PDU-session establishment, and three successful
   pings from `tun_srsue` to `10.45.1.1`.

A successful run ends with:

```text
event=native_legacy_1x1_attach_gate result=pass ...
```

Run evidence is stored under:

```text
$OCUDU_NATIVE_ROOT/results/logs/ocudu-interop/<UTC timestamp>/
$OCUDU_NATIVE_ROOT/results/reports/ocudu-interop/<UTC timestamp>/
```

The report includes the exact source manifest, binary/configuration SHA-256
hashes, Broker counters, and attach summary. Any process or namespace created
by the gate is terminated on normal exit, failure, or Ctrl-C.

## Run gNB–UE with Sionna RT and the Web UI

The Sionna path uses the same Docker-free 1×1 gNB–UE stack. It adds a
low-rate Sionna RT controller and the read-only Web UI; it does not enable a
multi-antenna topology. Only one terminal is required:

```bash
export OCUDU_NATIVE_ROOT="$HOME/ocudu-native-workspace"
export CUDACXX="$(command -v nvcc)"
export OCUDU_NATIVE_GPU_DEVICE=0

./scripts/native/run-ocudu-sionna-1x1.sh
```

The launcher discovers the locally installed OptiX library and uses
`../venvs/sionna/bin/python` by default. Override the Python executable with
`OCUDU_NATIVE_SIONNA_PYTHON` when needed. It prints the Web UI URL after the
HTTP server starts and prints `event=native_sionna_1x1_live_ready` only after
all of these conditions hold:

1. The OCUDU gNB and srsUE establish RRC and a PDU session.
2. The UE namespace successfully pings `10.45.1.1` through `tun_srsue`.
3. Sionna RT atomically updates both directed 1×1 channel profiles.
4. The Web UI receives both the Sionna JSONL feed and Broker telemetry.

Open <http://127.0.0.1:8080> while the command is running. The default live
run continues until Ctrl-C. For an automatically terminating evidence run,
set a duration in seconds before starting it:

```bash
export OCUDU_NATIVE_SIONNA_DURATION_SECONDS=150
./scripts/native/run-ocudu-sionna-1x1.sh
```

Optional settings are `OCUDU_NATIVE_WEB_PORT` (default `8080`),
`OCUDU_NATIVE_SIONNA_UPDATE_HZ` (default `10`), and
`OCUDU_NATIVE_SIONNA_READY_SECONDS` (default `120`). The Web server is
restricted to loopback.

Node positions can come from a live publisher instead of the scenario's
scripted routes (a simulator, a motion log, anything that publishes positions):
`OCUDU_NATIVE_SIONNA_POSITION_ENDPOINT` is passed to the bridge as
`--position-endpoint`, with the optional `OCUDU_NATIVE_SIONNA_POSITION_OFFSET`
(`x,y,z`, publisher-frame origin in scene metres) and
`OCUDU_NATIVE_SIONNA_POSITION_TIMEOUT_S` (default `1`). The bridge runs inside
the gate's `unshare --net` namespace, so a `tcp://127.0.0.1` publisher on the
host is unreachable from it: the endpoint must be an absolute `ipc://` socket
that the publisher **binds** on the shared filesystem, for example
`ipc:///workspace/ocudu-spark/run/arena/positions.sock`, and the bridge
connects to it (the same rule as the control and telemetry sockets). The
message is one JSON frame per update,
`{"event":"positions","t_unix_ms":…,"frame":"arena","nodes":{"ue0":{"position_m":[x,y,z],"velocity_mps":[vx,vy,vz]}}}`;
nodes it omits keep their scripted route, unknown node ids are ignored with
one warning, and after the timeout the last positions are kept and flagged
`stale` in the `sionna_rt_update` status record (`position_source`,
`position_status`). The same variables apply to `run-ocudu-sionna-multi-ue.sh`.

The Sionna multi-UE gate (`run-ocudu-sionna-multi-ue.sh`) has these further knobs; every default keeps the
run the gate always did:

| Variable | Default | Effect |
| --- | --- | --- |
| `OCUDU_NATIVE_SIONNA_AWGN_SNR_DB` | `40` (`off` = none) | Absolute receiver noise floor on every node (`rx_model`, an `awgn` step with `noise_power`), sized so a unit-gain link sees this SNR: `noise = tx_power / 10^(snr/10)` with the transmit levels measured on the wire (`render-sionna-multi-ue-configs.py` `TX_POWER_DL`/`TX_POWER_UL`, `wire-capture-power.py`). A `snr_db` step would size its noise against the *faded* signal and give a shadowed UE the same SNR as one in line of sight; the absolute floor is what makes Sionna's path loss reach the decoder. On the ring scene 40 dB reads as 34 dB (LOS) / ~16 dB (pillar shadow) at srsUE; 26.6 and 34.6 break srsUE's initial access and sync in the shadow. Sionna mode only. |
| `OCUDU_NATIVE_SIONNA_TX_POWER_DL` / `_UL` | renderer constants | Override the measured transmit levels (mean `\|x\|^2` of active samples). |
| `OCUDU_NATIVE_SIONNA_UL_POWER_OFFSET_DB` | `-7` (23 dBm UE vs 30 dBm gNB) | Emitted UE power relative to the gNB. The renderer folds it, together with the measured wire levels, into each UE port's `tx_scale_db` (`ue_tx_scale_db = offset - 10 log10(TX_POWER_UL / TX_POWER_DL)`, about -71 dB), so a 0 dB tap means the same emitted power in both directions; the gNB noise floor is sized from the scaled uplink. srsUE cannot do this itself: a negative `[rf] tx_gain` means automatic (measured: -21 applied 40 dB) and its floor is 0 dB. |
| `OCUDU_NATIVE_MUE_DURATION_SECONDS` | `240` (min 160) | Broker `--duration`; the attach window is the first 150 s. |
| `OCUDU_NATIVE_MUE_STRICT_REALTIME` | `0` | Pass `--strict-realtime` to the broker (it then exits 1 if any starvation/overflow/gap counter is non-zero, which fails the gate). |
| `OCUDU_NATIVE_MUE_UE_EXEC` | unset | A `bash -c` template started inside each UE's network namespace right after that UE's ping passes (the tun and its address exist only then). Placeholders: `{ue_id}` (ue0…), `{ue_ip}` (10.45.1.2…), `{ue_index}`, `{ue_netns}`, `{ue_gateway}`, `{log_dir}`, `{run_dir}`, `{config_dir}`. Only the network namespace is entered, so the filesystem (ipc sockets, logs) is shared with the stack. Logged to `ue-exec-<ue>.log`, stopped first at teardown. |
| `OCUDU_NATIVE_MUE_ROOT_EXEC` | unset | The same for one command in the stack's own namespace (where `ogstun` 10.45.1.1, the broker and the bridge live), started as soon as `ogstun` exists — the place for a robot brain the UEs reach at `{ue_gateway}`. Placeholders: `{ue_ids}`, `{ue_ips}`, `{ue_gateway}`, `{log_dir}`, `{run_dir}`, `{config_dir}`. Logged to `root-exec.log`. |
| `OCUDU_NATIVE_MUE_WIRE_CAPTURE_SAMPLES` / `_SKIP_SECONDS` | `0` / `60` | Broker wire capture per port and direction into `<log_dir>/wire-capture`; `wire-capture-power.py` reads it. |
| `OCUDU_NATIVE_GNB_MAIN_POOL_THREADS` | `5` on `spark-gb10`, unset elsewhere | `expert_execution.threads.main_pool.nof_threads` in the rendered gNB config. In ZMQ mode OCUDU runs every DU-high cell executor as a synchronous strand over this pool and sizes it `min(5, cores-3)`, i.e. 2 under a 5-core gNB pin; two synchronous waiters (RLC buffer-state, timers) then exhaust it, the slot indication never runs, the downlink stops and the lock-step relay wedges (3 of 4 two-UE uplink-load runs). With 5 threads the gNB held 2x20 Mbit/s uplink for 200 s. See `docs/reports/experiments/x6-gnb-realtime-traffic/README.md`. |
| `OCUDU_NATIVE_GNB_LOWER_PHY_PROFILE` | unset | Writes `expert_execution.threads.lower_phy.execution_profile`; recorded as `gnb_lower_phy_mode` in the summary. Inert for ZMQ radios: OCUDU forces the sequential profile in three source sites. |
| `OCUDU_NATIVE_ATTACH_STRICT` | `1` | Strict attach verdict (X0) in the native multi-UE and multi-gNB inner gates. `1`: a UE is attached only if, besides RRC Connected + PDU session + ping, its srsUE log shows no `Scheduling request failed`, no `Random Access Transmission` after the first `RRC Connected`, no RLF, and every ping was answered (`ping_received == ping_sent > 0`); `0`: the old verdict (ever RRC + PDU + one `ping -c 3` exit 0, which passed a run whose UEs lost the link 30 s after attaching). The summary records `attach_strict` and, per UE, `sr_failures`, `reattach_attempts`, `rlf_count`, `ping_sent`, `ping_received`, `attach_clean`; the gate log gets one `event=ue_attach_evidence ue=… clean=0/1` line per UE. The gate's own teardown release (`Received RRC Release` right before `Stopping ..`) is not counted. |
| `OCUDU_NATIVE_MUE_PIN_UES` | `1` | With a platform profile, the srsUEs are pinned to the profile's UE cores like the OAI gates' nrUE; `0` leaves them unpinned. |

The gate also applies the platform CPU placement of `platform-profiles.json`
(`OCUDU_NATIVE_PLATFORM=none` turns it off), writes every srsUE's per-second
metrics to `srsue-metrics-<ue>.csv` (with `srsue-<ue>.start_unix_ms` for the
wall clock), records what it applied in `run-parameters.json`, and its
`attach-summary.json` carries the control-plane counters, the last broker
heartbeat per device and the Sionna feed status next to the verdict.
`analyze-sionna-multi-ue-run.py --log-dir …` joins the UE metrics with the
bridge's positions and per-link gains and reports the link quality in line of
sight against the shadow. A remote browser can use SSH port forwarding:

```bash
ssh -L 8080:127.0.0.1:8080 YOUR_USER@GPU_HOST
```

### Keeping the RAN KPI panel populated

`OCUDU_NATIVE_GNB_METRICS=1` turns on the per-UE scheduler KPI panel. Two
further switches decide whether that panel has anything to show, and both are
off by default so every gate renders and runs exactly as before:

| Variable | Default | Effect |
| --- | --- | --- |
| `OCUDU_NATIVE_UE_INACTIVITY_SECONDS` | unset (gNB default, 120 s) | Renders `cu_cp.inactivity_timer`, accepted range 1-7200. Without it the gNB releases an idle UE about two minutes after the acceptance ping and the panel correctly reports no connected UEs. |
| `OCUDU_NATIVE_UE_KEEPALIVE_SECONDS` | `0` (off) | Ping interval in the UE namespace, accepted range 0.2-60 s, fractional. Started only on an unbounded run and only after the acceptance verdict is written, logging to `ue-keepalive.log`. |

Set the keepalive interval well under the scheduler's report period
(`metrics.periodicity.du_report_period`, 1 s as rendered). The KPIs are sums
over one report period, so an interval at or above that period leaves the
periods in between reporting zero throughput, zero HARQ counts and no SINR --
truthfully, since nothing was transmitted in them. `0.2` puts five packets in
every report and keeps the row continuously populated:

```bash
OCUDU_NATIVE_GNB_METRICS=1 \
OCUDU_NATIVE_UE_INACTIVITY_SECONDS=7200 \
OCUDU_NATIVE_UE_KEEPALIVE_SECONDS=0.2 \
./scripts/native/run-ocudu-sionna-rank1.sh
```

The CQI and DL RI columns stay empty regardless: this configuration runs with
`csi_rs_enabled: false`, so the scheduler reports `cqi = -1` ("no CSI report")
for the whole run.

Sionna mode stores logs and reports separately from the legacy gate:

```text
$OCUDU_NATIVE_ROOT/results/logs/ocudu-sionna-1x1/<UTC timestamp>/
$OCUDU_NATIVE_ROOT/results/reports/ocudu-sionna-1x1/<UTC timestamp>/
```

The runtime uses Unix-domain ZeroMQ endpoints under the native workspace for
Sionna control and Web UI telemetry. They cross the disposable mount/network
namespace through the shared filesystem without exposing a host TCP control
port or requiring host networking privileges.

## Run scenario-sized rank-1 MISO/SIMO with Sionna and the Web UI

The rank-1 launcher reads the gNB transmit and receive port counts from the
Sionna scenario instead of carrying a separate 2x1 or 4x1 shell setting. The
default scenario resolves to 4 gNB TX ports, 4 gNB RX ports, and a one-port UE
(4x1 DL MISO and 1x4 UL SIMO):

```bash
export OCUDU_NATIVE_ROOT="$HOME/ocudu-native-workspace"
export CUDACXX="$(command -v nvcc)"
export OCUDU_NATIVE_GPU_DEVICE=0
export OCUDU_NATIVE_SIONNA_PYTHON="$HOME/venvs/sionna/bin/python"

./scripts/native/run-ocudu-sionna-rank1.sh
```

To change the live dimensions, point the launcher at an absolute scenario
path and edit `nodes.gnb0.tx_array` / `nodes.gnb0.rx_array`. The native OCUDU
gate accepts 1, 2, or 4 ports in each direction; the srsUE arrays must remain
1x1. The scenario links must remain the single `gnb0 -> ue0` downlink and
`ue0 -> gnb0` uplink:

```bash
export OCUDU_NATIVE_SIONNA_SCENARIO=/absolute/path/to/scenario.json
./scripts/native/run-ocudu-sionna-rank1.sh
```

The renderer derives `nof_antennas_dl`, `nof_antennas_ul`, every ZMQ port,
Broker radio-node ordering, and both control link ids from that scenario. It
does not declare `fixed_mimo`; Sionna supplies a complete matrix profile for
each direction before the gNB and srsUE start. Readiness is reported as
`event=native_sionna_rank1_live_ready`, and run artifacts are stored under
`results/{logs,reports}/ocudu-sionna-rank1/`.

## Run the two-cell native gate with Sionna RT (X-track)

`run-ocudu-multi-gnb.sh` is the Docker-free two-cell gate (two OCUDU gNBs,
two srsUEs, one core, one broker). Besides the fixed-TDL topology it always
ran, it now takes the Sionna bridge the multi-UE gate uses:

| Variable | Default | Effect |
| --- | --- | --- |
| `OCUDU_NATIVE_CHANNEL_MODE` | `legacy` | `sionna`: broker `--control-endpoint`/`--telemetry-endpoint` under the run directory, the bridge (`run_bridge.py --scenario-config …`) started after the broker and before the gNBs, first `sionna_rt_update` awaited (180 s). |
| `OCUDU_NATIVE_SIONNA_SCENARIO` | required in sionna mode | Absolute scenario JSON naming `gnb0`, `gnb1`, `ue0`, `ue1`. Its `links` must be exactly the topology's (same `from>to:model` keys) and its gNB arrays one port, or the broker rejects the first profile swap (`unknown link_id`, `dimensions 1x4 do not match`). |
| `OCUDU_NATIVE_SIONNA_PYTHON` / `_UPDATE_HZ` | required / `10` | As in the multi-UE gate. |
| `OCUDU_NATIVE_MGNB_TOPOLOGY` | `use_cases/configs/topologies/ocudu_docker/topology.multi-gnb.cuda.yaml`; sionna: `use_cases/configs/topologies/sionna/topology.sionna-2gnb-2ue.cuda.yaml` | Broker topology (absolute path). The Sionna one carries the eight serving/intercell `sionna_rt` links, carrier labels, UE `tx_scale_db` and absolute noise floors. |
| `OCUDU_NATIVE_MGNB_WIRE_CAPTURE_SAMPLES` / `_SKIP_SECONDS` | `0` / `60` | Broker wire capture, as in the multi-UE gate. |

The renderer gives the two cells distinct PRACH root sequences
(`prach_root_sequence_index` 1 and 200): the ZMQ radios share the broker's
lock-step time, so both UEs RACH on the same occasion, and with one root each
gNB also detected the other cell's preamble over the inter-cell path and
admitted a phantom UE (`crc=KO` PUSCH for an RNTI the real UE never followed).

## OAI nrUE gates: wire capture, carrier labels, UE transmit scale, TDD multi-UE (X-track)

The OAI gates (`run-ocudu-oai-1x1.sh`, `run-ocudu-oai-2x2.sh`) now render the
same per-port carrier labels as the srsUE gates (gNB `n3-dl`/`n3-ul`, UE the
reverse) and a UE `tx_scale_db`. The OAI nrUE emits every uplink channel at a
fixed digital amplitude per resource element and the ZMQ module scales int16
by 1/32767, so its wire level is tiny and follows the allocation width:
measured 1.17e-5 (-49.3 dB, PUSCH+PUCCH mix over the gate's pings,
oai-1x1/20261001T110037Z) against the gNB's 1.12e-2, i.e. 29.8 dB *quieter*
than the gNB where srsUE is 64 dB hotter. With 23 dBm vs 30 dBm the UE port
gets `tx_scale_db` +22.8 dB. The fixed-TDL OAI topologies have no absolute
noise floor, so the scale only changes the level the gNB receives (PUSCH now
about -22 dB instead of -45 dB). The 2x2 gate uses the same constant: its
per-channel levels match 1x1 within 0.3 dB (OAI emits a fixed amplitude per
RE) and the stock single-layer uplink leaves port 1 silent
(oai-2x2/20261001T114323Z; with UL rank 2 both ports transmit at the same
per-RE level).

| Variable | Default | Effect |
| --- | --- | --- |
| `OCUDU_NATIVE_OAI1X1_WIRE_CAPTURE_SAMPLES` / `_SKIP_SECONDS` | `0` / `2` | Broker wire capture for the OAI 1x1 gate (per port, skip counts from the port's first sample). |
| `OCUDU_NATIVE_OAI_UE_TX_POWER` | `1.17e-5` | Measured OAI nrUE wire level the scale is derived from. |
| `OCUDU_NATIVE_OAI_UE_TX_SCALE_DB` | derived (`+22.8`) | Force the UE `tx_scale_db`, or `off` for the pre-X1 topology. |
| `OCUDU_NATIVE_OAI2X2_WIRE_CAPTURE_SAMPLES` / `_SKIP_SECONDS` | `0` / `2` | Broker wire capture for the OAI 2x2 gate (same semantics as 1x1). |

A PRACH timing-advance mis-detection seen once at the scaled level
(oai-1x1/20261001T110703Z: 14 us instead of 0.78 us, two extra RAs) is not a
level effect. The uplink reached the gNB with a +601 Hz offset, 0.48 of a PRACH
subcarrier, which puts a Zadoff-Chu side peak at -0.7 dB of the main one and
13.3 us away for that preamble's root; 22 of 64 preambles are vulnerable at
any level. Two causes: the broker's `phase` step also applied the link's CFO
(fixed: 125 Hz is now 125 Hz, it used to arrive as 250 Hz), and the gate's
`--cont-fo-comp 1` pre-rotates the uplink instead of cancelling. Setting
`OCUDU_NATIVE_OAI_UE_CONT_FO_COMP=3` leaves the uplink unrotated (verified:
side peak -12 dB, TA 0.78 us). See `docs/reports/experiments/x7-oai-levels-prach/README.md`.

**TDD two-UE gate (`run-ocudu-oai-multi-ue.sh`, X3/X4).** One OCUDU gNB on a
TDD n78 20 MHz 30 kHz cell (dl_arfcn 632628, 51 PRB, 23.04 MS/s with the
nrUE's `-E` 3/4 sampling, tdd period 10 slots 7D/1S/2U, PRACH index 159), two
OAI nrUEs in their own namespaces (IMSIs from the multi-UE subscriber fixture),
Open5GS, the broker in Sionna mode with `carrier: n78` on every port (one
carrier, so UE<->UE edges are admitted), and the strict verdict applied to the
nrUE log (`State = NR_RRC_CONNECTED`, `Received PDU Session Establishment
Accept`, `SR not served!`, new RA procedures, `Timer T310 expired`, plus a late
5-ping check 30 s before the broker stops). Knobs: `OCUDU_NATIVE_SIONNA_SCENARIO`
(`use_cases/configs/sionna/scenarios/robot_ring/robot-ring-walk-tdd*.json`; the `-los` variants keep both
robots on the line-of-sight side because the nrUE has no AGC and loses sync in
the pillar shadow), `OCUDU_NATIVE_OAI_MUE_UE_CPUS="a;b"` (default `18,4;19,14`
on the GB10), the multi-UE gate's duration / wire-capture / `UE_EXEC` knobs,
`OCUDU_NATIVE_SIONNA_TX_POWER_UL` to override the OAI level.
`wire-capture-tdd-slots.py` cuts a capture into 0.5 ms slots. Design and runs:
`docs/reports/experiments/x3-tdd-multi-ue/README.md`.

## Common blockers

- `unshare: ... Operation not permitted`: the containing host, VM, LXC, or
  command sandbox disables unprivileged user namespaces.
- `/dev/net/tun is absent`: expose the TUN device to the environment running
  the gate.
- `CUDA hardware probe failed`: check the NVIDIA device/driver visibility and
  `OCUDU_NATIVE_GPU_DEVICE`.
- `workspace lock` or revision mismatch: use the pinned native workspace; the
  gate fails closed instead of silently using different RAN/core binaries.

## Shutdown and troubleshooting the Sionna 1×1 run

Press Ctrl-C once in the launch terminal and allow cleanup to finish. The
supervisor retains the run lock, closes that descriptor in child processes,
and signals the inner runner before stopping its namespace supervisor. The
inner runner stops its process groups and removes its temporary namespace
and TUN state. Normal shutdown releases the lock and closes the Web port.

If `another native 1x1 run is active` appears after an interrupted run, inspect
the lock before starting another instance; a remaining process may still own it:

```bash
fuser scripts/native/run-ocudu-legacy-1x1.sh 2>/dev/null || true
flock -n scripts/native/run-ocudu-legacy-1x1.sh -c 'echo native_lock=free'
```

For an inaccessible dashboard, check the printed URL, the configured
`OCUDU_NATIVE_WEB_PORT`, and the SSH forwarding port. An HTTP response alone
is not proof of radio attachment: check `native_sionna_1x1_live_ready` and the
run's RRC, PDU and ping evidence. For a CUDA probe failure, inspect
`nvidia-smi`, `"$CUDACXX" --version`, and the selected GPU index. A missing
TUN device or blocked `unshare` requires a suitable host/container environment.

The report directory contains `live-ready.json`, `web-ui-status.json`,
`attach-summary.json`, and `source-evidence.json`; the log directory contains
`sionna-status.jsonl`. Workspace verification checks pinned dependencies and
binaries, not radio connectivity. See the
[current Sionna guide](../../docs/history/milestones/sionna-integration-20260914.md) for matrix updates,
telemetry freshness, multi-gNB metrics and the separate UE recovery results.

## Local gNB patches: a threaded lower PHY over ZMQ (S17)

`build-ocudu-gnb-local.sh` builds the pinned CPU OCUDU gNB with the patches in
`ocudu-gnb-local-patches.lock.json` into `builds/ocudu-zmq-local`, next to the
pinned build and without touching it (the same policy as the OAI and srsUE
local patches). A gate picks the result with
`OCUDU_NATIVE_GNB_BINARY=$OCUDU_NATIVE_ROOT/builds/ocudu-zmq-local/apps/gnb/gnb`
and passes the knobs through `OCUDU_NATIVE_GNB_WRAPPER="env ..."`. Every knob
unset leaves the stock behaviour, so the patched binary is a drop-in.

OCUDU forces the lower PHY into its blocking/sequential profile whenever the
radio is ZMQ (one worker runs lower-PHY TX, RX and the whole upper PHY), so the
lock-step loop with the UE is strictly serial. The patch
(`integrations/ocudu/patches/ocudu-zmq-lower-phy-profile.patch`) keeps a threaded profile and
fixes what then breaks:

| Variable | Effect |
| --- | --- |
| `OCUDU_ZMQ_LOWER_PHY_PROFILE=single\|dual\|triple` | Keep that lower-PHY thread profile with the ZMQ radio instead of the forced sequential mode. |
| `OCUDU_LPHY_DL_WAIT_MS` | Replace the 2-slot wall-clock escape of `dl_process` (how long DL waits for the UL timestamp); needed because the ZMQ loop is peer-paced, not clock-paced. |
| `OCUDU_LPHY_RX_TO_TX_MAX_MS` | Scale the 1 ms bound by which DL may run ahead of the last received UL timestamp. |
| `OCUDU_ZMQ_RADIO_RT_PRIO=k` | Run the `radio` worker (all ZMQ requests and replies) with SCHED_FIFO priority max-k; the FIFO lower-PHY workers otherwise starve it. |
| `OCUDU_ZMQ_IO_RT_PRIO=k` | The libzmq I/O thread with SCHED_FIFO priority max-k. |
| `OCUDU_ZMQ_RADIO_IDLE_US` | Sleep this long after a poll that made no progress instead of re-queuing immediately (stock busy-loops once the radio worker is real-time). |
| `OCUDU_ZMQ_RX_POP_SLEEP_US` | Poll period of the RX circular buffer (stock 1 us on the real-time `lower_phy_rx` worker). |
| `OCUDU_ZMQ_SINGLE_COPY=1` | Receive straight from the ZMQ message into the circular buffer and pop straight into the outgoing message (stock copies each message twice). |

Recommended set (the one the S17 measurements used):

```bash
OCUDU_NATIVE_GNB_WRAPPER="env OCUDU_ZMQ_LOWER_PHY_PROFILE=dual OCUDU_LPHY_DL_WAIT_MS=5000 \
  OCUDU_LPHY_RX_TO_TX_MAX_MS=2 OCUDU_ZMQ_RADIO_RT_PRIO=1 OCUDU_ZMQ_IO_RT_PRIO=2 \
  OCUDU_ZMQ_RADIO_IDLE_US=5 OCUDU_ZMQ_RX_POP_SLEEP_US=10 OCUDU_ZMQ_SINGLE_COPY=1"
```

Measured on the DGX Spark GB10 (OAI nrUE 2x2, zero-copy broker, gate
defaults), real-time factor stock -> patched: 100 MHz idle 0.65-0.67 ->
0.82-0.85, 100 MHz under load 0.25 -> 0.32, 50 MHz idle 0.99 -> 1.00, 20 MHz
under load 0.89 -> 0.98, 100 MHz 1x1 1.00 -> 1.00. What remains is the
per-message REQ/REP round trip on both ends and the OAI UE's decode time under
load, not the broker. [SPARK_MILESTONES.md](../../docs/history/milestones/SPARK_MILESTONES.md) S17 has the runs, the hop traces
and the run-queue probe that found the starvation.

`integrations/oai/patches/oai-zmq-neon-convert.patch` (NEON cf32<->int16 conversion for the
OAI ZMQ module on aarch64) is recorded as an optional entry in
`integrations/oai/oai-local-patches.lock.json`: it halves the UE's per-message ingest time but
does not move the real-time factor, so the default module does not carry it.

## Demo gates

The two-cell demo gates live with their demos and reuse this directory's
`env.sh`, `oai-gate-defaults.sh`, workspace lock and renderers:

- robot fight (R5): [`use_cases/robot_fight/`](../../use_cases/robot_fight/README.md#native-gate-run-ocudu-robot-fightsh)
- scheduler benchmark: [`use_cases/scheduler_benchmark/`](../../use_cases/scheduler_benchmark/README.md)
