#include "ocudu_gpu_channel/cpu_backend.h"
#include "ocudu_gpu_channel/delay.h"
#include <algorithm>
#include <cmath>
#include <functional>
#include <iostream>
#include <numbers>
#include <stdexcept>

namespace ocg {
namespace {

double param_or(const ModelStep& step, const std::string& name, double fallback)
{
  auto it = step.params.find(name);
  return it == step.params.end() ? fallback : it->second;
}

double estimate_average_power(std::span<const IqSample> input)
{
  if (input.empty()) {
    return 0.0;
  }
  double sum = 0.0;
  for (const auto& sample : input) {
    sum += power(sample);
  }
  return sum / static_cast<double>(input.size());
}

} // namespace

// Thin wrapper around the shared `prepare_tdl_state` helper in delay.h --
// kept as a private static so the StepState type stays encapsulated. The
// helper sizes `tdl_polyphase` and the cross-slot `delay_line` ring; no tap
// data is cached here because the kernel reads `step.taps` directly from the
// owning ModelConfig at runtime. When the step has a fading sub-config, also
// draws the deterministic Jakes sub-ray angles / phases / LOS phases into
// state.tdl_fading via the same seed both backends use.
void CpuChannelProcessor::prepare_tdl_step(StepState& state, const ModelStep& step,
                                            std::uint64_t fading_seed)
{
  prepare_tdl_state(step.taps, state.tdl_polyphase, state.delay_line);
  prepare_tdl_fading_state(step, fading_seed, state.tdl_fading);
}

CpuChannelProcessor::LinkState& CpuChannelProcessor::ensure_link_state(const std::string& key,
                                                                       const ModelConfig& model,
                                                                       std::size_t sample_count,
                                                                       const std::string& physical_link_key,
                                                                       int rx_port,
                                                                       int tx_port)
{
  auto it = states_.find(key);
  if (it == states_.end()) {
    // Lazy creation only happens for single-shot use; the broker preallocates
    // every link in prepare() so process_superposition() never inserts
    // concurrently. try_emplace() default-constructs LinkState in place —
    // required since LinkState contains a non-movable BrokerLinkControl.
    it = states_.try_emplace(key).first;
  }

  LinkState& state = it->second;
  if (state.scratch_a.size() < sample_count) {
    state.scratch_a.resize(sample_count);
    state.scratch_b.resize(sample_count);
  }
  if (state.steps.size() != model.chain.size()) {
    state.steps.assign(model.chain.size(), StepState{});
    // M2: the stochastic channel belongs to the physical link. Its seed is
    // derived once here, and each lane's realisation is derived from it and
    // the lane's (rx_port, tx_port) position -- so two lanes of one link are
    // independent draws of the same link, not two unrelated links.
    const std::uint64_t link_seed = physical_link_seed(physical_link_key);
    for (std::size_t i = 0; i != state.steps.size(); ++i) {
      state.steps[i].rng.seed(static_cast<unsigned>(std::hash<std::string>{}(key + ":" + std::to_string(i))));
      if (model.chain[i].type == ModelStepType::Tdl) {
        // The CUDA backend derives the seed through the same two functions,
        // so both backends draw the same Jakes sub-ray angles for a lane.
        prepare_tdl_step(state.steps[i], model.chain[i],
                         lane_fading_seed(link_seed, rx_port, tx_port,
                                          static_cast<int>(i)));
      }
    }
    // Phase 3 v1: populate runtime-mutable params from YAML. apply_chain_to_link
    // (post-C2a) reads path_loss_db + cfo_hz from `live`. The control plane
    // (C3+) writes to `ctl.shadow` and bumps `ctl.seqno`; snap_mutable_params()
    // at the top of every serve picks up shadow → live transitions. Initialise
    // shadow == live so the first serve's snap is a no-op.
    state.live = populate_mutable_params_from_yaml(model, /*reference_power=*/0.0,
                                                   /*sample_rate_hz=*/0);
    init_broker_link_control(state.ctl, state.live);

    // v2.0-F3: cache the eligibility flag — a profile_swap is only
    // honoured when the chain starts with a tdl step (or when force=true
    // on the swap REQ). Done at prepare so the per-slot snap doesn't
    // re-walk the chain.
    state.chain_has_leading_tdl =
        !model.chain.empty() && model.chain.front().type == ModelStepType::Tdl;

    // v2.2 follow-on: write per-link hints for the control plane's
    // warmup-cap check. dl_size_samples_hint is the leading tdl
    // step's delay_line size (0 if no leading tdl); slot_count_hint
    // is the per-slot sample count this link receives.
    if (state.chain_has_leading_tdl && !state.steps.empty()) {
      state.ctl.dl_size_samples_hint =
          static_cast<int>(state.steps.front().delay_line.size());
    }
    state.ctl.slot_count_hint = static_cast<int>(sample_count);
  }
  return state;
}

void CpuChannelProcessor::prepare(const TopologyConfig& config)
{
  // State is keyed by the SAME resolved lane table the broker drives serves
  // from, so a key can never be missing at serve time -- process_superposition
  // must never insert into the map concurrently.
  const ResolvedTopology resolved = resolve_topology(config);

  std::map<std::string, const ResolvedNode*> node_by_id;
  for (const auto& node : resolved.nodes) {
    node_by_id.emplace(node.id, &node);
  }

  for (const auto& lane : resolved.lanes) {
    const auto* model = find_model(config, lane.model_id);
    auto destination = node_by_id.find(lane.dst_node);
    if (model == nullptr || destination == node_by_id.end()) {
      continue;
    }
    const std::size_t count =
        resolve_batch_samples(config.runtime, destination->second->sample_rate_hz);
    ensure_link_state(lane.key, *model, count, lane.physical_link_key,
                      lane.rx_port, lane.tx_port);
  }

  // Receiver-model state, one entry per output ROW. Sibling rows must not share
  // the receiver chain's CFO phase and delay line; at Nr = 1 rx_state_key still
  // yields the pre-M1 "<node>>rx".
  for (const auto& node : resolved.nodes) {
    if (node.rx_model.empty()) {
      continue;
    }
    const auto* model = find_model(config, node.rx_model);
    if (model == nullptr) {
      continue;
    }
    const std::size_t count = resolve_batch_samples(config.runtime, node.sample_rate_hz);
    const int nr = static_cast<int>(node.rx_ports.size());
    for (int r = 0; r != nr; ++r) {
      // A receiver model is not carried by any link, so the row's own state
      // key is its stochastic identity, and the row index is its position:
      // sibling rows draw independently, as they must.
      const std::string key = rx_state_key(node.id, r, nr);
      ensure_link_state(key, *model, count, key, r, /*tx_port=*/0);
    }
  }
}

void CpuChannelProcessor::apply_chain_to_link(const std::string& link_key_value,
                                              const ModelConfig& model,
                                              std::span<const IqSample> input,
                                              std::span<IqSample> output,
                                              std::uint64_t sample_rate_hz)
{
  if (output.size() != input.size()) {
    throw std::runtime_error("apply_chain_to_link input and output sizes must match");
  }
  if (input.empty()) {
    return;
  }

  // prepare() has already created every state the broker serves, so this
  // lookup finds one and the identity arguments are not read. They matter only
  // for a caller that skipped prepare() (single-shot use): such a state is its
  // own physical link at matrix position (0, 0).
  LinkState& state = ensure_link_state(link_key_value, model, input.size(),
                                       link_key_value, /*rx_port=*/0, /*tx_port=*/0);

  // Phase 3 C2b: snap any pending shadow update from the control plane into
  // `live` before the chain reads it. No-op when seqno hasn't advanced
  // (single relaxed-acquire load + early-return branch). In v1 with no
  // control plane wired this is always a no-op; cost is negligible.
  //
  // v2.1: pass the link's per-link slot counter so the snap can honour
  // ctl.take_effect_at_slot. Counter advances after the snap so each
  // call's snap_idx matches "the slot we're about to compute".
  const std::uint64_t snap_idx = state.next_slot;
  const bool snap_changed = snap_mutable_params(state.live, state.live_seqno,
                                                state.ctl, snap_idx);
  state.next_slot = snap_idx + 1;

  // v2.0-F3: profile-swap snap. Idempotent re-copy on every seqno bump
  // (cheap — single ~1KB memcpy). Eligibility check is local: if the
  // chain has no leading tdl AND force=false on the shadow, drop the
  // pending profile silently this slot (control plane gets no feedback
  // beyond the existing event=control_update line — F4 doc will note
  // this). Future commits can add a counter / log_line.
  bool profile_just_activated = false;
  if (snap_changed && state.ctl.profile_pending) {
    if (state.chain_has_leading_tdl || state.ctl.shadow_profile.force) {
      snap_profile_from_shadow(state.live_profile, state.ctl);
      state.live_profile_active = true;
      profile_just_activated = true;
      // v3.1: warn when force was needed AND the chain has no leading
      // tdl. The profile is stored + snapped but the per-sample chain
      // never reaches a Tdl branch, so kernel output is unchanged.
      // Option C from the v3 plan — explicit visibility without
      // attempting to flip the dispatch gate.
      if (!state.chain_has_leading_tdl) {
        std::cout << "event=control_force_warning link_id=" << link_key_value
                  << " reason=\"chain has no leading tdl; profile stored but inert\"\n";
        state.ctl.force_inert_warnings.fetch_add(1, std::memory_order_relaxed);
      }
    }
    // else: eligibility failed (no force on a non-tdl-leading chain),
    // profile sits silently in shadow until a YAML reload — or until
    // a re-send with force=true.
  }

  // v2.2 W1: when a profile snap arms, zero the cross-slot delay_line so
  // the new tap layout doesn't convolve with stale ring contents. Set
  // warmup_until_slot so the broker can flag the next dl_size samples
  // as warmup artefacts. ceil(dl_size / count) slots; typically 1 in
  // production (count=23040 >> dl_size=~128). Find the leading tdl
  // step's StepState (its delay_line is the link's cross-slot ring).
  if (profile_just_activated) {
    StepState* leading_tdl_state = nullptr;
    for (std::size_t i = 0; i < model.chain.size(); ++i) {
      if (model.chain[i].type == ModelStepType::Tdl) {
        leading_tdl_state = &state.steps[i];
        break;
      }
    }
    std::size_t dl_size_samples = 0;
    if (leading_tdl_state) {
      std::fill(leading_tdl_state->delay_line.begin(),
                leading_tdl_state->delay_line.end(), IqSample{});
      dl_size_samples = leading_tdl_state->delay_line.size();
    }
    const std::size_t count_samples = input.size();
    const std::uint64_t warmup_slots = count_samples == 0
        ? 1
        : ((dl_size_samples + count_samples - 1) / count_samples);
    // The snap call we just completed is the FIRST slot to run with the
    // new profile (snap_idx == state.next_slot - 1). Warmup ends when
    // the link finishes `warmup_slots` slots, so end-slot = snap_idx + warmup_slots.
    state.warmup_until_slot = snap_idx + warmup_slots;
    std::cout << "event=control_warmup_begin slot=" << snap_idx
              << " link_id=" << link_key_value
              << " dl_samples=" << dl_size_samples
              << " warmup_slots=" << warmup_slots << '\n';
  }

  // v2.2 W2: emit end-event when this slot is the one that closes the
  // warmup window. `warmup_until_slot - 1` is the last warmup slot (so
  // snap_idx == warmup_until_slot means "first post-warmup slot").
  if (state.warmup_until_slot != 0 && snap_idx >= state.warmup_until_slot) {
    std::cout << "event=control_warmup_end slot=" << snap_idx
              << " link_id=" << link_key_value << '\n';
    state.warmup_until_slot = 0;
  }

  // v3.0 TM1: publish the per-link telemetry snapshot. Cheap (seqlock
  // pre-bump + POD copy + post-bump); the telemetry thread reads at
  // its own cadence and tolerates a torn-read retry if it lands mid-
  // write.
  {
    TelemetrySnapshot ts;
    ts.slot              = snap_idx;
    ts.live_seqno        = state.live_seqno;
    ts.live              = state.live;
    ts.profile_active    = state.live_profile_active;
    ts.warmup_until_slot = state.warmup_until_slot;
    publish_telemetry_snapshot(state.ctl, ts);
  }

  std::copy(input.begin(), input.end(), state.scratch_a.begin());
  std::span<IqSample> current(state.scratch_a.data(), input.size());
  std::span<IqSample> next(state.scratch_b.data(), input.size());

  for (std::size_t step_index = 0; step_index != model.chain.size(); ++step_index) {
    const auto& step = model.chain[step_index];
    StepState& step_state = state.steps[step_index];

    switch (step.type) {
      case ModelStepType::PathLoss: {
        // Phase 3 C2a: path_loss_db sourced from per-link `live` (populated
        // from YAML at prepare; will be overwritten by snap-from-shadow in
        // C2b once the control plane is wired).
        const float factor = static_cast<float>(std::pow(10.0, -state.live.path_loss_db / 20.0));
        for (std::size_t i = 0; i != current.size(); ++i) {
          next[i] = scale(current[i], factor);
        }
        break;
      }
      case ModelStepType::Awgn: {
        // v1-fin-A: AWGN with two source modes.
        //   - explicit `noise_power`: an absolute knob, independent of
        //     input power. Stays YAML-only; not runtime-mutable in v1
        //     because the runtime control plane works in dB-relative
        //     terms and absolute power lacks a meaningful unit at the
        //     control surface.
        //   - implicit `snr_db`: relative to measured input power.
        //     Sourced from per-link `live.awgn_snr_db` and IS runtime-
        //     mutable. σ derives per slot from current power + live SNR.
        double noise_power = param_or(step, "noise_power", -1.0);
        if (noise_power < 0.0) {
          const double snr_db = static_cast<double>(state.live.awgn_snr_db);
          noise_power = estimate_average_power(current) /
                        std::pow(10.0, snr_db / 10.0);
        }
        const double sigma = std::sqrt(std::max(0.0, noise_power) / 2.0);
        const std::normal_distribution<float>::param_type params(0.0F, static_cast<float>(sigma));
        for (std::size_t i = 0; i != current.size(); ++i) {
          next[i] = {current[i].i + step_state.noise(step_state.rng, params),
                     current[i].q + step_state.noise(step_state.rng, params)};
        }
        break;
      }
      case ModelStepType::Phase:
      case ModelStepType::Cfo: {
        // Phase 3 C2a: cfo_hz sourced from per-link `live`. phase_rad stays on
        // the step (not a v1 mutable param).
        const double fixed_phase = param_or(step, "phase_rad", 0.0);
        const double cfo_hz = static_cast<double>(state.live.cfo_hz);
        const double phase_increment =
            sample_rate_hz == 0 ? 0.0 : 2.0 * std::numbers::pi * cfo_hz / static_cast<double>(sample_rate_hz);
        for (std::size_t i = 0; i != current.size(); ++i) {
          next[i] = rotate(current[i], fixed_phase + step_state.phase_rad);
          step_state.phase_rad += phase_increment;
          if (step_state.phase_rad > 2.0 * std::numbers::pi) {
            step_state.phase_rad = std::fmod(step_state.phase_rad, 2.0 * std::numbers::pi);
          }
        }
        break;
      }
      case ModelStepType::Tdl: {
        // Delegated to the shared apply_tdl_step / apply_tdl_step_fading
        // helpers in delay.h so the CPU and CUDA backends call literally the
        // same multi-tap convolution.
        //
        // Phase 3 v1-fin-C: tap 0 is runtime-mutable on this backend too.
        // Build effective_taps each slot by copying step.taps and overriding
        // [0] with values from per-link `live`; mirror that into
        // effective_polyphase (live keeps tap0_delay integer in v1, so
        // polyphase[0] collapses to a unit impulse at i=3). The other taps
        // pass through unchanged from the YAML chain and the cached
        // polyphase. Per-slot copy cost is O(n_taps) ≤ 32 — negligible
        // compared to the per-sample convolution.
        std::vector<ocg::TapSpec> effective_taps;
        std::vector<std::array<float, kTdlFracFilterTaps>> effective_polyphase;

        if (state.live_profile_active) {
          // v2.0-F3: ALL taps sourced from the live profile. Polyphase
          // recomputed per-tap from each tap's fractional delay so the
          // resulting kernel output matches a fresh prepare with the new
          // profile. CPU↔CUDA parity holds post-warmup because both
          // backends derive polyphase from compute_windowed_sinc_taps.
          const int n_taps = state.live_profile.n_taps;
          effective_taps.resize(static_cast<std::size_t>(n_taps));
          effective_polyphase.resize(static_cast<std::size_t>(n_taps));
          for (int k = 0; k < n_taps; ++k) {
            effective_taps[k] = state.live_profile.taps[k];
            const double tau_int = std::floor(effective_taps[k].delay_samples);
            const double frac    = effective_taps[k].delay_samples - tau_int;
            compute_windowed_sinc_taps(frac, effective_polyphase[k]);
          }
        } else {
          // v1 path: YAML chain + per-tap-0 scalar overrides from `live`.
          effective_taps = step.taps;
          effective_polyphase = step_state.tdl_polyphase;
          if (!effective_taps.empty()) {
            effective_taps[0].delay_samples = static_cast<double>(state.live.tap0_delay_samples);
            effective_taps[0].gain_db       = static_cast<double>(state.live.tap0_gain_db);
            effective_taps[0].phase_rad     = static_cast<double>(state.live.tap0_phase_rad);
            if (effective_taps[0].is_los) {
              effective_taps[0].los_k_db = static_cast<double>(state.live.los_k_db);
            }
            if (!effective_polyphase.empty()) {
              const double tau_int = std::floor(effective_taps[0].delay_samples);
              const double frac    = effective_taps[0].delay_samples - tau_int;
              compute_windowed_sinc_taps(frac, effective_polyphase[0]);
            }
          }
        }

        if (step.fading_enabled) {
          apply_tdl_step_fading(current.data(), next.data(), current.size(),
                                effective_taps, effective_polyphase,
                                step_state.delay_line, step_state.tdl_fading,
                                sample_rate_hz);
        } else {
          apply_tdl_step(current.data(), next.data(), current.size(),
                         effective_taps, effective_polyphase,
                         step_state.delay_line);
        }
        break;
      }
    }
    std::swap(current, next);
  }

  std::copy(current.begin(), current.end(), output.begin());
}

void CpuChannelProcessor::process_superposition(const std::string& dst_key,
                                                const std::vector<SuperpositionInput>& inputs,
                                                const ModelConfig* rx_model,
                                                std::uint64_t sample_rate_hz,
                                                std::span<std::span<IqSample>> outputs)
{
  // Reference superposition: shape each incoming lane through its own model,
  // then sum it into the output row its `rx_port` names. The CUDA backend
  // fuses this into one kernel; here it stays a plain loop so it can serve as
  // the correctness reference.
  if (outputs.empty()) {
    return;
  }
  // Every row is one slot of the same window, so they share a length.
  const std::size_t count = outputs[0].size();
  for (const auto& row : outputs) {
    if (row.size() != count) {
      throw std::runtime_error("CPU superposition output rows have unequal lengths");
    }
    std::fill(row.begin(), row.end(), IqSample{});
  }
  if (count == 0) {
    return;
  }
  // Reused across calls on this thread (the broker runs one producer thread
  // per destination node), so a serve does not allocate.
  thread_local IqBuffer scratch;
  if (scratch.size() < count) {
    scratch.resize(count);
  }
  const std::span<IqSample> shaped(scratch.data(), count);
  for (const auto& lane : inputs) {
    if (lane.model == nullptr || lane.samples.size() != count) {
      throw std::runtime_error("CPU superposition input is malformed");
    }
    if (lane.rx_port < 0 ||
        static_cast<std::size_t>(lane.rx_port) >= outputs.size()) {
      throw std::runtime_error("CPU superposition lane rx_port is out of range: " +
                               lane.link_key);
    }
    apply_chain_to_link(lane.link_key, *lane.model, lane.samples, shaped, sample_rate_hz);
    const std::span<IqSample> row = outputs[static_cast<std::size_t>(lane.rx_port)];
    for (std::size_t s = 0; s != count; ++s) {
      row[s] += scratch[s];
    }
  }
  // Receiver model (noise floor) applied once per row to that row's sum, each
  // row against its OWN state so sibling rows do not share the receiver chain's
  // CFO phase and delay line. At Nr = 1 the key is the pre-M1 "<node>>rx".
  if (rx_model != nullptr) {
    const int nr = static_cast<int>(outputs.size());
    for (int r = 0; r != nr; ++r) {
      const std::span<IqSample> row = outputs[static_cast<std::size_t>(r)];
      const std::span<const IqSample> summed(row.data(), row.size());
      apply_chain_to_link(rx_state_key(dst_key, r, nr), *rx_model, summed, row, sample_rate_hz);
    }
  }
}

std::unordered_map<std::string, BrokerLinkControl*>
CpuChannelProcessor::collect_control_links()
{
  // Walk every per-link state struct created at prepare() and expose its
  // BrokerLinkControl by link key. Pointers stay stable for the lifetime
  // of `states_` (no rehashing on read; the broker calls collect_control_
  // links() once after prepare() and hands the map to ControlServer).
  std::unordered_map<std::string, BrokerLinkControl*> out;
  out.reserve(states_.size());
  for (auto& [key, state] : states_) {
    out.emplace(key, &state.ctl);
  }
  return out;
}

} // namespace ocg
