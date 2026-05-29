# syntax=docker/dockerfile:1
#
# ocudu-gpu-channel — containerised broker runtime.
#
# Multi-stage, parameterised for portability across GPUs and CUDA versions,
# with a CPU-only escape hatch for hosts without an NVIDIA GPU.
#
#   GPU image (default, validated on RTX 5090 / CUDA 12.8):
#     docker build -t ocudu-gpu-channel:latest .
#     docker run --rm --gpus all --network host ocudu-gpu-channel:latest \
#       --config /opt/ocudu/examples/topology.mvp.cuda.yaml --duration 15s
#
#   Single-arch build (faster; pick your GPU's compute capability):
#     docker build --build-arg CUDA_ARCH=90-real -t ocudu-gpu-channel:h100 .
#
#   CPU-only image (no NVIDIA GPU / CI / Mac / AMD):
#     docker build \
#       --build-arg ENABLE_CUDA=OFF \
#       --build-arg DEVEL_BASE=ubuntu:24.04 \
#       --build-arg RUNTIME_BASE=ubuntu:24.04 \
#       -t ocudu-gpu-channel:cpu .
#
# Prerequisite for --gpus all: NVIDIA Container Toolkit on the host.

# --- Build-time configuration ------------------------------------------------
# CUDA toolkit version for the default NVIDIA base images. The host driver
# must support this CUDA runtime version. Arch 120 (Blackwell) needs >= 12.8.
ARG CUDA_VER=12.8.1
ARG DEVEL_BASE=nvidia/cuda:${CUDA_VER}-devel-ubuntu24.04
ARG RUNTIME_BASE=nvidia/cuda:${CUDA_VER}-runtime-ubuntu24.04

# Multi-arch by default: native SASS for Ampere -> Blackwell, plus PTX from
# compute_80 so any unlisted GPU >= 8.0 JIT-compiles at first launch.
# Override to a single arch (e.g. 90-real) for a smaller, faster build.
ARG CUDA_ARCH="80-real;86-real;89-real;90-real;120-real;80-virtual"

# ON builds the CUDA backend; OFF builds the CPU backend only (the CPU backend
# is always present, so an OFF image still runs and passes the CPU tests).
ARG ENABLE_CUDA=ON


# --- Stage 1: build (keeps /src/build so tests can run post-build) -----------
FROM ${DEVEL_BASE} AS build
ARG CUDA_ARCH
ARG ENABLE_CUDA
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        cmake \
        pkg-config \
        libzmq3-dev \
        git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY . /src

# Configure + build everything (apps + the 8 ctest targets). The CUDA backend
# is enabled per ENABLE_CUDA; CUDA_ARCHITECTURES is the multi-arch list above.
RUN cmake -S /src -B /src/build \
        -DCMAKE_BUILD_TYPE=Release \
        -DOCUDU_GPU_CHANNEL_ENABLE_CUDA=${ENABLE_CUDA} \
        -DOCUDU_GPU_CHANNEL_CUDA_ARCHITECTURES="${CUDA_ARCH}" \
    && cmake --build /src/build -j"$(nproc)"

# NOTE: ctest is NOT run here. CUDA-gated tests (hardware_probe,
# runtime_update_parity, broker) need a visible GPU, and `docker build` has
# no GPU access. Run them post-build against this stage:
#   docker build --target build -t ocudu-gpu-channel:build .
#   docker run --rm --gpus all --entrypoint bash ocudu-gpu-channel:build \
#     -c 'cd /src/build && ctest --output-on-failure'


# --- Stage 2: runtime (slim) -------------------------------------------------
FROM ${RUNTIME_BASE} AS runtime
ENV DEBIAN_FRONTEND=noninteractive

LABEL org.opencontainers.image.title="ocudu-gpu-channel" \
      org.opencontainers.image.description="GPU-accelerated real-time wireless channel emulator for live srsRAN and OCUDU stacks (ZMQ, TR 38.901 TDL on CUDA at 5G NR slot cadence)." \
      org.opencontainers.image.source="https://github.com/zhouyou-gu/ocudu-gpu-channel" \
      org.opencontainers.image.licenses="MIT"

RUN apt-get update && apt-get install -y --no-install-recommends \
        libzmq5 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --create-home --home-dir /home/ocudu ocudu

# App binaries + self-contained example topologies (zero-config quickstart).
COPY --from=build /src/build/ocudu-gpu-channel        /usr/local/bin/
COPY --from=build /src/build/ocudu-gpu-channel-bench  /usr/local/bin/
COPY --from=build /src/build/ocudu-zmq-source         /usr/local/bin/
COPY --from=build /src/build/ocudu-zmq-sink           /usr/local/bin/
COPY --from=build /src/examples                        /opt/ocudu/examples

USER ocudu
WORKDIR /opt/ocudu

# Default entrypoint is the broker; override --entrypoint for the other CLIs.
ENTRYPOINT ["ocudu-gpu-channel"]
CMD ["--help"]
