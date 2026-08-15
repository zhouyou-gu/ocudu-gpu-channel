#include "ocudu_gpu_channel/backend.h"
#include "ocudu_gpu_channel/config.h"
#include "ocudu_gpu_channel/cpu_backend.h"
#include "ocudu_gpu_channel/delay.h"
#include "ocudu_gpu_channel/processing.h"
#include <array>
#include <cmath>
#include <cstdlib>
#include <iostream>
#include <numbers>
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

// Bit-exact equivalence. For cases where two computations on the SAME backend
// MUST produce identical floats (e.g., a determinism guard: same seed, same
// model, same link key) -- not "near", literally byte-identical. Tightens the
// assertion so a float-determinism regression across recompiles is caught
// instead of being hidden by the 1e-3 numeric tolerance.
void require_equal_buffer(const ocg::IqBuffer& lhs, const ocg::IqBuffer& rhs, const char* message)
{
  require(lhs.size() == rhs.size(), message);
  for (std::size_t i = 0; i != lhs.size(); ++i) {
    if (lhs[i].i != rhs[i].i || lhs[i].q != rhs[i].q) {
      std::cerr << "FAIL: " << message << " at sample " << i << " lhs=(" << lhs[i].i << "," << lhs[i].q
                << ") rhs=(" << rhs[i].i << "," << rhs[i].q << ")\n";
      std::exit(1);
    }
  }
}

// Single-edge shape: call process_superposition with one input, no rx_model.
// Replaces the old process_into convenience entry point.
void shape_link(ocg::ChannelProcessor& proc,
                const std::string& dst_id,
                const std::string& link_key,
                const ocg::ModelConfig& model,
                const ocg::IqBuffer& input,
                ocg::IqBuffer& output,
                std::uint64_t sample_rate_hz)
{
  ocg::SuperpositionInput edge{.link_key = link_key,
                               .model = &model,
                               .samples = std::span<const ocg::IqSample>(input.data(), input.size())};
  proc.process_superposition(dst_id, {edge}, nullptr, sample_rate_hz,
                              std::span<ocg::IqSample>(output.data(), output.size()));
}

ocg::IqBuffer shape_link_buf(ocg::ChannelProcessor& proc,
                             const std::string& dst_id,
                             const std::string& link_key,
                             const ocg::ModelConfig& model,
                             const ocg::IqBuffer& input,
                             std::uint64_t sample_rate_hz)
{
  ocg::IqBuffer out(input.size());
  shape_link(proc, dst_id, link_key, model, input, out, sample_rate_hz);
  return out;
}

} // namespace

int main()
{
  // --- tdl prerequisite: windowed-sinc fractional-delay filter helper. ---
  // Two assertions: (1) for frac == 0 the 8-tap filter collapses to a unit
  // impulse at index 3 (DC-normalised; coefficient 1.0 at i=3, 0 elsewhere),
  // so an integer-delay tap shares the fractional code path without a special
  // case; (2) coefficients sum to 1 for any frac in [0, 1) so a constant input
  // passes through unchanged.
  {
    std::array<float, ocg::kTdlFracFilterTaps> coeffs{};
    ocg::compute_windowed_sinc_taps(0.0, coeffs);
    require(std::fabs(coeffs[3] - 1.0F) < 1e-6F, "sinc(frac=0): coeff[3] must be 1");
    for (int i = 0; i < ocg::kTdlFracFilterTaps; ++i) {
      if (i == 3) continue;
      require(std::fabs(coeffs[static_cast<std::size_t>(i)]) < 1e-6F,
              "sinc(frac=0): non-center coeffs must be zero");
    }
    for (double frac : {0.1, 0.25, 0.5, 0.75, 0.9}) {
      ocg::compute_windowed_sinc_taps(frac, coeffs);
      double sum = 0.0;
      for (float c : coeffs) sum += c;
      require(std::fabs(sum - 1.0) < 1e-5,
              "sinc coefficients must sum to 1 for any fractional offset");
    }
  }

  ocg::TopologyConfig config;
  config.runtime.batch_samples_auto = false;
  config.runtime.batch_samples = 4;
  config.devices = {
      {.id = "gnb0", .role = "gnb", .sample_rate_hz = 1000, .tx_endpoint = "tx0", .rx_endpoint = "rx0"},
      {.id = "ue0", .role = "ue", .sample_rate_hz = 1000, .tx_endpoint = "tx1", .rx_endpoint = "rx1"}};
  config.links = {{.from = "gnb0", .to = "ue0", .model = "gain2"}, {.from = "ue0", .to = "gnb0", .model = "gain2"}};
  ocg::ModelConfig model;
  model.id = "gain2";
  model.chain.push_back({.type = ocg::ModelStepType::Tdl,
                         .params = {},
                         .taps = {{.delay_samples = 0.0, .gain_db = 6.020599913, .phase_rad = 0.0}},
                         .taps_declared = true});
  config.models.emplace(model.id, model);

  std::unordered_map<std::string, ocg::IqBuffer> latest;
  latest["gnb0"] = {{1.0F, 0.0F}, {0.0F, 1.0F}, {1.0F, 1.0F}, {0.0F, 0.0F}};
  latest["ue0"] = {{0.5F, 0.0F}, {0.0F, 0.5F}, {0.5F, 0.5F}, {0.0F, 0.0F}};

  // prepare() + a prepared link key: the path the broker exercises.
  ocg::CpuChannelProcessor processor;
  processor.prepare(config);

  ocg::IqBuffer output(4);
  shape_link(processor, "ue0", ocg::link_key(config.links[0]), model, latest["gnb0"], output, 1000);
  require(near(output[2].i, 2.0F), "single-edge superposition: gain on I");
  require(near(output[2].q, 2.0F), "single-edge superposition: gain on Q");

  ocg::ModelConfig delay;
  delay.id = "delay";
  delay.chain.push_back({.type = ocg::ModelStepType::Tdl,
                         .params = {},
                         .taps = {{.delay_samples = 2.0, .gain_db = 0.0, .phase_rad = 0.0}},
                         .taps_declared = true});
  auto delayed = shape_link_buf(processor, "ue0", "link-delay", delay, latest["gnb0"], 1000);
  require(near(delayed[0].i, 0.0F), "delay inserts zero sample 0");
  require(near(delayed[1].i, 0.0F), "delay inserts zero sample 1");
  require(near(delayed[2].i, 1.0F), "delay sample 2");
  ocg::IqBuffer more = {{2.0F, 0.0F}, {3.0F, 0.0F}, {4.0F, 0.0F}, {5.0F, 0.0F}};
  auto delayed_more = shape_link_buf(processor, "ue0", "link-delay", delay, more, 1000);
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
        {.id = "gnb0", .role = "gnb", .sample_rate_hz = 23040000, .tx_endpoint = "tx0", .rx_endpoint = "rx0"},
        {.id = "ue0", .role = "ue", .sample_rate_hz = 23040000, .tx_endpoint = "tx1", .rx_endpoint = "rx1"}};
    cuda_config.links = {{.from = "gnb0", .to = "ue0", .model = "cuda_mvp"}};

    ocg::ModelConfig cuda_model;
    cuda_model.id = "cuda_mvp";
    cuda_model.chain.push_back({.type = ocg::ModelStepType::Tdl,
                                .params = {},
                                .taps = {{.delay_samples = 0.0, .gain_db = 3.0, .phase_rad = 0.0}},
                                .taps_declared = true});
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

    shape_link(cpu_reference, "ue0", key, cuda_model, first_batch, cpu_out, 23040000);
    shape_link(*cuda_processor, "ue0", key, cuda_model, first_batch, cuda_out, 23040000);
    require_near_buffer(cpu_out, cuda_out, "CUDA first batch should match CPU reference");

    shape_link(cpu_reference, "ue0", key, cuda_model, second_batch, cpu_out, 23040000);
    shape_link(*cuda_processor, "ue0", key, cuda_model, second_batch, cuda_out, 23040000);
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
        {.id = "gnb0", .role = "gnb", .sample_rate_hz = 23040000, .tx_endpoint = "tx0", .rx_endpoint = "rx0"},
        {.id = "ue0", .role = "ue", .sample_rate_hz = 23040000, .tx_endpoint = "tx1", .rx_endpoint = "rx1"}};
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
    shape_link(*awgn_processor, "ue0", "gnb0>ue0:awgn", awgn_model, zeros, noisy, 23040000);

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
    shape_link(*awgn_processor, "ue0", "gnb0>ue0:awgn", awgn_model, zeros, noisy2, 23040000);
    bool differs = false;
    for (std::size_t s = 0; s != noisy.size(); ++s) {
      if (noisy[s].i != noisy2[s].i || noisy[s].q != noisy2[s].q) {
        differs = true;
        break;
      }
    }
    require(differs, "CUDA AWGN should produce a fresh noise stream each batch");

    // Superposition: process_superposition over two edges into one node must
    // equal the CPU reference sum of the two per-edge channel chains.
    ocg::TopologyConfig sp_config;
    sp_config.runtime.backend = ocg::Backend::Cuda;
    sp_config.runtime.batch_samples_auto = false;
    sp_config.runtime.batch_samples = 8;
    sp_config.runtime.queue_samples = 64;
    sp_config.devices = {
        {.id = "gnb0", .role = "gnb", .sample_rate_hz = 23040000, .tx_endpoint = "t0", .rx_endpoint = "r0"},
        {.id = "gnb1", .role = "gnb", .sample_rate_hz = 23040000, .tx_endpoint = "t1", .rx_endpoint = "r1"},
        {.id = "ue0", .role = "ue", .sample_rate_hz = 23040000, .tx_endpoint = "t2", .rx_endpoint = "r2"}};
    sp_config.links = {{.from = "gnb0", .to = "ue0", .model = "edge_a"},
                       {.from = "gnb1", .to = "ue0", .model = "edge_b"}};
    ocg::ModelConfig edge_a;
    edge_a.id = "edge_a";
    edge_a.chain.push_back({.type = ocg::ModelStepType::Tdl,
                            .params = {},
                            .taps = {{.delay_samples = 0.0, .gain_db = -3.0, .phase_rad = 0.0}},
                            .taps_declared = true});
    edge_a.chain.push_back({.type = ocg::ModelStepType::Phase, .params = {{"phase_rad", 0.2}}});
    ocg::ModelConfig edge_b;
    edge_b.id = "edge_b";
    edge_b.chain.push_back({.type = ocg::ModelStepType::PathLoss, .params = {{"path_loss_db", 6.0}}});
    edge_b.chain.push_back({.type = ocg::ModelStepType::Cfo, .params = {{"cfo_hz", 300.0}}});
    sp_config.models.emplace("edge_a", edge_a);
    sp_config.models.emplace("edge_b", edge_b);

    auto sp_processor = ocg::create_channel_processor(sp_config);
    ocg::CpuChannelProcessor sp_reference;
    sp_reference.prepare(sp_config);

    const ocg::IqBuffer in_a = {{0.30F, -0.10F}, {0.45F, 0.25F}, {-0.20F, 0.60F}, {0.15F, -0.55F},
                                {0.70F, 0.05F},  {-0.35F, 0.40F}, {0.50F, -0.30F}, {-0.65F, 0.20F}};
    const ocg::IqBuffer in_b = {{-0.25F, 0.35F}, {0.55F, -0.45F}, {0.10F, 0.80F}, {-0.60F, -0.15F},
                                {0.40F, 0.50F},  {0.20F, -0.70F}, {-0.30F, 0.25F}, {0.65F, -0.05F}};
    ocg::IqBuffer ref_a(8);
    ocg::IqBuffer ref_b(8);
    shape_link(sp_reference, "ue0", "gnb0>ue0:edge_a", edge_a, in_a, ref_a, 23040000);
    shape_link(sp_reference, "ue0", "gnb1>ue0:edge_b", edge_b, in_b, ref_b, 23040000);
    ocg::IqBuffer reference(8);
    for (std::size_t s = 0; s != 8; ++s) {
      reference[s] = ref_a[s] + ref_b[s];
    }

    std::vector<ocg::SuperpositionInput> edges = {
        {.link_key = "gnb0>ue0:edge_a", .model = &edge_a, .samples = in_a},
        {.link_key = "gnb1>ue0:edge_b", .model = &edge_b, .samples = in_b}};
    ocg::IqBuffer superposed(8);
    sp_processor->process_superposition("ue0", edges, nullptr, 23040000, superposed);
    require_near_buffer(reference, superposed, "CUDA superposition should equal the CPU edge sum");

    // Integer/fractional delay: a chain-leading sample delay runs on the CUDA
    // backend (applied host-side at staging) and must match the CPU reference
    // bit-for-bit, including cross-batch delay-line continuity.
    ocg::TopologyConfig delay_config;
    delay_config.runtime.backend = ocg::Backend::Cuda;
    delay_config.runtime.batch_samples_auto = false;
    delay_config.runtime.batch_samples = 8;
    delay_config.runtime.queue_samples = 64;
    delay_config.devices = {
        {.id = "gnb0", .role = "gnb", .sample_rate_hz = 23040000, .tx_endpoint = "tx0", .rx_endpoint = "rx0"},
        {.id = "ue0", .role = "ue", .sample_rate_hz = 23040000, .tx_endpoint = "tx1", .rx_endpoint = "rx1"}};
    delay_config.links = {{.from = "gnb0", .to = "ue0", .model = "delay_chain"}};
    ocg::ModelConfig delay_chain;
    delay_chain.id = "delay_chain";
    // Leading tdl (fractional delay + gain folded into a single tap), then a
    // per-sample step -- exercises the no-op device tdl step followed by a
    // real per-sample step.
    delay_chain.chain.push_back({.type = ocg::ModelStepType::Tdl,
                                 .params = {},
                                 .taps = {{.delay_samples = 2.75, .gain_db = -3.0, .phase_rad = 0.0}},
                                 .taps_declared = true});
    delay_config.models.emplace(delay_chain.id, delay_chain);

    ocg::CpuChannelProcessor delay_reference;
    delay_reference.prepare(delay_config);
    auto delay_processor = ocg::create_channel_processor(delay_config);
    const std::string delay_key = "gnb0>ue0:delay_chain";
    ocg::IqBuffer delay_cpu(8);
    ocg::IqBuffer delay_cuda(8);

    shape_link(delay_reference, "ue0", delay_key, delay_chain, first_batch, delay_cpu, 23040000);
    shape_link(*delay_processor, "ue0", delay_key, delay_chain, first_batch, delay_cuda, 23040000);
    require_near_buffer(delay_cpu, delay_cuda, "CUDA leading delay should match the CPU reference");

    shape_link(delay_reference, "ue0", delay_key, delay_chain, second_batch, delay_cpu, 23040000);
    shape_link(*delay_processor, "ue0", delay_key, delay_chain, second_batch, delay_cuda, 23040000);
    require_near_buffer(delay_cpu, delay_cuda, "CUDA delay should carry its history across batches");

    // The delay path also holds inside the superposition kernel: one delayed
    // edge plus one plain edge must still equal the CPU reference sum.
    ocg::TopologyConfig spd_config;
    spd_config.runtime.backend = ocg::Backend::Cuda;
    spd_config.runtime.batch_samples_auto = false;
    spd_config.runtime.batch_samples = 8;
    spd_config.runtime.queue_samples = 64;
    spd_config.devices = {
        {.id = "gnb0", .role = "gnb", .sample_rate_hz = 23040000, .tx_endpoint = "t0", .rx_endpoint = "r0"},
        {.id = "gnb1", .role = "gnb", .sample_rate_hz = 23040000, .tx_endpoint = "t1", .rx_endpoint = "r1"},
        {.id = "ue0", .role = "ue", .sample_rate_hz = 23040000, .tx_endpoint = "t2", .rx_endpoint = "r2"}};
    spd_config.links = {{.from = "gnb0", .to = "ue0", .model = "delayed_edge"},
                        {.from = "gnb1", .to = "ue0", .model = "edge_a"}};
    ocg::ModelConfig delayed_edge;
    delayed_edge.id = "delayed_edge";
    delayed_edge.chain.push_back({.type = ocg::ModelStepType::Tdl,
                                  .params = {},
                                  .taps = {{.delay_samples = 3.0, .gain_db = 0.0, .phase_rad = 0.0}},
                                  .taps_declared = true});
    delayed_edge.chain.push_back({.type = ocg::ModelStepType::PathLoss, .params = {{"path_loss_db", 6.0}}});
    spd_config.models.emplace("delayed_edge", delayed_edge);
    spd_config.models.emplace("edge_a", edge_a);

    auto spd_processor = ocg::create_channel_processor(spd_config);
    ocg::CpuChannelProcessor spd_reference;
    spd_reference.prepare(spd_config);
    ocg::IqBuffer spd_ref_a(8);
    ocg::IqBuffer spd_ref_b(8);
    shape_link(spd_reference, "ue0", "gnb0>ue0:delayed_edge", delayed_edge, in_a, spd_ref_a, 23040000);
    shape_link(spd_reference, "ue0", "gnb1>ue0:edge_a", edge_a, in_b, spd_ref_b, 23040000);
    ocg::IqBuffer spd_reference_sum(8);
    for (std::size_t s = 0; s != 8; ++s) {
      spd_reference_sum[s] = spd_ref_a[s] + spd_ref_b[s];
    }
    std::vector<ocg::SuperpositionInput> spd_edges = {
        {.link_key = "gnb0>ue0:delayed_edge", .model = &delayed_edge, .samples = in_a},
        {.link_key = "gnb1>ue0:edge_a", .model = &edge_a, .samples = in_b}};
    ocg::IqBuffer spd_superposed(8);
    spd_processor->process_superposition("ue0", spd_edges, nullptr, 23040000, spd_superposed);
    require_near_buffer(spd_reference_sum, spd_superposed, "CUDA superposition with a delayed edge should match CPU");
  }
