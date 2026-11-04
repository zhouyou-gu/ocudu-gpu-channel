# SUTD shared-carrier crosstalk demo

This synthetic-IQ demonstration shows two four-port gNB nodes, two moving single-port UEs and ten directed channel edges, including both UE-to-UE edges. Sionna traces every edge at 1.9 GHz. All synthetic transmitters operate continuously; there is no TDD slot scheduler, real radio attachment or packet-traffic qualification.

## Clean Spark setup

Start on a freshly initialized DGX Spark host with factory DGX OS (Ubuntu 24.04/aarch64), a working NVIDIA driver, an initial user account with sudo access, and Internet access. No Docker, development tools, Python environment or cloned repository is assumed. Run this demo directly on the host; a container is optional and not needed.

Check the factory platform first:

```bash
uname -m
cat /etc/os-release
nvidia-smi
```

Expect `aarch64`, Ubuntu 24.04 and a working GB10 GPU. If the factory driver is missing or broken, complete NVIDIA's supported DGX OS setup before continuing; installing the CUDA toolkit alone does not repair the driver.

Install the application prerequisites before cloning:

```bash
sudo apt update
sudo apt install -y build-essential cmake pkg-config git libzmq3-dev python3 python3-venv python3-dev curl ca-certificates
```

Check `/usr/local/cuda/bin/nvcc --version`. If the compiler is absent, install the CUDA 13.0 toolkit from NVIDIA's Ubuntu 24.04 ARM64-SBSA repository:

```bash
CUDA_KEYRING_DEB="$(mktemp /tmp/cuda-keyring.XXXXXX.deb)"
curl -fL https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/sbsa/cuda-keyring_1.1-1_all.deb -o "$CUDA_KEYRING_DEB"
sudo dpkg -i "$CUDA_KEYRING_DEB"
rm "$CUDA_KEYRING_DEB"
sudo apt update
sudo apt install -y cuda-toolkit-13-0
```

