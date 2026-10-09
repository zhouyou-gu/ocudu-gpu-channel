# OCUDU Runtime Interop

Use this guide after the [synthetic tutorial](../getting_started/first-run.md). Select the Docker or native workflow in the sections below, preserve its pinned revisions, and inspect actual UE registration, PDU sessions and traffic. Historical milestone outcomes are evidence for their recorded setup, not a qualification of every current configuration.

For the current Sionna two-gNB/two-UE implementation, one-dashboard subscriptions and separately pinned UE recovery build, see the [integration guide](../history/milestones/sionna-integration-20260914.md). Its qualification limits apply to that moving setup; the smokes below are separate gates.

For the Docker-free, ordinary-user 1×1 gNB–UE attach path, see
[`scripts/native/README.md`](../../scripts/native/README.md). It uses an isolated
user/network/mount namespace and the current checkout's freshly built broker;
it does not include or require the multi-antenna engine.

This runbook validates `ocudu-gpu-channel` against OCUDU Split 8 ZMQ endpoints on the RTX workstation. The target path is:

```text
OCUDU Docker gNB <-> host CUDA broker <-> synthetic UE or srsUE proof-of-concept
```

## Source-Backed Constraints

- OCUDU's SDR-RU path can use UHD/ZMQ and pulls Lower PHY back into the host baseband path: <https://docs.ocuduindia.org/docs/architecture/overview/>.
- OCUDU must be built with ZMQ support enabled, using `-DENABLE_EXPORT=ON -DENABLE_ZEROMQ=ON`: <https://ocudu-docs-604e90.gitlab.io/user_manual/installation/>.
- OCUDU ZMQ TX binds a REP socket, RX connects a REQ socket, and the radio stream carries `std::complex<float>`/`cf32` IQ samples. This is verified against the OCUDU source files under `lib/radio/zmq/`.
- srsUE is only a proof-of-concept UE path for this project, not a basis for scale claims. The srsRAN documentation marks the srsUE setup as proof-of-concept and shows the 23.04 MS/s ZMQ RF settings: <https://docs.srsran.com/projects/project/en/latest/tutorials/source/srsUE/source/>.

## Tracked Assets

- `use_cases/configs/ran/ocudu/docker/gnb_zmq_b210_fdd_srsue.yaml`: OCUDU gNB config derived from OCUDU's B210 FDD srsUE example with the `ru_sdr` section changed to ZMQ for Docker bridge interop.
- `use_cases/configs/topologies/ocudu_docker/topology.ocudu-docker.cuda.yaml`: CUDA broker topology for `gnb0 <-> ue0` using gNB ports `2000/2001` and UE ports `2101/2100`.
- `scripts/remote/ocudu-interop-smoke.sh`: sample-flow proof with OCUDU gNB, Open5GS, CUDA broker, and synthetic UE ZMQ tools.
- `scripts/remote/ocudu-attach-smoke.sh`: attach/ping proof attempt with OCUDU gNB, Open5GS, CUDA broker, and a Docker-built srsUE proof-of-concept.

## Remote Artifact Policy

Runtime outputs stay outside the tracked repo:

```text
~/ocudu-gpu-channel-workspace/
├── builds/ocudu-gpu-channel/cuda-release/
├── configs/ocudu/<timestamp>/
├── results/logs/ocudu-interop/<timestamp>/
└── results/reports/ocudu-interop/<timestamp>/
```

Promote only curated summaries back into `docs/` or `use_cases/`.

## Endpoint Map

The Docker gNB publishes its TX REP socket and connects its RX REQ socket back to the host broker:

```yaml
ru_sdr:
  device_driver: zmq
  device_args: tx_port=tcp://*:2000,rx_port=tcp://host.docker.internal:2001,base_srate=23.04e6
```

The host CUDA broker uses:

```text
gNB TX pull: tcp://127.0.0.1:2000
gNB RX bind: tcp://*:2001
UE TX pull:  tcp://127.0.0.1:2101
UE RX bind:  tcp://127.0.0.1:2100
```

Docker must run with `host.docker.internal:host-gateway` so containers can reach host-bound broker endpoints.

The interop topology uses `batch_samples: 23040` because OCUDU's ZMQ radio exchanges and consumes IQ samples in 23040-sample (1 ms slot) units; serving larger replies overflows the OCUDU gNB RX buffer. `queue_samples: 2457600` gives the broker pull/serve slack and absorbs OCUDU's variable ZMQ TX payload, whose upper bound is the OCUDU `DEFAULT_STREAM_BUFFER_SIZE` of 614400 samples. Synthetic UE tools in the smoke test use the same 23040-sample slot unit and a 1 ms request cadence.