#endif

  // tx_timing_offset_samples on a source must be equivalent to manually
  // adding a chain-leading integer_delay on every outgoing link.
  {
    ocg::IqBuffer in = {{1.0F, -0.5F}, {0.25F, 0.0F}, {-0.5F, 0.75F}, {0.0F, 1.0F},
                       {0.3F, -0.3F}, {-0.7F, 0.2F}, {1.1F, 0.0F},   {-0.4F, 0.6F}};

    // Reference: explicit integer_delay 3 on the link.
    ocg::TopologyConfig ref_cfg;
    ref_cfg.runtime.batch_samples_auto = false;
    ref_cfg.runtime.batch_samples = 8;
    ref_cfg.runtime.queue_samples = 64;
    ref_cfg.devices = {{.id = "gnb0", .role = "gnb", .sample_rate_hz = 1000, .tx_endpoint = "tx0", .rx_endpoint = "rx0"},
                       {.id = "ue0", .role = "ue", .sample_rate_hz = 1000, .tx_endpoint = "tx1", .rx_endpoint = "rx1"}};
    ref_cfg.links = {{.from = "gnb0", .to = "ue0", .model = "with_delay"},
                     {.from = "ue0", .to = "gnb0", .model = "plain"}};
    ocg::ModelConfig with_delay;
    with_delay.id = "with_delay";
    with_delay.chain.push_back({.type = ocg::ModelStepType::Tdl,
                                 .params = {},
                                 .taps = {{.delay_samples = 3.0, .gain_db = -3.0, .phase_rad = 0.0}},
                                 .taps_declared = true});
    ocg::ModelConfig plain;
    plain.id = "plain";
    plain.chain.push_back({.type = ocg::ModelStepType::Tdl,
                           .params = {},
                           .taps = {{.delay_samples = 0.0, .gain_db = -3.0, .phase_rad = 0.0}},
                           .taps_declared = true});
    ref_cfg.models.emplace(with_delay.id, with_delay);
    ref_cfg.models.emplace(plain.id, plain);

    ocg::CpuChannelProcessor ref_proc;
    ref_proc.prepare(ref_cfg);
    ocg::IqBuffer ref_out(8);
    shape_link(ref_proc, ref_cfg.links[0].to, ocg::link_key(ref_cfg.links[0]), with_delay, in, ref_out, 1000);

    // Under test: same link with NO explicit delay on the model, but with
    // tx_timing_offset_samples = 3 on the source device.
    ocg::TopologyConfig off_cfg;
    off_cfg.runtime.batch_samples_auto = false;
    off_cfg.runtime.batch_samples = 8;
    off_cfg.runtime.queue_samples = 64;
    off_cfg.devices = {{.id = "gnb0",
                       .role = "gnb",
                       .sample_rate_hz = 1000,
                       .tx_endpoint = "tx0",
                       .rx_endpoint = "rx0",
                       .rx_model = "",
                       .tx_timing_offset_samples = 3.0},
                      {.id = "ue0", .role = "ue", .sample_rate_hz = 1000, .tx_endpoint = "tx1", .rx_endpoint = "rx1"}};
    off_cfg.links = {{.from = "gnb0", .to = "ue0", .model = "plain"},
                     {.from = "ue0", .to = "gnb0", .model = "plain"}};
    off_cfg.models.emplace(plain.id, plain);
    ocg::fold_link_leading_delays(off_cfg);

    // After folding, link gnb0->ue0 must reference a synthesized effective
    // model; look it up rather than using the raw name.
    const auto* off_model = ocg::find_model(off_cfg, off_cfg.links[0].model);
    require(off_model != nullptr, "effective model must exist after offset apply");
    ocg::CpuChannelProcessor off_proc;
    off_proc.prepare(off_cfg);
    ocg::IqBuffer off_out(8);
    shape_link(off_proc, off_cfg.links[0].to, ocg::link_key(off_cfg.links[0]), *off_model, in, off_out, 1000);

    require_near_buffer(ref_out, off_out, "tx_timing_offset CPU output must equal explicit per-link delay");

#if OCUDU_GPU_CHANNEL_HAS_CUDA
    if (ocg::cuda_compiled()) {
      // Same equivalence on the CUDA backend.
      ocg::TopologyConfig cuda_ref = ref_cfg;
      cuda_ref.runtime.backend = ocg::Backend::Cuda;
      cuda_ref.devices[0].sample_rate_hz = 23040000;
      cuda_ref.devices[1].sample_rate_hz = 23040000;
      auto cuda_ref_proc = ocg::create_channel_processor(cuda_ref);
      ocg::IqBuffer cuda_ref_out(8);
      shape_link(*cuda_ref_proc, cuda_ref.links[0].to, ocg::link_key(cuda_ref.links[0]), with_delay, in,
                 cuda_ref_out, 23040000);

      ocg::TopologyConfig cuda_off = off_cfg;
      cuda_off.runtime.backend = ocg::Backend::Cuda;
      cuda_off.devices[0].sample_rate_hz = 23040000;
      cuda_off.devices[1].sample_rate_hz = 23040000;
      // Note: apply_tx_timing_offsets was already called on off_cfg above and
      // copied into cuda_off, so the effective model is in place.
      const auto* cuda_off_model = ocg::find_model(cuda_off, cuda_off.links[0].model);
      require(cuda_off_model != nullptr, "CUDA effective model must exist");
      auto cuda_off_proc = ocg::create_channel_processor(cuda_off);
      ocg::IqBuffer cuda_off_out(8);
      shape_link(*cuda_off_proc, cuda_off.links[0].to, ocg::link_key(cuda_off.links[0]), *cuda_off_model, in,
                 cuda_off_out, 23040000);
      require_near_buffer(cuda_ref_out, cuda_off_out,
                          "tx_timing_offset CUDA output must equal explicit per-link delay");
    }
#endif
  }

  // Link-level propagation_delay_samples must be equivalent to manually
  // prepending an integer_delay step on the link's chain.
  {
    ocg::IqBuffer in = {{1.0F, -0.5F}, {0.25F, 0.0F}, {-0.5F, 0.75F}, {0.0F, 1.0F},
                       {0.3F, -0.3F}, {-0.7F, 0.2F}, {1.1F, 0.0F},   {-0.4F, 0.6F}};

    // Reference: explicit integer_delay 4 on the link.
    ocg::TopologyConfig ref_cfg;
    ref_cfg.runtime.batch_samples_auto = false;
    ref_cfg.runtime.batch_samples = 8;
    ref_cfg.runtime.queue_samples = 64;
    ref_cfg.devices = {{.id = "gnb0", .role = "gnb", .sample_rate_hz = 1000, .tx_endpoint = "tx0", .rx_endpoint = "rx0"},
                       {.id = "ue0", .role = "ue", .sample_rate_hz = 1000, .tx_endpoint = "tx1", .rx_endpoint = "rx1"}};
    ref_cfg.links = {{.from = "gnb0", .to = "ue0", .model = "with_delay"},
                     {.from = "ue0", .to = "gnb0", .model = "plain"}};
    ocg::ModelConfig with_delay;
    with_delay.id = "with_delay";
    with_delay.chain.push_back({.type = ocg::ModelStepType::Tdl,
                                 .params = {},
                                 .taps = {{.delay_samples = 4.0, .gain_db = 0.0, .phase_rad = 0.0}},
                                 .taps_declared = true});
    with_delay.chain.push_back({.type = ocg::ModelStepType::PathLoss, .params = {{"path_loss_db", 6.0}}});
    ocg::ModelConfig plain;
    plain.id = "plain";
    plain.chain.push_back({.type = ocg::ModelStepType::PathLoss, .params = {{"path_loss_db", 6.0}}});
    ref_cfg.models.emplace(with_delay.id, with_delay);
    ref_cfg.models.emplace(plain.id, plain);

    ocg::CpuChannelProcessor ref_proc;
    ref_proc.prepare(ref_cfg);
    ocg::IqBuffer ref_out(8);
    shape_link(ref_proc, ref_cfg.links[0].to, ocg::link_key(ref_cfg.links[0]), with_delay, in, ref_out, 1000);

    // Under test: same edge with link.propagation_delay_samples = 4 and a
    // plain (delay-free) base model.
    ocg::TopologyConfig prop_cfg;
    prop_cfg.runtime.batch_samples_auto = false;
    prop_cfg.runtime.batch_samples = 8;
    prop_cfg.runtime.queue_samples = 64;
    prop_cfg.devices = {{.id = "gnb0", .role = "gnb", .sample_rate_hz = 1000, .tx_endpoint = "tx0", .rx_endpoint = "rx0"},
                       {.id = "ue0", .role = "ue", .sample_rate_hz = 1000, .tx_endpoint = "tx1", .rx_endpoint = "rx1"}};
    prop_cfg.links = {{.from = "gnb0", .to = "ue0", .model = "plain", .propagation_delay_samples = 4.0},
                     {.from = "ue0", .to = "gnb0", .model = "plain"}};
    prop_cfg.models.emplace(plain.id, plain);
    ocg::fold_link_leading_delays(prop_cfg);

    const auto* prop_model = ocg::find_model(prop_cfg, prop_cfg.links[0].model);
    require(prop_model != nullptr, "propagation_delay effective model must exist");
    ocg::CpuChannelProcessor prop_proc;
    prop_proc.prepare(prop_cfg);
    ocg::IqBuffer prop_out(8);
    shape_link(prop_proc, prop_cfg.links[0].to, ocg::link_key(prop_cfg.links[0]), *prop_model, in, prop_out, 1000);

    require_near_buffer(ref_out, prop_out,
                        "link propagation_delay_samples CPU output must equal explicit chain-leading delay");

    // Compose: tx_timing_offset 2 (device) + propagation_delay 4 (link) must
    // equal a manual chain-leading integer_delay 6.
    ocg::TopologyConfig ref6_cfg = ref_cfg;
    ref6_cfg.models.erase(with_delay.id);
    ocg::ModelConfig with_delay_6 = with_delay;
    with_delay_6.id = "with_delay_6";
    // with_delay is now a single-step tdl; bump its first (only) tap's delay
    // from 3 to 6 to match the composed offset under test.
    with_delay_6.chain.front().taps.front().delay_samples = 6.0;
    ref6_cfg.models.emplace(with_delay_6.id, with_delay_6);
    ref6_cfg.links[0].model = with_delay_6.id;
    ocg::CpuChannelProcessor ref6_proc;
    ref6_proc.prepare(ref6_cfg);
    ocg::IqBuffer ref6_out(8);
    shape_link(ref6_proc, ref6_cfg.links[0].to, ocg::link_key(ref6_cfg.links[0]), with_delay_6, in, ref6_out, 1000);

    ocg::TopologyConfig compose_cfg;
    compose_cfg.runtime.batch_samples_auto = false;
    compose_cfg.runtime.batch_samples = 8;
    compose_cfg.runtime.queue_samples = 64;
    compose_cfg.devices = {{.id = "gnb0",
                            .role = "gnb",
                            .sample_rate_hz = 1000,
                            .tx_endpoint = "tx0",
                            .rx_endpoint = "rx0",
                            .rx_model = "",
                            .tx_timing_offset_samples = 2.0},
                           {.id = "ue0", .role = "ue", .sample_rate_hz = 1000, .tx_endpoint = "tx1", .rx_endpoint = "rx1"}};
    compose_cfg.links = {{.from = "gnb0", .to = "ue0", .model = "plain", .propagation_delay_samples = 4.0},
                         {.from = "ue0", .to = "gnb0", .model = "plain"}};
    compose_cfg.models.emplace(plain.id, plain);
    ocg::fold_link_leading_delays(compose_cfg);

    const auto* compose_model = ocg::find_model(compose_cfg, compose_cfg.links[0].model);
    require(compose_model != nullptr, "composed effective model must exist");
    // After Phase 1.3 retargeting, fold synthesizes a single-tap tdl when the
    // source chain has no leading propagation step. The legacy
    // params["delay_samples"] field is replaced by taps[0].delay_samples.
    require(compose_model->chain.front().type == ocg::ModelStepType::Tdl,
            "composed leading step must be a single-tap tdl");
    require(compose_model->chain.front().taps.front().delay_samples == 6.0,
            "composed leading tdl tap delay must be tx_timing_offset + propagation_delay");
    ocg::CpuChannelProcessor compose_proc;
    compose_proc.prepare(compose_cfg);
    ocg::IqBuffer compose_out(8);
    shape_link(compose_proc, compose_cfg.links[0].to, ocg::link_key(compose_cfg.links[0]), *compose_model, in,
               compose_out, 1000);
    require_near_buffer(ref6_out, compose_out,
                        "device tx_timing_offset + link propagation_delay must compose into one leading delay");
  }

  // ---- tdl (Phase 1.1 CPU kernel): behaviour tests ----
  // Helper: build a minimal 1-link topology and prepared CPU processor that
  // runs `model` on a 1 MS/s loop between gnb0 and ue0. Each call returns a
  // fresh processor so cross-slot history is isolated per test case.
  auto make_tdl_processor = [](const ocg::ModelConfig& model, std::size_t batch)
      -> std::unique_ptr<ocg::CpuChannelProcessor> {
    ocg::TopologyConfig cfg;
    cfg.runtime.batch_samples_auto = false;
    cfg.runtime.batch_samples = batch;
    cfg.runtime.queue_samples = std::max<std::size_t>(614400, batch * 8);
    cfg.devices = {{.id = "gnb0", .role = "gnb", .sample_rate_hz = 1000, .tx_endpoint = "tx0", .rx_endpoint = "rx0"},
                   {.id = "ue0", .role = "ue", .sample_rate_hz = 1000, .tx_endpoint = "tx1", .rx_endpoint = "rx1"}};
    cfg.links = {{.from = "gnb0", .to = "ue0", .model = model.id},
                 {.from = "ue0", .to = "gnb0", .model = model.id}};
    cfg.models.emplace(model.id, model);
    auto proc = std::make_unique<ocg::CpuChannelProcessor>();
    proc->prepare(cfg);
    return proc;
  };

  // (a) Single-tap tdl with tau=0, gain_db=0, phase_rad=0 must produce the
  // input bit-for-bit. The polyphase coefficients collapse to an impulse at
  // i=3 (sinc(0)=1, sinc(integer!=0)=0) and the complex gain is identity.
  {
    ocg::ModelConfig identity_model;
    identity_model.id = "tdl_identity";
    ocg::ModelStep step;
    step.type = ocg::ModelStepType::Tdl;
    step.taps = {ocg::TapSpec{.delay_samples = 0.0, .gain_db = 0.0, .phase_rad = 0.0}};
    step.taps_declared = true;
    identity_model.chain.push_back(step);

    auto proc = make_tdl_processor(identity_model, 8);
    const ocg::IqBuffer input = {{1.0F, 0.0F}, {0.0F, 1.0F}, {0.5F, -0.5F}, {-1.0F, 0.25F},
                                 {0.75F, 0.75F}, {0.0F, 0.0F}, {-0.5F, -0.5F}, {1.0F, 1.0F}};
    ocg::IqBuffer output(8);
    shape_link(*proc, "ue0", ocg::link_key({.from = "gnb0", .to = "ue0", .model = identity_model.id}),
               identity_model, input, output, 1000);
    require_near_buffer(input, output, "tdl(tau=0,gain=0,phase=0) must be identity");
  }

  // (b) was a bit-exact-vs-legacy [integer_delay 4, gain -3] check during the
  // Phase 1.0 -> 1.2 migration. The legacy step types are removed in Phase 1.3
  // (commit C), so the comparison no longer has anything to compare against;
  // the assertion served its purpose at the time and is dropped here. The
  // remaining three behaviour tests (identity, 3-tap cross-slot impulse,
  // sinusoid passband) cover the same tdl correctness surface on their own.

  // (c) 3-tap impulse response across two slots. An impulse at slot 0 sample
  // 0 should reappear at the three tap offsets with the expected gains -- the
  // tap with delay 12 lands in slot 1 (batch=8), exercising the cross-slot
  // ring directly. Other tap delays (3 and 7) land within slot 0.
  {
    ocg::ModelConfig three_tap;
    three_tap.id = "tdl_three_tap";
    ocg::ModelStep step;
    step.type = ocg::ModelStepType::Tdl;
    step.taps = {ocg::TapSpec{.delay_samples = 0.0, .gain_db = 0.0, .phase_rad = 0.0},
                 ocg::TapSpec{.delay_samples = 3.0, .gain_db = -6.0, .phase_rad = 0.0},
                 ocg::TapSpec{.delay_samples = 12.0, .gain_db = -12.0, .phase_rad = 0.0}};
    step.taps_declared = true;
    three_tap.chain.push_back(step);

    auto proc = make_tdl_processor(three_tap, 8);
    ocg::IqBuffer slot0_in(8, ocg::IqSample{0.0F, 0.0F});
    slot0_in[0] = {1.0F, 0.0F};
    const ocg::IqBuffer slot1_in(8, ocg::IqSample{0.0F, 0.0F});
    ocg::IqBuffer slot0_out(8), slot1_out(8);
    const std::string link = ocg::link_key({.from = "gnb0", .to = "ue0", .model = three_tap.id});
    shape_link(*proc, "ue0", link, three_tap, slot0_in, slot0_out, 1000);
    shape_link(*proc, "ue0", link, three_tap, slot1_in, slot1_out, 1000);

    const float g3 = static_cast<float>(std::pow(10.0, -6.0 / 20.0));   // ~0.501
    const float g12 = static_cast<float>(std::pow(10.0, -12.0 / 20.0)); // ~0.251
    require(near(slot0_out[0].i, 1.0F) && near(slot0_out[0].q, 0.0F),
            "tdl 3-tap: tau=0 impulse at slot 0 sample 0");
    require(near(slot0_out[3].i, g3) && near(slot0_out[3].q, 0.0F),
            "tdl 3-tap: tau=3 echo at slot 0 sample 3 with gain -6 dB");
    // Slot 0 has no contribution from the tau=12 tap (it lands in slot 1).
    require(near(slot0_out[7].i, 0.0F) && near(slot0_out[7].q, 0.0F),
            "tdl 3-tap: tau=12 echo must not appear in slot 0");
    // tau=12: impulse at global sample 0 -> echo at global sample 12, which is
    // slot 1 local index 4 (slot 1 spans global 8..15).
    require(near(slot1_out[4].i, g12) && near(slot1_out[4].q, 0.0F),
            "tdl 3-tap: tau=12 echo at slot 1 sample 4 with gain -12 dB (cross-slot)");
    for (std::size_t n = 0; n < slot1_out.size(); ++n) {
      if (n == 4) continue;
      require(near(slot1_out[n].i, 0.0F) && near(slot1_out[n].q, 0.0F),
              "tdl 3-tap: slot 1 zero outside of tau=12 echo position");
    }
  }

  // (d) Sinusoid passband: a complex sinusoid at f = fs/8 fed through
  // tdl(tau=2.5, gain_db=0) should come out with the same magnitude (filter
  // passband is flat at this frequency) and a phase shift of -2*pi*f*tau/fs.
  // This is the bit-exact-with-legacy contract REPLACEMENT after we upgraded
  // from 2-tap linear to 8-tap windowed sinc -- we cannot match legacy
  // output any more, but we can prove the fractional delay is correct.
  {
    ocg::ModelConfig frac_model;
    frac_model.id = "tdl_frac";
    ocg::ModelStep step;
    step.type = ocg::ModelStepType::Tdl;
    step.taps = {ocg::TapSpec{.delay_samples = 2.5, .gain_db = 0.0, .phase_rad = 0.0}};
    step.taps_declared = true;
    frac_model.chain.push_back(step);

    constexpr std::size_t batch = 64;
    auto proc = make_tdl_processor(frac_model, batch);

    // Complex tone at f = fs/8 over two slots so the filter's startup
    // transient (first ~kTdlFracFilterTaps samples) is fully past by the
    // time we measure.
    constexpr double tau = 2.5;
    constexpr double f_rel = 1.0 / 8.0; // cycles per sample
    constexpr double two_pi = 2.0 * std::numbers::pi;
    ocg::IqBuffer slot0_in(batch), slot1_in(batch);
    for (std::size_t n = 0; n < batch; ++n) {
      const double t0 = static_cast<double>(n);
      const double t1 = static_cast<double>(batch + n);
      slot0_in[n] = {static_cast<float>(std::cos(two_pi * f_rel * t0)),
                     static_cast<float>(std::sin(two_pi * f_rel * t0))};
      slot1_in[n] = {static_cast<float>(std::cos(two_pi * f_rel * t1)),
                     static_cast<float>(std::sin(two_pi * f_rel * t1))};
    }
    ocg::IqBuffer slot0_out(batch), slot1_out(batch);
    const std::string link = ocg::link_key({.from = "gnb0", .to = "ue0", .model = frac_model.id});
    shape_link(*proc, "ue0", link, frac_model, slot0_in, slot0_out, 1000);
    shape_link(*proc, "ue0", link, frac_model, slot1_in, slot1_out, 1000);

    // Measure on slot 1 well past the filter transient (n >= 16 of slot 1).
    double sum_err_mag = 0.0;
    double sum_phase_err = 0.0;
    std::size_t n_meas = 0;
    for (std::size_t n = 16; n < batch; ++n) {
      const double global_n = static_cast<double>(batch + n);
      // Expected: cos/sin(2*pi*f*(global_n - tau)).
      const double exp_arg = two_pi * f_rel * (global_n - tau);
      const float exp_i = static_cast<float>(std::cos(exp_arg));
      const float exp_q = static_cast<float>(std::sin(exp_arg));
      const float got_i = slot1_out[n].i;
      const float got_q = slot1_out[n].q;
      const double got_mag = std::sqrt(got_i * got_i + got_q * got_q);
      sum_err_mag += std::fabs(got_mag - 1.0);
      // Phase error: dot the expected and got unit-magnitude vectors;
      // dot product == cos(phase_error).
      const double dot = exp_i * got_i + exp_q * got_q;
      const double phase_err = std::acos(std::clamp(dot / std::max(got_mag, 1e-9), -1.0, 1.0));
      sum_phase_err += phase_err;
      ++n_meas;
    }
    const double mean_mag_err = sum_err_mag / static_cast<double>(n_meas);
    const double mean_phase_err = sum_phase_err / static_cast<double>(n_meas);
    require(mean_mag_err < 0.02,
            "tdl(tau=2.5) at f=fs/8: passband magnitude error < 2% (windowed sinc passband)");
    require(mean_phase_err < 0.02,
            "tdl(tau=2.5) at f=fs/8: phase delay matches tau=2.5 within 0.02 rad");
  }

