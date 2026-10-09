# Live radio stacks

Use this guide after the [synthetic tutorial](../getting_started/first-run.md). Select the Docker or native workflow in the sections below, preserve its pinned revisions, and inspect actual UE registration, PDU sessions and traffic. Historical milestone outcomes are evidence for their recorded setup, not a qualification of every current configuration.

For shipping channel updates and one-dashboard subscriptions, use the [Sionna](sionna.md) and [dashboard](dashboard.md) guides. The [September integration record](../history/milestones/sionna-integration-20260914.md) preserves the separately pinned UE recovery build and moving-run evidence; the smokes below are separate gates.

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

## Synthetic sample-flow check

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

## Live attach and traffic gate

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

## Sionna and dashboard

Follow the [Sionna guide](sionna.md) for matching scene/topology inputs and supported synthetic or existing-broker launches. Follow the [dashboard guide](dashboard.md) for multiple scheduler subscriptions. The [control](../reference/control-api.md) and [telemetry](../reference/telemetry.md) references define the live interfaces.

For actual radio gates, the [native workflow index](../../scripts/native/README.md) and [remote workflow index](../../scripts/remote/README.md) own the stack-specific commands and environment variables. Use the selected gate's exact pinned binaries and record both UE traffic streams and serving PCIs for multi-gNB runs.

The [October migration report](../reports/validation/structure-migration-20261009.md) records successful targeted live checks using pinned prebuilt images, plus provisioning limitations and two receive starvations. Existing bootstrap/image prerequisites still apply; that report does not qualify a clean-host rebuild or strict real-time operation.

## Failure Gates

Treat any of these as a failed real-time interop run:

- nonzero broker starvation, overflow, sequence-gap, or ZMQ-error counters;
- OCUDU recurring late, underflow, overflow, fatal, or ZMQ errors;
- zero sample counts in the sample-flow milestone;
- unbounded queue growth or hidden buffering used to mask timing drift.

Wi-Fi is acceptable only for SSH/control. IQ transport in this phase stays on the RTX host and Docker bridge.

## Cleanup and diagnostics

Use the selected gate’s cleanup routine and retain its run directory. Do not kill unrelated radio services. For failed attachment, inspect gNB/UE/core logs and endpoint mappings; for timing failures, retain all strict counters alongside the traffic result. See [troubleshooting](troubleshooting.md).
