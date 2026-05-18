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
  Gain,
  PathLoss,
  Awgn,
  IntegerDelay,
  FractionalDelay,
  Phase,
  Cfo
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
};

// A directed edge in the graph: the channel `model` shapes IQ travelling from
// node `from` to node `to`. Multiple edges may target the same node.
struct LinkConfig {
  std::string from;
  std::string to;
  std::string model;
};

struct ModelStep {
  ModelStepType type = ModelStepType::Gain;
  std::map<std::string, double> params;
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
