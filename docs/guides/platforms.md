# Platforms: what runs where, and how each host is set up

The hardware and version table records tested setups, not automatically the newest supported toolchain. Consult [current qualification status](../getting_started/status.md) before interpreting the measured envelopes. Select a platform, its workspace lock and its actual resolved CPU placement together.

The emulator and its live gates have been run on three hosts. They differ in
GPU memory model, CPU topology and container privileges, and each difference
has a knob, a lock file or a patch behind it. This page collects them; the
milestone logs hold the measurements.

| | RTX 5090 workstation | DGX Spark (GB10) | Jetson AGX Orin 64 GB |
|---|---|---|---|
| GPU | RTX 5090, `sm_120`, discrete, 32 GB | GB10, `sm_121`, 48 SMs, integrated | Orin, `sm_87`, 8 SMs, integrated |
| CPU | Core Ultra 9 285K (x86_64) | Cortex-X925 + A725, 20 cores (aarch64) | Cortex-A78AE, 12 cores, 8 online at 30 W (aarch64) |
| Memory | host + VRAM | 128 GB unified | 64 GB unified |
| CUDA / driver | 12.8.93 / 595.71 | 13.0.88 / 580.178 | 12.6.68 / 540.4 (L4T R36.4.4) |
| `cudaDevAttrIntegrated` / `ConcurrentManagedAccess` / `PageableMemoryAccessUsesHostPageTables` | 0 / 1 / 0 | 1 / 1 / 1 | 1 / 0 / 0 |
| OS | Ubuntu 24.04 | DGX OS 7.5 (Ubuntu 24.04) host, Ubuntu 24.04 container | JetPack 6.x host, Ubuntu 22.04 container |
| Role | reference and regression host, C track (CUDA gNB) | S and Z tracks, OAI MIMO gates, X track, S17 | J track, Z6 |
| Milestone logs | `CUDA_MILESTONES.md`, `docs/plans/m*.md` | `SPARK_MILESTONES.md`, `ZERO_COPY_MILESTONES.md`, `CROSSTALK_MILESTONES.md` | `JETSON_MILESTONES.md` |

## What differs per platform

### Broker host memory (`runtime.cuda_host_memory`)

`auto` (the default since Z7) selects `zero_copy` when the device reports
`cudaDevAttrIntegrated` and can map host memory, else `copy`. On the GB10 the
kernels also read the caller's input spans and write the output rows in place
(pageable memory access); on the Orin only the mapped input and output buffers
are zero-copy (`PageableMemoryAccess=0`), and the managed-memory path is never
used because `ConcurrentManagedAccess=0`. On the 5090 `auto` stays on `copy`:
every zero-copy access would cross PCIe. Output is bit-identical in all modes.
Evidence: `ZERO_COPY_MILESTONES.md` Z3 (GB10), Z6 (Orin), Z7 (default).

### CPU placement (`scripts/native/platform-profiles.json`)

The gNB <-> broker <-> UE chain is lock-step, so a thread handoff that lands on
a little core or wakes a core from deep idle pushes the slot round past the slot
period. The native gates resolve a profile with
`scripts/native/platform-profile.py` (`OCUDU_NATIVE_PLATFORM=auto`, the
default; `none` applies nothing; per-process `OCUDU_NATIVE_{GNB,BROKER,NRUE}_CPUS`
win over the profile):

| profile | detect | gNB | broker | UE | extra | evidence |
|---|---|---|---|---|---|---|
| `spark-gb10` | GPU "NVIDIA GB10", fastest CPUs 5-9,15-19 | 5-9 | 15-17 | 18,19 | `OCG_BROKER_SPIN=1`, `OCUDU_NATIVE_GNB_MAIN_POOL_THREADS=5` | S10/S11 (100 MHz OAI 1x1 1,424-1,506 -> 1,995-2,077 slots/s), S15 (spin), X6 (main pool) |
| `jetson-orin-30w` | GPU "Orin (nvgpu)", 8 of 12 cores online | 0-4 | 6-7 | 5 | | J8 (OAI 1x1 20 MHz 0.58-0.60x -> 0.81x; a gNB on 4 cores never starts its radio) |
| (none) | x86_64 workstation | unpinned | unpinned | unpinned | | the gates behave as before the profiles existed |

The GB10 profile allocates all ten big cores; the little cores (0-4, 10-14) are
left out on purpose, and sending the broker there makes the round slower.

### CUDA gNB (vendor OCUDU fork) locks and patches

The CUDA-accelerated OCUDU gNB is a separate source tree selected by
`OCUDU_CUDA_WORKSPACE_LOCK` and audited by `scripts/cuda/resolve-cuda-gnb.py`
(source commit, untracked files, locked patch checksum, built binary). The
vendor pin is `5830c9cb` on every platform; the patch lineage differs:

| platform | lock | patch | content |
|---|---|---|---|
| RTX 5090 | `cuda-workspace.lock.json` (default) | `c1-ocudu-cuda-discrete-and-config.patch` | C1: discrete-GPU grid paths the vendor code assumed a managed grid for (D1-D5) |
| RTX 5090 | `cuda-workspace.c1-d10.lock.json` | `c1-d10-ocudu-cuda-discrete.patch` | C1 + D10 (GPU TB encoder honours a zero LDPC filler count) |
| GB10 | `cuda-workspace.spark.lock.json` | `c1-ocudu-cuda-discrete-and-config.patch` | C1 as on the 5090 |
| GB10 | `cuda-workspace.spark-d8.lock.json` | `s-c1-d8-d9.patch` | + D8 (slope-compensated FD smoothing, 1-PRB SINR) + D9 (LDPC NMS scale 0.8) |
| GB10 | `cuda-workspace.spark-d10.lock.json` | `s-c1-d8-d9-d10.patch` | + D10; the lock the OAI 2x2 gate uses for a CUDA gNB on the Spark |
| Orin | `cuda-workspace.jetson.lock.json` | `c1-ocudu-cuda-discrete-and-config.patch` | C1 |
| Orin | `cuda-workspace.jetson-live.lock.json` | `j2c-ocudu-cuda-orin-d8-d9.patch` | + D6/D7 (Orin: no concurrent managed access, pinned grids) + D8/D9 |
| Orin | `cuda-workspace.jetson-d10.lock.json` | `j2d-ocudu-cuda-orin-d8-d9-d10.patch` | + D10 |

The CUDA architecture follows the lock (120 / 121 / 87); the broker's own
build takes `OCUDU_NATIVE_CUDA_ARCH` (default 120, GB10 121, Orin 87). A CUDA
gNB that shares the GPU with the broker runs under MPS by default
(`OCUDU_NATIVE_MPS=auto`), and `runtime.cuda_stream_priority` can raise the
broker's stream inside that shared context. `docs/cuda-ocudu-integration.html`
explains what the C1 patch changes and why.

### Local patches to the CPU stack (all platforms, build-time, pinned trees untouched)

| lock | pinned tree | output | patches |
|---|---|---|---|
| `integrations/oai/oai-local-patches.lock.json` | OAI `2b69bde6` | `builds/oai-zmq-patched` (ZMQ radio module), `builds/oai-zmq-local` (nr-uesoftmodem) | module: REP reply poll, RX saturation without AVX2, RX gain; UE: 2-layer MMSE int16 wrap, CSI RI accumulator init. Optional (measured, not built): RX poll, NEON conversion (aarch64) |
| `integrations/srsran/srsue-local-patches.lock.json` | srsRAN_4G `eea87b1d` | `builds/srsran4g-zmq-local` | RA contention: fixed preamble 0 and ConRes accepted on mismatch merged simultaneous UEs onto one C-RNTI |
| `integrations/ocudu/ocudu-gnb-local-patches.lock.json` | OCUDU `a1916edc` | `builds/ocudu-zmq-local` | threaded lower PHY over ZMQ, real-time radio worker, no busy polls, single copy (S17; every knob unset = stock) |

`scripts/native/check-oai-local-patches.sh` re-verifies the OAI patches and
artifacts (sha256, dry-run apply, build manifests); `--probe` runs one short
2x2 gate per patch to show the defect without it and the fix with it. The
gates default to the patched OAI module and UE (`OCUDU_NATIVE_OAI_ZMQ_MODULE`,
`OCUDU_NATIVE_OAI_UE`), the patched srsUE in the multi-UE gates, and the
pinned CPU gNB unless `OCUDU_NATIVE_GNB_BINARY` points at the local or a CUDA
build.

The RX-saturation patch matters on aarch64: without AVX2 the stock OAI module
converts every sample in a scalar cleanup loop on the UE's receive thread.

### Measured real-time envelopes (gate defaults)

| cell | 5090 | GB10 | Orin (30 W) |
|---|---|---|---|
| OAI 1x1 20 MHz | real time | real time | 0.81x (J8) |
| OAI 1x1 100 MHz | | real time (2,001 slots/s) | |
| OAI 2x2 20 MHz (FDD band 3, 23.04 MS/s) | real time, DL rank 2 at 2.0x rank 1 (M6) | idle 1.00, under load 0.98 (S17) | attached (J8), no envelope |
| OAI 2x2 50 MHz | | idle 1.00, under load 0.39 | |
| OAI 2x2 100 MHz (TDD n78) | | idle 0.82-0.85, under load 0.32 (S17) | |
| two-cell TDD, 2 gNB + 2 nrUE, 20 MHz | | real time (X5) | |