#if OCUDU_GPU_CHANNEL_HAS_CUDA
  // ---- Phase 1.2 CPU<->CUDA bit-exact checks for tdl ----
  // The CUDA path runs the multi-tap convolution HOST-SIDE in stage_link()
  // before the H2D copy (mirrors the existing chain-leading delay flow), so
  // both backends call the same `apply_tdl_step` helper in delay.h. CPU<->CUDA
  // bit-exactness therefore reduces to "did the CUDA staging path call the
  // right helper" -- still worth asserting because the staged-buffer round-trip
  // through cudaMemcpyAsync + the no-op Scale(1.0) device step is the integration
  // surface where a regression would land.
  if (ocg::cuda_compiled()) {
    auto build_cuda_tdl_config = [](const ocg::ModelConfig& model,
                                    std::size_t batch_samples) {
      ocg::TopologyConfig cfg;
      cfg.runtime.backend = ocg::Backend::Cuda;
      cfg.runtime.batch_samples_auto = false;
      cfg.runtime.batch_samples = batch_samples;
      cfg.runtime.queue_samples = std::max<std::size_t>(614400, batch_samples * 8);
      cfg.devices = {{.id = "gnb0", .role = "gnb", .sample_rate_hz = 23040000,
                      .tx_endpoint = "tx0", .rx_endpoint = "rx0"},
                     {.id = "ue0", .role = "ue", .sample_rate_hz = 23040000,
                      .tx_endpoint = "tx1", .rx_endpoint = "rx1"}};
      cfg.links = {{.from = "gnb0", .to = "ue0", .model = model.id},
                   {.from = "ue0", .to = "gnb0", .model = model.id}};
      cfg.models.emplace(model.id, model);
      return cfg;
    };

    auto run_cpu_vs_cuda = [&](const ocg::ModelConfig& model,
                               const std::vector<ocg::IqBuffer>& slot_inputs,
                               const char* label) {
      const auto cfg = build_cuda_tdl_config(model, slot_inputs.front().size());
      // Same config drives both backends; the CPU reference ignores
      // runtime.backend and just uses the device/link/model layout.
      ocg::CpuChannelProcessor cpu;
      cpu.prepare(cfg);
      auto cuda = ocg::create_channel_processor(cfg);
      const std::string key = ocg::link_key({.from = "gnb0", .to = "ue0", .model = model.id});
      for (std::size_t s = 0; s < slot_inputs.size(); ++s) {
        ocg::IqBuffer cpu_out(slot_inputs[s].size());
        ocg::IqBuffer cuda_out(slot_inputs[s].size());
        shape_link(cpu, "ue0", key, model, slot_inputs[s], cpu_out, 23040000);
        shape_link(*cuda, "ue0", key, model, slot_inputs[s], cuda_out, 23040000);
        // Both backends should be bit-identical because both call
        // apply_tdl_step in delay.h -- but require_near_buffer's 1e-3
        // tolerance is the right gate (the device-side Scale 1.0 no-op
        // is exactly identity but float fp through cudaMemcpyAsync can
        // theoretically perturb if a subnormal slipped in; tolerance
        // covers that without hiding a real divergence).
        require_near_buffer(cpu_out, cuda_out, label);
      }
    };

    // (a) Identity: single tap, no delay, unit gain.
    {
      ocg::ModelConfig m;
      m.id = "tdl_cuda_identity";
      ocg::ModelStep step;
      step.type = ocg::ModelStepType::Tdl;
      step.taps = {ocg::TapSpec{.delay_samples = 0.0, .gain_db = 0.0, .phase_rad = 0.0}};
      step.taps_declared = true;
      m.chain.push_back(step);
      run_cpu_vs_cuda(m,
                      {{{1.0F, 0.0F}, {0.0F, 1.0F}, {0.5F, -0.5F}, {-1.0F, 0.25F},
                        {0.75F, 0.75F}, {0.0F, 0.0F}, {-0.5F, -0.5F}, {1.0F, 1.0F}}},
                      "CUDA tdl(tau=0,gain=0,phase=0) must match CPU bit-exactly");
    }

    // (b) Single-tap tdl(tau=4, gain_db=-3): exercises the windowed-sinc
    // collapse to impulse for integer tau and cross-slot delay_line continuity.
    {
      ocg::ModelConfig m;
      m.id = "tdl_cuda_delay_gain";
      ocg::ModelStep step;
      step.type = ocg::ModelStepType::Tdl;
      step.taps = {ocg::TapSpec{.delay_samples = 4.0, .gain_db = -3.0, .phase_rad = 0.0}};
      step.taps_declared = true;
      m.chain.push_back(step);
      const ocg::IqBuffer slot0 = {{1.0F, 0.0F}, {0.0F, 1.0F}, {0.5F, -0.5F}, {-1.0F, 0.25F},
                                   {0.75F, 0.75F}, {0.0F, 0.0F}, {-0.5F, -0.5F}, {1.0F, 1.0F}};
      const ocg::IqBuffer slot1 = {{0.25F, 0.25F}, {-0.25F, 0.5F}, {0.5F, 0.5F}, {1.0F, -1.0F},
                                   {0.0F, 0.0F}, {-0.5F, -0.5F}, {0.5F, 0.0F}, {0.0F, 0.5F}};
      run_cpu_vs_cuda(m, {slot0, slot1},
                      "CUDA tdl(tau=4,gain=-3) cross-slot must match CPU bit-exactly");
    }

    // (c) 3-tap impulse response with the tau=12 echo crossing into slot 1
    // sample 4 at batch=8: exercises multi-tap convolution and the ring update.
    {
      ocg::ModelConfig m;
      m.id = "tdl_cuda_three_tap";
      ocg::ModelStep step;
      step.type = ocg::ModelStepType::Tdl;
      step.taps = {ocg::TapSpec{.delay_samples = 0.0, .gain_db = 0.0, .phase_rad = 0.0},
                   ocg::TapSpec{.delay_samples = 3.0, .gain_db = -6.0, .phase_rad = 0.0},
                   ocg::TapSpec{.delay_samples = 12.0, .gain_db = -12.0, .phase_rad = 0.0}};
      step.taps_declared = true;
      m.chain.push_back(step);
      ocg::IqBuffer slot0(8, ocg::IqSample{0.0F, 0.0F});
      slot0[0] = {1.0F, 0.0F};
      const ocg::IqBuffer slot1(8, ocg::IqSample{0.0F, 0.0F});
      run_cpu_vs_cuda(m, {slot0, slot1},
                      "CUDA 3-tap tdl with cross-slot ring must match CPU bit-exactly");
    }

    // (d) Fractional tap tau=2.5 on a complex tone -- exercises the
    // windowed-sinc polyphase coefficients in flight.
    {
      ocg::ModelConfig m;
      m.id = "tdl_cuda_frac";
      ocg::ModelStep step;
      step.type = ocg::ModelStepType::Tdl;
      step.taps = {ocg::TapSpec{.delay_samples = 2.5, .gain_db = 0.0, .phase_rad = 0.0}};
      step.taps_declared = true;
      m.chain.push_back(step);
      constexpr std::size_t batch = 64;
      ocg::IqBuffer slot0(batch), slot1(batch);
      constexpr double two_pi = 2.0 * std::numbers::pi;
      constexpr double f_rel = 1.0 / 8.0;
      for (std::size_t n = 0; n < batch; ++n) {
        const double t0 = static_cast<double>(n);
        const double t1 = static_cast<double>(batch + n);
        slot0[n] = {static_cast<float>(std::cos(two_pi * f_rel * t0)),
                    static_cast<float>(std::sin(two_pi * f_rel * t0))};
        slot1[n] = {static_cast<float>(std::cos(two_pi * f_rel * t1)),
                    static_cast<float>(std::sin(two_pi * f_rel * t1))};
      }
      run_cpu_vs_cuda(m, {slot0, slot1},
                      "CUDA tdl(tau=2.5) sinusoid passband must match CPU bit-exactly");
    }

    // (e) Fading parity: tdl with Jakes fading enabled must produce
    // bit-identical output on CPU and CUDA across two slots. Both backends
    // seed prepare_tdl_fading_state with hash("<link_key>:fading:0"), and the
    // apply_tdl_step_fading helper in delay.h is the single source of truth
    // for the kernel math -- this test guards the staging-path integration
    // where the seed-derivation or the slot_start_samples accumulator could
    // diverge between backends. Includes a non-LOS tap (Rayleigh-Jakes) and
    // a LOS tap (Rician composition) to exercise both branches.
    {
      ocg::ModelConfig m;
      m.id = "tdl_cuda_fading";
      ocg::ModelStep step;
      step.type = ocg::ModelStepType::Tdl;
      step.taps = {ocg::TapSpec{.delay_samples = 0.0, .gain_db = 0.0,
                                .phase_rad = 0.0, .is_los = true,
                                .los_k_db = 6.0, .los_angle_rad = 0.0},
                   ocg::TapSpec{.delay_samples = 3.5, .gain_db = -3.0,
                                .phase_rad = 0.0}};
      step.taps_declared = true;
      step.fading_enabled = true;
      step.fading_f_d_max_hz = 50.0;
      step.fading_grid_us = 100.0;
      step.fading_spectrum = ocg::FadingSpectrum::Jakes;
      m.chain.push_back(step);
      constexpr std::size_t batch = 64;
      ocg::IqBuffer slot0(batch), slot1(batch);
      constexpr double two_pi = 2.0 * std::numbers::pi;
      constexpr double f_rel = 1.0 / 16.0;
      for (std::size_t n = 0; n < batch; ++n) {
        const double t0 = static_cast<double>(n);
        const double t1 = static_cast<double>(batch + n);
        slot0[n] = {static_cast<float>(std::cos(two_pi * f_rel * t0)),
                    static_cast<float>(std::sin(two_pi * f_rel * t0))};
        slot1[n] = {static_cast<float>(std::cos(two_pi * f_rel * t1)),
                    static_cast<float>(std::sin(two_pi * f_rel * t1))};
      }
      run_cpu_vs_cuda(m, {slot0, slot1},
                      "CUDA tdl fading (Jakes + LOS) must match CPU bit-exactly across slots");
    }

    // (f) Dispatch gate: a leading-tdl model must dispatch through the device
    // channel kernel (Phase 2 D2b path), and a leading-non-tdl model must fall
    // back to host-side stage_link. Without this assertion a regression in the
    // gate could silently revert every CUDA run to host staging -- parity
    // would still hold (host stage_link is the reference) but the 183x perf
    // win disappears with no test failure. This test gates that.
    {
      // Leading tdl: device channel kernel SHOULD engage.
      ocg::ModelConfig m;
      m.id = "tdl_cuda_dispatch_on";
      ocg::ModelStep step;
      step.type = ocg::ModelStepType::Tdl;
      step.taps = {ocg::TapSpec{.delay_samples = 0.0, .gain_db = 0.0, .phase_rad = 0.0}};
      step.taps_declared = true;
      m.chain.push_back(step);
      const auto cfg = build_cuda_tdl_config(m, 64);
      auto cuda = ocg::create_channel_processor(cfg);
      const std::string key = ocg::link_key({.from = "gnb0", .to = "ue0", .model = m.id});
      ocg::IqBuffer in(64, ocg::IqSample{1.0F, 0.0F});
      ocg::IqBuffer out(64);
      shape_link(*cuda, "ue0", key, m, in, out, 23040000);
      require(cuda->last_timings().used_device_channel,
              "CUDA dispatch gate: leading-tdl model must engage the device channel kernel");
    }
    {
      // Leading non-tdl (phase): device channel gate is OFF, host stage path.
      ocg::TopologyConfig cfg;
      cfg.runtime.backend = ocg::Backend::Cuda;
      cfg.runtime.batch_samples_auto = false;
      cfg.runtime.batch_samples = 64;
      cfg.runtime.queue_samples = 512;
      cfg.devices = {{.id = "gnb0", .role = "gnb", .sample_rate_hz = 23040000,
                      .tx_endpoint = "tx0", .rx_endpoint = "rx0"},
                     {.id = "ue0", .role = "ue", .sample_rate_hz = 23040000,
                      .tx_endpoint = "tx1", .rx_endpoint = "rx1"}};
      cfg.links = {{.from = "gnb0", .to = "ue0", .model = "leading_phase"},
                   {.from = "ue0", .to = "gnb0", .model = "leading_phase"}};
      ocg::ModelConfig m;
      m.id = "leading_phase";
      m.chain.push_back({.type = ocg::ModelStepType::Phase, .params = {{"phase_rad", 0.1}}});
      cfg.models.emplace(m.id, m);
      auto cuda = ocg::create_channel_processor(cfg);
      const std::string key = ocg::link_key({.from = "gnb0", .to = "ue0", .model = m.id});
      ocg::IqBuffer in(64, ocg::IqSample{1.0F, 0.0F});
      ocg::IqBuffer out(64);
      shape_link(*cuda, "ue0", key, m, in, out, 23040000);
      require(!cuda->last_timings().used_device_channel,
              "CUDA dispatch gate: leading-non-tdl model must fall back to host stage path");
    }

    // (g) D4 source rebuffering: a destination with TWO incoming edges from
    // the SAME source must produce the same output as the CPU reference.
    // The device path packs the shared source's IQ ONCE into host_source_iq
    // (D4) and both edges' kernels read from that single slot via their
    // DeviceLinkState::src_index. If the dedup or indexing is wrong, the
    // CPU↔CUDA parity assertion below catches it.
    {
      ocg::TopologyConfig cfg;
      cfg.runtime.backend = ocg::Backend::Cuda;
      cfg.runtime.batch_samples_auto = false;
      cfg.runtime.batch_samples = 8;
      cfg.runtime.queue_samples = 64;
      cfg.devices = {
          {.id = "gnb0", .role = "gnb", .sample_rate_hz = 23040000,
           .tx_endpoint = "tx0", .rx_endpoint = "rx0"},
          {.id = "ue0",  .role = "ue",  .sample_rate_hz = 23040000,
           .tx_endpoint = "tx1", .rx_endpoint = "rx1"}};
      // Two links from gnb0 → ue0 with different models — both edges share
      // the same source. D4 dedup means host_source_iq has 1 slot, not 2.
      cfg.links = {{.from = "gnb0", .to = "ue0", .model = "desired"},
                   {.from = "gnb0", .to = "ue0", .model = "crosstalk"}};
      ocg::ModelConfig desired;
      desired.id = "desired";
      ocg::ModelStep ds;
      ds.type = ocg::ModelStepType::Tdl;
      ds.taps = {ocg::TapSpec{.delay_samples = 0.0, .gain_db = 0.0, .phase_rad = 0.0}};
      ds.taps_declared = true;
      desired.chain.push_back(ds);
      ocg::ModelConfig crosstalk;
      crosstalk.id = "crosstalk";
      ocg::ModelStep cs;
      cs.type = ocg::ModelStepType::Tdl;
      cs.taps = {ocg::TapSpec{.delay_samples = 0.0, .gain_db = -6.0, .phase_rad = 0.0}};
      cs.taps_declared = true;
      crosstalk.chain.push_back(cs);
      cfg.models.emplace("desired", desired);
      cfg.models.emplace("crosstalk", crosstalk);

      ocg::CpuChannelProcessor cpu;
      cpu.prepare(cfg);
      auto cuda_proc = ocg::create_channel_processor(cfg);

      // Same source samples on both edges (since they read the same ring at
      // the same cursor in production).
      const ocg::IqBuffer src_iq = {{0.30F, -0.10F}, {0.45F, 0.25F}, {-0.20F, 0.60F}, {0.15F, -0.55F},
                                    {0.70F, 0.05F},  {-0.35F, 0.40F}, {0.50F, -0.30F}, {-0.65F, 0.20F}};

      // CPU reference: sum of (1.0 × src) + (0.5012 × src) per sample.
      ocg::IqBuffer ref_a(8), ref_b(8);
      shape_link(cpu, "ue0", "gnb0>ue0:desired",   desired,   src_iq, ref_a, 23040000);
      shape_link(cpu, "ue0", "gnb0>ue0:crosstalk", crosstalk, src_iq, ref_b, 23040000);
      ocg::IqBuffer ref(8);
      for (std::size_t s = 0; s != 8; ++s) ref[s] = ref_a[s] + ref_b[s];

      std::vector<ocg::SuperpositionInput> edges = {
          {.link_key = "gnb0>ue0:desired",   .model = &desired,   .samples = src_iq},
          {.link_key = "gnb0>ue0:crosstalk", .model = &crosstalk, .samples = src_iq}};
      ocg::IqBuffer cuda_out(8);
      cuda_proc->process_superposition("ue0", edges, nullptr, 23040000, cuda_out);

      require(cuda_proc->last_timings().used_device_channel,
              "D4: same-source 2-edge topology must engage the device-channel kernel");
      require_near_buffer(ref, cuda_out,
                          "D4: same-source 2-edge CUDA output must match CPU sum at 1e-3");
    }
  }
