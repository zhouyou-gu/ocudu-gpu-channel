#include "ocudu_gpu_channel/cuda_backend.h"
#include "ocudu_gpu_channel/delay.h"
#include "ocudu_gpu_channel/device_channel.h"
#include "ocudu_gpu_channel/mutable_params.h"
#include "ocudu_gpu_channel/processing.h"
#include "ocudu_gpu_channel/runtime_control.h"
#include <algorithm>
#include <chrono>
#include <cmath>
#include <cuda_runtime.h>
#include <curand_kernel.h>
#include <functional>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <vector>

namespace ocg {
namespace {

constexpr double kPi = 3.14159265358979323846;

// One GPU-executable channel step. Field meaning depends on `type`:
//   Scale:    a = amplitude factor.
//   Rotate:   a = start phase (rad), b = per-sample phase increment (rad).
//   AddNoise: a = noise std-dev sigma; seed + counter drive the per-sample RNG.
struct GpuStep {
  int type = 0;
  float a = 0.0F;
  float b = 0.0F;
  unsigned int seed = 0;
  unsigned long long counter = 0;
};

enum GpuStepType {
  Scale = 0,
  Rotate = 1,
  AddNoise = 2
};

GpuStep make_step(int type, float a, float b)
{
  GpuStep step;
  step.type = type;
  step.a = a;
  step.b = b;
  return step;
}

// The model-chain running state for one link or one receiver model: the built
// GPU steps plus the per-step CFO phase and AWGN counters that persist across
// batches. build_steps() operates on this; it owns no device memory.
//
// The chain-leading propagation step (tdl, multi-tap convolution) runs HOST-
// SIDE during staging, not in the per-sample CUDA kernel, because the kernel
// reads only the current sample. `delay_line` carries the previous batch's
// tail across calls; `tdl_polyphase` caches the per-tap windowed-sinc
// coefficient sets. Tap data itself is NOT cached here -- the staging path
// reads `step.taps` live from the owning ModelConfig (single source of truth,
// no duplication between CPU and CUDA backends).
struct LinkModelState {
  std::size_t step_capacity = 0;
  std::vector<GpuStep> host_steps;
  std::vector<double> phase_rad;
  std::vector<unsigned int> noise_seed;          // per-step AWGN RNG seed
  std::vector<unsigned long long> noise_counter; // per-step AWGN sample counter
  // Chain-leading propagation (tdl). Populated when the link's chain leads
  // with a tdl step; otherwise has_tdl is false and stage_link just copies.
  bool has_tdl = false;
  const std::vector<TapSpec>* tdl_taps = nullptr;  // borrowed view into ModelConfig::chain[0].taps
  std::vector<std::array<float, kTdlFracFilterTaps>> tdl_polyphase;
  std::vector<IqSample> delay_line;
  // Per-link fading sub-config state (Phase 1.4). Populated by
  // configure_leading_propagation when the chain-leading tdl step has
  // fading_enabled; stage_link dispatches to apply_tdl_step_fading in that
  // case. The seed is derived from the link key + step index so the CPU and
  // CUDA backends draw the same Jakes sub-ray angles.
  TdlFadingState tdl_fading;
  // Runtime-mutable scalar params (Phase 3 v1). `live` is the canonical
  // source read by build_steps (post-C2a). `ctl.shadow` is the write target
  // for the ZMQ control thread (Phase 3 C3+); the snap step at the top of
  // every serve copies shadow → live if ctl.seqno advanced since the last
  // snap. `live_seqno` tracks the version this LinkModelState last consumed.
  MutableParams live;
  BrokerLinkControl ctl;
  std::uint32_t live_seqno = 0;

