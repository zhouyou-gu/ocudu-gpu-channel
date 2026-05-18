#include "ocudu_gpu_channel/backend.h"
#include "ocudu_gpu_channel/config.h"
#include "ocudu_gpu_channel/cpu_backend.h"
#include "ocudu_gpu_channel/processing.h"
#include <cmath>
#include <cstdlib>
#include <iostream>
#include <unordered_map>

namespace {

void require(bool condition, const char* message)
{
  if (!condition) {
    std::cerr << "FAIL: " << message << "\n";
    std::exit(1);
  }
}

bool near(float lhs, float rhs)
{
  return std::fabs(lhs - rhs) < 1e-4F;
}

void require_near_buffer(const ocg::IqBuffer& lhs, const ocg::IqBuffer& rhs, const char* message)
{
  require(lhs.size() == rhs.size(), message);
  for (std::size_t i = 0; i != lhs.size(); ++i) {
    if (std::fabs(lhs[i].i - rhs[i].i) > 1e-3F || std::fabs(lhs[i].q - rhs[i].q) > 1e-3F) {
      std::cerr << "FAIL: " << message << " at sample " << i << " lhs=(" << lhs[i].i << "," << lhs[i].q
                << ") rhs=(" << rhs[i].i << "," << rhs[i].q << ")\n";
      std::exit(1);
    }
  }
}

} // namespace

