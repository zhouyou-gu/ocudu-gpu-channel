// Phase 3 C5: runtime-update parity test.
//
// Drives a scripted sequence of shadow writes against both the CPU and
// (when built with CUDA) CUDA backends and asserts that:
//   1. The same update sequence on both backends produces matching output
//      slot-by-slot at the project's 1e-3 parity tolerance.
//   2. Output actually changes after each update — proving the snap
//      mechanism in fact threaded the new value into the kernel.
//
// The test bypasses ControlServer and writes directly to BrokerLinkControl::
// shadow so the test does not depend on the ZMQ wire layer; ControlServer
// is already covered by tests/test_control_server.cpp.

#include "ocudu_gpu_channel/config.h"
#include "ocudu_gpu_channel/cpu_backend.h"
#include "ocudu_gpu_channel/processing.h"
#include "ocudu_gpu_channel/control_server.h"
#include "ocudu_gpu_channel/runtime_control.h"

#include <atomic>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <memory>
#include <sstream>
#include <string>
#include <vector>

#if OCUDU_GPU_CHANNEL_HAS_CUDA
#include "ocudu_gpu_channel/cuda_backend.h"
#include "ocudu_gpu_channel/backend.h"
#endif

namespace {

void require(bool condition, const char* message)
{
  if (!condition) {
    std::cerr << "FAIL: " << message << "\n";
    std::exit(1);
  }
}

constexpr float kParityTolerance = 1e-3F;

bool near_float(float a, float b, float tol = kParityTolerance)
{
  return std::fabs(a - b) <= tol;
}

float max_abs_diff(const std::vector<ocg::IqSample>& a,
                   const std::vector<ocg::IqSample>& b)
{
  float m = 0.0F;
  for (std::size_t i = 0; i < a.size(); ++i) {
    m = std::max(m, std::fabs(a[i].i - b[i].i));
    m = std::max(m, std::fabs(a[i].q - b[i].q));
  }
  return m;
}

// Build a minimal 1-link topology: one gNB → one UE, one tdl (unit gain
// pass-through) followed by a path_loss step. path_loss_db is the v1
// mutable param under test.
ocg::TopologyConfig make_topology(ocg::Backend backend)
{
  ocg::TopologyConfig cfg;
  cfg.runtime.backend = backend;
  cfg.runtime.batch_samples_auto = false;
  cfg.runtime.batch_samples = 8;
  cfg.runtime.queue_samples = 64;
  cfg.devices = {
      {.id = "gnb0", .role = "gnb", .sample_rate_hz = 23040000,
       .tx_endpoint = "tx0", .rx_endpoint = "rx0"},
      {.id = "ue0",  .role = "ue",  .sample_rate_hz = 23040000,
       .tx_endpoint = "tx1", .rx_endpoint = "rx1"}};
  cfg.links = {{.from = "gnb0", .to = "ue0", .model = "chain"}};

  ocg::ModelConfig m;
  m.id = "chain";
  m.chain.push_back({.type = ocg::ModelStepType::Tdl,
                     .params = {},
                     .taps = {{.delay_samples = 0.0, .gain_db = 0.0, .phase_rad = 0.0}},
                     .taps_declared = true});
  ocg::ModelStep pl;
  pl.type = ocg::ModelStepType::PathLoss;
  pl.params["path_loss_db"] = 0.0;  // YAML default; will be runtime-updated
  m.chain.push_back(pl);
  cfg.models.emplace(m.id, m);
  return cfg;
}

// Run one slot through `proc` and return the output buffer.
// Compute the broker's canonical link key for the test's single-link
// topology. Mirrors ocg::link_key(): "<from>><to>:<model_id>". CUDA
// backend requires this exact key (no lazy creation); CPU backend
// lazy-creates so anything works, but for parity we always pass the
// real key.
std::string test_link_key(const ocg::ModelConfig& model)
{
  return "gnb0>ue0:" + model.id;
}

std::vector<ocg::IqSample> run_slot(ocg::ChannelProcessor& proc,
                                    const ocg::ModelConfig& model,
                                    const std::vector<ocg::IqSample>& input)
{
  std::vector<ocg::IqSample> output(input.size(), {0.0F, 0.0F});
  const std::span<const ocg::IqSample> input_span(input.data(), input.size());
  std::vector<ocg::SuperpositionInput> edges;
  edges.push_back({.link_key = test_link_key(model), .model = &model, .samples = input_span});
  proc.process_superposition("ue0", edges, nullptr,
                             23040000ULL, std::span<ocg::IqSample>(output.data(), output.size()));
  return output;
}

ocg::BrokerLinkControl* lookup_ctl(ocg::ChannelProcessor& proc,
                                   const ocg::ModelConfig& model)
{
  auto map = proc.collect_control_links();
  auto it = map.find(test_link_key(model));
  require(it != map.end() && it->second != nullptr, "link found in control map");
  return it->second;
}

void write_shadow_path_loss(ocg::ChannelProcessor& proc,
                            const ocg::ModelConfig& model, float new_db)
{
  auto* ctl = lookup_ctl(proc, model);
  ctl->shadow.path_loss_db = new_db;
  ctl->seqno.fetch_add(1, std::memory_order_release);
}

void write_shadow_awgn_snr(ocg::ChannelProcessor& proc,
                           const ocg::ModelConfig& model, float snr_db)
{
  auto* ctl = lookup_ctl(proc, model);
  ctl->shadow.awgn_snr_db = snr_db;
  ctl->seqno.fetch_add(1, std::memory_order_release);
}

void write_shadow_tap0_gain(ocg::ChannelProcessor& proc,
                            const ocg::ModelConfig& model, float gain_db)
{
  auto* ctl = lookup_ctl(proc, model);
  ctl->shadow.tap0_gain_db = gain_db;
  ctl->seqno.fetch_add(1, std::memory_order_release);
}

// v2.0-F3a: write a 2-tap profile_swap into the link's shadow. Caller
// passes raw tap params; helper writes shadow_profile + profile_pending
// and bumps seqno (mirrors what ControlServer's handle_profile_swap
// does without going through ZMQ).
void write_shadow_profile_swap_2tap(
    ocg::ChannelProcessor& proc,
    const ocg::ModelConfig& model,
    double delay0, double gain_db0,
    double delay1, double gain_db1)
{
  auto* ctl = lookup_ctl(proc, model);
  ocg::ProfileShadow& sp = ctl->shadow_profile;
  sp.n_taps = 2;
  sp.taps[0].delay_samples = delay0;
  sp.taps[0].gain_db       = gain_db0;
  sp.taps[0].phase_rad     = 0.0;
  sp.taps[0].is_los        = false;
  sp.taps[1].delay_samples = delay1;
  sp.taps[1].gain_db       = gain_db1;
  sp.taps[1].phase_rad     = 0.0;
  sp.taps[1].is_los        = false;
  sp.fading_enabled = false;
  sp.force = false;
  ctl->profile_pending = true;
  ctl->seqno.fetch_add(1, std::memory_order_release);
}

double mean_power(const std::vector<ocg::IqSample>& samples)
{
  double p = 0.0;
  for (const auto& s : samples) p += static_cast<double>(s.i) * s.i + static_cast<double>(s.q) * s.q;
  return p / static_cast<double>(samples.size());
}

// Build a topology whose chain is: tdl pass-through → snr_db AWGN. Used
// to validate that live.awgn_snr_db actually changes the output noise
// power at runtime.
ocg::TopologyConfig make_awgn_topology(ocg::Backend backend)
{
  ocg::TopologyConfig cfg;
  cfg.runtime.backend = backend;
  cfg.runtime.batch_samples_auto = false;
  cfg.runtime.batch_samples = 4096;
  cfg.runtime.queue_samples = 32768;
  cfg.devices = {
      {.id = "gnb0", .role = "gnb", .sample_rate_hz = 23040000,
       .tx_endpoint = "tx0", .rx_endpoint = "rx0"},
      {.id = "ue0",  .role = "ue",  .sample_rate_hz = 23040000,
       .tx_endpoint = "tx1", .rx_endpoint = "rx1"}};
  cfg.links = {{.from = "gnb0", .to = "ue0", .model = "awgn-chain"}};

  ocg::ModelConfig m;
  m.id = "awgn-chain";
  m.chain.push_back({.type = ocg::ModelStepType::Tdl,
                     .params = {},
                     .taps = {{.delay_samples = 0.0, .gain_db = 0.0, .phase_rad = 0.0}},
                     .taps_declared = true});
  ocg::ModelStep awgn;
  awgn.type = ocg::ModelStepType::Awgn;
  awgn.params["snr_db"] = 20.0;  // YAML default; runtime-overridable
  m.chain.push_back(awgn);
  cfg.models.emplace(m.id, m);
  return cfg;
}

}  // namespace

