#include "ocudu_gpu_channel/cpu_backend.h"
#include "ocudu_gpu_channel/correlation.h"
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
                                                                       const LaneIdentity& identity)
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
  if (state.link == nullptr) {
    // Bound once, when the state is created. A later call cannot rebind it: a
    // lane belongs to the link it was prepared under, and re-pointing it
    // mid-run is exactly the drift this design removes.
    state.link = &links_[identity.physical_link_key];
    state.lane_index = identity.lane_index();
    state.lane_count = identity.lane_count();
    const auto los = lane_los_coefficients(model.los_matrix, identity.nt, identity.nr);
    const CplxD coefficient = los[static_cast<std::size_t>(state.lane_index)];
    state.los_coefficient = std::complex<float>(static_cast<float>(coefficient.real()),
                                                static_cast<float>(coefficient.imag()));
  }
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
    const std::uint64_t link_seed = physical_link_seed(identity.physical_link_key);
    for (std::size_t i = 0; i != state.steps.size(); ++i) {
      state.steps[i].rng.seed(static_cast<unsigned>(std::hash<std::string>{}(key + ":" + std::to_string(i))));
      if (model.chain[i].type == ModelStepType::Tdl) {
        // The CUDA backend derives the seed through the same two functions,
        // so both backends draw the same Jakes sub-ray angles for a lane.
        prepare_tdl_step(state.steps[i], model.chain[i],
                         lane_fading_seed(link_seed, identity.rx_port, identity.tx_port,
                                          static_cast<int>(i)));
      }
    }
    // Phase 3 v1: populate runtime-mutable params from YAML. apply_chain_to_link
    // (post-C2a) reads path_loss_db + cfo_hz from `live`. The control plane
    // (C3+) writes to `ctl.shadow` and bumps `ctl.seqno`; snap_mutable_params()
    // at the top of every serve picks up shadow → live transitions. Initialise
    // shadow == live so the first serve's snap is a no-op.
    // Per-lane values from the lane's OWN model (a fixed_mimo lane runs a clone
    // whose taps carry that lane's matrix coefficient).
    state.live = populate_mutable_params_from_yaml(model, /*reference_power=*/0.0,
                                                   /*sample_rate_hz=*/0);
    // The link's shadow is initialised once, by the first lane to arrive.
    if (state.link->control.seqno.load(std::memory_order_relaxed) == 0 &&
        state.link->live_seqno == 0) {
      state.link->live = state.live;
      init_broker_link_control(state.link->control, state.link->live);
    }

    // v2.0-F3: cache the eligibility flag — a profile_swap is only
    // honoured when the chain starts with a tdl step (or when force=true
    // on the swap REQ). Done at prepare so the per-slot snap doesn't
    // re-walk the chain.
    state.link->chain_has_leading_tdl =
        !model.chain.empty() && model.chain.front().type == ModelStepType::Tdl;

    // v2.2 follow-on: write per-link hints for the control plane's
    // warmup-cap check. dl_size_samples_hint is the leading tdl
    // step's delay_line size (0 if no leading tdl); slot_count_hint
    // is the per-slot sample count this link receives.
    if (state.link->chain_has_leading_tdl && !state.steps.empty()) {
      state.link->control.dl_size_samples_hint =
          static_cast<int>(state.steps.front().delay_line.size());
    }
    state.link->control.slot_count_hint = static_cast<int>(sample_count);
  }
  return state;
}

