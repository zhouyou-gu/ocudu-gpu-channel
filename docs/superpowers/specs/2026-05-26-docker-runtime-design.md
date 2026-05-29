# Containerised broker runtime — design

**Date:** 2026-05-26
**Status:** approved, implementing
**Scope:** one multi-stage `Dockerfile` + `.dockerignore` at repo root, plus
documentation (README quickstart + `docs/index.html` §21.2). No change to
build system or runtime source.

## Goal

Package the `ocudu-gpu-channel` broker (and companion CLIs) so a stranger
with an NVIDIA GPU — or none — can build and run it without replicating the
author's RTX 5090 / CUDA 12.8 workstation by hand. Portability to other
people is the explicit design driver.

## Non-goals

- No compose wiring into the open5gs / srsRAN live-radio stack (deferred;
  the existing `scripts/remote/ocudu-*-smoke.sh` already orchestrate that on
  the host).
- No registry publishing / CI image push (future; documented as a pointer).
- No change to `src/`.

## Deviation discovered during validation (CMake)

The original non-goal "no change to CMake" was relaxed by one line. The
multi-arch default (`80-real;…`) exposed a latent bug at
`CMakeLists.txt:64`: the kernel-SM macro took the first
`CUDA_ARCHITECTURES` entry verbatim, so `80-real` produced
`-DOCUDU_GPU_CHANNEL_KERNEL_SM=80-real` and `const int s = 80-real;` failed
to compile (`'real' was not declared`). CMake officially supports
`-real`/`-virtual` decoration on arch list entries, so the extraction was
fragile for any decorated list, not just this Docker image. Fixed at the
source by stripping the decoration:
`string(REGEX MATCH "^[0-9]+" _ocg_first_sm "${_ocg_first_sm}")`. This is the
root-cause fix (a one-liner that benefits every caller), per the
"improve code you're working in" principle, rather than a Docker-only
workaround of avoiding decorated entries.

## Portability decisions

1. **Multi-arch CUDA by default.** `ARG CUDA_ARCH` defaults to
   `"80-real;86-real;89-real;90-real;120-real;80-virtual"` — native SASS for
   Ampere (A100/3090), Ampere-consumer (3090Ti), Ada (4090), Hopper (H100),
   and Blackwell (RTX 5090), plus PTX from compute_80 so any GPU ≥ 8.0 that
   isn't explicitly listed JIT-compiles at first launch. Overridable to a
   single arch (e.g. `--build-arg CUDA_ARCH=90-real`) for faster builds.
   Coupling note: arch `120` requires CUDA ≥ 12.8; lowering `CUDA_VER` below
   12.8 means dropping `120-real` from the list.

2. **Parameterised CUDA version.** `ARG CUDA_VER=12.8.1`. Host driver must
   support the chosen CUDA runtime version; lower it to match an older
   driver.

3. **CPU-only variant from the same file.** `ARG ENABLE_CUDA=ON` plus
   `ARG DEVEL_BASE` / `ARG RUNTIME_BASE`. Building with
   `--build-arg ENABLE_CUDA=OFF --build-arg DEVEL_BASE=ubuntu:24.04
   --build-arg RUNTIME_BASE=ubuntu:24.04` produces a GPU-less image on plain
   Ubuntu for CI, Mac, AMD, or laptop users. The CPU backend is always built
   (the CUDA backend is gated by `OCUDU_GPU_CHANNEL_HAS_CUDA`).

4. **Self-contained image.** `examples/` is copied into the runtime image at
   `/opt/ocudu/examples`, so `docker run <img> --config
   /opt/ocudu/examples/topology.mvp.cuda.yaml` works with zero host files.

5. **Non-root + labelled.** Runs as an unprivileged `ocudu` user; OCI
   `LABEL`s carry source, description, license.

## Image structure

Multi-stage, three named targets:

- **`build`** (`${DEVEL_BASE}`, default `nvidia/cuda:${CUDA_VER}-devel-ubuntu24.04`):
  installs `cmake pkg-config libzmq3-dev git`, configures with
  `-DOCUDU_GPU_CHANNEL_ENABLE_CUDA=${ENABLE_CUDA}
  -DOCUDU_GPU_CHANNEL_CUDA_ARCHITECTURES=${CUDA_ARCH}`, builds everything
  including the 8 test binaries. Retains `/src/build` so tests can run.
- **`runtime`** (`${RUNTIME_BASE}`, default
  `nvidia/cuda:${CUDA_VER}-runtime-ubuntu24.04`): installs only `libzmq5`,
  copies the four app binaries to `/usr/local/bin` and `examples/` to
  `/opt/ocudu/examples`, drops to the `ocudu` user.
  `ENTRYPOINT ["ocudu-gpu-channel"]`, `CMD ["--help"]`.

## Run contract (documented)

```
# GPU, Linux host networking (matches the live gNB's host.docker.internal):
docker run --rm --gpus all --network host \
  ocudu-gpu-channel --config /opt/ocudu/examples/topology.mvp.cuda.yaml --duration 15s

# Port-mapped (Docker Desktop / non-host networking):
docker run --rm --gpus all -p 5559:5559 -p 5560:5560 ... ocudu-gpu-channel ...
```

Prerequisite: NVIDIA Container Toolkit on the host for `--gpus all`.
Real-time note: prefer `--cpuset-cpus` pinning over `--cpus` quota (a CFS
quota can inject scheduling stalls the broker reports as `rx_starvations`).

## Verification (in container, on the RTX 5090)

`docker build` has NO GPU access, so the CUDA-gated tests cannot run in a
build-time `RUN` layer. Tests run post-build against the `build` target:

```
docker build --target build -t ocudu-gpu-channel:build .
docker run --rm --gpus all --entrypoint bash ocudu-gpu-channel:build \
  -c 'cd /src/build && ctest --output-on-failure'      # expect 8/8
docker build -t ocudu-gpu-channel:latest .             # slim runtime image
docker run --rm --gpus all ocudu-gpu-channel:latest \
  --config /opt/ocudu/examples/topology.mvp.cuda.yaml --duration 5s  # smoke
```

Only arch `120` is hardware-validated (the only GPU available); the other
arches are compiled but unverified — documented as such.

## Documentation

- README: short "Run in Docker" quickstart (the repo's front door).
- `docs/index.html`: new **§21.2 Containerised deployment** under the
  existing §21 Operational guidance. Added as a subsection (not a new
  top-level section) to avoid renumbering §22 and the doc's hardcoded
  cross-references.