  // v2.0-F3b: live profile-swap state, mirror of CPU LinkState. When
  // live_profile_active is true, the next slot's snap path calls
  // refresh_all_taps_from_live() on the matching DeviceLinkState before
  // the H2D round-trip, so the channel kernel reads the new tap layout.
  ProfileShadow live_profile;
  bool          live_profile_active = false;
  bool          chain_has_leading_tdl = false;
};

void init_model_state(LinkModelState& state, std::size_t steps, const std::string& seed_prefix)
{
  state.step_capacity = steps;
  state.host_steps.assign(steps, GpuStep{});
  state.phase_rad.assign(steps, 0.0);
  state.noise_counter.assign(steps, 0ULL);
  state.noise_seed.assign(steps, 0U);
  for (std::size_t s = 0; s != steps; ++s) {
    // Stable per-step seed: an independent, run-to-run reproducible AWGN stream.
    state.noise_seed[s] =
        static_cast<unsigned int>(std::hash<std::string>{}(seed_prefix + ":awgn:" + std::to_string(s)));
  }
}

// Applies one link's channel-model chain to sample `idx`'s (i,q) in registers.
// Shared by the single-link kernel and the superposition kernel.
__device__ inline void apply_chain(float& i, float& q, const GpuStep* steps, int step_count, std::size_t idx)
{
  for (int step_idx = 0; step_idx != step_count; ++step_idx) {
    const GpuStep step = steps[step_idx];
    if (step.type == Scale) {
      i *= step.a;
      q *= step.a;
    } else if (step.type == AddNoise) {
      // Counter-based Philox RNG: stateless and bit-reproducible regardless of
      // thread scheduling. Each sample gets an independent substream from
      // (seed, counter + idx); `counter` advances by the batch size every call.
      curandStatePhilox4_32_10_t rng;
      curand_init(static_cast<unsigned long long>(step.seed), step.counter + idx, 0ULL, &rng);
      const float2 noise = curand_normal2(&rng);
      i += step.a * noise.x;
      q += step.a * noise.y;
    } else {
      const float phase = step.a + step.b * static_cast<float>(idx);
      float sin_value = 0.0F;
      float cos_value = 0.0F;
      sincosf(phase, &sin_value, &cos_value);
      const float rotated_i = i * cos_value - q * sin_value;
      const float rotated_q = i * sin_value + q * cos_value;
      i = rotated_i;
      q = rotated_q;
    }
  }
}

// Single-link kernel: one GPU thread shapes one output sample. Safe in place
// (input == output): each thread touches only its own index.
__global__ void apply_steps_kernel(const IqSample* input,
                                   IqSample* output,
                                   std::size_t count,
                                   const GpuStep* steps,
                                   int step_count)
{
  const std::size_t idx = blockIdx.x * blockDim.x + threadIdx.x;
  if (idx >= count) {
    return;
  }
  float i = input[idx].i;
  float q = input[idx].q;
  apply_chain(i, q, steps, step_count, idx);
  output[idx] = {i, q};
}

// Superposition kernel -- the multi-link interference compute.
// One GPU thread owns one output sample and, for that sample, walks every
// incoming edge: it shapes the edge's input through that edge's own model
// chain and accumulates the result. dst[idx] is the summed received signal
// (desired + interference + crosstalk) for sample idx, before the receiver
// model. `staged` holds the edges' input batches back to back: edge k's
// sample idx is staged[k*count+idx]. `step_meta` packs the per-edge step
// offsets in [0,link_count) and step counts in [link_count,2*link_count).
__global__ void superpose_kernel(IqSample* dst,
                                 std::size_t count,
                                 int link_count,
                                 const IqSample* staged,
                                 const GpuStep* steps,
                                 const int* step_meta)
{
  const std::size_t idx = blockIdx.x * blockDim.x + threadIdx.x;
  if (idx >= count) {
    return;
  }
  float acc_i = 0.0F;
  float acc_q = 0.0F;
  for (int k = 0; k != link_count; ++k) {
    const IqSample sample = staged[static_cast<std::size_t>(k) * count + idx];
    float i = sample.i;
    float q = sample.q;
    apply_chain(i, q, steps + step_meta[k], step_meta[link_count + k], idx);
    acc_i += i;
    acc_q += q;
  }
  dst[idx] = {acc_i, acc_q};
}

void check(cudaError_t status, const char* operation)
{
  if (status != cudaSuccess) {
    throw std::runtime_error(std::string(operation) + " failed: " + cudaGetErrorString(status));
  }
}

double param_or(const ModelStep& step, const std::string& name, double fallback)
{
  auto it = step.params.find(name);
  return it == step.params.end() ? fallback : it->second;
}

// Records the chain-leading propagation step (integer/fractional delay OR tdl
// multi-tap convolution) on the model state. validate_cuda_support() has
// already guaranteed any such step is the chain's first step, so only
// chain.front() can be one. At most one of has_delay / has_tdl ends true.
void configure_leading_propagation(LinkModelState& state, const ModelConfig& model,
                                    const std::string& link_key_value)
{
  state.has_tdl = false;
  state.tdl_taps = nullptr;
  state.tdl_polyphase.clear();
  state.delay_line.clear();
  state.tdl_fading = TdlFadingState{};  // reset to disabled
  if (model.chain.empty()) {
    return;
  }
  const auto& first = model.chain.front();
  if (first.type == ModelStepType::Tdl) {
    state.has_tdl = true;
    state.tdl_taps = &first.taps;  // borrow the live ModelConfig tap list -- not copied
    prepare_tdl_state(first.taps, state.tdl_polyphase, state.delay_line);
    // Step index of the leading tdl is by construction 0; bake it into the
    // fading seed the same way the CPU backend does so the two backends draw
    // the same Jakes sub-ray angles.
    const std::uint64_t fading_seed = static_cast<std::uint64_t>(
        std::hash<std::string>{}(link_key_value + ":fading:0"));
    prepare_tdl_fading_state(first, fading_seed, state.tdl_fading);
  }
}

// Stages one link's `count` input samples into `out`, applying the chain-
// leading propagation step (tdl multi-tap convolution) host-side, or a plain
// copy when the link's chain has no leading propagation. The device kernel
// then runs the rest of the chain per-sample.
void stage_link(LinkModelState& state, const IqSample* in, IqSample* out,
                std::size_t count, std::uint64_t sample_rate_hz)
{
  if (state.has_tdl) {
    // tdl_taps borrowed from ModelConfig; the staging path is bit-identical
    // to the CPU backend because both call the same apply_tdl_step (no
    // fading) or apply_tdl_step_fading (when the leading tdl has a fading
    // sub-config) from delay.h.
    if (state.tdl_fading.enabled) {
      apply_tdl_step_fading(in, out, count, *state.tdl_taps,
                             state.tdl_polyphase, state.delay_line,
                             state.tdl_fading, sample_rate_hz);
    } else {
      apply_tdl_step(in, out, count, *state.tdl_taps, state.tdl_polyphase,
                     state.delay_line);
    }
  } else {
    std::copy(in, in + count, out);
  }
}

// Per-link model state (chain phase / AWGN counter / delay_line ring) held
// alongside the per-destination superposition state. Every incoming edge of
// a destination contributes one of these to the staged buffer.
struct CudaLinkSlot {
  LinkModelState model;
};

// Per-destination-node state for the fused superposition kernel: one pinned
// staging buffer holding every incoming edge's input batch back to back, one
// device accumulator, the flattened model steps, the receiver-model steps,
// and one stream.
struct CudaSuperposeState {
  std::size_t capacity = 0;  // samples per batch
  std::size_t max_links = 0; // incoming edges
  std::size_t max_steps = 0; // longest edge model chain
  IqSample* host_staged = nullptr;
  IqSample* host_output = nullptr;
  IqSample* device_staged = nullptr;
  IqSample* device_output = nullptr;
  GpuStep* device_steps = nullptr;
  int* device_step_meta = nullptr;    // 2*max_links: offsets then counts
  GpuStep* device_rx_steps = nullptr; // receiver-model chain (may be unused)
  // Per-(dst_node x incoming edge) device-side link state consumed by
  // apply_channel_kernel. Populated in prepare(); used at serve time
  // whenever sp.use_device_channel is true (every incoming edge has a
  // leading tdl). Mixed nodes fall back to host stage_link.
  DeviceLinkState* device_link_states = nullptr;
  std::vector<DeviceLinkState> host_link_states;
  // D4: paired host (pinned) + device buffers carrying the *raw* (pre-channel)
  // IQ to the GPU when use_device_channel == true. Sized by UNIQUE SOURCE
  // count (num_sources), not by incoming edge count: multiple edges sharing
  // a source read the same slot via DeviceLinkState::src_index. Saves
  // (n_edges - n_sources) * count IQ samples of H2D bandwidth per slot.
  std::size_t num_sources = 0;
  // Per-edge mapping built at prepare time: for each unique source slot s in
  // [0, num_sources), source_first_edge[s] is the index k of the FIRST edge
  // (in the broker's incoming order) that consumes that source. Used at
  // serve time to pack the source's IQ exactly once.
  std::vector<int> source_first_edge;
  IqSample* host_source_iq = nullptr;
  IqSample* device_source_iq = nullptr;
  // Dispatch gate: true when every incoming edge of this destination has a
  // leading tdl step (with or without fading -- the device fading kernel
  // landed in D3 and handles both branches internally). True path:
  //   raw IQ -> host_source_iq -> H2D -> apply_channel_kernel ->
  //   device_staged -> superpose_kernel
  // False path: host-side stage_link writes device_staged via host_staged.
  // Test observability: surfaced through ProcessorTimings.used_device_channel.
  bool use_device_channel = false;
  cudaStream_t stream = nullptr;
  cudaEvent_t h2d_start = nullptr;
  cudaEvent_t h2d_done = nullptr;
  cudaEvent_t kernel_done = nullptr;
  cudaEvent_t d2h_done = nullptr;
  LinkModelState rx_model; // receiver-model state; step_capacity 0 = no rx model
  std::vector<GpuStep> host_steps;
  std::vector<int> host_step_meta;
};

void free_superpose_state(CudaSuperposeState& state)
{
  if (state.d2h_done != nullptr) {
    cudaEventDestroy(state.d2h_done);
  }
  if (state.kernel_done != nullptr) {
    cudaEventDestroy(state.kernel_done);
  }
  if (state.h2d_done != nullptr) {
    cudaEventDestroy(state.h2d_done);
  }
  if (state.h2d_start != nullptr) {
    cudaEventDestroy(state.h2d_start);
  }
  if (state.stream != nullptr) {
    cudaStreamDestroy(state.stream);
  }
  if (state.device_source_iq != nullptr) {
    cudaFree(state.device_source_iq);
  }
  if (state.host_source_iq != nullptr) {
    cudaFreeHost(state.host_source_iq);
  }
  if (state.device_link_states != nullptr) {
    cudaFree(state.device_link_states);
  }
  if (state.device_rx_steps != nullptr) {
    cudaFree(state.device_rx_steps);
  }
  if (state.device_step_meta != nullptr) {
    cudaFree(state.device_step_meta);
  }
  if (state.device_steps != nullptr) {
    cudaFree(state.device_steps);
  }
  if (state.device_output != nullptr) {
    cudaFree(state.device_output);
  }
  if (state.device_staged != nullptr) {
    cudaFree(state.device_staged);
  }
  if (state.host_output != nullptr) {
    cudaFreeHost(state.host_output);
  }
  if (state.host_staged != nullptr) {
    cudaFreeHost(state.host_staged);
  }
  state = {};
}

class CudaChannelProcessor final : public ChannelProcessor {
public:
  explicit CudaChannelProcessor(int device) : device_(device)
  {
    check(cudaSetDevice(device_), "cudaSetDevice");
  }

