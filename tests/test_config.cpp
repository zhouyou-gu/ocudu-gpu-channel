#include "ocudu_gpu_channel/config.h"
#include "ocudu_gpu_channel/processing.h"
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <stdexcept>

namespace {

void require(bool condition, const char* message)
{
  if (!condition) {
    std::cerr << "FAIL: " << message << "\n";
    std::exit(1);
  }
}

} // namespace

int main()
{
  const char* path = "test_topology.yaml";
  std::ofstream out(path);
  out << R"yaml(
runtime:
  backend: cpu
  gpu_device: 0
  batch_samples: auto
  queue_samples: 614400
devices:
  - id: gnb0
    role: gnb
    sample_rate_hz: 23040000
    tx_endpoint: tcp://127.0.0.1:2000
    rx_endpoint: tcp://127.0.0.1:2001
  - id: ue0
    role: ue
    sample_rate_hz: 23040000
    tx_endpoint: tcp://127.0.0.1:2101
    rx_endpoint: tcp://127.0.0.1:2100
links:
  - from: gnb0
    to: ue0
    model: clean
  - from: ue0
    to: gnb0
    model: clean
models:
  clean:
    chain:
      - type: gain
        gain_db: 0
)yaml";
  out.close();

  auto config = ocg::load_config_file(path);
  require(config.devices.size() == 2, "expected two devices");
  require(config.links.size() == 2, "expected two links");
  require(config.models.count("clean") == 1, "expected clean model");
  require(ocg::resolve_batch_samples(config.runtime, 23040000) == 23040, "auto batch should resolve to 1 ms");
  require(ocg::validate_config(config).empty(), "config should validate");

  const char* bad_path = "test_bad_topology.yaml";
  std::ofstream bad(bad_path);
  bad << R"yaml(
runtime:
  backend: cpu
devices:
  - id: gnb0
    role: gnb
    sample_rate_hz: 23040000
    tx_endpoint: tcp://127.0.0.1:2000
    rx_endpoint: tcp://127.0.0.1:2001
links:
  - from: gnb0
    to: gnb0
    model: typo
models:
  typo:
    chain:
      - type: gain
        gain_dB: 0
)yaml";
  bad.close();

  bool rejected = false;
  try {
    (void)ocg::load_config_file(bad_path);
  } catch (const std::runtime_error&) {
    rejected = true;
  }
  require(rejected, "unknown model parameter should be rejected");

  const char* bad_number_path = "test_bad_number_topology.yaml";
  std::ofstream bad_number(bad_number_path);
  bad_number << R"yaml(
runtime:
  backend: cpu
  queue_samples: 614400abc
devices:
  - id: gnb0
    role: gnb
    sample_rate_hz: 23040000oops
    tx_endpoint: tcp://127.0.0.1:2000
    rx_endpoint: tcp://127.0.0.1:2001
models:
  clean:
    chain:
      - type: gain
        gain_db: 0x
)yaml";
  bad_number.close();

  rejected = false;
  try {
    (void)ocg::load_config_file(bad_number_path);
  } catch (const std::runtime_error&) {
    rejected = true;
  }
  require(rejected, "numeric values with trailing garbage should be rejected");

  const char* mixed_rate_path = "test_mixed_rate_topology.yaml";
  std::ofstream mixed_rate(mixed_rate_path);
  mixed_rate << R"yaml(
runtime:
  backend: cpu
devices:
  - id: gnb0
    role: gnb
    sample_rate_hz: 23040000
    tx_endpoint: tcp://127.0.0.1:2000
    rx_endpoint: tcp://127.0.0.1:2001
  - id: ue0
    role: ue
    sample_rate_hz: 30720000
    tx_endpoint: tcp://127.0.0.1:2101
    rx_endpoint: tcp://127.0.0.1:2100
links:
  - from: gnb0
    to: ue0
    model: clean
models:
  clean:
    chain:
      - type: gain
        gain_db: 0
)yaml";
  mixed_rate.close();

  rejected = false;
  try {
    (void)ocg::load_config_file(mixed_rate_path);
  } catch (const std::runtime_error&) {
    rejected = true;
  }
  require(rejected, "mixed sample-rate links should be rejected until resampling exists");

  config.runtime.backend = ocg::Backend::Cuda;
  ocg::ModelConfig gpu_awgn;
  gpu_awgn.id = "gpu_awgn";
  gpu_awgn.chain.push_back({.type = ocg::ModelStepType::Awgn, .params = {{"snr_db", 30.0}}});
  ocg::ModelConfig gpu_delay;
  gpu_delay.id = "gpu_delay";
  gpu_delay.chain.push_back({.type = ocg::ModelStepType::IntegerDelay, .params = {{"delay_samples", 4.0}}});
  config.models.clear();
  config.models.emplace(gpu_awgn.id, gpu_awgn);
  config.models.emplace(gpu_delay.id, gpu_delay);
  config.links = {{.from = "gnb0", .to = "ue0", .model = gpu_awgn.id}};
  require(ocg::validate_cuda_support(config).empty(), "CUDA should accept the AWGN model step");
  config.links = {{.from = "gnb0", .to = "ue0", .model = gpu_delay.id}};
  require(!ocg::validate_cuda_support(config).empty(), "CUDA should reject the integer-delay model step");

  std::remove(path);
  std::remove(bad_path);
  std::remove(bad_number_path);
  std::remove(mixed_rate_path);
  return 0;
}
