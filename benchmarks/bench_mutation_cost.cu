// Microbench: measure M, the extra per-slot cost the CURRENT runtime-mutation
// path adds when channel parameters change.
//
// Reproduces, in isolation, the block at src/cuda_backend.cu:726-768 that runs
// per dirty edge on a control update:
//
//     cudaMemcpyAsync(h_state, d_state, sizeof(DeviceLinkState), D2H, stream)
//     cudaStreamSynchronize(stream)          <-- the drain
//     refresh_tap0_from_live(...)            <-- host recompute
//     cudaMemcpyAsync(d_state, h_state, sizeof(DeviceLinkState), H2D, stream)
//
// Three sweeps:
//   (A) single-edge M, idle stream            -- the original measurement
//   (B) M vs edges-mutated-per-slot E=1..32    -- the RL-agent fan-out case;
//       the per-edge block repeats E times, exactly as the broker loops over
//       dirty edges within one serve
//   (C) single-edge M with a BUSY stream       -- queue GPU work before the
//       mutation block so the drain at line 729 serializes against in-flight
//       work (the post-double-buffering condition, where the stream is not
//       idle between serves)
//
// Build gated by OCUDU_GPU_CHANNEL_HAS_CUDA (see CMakeLists.txt). Not a ctest.

#include "ocudu_gpu_channel/device_channel.h"

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <vector>

#include <cuda_runtime.h>

using ocg::DeviceLinkState;
using ocg::refresh_tap0_from_live;

namespace {

void ck(cudaError_t e, const char* what)
{
  if (e != cudaSuccess) {
    std::fprintf(stderr, "CUDA error at %s: %s\n", what, cudaGetErrorString(e));
    std::exit(1);
  }
}

double pct(std::vector<double> v, double p)
{
  std::sort(v.begin(), v.end());
  const std::size_t idx =
      static_cast<std::size_t>(p / 100.0 * (v.size() - 1) + 0.5);
  return v[idx];
}

double mean(const std::vector<double>& v)
{
  double s = 0.0;
  for (double x : v) s += x;
  return v.empty() ? 0.0 : s / v.size();
}

// Trivial busy-work kernel: keeps the SMs occupied for a tunable spin so the
// stream is non-idle when the mutation block's drain hits it.
__global__ void spin_kernel(volatile int* sink, int spins)
{
  int acc = 0;
  for (int i = 0; i < spins; ++i) acc += i * (threadIdx.x + 1);
  if (threadIdx.x == 0) *sink = acc;
}

// One per-edge mutation step exactly as the broker runs it.
inline void mutate_one_edge(DeviceLinkState* d_state, DeviceLinkState* h_state,
                            cudaStream_t stream)
{
  ck(cudaMemcpyAsync(h_state, d_state, sizeof(DeviceLinkState),
                     cudaMemcpyDeviceToHost, stream), "D2H");
  ck(cudaStreamSynchronize(stream), "D2H drain");
  refresh_tap0_from_live(*h_state);
  ck(cudaMemcpyAsync(d_state, h_state, sizeof(DeviceLinkState),
                     cudaMemcpyHostToDevice, stream), "H2D");
}

} // namespace

int main(int argc, char** argv)
{
  const int iters = (argc > 1) ? std::atoi(argv[1]) : 3000;
  const int warmup = 200;
  const int max_edges = 32;

  std::printf("sizeof(DeviceLinkState) = %zu bytes\n\n", sizeof(DeviceLinkState));

  // Allocate max_edges device states + host mirrors (the broker has one per
  // incoming edge of a destination node).
  std::vector<DeviceLinkState*> d_states(max_edges, nullptr);
  std::vector<DeviceLinkState*> h_states(max_edges, nullptr);
  for (int e = 0; e < max_edges; ++e) {
    ck(cudaMalloc(&d_states[e], sizeof(DeviceLinkState)), "cudaMalloc d");
    ck(cudaMallocHost(&h_states[e], sizeof(DeviceLinkState)), "cudaMallocHost h");
    *h_states[e] = DeviceLinkState{};
    h_states[e]->n_taps = 1;
    h_states[e]->delay_line_size = 8;
    h_states[e]->live.tap0_gain_db = -3.0F;
    ck(cudaMemcpy(d_states[e], h_states[e], sizeof(DeviceLinkState),
                  cudaMemcpyHostToDevice), "seed H2D");
  }

  cudaStream_t stream = nullptr;
  ck(cudaStreamCreateWithFlags(&stream, cudaStreamNonBlocking),
     "cudaStreamCreateWithFlags");

  int* d_sink = nullptr;
  ck(cudaMalloc(&d_sink, sizeof(int)), "cudaMalloc sink");

  // ---- (A) + (B): M vs edges-mutated-per-slot, idle stream ----
  std::printf("(A/B) M vs edges-mutated-per-slot E (idle stream):\n");
  std::printf("  %-6s %-10s %-10s %-10s\n", "E", "p50_us", "p99_us", "mean_us");
  for (int E = 1; E <= max_edges; E <<= 1) {
    std::vector<double> samp;
    samp.reserve(iters);
    for (int it = 0; it < warmup + iters; ++it) {
      auto t0 = std::chrono::steady_clock::now();
      for (int e = 0; e < E; ++e) mutate_one_edge(d_states[e], h_states[e], stream);
      auto t1 = std::chrono::steady_clock::now();
      if (it >= warmup)
        samp.push_back(std::chrono::duration<double, std::micro>(t1 - t0).count());
    }
    std::printf("  %-6d %-10.2f %-10.2f %-10.2f\n", E, pct(samp, 50.0),
                pct(samp, 99.0), mean(samp));
  }

  // ---- (C): single-edge M with a busy stream ----
  // Queue a spin kernel on the same stream just before the mutation block, so
  // the drain at line 729 must wait for it. Sweep the spin length to show how
  // M tracks whatever is in flight (the post-double-buffering regime).
  std::printf("\n(C) single-edge M with busy stream (drain waits on in-flight kernel):\n");
  std::printf("  %-12s %-10s %-10s %-10s\n", "spin", "p50_us", "p99_us", "mean_us");
  for (int spins : {0, 1000, 10000, 100000}) {
    std::vector<double> samp;
    samp.reserve(iters);
    for (int it = 0; it < warmup + iters; ++it) {
      // enqueue busy work first (non-blocking), then time the mutation block
      if (spins > 0) spin_kernel<<<64, 256, 0, stream>>>(d_sink, spins);
      auto t0 = std::chrono::steady_clock::now();
      mutate_one_edge(d_states[0], h_states[0], stream);
      auto t1 = std::chrono::steady_clock::now();
      ck(cudaStreamSynchronize(stream), "tail drain");
      if (it >= warmup)
        samp.push_back(std::chrono::duration<double, std::micro>(t1 - t0).count());
    }
    std::printf("  %-12d %-10.2f %-10.2f %-10.2f\n", spins, pct(samp, 50.0),
                pct(samp, 99.0), mean(samp));
  }

  cudaFree(d_sink);
  cudaStreamDestroy(stream);
  for (int e = 0; e < max_edges; ++e) {
    cudaFreeHost(h_states[e]);
    cudaFree(d_states[e]);
  }
  return 0;
}
