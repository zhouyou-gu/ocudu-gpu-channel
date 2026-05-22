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
  // A chain-leading sample delay is GPU-supported (applied host-side at staging).
  ocg::ModelConfig gpu_lead_delay;
  gpu_lead_delay.id = "gpu_lead_delay";
  gpu_lead_delay.chain.push_back({.type = ocg::ModelStepType::IntegerDelay, .params = {{"delay_samples", 4.0}}});
  gpu_lead_delay.chain.push_back({.type = ocg::ModelStepType::Gain, .params = {{"gain_db", -3.0}}});
  // A delay that does not lead the chain is not GPU-supported.
  ocg::ModelConfig gpu_mid_delay;
  gpu_mid_delay.id = "gpu_mid_delay";
  gpu_mid_delay.chain.push_back({.type = ocg::ModelStepType::Gain, .params = {{"gain_db", -3.0}}});
  gpu_mid_delay.chain.push_back({.type = ocg::ModelStepType::FractionalDelay, .params = {{"delay_samples", 4.5}}});
  config.models.clear();
  config.models.emplace(gpu_awgn.id, gpu_awgn);
  config.models.emplace(gpu_lead_delay.id, gpu_lead_delay);
  config.models.emplace(gpu_mid_delay.id, gpu_mid_delay);
  config.links = {{.from = "gnb0", .to = "ue0", .model = gpu_awgn.id}};
  require(ocg::validate_cuda_support(config).empty(), "CUDA should accept the AWGN model step");
  config.links = {{.from = "gnb0", .to = "ue0", .model = gpu_lead_delay.id}};
  require(ocg::validate_cuda_support(config).empty(), "CUDA should accept a chain-leading sample delay");
  config.links = {{.from = "gnb0", .to = "ue0", .model = gpu_mid_delay.id}};
  require(!ocg::validate_cuda_support(config).empty(), "CUDA should reject a non-leading sample delay");

  // tx_timing_offset_samples on a source folds into every outgoing link's
  // chain-leading delay.
  const char* tx_offset_path = "test_tx_offset_topology.yaml";
  {
    std::ofstream f(tx_offset_path);
    f << R"yaml(
runtime:
  backend: cpu
devices:
  - id: gnb0
    role: gnb
    sample_rate_hz: 23040000
    tx_endpoint: tcp://127.0.0.1:2000
    rx_endpoint: tcp://127.0.0.1:2001
    tx_timing_offset_samples: 3
  - id: ue0
    role: ue
    sample_rate_hz: 23040000
    tx_endpoint: tcp://127.0.0.1:2101
    rx_endpoint: tcp://127.0.0.1:2100
  - id: ue1
    role: ue
    sample_rate_hz: 23040000
    tx_endpoint: tcp://127.0.0.1:2103
    rx_endpoint: tcp://127.0.0.1:2102
links:
  - from: gnb0
    to: ue0
    model: serving
  - from: gnb0
    to: ue1
    model: with_delay
  - from: ue0
    to: gnb0
    model: serving
  - from: ue1
    to: gnb0
    model: serving
models:
  serving:
    chain:
      - type: gain
        gain_db: 0
  with_delay:
    chain:
      - type: integer_delay
        delay_samples: 5
      - type: gain
        gain_db: -3
)yaml";
  }
  auto tx_offset_config = ocg::load_config_file(tx_offset_path);

  // Link gnb0 -> ue0 had no chain-leading delay; offset 3 was prepended as
  // integer_delay 3.
  const auto* gnb0_ue0 = ocg::find_model(tx_offset_config, tx_offset_config.links[0].model);
  require(gnb0_ue0 != nullptr, "gnb0>ue0 effective model must exist");
  require(gnb0_ue0->chain.size() == 2, "gnb0>ue0 should have a prepended leading delay");
  require(gnb0_ue0->chain.front().type == ocg::ModelStepType::IntegerDelay,
          "gnb0>ue0 prepended step must be integer_delay");
  require(gnb0_ue0->chain.front().params.at("delay_samples") == 3.0,
          "gnb0>ue0 prepended delay must equal the source offset");

  // Link gnb0 -> ue1 already had integer_delay 5; offset 3 was added to it -> 8.
  const auto* gnb0_ue1 = ocg::find_model(tx_offset_config, tx_offset_config.links[1].model);
  require(gnb0_ue1 != nullptr, "gnb0>ue1 effective model must exist");
  require(gnb0_ue1->chain.size() == 2, "gnb0>ue1 chain length unchanged when offset merges");
  require(gnb0_ue1->chain.front().type == ocg::ModelStepType::IntegerDelay,
          "gnb0>ue1 leading step stays integer_delay when both are integer");
  require(gnb0_ue1->chain.front().params.at("delay_samples") == 8.0,
          "gnb0>ue1 leading delay must be sum of existing + offset");

  // Reverse links (ue0->gnb0 and ue1->gnb0) have offset-0 sources; their
  // models must be the original "serving" (no synthesis).
  require(tx_offset_config.links[2].model == "serving",
          "ue0>gnb0 source has no offset; model must be unchanged");
  require(tx_offset_config.links[3].model == "serving",
          "ue1>gnb0 source has no offset; model must be unchanged");

  // Fractional offset: promotes the prepended step to FractionalDelay.
  const char* tx_frac_path = "test_tx_frac_offset_topology.yaml";
  {
    std::ofstream f(tx_frac_path);
    f << R"yaml(
runtime:
  backend: cpu
devices:
  - id: gnb0
    role: gnb
    sample_rate_hz: 23040000
    tx_endpoint: tcp://127.0.0.1:2000
    rx_endpoint: tcp://127.0.0.1:2001
    tx_timing_offset_samples: 2.5
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
  }
  auto tx_frac_config = ocg::load_config_file(tx_frac_path);
  const auto* frac_model = ocg::find_model(tx_frac_config, tx_frac_config.links[0].model);
  require(frac_model != nullptr, "fractional-offset effective model must exist");
  require(frac_model->chain.front().type == ocg::ModelStepType::FractionalDelay,
          "fractional offset should produce a FractionalDelay leading step");
  require(frac_model->chain.front().params.at("delay_samples") == 2.5,
          "fractional offset delay_samples preserved");

  // Link-level propagation_delay_samples must also fold into the chain-leading
  // delay, summing with any tx_timing_offset on the source.
  const char* prop_path = "test_propagation_delay_topology.yaml";
  {
    std::ofstream f(prop_path);
    f << R"yaml(
runtime:
  backend: cpu
devices:
  - id: gnb0
    role: gnb
    sample_rate_hz: 23040000
    tx_endpoint: tcp://127.0.0.1:2000
    rx_endpoint: tcp://127.0.0.1:2001
    tx_timing_offset_samples: 2
  - id: ue0
    role: ue
    sample_rate_hz: 23040000
    tx_endpoint: tcp://127.0.0.1:2101
    rx_endpoint: tcp://127.0.0.1:2100
  - id: ue1
    role: ue
    sample_rate_hz: 23040000
    tx_endpoint: tcp://127.0.0.1:2103
    rx_endpoint: tcp://127.0.0.1:2102
links:
  - from: gnb0
    to: ue0
    model: clean
    propagation_delay_samples: 4
  - from: gnb0
    to: ue1
    model: clean
    propagation_delay_samples: 1.5
  - from: ue0
    to: gnb0
    model: clean
  - from: ue1
    to: gnb0
    model: clean
models:
  clean:
    chain:
      - type: gain
        gain_db: 0
)yaml";
  }
  auto prop_config = ocg::load_config_file(prop_path);
  // gnb0->ue0: tx_timing_offset 2 + propagation_delay 4 = 6 (integer).
  const auto* prop_ue0 = ocg::find_model(prop_config, prop_config.links[0].model);
  require(prop_ue0 != nullptr, "gnb0>ue0 effective model must exist");
  require(prop_ue0->chain.front().type == ocg::ModelStepType::IntegerDelay,
          "gnb0>ue0 leading step must be integer_delay for an integer total");
  require(prop_ue0->chain.front().params.at("delay_samples") == 6.0,
          "gnb0>ue0 leading delay must be tx_timing_offset + propagation_delay");
  // gnb0->ue1: tx_timing_offset 2 + propagation_delay 1.5 = 3.5 (fractional).
  const auto* prop_ue1 = ocg::find_model(prop_config, prop_config.links[1].model);
  require(prop_ue1 != nullptr, "gnb0>ue1 effective model must exist");
  require(prop_ue1->chain.front().type == ocg::ModelStepType::FractionalDelay,
          "gnb0>ue1 leading step must be fractional_delay for fractional total");
  require(prop_ue1->chain.front().params.at("delay_samples") == 3.5,
          "gnb0>ue1 leading delay must compose to 3.5");
  // The two synthesized clones must be DIFFERENT models even though both
  // sources are gnb0 with the same base "clean" — propagation_delay differs.
  require(prop_config.links[0].model != prop_config.links[1].model,
          "different propagation_delay on same source must yield distinct effective models");

  // Link-only propagation_delay (source has no tx_timing_offset) still folds.
  const char* link_only_path = "test_propagation_only_topology.yaml";
  {
    std::ofstream f(link_only_path);
    f << R"yaml(
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
    sample_rate_hz: 23040000
    tx_endpoint: tcp://127.0.0.1:2101
    rx_endpoint: tcp://127.0.0.1:2100
links:
  - from: gnb0
    to: ue0
    model: clean
    propagation_delay_samples: 7
  - from: ue0
    to: gnb0
    model: clean
models:
  clean:
    chain:
      - type: gain
        gain_db: 0
)yaml";
  }
  auto link_only = ocg::load_config_file(link_only_path);
  const auto* link_only_model = ocg::find_model(link_only, link_only.links[0].model);
  require(link_only_model != nullptr, "link-only effective model must exist");
  require(link_only_model->chain.front().type == ocg::ModelStepType::IntegerDelay,
          "link-only integer propagation_delay must produce integer_delay leading step");
  require(link_only_model->chain.front().params.at("delay_samples") == 7.0,
          "link-only propagation_delay should pass through unchanged");

  // Negative offset is rejected.
  const char* neg_path = "test_tx_neg_offset_topology.yaml";
  {
    std::ofstream f(neg_path);
    f << R"yaml(
runtime:
  backend: cpu
devices:
  - id: gnb0
    role: gnb
    sample_rate_hz: 23040000
    tx_endpoint: tcp://127.0.0.1:2000
    rx_endpoint: tcp://127.0.0.1:2001
    tx_timing_offset_samples: -1
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
  }
  rejected = false;
  try {
    (void)ocg::load_config_file(neg_path);
  } catch (const std::runtime_error&) {
    rejected = true;
  }
  require(rejected, "negative tx_timing_offset_samples must be rejected");

  // Negative link propagation_delay also rejected.
  const char* neg_prop_path = "test_neg_prop_topology.yaml";
  {
    std::ofstream f(neg_prop_path);
    f << R"yaml(
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
    sample_rate_hz: 23040000
    tx_endpoint: tcp://127.0.0.1:2101
    rx_endpoint: tcp://127.0.0.1:2100
links:
  - from: gnb0
    to: ue0
    model: clean
    propagation_delay_samples: -2
  - from: ue0
    to: gnb0
    model: clean
models:
  clean:
    chain:
      - type: gain
        gain_db: 0
)yaml";
  }
  rejected = false;
  try {
    (void)ocg::load_config_file(neg_prop_path);
  } catch (const std::runtime_error&) {
    rejected = true;
  }
  require(rejected, "negative propagation_delay_samples must be rejected");

  // ---- tdl: schema-level tests ----
  // A 3-tap tdl with mixed integer + fractional delays parses cleanly and the
  // tap data round-trips into the TopologyConfig.
  const char* tdl_path = "test_tdl_topology.yaml";
  {
    std::ofstream f(tdl_path);
    f << R"yaml(
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
    sample_rate_hz: 23040000
    tx_endpoint: tcp://127.0.0.1:2101
    rx_endpoint: tcp://127.0.0.1:2100
links:
  - from: gnb0
    to: ue0
    model: multipath
  - from: ue0
    to: gnb0
    model: multipath
models:
  multipath:
    chain:
      - type: tdl
        taps:
          - delay_samples: 0
            gain_db: 0
          - delay_samples: 2.25
            gain_db: -3
            phase_rad: 1.5708
          - delay_samples: 7
            gain_db: -6
)yaml";
  }
  auto tdl_config = ocg::load_config_file(tdl_path);
  require(ocg::validate_config(tdl_config).empty(), "tdl config should validate");
  const auto* tdl_model = ocg::find_model(tdl_config, "multipath");
  require(tdl_model != nullptr, "tdl model must exist");
  require(tdl_model->chain.size() == 1, "tdl model should have exactly one chain step");
  const auto& tdl_step = tdl_model->chain.front();
  require(tdl_step.type == ocg::ModelStepType::Tdl, "tdl step type must round-trip");
  require(tdl_step.taps.size() == 3, "tdl step must have 3 taps");
  require(tdl_step.taps[0].delay_samples == 0.0, "tap 0 delay = 0");
  require(tdl_step.taps[0].gain_db == 0.0, "tap 0 gain_db = 0");
  require(tdl_step.taps[0].phase_rad == 0.0, "tap 0 phase defaults to 0");
  require(tdl_step.taps[1].delay_samples == 2.25, "tap 1 fractional delay preserved");
  require(tdl_step.taps[1].gain_db == -3.0, "tap 1 gain_db preserved");
  require(tdl_step.taps[1].phase_rad == 1.5708, "tap 1 phase preserved");
  require(tdl_step.taps[2].delay_samples == 7.0, "tap 2 integer delay preserved");
  require(tdl_step.taps[2].gain_db == -6.0, "tap 2 gain_db preserved");

  // Empty tap list is rejected by the validator.
  const char* tdl_empty_path = "test_tdl_empty_topology.yaml";
  {
    std::ofstream f(tdl_empty_path);
    f << R"yaml(
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
    sample_rate_hz: 23040000
    tx_endpoint: tcp://127.0.0.1:2101
    rx_endpoint: tcp://127.0.0.1:2100
links:
  - from: gnb0
    to: ue0
    model: empty_tdl
  - from: ue0
    to: gnb0
    model: empty_tdl
models:
  empty_tdl:
    chain:
      - type: tdl
)yaml";
  }
  rejected = false;
  try {
    (void)ocg::load_config_file(tdl_empty_path);
  } catch (const std::runtime_error&) {
    rejected = true;
  }
  require(rejected, "tdl step with no taps must be rejected");

  // Negative tap delay is rejected.
  const char* tdl_neg_path = "test_tdl_neg_topology.yaml";
  {
    std::ofstream f(tdl_neg_path);
    f << R"yaml(
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
    sample_rate_hz: 23040000
    tx_endpoint: tcp://127.0.0.1:2101
    rx_endpoint: tcp://127.0.0.1:2100
links:
  - from: gnb0
    to: ue0
    model: neg_tap
  - from: ue0
    to: gnb0
    model: neg_tap
models:
  neg_tap:
    chain:
      - type: tdl
        taps:
          - delay_samples: -1.0
            gain_db: 0
)yaml";
  }
  rejected = false;
  try {
    (void)ocg::load_config_file(tdl_neg_path);
  } catch (const std::runtime_error&) {
    rejected = true;
  }
  require(rejected, "tdl tap with negative delay_samples must be rejected");

  // Duplicate tap delays are rejected so the processor never has to break ties.
  const char* tdl_dup_path = "test_tdl_dup_topology.yaml";
  {
    std::ofstream f(tdl_dup_path);
    f << R"yaml(
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
    sample_rate_hz: 23040000
    tx_endpoint: tcp://127.0.0.1:2101
    rx_endpoint: tcp://127.0.0.1:2100
links:
  - from: gnb0
    to: ue0
    model: dup_tap
  - from: ue0
    to: gnb0
    model: dup_tap
models:
  dup_tap:
    chain:
      - type: tdl
        taps:
          - delay_samples: 2
            gain_db: 0
          - delay_samples: 2
            gain_db: -3
)yaml";
  }
  rejected = false;
  try {
    (void)ocg::load_config_file(tdl_dup_path);
  } catch (const std::runtime_error&) {
    rejected = true;
  }
  require(rejected, "tdl with duplicate tap delay_samples must be rejected");

  // taps on a non-tdl step is rejected: only tdl carries a tap list.
  const char* taps_on_gain_path = "test_taps_on_gain_topology.yaml";
  {
    std::ofstream f(taps_on_gain_path);
    f << R"yaml(
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
    sample_rate_hz: 23040000
    tx_endpoint: tcp://127.0.0.1:2101
    rx_endpoint: tcp://127.0.0.1:2100
links:
  - from: gnb0
    to: ue0
    model: misplaced_taps
  - from: ue0
    to: gnb0
    model: misplaced_taps
models:
  misplaced_taps:
    chain:
      - type: gain
        gain_db: 0
        taps:
          - delay_samples: 0
            gain_db: 0
)yaml";
  }
  rejected = false;
  try {
    (void)ocg::load_config_file(taps_on_gain_path);
  } catch (const std::runtime_error&) {
    rejected = true;
  }
  require(rejected, "non-tdl step with a taps list must be rejected");

  // Single-tap tdl is valid: the smallest meaningful tdl config still passes.
  const char* tdl_single_path = "test_tdl_single_topology.yaml";
  {
    std::ofstream f(tdl_single_path);
    f << R"yaml(
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
    sample_rate_hz: 23040000
    tx_endpoint: tcp://127.0.0.1:2101
    rx_endpoint: tcp://127.0.0.1:2100
links:
  - from: gnb0
    to: ue0
    model: single_tap
  - from: ue0
    to: gnb0
    model: single_tap
models:
  single_tap:
    chain:
      - type: tdl
        taps:
          - delay_samples: 3.5
            gain_db: -2.0
            phase_rad: 0.25
)yaml";
  }
  auto tdl_single_config = ocg::load_config_file(tdl_single_path);
  const auto* single_model = ocg::find_model(tdl_single_config, "single_tap");
  require(single_model != nullptr && single_model->chain.size() == 1, "single-tap tdl model loads");
  require(single_model->chain.front().taps.size() == 1, "single-tap tdl has exactly one tap");
  require(single_model->chain.front().taps.front().delay_samples == 3.5,
          "single-tap tdl preserves fractional delay");

  // tdl composed with another chain step: e.g., `tdl` then `awgn`. The parser
  // must close the tap list when a new `- ` step appears at indent 6.
  const char* tdl_mixed_path = "test_tdl_mixed_topology.yaml";
  {
    std::ofstream f(tdl_mixed_path);
    f << R"yaml(
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
    sample_rate_hz: 23040000
    tx_endpoint: tcp://127.0.0.1:2101
    rx_endpoint: tcp://127.0.0.1:2100
links:
  - from: gnb0
    to: ue0
    model: mixed
  - from: ue0
    to: gnb0
    model: mixed
models:
  mixed:
    chain:
      - type: tdl
        taps:
          - delay_samples: 0
            gain_db: 0
          - delay_samples: 2
            gain_db: -3
      - type: awgn
        snr_db: 30
      - type: cfo
        cfo_hz: 250
)yaml";
  }
  auto tdl_mixed_config = ocg::load_config_file(tdl_mixed_path);
  const auto* mixed_model = ocg::find_model(tdl_mixed_config, "mixed");
  require(mixed_model != nullptr, "mixed-chain model loads");
  require(mixed_model->chain.size() == 3, "mixed-chain has 3 steps: tdl + awgn + cfo");
  require(mixed_model->chain[0].type == ocg::ModelStepType::Tdl, "step 0 is tdl");
  require(mixed_model->chain[0].taps.size() == 2, "tdl step has 2 taps");
  require(mixed_model->chain[1].type == ocg::ModelStepType::Awgn, "step 1 is awgn (tap list closed)");
  require(mixed_model->chain[1].taps.empty(), "awgn step has no taps");
  require(mixed_model->chain[2].type == ocg::ModelStepType::Cfo, "step 2 is cfo");
  require(mixed_model->chain[2].params.at("cfo_hz") == 250.0, "cfo param flows after tdl");

  // Tap order is preserved as written, NOT sorted. Future fading sub-config
  // will key Philox seeds on tap index, so a parser that silently sorts would
  // change RNG output.
  const char* tdl_order_path = "test_tdl_order_topology.yaml";
  {
    std::ofstream f(tdl_order_path);
    f << R"yaml(
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
    sample_rate_hz: 23040000
    tx_endpoint: tcp://127.0.0.1:2101
    rx_endpoint: tcp://127.0.0.1:2100
links:
  - from: gnb0
    to: ue0
    model: unsorted
  - from: ue0
    to: gnb0
    model: unsorted
models:
  unsorted:
    chain:
      - type: tdl
        taps:
          - delay_samples: 7
            gain_db: -6
          - delay_samples: 0
            gain_db: 0
          - delay_samples: 3
            gain_db: -3
)yaml";
  }
  auto tdl_order_config = ocg::load_config_file(tdl_order_path);
  const auto& order_taps = ocg::find_model(tdl_order_config, "unsorted")->chain.front().taps;
  require(order_taps.size() == 3, "unsorted tdl loads three taps");
  require(order_taps[0].delay_samples == 7.0, "tap 0 keeps YAML position even with delay 7");
  require(order_taps[1].delay_samples == 0.0, "tap 1 keeps YAML position with delay 0");
  require(order_taps[2].delay_samples == 3.0, "tap 2 keeps YAML position with delay 3");

  // taps: with no items on a non-tdl step is rejected by the validator via
  // ModelStep::taps_declared -- the parser cannot reject it directly because
  // YAML key order can place `taps:` before `type:`.
  const char* empty_taps_on_gain_path = "test_empty_taps_on_gain_topology.yaml";
  {
    std::ofstream f(empty_taps_on_gain_path);
    f << R"yaml(
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
    sample_rate_hz: 23040000
    tx_endpoint: tcp://127.0.0.1:2101
    rx_endpoint: tcp://127.0.0.1:2100
links:
  - from: gnb0
    to: ue0
    model: phantom_taps
  - from: ue0
    to: gnb0
    model: phantom_taps
models:
  phantom_taps:
    chain:
      - type: gain
        gain_db: 0
        taps:
      - type: awgn
        snr_db: 20
)yaml";
  }
  rejected = false;
  try {
    (void)ocg::load_config_file(empty_taps_on_gain_path);
  } catch (const std::runtime_error&) {
    rejected = true;
  }
  require(rejected, "empty taps block on a non-tdl step must be rejected");

  // tdl with more taps than the validator cap is rejected. Cap is 64;
  // generate 65 taps with distinct delays (1..65).
  const char* tdl_too_many_taps_path = "test_tdl_too_many_taps_topology.yaml";
  {
    std::ofstream f(tdl_too_many_taps_path);
    f << R"yaml(
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
    sample_rate_hz: 23040000
    tx_endpoint: tcp://127.0.0.1:2101
    rx_endpoint: tcp://127.0.0.1:2100
links:
  - from: gnb0
    to: ue0
    model: huge_tdl
  - from: ue0
    to: gnb0
    model: huge_tdl
models:
  huge_tdl:
    chain:
      - type: tdl
        taps:
)yaml";
    for (int k = 1; k <= 65; ++k) {
      f << "          - delay_samples: " << k << "\n";
      f << "            gain_db: 0\n";
    }
  }
  rejected = false;
  try {
    (void)ocg::load_config_file(tdl_too_many_taps_path);
  } catch (const std::runtime_error&) {
    rejected = true;
  }
  require(rejected, "tdl with more than 64 taps must be rejected");

  // tdl with a tap delay above the 1e6 cap is rejected.
  const char* tdl_huge_delay_path = "test_tdl_huge_delay_topology.yaml";
  {
    std::ofstream f(tdl_huge_delay_path);
    f << R"yaml(
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
    sample_rate_hz: 23040000
    tx_endpoint: tcp://127.0.0.1:2101
    rx_endpoint: tcp://127.0.0.1:2100
links:
  - from: gnb0
    to: ue0
    model: huge_delay_tdl
  - from: ue0
    to: gnb0
    model: huge_delay_tdl
models:
  huge_delay_tdl:
    chain:
      - type: tdl
        taps:
          - delay_samples: 2000000
            gain_db: 0
)yaml";
  }
  rejected = false;
  try {
    (void)ocg::load_config_file(tdl_huge_delay_path);
  } catch (const std::runtime_error&) {
    rejected = true;
  }
  require(rejected, "tdl tap with delay above 1e6 samples must be rejected");

  // Phase 1.1 landed the CPU `tdl` kernel; validate_cpu_support now accepts
  // it. Use a programmatic Tdl-only topology and assert the CPU validator
  // returns no errors. (The symmetric CUDA validator still rejects tdl in
  // non-chain-leading positions; the chain-leading-only case is exercised
  // implicitly by Phase 1.2's CUDA tests below.)
  {
    ocg::TopologyConfig cpu_tdl_config;
    cpu_tdl_config.runtime.backend = ocg::Backend::Cpu;
    cpu_tdl_config.runtime.queue_samples = 614400;
    ocg::DeviceConfig src;
    src.id = "gnb0";
    src.sample_rate_hz = 23040000;
    src.tx_endpoint = "tcp://127.0.0.1:2000";
    src.rx_endpoint = "tcp://127.0.0.1:2001";
    ocg::DeviceConfig dst;
    dst.id = "ue0";
    dst.sample_rate_hz = 23040000;
    dst.tx_endpoint = "tcp://127.0.0.1:2101";
    dst.rx_endpoint = "tcp://127.0.0.1:2100";
    cpu_tdl_config.devices = {src, dst};
    cpu_tdl_config.links = {{.from = "gnb0", .to = "ue0", .model = "tdl_only"},
                            {.from = "ue0", .to = "gnb0", .model = "tdl_only"}};
    ocg::ModelConfig tdl_only;
    tdl_only.id = "tdl_only";
    tdl_only.chain.push_back({.type = ocg::ModelStepType::Tdl,
                              .params = {},
                              .taps = {{0.0, 0.0, 0.0}},
                              .taps_declared = true});
    cpu_tdl_config.models.emplace("tdl_only", std::move(tdl_only));
    const auto cpu_errors = ocg::validate_cpu_support(cpu_tdl_config);
    require(cpu_errors.empty(),
            "CPU validator must accept tdl after Phase 1.1 landed the kernel");

    // CUDA validator should also accept a chain-leading tdl (the Phase 1.2
    // staging path handles it). Mid-chain tdl is still rejected (no test
    // here because Phase 1.0's chain-step validator already gates on tdl
    // schema; the CUDA position constraint is tested via the leading-only
    // path's success case).
    const auto cuda_errors = ocg::validate_cuda_support(cpu_tdl_config);
    require(cuda_errors.empty(),
            "CUDA validator must accept chain-leading tdl after Phase 1.2");
  }

  // A flat scalar parameter on tdl is rejected -- tdl has no scalar params today.
  const char* tdl_flat_param_path = "test_tdl_flat_param_topology.yaml";
  {
    std::ofstream f(tdl_flat_param_path);
    f << R"yaml(
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
    sample_rate_hz: 23040000
    tx_endpoint: tcp://127.0.0.1:2101
    rx_endpoint: tcp://127.0.0.1:2100
links:
  - from: gnb0
    to: ue0
    model: tdl_with_flat
  - from: ue0
    to: gnb0
    model: tdl_with_flat
models:
  tdl_with_flat:
    chain:
      - type: tdl
        gain_db: -3
        taps:
          - delay_samples: 0
            gain_db: 0
)yaml";
  }
  rejected = false;
  try {
    (void)ocg::load_config_file(tdl_flat_param_path);
  } catch (const std::runtime_error&) {
    rejected = true;
  }
  require(rejected, "flat scalar params on a tdl step must be rejected");

  std::remove(path);
  std::remove(bad_path);
  std::remove(bad_number_path);
  std::remove(mixed_rate_path);
  std::remove(tx_offset_path);
  std::remove(tx_frac_path);
  std::remove(prop_path);
  std::remove(link_only_path);
  std::remove(neg_path);
  std::remove(neg_prop_path);
  std::remove(tdl_path);
  std::remove(tdl_empty_path);
  std::remove(tdl_neg_path);
  std::remove(tdl_dup_path);
  std::remove(taps_on_gain_path);
  std::remove(tdl_flat_param_path);
  std::remove(tdl_single_path);
  std::remove(tdl_mixed_path);
  std::remove(tdl_order_path);
  std::remove(empty_taps_on_gain_path);
  std::remove(tdl_too_many_taps_path);
  std::remove(tdl_huge_delay_path);
  return 0;
}