  ~CudaChannelProcessor() override
  {
    for (auto& [_, sp] : superpose_states_) {
      free_superpose_state(sp);
    }
  }

  void prepare(const TopologyConfig& config) override
  {
    check(cudaSetDevice(config.runtime.gpu_device), "cudaSetDevice");
    device_ = config.runtime.gpu_device;

    // Per-link model state (chain phase / AWGN counter / delay_line). One
    // slot per link, looked up by link_key when packing the staged buffer.
    for (const auto& link : config.links) {
      const auto* destination = find_device(config, link.to);
      const auto* model = find_model(config, link.model);
      if (destination == nullptr || model == nullptr) {
        continue;
      }
      auto& slot = link_slots_[link_key(link)];
      init_model_state(slot.model, model->chain.size(), link_key(link));
      configure_leading_propagation(slot.model, *model, link_key(link));
      // Phase 3 v1: populate runtime-mutable params from YAML. build_steps
      // (post-C2a) reads path_loss_db + cfo_hz from `live`. The control
      // plane (C3+) writes to `ctl.shadow` and bumps `ctl.seqno`;
      // snap_mutable_params() in process_superposition picks up shadow → live
      // transitions per slot. Initialise shadow == live so the first serve's
      // snap is a no-op.
      slot.model.live = populate_mutable_params_from_yaml(
          *model, /*reference_power=*/0.0, destination->sample_rate_hz);
      init_broker_link_control(slot.model.ctl, slot.model.live);
      // v2.0-F3b: cache the eligibility flag for the snap path's
      // profile_swap check.
      slot.model.chain_has_leading_tdl =
          !model->chain.empty() && model->chain.front().type == ModelStepType::Tdl;
    }

    // Per-destination superposition state: one entry per node that is the
    // target of at least one link.
    for (const auto& device : config.devices) {
      std::size_t incoming = 0;
      std::size_t max_steps = 0;
      for (const auto& link : config.links) {
        if (link.to != device.id) {
          continue;
        }
        const auto* model = find_model(config, link.model);
        if (model == nullptr) {
          continue;
        }
        ++incoming;
        max_steps = std::max(max_steps, model->chain.size());
      }
      if (incoming == 0) {
        continue;
      }
      const std::size_t capacity = resolve_batch_samples(config.runtime, device.sample_rate_hz);
      // Exception safety: if any of the ~12 sequential cudaHostAlloc /
      // cudaMalloc / cudaStreamCreate / cudaEventCreate / cudaMemcpy calls
      // below throws (via check()), the partially-allocated sp leaks until
      // the processor destructs. Catch + free + erase here so a thrown
      // prepare() leaves either a fully-built entry or no entry at all --
      // re-prepare from scratch stays well-defined, and a leaked processor
      // (caller forgets to destruct on failure) doesn't leak GPU memory.
      try {
      auto& sp = superpose_states_[device.id];
      free_superpose_state(sp);
      sp.capacity = capacity;
      sp.max_links = incoming;
      sp.max_steps = std::max<std::size_t>(1, max_steps);
      sp.host_steps.assign(incoming * sp.max_steps, GpuStep{});
      sp.host_step_meta.assign(2 * incoming, 0);

      const std::size_t staged_bytes = incoming * capacity * sizeof(IqSample);
      const std::size_t out_bytes = capacity * sizeof(IqSample);
      check(cudaHostAlloc(reinterpret_cast<void**>(&sp.host_staged), staged_bytes, cudaHostAllocDefault),
            "cudaHostAlloc superpose staged");
      check(cudaHostAlloc(reinterpret_cast<void**>(&sp.host_output), out_bytes, cudaHostAllocDefault),
            "cudaHostAlloc superpose output");
      check(cudaMalloc(reinterpret_cast<void**>(&sp.device_staged), staged_bytes), "cudaMalloc superpose staged");
      check(cudaMalloc(reinterpret_cast<void**>(&sp.device_output), out_bytes), "cudaMalloc superpose output");
      check(cudaMalloc(reinterpret_cast<void**>(&sp.device_steps), incoming * sp.max_steps * sizeof(GpuStep)),
            "cudaMalloc superpose steps");
      check(cudaMalloc(reinterpret_cast<void**>(&sp.device_step_meta), 2 * incoming * sizeof(int)),
            "cudaMalloc superpose step meta");
      check(cudaStreamCreateWithFlags(&sp.stream, cudaStreamNonBlocking), "cudaStreamCreateWithFlags superpose");
      check(cudaEventCreate(&sp.h2d_start), "cudaEventCreate superpose h2d_start");
      check(cudaEventCreate(&sp.h2d_done), "cudaEventCreate superpose h2d_done");
      check(cudaEventCreate(&sp.kernel_done), "cudaEventCreate superpose kernel_done");
      check(cudaEventCreate(&sp.d2h_done), "cudaEventCreate superpose d2h_done");

      // Optional receiver model (a thermal-noise floor) applied after the sum.
      const auto* rx = device.rx_model.empty() ? nullptr : find_model(config, device.rx_model);
      if (rx != nullptr) {
        init_model_state(sp.rx_model, rx->chain.size(), device.id + ">rx");
        check(cudaMalloc(reinterpret_cast<void**>(&sp.device_rx_steps),
                         std::max<std::size_t>(1, rx->chain.size()) * sizeof(GpuStep)),
              "cudaMalloc superpose rx steps");
      }

      // Build one DeviceLinkState per incoming edge of this destination,
      // in the same YAML-order the broker uses when it builds its
      // `incoming` filter (src/broker.cpp). Consumed by the per-serve
      // apply_channel_kernel + update_delay_line_kernel dispatch below.
      sp.host_link_states.assign(incoming, DeviceLinkState{});
      check(cudaMalloc(reinterpret_cast<void**>(&sp.device_link_states),
                       incoming * sizeof(DeviceLinkState)),
            "cudaMalloc superpose device_link_states");

      // D4: build the per-source dedup map. Walk incoming edges in broker
      // order, assigning each unique source a contiguous src_index. Edges
      // sharing a source share an index — kernel reads source_iq once per
      // source instead of once per edge.
      std::unordered_map<std::string, int> src_to_index;
      sp.source_first_edge.clear();
      std::vector<int> per_edge_src_index;
      per_edge_src_index.reserve(incoming);
      for (const auto& link : config.links) {
        if (link.to != device.id) continue;
        const auto* model = find_model(config, link.model);
        if (model == nullptr) continue;
        auto ls_it = link_slots_.find(link_key(link));
        if (ls_it == link_slots_.end()) continue;
        auto [it, inserted] = src_to_index.try_emplace(
            link.from, static_cast<int>(sp.source_first_edge.size()));
        if (inserted) {
          sp.source_first_edge.push_back(static_cast<int>(per_edge_src_index.size()));
        }
        per_edge_src_index.push_back(it->second);
      }
      sp.num_sources = sp.source_first_edge.size();
      const std::size_t source_bytes = sp.num_sources * capacity * sizeof(IqSample);

      // Paired pinned-host + device buffers for the raw-IQ-into-channel-kernel
      // path. Sized by UNIQUE source count (D4), not by edge count.
      check(cudaHostAlloc(reinterpret_cast<void**>(&sp.host_source_iq),
                          source_bytes, cudaHostAllocDefault),
            "cudaHostAlloc superpose source_iq");
      check(cudaMalloc(reinterpret_cast<void**>(&sp.device_source_iq),
                       source_bytes),
            "cudaMalloc superpose source_iq");
      std::size_t k_idx = 0;
      // Dispatch gate: every incoming edge must have a leading tdl step
      // (fading-enabled tdl included -- the device kernel handles both
      // static and Jakes/Rician branches internally). Mixed nodes (any
      // non-tdl-leading edge) fall back to host stage_link for the whole
      // destination.
      bool all_leading_tdl = (incoming > 0);
      for (const auto& link : config.links) {
        if (link.to != device.id) {
          continue;
        }
        const auto* model = find_model(config, link.model);
        if (model == nullptr) {
          continue;
        }
        auto ls_it = link_slots_.find(link_key(link));
        if (ls_it == link_slots_.end()) {
          continue;
        }
        const auto& lms = ls_it->second.model;
        const bool has_leading_tdl =
            !model->chain.empty() && model->chain.front().type == ModelStepType::Tdl;
        if (!has_leading_tdl) {
          all_leading_tdl = false;
        }
        const int src_idx = per_edge_src_index[k_idx];
        if (has_leading_tdl) {
          // Only links with a leading tdl step contribute a non-trivial
          // DeviceLinkState. The build helper handles the non-tdl case by
          // zeroing the struct + clearing has_tdl (but still stores src_index
          // so the pass-through branch reads the right source slot).
          (void)build_device_link_state(model->chain.front(),
                                         lms.tdl_polyphase,
                                         lms.tdl_fading,
                                         static_cast<int>(lms.delay_line.size()),
                                         src_idx,
                                         sp.host_link_states[k_idx]);
        } else {
          // Non-tdl-leading: still set src_index for the pass-through path.
          sp.host_link_states[k_idx].src_index = src_idx;
          sp.host_link_states[k_idx].has_tdl = 0;
        }
        // Phase 3 v1: mirror the host-side initial mutable state into this
        // edge's DeviceLinkState so the device path's `live` matches the
        // host fallback's `live` on slot 0.
        sp.host_link_states[k_idx].live = lms.live;
        ++k_idx;
      }
      sp.use_device_channel = all_leading_tdl;
      check(cudaMemcpy(sp.device_link_states, sp.host_link_states.data(),
                       incoming * sizeof(DeviceLinkState), cudaMemcpyHostToDevice),
            "cudaMemcpy device_link_states H2D");
      } catch (...) {
        auto it = superpose_states_.find(device.id);
        if (it != superpose_states_.end()) {
          free_superpose_state(it->second);
          superpose_states_.erase(it);
        }
        throw;
      }
    }
  }