On the GB10 the remaining bound at 100 MHz is the per-message REQ/REP round
trip on both ends and the OAI UE's decode time under load, not the broker
(`SPARK_MILESTONES.md` S15, S17).

## Setting a host up

Common to all three: the repository, a native workspace provisioned by
`scripts/native/bootstrap-workspace.sh` (`OCUDU_NATIVE_ROOT`; pinned OCUDU,
srsRAN_4G, Open5GS, MongoDB and the user-space dependency overlay,
`native-workspace.lock.json`), the local-patch builds above, and a user that
can create network namespaces and set real-time priorities. The gates need no
host-wide privileges beyond that (`scripts/native/README.md`).

### RTX 5090 workstation

- Native workspace at `~/ocudu-native-workspace` (default
  `/home/ubuntu/ocudu-native-workspace`), provisioned without sudo or Docker.
- The CUDA gNB tree and build live next to it; the default lock is the 5090
  one, so nothing has to be exported.
- `docs/project-structure.md` describes the remote-workspace layout the
  `scripts/remote/*` helpers assume (`.config`, kept out of Git).

### DGX Spark (GB10)

- One container per user, created on the host with
  `scripts/cuda/spark/create-containers.sh` (`scripts/cuda/spark/README.md`):
  Ubuntu 24.04 image with the workstation's Debian overlay (minus the x86-only
  `libfftw3-quad3`), GPU through the NVIDIA CDI spec, the host CUDA 13.0
  toolkit mounted read-only, per-user volumes for `/home/dev` and
  `/workspace`, bridge network. The emulator container additionally needs
  `NET_ADMIN`, `SYS_ADMIN` (for `ip netns add`), `SYS_NICE`, `/dev/net/tun`,
  `ulimit -r 99`, unlimited memlock and AppArmor unconfined.
- Work root `/workspace/ocudu-spark` (`OCUDU_NATIVE_ROOT`); the CUDA gNB locks
  are selected with `OCUDU_CUDA_WORKSPACE_LOCK=scripts/cuda/cuda-workspace.spark*.lock.json`.
- The GB10 is a shared device: the gates take a gate lock, and every milestone
  run records that no other GPU process was running.
- `scripts/cuda/spark/s3-build-stack.sh` builds the aarch64 stack; the OAI
  module needs the RX-saturation patch (above) to keep up.

### Jetson AGX Orin

- Container recreated from the lab's L4T JetPack image with
  `scripts/cuda/jetson/recreate-container.sh` (`Dockerfile` beside it):
  `NET_ADMIN` + `SYS_ADMIN` for network namespaces, `/dev/net/tun`,
  real-time limits, bridge network (never `--network host` on a shared
  Jetson). Dependencies are the jammy arm64 equivalents of the workstation
  overlay; MongoDB is the aarch64 tarball of the same version.
- Power mode is set on the host (`nvpmodel`, `jetson_clocks`) and is not
  visible from the container; `scripts/cuda/jetson/probe-platform.sh` records
  online cores, clocks and the CUDA attributes from sysfs instead, and every
  result is labelled with the mode (J8 ran at MODE_30W: 8 cores online).
- The L4T kernel has no SCTP module, which Open5GS and the gNB need for NGAP:
  `scripts/cuda/jetson/sctp-oot/prepare-sctp-oot.py` builds it out of tree
  from the kernel's own `net/sctp` sources.
- Disk is a single 59 GB eMMC; the whole workspace fits in about 10 GB
  (`JETSON_MILESTONES.md` J0-3).
- Work root `/workspace/ocudu-jetson`; locks
  `scripts/cuda/cuda-workspace.jetson*.lock.json`; platform profile
  `jetson-orin-30w`.

## Where the evidence is

- `CUDA_MILESTONES.md`: C0-C5 on the 5090 (vendor validation, C1 patch,
  staged acceleration, BLER/SINR, measurement).
- `SPARK_MILESTONES.md`: S0-S17 on the GB10 (vendor validation, stack build,
  live gates 20-100 MHz, lock-step round analysis, D8-D10, S17 pipelining).
- `ZERO_COPY_MILESTONES.md`: Z0-Z8, zero-copy on both integrated GPUs.
- `JETSON_MILESTONES.md`: J0-J9 on the Orin (container, vendor validation,
  D6/D7, live OAI 1x1 and 2x2, CPU placement, D10).
- `CROSSTALK_MILESTONES.md` and `docs/plans/x3|x5|x6|x7`: UE-to-UE
  interference and gNB real time under load on the GB10.
- `docs/plans/m6-rank2-su-mimo-live.md`: OAI 2x2 rank 2 on the 5090.
