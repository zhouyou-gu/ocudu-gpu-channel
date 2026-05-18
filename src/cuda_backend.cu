#include "ocudu_gpu_channel/cuda_backend.h"
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
  output[idx] = {i, q};
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

struct CudaLinkState {
  std::size_t capacity = 0;
  std::size_t step_capacity = 0;
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
  std::vector<GpuStep> host_steps;
  std::vector<double> phase_rad;
  std::vector<unsigned int> noise_seed;        // per-step AWGN RNG seed (set in prepare)
  std::vector<unsigned long long> noise_counter; // per-step AWGN sample counter (advances each call)
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
  }

  void prepare(const TopologyConfig& config) override
  {
    check(cudaSetDevice(config.runtime.gpu_device), "cudaSetDevice");
    device_ = config.runtime.gpu_device;

    for (const auto& link : config.links) {
      const auto* destination = find_device(config, link.to);
      const auto* model = find_model(config, link.model);
      if (destination == nullptr || model == nullptr) {
        continue;
      }
      const std::size_t samples = resolve_batch_samples(config.runtime, destination->sample_rate_hz);
      const std::size_t steps = model->chain.size();
      auto& state = states_[link_key(link)];
      free_state(state);
      state.capacity = samples;
      state.step_capacity = steps;
      state.host_steps.resize(steps);
      state.phase_rad.assign(steps, 0.0);
      state.noise_counter.assign(steps, 0ULL);
      state.noise_seed.assign(steps, 0U);
      for (std::size_t s = 0; s != steps; ++s) {
        // Stable per-step seed so each AWGN step has an independent,
        // run-to-run reproducible noise stream.
        state.noise_seed[s] =
            static_cast<unsigned int>(std::hash<std::string>{}(link_key(link) + ":awgn:" + std::to_string(s)));
      }

      const std::size_t sample_bytes = samples * sizeof(IqSample);
      check(cudaHostAlloc(reinterpret_cast<void**>(&state.host_input), sample_bytes, cudaHostAllocDefault),
            "cudaHostAlloc input");
      check(cudaHostAlloc(reinterpret_cast<void**>(&state.host_output), sample_bytes, cudaHostAllocDefault),
            "cudaHostAlloc output");
      check(cudaMalloc(reinterpret_cast<void**>(&state.device_input), sample_bytes), "cudaMalloc input");
      check(cudaMalloc(reinterpret_cast<void**>(&state.device_output), sample_bytes), "cudaMalloc output");
      check(cudaMalloc(reinterpret_cast<void**>(&state.device_steps), std::max<std::size_t>(1, steps) * sizeof(GpuStep)),
            "cudaMalloc steps");
      check(cudaStreamCreateWithFlags(&state.stream, cudaStreamNonBlocking), "cudaStreamCreateWithFlags");
      check(cudaEventCreate(&state.h2d_start), "cudaEventCreate h2d_start");
      check(cudaEventCreate(&state.h2d_done), "cudaEventCreate h2d_done");
      check(cudaEventCreate(&state.kernel_done), "cudaEventCreate kernel_done");
      check(cudaEventCreate(&state.d2h_done), "cudaEventCreate d2h_done");
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
    if (model.chain.size() > state.step_capacity) {
      throw std::runtime_error("CUDA model chain exceeds preallocated step capacity");
    }

    const auto total_start = std::chrono::steady_clock::now();
    std::copy(input.begin(), input.end(), state.host_input);
    build_steps(state, model, input.size(), sample_rate_hz);

    const std::size_t sample_bytes = input.size() * sizeof(IqSample);
    const std::size_t step_bytes = std::max<std::size_t>(1, model.chain.size()) * sizeof(GpuStep);
    check(cudaEventRecord(state.h2d_start, state.stream), "cudaEventRecord h2d_start");
    check(cudaMemcpyAsync(state.device_steps, state.host_steps.data(), step_bytes, cudaMemcpyHostToDevice, state.stream),
          "cudaMemcpyAsync steps H2D");
    check(cudaMemcpyAsync(state.device_input, state.host_input, sample_bytes, cudaMemcpyHostToDevice, state.stream),
          "cudaMemcpyAsync samples H2D");
    check(cudaEventRecord(state.h2d_done, state.stream), "cudaEventRecord h2d_done");

    constexpr int block_size = 256;
    const int grid_size = static_cast<int>((input.size() + block_size - 1) / block_size);
    apply_steps_kernel<<<grid_size, block_size, 0, state.stream>>>(
        state.device_input, state.device_output, input.size(), state.device_steps, static_cast<int>(model.chain.size()));
    check(cudaGetLastError(), "apply_steps_kernel launch");
    check(cudaEventRecord(state.kernel_done, state.stream), "cudaEventRecord kernel_done");

    check(cudaMemcpyAsync(state.host_output, state.device_output, sample_bytes, cudaMemcpyDeviceToHost, state.stream),
          "cudaMemcpyAsync samples D2H");
    check(cudaEventRecord(state.d2h_done, state.stream), "cudaEventRecord d2h_done");
    check(cudaStreamSynchronize(state.stream), "cudaStreamSynchronize");
    std::copy(state.host_output, state.host_output + input.size(), output.begin());

    float h2d_ms = 0.0F;
    float kernel_ms = 0.0F;
    float d2h_ms = 0.0F;
    check(cudaEventElapsedTime(&h2d_ms, state.h2d_start, state.h2d_done), "cudaEventElapsedTime H2D");
    check(cudaEventElapsedTime(&kernel_ms, state.h2d_done, state.kernel_done), "cudaEventElapsedTime kernel");
    check(cudaEventElapsedTime(&d2h_ms, state.kernel_done, state.d2h_done), "cudaEventElapsedTime D2H");
    const auto total_elapsed = std::chrono::steady_clock::now() - total_start;
    // process_into() can run concurrently for distinct links (one broker server
    // thread per destination device); guard the shared last-timings snapshot.
    {
      std::lock_guard<std::mutex> lock(timings_mutex_);
      last_timings_.h2d_us = static_cast<double>(h2d_ms) * 1000.0;
      last_timings_.kernel_us = static_cast<double>(kernel_ms) * 1000.0;
      last_timings_.d2h_us = static_cast<double>(d2h_ms) * 1000.0;
      last_timings_.gpu_process_us =
          static_cast<double>(std::chrono::duration_cast<std::chrono::nanoseconds>(total_elapsed).count()) / 1000.0;
    }
  }

  ProcessorTimings last_timings() const override
  {
    std::lock_guard<std::mutex> lock(timings_mutex_);
    return last_timings_;
  }
  const char* backend_name() const override { return "cuda"; }

private:
  void build_steps(CudaLinkState& state,
                   const ModelConfig& model,
                   std::size_t sample_count,
                   std::uint64_t sample_rate_hz)
  {
    // Average IQ power is tracked analytically through the chain so an snr_db
    // AWGN step can size its noise without a device-side reduction. Every
    // GPU-supported step has a closed-form power transfer (Scale: x factor^2,
    // Rotate: unchanged, AddNoise: + noise_power).
    double running_power = 0.0;
    const bool has_awgn = std::any_of(model.chain.begin(), model.chain.end(),
                                      [](const ModelStep& s) { return s.type == ModelStepType::Awgn; });
    if (has_awgn && sample_count != 0) {
      double sum = 0.0;
      for (std::size_t n = 0; n != sample_count; ++n) {
        const double si = state.host_input[n].i;
        const double sq = state.host_input[n].q;
        sum += si * si + sq * sq;
      }
      running_power = sum / static_cast<double>(sample_count);
    }

    for (std::size_t step_index = 0; step_index != model.chain.size(); ++step_index) {
      const auto& step = model.chain[step_index];
      auto& gpu_step = state.host_steps[step_index];
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
          gpu_step = make_step(Rotate, static_cast<float>(fixed_phase + state.phase_rad[step_index]),
                               static_cast<float>(phase_increment));
          state.phase_rad[step_index] =
              std::fmod(state.phase_rad[step_index] + phase_increment * sample_count, 2.0 * kPi);
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
          noise_step.seed = state.noise_seed[step_index];
          noise_step.counter = state.noise_counter[step_index];
          gpu_step = noise_step;
          state.noise_counter[step_index] += sample_count;
          running_power += noise_power;
          break;
        }
        case ModelStepType::IntegerDelay:
        case ModelStepType::FractionalDelay:
          throw std::runtime_error("unsupported CUDA model step reached execution: " + to_string(step.type));
      }
    }
  }

  int device_ = 0;
  mutable std::mutex timings_mutex_;
  ProcessorTimings last_timings_;
  std::unordered_map<std::string, CudaLinkState> states_;
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
