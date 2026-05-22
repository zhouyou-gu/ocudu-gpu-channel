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
  ocg::TopologyConfig config;
  config.runtime.batch_samples_auto = false;
  config.runtime.batch_samples = 4;
  config.devices = {
      {.id = "gnb0", .role = "gnb", .sample_rate_hz = 1000, .tx_endpoint = "tx0", .rx_endpoint = "rx0"},
      {.id = "ue0", .role = "ue", .sample_rate_hz = 1000, .tx_endpoint = "tx1", .rx_endpoint = "rx1"}};
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
  shape_link(processor, "ue0", ocg::link_key(config.links[0]), model, latest["gnb0"], output, 1000);
  require(near(output[2].i, 2.0F), "single-edge superposition: gain on I");
  require(near(output[2].q, 2.0F), "single-edge superposition: gain on Q");

  ocg::ModelConfig delay;
  delay.id = "delay";
  delay.chain.push_back({.type = ocg::ModelStepType::IntegerDelay, .params = {{"delay_samples", 2.0}}});
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
    edge_a.chain.push_back({.type = ocg::ModelStepType::Gain, .params = {{"gain_db", -3.0}}});
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
    // Leading fractional delay, then a per-sample step -- exercises the no-op
    // device delay step followed by a real step.
    delay_chain.chain.push_back({.type = ocg::ModelStepType::FractionalDelay, .params = {{"delay_samples", 2.75}}});
    delay_chain.chain.push_back({.type = ocg::ModelStepType::Gain, .params = {{"gain_db", -3.0}}});
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
    delayed_edge.chain.push_back({.type = ocg::ModelStepType::IntegerDelay, .params = {{"delay_samples", 3.0}}});
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
    with_delay.chain.push_back({.type = ocg::ModelStepType::IntegerDelay, .params = {{"delay_samples", 3.0}}});
    with_delay.chain.push_back({.type = ocg::ModelStepType::Gain, .params = {{"gain_db", -3.0}}});
    ocg::ModelConfig plain;
    plain.id = "plain";
    plain.chain.push_back({.type = ocg::ModelStepType::Gain, .params = {{"gain_db", -3.0}}});
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
    with_delay.chain.push_back({.type = ocg::ModelStepType::IntegerDelay, .params = {{"delay_samples", 4.0}}});
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
    with_delay_6.chain.front().params["delay_samples"] = 6.0;
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
    require(compose_model->chain.front().params.at("delay_samples") == 6.0,
            "composed leading delay must be tx_timing_offset + propagation_delay");
    ocg::CpuChannelProcessor compose_proc;
    compose_proc.prepare(compose_cfg);
    ocg::IqBuffer compose_out(8);
    shape_link(compose_proc, compose_cfg.links[0].to, ocg::link_key(compose_cfg.links[0]), *compose_model, in,
               compose_out, 1000);
    require_near_buffer(ref6_out, compose_out,
                        "device tx_timing_offset + link propagation_delay must compose into one leading delay");
  }

  return 0;
}