The remote helper generates a temporary OCUDU Dockerfile under `configs/ocudu/<timestamp>/` that installs `libzmq3-dev` in the build stage and `libzmq5` in the runtime stage. It also passes `ZEROMQ_INCLUDE_DIRS=/usr/include` and `ZEROMQ_LIBRARIES=/usr/lib/x86_64-linux-gnu/libzmq.so` into the OCUDU CMake build. This works around the current OCUDU CMake finder looking for pkg-config module `ZeroMQ` while Ubuntu's development package exposes `libzmq`.

## Milestone A: Sample-Flow Proof

Run from the local repo after pushing the branch that contains the interop assets:

```sh
./scripts/remote/ocudu-interop-smoke.sh
```

For pre-push validation of a manually rsynced remote worktree, set `OCUDU_INTEROP_SKIP_REMOTE_PULL=1`.

The script:

1. Clean-pulls the remote project clone to the current pushed branch.
2. Builds the CUDA release tree with the remote user-space CMake/CUDA/ZeroMQ toolchain.
3. Builds OCUDU Docker services with ZMQ explicitly enabled.
4. Starts Open5GS, the OCUDU Docker gNB, synthetic UE TX/RX tools, and the CUDA broker in strict realtime mode.
5. Writes logs under `results/logs/ocudu-interop/<timestamp>/` and a JSON summary under `results/reports/ocudu-interop/<timestamp>/`.

Pass criteria:

- Broker exits with nonzero IQ flow and zero starvation, overflow, sequence-gap, and ZMQ-error counters.
- Synthetic UE source and sink report nonzero sample counts.
- OCUDU logs do not show recurring late, underflow, overflow, fatal, or ZMQ failures.

## Milestone B: Attach/Ping Proof

Run from the local repo after Milestone A is healthy:

```sh
./scripts/remote/ocudu-attach-smoke.sh
```

For pre-push validation of a manually rsynced remote worktree, set `OCUDU_ATTACH_SKIP_REMOTE_PULL=1`.

The script builds a Docker srsUE proof-of-concept image from `srsRAN_4G` and runs it through the same CUDA broker path.

Pass criteria:

- srsUE logs `RRC Connected`.
- srsUE logs `PDU Session Establishment successful`.
- Broker strict counters remain zero.
- A ping through the UE namespace succeeds.

If srsUE build/runtime fails before attach while the broker path is otherwise intact, record the result as a UE-stack blocker, not as a CUDA broker failure.

## Sionna RT + Web UI validation

The bridge now reads node positions, directed links, model IDs, and TX/RX
arrays from a JSON scenario file. It sends one `matrix_profile_swap` for each
physical link. The Broker validates `Nt` and `Nr` against the topology's
startup port lists, requires every `(rx_port, tx_port)` lane exactly once, and
snaps the complete matrix at one slot boundary. Array dimensions cannot change
during a run.

The four tracked scenario/topology pairs are:

| Validation | Broker topology | Sionna scenario |
|---|---|---|
| single cell / UE | `use_cases/configs/topologies/ocudu_docker/topology.ocudu-docker.cuda.yaml` | `use_cases/configs/sionna/scenarios/simple_street/ocudu-docker.json` |
| one cell / multiple UEs | `use_cases/configs/topologies/ocudu_docker/topology.ocudu-docker.multi-ue.cuda.yaml` | `use_cases/configs/sionna/scenarios/simple_street/ocudu-docker-multi-ue.json` |
| interference + crosstalk graph | `use_cases/configs/topologies/basic/topology.graph.cuda.yaml` | `use_cases/configs/sionna/scenarios/simple_street/graph.json` |
| two cells / eight directed links | `use_cases/configs/topologies/ocudu_docker/topology.multi-gnb.cuda.yaml` | `use_cases/configs/sionna/scenarios/simple_street/multi-gnb.json` |

The existing post-TDL chain remains active. For example, the near/far path
loss and AWGN settings still apply after Sionna's instantaneous CIR. Models
that previously had no TDL now contain a neutral leading impulse, so the
static test behaves the same while a live Sionna profile has a prepared device
path to replace.

For a quick synthetic radio-flow check and a live dashboard, choose any pair:

```bash
cd /home/ubuntu/OCUDU/ocudu-gpu-channel
./scripts/local/run_synthetic_web_ui.sh single
./scripts/local/run_synthetic_web_ui.sh multi-ue
./scripts/local/run_synthetic_web_ui.sh graph
./scripts/local/run_synthetic_web_ui.sh multi-gnb
```

Run one command at a time. The launcher prints its temporary log directory and
serves `http://127.0.0.1:8080`. The page shows Sionna positions and per-link
matrix dimensions, lane-(0,0) taps, aggregate matrix power, control ACK
sequence numbers, Broker application/warm-up state, slot latency, integrity
counters, and CPU/GPU usage. Set `OCUDU_SIONNA_DEMO_DURATION_SECONDS=0` only
when intentionally running until interrupted; the normal default is 60 s.

For the full Docker-free single-cell attach, PDU, and ping verdict plus the
same Web UI:

```bash
./scripts/native/run-ocudu-sionna-1x1.sh
```

For the full multi-UE remote gate:

```bash
OCUDU_MUE_CHANNEL_MODE=sionna \
  ./scripts/remote/ocudu-multi-ue-smoke.sh
```

The multi-UE dashboard is `http://127.0.0.1:8080` on the execution host and its
JSONL evidence is `results/logs/ocudu-multi-ue/<timestamp>/sionna-status.jsonl`.
Use an SSH tunnel such as `ssh -L 8080:127.0.0.1:8080 <host>` when the gate runs
remotely.

### Local 2-gNB / 2-UE Sionna gate

On a GPU host containing sibling `ocudu`, `ocudu-gpu-channel`, and
`venvs/sionna` directories, run the complete two-cell test without configuring
an SSH validation mirror:

```bash
cd /home/ubuntu/OCUDU/ocudu-gpu-channel
./scripts/local/ocudu-gnb-ue-sionna-smoke.sh
```

The launcher drives the same validated eight-link multi-gNB topology from
`use_cases/configs/sionna/scenarios/simple_street/multi-gnb.json`, serves `http://127.0.0.1:8080`, and verifies both gNB
cells, both UE RRC connections, both PDU sessions, both data-plane pings, live
Sionna profile updates, and the broker telemetry feed. Override detected paths
when needed with `OCUDU_MGNB_OCUDU_ROOT`, `OCUDU_MGNB_SIONNA_PYTHON`, or
`OCUDU_MGNB_CUDA_COMPILER`; use `OCUDU_MGNB_WEB_PORT` to change the Web UI
port.

### Config-driven arrays and the fixed-MIMO boundary

`array`, `tx_array`, and `rx_array` in a scenario accept `rows`, `cols`,
`pattern`, and `polarization`. Per-link `Nt` is the source node's TX antenna
count and `Nr` is the destination node's RX antenna count. The order is the
Broker's canonical row-major order, `lane = rx_port * Nt + tx_port`.

Changing only the JSON from 1×1 to 1×2 or 1×4 is intentionally rejected: the
matching Broker `radio_nodes.tx_ports`/`rx_ports` and radio transport endpoints
must also be present at startup. Runtime resizing would invalidate prepared
CUDA buffers. A topology model with `fixed_mimo` is also rejected for dynamic
matrix updates because zero coefficients may have pruned lanes at load time;
use a normal leading-TDL model when Sionna owns the full matrix. Static
`fixed_mimo` tests and their atomic physical-link control behavior are
otherwise unchanged.

The host must allow Docker to create bridge-network namespaces. Recent `runc`
security fixes can expose an LXC/LXD AppArmor incompatibility that fails with
`open sysctl net.ipv4.ip_unprivileged_port_start ... permission denied`. This
must be fixed in the outer LXC/LXD host profile and followed by a guest restart;
weakening or downgrading `runc` inside the guest is not part of this test. See
the upstream [`runc` analysis](https://github.com/opencontainers/runc/issues/4968).

## Failure Gates

Treat any of these as a failed real-time interop run:

- nonzero broker starvation, overflow, sequence-gap, or ZMQ-error counters;
- OCUDU recurring late, underflow, overflow, fatal, or ZMQ errors;
- zero sample counts in the sample-flow milestone;
- unbounded queue growth or hidden buffering used to mask timing drift.

Wi-Fi is acceptable only for SSH/control. IQ transport in this phase stays on the RTX host and Docker bridge.