  void process_superposition(const std::string& dst_key,
                             const std::vector<SuperpositionInput>& inputs,
                             const ModelConfig* rx_model,
                             std::uint64_t sample_rate_hz,
                             std::span<IqSample> output) override
  {
    if (output.empty()) {
      return;
    }
    auto sp_it = superpose_states_.find(dst_key);
    if (sp_it == superpose_states_.end()) {
      throw std::runtime_error("CUDA superposition state was not preallocated: " + dst_key);
    }
    auto& sp = sp_it->second;
    const std::size_t count = output.size();
    if (count > sp.capacity) {
      throw std::runtime_error("CUDA superposition batch exceeds preallocated capacity");
    }
    if (inputs.empty()) {
      std::fill(output.begin(), output.end(), IqSample{});
      return;
    }
    if (inputs.size() > sp.max_links) {
      throw std::runtime_error("CUDA superposition edge count exceeds preallocated capacity");
    }
    if (rx_model != nullptr && sp.device_rx_steps == nullptr) {
      throw std::runtime_error("CUDA superposition rx_model state was not preallocated: " + dst_key);
    }

    // Stage every incoming edge's input batch contiguously and build its model
    // chain. Per-edge CFO/AWGN state lives in the edge's own CudaLinkSlot.
    //
    // Two dispatch modes (set in prepare()):
    //   sp.use_device_channel == false  →  host-side stage_link path (legacy):
    //       per-edge apply_tdl_step / _fading on host, shaped IQ goes into
    //       host_staged[k·count] and ships H2D into device_staged.
    //   sp.use_device_channel == true   →  device-kernel path (Phase 2 D2b
    //       + D4): raw source-node IQ goes into host_source_iq[src_idx·count]
    //       once per UNIQUE source (not per edge — multiple edges sharing a
    //       source share a slot). H2D copies num_sources × count IQ samples
    //       (smaller than the previous link_count × count); the device kernel
    //       reads via DeviceLinkState::src_index. Build_steps still walks
    //       per-edge and reads the raw input for its AWGN snr_db power
    //       estimate (a small semantic shift from the host path which read
    //       the shaped output; explicit noise_power AWGN is unaffected).
    const int link_count = static_cast<int>(inputs.size());
    int total_steps = 0;
    // D4 pack-per-source: walk unique sources once. source_first_edge[s]
    // identifies the first edge that consumes source s (alphabetical/YAML
    // order); copying that edge's samples into host_source_iq[s·count]
    // suffices for every edge sharing source s, because the broker reads
    // all such edges from the same tx_ring at the same cursor — they
    // carry identical bytes.
    if (sp.use_device_channel) {
      for (std::size_t s = 0; s < sp.num_sources; ++s) {
        const int k = sp.source_first_edge[s];
        if (k < 0 || static_cast<std::size_t>(k) >= inputs.size()) {
          throw std::runtime_error("CUDA superposition: source_first_edge out of range");
        }
        const auto& edge = inputs[static_cast<std::size_t>(k)];
        if (edge.samples.size() != count) {
          throw std::runtime_error("CUDA superposition input is malformed");
        }
        IqSample* src_slot = sp.host_source_iq + s * count;
        std::copy(edge.samples.data(), edge.samples.data() + count, src_slot);
      }
    }
    for (std::size_t k = 0; k != inputs.size(); ++k) {
      const auto& edge = inputs[k];
      if (edge.model == nullptr || edge.samples.size() != count) {
        throw std::runtime_error("CUDA superposition input is malformed");
      }
      if (edge.model->chain.size() > sp.max_steps) {
        throw std::runtime_error("CUDA superposition model chain exceeds preallocated capacity");
      }
      auto ls_it = link_slots_.find(edge.link_key);
      if (ls_it == link_slots_.end()) {
        throw std::runtime_error("CUDA link state was not preallocated: " + edge.link_key);
      }
      // Phase 3 C2b: snap any pending shadow update from the control plane
      // into `live` before build_steps reads it. No-op when seqno hasn't
      // advanced (single acquire-load + early-return).
      auto& lms_for_snap = ls_it->second.model;
      const bool live_changed = snap_mutable_params(
          lms_for_snap.live, lms_for_snap.live_seqno, lms_for_snap.ctl);

      // v2.0-F3b: profile-swap snap with eligibility check. Same logic
      // as the CPU branch in apply_chain_to_link — if profile_pending
      // is set on this seqno bump AND the chain leads with a tdl (or
      // force=true), copy the profile shadow into live_profile and mark
      // active. The downstream refresh below picks the all-taps refresh
      // when active and the tap-0-only refresh otherwise.
      bool profile_just_activated = false;
      if (live_changed && lms_for_snap.ctl.profile_pending) {
        if (lms_for_snap.chain_has_leading_tdl ||
            lms_for_snap.ctl.shadow_profile.force) {
          if (snap_profile_from_shadow(lms_for_snap.live_profile,
                                       lms_for_snap.ctl)) {
            lms_for_snap.live_profile_active = true;
            profile_just_activated = true;
          }
        }
      }

      // Phase 3 v1-fin-B / v2.0-F3b: when the snap changed tap-0 / LOS
      // params (v1) or replaced the whole tap layout (v2), refresh the
      // derived fields the kernel reads from DeviceLinkState. Round-trip
      // is conditional: skipped entirely when nothing changed; ~one D2H
      // + one H2D of one DeviceLinkState per dirty edge per update slot.
      // D2H is needed because the device owns cross-slot delay_line +
      // slot_start_samples; an unconditional H2D would clobber that
      // continuity.
      if (live_changed && sp.use_device_channel) {
        std::size_t edge_idx_for_refresh = 0;
        bool found_edge = false;
        for (std::size_t kk = 0; kk < inputs.size(); ++kk) {
          if (inputs[kk].link_key == edge.link_key) {
            edge_idx_for_refresh = kk;
            found_edge = true;
            break;
          }
        }
        if (found_edge && sp.host_link_states[edge_idx_for_refresh].has_tdl) {
          DeviceLinkState* d_state = sp.device_link_states + edge_idx_for_refresh;
          DeviceLinkState* h_state = &sp.host_link_states[edge_idx_for_refresh];
          check(cudaMemcpyAsync(h_state, d_state, sizeof(DeviceLinkState),
                                cudaMemcpyDeviceToHost, sp.stream),
                "snap-refresh D2H");
          check(cudaStreamSynchronize(sp.stream), "snap-refresh D2H sync");
          // Mirror host live into device state in either case.
          h_state->live = lms_for_snap.live;
          if (lms_for_snap.live_profile_active) {
            // v2.0-F3b: all-taps refresh. Replaces tap-0 refresh too —
            // the live profile is the canonical layout for this link.
            refresh_all_taps_from_live(*h_state,
                                       lms_for_snap.live_profile.n_taps,
                                       lms_for_snap.live_profile.taps);
            (void)profile_just_activated;   // unused; future v2.2 warmup uses it
          } else {
            // v1 path: tap-0 refresh only.
            refresh_tap0_from_live(*h_state);
          }
          check(cudaMemcpyAsync(d_state, h_state, sizeof(DeviceLinkState),
                                cudaMemcpyHostToDevice, sp.stream),
                "snap-refresh H2D");
        }
      }
      if (sp.use_device_channel) {
        // Device-kernel path: build_steps reads the SHARED source slot for
        // this edge's snr_db power estimator. Edges sharing a source share
        // the same raw_slot pointer here — fine because build_steps is
        // read-only on its input buffer.
        const int src_idx = sp.host_link_states[k].src_index;
        IqSample* raw_slot = sp.host_source_iq + static_cast<std::size_t>(src_idx) * count;
        build_steps(ls_it->second.model, *edge.model, raw_slot, count, sample_rate_hz);
      } else {
        // Host stage_link path (legacy).
        IqSample* slot = sp.host_staged + k * count;
        stage_link(ls_it->second.model, edge.samples.data(), slot, count, sample_rate_hz);
        build_steps(ls_it->second.model, *edge.model, slot, count, sample_rate_hz);
      }
      const int nsteps = static_cast<int>(edge.model->chain.size());
      sp.host_step_meta[k] = total_steps;                          // offset
      sp.host_step_meta[static_cast<std::size_t>(link_count) + k] = nsteps; // count
      std::copy(ls_it->second.model.host_steps.begin(), ls_it->second.model.host_steps.begin() + nsteps,
                sp.host_steps.begin() + total_steps);
      total_steps += nsteps;
    }

    // Build the receiver model (applied once to the sum). It is a thermal-noise
    // floor, so its AWGN should use an absolute noise_power -- it is built with
    // no input signal.
    int rx_steps = 0;
    if (rx_model != nullptr) {
      if (rx_model->chain.size() > sp.rx_model.step_capacity) {
        throw std::runtime_error("CUDA superposition rx_model chain exceeds preallocated capacity");
      }
      build_steps(sp.rx_model, *rx_model, nullptr, count, sample_rate_hz);
      rx_steps = static_cast<int>(rx_model->chain.size());
    }

    const std::size_t sample_bytes = count * sizeof(IqSample);
    const auto total_start = std::chrono::steady_clock::now();

    check(cudaEventRecord(sp.h2d_start, sp.stream), "cudaEventRecord superpose h2d_start");
    if (sp.use_device_channel) {
      // Phase 2 D2b + D4: raw IQ H2D into device_source_iq, sized by UNIQUE
      // source count. The channel kernel reads per-edge via
      // DeviceLinkState::src_index and writes device_staged. Bytes saved vs
      // pre-D4 = (link_count - num_sources) * count * 8.
      check(cudaMemcpyAsync(sp.device_source_iq, sp.host_source_iq,
                            sp.num_sources * sample_bytes,
                            cudaMemcpyHostToDevice, sp.stream),
            "cudaMemcpyAsync superpose source_iq H2D");
    } else {
      check(cudaMemcpyAsync(sp.device_staged, sp.host_staged,
                            static_cast<std::size_t>(link_count) * sample_bytes,
                            cudaMemcpyHostToDevice, sp.stream),
            "cudaMemcpyAsync superpose staged H2D");
    }
    check(cudaMemcpyAsync(sp.device_steps, sp.host_steps.data(),
                          static_cast<std::size_t>(total_steps) * sizeof(GpuStep), cudaMemcpyHostToDevice, sp.stream),
          "cudaMemcpyAsync superpose steps H2D");
    check(cudaMemcpyAsync(sp.device_step_meta, sp.host_step_meta.data(),
                          static_cast<std::size_t>(2 * link_count) * sizeof(int), cudaMemcpyHostToDevice, sp.stream),
          "cudaMemcpyAsync superpose step meta H2D");
    if (rx_steps != 0) {
      check(cudaMemcpyAsync(sp.device_rx_steps, sp.rx_model.host_steps.data(),
                            static_cast<std::size_t>(rx_steps) * sizeof(GpuStep), cudaMemcpyHostToDevice, sp.stream),
            "cudaMemcpyAsync superpose rx steps H2D");
    }
    check(cudaEventRecord(sp.h2d_done, sp.stream), "cudaEventRecord superpose h2d_done");

    // Phase 2 D2b: run the per-edge channel on the GPU before superposition.
    // Writes device_source_iq → device_staged via apply_channel_kernel
    // (per-edge by DeviceLinkState::src_index), then rolls each link's
    // delay_line ring forward for cross-slot continuity (mirrors
    // apply_tdl_step's host post-loop ring update). Launched on sp.stream
    // so it serialises before superpose_kernel naturally.
    if (sp.use_device_channel) {
      launch_apply_channel_kernel_static(sp.device_link_states,
                                          sp.device_source_iq,
                                          sp.device_staged,
                                          link_count,
                                          static_cast<int>(count),
                                          static_cast<float>(sample_rate_hz),
                                          sp.stream);
      check(cudaGetLastError(), "apply_channel_kernel launch");
      launch_update_delay_line_kernel(sp.device_link_states,
                                       sp.device_source_iq,
                                       link_count,
                                       static_cast<int>(count),
                                       sp.stream);
      check(cudaGetLastError(), "update_delay_line_kernel launch");
    }

    // 256 threads/block is a portable default across the architectures we
    // target today (sm_80, sm_89, sm_120); the per-sample kernel is fully
    // memory-bound at any reasonable choice, so this stays a constant rather
    // than an auto-tuned knob. Revisit when the multi-tap `tdl` kernel lands
    // and tap-count fan-out changes the register pressure picture.
    constexpr int block_size = 256;
    const int grid_size = static_cast<int>((count + block_size - 1) / block_size);
    superpose_kernel<<<grid_size, block_size, 0, sp.stream>>>(sp.device_output, count, link_count, sp.device_staged,
                                                              sp.device_steps, sp.device_step_meta);
    check(cudaGetLastError(), "superpose_kernel launch");
    if (rx_steps != 0) {
      // Receiver model applied in place to the summed signal.
      apply_steps_kernel<<<grid_size, block_size, 0, sp.stream>>>(sp.device_output, sp.device_output, count,
                                                                 sp.device_rx_steps, rx_steps);
      check(cudaGetLastError(), "superpose rx kernel launch");
    }
    check(cudaEventRecord(sp.kernel_done, sp.stream), "cudaEventRecord superpose kernel_done");

    check(cudaMemcpyAsync(sp.host_output, sp.device_output, sample_bytes, cudaMemcpyDeviceToHost, sp.stream),
          "cudaMemcpyAsync superpose D2H");
    check(cudaEventRecord(sp.d2h_done, sp.stream), "cudaEventRecord superpose d2h_done");
    check(cudaStreamSynchronize(sp.stream), "cudaStreamSynchronize superpose");
    std::copy(sp.host_output, sp.host_output + count, output.begin());

    record_timings(sp.h2d_start, sp.h2d_done, sp.kernel_done, sp.d2h_done, total_start,
                   sp.use_device_channel);
  }

