#include "ocudu_gpu_channel/cuda_backend.h"
#include "ocudu_gpu_channel/delay.h"
#include "ocudu_gpu_channel/processing.h"
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
struct LinkModelState {
  std::size_t step_capacity = 0;
  std::vector<GpuStep> host_steps;
  std::vector<double> phase_rad;
  std::vector<unsigned int> noise_seed;        // per-step AWGN RNG seed
  std::vector<unsigned long long> noise_counter; // per-step AWGN sample counter
  // Chain-leading propagation delay. The CUDA kernel is strictly per-sample, so
  // a delay -- which needs neighbouring and previous-batch samples -- is applied
  // host-side while staging (see apply_leading_delay / delay.h) and is a no-op
  // on the device. delay_line carries the previous batch's tail.
  bool has_delay = false;
  std::size_t delay_int = 0;
  double delay_frac = 0.0;
  std::vector<IqSample> delay_line;
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

// Records a chain-leading sample-delay step on the model state. validate_cuda_
// support() has already guaranteed any delay step is a chain's first step, so
// only chain.front() can be one.
void configure_delay(LinkModelState& state, const ModelConfig& model)
{
  state.has_delay = false;
  state.delay_int = 0;
  state.delay_frac = 0.0;
  state.delay_line.clear();
  if (model.chain.empty()) {
    return;
  }
  const auto& first = model.chain.front();
  if (first.type != ModelStepType::IntegerDelay && first.type != ModelStepType::FractionalDelay) {
    return;
  }
  const double requested = param_or(first, "delay_samples", 0.0);
  state.has_delay = true;
  state.delay_int = static_cast<std::size_t>(std::max(0.0, std::floor(requested)));
  state.delay_frac = first.type == ModelStepType::FractionalDelay ? requested - std::floor(requested) : 0.0;
}

// Stages one link's `count` input samples into `out`, applying the chain-
// leading propagation delay (or a plain copy when the link has none). The
// device kernel then runs the rest of the chain per-sample.
void apply_leading_delay(LinkModelState& state, const IqSample* in, IqSample* out, std::size_t count)
{
  if (state.has_delay) {
    apply_sample_delay(in, out, count, state.delay_int, state.delay_frac, state.delay_line);
  } else {
    std::copy(in, in + count, out);
  }
}

// One link's broker-side CUDA state: its model-chain state plus the per-call
// device scratch (input/output/steps buffers, stream, events). Only
// process_into() uses the device scratch; it is allocated lazily.
struct CudaLinkState {
  std::size_t capacity = 0;
  LinkModelState model;
  IqSample* host_input = nullptr;
  IqSample* host_output = nullptr;
  IqSample* device_input = nullptr;
  IqSample* device_output = nullptr;
  GpuStep* device_steps = nullptr;
  cudaStream_t stream = nullptr;
  cudaEvent_t h2d_start = nullptr;
  cudaEvent_t h2d_done = nullptr;
  cudaEvent_t kernel_done = nullptr;
  cudaEvent_t d2h_done = nullptr;
};

void free_state(CudaLinkState& state)
{
  if (state.h2d_start != nullptr) {
    cudaEventDestroy(state.h2d_start);
  }
  if (state.h2d_done != nullptr) {
    cudaEventDestroy(state.h2d_done);
  }
  if (state.kernel_done != nullptr) {
    cudaEventDestroy(state.kernel_done);
  }
  if (state.d2h_done != nullptr) {
    cudaEventDestroy(state.d2h_done);
  }
  if (state.stream != nullptr) {
    cudaStreamDestroy(state.stream);
  }
  if (state.device_steps != nullptr) {
    cudaFree(state.device_steps);
  }
  if (state.device_output != nullptr) {
    cudaFree(state.device_output);
  }
  if (state.device_input != nullptr) {
    cudaFree(state.device_input);
  }
  if (state.host_output != nullptr) {
    cudaFreeHost(state.host_output);
  }
  if (state.host_input != nullptr) {
    cudaFreeHost(state.host_input);
  }
  state = {};
}

// Lazily allocates a link's per-call device scratch. Only process_into() needs
// it; a processor used purely for superposition never pays for it.
void ensure_link_device(CudaLinkState& state)
{
  if (state.device_input != nullptr) {
    return;
  }
  const std::size_t sample_bytes = state.capacity * sizeof(IqSample);
  check(cudaHostAlloc(reinterpret_cast<void**>(&state.host_input), sample_bytes, cudaHostAllocDefault),
        "cudaHostAlloc input");
  check(cudaHostAlloc(reinterpret_cast<void**>(&state.host_output), sample_bytes, cudaHostAllocDefault),
        "cudaHostAlloc output");
  check(cudaMalloc(reinterpret_cast<void**>(&state.device_input), sample_bytes), "cudaMalloc input");
  check(cudaMalloc(reinterpret_cast<void**>(&state.device_output), sample_bytes), "cudaMalloc output");
  check(cudaMalloc(reinterpret_cast<void**>(&state.device_steps),
                   std::max<std::size_t>(1, state.model.step_capacity) * sizeof(GpuStep)),
        "cudaMalloc steps");
  check(cudaStreamCreateWithFlags(&state.stream, cudaStreamNonBlocking), "cudaStreamCreateWithFlags");
  check(cudaEventCreate(&state.h2d_start), "cudaEventCreate h2d_start");
  check(cudaEventCreate(&state.h2d_done), "cudaEventCreate h2d_done");
  check(cudaEventCreate(&state.kernel_done), "cudaEventCreate kernel_done");
  check(cudaEventCreate(&state.d2h_done), "cudaEventCreate d2h_done");
}

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
    for (auto& [_, state] : states_) {
      free_state(state);
    }
    for (auto& [_, sp] : superpose_states_) {
      free_superpose_state(sp);
    }
  }

  void prepare(const TopologyConfig& config) override
  {
    check(cudaSetDevice(config.runtime.gpu_device), "cudaSetDevice");
    device_ = config.runtime.gpu_device;

    // Per-link model state. Device scratch is allocated lazily by process_into().
    for (const auto& link : config.links) {
      const auto* destination = find_device(config, link.to);
      const auto* model = find_model(config, link.model);
      if (destination == nullptr || model == nullptr) {
        continue;
      }
      auto& state = states_[link_key(link)];
      free_state(state);
      state.capacity = resolve_batch_samples(config.runtime, destination->sample_rate_hz);
      init_model_state(state.model, model->chain.size(), link_key(link));
      configure_delay(state.model, *model);
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
    }
  }

  void process_into(const std::string& link_key_value,
                    const ModelConfig& model,
                    std::span<const IqSample> input,
                    std::span<IqSample> output,
                    std::uint64_t sample_rate_hz) override
  {
    if (output.size() != input.size()) {
      throw std::runtime_error("CUDA process_into input and output sizes must match");
    }
    if (input.empty()) {
      std::lock_guard<std::mutex> lock(timings_mutex_);
      last_timings_ = {};
      return;
    }
    auto it = states_.find(link_key_value);
    if (it == states_.end()) {
      throw std::runtime_error("CUDA link state was not preallocated: " + link_key_value);
    }
    auto& state = it->second;
    if (input.size() > state.capacity) {
      throw std::runtime_error("CUDA input batch exceeds preallocated link capacity");
    }
    if (model.chain.size() > state.model.step_capacity) {
      throw std::runtime_error("CUDA model chain exceeds preallocated step capacity");
    }
    ensure_link_device(state);

    const auto total_start = std::chrono::steady_clock::now();
    apply_leading_delay(state.model, input.data(), state.host_input, input.size());
    build_steps(state.model, model, state.host_input, input.size(), sample_rate_hz);

    const std::size_t sample_bytes = input.size() * sizeof(IqSample);
    const std::size_t step_bytes = std::max<std::size_t>(1, model.chain.size()) * sizeof(GpuStep);
    check(cudaEventRecord(state.h2d_start, state.stream), "cudaEventRecord h2d_start");
    check(cudaMemcpyAsync(state.device_steps, state.model.host_steps.data(), step_bytes, cudaMemcpyHostToDevice,
                          state.stream),
          "cudaMemcpyAsync steps H2D");
    check(cudaMemcpyAsync(state.device_input, state.host_input, sample_bytes, cudaMemcpyHostToDevice, state.stream),
          "cudaMemcpyAsync samples H2D");
    check(cudaEventRecord(state.h2d_done, state.stream), "cudaEventRecord h2d_done");

    constexpr int block_size = 256;
    const int grid_size = static_cast<int>((input.size() + block_size - 1) / block_size);
    apply_steps_kernel<<<grid_size, block_size, 0, state.stream>>>(
        state.device_input, state.device_output, input.size(), state.device_steps,
        static_cast<int>(model.chain.size()));
    check(cudaGetLastError(), "apply_steps_kernel launch");
    check(cudaEventRecord(state.kernel_done, state.stream), "cudaEventRecord kernel_done");

    check(cudaMemcpyAsync(state.host_output, state.device_output, sample_bytes, cudaMemcpyDeviceToHost, state.stream),
          "cudaMemcpyAsync samples D2H");
    check(cudaEventRecord(state.d2h_done, state.stream), "cudaEventRecord d2h_done");
    check(cudaStreamSynchronize(state.stream), "cudaStreamSynchronize");
    std::copy(state.host_output, state.host_output + input.size(), output.begin());

    record_timings(state.h2d_start, state.h2d_done, state.kernel_done, state.d2h_done, total_start);
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
    // chain. Per-edge CFO/AWGN state lives in the edge's own CudaLinkState.
    const int link_count = static_cast<int>(inputs.size());
    int total_steps = 0;
    for (std::size_t k = 0; k != inputs.size(); ++k) {
      const auto& edge = inputs[k];
      if (edge.model == nullptr || edge.samples.size() != count) {
        throw std::runtime_error("CUDA superposition input is malformed");
      }
      if (edge.model->chain.size() > sp.max_steps) {
        throw std::runtime_error("CUDA superposition model chain exceeds preallocated capacity");
      }
      auto ls_it = states_.find(edge.link_key);
      if (ls_it == states_.end()) {
        throw std::runtime_error("CUDA link state was not preallocated: " + edge.link_key);
      }
      IqSample* slot = sp.host_staged + k * count;
      apply_leading_delay(ls_it->second.model, edge.samples.data(), slot, count);
      build_steps(ls_it->second.model, *edge.model, slot, count, sample_rate_hz);
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
    check(cudaMemcpyAsync(sp.device_staged, sp.host_staged, static_cast<std::size_t>(link_count) * sample_bytes,
                          cudaMemcpyHostToDevice, sp.stream),
          "cudaMemcpyAsync superpose staged H2D");
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

    record_timings(sp.h2d_start, sp.h2d_done, sp.kernel_done, sp.d2h_done, total_start);
  }

  ProcessorTimings last_timings() const override
  {
    std::lock_guard<std::mutex> lock(timings_mutex_);
    return last_timings_;
  }
  const char* backend_name() const override { return "cuda"; }

private:
  void record_timings(cudaEvent_t h2d_start,
                      cudaEvent_t h2d_done,
                      cudaEvent_t kernel_done,
                      cudaEvent_t d2h_done,
                      std::chrono::steady_clock::time_point total_start)
  {
    float h2d_ms = 0.0F;
    float kernel_ms = 0.0F;
    float d2h_ms = 0.0F;
    check(cudaEventElapsedTime(&h2d_ms, h2d_start, h2d_done), "cudaEventElapsedTime H2D");
    check(cudaEventElapsedTime(&kernel_ms, h2d_done, kernel_done), "cudaEventElapsedTime kernel");
    check(cudaEventElapsedTime(&d2h_ms, kernel_done, d2h_done), "cudaEventElapsedTime D2H");
    const auto total_elapsed = std::chrono::steady_clock::now() - total_start;
    // process_into()/process_superposition() can run concurrently for distinct
    // links/nodes; guard the shared last-timings snapshot.
    std::lock_guard<std::mutex> lock(timings_mutex_);
    last_timings_.h2d_us = static_cast<double>(h2d_ms) * 1000.0;
    last_timings_.kernel_us = static_cast<double>(kernel_ms) * 1000.0;
    last_timings_.d2h_us = static_cast<double>(d2h_ms) * 1000.0;
    last_timings_.gpu_process_us =
        static_cast<double>(std::chrono::duration_cast<std::chrono::nanoseconds>(total_elapsed).count()) / 1000.0;
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
        case ModelStepType::Gain: {
          const float factor = static_cast<float>(std::pow(10.0, param_or(step, "gain_db", 0.0) / 20.0));
          gpu_step = make_step(Scale, factor, 0.0F);
          running_power *= static_cast<double>(factor) * factor;
          break;
        }
        case ModelStepType::PathLoss: {
          const float factor = static_cast<float>(std::pow(10.0, -param_or(step, "path_loss_db", 0.0) / 20.0));
          gpu_step = make_step(Scale, factor, 0.0F);
          running_power *= static_cast<double>(factor) * factor;
          break;
        }
        case ModelStepType::Phase:
        case ModelStepType::Cfo: {
          const double fixed_phase = param_or(step, "phase_rad", 0.0);
          const double cfo_hz = param_or(step, "cfo_hz", 0.0);
          const double phase_increment =
              sample_rate_hz == 0 ? 0.0 : 2.0 * kPi * cfo_hz / static_cast<double>(sample_rate_hz);
          gpu_step = make_step(Rotate, static_cast<float>(fixed_phase + ms.phase_rad[step_index]),
                               static_cast<float>(phase_increment));
          ms.phase_rad[step_index] = std::fmod(ms.phase_rad[step_index] + phase_increment * sample_count, 2.0 * kPi);
          break;
        }
        case ModelStepType::Awgn: {
          double noise_power = param_or(step, "noise_power", -1.0);
          if (noise_power < 0.0) {
            const double snr_db = param_or(step, "snr_db", 60.0);
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
        case ModelStepType::IntegerDelay:
        case ModelStepType::FractionalDelay:
          // apply_leading_delay() already delayed the staged input host-side;
          // on the device the delay is a no-op pass-through.
          gpu_step = make_step(Scale, 1.0F, 0.0F);
          break;
      }
    }
  }

  int device_ = 0;
  mutable std::mutex timings_mutex_;
  ProcessorTimings last_timings_;
  std::unordered_map<std::string, CudaLinkState> states_;
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
