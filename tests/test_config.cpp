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
      - type: tdl
        taps:
          - delay_samples: 0.0
            gain_db: 0.0
            phase_rad: 0.0
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
      - type: tdl
        taps:
          - delay_samples: 0.0
            gain_dB: 0
)yaml";
  bad.close();

  bool rejected = false;
  try {
    (void)ocg::load_config_file(bad_path);
  } catch (const std::runtime_error&) {
    rejected = true;
  }
  require(rejected, "unknown tap parameter should be rejected (gain_dB typo)");

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
      - type: tdl
        taps:
          - delay_samples: 0.0
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
      - type: tdl
        taps:
          - delay_samples: 0.0
            gain_db: 0.0
            phase_rad: 0.0
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
  // A chain-leading tdl step (gain + delay combined into a single tap) is
  // GPU-supported (applied host-side at staging by stage_link).
  ocg::ModelConfig gpu_lead_tdl;
  gpu_lead_tdl.id = "gpu_lead_tdl";
  gpu_lead_tdl.chain.push_back({.type = ocg::ModelStepType::Tdl,
                                .params = {},
                                .taps = {{.delay_samples = 4.0, .gain_db = -3.0, .phase_rad = 0.0}},
                                .taps_declared = true});
  // A tdl mid-chain (after a per-sample step) is not GPU-supported -- the
  // CUDA backend can only run a leading-propagation step at chain[0].
  ocg::ModelConfig gpu_mid_tdl;
  gpu_mid_tdl.id = "gpu_mid_tdl";
  gpu_mid_tdl.chain.push_back({.type = ocg::ModelStepType::Awgn, .params = {{"snr_db", 30.0}}});
  gpu_mid_tdl.chain.push_back({.type = ocg::ModelStepType::Tdl,
                               .params = {},
                               .taps = {{.delay_samples = 4.5, .gain_db = 0.0, .phase_rad = 0.0}},
                               .taps_declared = true});
  config.models.clear();
  config.models.emplace(gpu_awgn.id, gpu_awgn);
  config.models.emplace(gpu_lead_tdl.id, gpu_lead_tdl);
  config.models.emplace(gpu_mid_tdl.id, gpu_mid_tdl);
  config.links = {{.from = "gnb0", .to = "ue0", .model = gpu_awgn.id}};
  require(ocg::validate_cuda_support(config).empty(), "CUDA should accept the AWGN model step");
  config.links = {{.from = "gnb0", .to = "ue0", .model = gpu_lead_tdl.id}};
  require(ocg::validate_cuda_support(config).empty(), "CUDA should accept a chain-leading tdl");
  config.links = {{.from = "gnb0", .to = "ue0", .model = gpu_mid_tdl.id}};
  require(!ocg::validate_cuda_support(config).empty(), "CUDA should reject a non-leading tdl");

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
      - type: tdl
        taps:
          - delay_samples: 0.0
            gain_db: 0.0
            phase_rad: 0.0
  with_delay:
    chain:
      - type: tdl
        taps:
          - delay_samples: 5.0
            gain_db: -3.0
            phase_rad: 0.0
)yaml";
  }
  auto tx_offset_config = ocg::load_config_file(tx_offset_path);

  // Link gnb0 -> ue0 used the `serving` model which after Commit B is a
  // single-tap tdl with delay 0 / gain 0. The tx_timing_offset 3 merges into
  // that tdl's only tap (shift by 3), so the synthesized chain is still a
  // single tdl step with the tap's delay now at 3.
  const auto* gnb0_ue0 = ocg::find_model(tx_offset_config, tx_offset_config.links[0].model);
  require(gnb0_ue0 != nullptr, "gnb0>ue0 effective model must exist");
  require(gnb0_ue0->chain.size() == 1, "gnb0>ue0 chain is the single merged tdl");
  require(gnb0_ue0->chain.front().type == ocg::ModelStepType::Tdl,
          "gnb0>ue0 leading step must remain a tdl after the offset merge");
  require(gnb0_ue0->chain.front().taps.size() == 1,
          "gnb0>ue0 tdl is single-tap (matches the source `serving` chain)");
  require(gnb0_ue0->chain.front().taps.front().delay_samples == 3.0,
          "gnb0>ue0 tdl tap delay must be original 0 + offset 3");
  require(gnb0_ue0->chain.front().taps.front().gain_db == 0.0,
          "gnb0>ue0 tdl tap gain (0 dB) must be preserved by the merge");

  // Link gnb0 -> ue1 already had a leading tdl with tap delay 5; the tx
  // offset 3 merges into the tdl-merge branch, shifting the tap delay by 3
  // to a final 8. The chain stays a single-step tdl model.
  const auto* gnb0_ue1 = ocg::find_model(tx_offset_config, tx_offset_config.links[1].model);
  require(gnb0_ue1 != nullptr, "gnb0>ue1 effective model must exist");
  require(gnb0_ue1->chain.size() == 1, "gnb0>ue1 chain is the single merged tdl");
  require(gnb0_ue1->chain.front().type == ocg::ModelStepType::Tdl,
          "gnb0>ue1 leading step is a single-tap tdl after the merge");
  require(gnb0_ue1->chain.front().taps.size() == 1,
          "gnb0>ue1 tdl tap count unchanged by the merge");
  require(gnb0_ue1->chain.front().taps.front().delay_samples == 8.0,
          "gnb0>ue1 tdl tap delay must be original 5 + offset 3");
  require(gnb0_ue1->chain.front().taps.front().gain_db == -3.0,
          "gnb0>ue1 tdl tap gain must be preserved at -3 dB after the merge");

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
      - type: tdl
        taps:
          - delay_samples: 0.0
            gain_db: 0.0
            phase_rad: 0.0
)yaml";
  }
  auto tx_frac_config = ocg::load_config_file(tx_frac_path);
  const auto* frac_model = ocg::find_model(tx_frac_config, tx_frac_config.links[0].model);
  require(frac_model != nullptr, "fractional-offset effective model must exist");
  // No leading propagation step in the source chain -> synthesized as a
  // single-tap tdl with the fractional delay preserved as the tap's
  // delay_samples (which may be fractional).
  require(frac_model->chain.front().type == ocg::ModelStepType::Tdl,
          "fractional offset should synthesize a leading tdl step");
  require(frac_model->chain.front().taps.size() == 1,
          "fractional offset synthesized tdl must be single-tap");
  require(frac_model->chain.front().taps.front().delay_samples == 2.5,
          "fractional offset tap delay_samples preserved");

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
      - type: tdl
        taps:
          - delay_samples: 0.0
            gain_db: 0.0
            phase_rad: 0.0
)yaml";
  }
  auto prop_config = ocg::load_config_file(prop_path);
  // gnb0->ue0: tx_timing_offset 2 + propagation_delay 4 = 6. Synthesized as a
  // single-tap tdl now (was integer_delay before Phase 1.3).
  const auto* prop_ue0 = ocg::find_model(prop_config, prop_config.links[0].model);
  require(prop_ue0 != nullptr, "gnb0>ue0 effective model must exist");
  require(prop_ue0->chain.front().type == ocg::ModelStepType::Tdl,
          "gnb0>ue0 leading step must be a single-tap tdl");
  require(prop_ue0->chain.front().taps.front().delay_samples == 6.0,
          "gnb0>ue0 tdl tap delay must be tx_timing_offset + propagation_delay");
  // gnb0->ue1: tx_timing_offset 2 + propagation_delay 1.5 = 3.5 (fractional).
  // Same single-tap tdl synthesis path; the tap's delay_samples just carries
  // the fractional value.
  const auto* prop_ue1 = ocg::find_model(prop_config, prop_config.links[1].model);
  require(prop_ue1 != nullptr, "gnb0>ue1 effective model must exist");
  require(prop_ue1->chain.front().type == ocg::ModelStepType::Tdl,
          "gnb0>ue1 leading step must be a single-tap tdl (carries fractional delay)");
  require(prop_ue1->chain.front().taps.front().delay_samples == 3.5,
          "gnb0>ue1 tdl tap delay_samples must compose to 3.5");
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
      - type: tdl
        taps:
          - delay_samples: 0.0
            gain_db: 0.0
            phase_rad: 0.0
)yaml";
  }
  auto link_only = ocg::load_config_file(link_only_path);
  const auto* link_only_model = ocg::find_model(link_only, link_only.links[0].model);
  require(link_only_model != nullptr, "link-only effective model must exist");
  require(link_only_model->chain.front().type == ocg::ModelStepType::Tdl,
          "link-only propagation_delay must synthesize a single-tap tdl leading step");
  require(link_only_model->chain.front().taps.front().delay_samples == 7.0,
          "link-only propagation_delay should pass through unchanged");

  // Tdl-merge: an existing leading multi-tap tdl combined with a per-source
  // tx_timing_offset_samples must shift every tap's delay by that offset
  // (rather than prepending a second tdl step, which would create two
  // leading-propagation steps and break the CUDA chain-leading-only rule).
  const char* tdl_merge_path = "test_tdl_merge_topology.yaml";
  {
    std::ofstream f(tdl_merge_path);
    f << R"yaml(
runtime:
  backend: cpu
devices:
  - id: gnb0
    role: gnb
    sample_rate_hz: 23040000
    tx_endpoint: tcp://127.0.0.1:2000
    rx_endpoint: tcp://127.0.0.1:2001
    tx_timing_offset_samples: 5
  - id: ue0
    role: ue
    sample_rate_hz: 23040000
    tx_endpoint: tcp://127.0.0.1:2101
    rx_endpoint: tcp://127.0.0.1:2100
links:
  - from: gnb0
    to: ue0
    model: two_tap
    propagation_delay_samples: 2
  - from: ue0
    to: gnb0
    model: two_tap
models:
  two_tap:
    chain:
      - type: tdl
        taps:
          - delay_samples: 0.0
            gain_db: 0.0
          - delay_samples: 3.0
            gain_db: -6.0
)yaml";
  }
  auto tdl_merge_config = ocg::load_config_file(tdl_merge_path);
  // Forward link composes tx_timing_offset 5 + propagation_delay 2 = 7
  // shift on every tap. Original taps were at delay 0 and 3; merged taps
  // should be at 7 and 10.
  const auto* forward_model = ocg::find_model(tdl_merge_config, tdl_merge_config.links[0].model);
  require(forward_model != nullptr, "gnb0->ue0 effective model must exist");
  require(forward_model->chain.front().type == ocg::ModelStepType::Tdl,
          "gnb0->ue0 leading step must remain tdl after merge");
  require(forward_model->chain.front().taps.size() == 2,
          "gnb0->ue0 tap count must not change after merge");
  require(forward_model->chain.front().taps[0].delay_samples == 7.0,
          "gnb0->ue0 first tap delay must be original 0 + composed offset 7");
  require(forward_model->chain.front().taps[1].delay_samples == 10.0,
          "gnb0->ue0 second tap delay must be original 3 + composed offset 7");
  // Reverse link (ue0->gnb0) has no offset on its source and no
  // propagation_delay; the model name must stay the unsynthesized "two_tap"
  // and its original tap delays are preserved (0 and 3).
  require(tdl_merge_config.links[1].model == "two_tap",
          "ue0->gnb0 source has no offset; model must be the original two_tap");
  const auto* reverse_model = ocg::find_model(tdl_merge_config, "two_tap");
  require(reverse_model != nullptr && reverse_model->chain.front().taps.size() == 2,
          "original two_tap must still exist with 2 taps");
  require(reverse_model->chain.front().taps[0].delay_samples == 0.0 &&
              reverse_model->chain.front().taps[1].delay_samples == 3.0,
          "original two_tap delays must be untouched by the gnb0->ue0 merge");

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
      - type: tdl
        taps:
          - delay_samples: 0.0
            gain_db: 0.0
            phase_rad: 0.0
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
      - type: tdl
        taps:
          - delay_samples: 0.0
            gain_db: 0.0
            phase_rad: 0.0
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
      - type: awgn
        snr_db: 25
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
  require(rejected, "non-tdl step (here awgn) with a taps list must be rejected");

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
      - type: awgn
        snr_db: 25
        taps:
      - type: phase
        phase_rad: 0.0
)yaml";
  }
  rejected = false;
  try {
    (void)ocg::load_config_file(empty_taps_on_gain_path);
  } catch (const std::runtime_error&) {
    rejected = true;
  }
  require(rejected, "empty taps block on a non-tdl step (here awgn) must be rejected");

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

  // ---- Phase 1.4a: fading sub-config schema tests ----
  // (a) Happy-path parse + round-trip: tdl with a fading block + a Rayleigh tap
  // + a Rician (LOS) tap.
  const char* fading_happy_path = "test_tdl_fading_happy_topology.yaml";
  {
    std::ofstream f(fading_happy_path);
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
    model: fading_chan
  - from: ue0
    to: gnb0
    model: fading_chan
models:
  fading_chan:
    chain:
      - type: tdl
        fading:
          f_d_max_hz: 350
          spectrum: jakes
          grid_us: 1.0
        taps:
          - delay_samples: 0.0
            gain_db: 0.0
            is_los: true
            los_k_db: 7.0
            los_angle_rad: 0.5
          - delay_samples: 5.0
            gain_db: -3.0
)yaml";
  }
  auto fading_config = ocg::load_config_file(fading_happy_path);
  const auto* fading_model = ocg::find_model(fading_config, "fading_chan");
  require(fading_model != nullptr, "fading model must exist");
  require(fading_model->chain.size() == 1, "fading model has one tdl step");
  const auto& fading_step = fading_model->chain.front();
  require(fading_step.type == ocg::ModelStepType::Tdl, "fading step is tdl");
  require(fading_step.fading_enabled, "fading_enabled set by parser");
  require(fading_step.fading_f_d_max_hz == 350.0, "f_d_max_hz round-trip");
  require(fading_step.fading_spectrum == ocg::FadingSpectrum::Jakes, "spectrum jakes round-trip");
  require(fading_step.fading_grid_us == 1.0, "grid_us round-trip");
  require(fading_step.taps.size() == 2, "two taps round-trip");
  require(fading_step.taps[0].is_los, "first tap is LOS");
  require(fading_step.taps[0].los_k_db == 7.0, "LOS K-factor round-trip");
  require(fading_step.taps[0].los_angle_rad == 0.5, "LOS angle round-trip");
  require(!fading_step.taps[1].is_los, "second tap is Rayleigh");

  // (b) Fading on a non-tdl step is rejected by the validator.
  const char* fading_on_awgn_path = "test_fading_on_awgn_topology.yaml";
  {
    std::ofstream f(fading_on_awgn_path);
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
    model: bogus
  - from: ue0
    to: gnb0
    model: bogus
models:
  bogus:
    chain:
      - type: awgn
        snr_db: 25
        fading:
          f_d_max_hz: 100
)yaml";
  }
  rejected = false;
  try {
    (void)ocg::load_config_file(fading_on_awgn_path);
  } catch (const std::runtime_error&) {
    rejected = true;
  }
  require(rejected, "fading sub-config on a non-tdl step must be rejected");

  // (c) Negative f_d_max_hz is rejected.
  const char* fading_neg_path = "test_fading_neg_topology.yaml";
  {
    std::ofstream f(fading_neg_path);
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
    model: neg_doppler
  - from: ue0
    to: gnb0
    model: neg_doppler
models:
  neg_doppler:
    chain:
      - type: tdl
        fading:
          f_d_max_hz: -10
        taps:
          - delay_samples: 0.0
            gain_db: 0.0
)yaml";
  }
  rejected = false;
  try {
    (void)ocg::load_config_file(fading_neg_path);
  } catch (const std::runtime_error&) {
    rejected = true;
  }
  require(rejected, "negative f_d_max_hz must be rejected");

  // (d) Unknown spectrum name rejected at parse time.
  const char* fading_bad_spec_path = "test_fading_bad_spectrum_topology.yaml";
  {
    std::ofstream f(fading_bad_spec_path);
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
    model: bad_spec
  - from: ue0
    to: gnb0
    model: bad_spec
models:
  bad_spec:
    chain:
      - type: tdl
        fading:
          f_d_max_hz: 100
          spectrum: lorentzian
        taps:
          - delay_samples: 0.0
            gain_db: 0.0
)yaml";
  }
  rejected = false;
  try {
    (void)ocg::load_config_file(fading_bad_spec_path);
  } catch (const std::runtime_error&) {
    rejected = true;
  }
  require(rejected, "unknown fading spectrum must be rejected");

  // (e) is_los on a non-fading tdl step is rejected (LOS only meaningful with fading).
  const char* los_no_fading_path = "test_los_no_fading_topology.yaml";
  {
    std::ofstream f(los_no_fading_path);
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
    model: los_no_fading
  - from: ue0
    to: gnb0
    model: los_no_fading
models:
  los_no_fading:
    chain:
      - type: tdl
        taps:
          - delay_samples: 0.0
            gain_db: 0.0
            is_los: true
            los_k_db: 5.0
)yaml";
  }
  rejected = false;
  try {
    (void)ocg::load_config_file(los_no_fading_path);
  } catch (const std::runtime_error&) {
    rejected = true;
  }
  require(rejected, "is_los on a tdl step without fading enabled must be rejected");

  // (f) is_los with defaulted los_k_db (= 0 dB) is rejected -- a forgotten
  // los_k_db field would otherwise silently produce a degenerate K=1 LOS tap
  // (specular and Rayleigh equal-power). The TR 38.901 LOS profiles publish
  // 13.3 dB / 22 dB; anything <=0 dB is a config bug.
  const char* los_k_zero_path = "test_los_k_zero_topology.yaml";
  {
    std::ofstream f(los_k_zero_path);
    f << R"yaml(
runtime:
  backend: cpu
  batch_samples: 1024
  queue_samples: 8192
devices:
  - id: gnb0
    role: gnb
    sample_rate_hz: 1000000
    tx_endpoint: tcp://127.0.0.1:2000
    rx_endpoint: tcp://127.0.0.1:2001
  - id: ue0
    role: ue
    sample_rate_hz: 1000000
    tx_endpoint: tcp://127.0.0.1:2101
    rx_endpoint: tcp://127.0.0.1:2100
links:
  - from: gnb0
    to: ue0
    model: los_k_zero
  - from: ue0
    to: gnb0
    model: los_k_zero
models:
  los_k_zero:
    chain:
      - type: tdl
        fading:
          f_d_max_hz: 100.0
          spectrum: jakes
        taps:
          - delay_samples: 0.0
            gain_db: 0.0
            is_los: true
)yaml";
  }
  rejected = false;
  try {
    (void)ocg::load_config_file(los_k_zero_path);
  } catch (const std::runtime_error&) {
    rejected = true;
  }
  require(rejected, "is_los with defaulted los_k_db (= 0) must be rejected");

  std::remove(los_k_zero_path);
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
  std::remove(tdl_merge_path);
  std::remove(fading_happy_path);
  std::remove(fading_on_awgn_path);
  std::remove(fading_neg_path);
  std::remove(fading_bad_spec_path);
  std::remove(los_no_fading_path);
  return 0;
}