  ProcessorTimings last_timings() const override
  {
    std::lock_guard<std::mutex> lock(timings_mutex_);
    return last_timings_;
  }
  const char* backend_name() const override { return "cuda"; }

  std::unordered_map<std::string, BrokerLinkControl*> collect_control_links() override
  {
    // Walk every per-link CudaLinkSlot and expose its host-side
    // BrokerLinkControl by link key. Pointers stay stable for the
    // lifetime of `link_slots_`; the broker calls this once after
    // prepare() and hands the map to ControlServer.
    std::unordered_map<std::string, BrokerLinkControl*> out;
    out.reserve(link_slots_.size());
    for (auto& [key, slot] : link_slots_) {
      out.emplace(key, &slot.model.ctl);
    }
    return out;
  }

private:
  void record_timings(cudaEvent_t h2d_start,
                      cudaEvent_t h2d_done,
                      cudaEvent_t kernel_done,
                      cudaEvent_t d2h_done,
                      std::chrono::steady_clock::time_point total_start,
                      bool used_device_channel)
  {
    float h2d_ms = 0.0F;
    float kernel_ms = 0.0F;
    float d2h_ms = 0.0F;
    check(cudaEventElapsedTime(&h2d_ms, h2d_start, h2d_done), "cudaEventElapsedTime H2D");
    check(cudaEventElapsedTime(&kernel_ms, h2d_done, kernel_done), "cudaEventElapsedTime kernel");
    check(cudaEventElapsedTime(&d2h_ms, kernel_done, d2h_done), "cudaEventElapsedTime D2H");
    const auto total_elapsed = std::chrono::steady_clock::now() - total_start;
    // process_superposition() can run concurrently for distinct destination
    // nodes; guard the shared last-timings snapshot.
    std::lock_guard<std::mutex> lock(timings_mutex_);
    last_timings_.h2d_us = static_cast<double>(h2d_ms) * 1000.0;
    last_timings_.kernel_us = static_cast<double>(kernel_ms) * 1000.0;
    last_timings_.d2h_us = static_cast<double>(d2h_ms) * 1000.0;
    last_timings_.gpu_process_us =
        static_cast<double>(std::chrono::duration_cast<std::chrono::nanoseconds>(total_elapsed).count()) / 1000.0;
    last_timings_.used_device_channel = used_device_channel;
  }

