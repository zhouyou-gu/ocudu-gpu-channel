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
  // Capacity of each RX port's output ring, in batches. The broker's producer
  // may run ahead by one batch; the remaining capacity is slack so a full batch
  // can be pushed while the REP worker is still draining the previous one.
  //
  // This ring is the one buffer the pre-MIMO design did not have -- it computed
  // each reply on demand -- so it adds directly to one-way latency. At the
  // reference 23.04 MS/s with a 23040-sample batch, the one-batch run-ahead
  // bound is 1 ms. Lower it if a live attach shows the added delay eating the
  // slot budget; Msg3 PUSCH is the thinnest margin in this system on record.
  std::size_t rx_ring_batches = 2;
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

// A RadioNode: the owner of a common sample epoch and, from M1 onward, of the
// canonical matrix index of its transport ports.
//
// `tx_ports` and `rx_ports` name Devices. **Writing order IS the matrix index**:
// tx_ports[0] is t = 0, rx_ports[1] is r = 1. Nothing parses a numeric suffix
// off a Device id to infer order -- names like `p0`/`p1` are for humans, the
// emulator only reads position. The two lists are independent, so a node may be
// asymmetric (Nt != Nr), and the same Device normally appears in both.
//
// Declaring `radio_nodes` is all-or-nothing: if the block is present, every
// Device must be claimed by exactly one node. Partial declaration is rejected
// because a half-declared topology silently leaves the remaining Devices as
// implicit singletons, and which matrix index they end up with would depend on
// parse order rather than on anything the author wrote.
//
// Omitting the block entirely keeps the pre-M1 behaviour: every Device lowers
// to its own implicit singleton node with Nt = Nr = 1.
struct RadioNodeConfig {
  std::string id;
  std::vector<std::string> tx_ports;
  std::vector<std::string> rx_ports;
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

// One entry of a `fixed_mimo` coefficient list: the complex weight applied to
// tap `tap` on the lane (rx, tx).
struct MimoCoefficient {
  int tap = 0;
  int rx = 0;
  int tx = 0;
  double real = 0.0;
  double imag = 0.0;
};

// ---------------------------------------------------------------------------
// Spatial correlation (M3)
//
// The lanes of one physical link are realisations of one channel, and in a real
// radio they are not independent: antennas sit centimetres apart, so the paths
// they see resemble each other. `spatial_correlation` declares that resemblance
// as the covariance R of the lane vector, and the emulator draws lanes with it.
enum class SpatialCorrelationKind {
  Iid,       // R = I. The M2 behaviour, and the default.
  Kronecker, // R = R_rx (x) R_tx in lane order l = r*Nt + t (see docs/plans/m3-*).
};

// One OFF-DIAGONAL entry of a Hermitian correlation matrix, upper triangle only
// (i < j). The diagonal is 1 by definition and the lower triangle is the
// conjugate mirror, so neither can be written down: a matrix that is not
// Hermitian, or whose diagonal is not unit, is unrepresentable here rather than
// rejected after the fact. What still has to be checked is positive
// semidefiniteness, which no syntax can enforce.
struct CorrelationEntry {
  int i = 0;
  int j = 0;
  double re = 0.0;
  double im = 0.0;
};

struct SpatialCorrelationConfig {
  // Distinguishes "no block" from "a block that declared nothing".
  bool declared = false;
  SpatialCorrelationKind kind = SpatialCorrelationKind::Iid;
  std::vector<CorrelationEntry> rx; // Nr x Nr, receiver-side correlation
  std::vector<CorrelationEntry> tx; // Nt x Nt, transmitter-side correlation
};

// One lane's complex LOS coefficient. The specular (line-of-sight) part of a
// Rician tap has a DETERMINISTIC phase relationship across lanes -- it is a
// rank-1 component of H, not an independent draw per lane. This declares that
// relationship. A lane with no entry carries the default 1 + 0j.
//
// It is declared rather than computed from antenna geometry because array
// geometry, beamforming and CDL are mission non-goals (AGENT_GOAL.mimo.md)
// until the user expands the mission.
struct LosCoefficient {
  int rx = 0;
  int tx = 0;
  double re = 1.0;
  double im = 0.0;
};

struct LosMatrixConfig {
  bool declared = false;
  std::vector<LosCoefficient> coefficients;
};

struct ModelConfig {
  std::string id;
  std::vector<ModelStep> chain;
  // Model-scope fixed channel matrix, M1. Sparse: a lane/tap with no entry has
  // coefficient ZERO, not one -- a path nobody wrote down does not exist. There
  // is no implicit 1/sqrt(Nt) normalisation either; write the scaling into the
  // coefficients. Hidden scaling would make the analytic expectations that
  // justify a *fixed* matrix impossible to check.
  //
  // The coefficients apply to the chain's first `tdl` step, whose taps carry
  // the complex weight a_k. They need no new runtime field: a coefficient is
  // folded into that tap's gain and phase at load time, since a tap already
  // expresses a·e^(j phi).
  std::vector<MimoCoefficient> fixed_mimo;
  // Distinguishes "no fixed_mimo block" from "a block that declared nothing".
  bool fixed_mimo_declared = false;