Use `cuda-toolkit-13-0`, which selects the toolkit, rather than the driver-inclusive `cuda` package. See [NVIDIA's CUDA installation guide](https://docs.nvidia.com/cuda/archive/13.0.0/cuda-installation-guide-linux/index.html). The tested version is CUDA 13.0; a different factory toolkit requires a fresh build and runtime check. Set the compiler path to the installed version if `/usr/local/cuda` is not present.

```bash
export CUDACXX=/usr/local/cuda-13.0/bin/nvcc
"$CUDACXX" --version
export DRJIT_LIBOPTIX_PATH="$(ldconfig -p | awk '/libnvoptix.so.1 / {print $NF; exit}')"
test -n "$DRJIT_LIBOPTIX_PATH" && test -r "$DRJIT_LIBOPTIX_PATH"
```

OptiX is supplied by the NVIDIA driver, not by the Python environment or CUDA compiler. If the OptiX check fails, restore the matching factory driver libraries before continuing. Do not substitute a library from another machine.

Clone the repository, or update an existing clean checkout without discarding local edits. On a fresh machine:

```bash
git clone https://github.com/zhouyou-gu/ocudu-gpu-channel.git
cd ocudu-gpu-channel
```

Record the exact commit used. From the repository root:

```bash
git rev-parse HEAD
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \
  -DOCUDU_GPU_CHANNEL_ENABLE_CUDA=ON \
  -DOCUDU_GPU_CHANNEL_CUDA_ARCHITECTURES=121 \
  -DCMAKE_CUDA_COMPILER="$CUDACXX"
cmake --build build -j8
python3 -m venv "$HOME/venvs/sionna"
"$HOME/venvs/sionna/bin/python" -m pip install --upgrade pip
"$HOME/venvs/sionna/bin/python" -m pip install '.[sionna]'
"$HOME/venvs/sionna/bin/python" -c 'import ctypes, os, sionna.rt, zmq, yaml; ctypes.CDLL(os.environ["DRJIT_LIBOPTIX_PATH"]); print("Sionna", sionna.rt.__version__)'
```

If OptiX lives elsewhere, use its actual path. SUTD geometry is included in the repository.

## Run and verify

```bash
export OCUDU_SIONNA_PYTHON="$HOME/venvs/sionna/bin/python"
export OCUDU_CHANNEL_BUILD="$PWD/build"
export OCUDU_SIONNA_LOG_DIR="$(mktemp -d /tmp/sutd-crosstalk.XXXXXX)"
export OCUDU_SIONNA_WEB_PORT=18080
export OCUDU_SIONNA_CONTROL_PORT=15559
export OCUDU_SIONNA_TELEMETRY_PORT=15560
export OCUDU_SIONNA_DEMO_DURATION_SECONDS=60
bash scripts/local/run_synthetic_web_ui.sh sutd-crosstalk
"$OCUDU_SIONNA_PYTHON" scripts/local/verify_sutd_crosstalk.py "$OCUDU_SIONNA_LOG_DIR"
```

The launcher prints readiness and a URL. On the Spark desktop, open that URL directly. For access from another PC, enable SSH on the Spark if it is not already configured (`sudo apt install -y openssh-server` and `sudo systemctl enable --now ssh`), then run this on your PC:

```bash
ssh -N -L 18080:127.0.0.1:18080 USER@SPARK_HOST
```

Replace `USER` and `SPARK_HOST` with the actual Spark login and network address, then open `http://localhost:18080` on your PC. If deliberately using a container, forward to that container's SSH endpoint instead; a host-only tunnel cannot reach container loopback. Port 8080 is not required.

The dashboard should show the SUTD geometry, both gNBs, two moving UEs and both crosstalk edges. `/api/status` exposes the ten Sionna channel records; check their carrier frequencies and control acknowledgements. HTTP 200 alone is insufficient.

The opening camera fits the radio positions and declared mobility routes; Reset view restores that framing, and Campus fits the full map. The dashboard fetches current status separately from chart history and negotiates compressed responses. This avoids oversized status requests on high-rate telemetry; see [issue #21](../issues/0021-dashboard-poll-timeouts.md) for the measured browser failure and fix.

The generator writes configuration copies beneath the log directory and preserves the original scene, routes and antenna dimensions. Data ports are 3000–3007, 3010–3017 and 3100–3103; the launcher refuses occupied ports without stopping other services. Control, telemetry and dashboard ports are configurable as above. The requested update rate defaults to 10 Hz for this case; change `OCUDU_SIONNA_UPDATE_HZ` if needed and report the measured rate separately.

A finite run cleans up its children. Ctrl-C also cleans up owned children. Duration zero keeps the bridge running until interrupted, with synthetic peers limited to one day; use finite runs for reproducible checks. The verifier requires a completed broker summary and reports starvation counters without claiming strict timing qualification.

Logs can grow quickly because each update includes scene/channel data. Retain the compact verifier output, then remove only the completed run's log directory when no longer needed. Logs, traces and generated files belong outside Git.

## Recorded Spark check

On 4 November 2026, a fresh ARM/CUDA `sm_121` build based on `c775de8` plus the demo changes completed a 45-second synthetic run. It committed 416 ten-edge batches with zero rejected updates, aborted batches, queue overflows, sequence gaps or ZMQ errors; three starvations remained. Dashboard HTTP and status checks confirmed four node positions and ten channels, including both crosstalk edges at 1.9 GHz. This was an API check, not a rendered-browser inspection. Both crosstalk directions had finite traced power.

A final 20-second run at a requested 10 Hz completed 196 updates with both UEs moving. Both crosstalk directions had finite power (approximately −71 to −50 dB); one starvation remained, with zero rejected updates, aborted batches, queue overflows, sequence gaps or ZMQ errors. All demo listening ports were released afterward; existing services were preserved.

The demo was tested in an existing Spark container with its dependencies installed. The fresh-host installation steps above are instructions, not a claim that a factory-reset installation was tested.