int main()
{
  const std::vector<ocg::IqSample> input = {
      {1.0F, 0.0F}, {0.0F, 1.0F}, {1.0F, 1.0F}, {0.5F, -0.5F},
      {-1.0F, 0.0F}, {0.0F, -1.0F}, {1.0F, -1.0F}, {-0.5F, 0.5F}};

  // ── CPU backend: drive 3 slots with path_loss updates between them ─────
  auto cpu_cfg = make_topology(ocg::Backend::Cpu);
  ocg::CpuChannelProcessor cpu;
  cpu.prepare(cpu_cfg);
  const ocg::ModelConfig& cpu_model = cpu_cfg.models["chain"];

  // Slot 0: YAML default (path_loss_db = 0 → unit gain)
  auto out0_cpu = run_slot(cpu, cpu_model, input);
  require(near_float(out0_cpu[0].i, 1.0F), "slot 0 CPU: unit gain on sample 0 I");
  require(near_float(out0_cpu[0].q, 0.0F), "slot 0 CPU: unit gain on sample 0 Q");

  // Update path_loss to -20 dB → 10x gain. (Negative path_loss_db = gain
  // in this code path; the factor is 10^(-path_loss_db/20).)
  write_shadow_path_loss(cpu, cpu_model, -20.0F);
  auto out1_cpu = run_slot(cpu, cpu_model, input);
  require(near_float(out1_cpu[0].i, 10.0F),
          "slot 1 CPU: -20 dB path_loss → 10x amplitude");
  require(near_float(out1_cpu[3].q, -5.0F),
          "slot 1 CPU: 10x scaling preserves Q sign + magnitude");

  // Update to +20 dB → 0.1x gain.
  write_shadow_path_loss(cpu, cpu_model, 20.0F);
  auto out2_cpu = run_slot(cpu, cpu_model, input);
  require(near_float(out2_cpu[0].i, 0.1F),
          "slot 2 CPU: +20 dB path_loss → 0.1x amplitude");

#if OCUDU_GPU_CHANNEL_HAS_CUDA
  if (ocg::cuda_compiled()) {
    // ── CUDA backend: same script, expect bit-similar outputs ────────────
    auto cuda_cfg = make_topology(ocg::Backend::Cuda);
    auto cuda = ocg::create_channel_processor(cuda_cfg);  // calls prepare()
    const ocg::ModelConfig& cuda_model = cuda_cfg.models["chain"];

    auto out0_cuda = run_slot(*cuda, cuda_model, input);
    require(max_abs_diff(out0_cpu, out0_cuda) <= kParityTolerance,
            "slot 0 CPU↔CUDA parity within 1e-3");

    write_shadow_path_loss(*cuda, cuda_model, -20.0F);
    auto out1_cuda = run_slot(*cuda, cuda_model, input);
    require(max_abs_diff(out1_cpu, out1_cuda) <= kParityTolerance,
            "slot 1 CPU↔CUDA parity within 1e-3 after first runtime update");

    write_shadow_path_loss(*cuda, cuda_model, 20.0F);
    auto out2_cuda = run_slot(*cuda, cuda_model, input);
    require(max_abs_diff(out2_cpu, out2_cuda) <= kParityTolerance,
            "slot 2 CPU↔CUDA parity within 1e-3 after second runtime update");
  }
#endif

  // ── v1-fin-C: tap0_gain_db runtime update changes amplitude (CPU) ─────
  // Drives a 1+0j signal through a single-tap tdl (no follow-on chain
  // steps so the gain shows up directly). Mutates tap0_gain_db at runtime
  // and asserts amplitude scales.
  {
    ocg::TopologyConfig tap_cfg;
    tap_cfg.runtime.backend = ocg::Backend::Cpu;
    tap_cfg.runtime.batch_samples_auto = false;
    tap_cfg.runtime.batch_samples = 4;
    tap_cfg.runtime.queue_samples = 32;
    tap_cfg.devices = {
        {.id = "gnb0", .role = "gnb", .sample_rate_hz = 23040000,
         .tx_endpoint = "tx0", .rx_endpoint = "rx0"},
        {.id = "ue0",  .role = "ue",  .sample_rate_hz = 23040000,
         .tx_endpoint = "tx1", .rx_endpoint = "rx1"}};
    tap_cfg.links = {{.from = "gnb0", .to = "ue0", .model = "tap-only"}};
    ocg::ModelConfig m;
    m.id = "tap-only";
    m.chain.push_back({.type = ocg::ModelStepType::Tdl,
                       .params = {},
                       .taps = {{.delay_samples = 0.0, .gain_db = 0.0, .phase_rad = 0.0}},
                       .taps_declared = true});
    tap_cfg.models.emplace(m.id, m);

    ocg::CpuChannelProcessor tap_cpu;
    tap_cpu.prepare(tap_cfg);
    const ocg::ModelConfig& tap_model = tap_cfg.models["tap-only"];

    const std::vector<ocg::IqSample> unit = {{1.0F, 0.0F}, {1.0F, 0.0F},
                                              {1.0F, 0.0F}, {1.0F, 0.0F}};

    // Slot 0: YAML gain=0 dB → output amplitude 1.0.
    auto t0 = run_slot(tap_cpu, tap_model, unit);
    require(near_float(t0[1].i, 1.0F), "tap0: gain=0 dB → amplitude 1.0");

    // Runtime update: tap0_gain_db = 6 dB → ~2x amplitude.
    write_shadow_tap0_gain(tap_cpu, tap_model, 6.020599913F);
    auto t1 = run_slot(tap_cpu, tap_model, unit);
    require(near_float(t1[1].i, 2.0F, 1e-3F),
            "tap0: runtime gain=+6 dB → amplitude ~2.0");

    // Runtime update: tap0_gain_db = -6 dB → ~0.5x amplitude.
    write_shadow_tap0_gain(tap_cpu, tap_model, -6.020599913F);
    auto t2 = run_slot(tap_cpu, tap_model, unit);
    require(near_float(t2[1].i, 0.5F, 1e-3F),
            "tap0: runtime gain=-6 dB → amplitude ~0.5");
  }

  // ── v1-fin-A: AWGN snr_db runtime update changes mean noise power ─────
  // Drives a unit-power signal through a tdl+awgn chain. Asserts the
  // measured output power changes by the expected dB ratio when snr_db
  // is updated at runtime. AWGN is stochastic so this is a power test,
  // not a bit-exact one; 4096 samples gives a ~5% standard error on the
  // mean-power estimate, so the assertion tolerates 25% mismatch.
  {
    std::vector<ocg::IqSample> unit_input(4096);
    for (std::size_t i = 0; i < unit_input.size(); ++i) {
      unit_input[i] = {1.0F, 0.0F};   // signal power = 1.0
    }

    auto awgn_cfg = make_awgn_topology(ocg::Backend::Cpu);
    ocg::CpuChannelProcessor awgn_cpu;
    awgn_cpu.prepare(awgn_cfg);
    const ocg::ModelConfig& awgn_model = awgn_cfg.models["awgn-chain"];

    // Slot 0: YAML snr_db=20 → noise_power ≈ 0.01 → output power ≈ 1.01.
    auto out_snr20 = run_slot(awgn_cpu, awgn_model, unit_input);
    const double p_snr20 = mean_power(out_snr20);
    require(std::fabs(p_snr20 - 1.01) < 0.25,
            "AWGN: snr=20 → output power should be ~1.01 (signal + noise)");

    // Slot 1: runtime update snr_db=0 → noise_power = 1.0 → output power ≈ 2.0.
    write_shadow_awgn_snr(awgn_cpu, awgn_model, 0.0F);
    auto out_snr0 = run_slot(awgn_cpu, awgn_model, unit_input);
    const double p_snr0 = mean_power(out_snr0);
    require(std::fabs(p_snr0 - 2.0) < 0.5,
            "AWGN: snr=0 → output power should be ~2.0 after runtime update");

    // Slot 2: back up to a quieter snr_db=40 → noise_power ≈ 1e-4 → ≈ 1.0001.
    write_shadow_awgn_snr(awgn_cpu, awgn_model, 40.0F);
    auto out_snr40 = run_slot(awgn_cpu, awgn_model, unit_input);
    const double p_snr40 = mean_power(out_snr40);
    require(std::fabs(p_snr40 - 1.0) < 0.1,
            "AWGN: snr=40 → output power should be ~1.0 after runtime update");
  }

  // ── v2.0-F3a: CPU profile_swap changes the per-edge filter ────────────
  // Drives a sharp impulse through a single-tap pass-through chain, then
  // does a profile_swap to a 2-tap (impulse + delayed-echo) profile and
  // checks the output now has both the direct impulse AND the echo at
  // the new delay. Demonstrates the live_profile path replaces the YAML
  // taps wholesale at runtime — not just tap-0 scalars.
  {
    ocg::TopologyConfig prof_cfg;
    prof_cfg.runtime.backend = ocg::Backend::Cpu;
    prof_cfg.runtime.batch_samples_auto = false;
    prof_cfg.runtime.batch_samples = 16;
    prof_cfg.runtime.queue_samples = 128;
    prof_cfg.devices = {
        {.id = "gnb0", .role = "gnb", .sample_rate_hz = 23040000,
         .tx_endpoint = "tx0", .rx_endpoint = "rx0"},
        {.id = "ue0",  .role = "ue",  .sample_rate_hz = 23040000,
         .tx_endpoint = "tx1", .rx_endpoint = "rx1"}};
    prof_cfg.links = {{.from = "gnb0", .to = "ue0", .model = "pass"}};
    ocg::ModelConfig m;
    m.id = "pass";
    m.chain.push_back({.type = ocg::ModelStepType::Tdl,
                       .params = {},
                       .taps = {{.delay_samples = 0.0, .gain_db = 0.0, .phase_rad = 0.0}},
                       .taps_declared = true});
    prof_cfg.models.emplace(m.id, m);

    ocg::CpuChannelProcessor proc_cpu;
    proc_cpu.prepare(prof_cfg);
    const ocg::ModelConfig& prof_model = prof_cfg.models["pass"];

    // Impulse at sample 0; zeros elsewhere. Single-tap YAML chain should
    // pass it through unchanged.
    std::vector<ocg::IqSample> impulse(16, {0.0F, 0.0F});
    impulse[0] = {1.0F, 0.0F};
    auto p0 = run_slot(proc_cpu, prof_model, impulse);
    require(near_float(p0[0].i, 1.0F), "profile_swap: YAML chain — impulse at sample 0");
    require(near_float(p0[3].i, 0.0F), "profile_swap: YAML chain — no echo at sample 3");

    // Runtime profile swap to 2 taps: impulse + an echo at delay 3 with
    // -6 dB (~0.5x amplitude).
    write_shadow_profile_swap_2tap(proc_cpu, prof_model,
                                   /*d0=*/0.0, /*g0=*/0.0,
                                   /*d1=*/3.0, /*g1=*/-6.020599913);
    // Fresh impulse input — the cross-slot delay_line still has residue
    // from the prior slot's impulse, so this test deliberately uses zero
    // input plus a fresh impulse to keep the assertion clean.
    std::vector<ocg::IqSample> impulse_after(16, {0.0F, 0.0F});
    impulse_after[0] = {1.0F, 0.0F};
    auto p1 = run_slot(proc_cpu, prof_model, impulse_after);
    require(near_float(p1[0].i, 1.0F),
            "profile_swap: after — direct impulse still passes through tap 0");
    require(near_float(p1[3].i, 0.5F, 1e-3F),
            "profile_swap: after — tap 1 produces 0.5x echo at delay 3");
  }

  // ── v2.1: take_effect_at_slot defers a scalar update by N slots ───────
  // Drives the same path_loss chain as the v2.0-F3 block. Runs one warm-up
  // slot (so process_superposition lazily creates the lookup-keyed link
  // state for the canonical link key), then writes a path_loss update with
  // take_effect_at_slot = 4. Slots 1..3 see the YAML default; slot 4 sees
  // the deferred update (-20 dB → 10x amplitude).
  {
    auto tea_cfg = make_topology(ocg::Backend::Cpu);
    ocg::CpuChannelProcessor tea_cpu;
    tea_cpu.prepare(tea_cfg);
    const ocg::ModelConfig& tea_model = tea_cfg.models["chain"];

    const std::vector<ocg::IqSample> unit = {
        {1.0F, 0.0F}, {1.0F, 0.0F}, {1.0F, 0.0F}, {1.0F, 0.0F},
        {1.0F, 0.0F}, {1.0F, 0.0F}, {1.0F, 0.0F}, {1.0F, 0.0F}};

    // Warm-up slot 0 → ensures the link's per-edge state is lazily
    // created with the canonical link key the helpers use. After this call
    // the link's next_slot is 1.
    auto warm = run_slot(tea_cpu, tea_model, unit);
    require(near_float(warm[0].i, 1.0F), "v2.1: warm-up slot 0 sees default");

    // Write the deferred update directly into the link's shadow with
    // take_effect_at_slot=4. Mirrors what ControlServer's
    // handle_scalar_update + v2.1 take_effect_at_slot does, without ZMQ.
    {
      auto* ctl = lookup_ctl(tea_cpu, tea_model);
      ctl->shadow.path_loss_db = -20.0F;
      ctl->take_effect_at_slot = 4;
      ctl->seqno.fetch_add(1, std::memory_order_release);
    }

    // Slots 1, 2, 3: gate not yet open → unit gain.
    auto sa = run_slot(tea_cpu, tea_model, unit);
    require(near_float(sa[0].i, 1.0F), "v2.1: slot 1 sees pre-take_effect default");
    auto sb = run_slot(tea_cpu, tea_model, unit);
    require(near_float(sb[0].i, 1.0F), "v2.1: slot 2 sees pre-take_effect default");
    auto sc = run_slot(tea_cpu, tea_model, unit);
    require(near_float(sc[0].i, 1.0F), "v2.1: slot 3 sees pre-take_effect default");

    // Slot 4: gate opens → 10x amplitude from the -20 dB path_loss.
    auto sd = run_slot(tea_cpu, tea_model, unit);
    require(near_float(sd[0].i, 10.0F),
            "v2.1: slot 4 sees the deferred update applied");
  }

  // ── v2.2: profile_swap arms a delay-line warmup window ────────────────
  // Same chain as the v2.0-F3a profile_swap test. After a profile_swap
  // activates, the snap path zero-fills the link's delay_line and emits
  // event=control_warmup_begin/end. We capture stdout to count events.
  {
    auto warm_cfg = make_topology(ocg::Backend::Cpu);
    ocg::CpuChannelProcessor warm_cpu;
    warm_cpu.prepare(warm_cfg);
    const ocg::ModelConfig& warm_model = warm_cfg.models["chain"];

    const std::vector<ocg::IqSample> unit = {
        {1.0F, 0.0F}, {1.0F, 0.0F}, {1.0F, 0.0F}, {1.0F, 0.0F},
        {1.0F, 0.0F}, {1.0F, 0.0F}, {1.0F, 0.0F}, {1.0F, 0.0F}};

    // Warm-up slot 0 lazily creates the link state with the canonical
    // link key the helpers expect (test_link_key()).
    (void)run_slot(warm_cpu, warm_model, unit);

    // Redirect std::cout into a stringstream so the assertion can grep
    // for warmup events without depending on the broker's normal stderr
    // path. RAII restores the original buf on scope exit.
    std::ostringstream captured;
    std::streambuf* const old_buf = std::cout.rdbuf(captured.rdbuf());

    // Profile swap: same two-tap (impulse + echo at delay 3) used in
    // v2.0-F3a, no take_effect_at_slot → applies ASAP.
    write_shadow_profile_swap_2tap(warm_cpu, warm_model, 0.0, 0.0, 3.0, -6.020599913);

    // First slot post-swap: snap fires, warmup begin emitted.
    (void)run_slot(warm_cpu, warm_model, unit);
    // Second slot: warmup_until_slot was snap_idx+1, so this slot
    // crosses the threshold and emits the end event.
    (void)run_slot(warm_cpu, warm_model, unit);

    std::cout.rdbuf(old_buf);

    const std::string log = captured.str();
    require(log.find("event=control_warmup_begin") != std::string::npos,
            "v2.2: warmup_begin event should appear after profile_swap snap");
    require(log.find("event=control_warmup_end") != std::string::npos,
            "v2.2: warmup_end event should appear after warmup window closes");
    require(log.find("link_id=gnb0>ue0") != std::string::npos,
            "v2.2: log lines should name the link");
    require(log.find("dl_samples=") != std::string::npos,
            "v2.2: begin event should include dl_samples=K");
  }

  // ── v2.2-W3: profile_swap REP carries warmup_until_slot ───────────────
  {
    // ControlServer test seam (no ZMQ) — synthesised link + a profile_
    // swap REQ. Confirms the REP envelope grew the new field.
    auto ctl = std::make_unique<ocg::BrokerLinkControl>();
    ocg::ControlServer::LinkMap link_map = {{"foo", ctl.get()}};
    ocg::ControlServerConfig cfg;
    cfg.endpoint = "inproc://test-v22";
    cfg.recv_timeout_ms = 100;
    ocg::ControlServer server(std::move(cfg), std::move(link_map));

    const std::string reply = server.handle_message(R"({
      "type":"profile_swap",
      "link_id":"foo",
      "taps":[{"delay_samples":0,"gain_db":0}]
    })");
    require(reply.find("\"ok\":true") != std::string::npos,
            "v2.2: profile_swap should succeed");
    require(reply.find("\"applied_at_slot\":") != std::string::npos,
            "v2.2: REP keeps applied_at_slot");
    require(reply.find("\"warmup_until_slot\":") != std::string::npos,
            "v2.2: REP adds warmup_until_slot");
  }

  // ── v3.1: force=true on non-tdl-leading chain emits inert warning ─────
  // Build a chain whose chain.front() is path_loss (NOT tdl), then send
  // a profile_swap with force=true. Snap path should: store + activate
  // the profile (force overrides eligibility) AND emit
  // event=control_force_warning AND bump ctl.force_inert_warnings.
  {
    ocg::TopologyConfig cfg;
    cfg.runtime.backend = ocg::Backend::Cpu;
    cfg.runtime.batch_samples_auto = false;
    cfg.runtime.batch_samples = 8;
    cfg.runtime.queue_samples = 64;
    cfg.devices = {
        {.id = "gnb0", .role = "gnb", .sample_rate_hz = 23040000,
         .tx_endpoint = "tx0", .rx_endpoint = "rx0"},
        {.id = "ue0",  .role = "ue",  .sample_rate_hz = 23040000,
         .tx_endpoint = "tx1", .rx_endpoint = "rx1"}};
    cfg.links = {{.from = "gnb0", .to = "ue0", .model = "no-tdl"}};
    ocg::ModelConfig m;
    m.id = "no-tdl";
    ocg::ModelStep pl;
    pl.type = ocg::ModelStepType::PathLoss;
    pl.params["path_loss_db"] = 0.0;
    m.chain.push_back(pl);   // chain.front() is PathLoss → not eligible
    cfg.models.emplace(m.id, m);

    ocg::CpuChannelProcessor proc;
    proc.prepare(cfg);
    const ocg::ModelConfig& model = cfg.models["no-tdl"];

    const std::vector<ocg::IqSample> input(8, {1.0F, 0.0F});

    // Warm-up so the lazy-created canonical-key state exists.
    (void)run_slot(proc, model, input);

    // Capture stdout to grep for the warning.
    std::ostringstream captured;
    std::streambuf* const old_buf = std::cout.rdbuf(captured.rdbuf());

    // Manually stage a profile_swap shadow with force=true (mirrors what
    // ControlServer::handle_profile_swap does under the hood).
    {
      auto* ctl = lookup_ctl(proc, model);
      ocg::ProfileShadow& sp = ctl->shadow_profile;
      sp.n_taps = 1;
      sp.taps[0].delay_samples = 0.0;
      sp.taps[0].gain_db       = 0.0;
      sp.taps[0].phase_rad     = 0.0;
      sp.taps[0].is_los        = false;
      sp.force = true;
      ctl->profile_pending = true;
      ctl->seqno.fetch_add(1, std::memory_order_release);
    }

    // One more slot → snap fires.
    (void)run_slot(proc, model, input);

    std::cout.rdbuf(old_buf);
    const std::string log = captured.str();

    require(log.find("event=control_force_warning") != std::string::npos,
            "v3.1: should emit event=control_force_warning");
    require(log.find("link_id=gnb0>ue0") != std::string::npos,
            "v3.1: warning should name the link");
    require(log.find("chain has no leading tdl") != std::string::npos,
            "v3.1: warning should explain the inertness");

    // Counter bumped to 1.
    auto* ctl = lookup_ctl(proc, model);
    require(ctl->force_inert_warnings.load() == 1,
            "v3.1: force_inert_warnings should increment to 1");
  }

  std::cout << "test_runtime_update_parity OK\n";
  return 0;
}