  // M3. `fixed_mimo` states what H IS; `spatial_correlation` states the
  // covariance of a random H. Declaring both says H twice, so the validator
  // rejects the combination (docs/plans/m3-spatial-correlation-and-los.md
  // section 2.6).
  SpatialCorrelationConfig spatial_correlation;
  LosMatrixConfig los_matrix;
};

// The emulated network as a directed graph: `devices` are the nodes, `links`
// are the directed edges, and each edge carries a channel `model`. A node's
// received signal is the superposition of every edge arriving at it, so the
// desired signal, interference (several edges into one node), and crosstalk
// (any leakage edge) are all expressed uniformly as graph fan-in.
struct TopologyConfig {
  RuntimeConfig runtime;
  std::vector<DeviceConfig> devices;
  // Optional. Empty means every Device lowers to an implicit singleton node.
  std::vector<RadioNodeConfig> radio_nodes;
  std::vector<LinkConfig> links;
  std::map<std::string, ModelConfig> models;
};

// ---------------------------------------------------------------------------
// Resolved view (M1)
//
// `TopologyConfig` is what the author wrote. `ResolvedTopology` is what the
// emulator runs: radio nodes with their ports in canonical matrix order, and
// links expanded into lanes.
//
// It exists because three consumers -- the broker, the CPU backend and the CUDA
// backend -- must agree exactly on the lane set, the lane ordering and the
// per-lane state keys. Deriving that separately in each place is how those
// three drift apart, and a drift here is silent: the run still produces IQ, it
// is just no longer the y = Hx the topology described.
struct ResolvedNode {
  std::string id;
  // Device ids, in canonical matrix order. tx_ports[t] is t, rx_ports[r] is r.
  std::vector<std::string> tx_ports;
  std::vector<std::string> rx_ports;
  std::uint64_t sample_rate_hz = 0;
  std::string rx_model;
  // True when the node was lowered from a bare Device rather than declared.
  bool implicit = true;
};

// One lane: the (rx_port, tx_port) pair of one physical link. A scalar 1x1 link
// is the single-lane case with both indices 0.
struct LaneConfig {
  // Per-lane channel-state key, shared verbatim by the broker and both
  // backends. Built by `lane_key()`.
  std::string key;
  std::string src_node;
  std::string dst_node;
  // The transport ports this lane reads from and accumulates into.
  std::string src_device;
  std::string dst_device;
  int tx_port = 0;
  int rx_port = 0;
  std::string model_id;
  // Index of the originating entry in TopologyConfig::links.
  std::size_t link_index = 0;
  // Identity of the PHYSICAL link this lane belongs to: `link_key()` of the
  // originating entry, with no lane suffix. Every lane of one link carries the
  // same value, and it does not change when the radios' port counts change.
  //
  // It is what the stochastic channel is owned by (M2): the fading seed is
  // derived from this identity rather than from the lane key, and the absolute
  // sample time is held once per physical link rather than once per lane.
  std::string physical_link_key;
};

struct ResolvedTopology {
  std::vector<ResolvedNode> nodes;
  // Stable-sorted by (destination node, rx_port). Lanes of one row are
  // therefore contiguous, which is what lets the CUDA superposition kernel take
  // a row as a [row_begin[r], row_begin[r+1]) range. Stability keeps the
  // float summation order fixed, which CPU/CUDA parity rests on.
  std::vector<LaneConfig> lanes;
};

// The canonical per-lane state key.
//
// At Nt = Nr = 1 it is exactly `link_key(link)` -- no suffix. That exception is
// deliberate and load-bearing: it is what makes a 1x1 topology produce output
// bit-identical to the pre-M1 broker, which is M1's safety net. The condition
// lives here and nowhere else.
std::string lane_key(const std::string& base_link_key, int rx_port, int tx_port, int nt, int nr);

// The receiver-model state key for one output row of a node.
//
// Carries the same Nr = 1 exception as `lane_key` and for the same reason: at
// Nr = 1 it is exactly the pre-M1 `"<node>>rx"`. Sibling rows must not share
// the receiver chain's CFO phase and delay line, so from Nr = 2 each row gets
// its own key.
std::string rx_state_key(const std::string& node_id, int rx_port, int nr);

// Expands `config` into its resolved view. Assumes `validate_config` passed;
// throws on a reference it still cannot resolve.
ResolvedTopology resolve_topology(const TopologyConfig& config);

// ---------------------------------------------------------------------------
// Stochastic-channel seeds (M2)
//
// The random channel of a physical link is owned by that link, and each of its
// lanes takes an independent realisation derived from the one link seed. The
// two steps are separate functions so M3 can replace the "independent" part
// with a correlated draw without touching how a link is identified.
//
// Two properties this derivation is here to guarantee:
//   - a lane realisation depends on the link's identity and the lane's matrix
//     position as NUMBERS, never on how a lane key happens to be spelled, so
//     changing the key format cannot change any realisation;
//   - the mixing is written here rather than taken from std::hash, whose value
//     is not specified across standard-library implementations -- a run is
//     reproducible across toolchains, not merely across runs of one binary.
//
// Seed identity of a physical link. `base_link_key` is `link_key(link)` (i.e.
// LaneConfig::physical_link_key), so the link keeps its identity when the
// radios grow ports.
std::uint64_t physical_link_seed(const std::string& base_link_key);

// Per-lane, per-chain-step seed derived from the link seed and the lane's
// position in the matrix. Both backends call this, so a lane's realisation is
// the same on CPU and on GPU.
std::uint64_t lane_fading_seed(std::uint64_t link_seed, int rx_port, int tx_port,
                               int step_index);

// Name of the per-lane model clone synthesized for a `fixed_mimo` base model.
// Deterministic and independent of whether the clone exists yet, so
// resolve_topology can name it before expand_fixed_mimo_models creates it.
std::string fixed_mimo_model_id(const std::string& base_model_id, int rx_port, int tx_port);

// Materializes one model clone per surviving (fixed_mimo model, rx, tx) lane,
// with the lane's coefficient folded into the taps. Called by
// load_config_file after fold_link_leading_delays; programmatic builders of
// TopologyConfig must call it themselves before handing the config to a
// processor.
void expand_fixed_mimo_models(TopologyConfig& config);

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
const RadioNodeConfig* find_radio_node(const TopologyConfig& config, const std::string& id);
const ModelConfig* find_model(const TopologyConfig& config, const std::string& id);

// Canonical per-link identity ("from>to:model"), shared by the broker and the
// channel processors so they key per-link state the same way.
std::string link_key(const LinkConfig& link);

} // namespace ocg
