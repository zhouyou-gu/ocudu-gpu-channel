#pragma once

#include <cstddef>
#include <cstdint>
#include <map>
#include <optional>
#include <string>
#include <vector>

namespace ocg {

enum class Backend {
  Cpu,
  Cuda
};

enum class ModelStepType {
  PathLoss,
  Awgn,
  Phase,
  Cfo,
  Tdl
};

// Doppler power spectrum shape applied to faded taps. Phase 1.4 implements the
// Jakes (cosine-symmetric, classical) generator -- the canonical choice for
// 3GPP TR 38.901 TDL profiles. Gaussian and Flat are reserved in the enum so
// the YAML schema accepts them and a later phase can drop in their generator
// math; today they throw "not yet implemented" at processor build time.
enum class FadingSpectrum {
  Jakes,
  Gaussian,
  Flat
};

// One tap of a `tdl` (tapped delay line) chain step. When the parent step has
// no `fading:` sub-config, the tap's complex weight is the time-invariant
//   a_k = 10^(gain_db/20) · exp(j·phase_rad).
// When fading is enabled on the parent step, `gain_db` is reinterpreted as the
// tap's mean power in dB (E[|a_k(t)|^2] = 10^(gain_db/10)) and the
// instantaneous value evolves as a stationary stochastic process with a
// Doppler-shaped autocorrelation. `is_los = true` adds a Rician specular path
// to that tap: the deterministic component has Doppler shift
// `f_d_max · cos(los_angle_rad)` and Rician K-factor `los_k_db` (linear
// K = 10^(los_k_db/10)). LOS fields are ignored when the parent step has no
// fading sub-config.
//
// Two taps with the same `delay_samples` are rejected by the validator --
// collapse them into one with the summed complex gain instead.
struct TapSpec {
  double delay_samples = 0.0;
  double gain_db = 0.0;
  double phase_rad = 0.0;
  bool is_los = false;
  double los_k_db = 0.0;
  double los_angle_rad = 0.0;
};

struct RuntimeConfig {
  Backend backend = Backend::Cuda;
  int gpu_device = 0;
  bool batch_samples_auto = true;
  std::size_t batch_samples = 0;
  std::size_t queue_samples = 614400;
};

// A node in the channel-emulation graph. gNBs and UEs are the SAME class -- a
// node is just a ZMQ endpoint pair and a sample rate. `role` is an optional
// free-form label for humans/logs ("gnb", "ue", "interferer", ...); the
// emulator never branches on it.
//
// `rx_model` is an optional receiver-side model id, applied once to the node's
// summed received signal -- the place for a thermal-noise floor, so N incoming
// edges share one noise floor instead of N. Empty = no receiver model.
struct DeviceConfig {
  std::string id;
  std::string role = "node";
  std::uint64_t sample_rate_hz = 23040000;
  std::string tx_endpoint;
  std::string rx_endpoint;
  std::string rx_model;
  // Constant TX-start-time offset for THIS endpoint, in samples. Models the
  // case where one radio brought its ZMQ socket up later than others — the lag
  // is a property of the device, not of any particular link. Applies uniformly
  // to every outgoing link. Folded into the chain-leading delay step at load
  // time; fractional values promote the leading step to FractionalDelay.
  //
  // Distinct from LinkConfig::propagation_delay_samples below — that one is a
  // per-link (geometry-driven) physical propagation delay.
  double tx_timing_offset_samples = 0.0;
};

// A directed edge in the graph: the channel `model` shapes IQ travelling from
// node `from` to node `to`. Multiple edges may target the same node.
struct LinkConfig {
  std::string from;
  std::string to;
  std::string model;
  // Physical propagation delay along THIS edge, in samples. Models the time it
  // takes for the source's signal to reach the receiver — a per-link, geometry-
  // driven effect (one sample at 23.04 MS/s is ~13 m of free-space propagation).
  // Folded into the chain-leading delay step at load time, summed with the
  // source device's tx_timing_offset_samples.
  //
  // Distinct from DeviceConfig::tx_timing_offset_samples above — that one is a
  // per-source constant TX-start-time offset, not tied to link geometry. From
  // the receiver's perspective the two effects are indistinguishable, but
  // exposing them separately lets a topology express each cleanly.
  double propagation_delay_samples = 0.0;
};

struct ModelStep {
  ModelStepType type = ModelStepType::Tdl;
  std::map<std::string, double> params;
  // Used only by `tdl` steps; empty for every other step type. Populated by the
  // YAML parser when a `taps:` block list is present under the step.
  std::vector<TapSpec> taps;
  // Set by the YAML parser when it sees a `taps:` key under this step, even if
  // the block is empty. Distinguishes "user wrote no taps:" (false) from "user
  // wrote taps: but added no items" (true) so the validator can reject the
  // latter on non-tdl steps -- otherwise an empty `taps:` block on `gain` would
  // be silently swallowed.
  bool taps_declared = false;
  // Fading sub-config (Phase 1.4). When `fading_enabled` is true on a `tdl`
  // step, every tap's gain becomes a time-varying stochastic process with the
  // chosen Doppler power spectrum. `fading_f_d_max_hz` is the per-LINK maximum
  // Doppler frequency (v · f_c / c) -- all taps share it; per-tap variation
  // comes from random sub-ray angle draws keyed by Philox at prepare time.
  // `fading_grid_us` is the coarse-grid stride at which g_k(t) is sampled
  // before linear interpolation up to per-sample resolution; default 1.0 us
  // (~kHz) keeps interpolation error well below the AWGN floor for any
  // practical f_d_max while bounding the kernel's sinusoid count.
  bool fading_enabled = false;
  double fading_f_d_max_hz = 0.0;
  FadingSpectrum fading_spectrum = FadingSpectrum::Jakes;
  double fading_grid_us = 100.0;  // 100 us == 10 kHz; ~28x oversampling vs 350 Hz Jakes
};

struct ModelConfig {
  std::string id;
  std::vector<ModelStep> chain;
};

// The emulated network as a directed graph: `devices` are the nodes, `links`
// are the directed edges, and each edge carries a channel `model`. A node's
// received signal is the superposition of every edge arriving at it, so the
// desired signal, interference (several edges into one node), and crosstalk
// (any leakage edge) are all expressed uniformly as graph fan-in.
struct TopologyConfig {
  RuntimeConfig runtime;
  std::vector<DeviceConfig> devices;
  std::vector<LinkConfig> links;
  std::map<std::string, ModelConfig> models;
};

TopologyConfig load_config_file(const std::string& path);
std::vector<std::string> validate_config(const TopologyConfig& config);

// Composes the two delay knobs — the source device's tx_timing_offset_samples
// (per-endpoint constant TX-start-time offset) and the link's
// propagation_delay_samples (per-edge geometry-driven propagation delay) —
// into the chain-leading delay step of a per-link synthesized model clone.
// The two offsets are physically distinct but indistinguishable at the
// receiver, so they are summed before being merged with (or prepended to) the
// model chain's leading sample-delay step. Called automatically by
// load_config_file; programmatic builders of TopologyConfig must call this
// themselves before handing the config to a channel processor.
void fold_link_leading_delays(TopologyConfig& config);

std::string to_string(Backend backend);
std::string to_string(ModelStepType type);

Backend parse_backend(const std::string& value);
ModelStepType parse_model_step_type(const std::string& value);

std::size_t resolve_batch_samples(const RuntimeConfig& runtime, std::uint64_t sample_rate_hz);
const DeviceConfig* find_device(const TopologyConfig& config, const std::string& id);
const ModelConfig* find_model(const TopologyConfig& config, const std::string& id);

// Canonical per-link identity ("from>to:model"), shared by the broker and the
// channel processors so they key per-link state the same way.
std::string link_key(const LinkConfig& link);

} // namespace ocg