void CpuChannelProcessor::refresh_lane_grids(LinkState& state, const ModelConfig& model,
                                             std::size_t count, std::uint64_t sample_rate_hz)
{
  state.link->fading.reshape(
      static_cast<std::size_t>(std::max(state.lane_count, state.lane_index + 1)),
      model.chain.size());
  auto& per_step = state.link->fading.lane_grids[static_cast<std::size_t>(state.lane_index)];
  for (std::size_t i = 0; i != model.chain.size(); ++i) {
    // Time comes from the link's clock, so every lane of the link builds its
    // grid against the same origin -- the property M2.3 made structural, now
    // relied on by a step that reads several lanes at once.
    generate_fading_grid(state.steps[i].tdl_fading, model.chain[i].taps.size(), count,
                         sample_rate_hz, state.link->clock.slot_start_samples, per_step[i]);
  }
  state.link->fading.lane_ready[static_cast<std::size_t>(state.lane_index)] = 1;
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
    ensure_link_state(lane.key, *model, count,
                      LaneIdentity{.physical_link_key = lane.physical_link_key,
                                   .rx_port = lane.rx_port,
                                   .tx_port = lane.tx_port,
                                   .nt = lane.nt,
                                   .nr = lane.nr});
  }

  // M3.4: one mixing matrix per physical link. Built here, from the resolver's
  // per-link lane grouping, so the backend never derives its own view of which
  // lanes belong together.
  for (const auto& group : resolved.link_groups) {
    int first_live = -1;
    for (int position : group.lane_index) {
      if (position >= 0) {
        first_live = position;
        break;
      }
    }
    if (first_live < 0) {
      continue;
    }
    const auto* model = find_model(config, resolved.lanes[static_cast<std::size_t>(first_live)].model_id);
    if (model == nullptr) {
      continue;
    }
    auto& fading = links_[group.physical_link_key].fading;
    fading.lanes = group.nt * group.nr;
    fading.mixing.clear();
    if (!model->spatial_correlation.declared ||
        model->spatial_correlation.kind == SpatialCorrelationKind::Iid) {
      continue; // iid links skip the mixing entirely
    }
    std::vector<CplxD> mixing;
    std::string error;
    if (!lane_mixing_matrix(model->spatial_correlation, group.nt, group.nr, mixing, error)) {
      // validate_config runs the same factorisation, so reaching here means a
      // programmatic config that never went through it.
      throw std::runtime_error("link " + group.physical_link_key + ": " + error);
    }
    fading.mixing.assign(mixing.size(), std::complex<float>{0.0F, 0.0F});
    for (std::size_t k = 0; k != mixing.size(); ++k) {
      fading.mixing[k] = std::complex<float>(static_cast<float>(mixing[k].real()),
                                             static_cast<float>(mixing[k].imag()));
    }
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
      // Each row's key already separates it from its siblings, so the row is a
      // single-lane link of its own. Passing r as a lane index here would point
      // at a row this link does not have.
      ensure_link_state(key, *model, count, LaneIdentity{.physical_link_key = key});
      rx_model_keys_.insert(key);
    }
  }
}

