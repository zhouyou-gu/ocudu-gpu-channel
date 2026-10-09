# Installation and build

Build the C++ broker first. Add Python applications only for the workflows you intend to run. All commands below start at the repository root on the validation host.

## CUDA broker

Verify `cmake --version`, `pkg-config --modversion libzmq`, `nvcc --version` and `nvidia-smi` before configuration. When using the managed RTX workspace, source `~/ocudu-gpu-channel-workspace/tools/env.sh` (or `tools/env.sh` under your configured workspace root) to expose the user-space toolchain; see [remote workspace setup](../guides/remote-workspace.md).

```sh
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \
  -DOCUDU_GPU_CHANNEL_ENABLE_CUDA=ON \
  -DOCUDU_GPU_CHANNEL_CUDA_ARCHITECTURES=120
cmake --build build -j4
```

Architecture `120` is the RTX 5090 setting. Use the value for your actual platform from the [platform guide](../guides/platforms.md). CMake must report a CUDA compiler and produce `build/ocudu-gpu-channel`, `build/ocudu-zmq-source` and `build/ocudu-zmq-sink`.

A CPU reference build uses a separate directory:

```sh
cmake -S . -B build-cpu -DCMAKE_BUILD_TYPE=Release \
  -DOCUDU_GPU_CHANNEL_ENABLE_CUDA=OFF
cmake --build build-cpu -j4
```

A CPU-only build rejects a CUDA topology. Select the CPU reference configuration explicitly; do not treat CPU execution as a CUDA result.

## Optional Python applications

Use a virtual environment. These commands install packages from this checkout:

```sh
python3 -m venv .build/apps-venv
. .build/apps-venv/bin/activate
python -m pip install '.[dashboard]'
ocudu-dashboard --help
```

For Sionna, install `'.[sionna]'` into the intended Sionna environment and run `ocudu-sionna-bridge --help`. The installed dashboard includes browser assets. The bridge requires explicit scenario/scene inputs when invoked outside the checkout.

For native-stack preparation, follow [live-stack integration](../guides/live-stacks.md); its pinned checkouts are separate from this repository. Container build and launch instructions are in [running the broker](../guides/broker.md#container).

## Verify and continue

Run CTest in the selected build directory on RTX. The [testing guide](../development/testing.md) describes what those checks cover. Then run the [synthetic tutorial](first-run.md).

If CMake cannot find CUDA or ZeroMQ, fix the toolchain path or development package first. If a binary cannot load a shared library, use the environment generated for that build rather than copying libraries into the repository.