int main()
{
  ocg::TopologyConfig config;
  config.runtime.batch_samples_auto = false;
  config.runtime.batch_samples = 4;
  config.devices = {
      {.id = "gnb0", .role = ocg::DeviceRole::Gnb, .sample_rate_hz = 1000, .tx_endpoint = "tx0", .rx_endpoint = "rx0"},
      {.id = "ue0", .role = ocg::DeviceRole::Ue, .sample_rate_hz = 1000, .tx_endpoint = "tx1", .rx_endpoint = "rx1"}};
  config.links = {{.from = "gnb0", .to = "ue0", .model = "gain2"}, {.from = "ue0", .to = "gnb0", .model = "gain2"}};
  ocg::ModelConfig model;
  model.id = "gain2";
  model.chain.push_back({.type = ocg::ModelStepType::Gain, .params = {{"gain_db", 6.020599913}}});
  config.models.emplace(model.id, model);

  std::unordered_map<std::string, ocg::IqBuffer> latest;
  latest["gnb0"] = {{1.0F, 0.0F}, {0.0F, 1.0F}, {1.0F, 1.0F}, {0.0F, 0.0F}};
  latest["ue0"] = {{0.5F, 0.0F}, {0.0F, 0.5F}, {0.5F, 0.5F}, {0.0F, 0.0F}};

  // prepare() + a prepared link key: the path the broker exercises.
  ocg::CpuChannelProcessor processor;
  processor.prepare(config);

  ocg::IqBuffer output(4);
  processor.process_into(ocg::link_key(config.links[0]), model, latest["gnb0"], output, 1000);
  require(near(output[2].i, 2.0F), "process_into gain on I");
  require(near(output[2].q, 2.0F), "process_into gain on Q");

  ocg::ModelConfig delay;
  delay.id = "delay";
  delay.chain.push_back({.type = ocg::ModelStepType::IntegerDelay, .params = {{"delay_samples", 2.0}}});
  auto delayed = processor.process("link-delay", delay, latest["gnb0"], 1000);
  require(near(delayed[0].i, 0.0F), "delay inserts zero sample 0");
  require(near(delayed[1].i, 0.0F), "delay inserts zero sample 1");
  require(near(delayed[2].i, 1.0F), "delay sample 2");
  ocg::IqBuffer more = {{2.0F, 0.0F}, {3.0F, 0.0F}, {4.0F, 0.0F}, {5.0F, 0.0F}};
  auto delayed_more = processor.process("link-delay", delay, more, 1000);
  require(near(delayed_more[0].i, 1.0F), "delay continuity sample 0");
  require(near(delayed_more[1].i, 0.0F), "delay continuity sample 1");

#if OCUDU_GPU_CHANNEL_HAS_CUDA
  if (ocg::cuda_compiled()) {
    ocg::TopologyConfig cuda_config;
    cuda_config.runtime.backend = ocg::Backend::Cuda;
    cuda_config.runtime.batch_samples_auto = false;
    cuda_config.runtime.batch_samples = 8;
    cuda_config.runtime.queue_samples = 64;
    cuda_config.devices = {
        {.id = "gnb0", .role = ocg::DeviceRole::Gnb, .sample_rate_hz = 23040000, .tx_endpoint = "tx0", .rx_endpoint = "rx0"},
        {.id = "ue0", .role = ocg::DeviceRole::Ue, .sample_rate_hz = 23040000, .tx_endpoint = "tx1", .rx_endpoint = "rx1"}};
    cuda_config.links = {{.from = "gnb0", .to = "ue0", .model = "cuda_mvp"}};

    ocg::ModelConfig cuda_model;
    cuda_model.id = "cuda_mvp";
    cuda_model.chain.push_back({.type = ocg::ModelStepType::Gain, .params = {{"gain_db", 3.0}}});
    cuda_model.chain.push_back({.type = ocg::ModelStepType::PathLoss, .params = {{"path_loss_db", 1.5}}});
    cuda_model.chain.push_back({.type = ocg::ModelStepType::Phase, .params = {{"phase_rad", 0.125}}});
    cuda_model.chain.push_back({.type = ocg::ModelStepType::Cfo, .params = {{"cfo_hz", 250.0}}});
    cuda_config.models.emplace(cuda_model.id, cuda_model);

    ocg::CpuChannelProcessor cpu_reference;
    cpu_reference.prepare(cuda_config);
    auto cuda_processor = ocg::create_channel_processor(cuda_config);

    ocg::IqBuffer first_batch = {{0.10F, -0.20F}, {0.25F, 0.30F},  {-0.40F, 0.15F}, {0.50F, -0.35F},
                                 {0.00F, 0.75F},  {-0.60F, -0.10F}, {0.90F, 0.05F},  {-0.15F, 0.45F}};
    ocg::IqBuffer second_batch = {{0.20F, 0.10F},  {-0.30F, 0.40F}, {0.55F, -0.25F}, {-0.70F, 0.05F},
                                  {0.35F, -0.50F}, {0.80F, 0.20F},  {-0.10F, -0.65F}, {0.45F, 0.15F}};
    ocg::IqBuffer cpu_out(first_batch.size());
    ocg::IqBuffer cuda_out(first_batch.size());
    const std::string key = "gnb0>ue0:cuda_mvp";

    cpu_reference.process_into(key, cuda_model, first_batch, cpu_out, 23040000);
    cuda_processor->process_into(key, cuda_model, first_batch, cuda_out, 23040000);
    require_near_buffer(cpu_out, cuda_out, "CUDA first batch should match CPU reference");

    cpu_reference.process_into(key, cuda_model, second_batch, cpu_out, 23040000);
    cuda_processor->process_into(key, cuda_model, second_batch, cuda_out, 23040000);
    require_near_buffer(cpu_out, cuda_out, "CUDA second batch should preserve CFO phase continuity");

    // AWGN is stochastic, so it cannot be compared bit-for-bit. Verify it
    // statistically: a zero input through an explicit-noise-power AWGN step
    // yields pure noise whose mean power equals noise_power and whose mean is 0.
    ocg::TopologyConfig awgn_config;
    awgn_config.runtime.backend = ocg::Backend::Cuda;
    awgn_config.runtime.batch_samples_auto = false;
    awgn_config.runtime.batch_samples = 8192;
    awgn_config.runtime.queue_samples = 65536;
    awgn_config.devices = {
        {.id = "gnb0", .role = ocg::DeviceRole::Gnb, .sample_rate_hz = 23040000, .tx_endpoint = "tx0", .rx_endpoint = "rx0"},
        {.id = "ue0", .role = ocg::DeviceRole::Ue, .sample_rate_hz = 23040000, .tx_endpoint = "tx1", .rx_endpoint = "rx1"}};
    awgn_config.links = {{.from = "gnb0", .to = "ue0", .model = "awgn"},
                         {.from = "ue0", .to = "gnb0", .model = "awgn"}};
    ocg::ModelConfig awgn_model;
    awgn_model.id = "awgn";
    const double target_noise_power = 0.04;
    awgn_model.chain.push_back({.type = ocg::ModelStepType::Awgn, .params = {{"noise_power", target_noise_power}}});
    awgn_config.models.emplace(awgn_model.id, awgn_model);

    auto awgn_processor = ocg::create_channel_processor(awgn_config);
    const ocg::IqBuffer zeros(8192, ocg::IqSample{0.0F, 0.0F});
    ocg::IqBuffer noisy(zeros.size());
    awgn_processor->process_into("gnb0>ue0:awgn", awgn_model, zeros, noisy, 23040000);

    double power_sum = 0.0;
    double i_sum = 0.0;
    double q_sum = 0.0;
    for (const auto& sample : noisy) {
      power_sum += ocg::power(sample);
      i_sum += sample.i;
      q_sum += sample.q;
    }
    const auto n = static_cast<double>(noisy.size());
    require(std::fabs(power_sum / n - target_noise_power) < 0.1 * target_noise_power,
            "CUDA AWGN mean power should match noise_power");
    require(std::fabs(i_sum / n) < 0.02 && std::fabs(q_sum / n) < 0.02, "CUDA AWGN should be zero-mean");

    // Second batch must draw fresh noise, not repeat the first.
    ocg::IqBuffer noisy2(zeros.size());
    awgn_processor->process_into("gnb0>ue0:awgn", awgn_model, zeros, noisy2, 23040000);
    bool differs = false;
    for (std::size_t s = 0; s != noisy.size(); ++s) {
      if (noisy[s].i != noisy2[s].i || noisy[s].q != noisy2[s].q) {
        differs = true;
        break;
      }
    }
    require(differs, "CUDA AWGN should produce a fresh noise stream each batch");
  }
#endif
  return 0;
}
