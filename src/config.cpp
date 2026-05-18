#include "ocudu_gpu_channel/config.h"
#include <algorithm>
#include <cctype>
#include <fstream>
#include <set>
#include <sstream>
#include <stdexcept>

namespace ocg {
namespace {

std::string trim(std::string value)
{
  auto is_space = [](unsigned char ch) { return std::isspace(ch) != 0; };
  value.erase(value.begin(), std::find_if(value.begin(), value.end(), [&](char ch) { return !is_space(ch); }));
  value.erase(std::find_if(value.rbegin(), value.rend(), [&](char ch) { return !is_space(ch); }).base(), value.end());
  return value;
}

std::string strip_comment(std::string value)
{
  const auto pos = value.find('#');
  if (pos != std::string::npos) {
    value.resize(pos);
  }
  return value;
}

int indent_of(const std::string& line)
{
  int indent = 0;
  while (indent < static_cast<int>(line.size()) && line[static_cast<std::size_t>(indent)] == ' ') {
    ++indent;
  }
  return indent;
}

std::pair<std::string, std::string> split_key_value(const std::string& line)
{
  const auto pos = line.find(':');
  if (pos == std::string::npos) {
    return {trim(line), ""};
  }
  std::string key = trim(line.substr(0, pos));
  std::string value = trim(line.substr(pos + 1));
  if (value.size() >= 2 &&
      ((value.front() == '"' && value.back() == '"') || (value.front() == '\'' && value.back() == '\''))) {
    value = value.substr(1, value.size() - 2);
  }
  return {key, value};
}

double parse_double(const std::string& value, const std::string& key)
{
  try {
    std::size_t parsed = 0;
    const double result = std::stod(value, &parsed);
    if (parsed != value.size()) {
      throw std::runtime_error("trailing characters");
    }
    return result;
  } catch (const std::exception&) {
    throw std::runtime_error("invalid numeric value for " + key + ": " + value);
  }
}

std::uint64_t parse_u64(const std::string& value, const std::string& key)
{
  try {
    if (!value.empty() && value.front() == '-') {
      throw std::runtime_error("negative value");
    }
    std::size_t parsed = 0;
    const auto result = static_cast<std::uint64_t>(std::stoull(value, &parsed));
    if (parsed != value.size()) {
      throw std::runtime_error("trailing characters");
    }
    return result;
  } catch (const std::exception&) {
    throw std::runtime_error("invalid unsigned value for " + key + ": " + value);
  }
}

std::size_t parse_size(const std::string& value, const std::string& key)
{
  return static_cast<std::size_t>(parse_u64(value, key));
}

enum class Section {
  None,
  Runtime,
  Devices,
  Links,
  Models
};

void apply_runtime(RuntimeConfig& runtime, const std::string& key, const std::string& value)
{
  if (key == "backend") {
    runtime.backend = parse_backend(value);
  } else if (key == "gpu_device") {
    runtime.gpu_device = static_cast<int>(parse_u64(value, key));
  } else if (key == "batch_samples") {
    if (value == "auto") {
      runtime.batch_samples_auto = true;
      runtime.batch_samples = 0;
    } else {
      runtime.batch_samples_auto = false;
      runtime.batch_samples = parse_size(value, key);
    }
  } else if (key == "queue_samples") {
    runtime.queue_samples = parse_size(value, key);
  } else {
    throw std::runtime_error("unknown runtime key: " + key);
  }
}

void apply_device(DeviceConfig& device, const std::string& key, const std::string& value)
{
  if (key == "id") {
    device.id = value;
  } else if (key == "role") {
    device.role = parse_device_role(value);
  } else if (key == "sample_rate_hz") {
    device.sample_rate_hz = parse_u64(value, key);
  } else if (key == "tx_endpoint") {
    device.tx_endpoint = value;
  } else if (key == "rx_endpoint") {
    device.rx_endpoint = value;
  } else {
    throw std::runtime_error("unknown device key: " + key);
  }
}

void apply_link(LinkConfig& link, const std::string& key, const std::string& value)
{
  if (key == "from") {
    link.from = value;
  } else if (key == "to") {
    link.to = value;
  } else if (key == "model") {
    link.model = value;
  } else {
    throw std::runtime_error("unknown link key: " + key);
  }
}

void apply_step(ModelStep& step, const std::string& key, const std::string& value)
{
  if (key == "type") {
    step.type = parse_model_step_type(value);
  } else if (!value.empty()) {
    step.params[key] = parse_double(value, key);
  } else {
    throw std::runtime_error("unknown empty model step key: " + key);
  }
}

bool is_allowed_param(ModelStepType type, const std::string& key)
{
  switch (type) {
    case ModelStepType::Gain:
      return key == "gain_db";
    case ModelStepType::PathLoss:
      return key == "path_loss_db";
    case ModelStepType::Awgn:
      return key == "snr_db" || key == "noise_power";
    case ModelStepType::IntegerDelay:
    case ModelStepType::FractionalDelay:
      return key == "delay_samples";
    case ModelStepType::Phase:
      return key == "phase_rad";
    case ModelStepType::Cfo:
      return key == "cfo_hz" || key == "phase_rad";
  }
  return false;
}

} // namespace

TopologyConfig load_config_file(const std::string& path)
{
  std::ifstream input(path);
  if (!input) {
    throw std::runtime_error("unable to open config file: " + path);
  }

  TopologyConfig config;
  Section section = Section::None;
  DeviceConfig* current_device = nullptr;
  LinkConfig* current_link = nullptr;
  ModelConfig* current_model = nullptr;
  ModelStep* current_step = nullptr;

  std::string raw_line;
  unsigned line_number = 0;
  while (std::getline(input, raw_line)) {
    ++line_number;
    const std::string without_comment = strip_comment(raw_line);
    if (trim(without_comment).empty()) {
      continue;
    }

    // This is a minimal block-style YAML reader. Reject the two inputs it would
    // otherwise misparse with a confusing key error instead of a clear one.
    for (char ch : without_comment) {
      if (ch == '\t') {
        throw std::runtime_error("tab indentation is not supported at line " + std::to_string(line_number) +
                                 "; use spaces");
      }
      if (ch != ' ') {
        break;
      }
    }

    const int indent = indent_of(without_comment);
    std::string line = trim(without_comment);

    // Reject flow-style mappings/sequences (`{...}` / `[...]`) used as a value
    // or list item. Checking only the value side avoids a false positive on a
    // bracketed IPv6 endpoint such as `tcp://[::1]:2000`.
    {
      std::string probe = line;
      if (probe.rfind("- ", 0) == 0) {
        probe = trim(probe.substr(2));
      }
      const auto colon = probe.find(':');
      const std::string value_part = colon == std::string::npos ? probe : trim(probe.substr(colon + 1));
      const bool bare_flow = !probe.empty() && (probe.front() == '{' || probe.front() == '[');
      const bool value_flow = !value_part.empty() && (value_part.front() == '{' || value_part.front() == '[');
      if (bare_flow || value_flow) {
        throw std::runtime_error("flow-style YAML is not supported at line " + std::to_string(line_number) +
                                 "; use block style");
      }
    }

    if (indent == 0) {
      current_device = nullptr;
      current_link = nullptr;
      current_model = nullptr;
      current_step = nullptr;
      if (line == "runtime:") {
        section = Section::Runtime;
      } else if (line == "devices:") {
        section = Section::Devices;
      } else if (line == "links:") {
        section = Section::Links;
      } else if (line == "models:") {
        section = Section::Models;
      } else {
        throw std::runtime_error("unknown top-level key at line " + std::to_string(line_number) + ": " + line);
      }
      continue;
    }

    if (section == Section::Runtime) {
      auto [key, value] = split_key_value(line);
      apply_runtime(config.runtime, key, value);
      continue;
    }

    if (section == Section::Devices) {
      if (line.rfind("- ", 0) == 0) {
        config.devices.emplace_back();
        current_device = &config.devices.back();
        auto [key, value] = split_key_value(trim(line.substr(2)));
        apply_device(*current_device, key, value);
      } else if (current_device != nullptr) {
        auto [key, value] = split_key_value(line);
        apply_device(*current_device, key, value);
      } else {
        throw std::runtime_error("malformed devices section at line " + std::to_string(line_number));
      }
      continue;
    }

    if (section == Section::Links) {
      if (line.rfind("- ", 0) == 0) {
        config.links.emplace_back();
        current_link = &config.links.back();
        auto [key, value] = split_key_value(trim(line.substr(2)));
        apply_link(*current_link, key, value);
      } else if (current_link != nullptr) {
        auto [key, value] = split_key_value(line);
        apply_link(*current_link, key, value);
      } else {
        throw std::runtime_error("malformed links section at line " + std::to_string(line_number));
      }
      continue;
    }

    if (section == Section::Models) {
      if (indent == 2 && line.ends_with(':')) {
        std::string model_id = line.substr(0, line.size() - 1);
        ModelConfig model;
        model.id = model_id;
        auto [it, _] = config.models.emplace(model_id, std::move(model));
        current_model = &it->second;
        current_step = nullptr;
      } else if (current_model != nullptr && line == "chain:") {
        current_step = nullptr;
      } else if (current_model != nullptr && line.rfind("- ", 0) == 0) {
        current_model->chain.emplace_back();
        current_step = &current_model->chain.back();
        auto [key, value] = split_key_value(trim(line.substr(2)));
        apply_step(*current_step, key, value);
      } else if (current_step != nullptr) {
        auto [key, value] = split_key_value(line);
        apply_step(*current_step, key, value);
      } else {
        throw std::runtime_error("malformed models section at line " + std::to_string(line_number));
      }
    }
  }

  auto errors = validate_config(config);
  if (!errors.empty()) {
    std::ostringstream oss;
    oss << "invalid config " << path << ":";
    for (const auto& error : errors) {
      oss << "\n- " << error;
    }
    throw std::runtime_error(oss.str());
  }
  return config;
}

std::vector<std::string> validate_config(const TopologyConfig& config)
{
  std::vector<std::string> errors;
  if (config.runtime.queue_samples == 0) {
    errors.emplace_back("runtime.queue_samples must be greater than zero");
  }
  if (!config.runtime.batch_samples_auto && config.runtime.batch_samples == 0) {
    errors.emplace_back("runtime.batch_samples must be greater than zero or auto");
  }

  std::set<std::string> device_ids;
  if (config.devices.empty()) {
    errors.emplace_back("at least one device is required");
  }
  for (const auto& device : config.devices) {
    if (device.id.empty()) {
      errors.emplace_back("device id is required");
    }
    if (!device_ids.insert(device.id).second) {
      errors.emplace_back("duplicate device id: " + device.id);
    }
    if (device.sample_rate_hz == 0) {
      errors.emplace_back("device " + device.id + " sample_rate_hz must be greater than zero");
    }
    // The broker ring must hold a pulled batch plus a batch of serve slack;
    // queue_samples == batch_samples deadlocks the puller's room check.
    if (device.sample_rate_hz != 0 && config.runtime.queue_samples != 0) {
      const std::size_t batch = resolve_batch_samples(config.runtime, device.sample_rate_hz);
      if (config.runtime.queue_samples < 2 * batch) {
        errors.emplace_back("device " + device.id + " needs runtime.queue_samples >= 2 * batch (" +
                            std::to_string(2 * batch) + "), got " + std::to_string(config.runtime.queue_samples));
      }
    }
    if (device.tx_endpoint.empty()) {
      errors.emplace_back("device " + device.id + " tx_endpoint is required");
    }
    if (device.rx_endpoint.empty()) {
      errors.emplace_back("device " + device.id + " rx_endpoint is required");
    }
  }

  for (const auto& link : config.links) {
    const auto* source = find_device(config, link.from);
    const auto* destination = find_device(config, link.to);
    if (source == nullptr) {
      errors.emplace_back("link source device does not exist: " + link.from);
    }
    if (destination == nullptr) {
      errors.emplace_back("link destination device does not exist: " + link.to);
    }
    if (find_model(config, link.model) == nullptr) {
      errors.emplace_back("link model does not exist: " + link.model);
    }
    if (source != nullptr && destination != nullptr && source->sample_rate_hz != destination->sample_rate_hz) {
      errors.emplace_back("link " + link.from + "->" + link.to +
                          " has mixed sample rates but no resampler is implemented");
    }
  }

  // The broker relays both directions for every device: it pulls each device's
  // TX and feeds each device's RX. A device that is no link's source has its
  // pulled IQ discarded; one that is no link's destination could only be
  // zero-filled. Reject both rather than silently mishandle such a device.
  for (const auto& device : config.devices) {
    const bool is_source = std::any_of(config.links.begin(), config.links.end(),
                                       [&](const LinkConfig& link) { return link.from == device.id; });
    const bool is_destination = std::any_of(config.links.begin(), config.links.end(),
                                            [&](const LinkConfig& link) { return link.to == device.id; });
    if (!is_source) {
      errors.emplace_back("device " + device.id + " is not the source of any link");
    }
    if (!is_destination) {
      errors.emplace_back("device " + device.id + " is not the destination of any link");
    }
  }

  for (const auto& [model_id, model] : config.models) {
    if (model.chain.empty()) {
      errors.emplace_back("model " + model_id + " must have at least one step");
    }
    for (const auto& step : model.chain) {
      for (const auto& [key, _] : step.params) {
        if (!is_allowed_param(step.type, key)) {
          errors.emplace_back("model " + model_id + " step " + to_string(step.type) + " has unknown parameter: " + key);
        }
      }
      auto delay_it = step.params.find("delay_samples");
      if (delay_it != step.params.end() && delay_it->second < 0.0) {
        errors.emplace_back("model " + model_id + " delay_samples must be non-negative");
      }
      auto noise_it = step.params.find("noise_power");
      if (noise_it != step.params.end() && noise_it->second < 0.0) {
        errors.emplace_back("model " + model_id + " noise_power must be non-negative");
      }
    }
  }

  return errors;
}

std::string to_string(Backend backend)
{
  return backend == Backend::Cuda ? "cuda" : "cpu";
}

std::string to_string(DeviceRole role)
{
  return role == DeviceRole::Gnb ? "gnb" : "ue";
}

std::string to_string(ModelStepType type)
{
  switch (type) {
    case ModelStepType::Gain:
      return "gain";
    case ModelStepType::PathLoss:
      return "path_loss";
    case ModelStepType::Awgn:
      return "awgn";
    case ModelStepType::IntegerDelay:
      return "integer_delay";
    case ModelStepType::FractionalDelay:
      return "fractional_delay";
    case ModelStepType::Phase:
      return "phase";
    case ModelStepType::Cfo:
      return "cfo";
  }
  return "unknown";
}

Backend parse_backend(const std::string& value)
{
  if (value == "cpu") {
    return Backend::Cpu;
  }
  if (value == "cuda") {
    return Backend::Cuda;
  }
  throw std::runtime_error("unsupported backend: " + value);
}

DeviceRole parse_device_role(const std::string& value)
{
  if (value == "gnb") {
    return DeviceRole::Gnb;
  }
  if (value == "ue") {
    return DeviceRole::Ue;
  }
  throw std::runtime_error("unsupported device role: " + value);
}

ModelStepType parse_model_step_type(const std::string& value)
{
  if (value == "gain") {
    return ModelStepType::Gain;
  }
  if (value == "path_loss") {
    return ModelStepType::PathLoss;
  }
  if (value == "awgn") {
    return ModelStepType::Awgn;
  }
  if (value == "integer_delay") {
    return ModelStepType::IntegerDelay;
  }
  if (value == "fractional_delay") {
    return ModelStepType::FractionalDelay;
  }
  if (value == "phase") {
    return ModelStepType::Phase;
  }
  if (value == "cfo") {
    return ModelStepType::Cfo;
  }
  throw std::runtime_error("unsupported model step type: " + value);
}

std::size_t resolve_batch_samples(const RuntimeConfig& runtime, std::uint64_t sample_rate_hz)
{
  if (!runtime.batch_samples_auto) {
    return runtime.batch_samples;
  }
  return std::max<std::size_t>(1, static_cast<std::size_t>(sample_rate_hz / 1000));
}

const DeviceConfig* find_device(const TopologyConfig& config, const std::string& id)
{
  auto it = std::find_if(config.devices.begin(), config.devices.end(), [&](const DeviceConfig& d) { return d.id == id; });
  return it == config.devices.end() ? nullptr : &*it;
}

const ModelConfig* find_model(const TopologyConfig& config, const std::string& id)
{
  auto it = config.models.find(id);
  return it == config.models.end() ? nullptr : &it->second;
}

std::string link_key(const LinkConfig& link)
{
  return link.from + ">" + link.to + ":" + link.model;
}

} // namespace ocg