ocg::PhysicalLinkRuntime* CpuChannelProcessor::apply_chain_to_link(const std::string& link_key_value,
                                              const ModelConfig& model,
                                              std::span<const IqSample> input,
                                              std::span<IqSample> output,
                                              std::uint64_t sample_rate_hz)
{
  if (output.size() != input.size()) {
    throw std::runtime_error("apply_chain_to_link input and output sizes must match");
  }
  if (input.empty()) {
    return nullptr;
  }

  // prepare() has already created every state the broker serves, so this
  // lookup finds one and the identity arguments are not read. They matter only
  // for a caller that skipped prepare() (single-shot use): such a state is its
  // own physical link at matrix position (0, 0).
  LinkState& state = ensure_link_state(link_key_value, model, input.size(),
                                       LaneIdentity{.physical_link_key = link_key_value});

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

        if (state.link->live_profile_active) {
          // v2.0-F3: ALL taps sourced from the live profile. Polyphase
          // recomputed per-tap from each tap's fractional delay so the
          // resulting kernel output matches a fresh prepare with the new
          // profile. CPU↔CUDA parity holds post-warmup because both
          // backends derive polyphase from compute_windowed_sinc_taps.
          const int n_taps = state.link->live_profile.n_taps;
          effective_taps.resize(static_cast<std::size_t>(n_taps));
          effective_polyphase.resize(static_cast<std::size_t>(n_taps));
          for (int k = 0; k < n_taps; ++k) {
            effective_taps[k] = state.link->live_profile.taps[k];
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
          // Time comes from the physical link, and the same value is handed to
          // every lane of that link this slot. The call does not advance it.
          apply_tdl_step_fading(current.data(), next.data(), current.size(),
                                effective_taps, effective_polyphase,
                                step_state.delay_line, step_state.tdl_fading,
                                sample_rate_hz,
                                state.link->clock.slot_start_samples,
                                state.link->fading.lane_grids[static_cast<std::size_t>(state.lane_index)]
                                                        [step_index],
                                state.los_coefficient);
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
  return state.link;
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

  // M3.3 -- generation pass. Every lane's fading grid for this slot is built
  // here, before any lane is shaped, because the cross-lane step M3.4 inserts
  // between the two loops needs all of a link's rows at once. Nothing in this
  // loop looks at another lane yet, so the output is exactly what M2 produced.
  // M4.2 -- one pass per LINK before any of its lanes is shaped: snap the
  // control update, then build the lane grids. Both belong here for the same
  // reason: they are decisions about the link that every lane must see the same
  // way, and a lane that made them for itself could make them differently.
  thread_local std::vector<PhysicalLinkRuntime*> touched_links;
  touched_links.clear();
  for (const auto& lane : inputs) {
    if (lane.model == nullptr) {
      throw std::runtime_error("CPU superposition input is malformed");
    }
    LinkState& state = ensure_link_state(lane.link_key, *lane.model, count,
                                         LaneIdentity{.physical_link_key = lane.link_key});
    if (std::find(touched_links.begin(), touched_links.end(), state.link) ==
        touched_links.end()) {
      touched_links.push_back(state.link);
      state.link->fading.begin_slot();
      const LinkSnapOutcome outcome = snap_physical_link(*state.link, lane.link_key, count);
      if (outcome.values_changed || outcome.profile_activated) {
        // Everything the snap decided is applied to EVERY lane of this link, in
        // this slot. The values are per lane and the cross-slot rings are per
        // lane, but the decision was the link's, so the sweep is what turns
        // "same slot for every lane" from an arrangement into a fact.
        for (const auto& sibling : inputs) {
          auto it = states_.find(sibling.link_key);
          if (it == states_.end() || it->second.link != state.link) {
            continue;
          }
          if (outcome.values_changed) {
            it->second.live = state.link->live;
          }
          if (outcome.profile_activated) {
            for (auto& step : it->second.steps) {
              std::fill(step.delay_line.begin(), step.delay_line.end(), IqSample{});
            }
          }
        }
      }
    }
    refresh_lane_grids(state, *lane.model, count, sample_rate_hz);
  }

  // M3.4 -- the cross-lane step, in the gap M3.3 opened. Every lane of a link
  // has its independent grid by now; this replaces them with g = L w, so the
  // lane vector has the covariance the topology declared. An iid link returns
  // immediately and its grids are not even read.
  for (auto* link : touched_links) {
    link->fading.apply_mixing();
  }

  // Clocks touched by this slot, each advanced once at the end. Reused across
  // calls on this thread; a physical link has one destination node, so no other
  // thread can be advancing the same clock.
  thread_local std::vector<PhysicalLinkRuntime*> touched;
  touched.clear();
  const auto touch = [](PhysicalLinkRuntime* link) {
    if (link == nullptr) {
      return;
    }
    for (const auto* seen : touched) {
      if (seen == link) {
        return; // sibling lane of a link already accounted for
      }
    }
    touched.push_back(link);
  };
  for (const auto& lane : inputs) {
    if (lane.model == nullptr || lane.samples.size() != count) {
      throw std::runtime_error("CPU superposition input is malformed");
    }
    if (lane.rx_port < 0 ||
        static_cast<std::size_t>(lane.rx_port) >= outputs.size()) {
      throw std::runtime_error("CPU superposition lane rx_port is out of range: " +
                               lane.link_key);
    }
    touch(apply_chain_to_link(lane.link_key, *lane.model, lane.samples, shaped,
                              sample_rate_hz));
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
      const std::string key = rx_state_key(dst_key, r, nr);
      LinkState& rx_state = ensure_link_state(key, *rx_model, count,
                                              LaneIdentity{.physical_link_key = key});
      rx_state.link->fading.begin_slot();
      if (snap_physical_link(*rx_state.link, key, count).values_changed) {
        rx_state.live = rx_state.link->live;
      }
      refresh_lane_grids(rx_state, *rx_model, count, sample_rate_hz);
      touch(apply_chain_to_link(key, *rx_model, summed, row, sample_rate_hz));
    }
  }
  // The slot is complete: every lane of a link has now been shaped against the
  // same time origin, so the link's clock moves on by exactly one slot. Doing
  // it here, once per link rather than once per lane, is what makes "the lanes
  // of a link share a time" a property of the code and not of the caller.
  for (auto* link : touched) {
    link->clock.slot_start_samples += count;
  }
}

std::unordered_map<std::string, BrokerLinkControl*>
CpuChannelProcessor::collect_control_links()
{
  // Walk every per-link state struct created at prepare() and expose its
  // BrokerLinkControl by link key. Pointers stay stable for the lifetime
  // of `states_` (no rehashing on read; the broker calls collect_control_
  // links() once after prepare() and hands the map to ControlServer).
  // M4.2: one entry per PHYSICAL LINK, keyed by the link's own identity -- so a
  // 2x2 link is one control endpoint rather than four, and the address carries
  // no lane suffix. At Nt = Nr = 1 the lane key and the link key are the same
  // string, so an existing 1x1 deployment's link_id does not change.
  //
  // Receiver-model rows are deliberately absent: they are node rows, not links.
  // The CUDA backend never exposed them, and the two backends disagreeing meant
  // the same REQ succeeded on one and was rejected on the other.
  std::unordered_map<std::string, BrokerLinkControl*> out;
  out.reserve(links_.size());
  for (auto& [key, link] : links_) {
    if (rx_model_keys_.count(key) != 0) {
      continue;
    }
    out.emplace(key, &link.control);
  }
  return out;
}

} // namespace ocg