#endif

  // ---- M1.6: multi-row output (Nr > 1) ------------------------------------
  //
  // The marker test in miniature: distinguishable IQ on each TX port, a SWAP
  // matrix, and the requirement that row r carries the signal of tx port
  // 1 - r. A port-order mistake, a row-boundary mistake, or a source-dedup
  // mistake all fail here, and none of them would show up as a crash.
  {
    ocg::TopologyConfig cfg;
    cfg.runtime.backend = ocg::Backend::Cpu;
    cfg.runtime.batch_samples_auto = false;
    cfg.runtime.batch_samples = 8;
    cfg.runtime.queue_samples = 64;
    int port = 2000;
    for (const char* id : {"gnb_p0", "gnb_p1", "ue_p0", "ue_p1"}) {
      ocg::DeviceConfig d;
      d.id = id;
      d.sample_rate_hz = 23040000;
      d.tx_endpoint = "tcp://127.0.0.1:" + std::to_string(port++);
      d.rx_endpoint = "tcp://127.0.0.1:" + std::to_string(port++);
      cfg.devices.push_back(d);
    }
    ocg::RadioNodeConfig gnb;
    gnb.id = "gnb";
    gnb.tx_ports = {"gnb_p0", "gnb_p1"};
    gnb.rx_ports = {"gnb_p0", "gnb_p1"};
    ocg::RadioNodeConfig ue;
    ue.id = "ue";
    ue.tx_ports = {"ue_p0", "ue_p1"};
    ue.rx_ports = {"ue_p0", "ue_p1"};
    cfg.radio_nodes = {gnb, ue};
    // Both directions: the validator requires every device to be reachable as
    // a source and as a destination, which is a real relay invariant.
    ocg::LinkConfig dl;
    dl.from = "gnb";
    dl.to = "ue";
    dl.model = "h_swap";
    ocg::LinkConfig ul;
    ul.from = "ue";
    ul.to = "gnb";
    ul.model = "h_swap";
    cfg.links = {dl, ul};

    ocg::ModelConfig h;
    h.id = "h_swap";
    ocg::ModelStep step;
    step.type = ocg::ModelStepType::Tdl;
    step.taps = {ocg::TapSpec{.delay_samples = 0.0, .gain_db = 0.0, .phase_rad = 0.0}};
    step.taps_declared = true;
    h.chain.push_back(step);
    h.fixed_mimo_declared = true;
    h.fixed_mimo = {{.tap = 0, .rx = 0, .tx = 1, .real = 1.0, .imag = 0.0},
                    {.tap = 0, .rx = 1, .tx = 0, .real = 1.0, .imag = 0.0}};
    cfg.models.emplace("h_swap", h);

    require(ocg::validate_config(cfg).empty(), "M1.6: 2x2 swap topology validates");
    ocg::expand_fixed_mimo_models(cfg);
    const auto resolved = ocg::resolve_topology(cfg);
    require(resolved.lanes.size() == 4,
            "M1.6: swap H keeps two off-diagonal lanes per direction");

    // Distinguishable per-port markers: port 0 carries +1, port 1 carries -1.
    ocg::IqBuffer port0(8, ocg::IqSample{1.0F, 0.0F});
    ocg::IqBuffer port1(8, ocg::IqSample{-1.0F, 0.0F});

    std::vector<ocg::SuperpositionInput> lanes;
    for (const auto& lane : resolved.lanes) {
      if (lane.dst_node != "ue") {
        continue;
      }
      const auto* model = ocg::find_model(cfg, lane.model_id);
      require(model != nullptr, "M1.6: lane model exists after expansion");
      lanes.push_back({.link_key = lane.key,
                       .model = model,
                       .samples = lane.tx_port == 0 ? std::span<const ocg::IqSample>(port0)
                                                    : std::span<const ocg::IqSample>(port1),
                       .rx_port = lane.rx_port,
                       .tx_port = lane.tx_port});
    }

    ocg::IqBuffer row0(8), row1(8);
    std::span<ocg::IqSample> rows[2] = {row0, row1};

    ocg::CpuChannelProcessor cpu;
    cpu.prepare(cfg);
    cpu.process_superposition("ue", lanes, nullptr, 23040000,
                              std::span<std::span<ocg::IqSample>>(rows));

    for (std::size_t k = 0; k != 8; ++k) {
      require(std::abs(row0[k].i - (-1.0F)) < 1e-6F && std::abs(row0[k].q) < 1e-6F,
              "M1.6: swap H puts tx port 1's marker on row 0");
      require(std::abs(row1[k].i - 1.0F) < 1e-6F && std::abs(row1[k].q) < 1e-6F,
              "M1.6: swap H puts tx port 0's marker on row 1");
    }

#if OCUDU_GPU_CHANNEL_HAS_CUDA
    if (ocg::cuda_compiled()) {
      ocg::TopologyConfig cuda_cfg = cfg;
      cuda_cfg.runtime.backend = ocg::Backend::Cuda;
      auto cuda_proc = ocg::create_channel_processor(cuda_cfg);
      ocg::IqBuffer c0(8), c1(8);
      std::span<ocg::IqSample> crows[2] = {c0, c1};
      cuda_proc->process_superposition("ue", lanes, nullptr, 23040000,
                                       std::span<std::span<ocg::IqSample>>(crows));
      require_near_buffer(row0, c0, "M1.6: CUDA row 0 matches CPU at 1e-3");
      require_near_buffer(row1, c1, "M1.6: CUDA row 1 matches CPU at 1e-3");
    }
#endif
  }

  // ---- M1.7: asymmetric dimensions and the 1x1 bit-exact gate --------------
  //
  // Builds a Nt x Nr topology with a chosen matrix, runs one slot, and returns
  // the rows. Nt != Nr is where an implementation that quietly assumes a square
  // matrix, or that swaps the two dimensions, comes apart.
  const auto run_matrix = [&](int nt, int nr,
                              const std::vector<ocg::MimoCoefficient>& coefficients,
                              const std::vector<ocg::IqBuffer>& per_tx_port,
                              std::vector<ocg::IqBuffer>& rows_out) {
    ocg::TopologyConfig cfg;
    cfg.runtime.backend = ocg::Backend::Cpu;
    cfg.runtime.batch_samples_auto = false;
    cfg.runtime.batch_samples = 4;
    cfg.runtime.queue_samples = 64;
    int port = 4000;
    ocg::RadioNodeConfig gnb;
    gnb.id = "gnb";
    ocg::RadioNodeConfig ue;
    ue.id = "ue";
    const auto add_device = [&](const std::string& id) {
      ocg::DeviceConfig d;
      d.id = id;
      d.sample_rate_hz = 23040000;
      d.tx_endpoint = "tcp://127.0.0.1:" + std::to_string(port++);
      d.rx_endpoint = "tcp://127.0.0.1:" + std::to_string(port++);
      cfg.devices.push_back(d);
    };
    for (int t = 0; t != nt; ++t) {
      const std::string id = "gnb_t" + std::to_string(t);
      add_device(id);
      gnb.tx_ports.push_back(id);
      gnb.rx_ports.push_back(id); // every device must be reachable both ways
    }
    for (int r = 0; r != nr; ++r) {
      const std::string id = "ue_r" + std::to_string(r);
      add_device(id);
      ue.rx_ports.push_back(id);
      ue.tx_ports.push_back(id);
    }
    cfg.radio_nodes = {gnb, ue};
    ocg::LinkConfig dl;
    dl.from = "gnb";
    dl.to = "ue";
    dl.model = "h";
    ocg::LinkConfig ul;
    ul.from = "ue";
    ul.to = "gnb";
    ul.model = "unit";
    cfg.links = {dl, ul};

    ocg::ModelStep step;
    step.type = ocg::ModelStepType::Tdl;
    step.taps = {ocg::TapSpec{.delay_samples = 0.0, .gain_db = 0.0, .phase_rad = 0.0}};
    step.taps_declared = true;
    ocg::ModelConfig h;
    h.id = "h";
    h.chain.push_back(step);
    h.fixed_mimo_declared = true;
    h.fixed_mimo = coefficients;
    ocg::ModelConfig unit;
    unit.id = "unit";
    unit.chain.push_back(step);
    cfg.models.emplace("h", h);
    cfg.models.emplace("unit", unit);

    const auto errors = ocg::validate_config(cfg);
    require(errors.empty(), "M1.7: asymmetric topology validates");
    ocg::expand_fixed_mimo_models(cfg);
    const auto resolved = ocg::resolve_topology(cfg);

    std::vector<ocg::SuperpositionInput> lanes;
    for (const auto& lane : resolved.lanes) {
      if (lane.dst_node != "ue") {
        continue;
      }
      lanes.push_back({.link_key = lane.key,
                       .model = ocg::find_model(cfg, lane.model_id),
                       .samples = per_tx_port.at(static_cast<std::size_t>(lane.tx_port)),
                       .rx_port = lane.rx_port,
                       .tx_port = lane.tx_port});
    }

    rows_out.assign(static_cast<std::size_t>(nr), ocg::IqBuffer(4));
    std::vector<std::span<ocg::IqSample>> rows;
    for (auto& row : rows_out) {
      rows.push_back(row);
    }
    ocg::CpuChannelProcessor cpu;
    cpu.prepare(cfg);
    cpu.process_superposition("ue", lanes, nullptr, 23040000,
                              std::span<std::span<ocg::IqSample>>(rows));
  };

  {
    // 2x1: two TX ports fan into ONE row. Distinct markers, so a lane silently
    // dropped or double-counted changes the sum.
    const ocg::IqBuffer tx0(4, ocg::IqSample{1.0F, 0.0F});
    const ocg::IqBuffer tx1(4, ocg::IqSample{0.0F, 1.0F});
    std::vector<ocg::IqBuffer> rows;
    run_matrix(2, 1,
               {{.tap = 0, .rx = 0, .tx = 0, .real = 1.0, .imag = 0.0},
                {.tap = 0, .rx = 0, .tx = 1, .real = 1.0, .imag = 0.0}},
               {tx0, tx1}, rows);
    require(rows.size() == 1, "M1.7: a 2x1 link produces exactly one row");
    for (std::size_t k = 0; k != 4; ++k) {
      require(std::abs(rows[0][k].i - 1.0F) < 1e-6F && std::abs(rows[0][k].q - 1.0F) < 1e-6F,
              "M1.7: 2x1 sums both TX ports into the single row");
    }
  }

  {
    // 1x2: one TX port feeds TWO rows through different coefficients. Row 1's
    // 0+1j must rotate by +pi/2, which a square-matrix assumption would miss.
    const ocg::IqBuffer tx0(4, ocg::IqSample{1.0F, 0.0F});
    std::vector<ocg::IqBuffer> rows;
    run_matrix(1, 2,
               {{.tap = 0, .rx = 0, .tx = 0, .real = 1.0, .imag = 0.0},
                {.tap = 0, .rx = 1, .tx = 0, .real = 0.0, .imag = 1.0}},
               {tx0}, rows);
    require(rows.size() == 2, "M1.7: a 1x2 link produces exactly two rows");
    for (std::size_t k = 0; k != 4; ++k) {
      require(std::abs(rows[0][k].i - 1.0F) < 1e-6F && std::abs(rows[0][k].q) < 1e-6F,
              "M1.7: 1x2 row 0 passes the source through");
      require(std::abs(rows[1][k].i) < 1e-6F && std::abs(rows[1][k].q - 1.0F) < 1e-6F,
              "M1.7: 1x2 row 1 rotates the source by +pi/2");
    }
  }

  {
    // 1x1 bit-exact gate. A declared single-port topology and the implicit
    // lowering of the same devices must key their channel state identically, so
    // the OUTPUT must be bit-identical -- not close. If lane expansion ever
    // changes the legacy path's meaning, this is where it shows.
    const auto build = [&](bool declared) {
      ocg::TopologyConfig cfg;
      cfg.runtime.backend = ocg::Backend::Cpu;
      cfg.runtime.batch_samples_auto = false;
      cfg.runtime.batch_samples = 8;
      cfg.runtime.queue_samples = 64;
      int port = 5000;
      for (const char* id : {"gnb0", "ue0"}) {
        ocg::DeviceConfig d;
        d.id = id;
        d.sample_rate_hz = 23040000;
        d.tx_endpoint = "tcp://127.0.0.1:" + std::to_string(port++);
        d.rx_endpoint = "tcp://127.0.0.1:" + std::to_string(port++);
        cfg.devices.push_back(d);
      }
      if (declared) {
        ocg::RadioNodeConfig a;
        a.id = "gnb0_node";
        a.tx_ports = {"gnb0"};
        a.rx_ports = {"gnb0"};
        ocg::RadioNodeConfig b;
        b.id = "ue0_node";
        b.tx_ports = {"ue0"};
        b.rx_ports = {"ue0"};
        cfg.radio_nodes = {a, b};
      }
      ocg::LinkConfig dl;
      dl.from = declared ? "gnb0_node" : "gnb0";
      dl.to = declared ? "ue0_node" : "ue0";
      dl.model = "chan";
      ocg::LinkConfig ul;
      ul.from = declared ? "ue0_node" : "ue0";
      ul.to = declared ? "gnb0_node" : "gnb0";
      ul.model = "chan";
      cfg.links = {dl, ul};
      ocg::ModelConfig chan;
      chan.id = "chan";
      ocg::ModelStep st;
      st.type = ocg::ModelStepType::Tdl;
      st.taps = {ocg::TapSpec{.delay_samples = 0.0, .gain_db = -3.0, .phase_rad = 0.25}};
      st.taps_declared = true;
      chan.chain.push_back(st);
      ocg::ModelStep cfo;
      cfo.type = ocg::ModelStepType::Cfo;
      cfo.params["cfo_hz"] = 125.0;
      chan.chain.push_back(cfo);
      cfg.models.emplace("chan", chan);
      require(ocg::validate_config(cfg).empty(), "M1.7: 1x1 bit-exact fixture validates");
      return cfg;
    };

    const ocg::IqBuffer src = {{0.30F, -0.10F}, {0.45F, 0.25F}, {-0.20F, 0.60F}, {0.15F, -0.55F},
                               {0.70F, 0.05F}, {-0.35F, 0.40F}, {0.50F, -0.30F}, {-0.65F, 0.20F}};
    const auto run = [&](bool declared) {
      ocg::TopologyConfig cfg = build(declared);
      const auto resolved = ocg::resolve_topology(cfg);
      const std::string dst = declared ? "ue0_node" : "ue0";
      std::vector<ocg::SuperpositionInput> lanes;
      for (const auto& lane : resolved.lanes) {
        if (lane.dst_node != dst) continue;
        lanes.push_back({.link_key = lane.key,
                         .model = ocg::find_model(cfg, lane.model_id),
                         .samples = src,
                         .rx_port = lane.rx_port,
                         .tx_port = lane.tx_port});
      }
      ocg::IqBuffer out(8);
      ocg::CpuChannelProcessor cpu;
      cpu.prepare(cfg);
      // Two slots, so the CFO phase accumulator and the delay line both carry
      // across -- a state-keying difference would only show from slot 2.
      cpu.process_superposition(dst, lanes, nullptr, 23040000, out);
      cpu.process_superposition(dst, lanes, nullptr, 23040000, out);
      return out;
    };

    const ocg::IqBuffer implicit_out = run(false);
    const ocg::IqBuffer declared_out = run(true);
    for (std::size_t k = 0; k != implicit_out.size(); ++k) {
      require(implicit_out[k].i == declared_out[k].i && implicit_out[k].q == declared_out[k].q,
              "M1.7: a declared 1x1 node is BIT-identical to implicit lowering");
    }
  }

  // ---- Phase 1.4b: fading kernel behaviour tests ----
  // Each test builds its own CpuChannelProcessor (process_superposition path)
  // so the per-link state is isolated.
  auto build_fading_processor = [](const ocg::ModelConfig& model,
                                   std::size_t batch,
                                   std::uint64_t sample_rate)
      -> std::unique_ptr<ocg::CpuChannelProcessor> {
    ocg::TopologyConfig cfg;
    cfg.runtime.batch_samples_auto = false;
    cfg.runtime.batch_samples = batch;
    cfg.runtime.queue_samples = std::max<std::size_t>(614400, batch * 8);
    cfg.devices = {{.id = "gnb0", .role = "gnb", .sample_rate_hz = sample_rate,
                    .tx_endpoint = "tx0", .rx_endpoint = "rx0"},
                   {.id = "ue0", .role = "ue", .sample_rate_hz = sample_rate,
                    .tx_endpoint = "tx1", .rx_endpoint = "rx1"}};
    cfg.links = {{.from = "gnb0", .to = "ue0", .model = model.id},
                 {.from = "ue0", .to = "gnb0", .model = model.id}};
    cfg.models.emplace(model.id, model);
    auto proc = std::make_unique<ocg::CpuChannelProcessor>();
    proc->prepare(cfg);
    return proc;
  };

  // (a) Stationary case: f_d_max_hz = 0 reduces to the static-tap result
  // (g_k(t) collapses to a constant whose magnitude equals 1 on average; with
  // zero Doppler the sub-ray sinusoids degenerate to constants
  // exp(j*phi_{k,m}) and the sum-of-sinusoids is deterministic. For zero
  // Doppler the output is therefore identical across slots).
  {
    ocg::ModelConfig m;
    m.id = "tdl_fading_stationary";
    ocg::ModelStep step;
    step.type = ocg::ModelStepType::Tdl;
    step.taps = {ocg::TapSpec{.delay_samples = 0.0, .gain_db = 0.0,
                              .phase_rad = 0.0}};
    step.taps_declared = true;
    step.fading_enabled = true;
    step.fading_f_d_max_hz = 0.0;  // stationary
    step.fading_spectrum = ocg::FadingSpectrum::Jakes;
    m.chain.push_back(step);

    auto proc = build_fading_processor(m, 32, 1000);
    ocg::IqBuffer in(32);
    for (std::size_t n = 0; n < in.size(); ++n) {
      in[n] = {static_cast<float>(0.5 * std::cos(0.1 * n)),
               static_cast<float>(0.5 * std::sin(0.1 * n))};
    }
    ocg::IqBuffer out0(32), out1(32);
    const std::string link = ocg::link_key({.from = "gnb0", .to = "ue0", .model = m.id});
    shape_link(*proc, "ue0", link, m, in, out0, 1000);
    shape_link(*proc, "ue0", link, m, in, out1, 1000);
    // f_d_max = 0 -> g_k(t) is a fixed complex constant; processing the same
    // input across two slots must produce the same output (after the initial-
    // history transient). The kernel's output magnitude is bounded above by
    // 1 (g_k constant complex of |g| <= 1) times the input.
    for (std::size_t n = 4; n < 32; ++n) {  // skip transient
      require(near(out0[n].i, out1[n].i) && near(out0[n].q, out1[n].q),
              "fading f_d_max=0: output must be slot-invariant once steady state");
    }
  }

  // (b) Determinism: two independent processors with the same model / link
  // key must produce bit-identical fading output across slots.
  {
    ocg::ModelConfig m;
    m.id = "tdl_fading_det";
    ocg::ModelStep step;
    step.type = ocg::ModelStepType::Tdl;
    step.taps = {ocg::TapSpec{.delay_samples = 0.0, .gain_db = 0.0,
                              .phase_rad = 0.0},
                 ocg::TapSpec{.delay_samples = 3.0, .gain_db = -3.0,
                              .phase_rad = 0.0}};
    step.taps_declared = true;
    step.fading_enabled = true;
    step.fading_f_d_max_hz = 50.0;
    step.fading_spectrum = ocg::FadingSpectrum::Jakes;
    m.chain.push_back(step);

    auto proc1 = build_fading_processor(m, 32, 1000);
    auto proc2 = build_fading_processor(m, 32, 1000);
    ocg::IqBuffer in(32);
    for (std::size_t n = 0; n < in.size(); ++n) {
      in[n] = {1.0F, 0.0F};
    }
    ocg::IqBuffer out1(32), out2(32);
    const std::string link = ocg::link_key({.from = "gnb0", .to = "ue0", .model = m.id});
    shape_link(*proc1, "ue0", link, m, in, out1, 1000);
    shape_link(*proc2, "ue0", link, m, in, out2, 1000);
    // Determinism is a same-backend / same-seed contract: floats must be
    // byte-identical, not "close". Tightened from 1e-3 to bit-exact so a
    // recompile that subtly perturbs the order of operations is caught
    // instead of being absorbed by the numeric tolerance.
    require_equal_buffer(out1, out2,
                         "fading determinism: same model + same link key -> bit-identical output");
    // Run a second slot through proc1; redo proc2 with two consecutive slots
    // and assert the second slot matches too (slot_start_samples accumulator
    // must advance deterministically).
    ocg::IqBuffer out1b(32);
    shape_link(*proc1, "ue0", link, m, in, out1b, 1000);
    auto proc3 = build_fading_processor(m, 32, 1000);
    ocg::IqBuffer out3a(32), out3b(32);
    shape_link(*proc3, "ue0", link, m, in, out3a, 1000);
    shape_link(*proc3, "ue0", link, m, in, out3b, 1000);
    require_equal_buffer(out1b, out3b,
                         "fading determinism: cross-slot accumulator must be bit-reproducible");
  }

  // (c) LOS at K = +Inf (effectively): when the Rician K-factor is very
  // large the tap is dominated by the deterministic specular, so the output
  // magnitude on a unit-power input should approach 1 (sqrt(K/(K+1)) ~ 1).
  // At f_d_max = 0 the specular is also stationary in time -- magnitude is
  // perfectly steady.
  {
    ocg::ModelConfig m;
    m.id = "tdl_fading_strong_los";
    ocg::ModelStep step;
    step.type = ocg::ModelStepType::Tdl;
    step.taps = {ocg::TapSpec{.delay_samples = 0.0,
                              .gain_db = 0.0,
                              .phase_rad = 0.0,
                              .is_los = true,
                              .los_k_db = 40.0,        // K ~ 10000 -> sqrt(K/(K+1)) ~ 0.99995
                              .los_angle_rad = 0.0}};
    step.taps_declared = true;
    step.fading_enabled = true;
    step.fading_f_d_max_hz = 0.0;
    step.fading_spectrum = ocg::FadingSpectrum::Jakes;
    m.chain.push_back(step);

    auto proc = build_fading_processor(m, 32, 1000);
    ocg::IqBuffer in(32, ocg::IqSample{1.0F, 0.0F});
    ocg::IqBuffer out(32);
    const std::string link = ocg::link_key({.from = "gnb0", .to = "ue0", .model = m.id});
    shape_link(*proc, "ue0", link, m, in, out, 1000);
    // Magnitudes should all be very close to 1 (within ~1% -- the LOS factor
    // sqrt(K/(K+1)) is ~0.99995, plus the tiny Rayleigh component of
    // magnitude ~1/sqrt(K+1) ~ 0.01 with random phase).
    for (std::size_t n = 4; n < 32; ++n) {
      const double mag = std::sqrt(static_cast<double>(out[n].i) * out[n].i +
                                    static_cast<double>(out[n].q) * out[n].q);
      require(mag > 0.95 && mag < 1.05,
              "LOS K=40 dB: output magnitude on unit input must be ~1");
    }
  }

  // (d) Jakes' autocorrelation matches the Bessel curve -- judged over an
  // ENSEMBLE of lanes, not over one.
  //
  // For a Rayleigh-Jakes process g(t) with isotropic scatterers the temporal
  // autocorrelation is R_g(tau) = J_0(2*pi * f_d_max * tau). That is an
  // ensemble statement: it is what the sub-ray angles average to. A single
  // realisation with M = 20 sub-rays follows its OWN curve,
  //     R(tau) = (1/M) * sum_m cos(2*pi * f_d_max * cos(alpha_m) * tau),
  // exactly -- and that curve sits far from J_0. Measured across 16 lanes at
  // tau = 5 ms the per-lane error spans -0.32 .. +0.51, while the 16-lane mean
  // lands at 0.02. So a single-lane J_0 gate at +/- 0.15 grades which angles
  // were drawn rather than whether the generator is right; the pre-M2 version
  // of this test passed on the luck of its seed, and re-seeding in M2.2 (a
  // change that cannot touch the generator) was enough to fail it.
  //
  // M2 makes the honest form cheap: the lanes of one physical link are
  // independent realisations of the same channel, so the ensemble to average
  // over is right there. This runs a 1 x 16 link and drives the single TX port
  // with DC, so each of the 16 output rows IS one lane's g(t), and asserts:
  //   1. per lane, the empirical autocorrelation matches THAT lane's own
  //      sum-of-sinusoids prediction. This is the tight check, and it also
  //      pins the seed derivation: the prediction is computed from the angles
  //      lane_fading_seed(physical_link_seed(link), r, t, step) draws, so a
  //      backend that seeded a lane any other way fails here.
  //   2. over the 16 lanes, the mean matches J_0 within +/- 0.15 -- the
  //      distributional property the milestone gate names.
  {
    constexpr int n_rx = 16;
    constexpr std::uint64_t sample_rate_hz = 100000;
    constexpr std::size_t batch = 2000;   // 20 ms per slot
    constexpr std::size_t n_slots = 600;  // 12 s per lane => 1200 fading cycles
    constexpr std::size_t max_lag = 500;

    ocg::ModelConfig m;
    m.id = "tdl_fading_autocorr";
    ocg::ModelStep step;
    step.type = ocg::ModelStepType::Tdl;
    step.taps = {ocg::TapSpec{.delay_samples = 0.0, .gain_db = 0.0,
                              .phase_rad = 0.0}};
    step.taps_declared = true;
    step.fading_enabled = true;
    step.fading_f_d_max_hz = 100.0;
    step.fading_grid_us = 100.0;
    step.fading_spectrum = ocg::FadingSpectrum::Jakes;
    m.chain.push_back(step);

    ocg::TopologyConfig cfg;
    cfg.runtime.backend = ocg::Backend::Cpu;
    cfg.runtime.batch_samples_auto = false;
    cfg.runtime.batch_samples = batch;
    cfg.runtime.queue_samples = batch * 8;
    ocg::RadioNodeConfig gnb;
    gnb.id = "gnb";
    ocg::RadioNodeConfig ue;
    ue.id = "ue";
    int port = 7000;
    const auto add_device = [&](const std::string& id) {
      ocg::DeviceConfig d;
      d.id = id;
      d.sample_rate_hz = sample_rate_hz;
      d.tx_endpoint = "tcp://127.0.0.1:" + std::to_string(port++);
      d.rx_endpoint = "tcp://127.0.0.1:" + std::to_string(port++);
      cfg.devices.push_back(d);
    };
    add_device("gnb_p0");
    gnb.tx_ports = {"gnb_p0"};
    gnb.rx_ports = {"gnb_p0"};
    for (int r = 0; r != n_rx; ++r) {
      const std::string id = "ue_p" + std::to_string(r);
      add_device(id);
      ue.rx_ports.push_back(id);
      ue.tx_ports.push_back(id); // every device must be reachable both ways
    }
    cfg.radio_nodes = {gnb, ue};
    cfg.links = {{.from = "gnb", .to = "ue", .model = m.id},
                 {.from = "ue", .to = "gnb", .model = m.id}};
    cfg.models.emplace(m.id, m);
    require(ocg::validate_config(cfg).empty(),
            "Bessel ensemble: 1 x 16 topology validates");

    const auto resolved = ocg::resolve_topology(cfg);
    std::vector<ocg::SuperpositionInput> lanes;
    ocg::IqBuffer dc_in(batch, ocg::IqSample{1.0F, 0.0F});
    for (const auto& lane : resolved.lanes) {
      if (lane.dst_node != "ue") {
        continue;
      }
      lanes.push_back({.link_key = lane.key,
                       .model = ocg::find_model(cfg, lane.model_id),
                       .samples = std::span<const ocg::IqSample>(dc_in),
                       .rx_port = lane.rx_port,
                       .tx_port = lane.tx_port});
    }
    require(lanes.size() == n_rx, "Bessel ensemble: one lane per RX port");

    ocg::CpuChannelProcessor proc;
    proc.prepare(cfg);

    // Streamed autocorrelation: keep only the previous slot's last `max_lag`
    // samples per lane instead of the whole 4 s series, and accumulate the
    // lag products slot by slot. Pairs are counted once, when the LATER of the
    // two samples falls in the slot being processed.
    const std::size_t lag_list[] = {0, 100, 300, 500};
    constexpr std::size_t n_lags = 4;
    std::vector<std::array<double, n_lags>> sums(n_rx, std::array<double, n_lags>{});
    std::vector<std::array<double, n_lags>> pairs(n_rx, std::array<double, n_lags>{});
    std::vector<ocg::IqBuffer> tails(n_rx);
    std::vector<ocg::IqBuffer> rows_buf(n_rx, ocg::IqBuffer(batch));
    std::vector<std::span<ocg::IqSample>> rows;
    for (auto& row : rows_buf) {
      rows.emplace_back(row.data(), row.size());
    }
    ocg::IqBuffer work(max_lag + batch);
    for (std::size_t s = 0; s != n_slots; ++s) {
      proc.process_superposition("ue", lanes, nullptr, sample_rate_hz,
                                 std::span<std::span<ocg::IqSample>>(rows));
      for (int r = 0; r != n_rx; ++r) {
        const std::size_t tail_len = tails[r].size();
        std::copy(tails[r].begin(), tails[r].end(), work.begin());
        std::copy(rows_buf[r].begin(), rows_buf[r].end(),
                  work.begin() + static_cast<std::ptrdiff_t>(tail_len));
        for (std::size_t li = 0; li != n_lags; ++li) {
          const std::size_t lag = lag_list[li];
          for (std::size_t j = 0; j != batch; ++j) {
            const std::size_t later = tail_len + j;
            if (later < lag) {
              continue; // no earlier sample retained for this pair yet
            }
            const ocg::IqSample& a = work[later - lag];
            const ocg::IqSample& b = work[later];
            sums[r][li] += static_cast<double>(a.i) * b.i +
                           static_cast<double>(a.q) * b.q;
            pairs[r][li] += 1.0;
          }
        }
        tails[r].assign(rows_buf[r].end() - static_cast<std::ptrdiff_t>(max_lag),
                        rows_buf[r].end());
      }
    }

    // J_0(2 pi f_d tau) at f_d = 100 Hz, for the three lags above:
    //   tau = 1 ms -> arg = 0.628 -> J_0 =  0.904
    //   tau = 3 ms -> arg = 1.885 -> J_0 =  0.305
    //   tau = 5 ms -> arg = pi    -> J_0 = -0.304
    const double bessel_expected[n_lags] = {1.0, 0.904, 0.305, -0.304};
    const std::uint64_t link_seed = ocg::physical_link_seed(
        ocg::link_key({.from = "gnb", .to = "ue", .model = m.id}));
    std::array<double, n_lags> ensemble{};
    for (int r = 0; r != n_rx; ++r) {
      const double r0 = sums[r][0] / pairs[r][0];
      require(r0 > 0.7 && r0 < 1.3,
              "Bessel ensemble: each lane's R(0) (= mean tap power) must be ~ 1");

      // This lane's own realisation, from the angles its documented seed draws.
      ocg::TdlFadingState state;
      ocg::prepare_tdl_fading_state(
          m.chain.front(),
          ocg::lane_fading_seed(link_seed, r, /*tx_port=*/0, /*step_index=*/0),
          state);
      require(state.tap_alpha.size() == 1,
              "Bessel ensemble: single-tap fading state");
      for (std::size_t li = 1; li != n_lags; ++li) {
        const double tau = static_cast<double>(lag_list[li]) /
                           static_cast<double>(sample_rate_hz);
        double predicted = 0.0;
        for (int mm = 0; mm != ocg::kTdlFadingSinusoids; ++mm) {
          predicted += std::cos(2.0 * std::numbers::pi * step.fading_f_d_max_hz *
                                std::cos(state.tap_alpha[0][mm]) * tau);
        }
        predicted /= static_cast<double>(ocg::kTdlFadingSinusoids);
        const double measured = (sums[r][li] / pairs[r][li]) / r0;
        // 0.10 is set by measurement, not taste: over these 16 lanes the
        // worst deviation is 0.058, and it is finite-window noise from
        // near-equal sub-ray pairs beating slowly (it falls off roughly as
        // 1/sqrt(T), so 12 s of data is where the margin stops being cheap).
        // The spread this must still discriminate is the per-lane departure
        // from J_0, which reaches 0.3 -- so the gate keeps its teeth.
        require(std::fabs(measured - predicted) < 0.10,
                "Bessel ensemble: a lane's autocorrelation must match the "
                "sum-of-sinusoids of the angles its seed draws");
        ensemble[li] += measured / static_cast<double>(n_rx);
      }
    }
    for (std::size_t li = 1; li != n_lags; ++li) {
      require(std::fabs(ensemble[li] - bessel_expected[li]) < 0.15,
              "Bessel ensemble: the lane-averaged autocorrelation must match "
              "J_0(2*pi*f_d*tau) within 0.15");
    }
  }

  // ---- Item 9 backfill: standalone CPU per-sample-step behaviour ----
  // The CPU backend's path_loss / phase / cfo / awgn steps were previously
  // only exercised indirectly via CUDA-vs-CPU parity. These tests assert each
  // step's analytic behaviour on the CPU path directly so a divergence in
  // the CPU reference is caught without depending on the GPU path.
  auto build_single_step_cpu = [](const ocg::ModelConfig& model, std::size_t batch,
                                  std::uint64_t sample_rate)
      -> std::unique_ptr<ocg::CpuChannelProcessor> {
    ocg::TopologyConfig cfg;
    cfg.runtime.batch_samples_auto = false;
    cfg.runtime.batch_samples = batch;
    cfg.runtime.queue_samples = std::max<std::size_t>(614400, batch * 8);
    cfg.devices = {{.id = "gnb0", .role = "gnb", .sample_rate_hz = sample_rate,
                    .tx_endpoint = "tx0", .rx_endpoint = "rx0"},
                   {.id = "ue0", .role = "ue", .sample_rate_hz = sample_rate,
                    .tx_endpoint = "tx1", .rx_endpoint = "rx1"}};
    cfg.links = {{.from = "gnb0", .to = "ue0", .model = model.id},
                 {.from = "ue0", .to = "gnb0", .model = model.id}};
    cfg.models.emplace(model.id, model);
    auto proc = std::make_unique<ocg::CpuChannelProcessor>();
    proc->prepare(cfg);
    return proc;
  };

  // (i) CPU path_loss: multiplies amplitude by 10^(-path_loss_db/20). For a
  // 6 dB loss the output amplitude should be 0.5 of the input (within float).
  {
    ocg::ModelConfig m;
    m.id = "pl_test";
    m.chain.push_back({.type = ocg::ModelStepType::PathLoss, .params = {{"path_loss_db", 6.0}}});
    auto proc = build_single_step_cpu(m, 8, 1000);
    const ocg::IqBuffer in(8, ocg::IqSample{1.0F, 0.0F});
    ocg::IqBuffer out(8);
    shape_link(*proc, "ue0", ocg::link_key({.from = "gnb0", .to = "ue0", .model = m.id}),
               m, in, out, 1000);
    const float expected = static_cast<float>(std::pow(10.0, -6.0 / 20.0));  // ~0.5012
    for (const auto& s : out) {
      require(std::fabs(s.i - expected) < 1e-4F && std::fabs(s.q) < 1e-4F,
              "CPU path_loss: 6 dB loss must scale amplitude by ~0.5012");
    }
  }

  // (ii) CPU phase: applies a fixed phase rotation. Input (1, 0) at phi=pi/3
  // should come out (cos(pi/3), sin(pi/3)) = (0.5, sqrt(3)/2).
  {
    ocg::ModelConfig m;
    m.id = "phase_test";
    m.chain.push_back({.type = ocg::ModelStepType::Phase,
                       .params = {{"phase_rad", std::numbers::pi / 3.0}}});
    auto proc = build_single_step_cpu(m, 4, 1000);
    const ocg::IqBuffer in(4, ocg::IqSample{1.0F, 0.0F});
    ocg::IqBuffer out(4);
    shape_link(*proc, "ue0", ocg::link_key({.from = "gnb0", .to = "ue0", .model = m.id}),
               m, in, out, 1000);
    const float ex_i = static_cast<float>(std::cos(std::numbers::pi / 3.0));
    const float ex_q = static_cast<float>(std::sin(std::numbers::pi / 3.0));
    for (const auto& s : out) {
      require(std::fabs(s.i - ex_i) < 1e-5F && std::fabs(s.q - ex_q) < 1e-5F,
              "CPU phase: static rotation must match cos+sin of phase_rad");
    }
  }

  // (iii) CPU cfo: per-sample phase increment 2*pi*f/fs. With f = fs/8, the
  // increment per sample is pi/4 -- after 8 samples we should be back at the
  // start, and the increment between sample 0 and sample 1 must equal pi/4.
  {
    ocg::ModelConfig m;
    m.id = "cfo_test";
    constexpr std::uint64_t sr = 1000;
    constexpr double cfo_hz = static_cast<double>(sr) / 8.0;  // 125 Hz
    m.chain.push_back({.type = ocg::ModelStepType::Cfo, .params = {{"cfo_hz", cfo_hz}}});
    auto proc = build_single_step_cpu(m, 8, sr);
    const ocg::IqBuffer in(8, ocg::IqSample{1.0F, 0.0F});
    ocg::IqBuffer out(8);
    shape_link(*proc, "ue0", ocg::link_key({.from = "gnb0", .to = "ue0", .model = m.id}),
               m, in, out, sr);
    // Sample n should have phase 2*pi*f*n/fs = n*pi/4.
    for (std::size_t n = 0; n < 8; ++n) {
      const double angle = 2.0 * std::numbers::pi * cfo_hz * n / sr;
      const float ex_i = static_cast<float>(std::cos(angle));
      const float ex_q = static_cast<float>(std::sin(angle));
      require(std::fabs(out[n].i - ex_i) < 1e-4F && std::fabs(out[n].q - ex_q) < 1e-4F,
              "CPU cfo: per-sample phase must advance by 2*pi*f/fs");
    }
  }

  // (iv) CPU AWGN explicit noise_power: zero input through an absolute-power
  // AWGN must yield zero-mean noise of the specified power. Mirrors the
  // existing CUDA AWGN statistical test on the CPU backend.
  {
    ocg::ModelConfig m;
    m.id = "awgn_test";
    const double target = 0.04;
    m.chain.push_back({.type = ocg::ModelStepType::Awgn, .params = {{"noise_power", target}}});
    auto proc = build_single_step_cpu(m, 8192, 23040000);
    const ocg::IqBuffer zeros(8192, ocg::IqSample{0.0F, 0.0F});
    ocg::IqBuffer noisy(zeros.size());
    shape_link(*proc, "ue0", ocg::link_key({.from = "gnb0", .to = "ue0", .model = m.id}),
               m, zeros, noisy, 23040000);
    double power = 0.0, ii = 0.0, qq = 0.0;
    for (const auto& s : noisy) {
      power += static_cast<double>(s.i) * s.i + static_cast<double>(s.q) * s.q;
      ii += s.i;
      qq += s.q;
    }
    const auto n = static_cast<double>(noisy.size());
    require(std::fabs(power / n - target) < 0.1 * target,
            "CPU AWGN: mean power must match noise_power (within 10%)");
    require(std::fabs(ii / n) < 0.02 && std::fabs(qq / n) < 0.02,
            "CPU AWGN: zero-mean per component");

    // Second slot must draw a fresh sequence.
    ocg::IqBuffer noisy2(zeros.size());
    shape_link(*proc, "ue0", ocg::link_key({.from = "gnb0", .to = "ue0", .model = m.id}),
               m, zeros, noisy2, 23040000);
    bool differs = false;
    for (std::size_t i = 0; i < noisy.size(); ++i) {
      if (noisy[i].i != noisy2[i].i || noisy[i].q != noisy2[i].q) { differs = true; break; }
    }
    require(differs, "CPU AWGN: fresh draw per slot");
  }

  // (v) CPU AWGN snr_db: noise power should be measured_signal_power /
  // 10^(snr_db/10). For a constant-amplitude unit-power input at snr_db=10,
  // expected noise power = 1.0 / 10 = 0.1. The empirical noise power is
  // measured by subtracting the (known constant) input from the output.
  {
    ocg::ModelConfig m;
    m.id = "awgn_snr_test";
    const double snr_db = 10.0;
    m.chain.push_back({.type = ocg::ModelStepType::Awgn, .params = {{"snr_db", snr_db}}});
    auto proc = build_single_step_cpu(m, 8192, 23040000);
    // Unit-power input: (1, 0) -> |s|^2 = 1.
    const ocg::IqBuffer ones(8192, ocg::IqSample{1.0F, 0.0F});
    ocg::IqBuffer noisy(ones.size());
    shape_link(*proc, "ue0", ocg::link_key({.from = "gnb0", .to = "ue0", .model = m.id}),
               m, ones, noisy, 23040000);
    double noise_power = 0.0;
    for (std::size_t i = 0; i < noisy.size(); ++i) {
      const float di = noisy[i].i - ones[i].i;
      const float dq = noisy[i].q - ones[i].q;
      noise_power += static_cast<double>(di) * di + static_cast<double>(dq) * dq;
    }
    noise_power /= static_cast<double>(noisy.size());
    const double expected = 1.0 / std::pow(10.0, snr_db / 10.0);  // 0.1
    require(std::fabs(noise_power - expected) < 0.15 * expected,
            "CPU AWGN snr_db: noise power must equal signal_power / 10^(snr_db/10)");
  }

  // (e) Rician envelope distribution: for a unit-power LOS tap with K-factor
  // K (linear), the envelope r = |g_k(t)| follows
  //   f(r) = (r/sigma^2) * exp(-(r^2 + nu^2)/(2 sigma^2)) * I_0(r*nu/sigma^2)
  // with nu^2 = K/(K+1) and 2*sigma^2 = 1/(K+1). The closed-form moments are
  //   E[r^2] = 1               (unit total power)
  //   E[r^4] = 1 + (2K+1)/(K+1)^2
  // Feeding DC into a unit-gain LOS tap makes y(n) = g_k(t_n) directly, so we
  // can estimate both moments empirically and compare to analytic values.
  // K = 10 dB (K_lin = 10) gives E[r^4] = 1 + 21/121 ~= 1.1736.
  {
    ocg::ModelConfig m;
    m.id = "tdl_fading_rician_pdf";
    ocg::ModelStep step;
    step.type = ocg::ModelStepType::Tdl;
    const double K_dB = 10.0;
    step.taps = {ocg::TapSpec{.delay_samples = 0.0, .gain_db = 0.0,
                              .phase_rad = 0.0, .is_los = true,
                              .los_k_db = K_dB, .los_angle_rad = 0.0}};
    step.taps_declared = true;
    step.fading_enabled = true;
    step.fading_f_d_max_hz = 100.0;
    step.fading_grid_us = 100.0;
    step.fading_spectrum = ocg::FadingSpectrum::Jakes;
    m.chain.push_back(step);

    constexpr std::uint64_t sample_rate_hz = 100000;
    constexpr std::size_t batch = 4000;
    constexpr std::size_t n_slots = 200;
    auto proc = build_fading_processor(m, batch, sample_rate_hz);
    ocg::IqBuffer dc_in(batch, ocg::IqSample{1.0F, 0.0F});

    double m2 = 0.0, m4 = 0.0;
    std::size_t n = 0;
    const std::string link = ocg::link_key({.from = "gnb0", .to = "ue0", .model = m.id});
    for (std::size_t s = 0; s < n_slots; ++s) {
      ocg::IqBuffer slot_out(batch);
      shape_link(*proc, "ue0", link, m, dc_in, slot_out, sample_rate_hz);
      for (const auto& sam : slot_out) {
        const double r2 = static_cast<double>(sam.i) * sam.i +
                          static_cast<double>(sam.q) * sam.q;
        m2 += r2;
        m4 += r2 * r2;
        ++n;
      }
    }
    m2 /= static_cast<double>(n);
    m4 /= static_cast<double>(n);

    const double K_lin = std::pow(10.0, K_dB / 10.0);
    const double m2_expected = 1.0;
    const double m4_expected = 1.0 + (2.0 * K_lin + 1.0) / std::pow(K_lin + 1.0, 2.0);

    require(std::fabs(m2 - m2_expected) < 0.05,
            "Rician PDF: E[r^2] should be ~1.0 for unit-power Rician (within 0.05)");
    require(std::fabs(m4 - m4_expected) < 0.10,
            "Rician PDF: E[r^4] should match analytic moment 1 + (2K+1)/(K+1)^2 within 0.10");
  }

  return 0;
}
