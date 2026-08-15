#include "ocudu_gpu_channel/backend.h"
#include "ocudu_gpu_channel/cuda_backend.h"
#include "ocudu_gpu_channel/delay.h"
#include "ocudu_gpu_channel/correlation.h"
#include "ocudu_gpu_channel/device_channel.h"
#include "ocudu_gpu_channel/physical_link.h"
#include "ocudu_gpu_channel/mutable_params.h"
#include "ocudu_gpu_channel/processing.h"
#include "ocudu_gpu_channel/runtime_control.h"
#include <algorithm>
#include <chrono>
#include <deque>
#include <cmath>
#include <cuda_runtime.h>
#include <curand_kernel.h>
#include <functional>
#include <iostream>
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
  // The lane's materialised view of the runtime-mutable params, read by
  // build_steps. The DECISION to change them belongs to the link (M4.2); the
  // values stay per lane so a fixed_mimo matrix, whose per-lane weights are
  // folded into per-lane model clones, is not flattened by sharing one copy.
  MutableParams live;

  // M3.5: this lane's LOS-matrix entry, used by the host fallback. The device
  // path carries its own copy in DeviceLinkState.
  std::complex<float> los_coefficient{1.0F, 0.0F};
  // M3.3: scratch grid for the HOST fallback path (stage_link). The device
  // path gets its grids from generate_fading_grid_kernel instead.
  FadingGrid fallback_grid;
  // The physical link this lane belongs to: its clock (M2.3), its lanes' grids
  // (M3.3) and its control block (M4.2). Borrowed from the processor's table.
  PhysicalLinkRuntime* link = nullptr;
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
                                 int nr,
                                 const int* row_begin,
                                 const IqSample* staged,
                                 const GpuStep* steps,
                                 const int* step_meta)
{
  const std::size_t idx = blockIdx.x * blockDim.x + threadIdx.x;
  const int r = static_cast<int>(blockIdx.y);
  if (idx >= count || r >= nr) {
    return;
  }
  // Lanes are grouped by destination row, so row r owns exactly
  // [row_begin[r], row_begin[r+1]). The grouping is built at prepare time by a
  // STABLE sort, which fixes the summation order and with it CPU/CUDA parity.
  float acc_i = 0.0F;
  float acc_q = 0.0F;
  for (int k = row_begin[r]; k != row_begin[r + 1]; ++k) {
    const IqSample sample = staged[static_cast<std::size_t>(k) * count + idx];
    float i = sample.i;
    float q = sample.q;
    apply_chain(i, q, steps + step_meta[k], step_meta[link_count + k], idx);
    acc_i += i;
    acc_q += q;
  }
  dst[static_cast<std::size_t>(r) * count + idx] = {acc_i, acc_q};
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
// `fading_seed` is the lane's seed for chain step 0, derived by the caller
// through physical_link_seed + lane_fading_seed -- the same two functions the
// CPU backend uses, which is what makes a lane's Jakes realisation identical
// on both backends.
void configure_leading_propagation(LinkModelState& state, const ModelConfig& model,
                                    std::uint64_t fading_seed)
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
    prepare_tdl_fading_state(first, fading_seed, state.tdl_fading);
  }
}