  // Builds a model chain into `ms`, advancing its per-step CFO phase and AWGN
  // counters. `input` (may be null) feeds the analytic power tracking that
  // sizes an snr_db AWGN step without a device-side reduction.
  void build_steps(LinkModelState& ms,
                   const ModelConfig& model,
                   const IqSample* input,
                   std::size_t sample_count,
                   std::uint64_t sample_rate_hz)
  {
    double running_power = 0.0;
    const bool has_awgn = std::any_of(model.chain.begin(), model.chain.end(),
                                      [](const ModelStep& s) { return s.type == ModelStepType::Awgn; });
    if (has_awgn && input != nullptr && sample_count != 0) {
      double sum = 0.0;
      for (std::size_t n = 0; n != sample_count; ++n) {
        const double si = input[n].i;
        const double sq = input[n].q;
        sum += si * si + sq * sq;
      }
      running_power = sum / static_cast<double>(sample_count);
    }

    for (std::size_t step_index = 0; step_index != model.chain.size(); ++step_index) {
      const auto& step = model.chain[step_index];
      auto& gpu_step = ms.host_steps[step_index];
      switch (step.type) {
        case ModelStepType::PathLoss: {
          // Phase 3 C2a: path_loss_db sourced from per-link `live` (populated
          // from YAML at prepare; will be overwritten by snap-from-shadow in
          // C2b once the control plane is wired).
          const float factor = static_cast<float>(std::pow(10.0, -ms.live.path_loss_db / 20.0));
          gpu_step = make_step(Scale, factor, 0.0F);
          running_power *= static_cast<double>(factor) * factor;
          break;
        }
        case ModelStepType::Phase:
        case ModelStepType::Cfo: {
          // Phase 3 C2a: cfo_hz sourced from per-link `live`. phase_rad stays
          // on the step (not a v1 mutable param).
          const double fixed_phase = param_or(step, "phase_rad", 0.0);
          const double cfo_hz = static_cast<double>(ms.live.cfo_hz);
          const double phase_increment =
              sample_rate_hz == 0 ? 0.0 : 2.0 * kPi * cfo_hz / static_cast<double>(sample_rate_hz);
          gpu_step = make_step(Rotate, static_cast<float>(fixed_phase + ms.phase_rad[step_index]),
                               static_cast<float>(phase_increment));
          ms.phase_rad[step_index] = std::fmod(ms.phase_rad[step_index] + phase_increment * sample_count, 2.0 * kPi);
          break;
        }
        case ModelStepType::Awgn: {
          // v1-fin-A: AWGN with two source modes (mirrors CPU backend).
          //   - explicit `noise_power`: absolute, YAML-only, not runtime-
          //     mutable in v1 (runtime control surface is dB).
          //   - implicit `snr_db`: sourced from per-link
          //     `live.awgn_snr_db`, derived per slot against running_power.
          double noise_power = param_or(step, "noise_power", -1.0);
          if (noise_power < 0.0) {
            const double snr_db = static_cast<double>(ms.live.awgn_snr_db);
            noise_power = running_power / std::pow(10.0, snr_db / 10.0);
          }
          noise_power = std::max(0.0, noise_power);
          GpuStep noise_step;
          noise_step.type = AddNoise;
          noise_step.a = static_cast<float>(std::sqrt(noise_power / 2.0)); // per-component sigma
          noise_step.seed = ms.noise_seed[step_index];
          noise_step.counter = ms.noise_counter[step_index];
          gpu_step = noise_step;
          ms.noise_counter[step_index] += sample_count;
          running_power += noise_power;
          break;
        }
        case ModelStepType::Tdl:
          // Leading tdl ran host-side in stage_link() before the H2D copy; on
          // the device this step is a no-op pass-through. configure_leading_
          // propagation() set up the per-link state at prepare time.
          gpu_step = make_step(Scale, 1.0F, 0.0F);
          break;
      }
    }
  }

  int device_ = 0;
  mutable std::mutex timings_mutex_;
  ProcessorTimings last_timings_;
  std::unordered_map<std::string, CudaLinkSlot> link_slots_;
  std::unordered_map<std::string, CudaSuperposeState> superpose_states_;
};

} // namespace

bool cuda_runtime_probe()
{
  int count = 0;
  return cudaGetDeviceCount(&count) == cudaSuccess && count > 0;
}

std::unique_ptr<ChannelProcessor> make_cuda_processor(const TopologyConfig& config)
{
  return std::make_unique<CudaChannelProcessor>(config.runtime.gpu_device);
}

} // namespace ocg
