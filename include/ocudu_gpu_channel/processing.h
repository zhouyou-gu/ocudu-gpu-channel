#pragma once

#include "ocudu_gpu_channel/config.h"
#include "ocudu_gpu_channel/iq.h"
#include <memory>
#include <span>
#include <string>
#include <unordered_map>
#include <vector>

namespace ocg { struct BrokerLinkControl; }

// Channel-processing backend interface and selection.
//
// The emulator has two backends, each in its own translation unit:
//   - cuda_backend (cuda_backend.h / .cu): the PRIMARY target — GPU-accelerated
//     channel emulation built to scale to many concurrent gNB/UE links.
//   - cpu_backend  (cpu_backend.h / .cpp): the reference/fallback backend.
//
// Both implement the ChannelProcessor interface below. create_channel_processor()
// picks one from the topology's runtime.backend (CUDA by default).

namespace ocg {

struct ProcessorTimings {
  double h2d_us = 0.0;
  double kernel_us = 0.0;
  double d2h_us = 0.0;
  double gpu_process_us = 0.0;
  // True when the most recent CUDA process_superposition call dispatched
  // through the device-channel kernel (Phase 2 D2b path). False when it fell
  // back to host-side stage_link (CUDA path before Phase 2, or when a node
  // has any non-tdl-leading incoming edge). Tests assert this so a
  // regression in the dispatch gate can't silently revert to host staging
  // -- parity would still hold but the 183x perf win would be lost.
  bool used_device_channel = false;
};

// One incoming lane of a superposition: the lane's channel model and the
// source port's current TX batch. `samples` is borrowed for the call only.
//
// A lane is the (rx_port, tx_port) pair of one physical link. For a scalar
// 1x1 link there is exactly one lane and both indices are 0, which is the
// pre-MIMO meaning of "one incoming edge" -- the field names changed, the
// semantics for existing topologies did not.
struct SuperpositionInput {
  std::string link_key;
  const ModelConfig* model = nullptr;
  std::span<const IqSample> samples;
  // Which output row this lane accumulates into: the destination radio's
  // RX-port index. Every lane with the same rx_port sums into the same row.
  int rx_port = 0;
  // The source radio's TX-port index. Lanes sharing a tx_port read identical
  // input bytes, which is what the CUDA backend's source-dedup path keys on.
  int tx_port = 0;
};

class ChannelProcessor {
public:
  virtual ~ChannelProcessor() = default;

  // prepare() must be called once for the whole topology before
  // process_superposition(). It preallocates all per-link and per-node state,
  // so process_superposition() can then be called concurrently from different
  // threads as long as each thread uses a distinct dst_key (one server thread
  // per destination device, in the broker).
  virtual void prepare(const TopologyConfig& config) = 0;

  // Processes every incoming lane of destination node `dst_key` through its
  // own channel model and accumulates it into the output row named by that
  // lane's `rx_port`, then applies the node's optional receiver model
  // `rx_model` (a thermal-noise floor) once per row -- writing the node's
  // received signal (desired + interference + crosstalk + noise) into
  // `outputs`. `rx_model` may be null. On the CUDA backend the per-lane
  // shaping, the summation, and the receiver model all run on the GPU in a
  // single fused launch.
  //
  // Every row in `outputs` must be the same length, and that length is the
  // per-lane sample count. `outputs.size()` is the destination radio's RX
  // port count Nr; a scalar destination passes one row and every lane's
  // rx_port is 0, which is exactly the pre-MIMO behaviour.
  //
  // Single-lane processing is just the N=1 case: pass a one-element inputs
  // list with rx_model = nullptr.
  virtual void process_superposition(const std::string& dst_key,
                                     const std::vector<SuperpositionInput>& inputs,
                                     const ModelConfig* rx_model,
                                     std::uint64_t sample_rate_hz,
                                     std::span<std::span<IqSample>> outputs) = 0;

  // Single-row adapter for scalar destinations. This is a non-virtual
  // convenience wrapper over the row-vector entry point above, not a second
  // processing path: it forwards to the same virtual call with Nr = 1.
  // Derived classes must pull it back into scope with
  //   using ChannelProcessor::process_superposition;
  // because overriding the virtual otherwise hides this overload.
  void process_superposition(const std::string& dst_key,
                             const std::vector<SuperpositionInput>& inputs,
                             const ModelConfig* rx_model,
                             std::uint64_t sample_rate_hz,
                             std::span<IqSample> output)
  {
    std::span<IqSample> rows[1] = {output};
    process_superposition(dst_key, inputs, rx_model, sample_rate_hz,
                          std::span<std::span<IqSample>>(rows));
  }

  virtual ProcessorTimings last_timings() const = 0;
  virtual const char* backend_name() const = 0;

  // Phase 3 C3: return a non-owning map from link_id (e.g. "ue0-gnb0") to
  // the BrokerLinkControl the backend allocated for that link. The broker
  // hands this to the ControlServer so REQs can find the right shadow.
  // Pointers stay valid for the life of the processor; map is built by
  // walking the backend's per-link state after prepare(). Default impl
  // returns an empty map for backends that have not yet wired it.
  virtual std::unordered_map<std::string, BrokerLinkControl*>
  collect_control_links() { return {}; }
};

// Returns the model steps in `config` the CUDA backend cannot run; empty when
// the topology is fully GPU-supported. Today every shipped step type
// (path_loss, awgn, phase, cfo, tdl) is GPU-supported, but `tdl` must lead the
// chain (it runs host-side during staging); a non-leading tdl is reported here.
std::vector<std::string> validate_cuda_support(const TopologyConfig& config);

// Same idea for the CPU backend: returns the model steps the CPU chain loop
// has no handler for. Today the CPU loop handles every step type at any
// position; this returns an empty vector for well-formed topologies. Exposed
// so external builders of TopologyConfig can pre-check.
std::vector<std::string> validate_cpu_support(const TopologyConfig& config);

// Builds the processor for config.runtime.backend (Backend::Cuda by default).
std::unique_ptr<ChannelProcessor> create_channel_processor(const TopologyConfig& config);

} // namespace ocg