// Stages one link's `count` input samples into `out`, applying the chain-
// leading propagation step (tdl multi-tap convolution) host-side, or a plain
// copy when the link's chain has no leading propagation. The device kernel
// then runs the rest of the chain per-sample.
// `slot_start_samples` is the physical link's clock value for this slot, read
// by the caller (physical_link.h). stage_link neither owns nor advances it.
void stage_link(LinkModelState& state, const IqSample* in, IqSample* out,
                std::size_t count, std::uint64_t sample_rate_hz,
                std::uint64_t slot_start_samples)
{
  if (state.has_tdl) {
    // tdl_taps borrowed from ModelConfig; the staging path is bit-identical
    // to the CPU backend because both call the same apply_tdl_step (no
    // fading) or apply_tdl_step_fading (when the leading tdl has a fading
    // sub-config) from delay.h.
    if (state.tdl_fading.enabled) {
      generate_fading_grid(state.tdl_fading, state.tdl_taps->size(), count, sample_rate_hz,
                           slot_start_samples, state.fallback_grid);
      apply_tdl_step_fading(in, out, count, *state.tdl_taps,
                             state.tdl_polyphase, state.delay_line,
                             state.tdl_fading, sample_rate_hz,
                             slot_start_samples, state.fallback_grid,
                             state.los_coefficient);
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
  GpuStep* device_rx_steps = nullptr; // receiver-model chains, rows * rx_step_capacity
  // Output rows (Nr) and the row->lane index boundaries the kernel reads.
  std::size_t rows = 1;
  std::vector<int> host_row_begin;   // rows + 1 entries
  int* device_row_begin = nullptr;
  std::size_t rx_step_capacity = 0;
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
  // M2.3: per-edge "where the next slot starts", staged host-side from each
  // edge's physical-link clock and shipped with the slot so
  // update_delay_line_kernel can assign it. Sized by edge count.
  unsigned long long* host_next_slot_start = nullptr;
  unsigned long long* device_next_slot_start = nullptr;
  // M3.3: this slot's Jakes grids for every incoming edge, written by
  // generate_fading_grid_kernel and read by apply_channel_kernel.
  float* device_fading_grid = nullptr;
  // M3.4: one entry per correlated link feeding this node. Empty when nothing
  // is correlated, and the mixing kernel then does not launch at all.
  DeviceCorrelationGroup* device_correlation_groups = nullptr;
  std::size_t n_correlation_groups = 0;
  // M4.4: host mirror plus the link each group belongs to, so a runtime swap
  // can rewrite ONE group and upload just that one.
  std::vector<DeviceCorrelationGroup> host_correlation_groups;
  // The link each group belongs to, by pointer: a lane key is not the link key
  // (it carries the matrix suffix), and string-matching one against the other
  // is exactly the kind of spelling dependence M2 and M4.2 removed.
  std::vector<PhysicalLinkRuntime*> correlation_group_owner;
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
  // Receiver-model state, one per output ROW. Sibling rows must not share the
  // receiver chain's CFO phase and AWGN counters.
  //
  // deque, not vector: LinkModelState holds a BrokerLinkControl with a
  // non-movable atomic, so a growing vector could not relocate its elements.
  std::deque<LinkModelState> rx_models;
  std::vector<GpuStep> host_steps;
  std::vector<int> host_step_meta;
};

void free_superpose_state(CudaSuperposeState& state)
{
  // v3 follow-on: CudaSuperposeState contains LinkModelState (rx_model)
  // which carries a non-movable atomic via BrokerLinkControl, so the
  // implicit operator= is deleted. Null each pointer inline after
  // freeing so a re-entrant call (re-prepare / dtor) doesn't
  // double-free. Vectors clear themselves; the rx_model is reset by
  // the next init_model_state call when prepare re-fires.
  if (state.d2h_done != nullptr)       { cudaEventDestroy(state.d2h_done);       state.d2h_done = nullptr; }
  if (state.kernel_done != nullptr)    { cudaEventDestroy(state.kernel_done);    state.kernel_done = nullptr; }
  if (state.h2d_done != nullptr)       { cudaEventDestroy(state.h2d_done);       state.h2d_done = nullptr; }
  if (state.h2d_start != nullptr)      { cudaEventDestroy(state.h2d_start);      state.h2d_start = nullptr; }
  if (state.stream != nullptr)         { cudaStreamDestroy(state.stream);        state.stream = nullptr; }
  if (state.device_correlation_groups != nullptr) { cudaFree(state.device_correlation_groups); state.device_correlation_groups = nullptr; }
  if (state.device_fading_grid != nullptr)     { cudaFree(state.device_fading_grid);         state.device_fading_grid = nullptr; }
  if (state.device_next_slot_start != nullptr) { cudaFree(state.device_next_slot_start);     state.device_next_slot_start = nullptr; }
  if (state.host_next_slot_start != nullptr)   { cudaFreeHost(state.host_next_slot_start);    state.host_next_slot_start = nullptr; }
  if (state.device_source_iq != nullptr)   { cudaFree(state.device_source_iq);    state.device_source_iq = nullptr; }
  if (state.host_source_iq != nullptr)     { cudaFreeHost(state.host_source_iq);  state.host_source_iq = nullptr; }
  if (state.device_link_states != nullptr) { cudaFree(state.device_link_states);  state.device_link_states = nullptr; }
  if (state.device_rx_steps != nullptr)    { cudaFree(state.device_rx_steps);     state.device_rx_steps = nullptr; }
  if (state.device_row_begin != nullptr)   { cudaFree(state.device_row_begin);    state.device_row_begin = nullptr; }
  state.rx_models.clear();
  if (state.device_step_meta != nullptr)   { cudaFree(state.device_step_meta);    state.device_step_meta = nullptr; }
  if (state.device_steps != nullptr)       { cudaFree(state.device_steps);        state.device_steps = nullptr; }
  if (state.device_output != nullptr)      { cudaFree(state.device_output);       state.device_output = nullptr; }
  if (state.device_staged != nullptr)      { cudaFree(state.device_staged);       state.device_staged = nullptr; }
  if (state.host_output != nullptr)        { cudaFreeHost(state.host_output);     state.host_output = nullptr; }
  if (state.host_staged != nullptr)        { cudaFreeHost(state.host_staged);     state.host_staged = nullptr; }
  state.capacity = 0;
  state.max_links = 0;
  state.max_steps = 0;
  state.num_sources = 0;
  state.n_correlation_groups = 0;
  state.host_correlation_groups.clear();
  state.host_correlation_groups.shrink_to_fit();
  state.correlation_group_owner.clear();
  state.correlation_group_owner.shrink_to_fit();
  state.use_device_channel = false;
  state.host_link_states.clear();
  state.host_link_states.shrink_to_fit();
  state.source_first_edge.clear();
  state.source_first_edge.shrink_to_fit();
  state.host_steps.clear();
  state.host_steps.shrink_to_fit();
  state.host_step_meta.clear();
  state.host_step_meta.shrink_to_fit();
}

class CudaChannelProcessor final : public ChannelProcessor {
public:
  explicit CudaChannelProcessor(int device) : device_(device)
  {
    check(cudaSetDevice(device_), "cudaSetDevice");
  }

  // Keep the base class's single-row convenience overload visible; overriding
  // the row-vector virtual below would otherwise hide it.
  using ChannelProcessor::process_superposition;

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

    // Same resolved lane table the broker serves from, so a lane key can never
    // be missing when process_superposition looks it up.
    const ResolvedTopology resolved = resolve_topology(config);
    std::unordered_map<std::string, const ResolvedNode*> node_by_id;
    for (const auto& node : resolved.nodes) {
      node_by_id.emplace(node.id, &node);
    }

    // Per-lane model state (chain phase / AWGN counter / delay_line). One slot
    // per lane, looked up by the lane key when packing the staged buffer.
    for (const auto& lane : resolved.lanes) {
      const auto* model = find_model(config, lane.model_id);
      auto dst_it = node_by_id.find(lane.dst_node);
      if (model == nullptr || dst_it == node_by_id.end()) {
        continue;
      }
      const ResolvedNode& destination_node = *dst_it->second;
      auto& slot = link_slots_[lane.key];
      // M2.3 / M4.2: time, grids and control all belong to the physical link,
      // so sibling lanes borrow one runtime rather than each keeping a copy.
      slot.model.link = &links_[lane.physical_link_key];
      init_model_state(slot.model, model->chain.size(), lane.key);
      // The leading tdl is chain step 0 by construction (validate_cuda_support
      // rejects a non-leading one), so the lane's step-0 seed is the one this
      // backend needs.
      configure_leading_propagation(
          slot.model, *model,
          lane_fading_seed(physical_link_seed(lane.physical_link_key),
                           lane.rx_port, lane.tx_port, /*step_index=*/0));
      // Phase 3 v1: populate runtime-mutable params from YAML. build_steps
      // (post-C2a) reads path_loss_db + cfo_hz from `live`. The control
      // plane (C3+) writes to `ctl.shadow` and bumps `ctl.seqno`;
      // snap_mutable_params() in process_superposition picks up shadow → live
      // transitions per slot. Initialise shadow == live so the first serve's
      // snap is a no-op.
      slot.model.live = populate_mutable_params_from_yaml(
          *model, /*reference_power=*/0.0, destination_node.sample_rate_hz);
      if (slot.model.link->control.seqno.load(std::memory_order_relaxed) == 0 &&
          slot.model.link->live_seqno == 0) {
        slot.model.link->live = slot.model.live;
        init_broker_link_control(slot.model.link->control, slot.model.link->live);
      }
      // v2.0-F3b: eligibility flag for the snap path's profile_swap check.
      slot.model.link->chain_has_leading_tdl =
          !model->chain.empty() && model->chain.front().type == ModelStepType::Tdl;

      // v2.2 follow-on: per-link hints for the control plane's
      // warmup-cap check. delay_line.size() is set by
      // configure_leading_propagation when has_leading_tdl is true.
      if (slot.model.link->chain_has_leading_tdl) {
        slot.model.link->control.dl_size_samples_hint =
            static_cast<int>(slot.model.delay_line.size());
      }
      // M4.4: dimensions the control thread validates a swap against.
      slot.model.link->control.nt_hint = lane.nt;
      slot.model.link->control.nr_hint = lane.nr;
      slot.model.link->control.correlation_declared = model->spatial_correlation.declared;
      slot.model.link->control.fixed_mimo_declared = model->fixed_mimo_declared;
      slot.model.link->control.slot_count_hint =
          static_cast<int>(resolve_batch_samples(config.runtime, destination_node.sample_rate_hz));
    }

    // Per-destination superposition state: one entry per radio node that is the
    // target of at least one lane. Keyed by NODE id, which is what the broker
    // passes as dst_key.
    for (const auto& node : resolved.nodes) {
      std::size_t incoming = 0;
      std::size_t max_steps = 0;
      for (const auto& lane : resolved.lanes) {
        if (lane.dst_node != node.id) {
          continue;
        }
        const auto* model = find_model(config, lane.model_id);
        if (model == nullptr) {
          continue;
        }
        ++incoming;
        max_steps = std::max(max_steps, model->chain.size());
      }
      if (incoming == 0) {
        continue;
      }
      const std::size_t capacity = resolve_batch_samples(config.runtime, node.sample_rate_hz);
      // Exception safety: if any of the ~12 sequential cudaHostAlloc /
      // cudaMalloc / cudaStreamCreate / cudaEventCreate / cudaMemcpy calls
      // below throws (via check()), the partially-allocated sp leaks until
      // the processor destructs. Catch + free + erase here so a thrown
      // prepare() leaves either a fully-built entry or no entry at all --
      // re-prepare from scratch stays well-defined, and a leaked processor
      // (caller forgets to destruct on failure) doesn't leak GPU memory.
      try {
      auto& sp = superpose_states_[node.id];
      free_superpose_state(sp);
      sp.capacity = capacity;
      sp.max_links = incoming;
      sp.rows = std::max<std::size_t>(1, node.rx_ports.size());
      sp.max_steps = std::max<std::size_t>(1, max_steps);
      sp.host_steps.assign(incoming * sp.max_steps, GpuStep{});
      sp.host_step_meta.assign(2 * incoming, 0);

      const std::size_t staged_bytes = incoming * capacity * sizeof(IqSample);
      // One row of output per RX port.
      const std::size_t out_bytes = sp.rows * capacity * sizeof(IqSample);
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

      // Row boundaries. The resolved lane table is already stable-sorted by
      // (destination node, rx_port), so walking this node's lanes in order
      // yields contiguous rows and the boundaries fall out of a counting pass.
      sp.host_row_begin.assign(sp.rows + 1, 0);
      {
        std::vector<int> per_row(sp.rows, 0);
        for (const auto& lane : resolved.lanes) {
          if (lane.dst_node != node.id) continue;
          if (find_model(config, lane.model_id) == nullptr) continue;
          if (lane.rx_port < 0 || static_cast<std::size_t>(lane.rx_port) >= sp.rows) {
            throw std::runtime_error("lane rx_port is outside the node's RX ports: " + lane.key);
          }
          ++per_row[static_cast<std::size_t>(lane.rx_port)];
        }
        for (std::size_t r = 0; r != sp.rows; ++r) {
          sp.host_row_begin[r + 1] = sp.host_row_begin[r] + per_row[r];
        }
      }
      check(cudaMalloc(reinterpret_cast<void**>(&sp.device_row_begin),
                       (sp.rows + 1) * sizeof(int)),
            "cudaMalloc superpose row_begin");
      check(cudaMemcpy(sp.device_row_begin, sp.host_row_begin.data(),
                       (sp.rows + 1) * sizeof(int), cudaMemcpyHostToDevice),
            "cudaMemcpy superpose row_begin H2D");

      // Optional receiver model (a thermal-noise floor) applied after the sum.
      // One state here, which is correct while Nr = 1 -- and Nr > 1 is still
      // rejected in process_superposition until M1.6 gives the kernel rows.
      // M1.6 grows this to one state per row, keyed by rx_state_key().
      const auto* rx = node.rx_model.empty() ? nullptr : find_model(config, node.rx_model);
      if (rx != nullptr) {
        sp.rx_step_capacity = std::max<std::size_t>(1, rx->chain.size());
        sp.rx_models.clear();
        const int nr = static_cast<int>(sp.rows);
        for (std::size_t r = 0; r != sp.rows; ++r) {
          init_model_state(sp.rx_models.emplace_back(), rx->chain.size(),
                           rx_state_key(node.id, static_cast<int>(r), nr));
        }
        check(cudaMalloc(reinterpret_cast<void**>(&sp.device_rx_steps),
                         sp.rows * sp.rx_step_capacity * sizeof(GpuStep)),
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
      //
      // The dedup key is the source DEVICE (transport port), not the source
      // node: two lanes off the same radio but different tx_ports carry
      // different IQ, so keying on the node would make them share one staged
      // buffer and silently feed every lane the port-0 signal.
      std::unordered_map<std::string, int> src_to_index;
      sp.source_first_edge.clear();
      std::vector<int> per_edge_src_index;
      per_edge_src_index.reserve(incoming);
      // M3.4: which link each edge belongs to and where it sits in that link's
      // matrix, recorded in the same walk so the correlation groups below are
      // built from the resolver's lane view rather than a second derivation.
      std::vector<const LaneConfig*> per_edge_lane;
      per_edge_lane.reserve(incoming);
      for (const auto& lane : resolved.lanes) {
        if (lane.dst_node != node.id) continue;
        const auto* model = find_model(config, lane.model_id);
        if (model == nullptr) continue;
        auto ls_it = link_slots_.find(lane.key);
        if (ls_it == link_slots_.end()) continue;
        per_edge_lane.push_back(&lane);
        auto [it, inserted] = src_to_index.try_emplace(
            lane.src_device, static_cast<int>(sp.source_first_edge.size()));
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
      // M2.3: one slot-start value per incoming edge.
      const std::size_t slot_start_bytes = incoming * sizeof(unsigned long long);
      check(cudaHostAlloc(reinterpret_cast<void**>(&sp.host_next_slot_start),
                          slot_start_bytes, cudaHostAllocDefault),
            "cudaHostAlloc superpose next_slot_start");
      check(cudaMalloc(reinterpret_cast<void**>(&sp.device_next_slot_start),
                       slot_start_bytes),
            "cudaMalloc superpose next_slot_start");
      // One grid per edge: kDeviceMaxTaps x kDeviceMaxGridPoints complex, ~8 KB
      // per edge, so a 16-edge node costs ~128 KB of device memory.
      check(cudaMalloc(reinterpret_cast<void**>(&sp.device_fading_grid),
                       incoming * kDeviceMaxTaps * kDeviceMaxGridPoints * 2 * sizeof(float)),
            "cudaMalloc superpose fading_grid");
      std::size_t k_idx = 0;
      // Dispatch gate: every incoming edge must have a leading tdl step
      // (fading-enabled tdl included -- the device kernel handles both
      // static and Jakes/Rician branches internally). Mixed nodes (any
      // non-tdl-leading edge) fall back to host stage_link for the whole
      // destination.
      bool all_leading_tdl = (incoming > 0);
      for (const auto& lane : resolved.lanes) {
        if (lane.dst_node != node.id) {
          continue;
        }
        const auto* model = find_model(config, lane.model_id);
        if (model == nullptr) {
          continue;
        }
        auto ls_it = link_slots_.find(lane.key);
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
        // M3.5: this edge's LOS-matrix entry, on both paths.
        {
          const auto los = lane_los_coefficients(model->los_matrix, lane.nt, lane.nr);
          const CplxD coefficient = los[static_cast<std::size_t>(lane.rx_port) * lane.nt + lane.tx_port];
          sp.host_link_states[k_idx].los_coeff_re = static_cast<float>(coefficient.real());
          sp.host_link_states[k_idx].los_coeff_im = static_cast<float>(coefficient.imag());
          link_slots_[lane.key].model.los_coefficient =
              std::complex<float>(static_cast<float>(coefficient.real()),
                                  static_cast<float>(coefficient.imag()));
        }
        ++k_idx;
      }
      sp.use_device_channel = all_leading_tdl;

      // M3.4: group this node's edges by physical link, and build the mixing
      // matrix of each correlated one.
      std::vector<DeviceCorrelationGroup> groups;
      {
        std::unordered_map<std::string, std::size_t> group_of;
        for (std::size_t edge = 0; edge != per_edge_lane.size(); ++edge) {
          const LaneConfig& lane = *per_edge_lane[edge];
          const auto* model = find_model(config, lane.model_id);
          // M4.4: a link that DECLARED the block gets a group even when it is
          // iid today -- the declaration is the opt-in that makes it
          // runtime-correlatable, and a swap needs somewhere to land. A link
          // that never declared one keeps the untouched pre-M3 path.
          if (model == nullptr || !model->spatial_correlation.declared) {
            continue;
          }
          // A correlated link cannot be served by the host fallback: the
          // fallback stages each edge on its own and has no cross-lane step, so
          // it would silently drop the correlation. Refuse instead.
          if (!sp.use_device_channel) {
            throw std::runtime_error(
                "node " + node.id + " falls back to host staging (an incoming edge does not lead "
                "with tdl), but link " + lane.physical_link_key +
                " declares spatial_correlation, which only the device path applies");
          }
          auto [it, inserted] = group_of.try_emplace(lane.physical_link_key, groups.size());
          if (inserted) {
            DeviceCorrelationGroup group{};
            group.n_lanes = lane.nt * lane.nr;
            for (int l = 0; l != kMaxCorrelatedLanes; ++l) {
              group.edge_index[l] = -1;
            }
            std::vector<CplxD> mixing;
            std::string error;
            if (!lane_mixing_matrix(model->spatial_correlation, lane.nt, lane.nr, mixing, error)) {
              throw std::runtime_error("link " + lane.physical_link_key + ": " + error);
            }
            sp.correlation_group_owner.push_back(&links_[lane.physical_link_key]);
            const int n = group.n_lanes;
            for (int r0 = 0; r0 != n; ++r0) {
              for (int c0 = 0; c0 != n; ++c0) {
                const CplxD value = mixing[static_cast<std::size_t>(r0) * n + c0];
                group.mixing_re[r0][c0] = static_cast<float>(value.real());
                group.mixing_im[r0][c0] = static_cast<float>(value.imag());
              }
            }
            groups.push_back(group);
          }
          groups[it->second].edge_index[lane.rx_port * lane.nt + lane.tx_port] =
              static_cast<int>(edge);
        }
        for (const auto& group : groups) {
          for (int l = 0; l != group.n_lanes; ++l) {
            if (group.edge_index[l] < 0) {
              throw std::runtime_error(
                  "node " + node.id +
                  " has a correlated link with a missing lane; correlation is defined over the "
                  "whole Nt x Nr matrix");
            }
          }
        }
      }
      sp.n_correlation_groups = groups.size();
      sp.host_correlation_groups = groups;
      if (!groups.empty()) {
        check(cudaMalloc(reinterpret_cast<void**>(&sp.device_correlation_groups),
                         groups.size() * sizeof(DeviceCorrelationGroup)),
              "cudaMalloc superpose correlation groups");
        check(cudaMemcpy(sp.device_correlation_groups, groups.data(),
                         groups.size() * sizeof(DeviceCorrelationGroup), cudaMemcpyHostToDevice),
              "cudaMemcpy correlation groups H2D");
      }

      check(cudaMemcpy(sp.device_link_states, sp.host_link_states.data(),
                       incoming * sizeof(DeviceLinkState), cudaMemcpyHostToDevice),
            "cudaMemcpy device_link_states H2D");
      } catch (...) {
        auto it = superpose_states_.find(node.id);
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
                             std::span<std::span<IqSample>> outputs) override
  {
    if (outputs.empty()) {
      return;
    }
    auto sp_it = superpose_states_.find(dst_key);
    if (sp_it == superpose_states_.end()) {
      throw std::runtime_error("CUDA superposition state was not preallocated: " + dst_key);
    }
    auto& sp = sp_it->second;
    if (outputs.size() != sp.rows) {
      throw std::runtime_error("CUDA superposition row count does not match the prepared node: " +
                               dst_key);
    }
    const std::size_t count = outputs[0].size();
    for (const auto& row : outputs) {
      if (row.size() != count) {
        throw std::runtime_error("CUDA superposition output rows have unequal lengths");
      }
    }
    if (count == 0) {
      return;
    }
    if (count > sp.capacity) {
      throw std::runtime_error("CUDA superposition batch exceeds preallocated capacity");
    }
    if (inputs.empty()) {
      for (const auto& row : outputs) {
        std::fill(row.begin(), row.end(), IqSample{});
      }
      return;
    }
    // The broker hands lanes in resolved order, which is grouped by rx_port,
    // so the k-th input is the k-th lane the row boundaries were built from.
    // Verify rather than assume: a mismatch here would silently sum the wrong
    // lanes into a row.
    if (static_cast<std::size_t>(sp.host_row_begin.back()) != inputs.size()) {
      throw std::runtime_error("CUDA superposition lane count does not match the prepared rows: " +
                               dst_key);
    }
    for (std::size_t r = 0; r != sp.rows; ++r) {
      for (int k = sp.host_row_begin[r]; k != sp.host_row_begin[r + 1]; ++k) {
        if (inputs[static_cast<std::size_t>(k)].rx_port != static_cast<int>(r)) {
          throw std::runtime_error("CUDA superposition inputs are not grouped by rx_port: " +
                                   dst_key);
        }
      }
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
    // M4.2 -- one snap per LINK per slot, before any of its edges is staged.
    // Running it per edge (as it did) meant a 2x2 link could take a swap in
    // four different slots, which is two channel matrices in one slot and
    // neither of them.
    std::vector<PhysicalLinkRuntime*> touched_links;
    std::vector<LinkSnapOutcome> link_outcomes;
    touched_links.reserve(inputs.size());
    link_outcomes.reserve(inputs.size());
    const auto link_index_of = [&](const PhysicalLinkRuntime* link) -> std::size_t {
      for (std::size_t i = 0; i != touched_links.size(); ++i) {
        if (touched_links[i] == link) {
          return i;
        }
      }
      return touched_links.size(); // unreachable: the pass below inserts every link
    };
    for (const auto& edge : inputs) {
      auto ls_it = link_slots_.find(edge.link_key);
      if (ls_it == link_slots_.end() || ls_it->second.model.link == nullptr) {
        continue;
      }
      PhysicalLinkRuntime* link = ls_it->second.model.link;
      if (link_index_of(link) != touched_links.size()) {
        continue; // a sibling lane already snapped this link
      }
      touched_links.push_back(link);
      link_outcomes.push_back(snap_physical_link(*link, edge.link_key, count));
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
      // M4.2: the snap already ran ONCE for this edge's link, in the pass above.
      // What is left here is per-edge work that the link's decision implies:
      // take the new values, and refresh the device-side derived fields.
      auto& lms_for_snap = ls_it->second.model;
      PhysicalLinkRuntime& link = *lms_for_snap.link;
      const LinkSnapOutcome outcome = link_outcomes[link_index_of(&link)];
      const std::uint64_t snap_idx = link.next_slot == 0 ? 0 : link.next_slot - 1;
      if (outcome.values_changed) {
        lms_for_snap.live = link.live;
      }

      // Phase 3 v1-fin-B / v2.0-F3b: when the snap changed tap-0 / LOS params
      // (v1) or replaced the whole tap layout (v2), refresh the derived fields
      // the kernel reads from DeviceLinkState. The round-trip is conditional
      // and the D2H is required: the device owns the cross-slot delay_line, so
      // an unconditional H2D would clobber that continuity.
      if (outcome.values_changed && sp.use_device_channel && sp.host_link_states[k].has_tdl) {
        DeviceLinkState* d_state = sp.device_link_states + k;
        DeviceLinkState* h_state = &sp.host_link_states[k];
        check(cudaMemcpyAsync(h_state, d_state, sizeof(DeviceLinkState),
                              cudaMemcpyDeviceToHost, sp.stream),
              "snap-refresh D2H");
        check(cudaStreamSynchronize(sp.stream), "snap-refresh D2H sync");
        h_state->live = lms_for_snap.live;
        if (link.live_profile_active) {
          // v2.0-F3b: all-taps refresh -- the live profile is the canonical
          // layout for this link.
          refresh_all_taps_from_live(*h_state, link.live_profile.n_taps,
                                     link.live_profile.taps);
          if (outcome.profile_activated) {
            // v2.2 W1: zero this lane's cross-slot ring so the new layout does
            // not convolve with the previous profile's tail. Every lane of the
            // link reaches this in the SAME slot, because the decision that
            // brought them here was the link's.
            for (int i = 0; i < kDeviceMaxDelayLine; ++i) {
              h_state->delay_line[i] = IqSample{};
            }
          }
        } else {
          refresh_tap0_from_live(*h_state);
        }
        check(cudaMemcpyAsync(d_state, h_state, sizeof(DeviceLinkState),
                              cudaMemcpyHostToDevice, sp.stream),
              "snap-refresh H2D");
      }
      // M4.4: a correlation swap landed for this link, so its device group has
      // to carry the new factor before this slot's mixing kernel runs.
      if (outcome.correlation_changed) {
        for (std::size_t g = 0; g != sp.correlation_group_owner.size(); ++g) {
          if (sp.correlation_group_owner[g] != &link) {
            continue;
          }
          DeviceCorrelationGroup& host_group = sp.host_correlation_groups[g];
          const auto& mixing = link.fading.mixing;
          const int n = host_group.n_lanes;
          for (int r0 = 0; r0 != n; ++r0) {
            for (int c0 = 0; c0 != n; ++c0) {
              const std::size_t idx = static_cast<std::size_t>(r0) * n + c0;
              const std::complex<float> value =
                  idx < mixing.size() ? mixing[idx]
                                      : std::complex<float>{r0 == c0 ? 1.0F : 0.0F, 0.0F};
              host_group.mixing_re[r0][c0] = value.real();
              host_group.mixing_im[r0][c0] = value.imag();
            }
          }
          check(cudaMemcpyAsync(sp.device_correlation_groups + g, &host_group,
                                sizeof(DeviceCorrelationGroup), cudaMemcpyHostToDevice,
                                sp.stream),
                "correlation-swap group H2D");
          break;
        }
      }

      // Host-fallback path keeps its own ring, zeroed on the same slot.
      if (outcome.profile_activated && !sp.use_device_channel) {
        std::fill(lms_for_snap.delay_line.begin(), lms_for_snap.delay_line.end(), IqSample{});
      }

      // M2.3: the device copy of this edge's time is written, not
      // accumulated, so record where its link's NEXT slot begins. Read before
      // any clock advance below, so every edge of a link ships the same value.
      sp.host_next_slot_start[k] =
          link.clock.slot_start_samples + static_cast<unsigned long long>(count);
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
        stage_link(ls_it->second.model, edge.samples.data(), slot, count, sample_rate_hz,
                   ls_it->second.model.link->clock.slot_start_samples);
        build_steps(ls_it->second.model, *edge.model, slot, count, sample_rate_hz);
      }
      const int nsteps = static_cast<int>(edge.model->chain.size());
      sp.host_step_meta[k] = total_steps;                          // offset
      sp.host_step_meta[static_cast<std::size_t>(link_count) + k] = nsteps; // count
      std::copy(ls_it->second.model.host_steps.begin(), ls_it->second.model.host_steps.begin() + nsteps,
                sp.host_steps.begin() + total_steps);
      total_steps += nsteps;
    }

    // Every edge of this node has now been staged against its link's slot-start
    // time, so each link's clock moves on by one slot -- once per link, not
    // once per lane. A link has a single destination node, so this thread is
    // its only writer.
    {
      // The links touched this slot are exactly the ones the snap pass found.
      for (auto* link : touched_links) {
        link->clock.slot_start_samples += count;
      }
    }

    // Build the receiver model (applied once to the sum). It is a thermal-noise
    // floor, so its AWGN should use an absolute noise_power -- it is built with
    // no input signal.
    int rx_steps = 0;
    const std::size_t rx_stride = sp.rx_step_capacity;
    if (rx_model != nullptr) {
      if (sp.rx_models.empty() || rx_model->chain.size() > sp.rx_models.front().step_capacity) {
        throw std::runtime_error("CUDA superposition rx_model chain exceeds preallocated capacity");
      }
      // One build per row: each row advances its own CFO phase and AWGN
      // counters, so they must not share state.
      for (std::size_t r = 0; r != sp.rows; ++r) {
        build_steps(sp.rx_models[r], *rx_model, nullptr, count, sample_rate_hz);
      }
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
      check(cudaMemcpyAsync(sp.device_next_slot_start, sp.host_next_slot_start,
                            static_cast<std::size_t>(link_count) * sizeof(unsigned long long),
                            cudaMemcpyHostToDevice, sp.stream),
            "cudaMemcpyAsync superpose next_slot_start H2D");
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
      for (std::size_t r = 0; r != sp.rows; ++r) {
        check(cudaMemcpyAsync(sp.device_rx_steps + r * rx_stride, sp.rx_models[r].host_steps.data(),
                              static_cast<std::size_t>(rx_steps) * sizeof(GpuStep),
                              cudaMemcpyHostToDevice, sp.stream),
              "cudaMemcpyAsync superpose rx steps H2D");
      }
    }
    check(cudaEventRecord(sp.h2d_done, sp.stream), "cudaEventRecord superpose h2d_done");

    // Phase 2 D2b: run the per-edge channel on the GPU before superposition.
    // Writes device_source_iq → device_staged via apply_channel_kernel
    // (per-edge by DeviceLinkState::src_index), then rolls each link's
    // delay_line ring forward for cross-slot continuity (mirrors
    // apply_tdl_step's host post-loop ring update). Launched on sp.stream
    // so it serialises before superpose_kernel naturally.
    if (sp.use_device_channel) {
      // M3.3: generate, then convolve. Same stream, so the ordering is the
      // dependency -- and M3.4's mixing goes between these two launches.
      launch_generate_fading_grid_kernel(sp.device_link_states,
                                         sp.device_fading_grid,
                                         link_count,
                                         static_cast<int>(count),
                                         static_cast<float>(sample_rate_hz),
                                         sp.stream);
      check(cudaGetLastError(), "generate_fading_grid_kernel launch");
      if (sp.n_correlation_groups != 0) {
        launch_mix_fading_grid_kernel(sp.device_fading_grid,
                                      sp.device_correlation_groups,
                                      sp.device_link_states,
                                      static_cast<int>(sp.n_correlation_groups),
                                      static_cast<int>(count),
                                      static_cast<float>(sample_rate_hz),
                                      sp.stream);
        check(cudaGetLastError(), "mix_fading_grid_kernel launch");
      }
      launch_apply_channel_kernel_static(sp.device_link_states,
                                          sp.device_source_iq,
                                          sp.device_fading_grid,
                                          sp.device_staged,
                                          link_count,
                                          static_cast<int>(count),
                                          static_cast<float>(sample_rate_hz),
                                          sp.stream);
      check(cudaGetLastError(), "apply_channel_kernel launch");
      launch_update_delay_line_kernel(sp.device_link_states,
                                       sp.device_source_iq,
                                       sp.device_next_slot_start,
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
    const dim3 grid(static_cast<unsigned>((count + block_size - 1) / block_size),
                    static_cast<unsigned>(sp.rows));
    superpose_kernel<<<grid, block_size, 0, sp.stream>>>(
        sp.device_output, count, link_count, static_cast<int>(sp.rows), sp.device_row_begin,
        sp.device_staged, sp.device_steps, sp.device_step_meta);
    check(cudaGetLastError(), "superpose_kernel launch");
    if (rx_steps != 0) {
      // Receiver model applied in place, once per row against that row's own
      // chain state. Nr is small, so a launch per row costs less than the
      // machinery a row-aware variant of this kernel would need.
      const int row_grid = static_cast<int>((count + block_size - 1) / block_size);
      for (std::size_t r = 0; r != sp.rows; ++r) {
        IqSample* row = sp.device_output + r * count;
        apply_steps_kernel<<<row_grid, block_size, 0, sp.stream>>>(
            row, row, count, sp.device_rx_steps + r * rx_stride, rx_steps);
      }
      check(cudaGetLastError(), "superpose rx kernel launch");
    }
    check(cudaEventRecord(sp.kernel_done, sp.stream), "cudaEventRecord superpose kernel_done");

    check(cudaMemcpyAsync(sp.host_output, sp.device_output, sp.rows * sample_bytes,
                          cudaMemcpyDeviceToHost, sp.stream),
          "cudaMemcpyAsync superpose D2H");
    check(cudaEventRecord(sp.d2h_done, sp.stream), "cudaEventRecord superpose d2h_done");
    check(cudaStreamSynchronize(sp.stream), "cudaStreamSynchronize superpose");
    for (std::size_t r = 0; r != sp.rows; ++r) {
      const IqSample* row = sp.host_output + r * count;
      std::copy(row, row + count, outputs[r].begin());
    }

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
    // M4.2: one entry per PHYSICAL LINK, keyed by the link's identity -- a 2x2
    // link is one control endpoint, not four, and the address carries no lane
    // suffix. At Nt = Nr = 1 the two strings are identical, so existing 1x1
    // deployments keep their link_id.
    std::unordered_map<std::string, BrokerLinkControl*> out;
    out.reserve(links_.size());
    for (auto& [key, link] : links_) {
      out.emplace(key, &link.control);
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
  // M2.3: absolute time, one entry per physical link, keyed by
  // LaneConfig::physical_link_key. Node-based storage, so the pointers the
  // lanes hold stay valid as the table grows.
  std::unordered_map<std::string, PhysicalLinkRuntime> links_;
};

} // namespace

bool cuda_runtime_probe()
{
  int count = 0;
  return cudaGetDeviceCount(&count) == cudaSuccess && count > 0;
}

// v3.2 HW1: real CUDA hardware probe. Mirrors the
// cudaGetDeviceProperties surface fields the broker needs to log + the
// footprint / strict-mode checks need at startup.
HardwareProbe probe_cuda_hardware(int device_id)
{
  HardwareProbe p;
  p.device_id = device_id;

  int count = 0;
  if (cudaGetDeviceCount(&count) != cudaSuccess || count <= 0) {
    p.ok = false;
    p.error = "no CUDA devices visible (cudaGetDeviceCount)";
    return p;
  }
  if (device_id < 0 || device_id >= count) {
    p.ok = false;
    p.error = "configured gpu_device=" + std::to_string(device_id)
              + " is out of range (0.." + std::to_string(count - 1) + ")";
    return p;
  }

  cudaDeviceProp props{};
  if (cudaGetDeviceProperties(&props, device_id) != cudaSuccess) {
    p.ok = false;
    p.error = "cudaGetDeviceProperties failed for device " + std::to_string(device_id);
    return p;
  }

  p.ok              = true;
  p.name            = props.name;
  p.sm_major        = props.major;
  p.sm_minor        = props.minor;
  p.total_mem_bytes = static_cast<std::uint64_t>(props.totalGlobalMem);

  cudaDriverGetVersion(&p.driver_version);
  cudaRuntimeGetVersion(&p.runtime_version);

  return p;
}

std::unique_ptr<ChannelProcessor> make_cuda_processor(const TopologyConfig& config)
{
  return std::make_unique<CudaChannelProcessor>(config.runtime.gpu_device);
}

} // namespace ocg
