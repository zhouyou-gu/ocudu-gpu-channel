#include "ocudu_gpu_channel/config.h"
#include "ocudu_gpu_channel/correlation.h"
#include <algorithm>
#include <cctype>
#include <cmath>
#include <fstream>
#include <map>
#include <set>
#include <sstream>
#include <stdexcept>
#include <tuple>
#include <utility>

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
  RadioNodes,
  Links,
  Models
};

// Which port list of the current radio node subsequent `- ` items belong to.
enum class PortList {
  None,
  Tx,
  Rx
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
  } else if (key == "rx_ring_batches") {
    runtime.rx_ring_batches = parse_size(value, key);
  } else {
    throw std::runtime_error("unknown runtime key: " + key);
  }
}

void apply_device(DeviceConfig& device, const std::string& key, const std::string& value)
{
  if (key == "id") {
    device.id = value;
  } else if (key == "role") {
    // Free-form label only; the emulator treats every node identically.
    device.role = value;
  } else if (key == "sample_rate_hz") {
    device.sample_rate_hz = parse_u64(value, key);
  } else if (key == "tx_endpoint") {
    device.tx_endpoint = value;
  } else if (key == "rx_endpoint") {
    device.rx_endpoint = value;
  } else if (key == "rx_model") {
    device.rx_model = value;
  } else if (key == "tx_timing_offset_samples") {
    device.tx_timing_offset_samples = parse_double(value, key);
  } else {
    throw std::runtime_error("unknown device key: " + key);
  }
}

void apply_mimo_coefficient(MimoCoefficient& coefficient, const std::string& key,
                            const std::string& value)
{
  if (key == "tap") {
    coefficient.tap = static_cast<int>(parse_u64(value, key));
  } else if (key == "rx") {
    coefficient.rx = static_cast<int>(parse_u64(value, key));
  } else if (key == "tx") {
    coefficient.tx = static_cast<int>(parse_u64(value, key));
  } else if (key == "real") {
    coefficient.real = parse_double(value, key);
  } else if (key == "imag") {
    coefficient.imag = parse_double(value, key);
  } else {
    throw std::runtime_error("unknown fixed_mimo coefficient key: " + key);
  }
}

void apply_radio_node(RadioNodeConfig& node, const std::string& key, const std::string& value)
{
  if (key == "id") {
    node.id = value;
  } else {
    throw std::runtime_error("unknown radio_nodes key: " + key);
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
  } else if (key == "propagation_delay_samples") {
    link.propagation_delay_samples = parse_double(value, key);
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

bool parse_bool(const std::string& value, const std::string& key)
{
  if (value == "true" || value == "True" || value == "TRUE" || value == "1") {
    return true;
  }
  if (value == "false" || value == "False" || value == "FALSE" || value == "0") {
    return false;
  }
  throw std::runtime_error("invalid boolean value for " + key + ": " + value);
}

SpatialCorrelationKind parse_spatial_correlation_kind(const std::string& value)
{
  if (value == "iid") {
    return SpatialCorrelationKind::Iid;
  }
  if (value == "kronecker") {
    return SpatialCorrelationKind::Kronecker;
  }
  if (value == "full") {
    // Named explicitly rather than folded into the generic error: `full` is a
    // planned kind (MIMO_MILESTONES M3) deferred out of this milestone, and a
    // reader who wrote it deserves to know it is deferred, not misspelled.
    throw std::runtime_error(
        "spatial_correlation kind 'full' is not implemented; use 'kronecker' or 'iid'");
  }
  throw std::runtime_error("unsupported spatial_correlation kind: " + value);
}

void apply_correlation_entry(CorrelationEntry& entry, const std::string& key,
                             const std::string& value)
{
  if (key == "i") {
    entry.i = static_cast<int>(parse_size(value, key));
  } else if (key == "j") {
    entry.j = static_cast<int>(parse_size(value, key));
  } else if (key == "re") {
    entry.re = parse_double(value, key);
  } else if (key == "im") {
    entry.im = parse_double(value, key);
  } else {
    throw std::runtime_error("unknown spatial_correlation entry key: " + key);
  }
}

void apply_los_coefficient(LosCoefficient& coefficient, const std::string& key,
                           const std::string& value)
{
  if (key == "rx") {
    coefficient.rx = static_cast<int>(parse_size(value, key));
  } else if (key == "tx") {
    coefficient.tx = static_cast<int>(parse_size(value, key));
  } else if (key == "re") {
    coefficient.re = parse_double(value, key);
  } else if (key == "im") {
    coefficient.im = parse_double(value, key);
  } else {
    throw std::runtime_error("unknown los_matrix coefficient key: " + key);
  }
}

void apply_tap(TapSpec& tap, const std::string& key, const std::string& value)
{
  if (key == "delay_samples") {
    tap.delay_samples = parse_double(value, key);
  } else if (key == "gain_db") {
    tap.gain_db = parse_double(value, key);
  } else if (key == "phase_rad") {
    tap.phase_rad = parse_double(value, key);
  } else if (key == "is_los") {
    tap.is_los = parse_bool(value, key);
  } else if (key == "los_k_db") {
    tap.los_k_db = parse_double(value, key);
  } else if (key == "los_angle_rad") {
    tap.los_angle_rad = parse_double(value, key);
  } else {
    throw std::runtime_error("unknown tap key: " + key);
  }
}

FadingSpectrum parse_fading_spectrum(const std::string& value)
{
  if (value == "jakes") {
    return FadingSpectrum::Jakes;
  }
  if (value == "gaussian") {
    return FadingSpectrum::Gaussian;
  }
  if (value == "flat") {
    return FadingSpectrum::Flat;
  }
  throw std::runtime_error("unsupported fading spectrum: " + value);
}

void apply_fading_key(ModelStep& step, const std::string& key, const std::string& value)
{
  if (key == "f_d_max_hz") {
    step.fading_f_d_max_hz = parse_double(value, key);
  } else if (key == "spectrum") {
    step.fading_spectrum = parse_fading_spectrum(value);
  } else if (key == "grid_us") {
    step.fading_grid_us = parse_double(value, key);
  } else {
    throw std::runtime_error("unknown fading key: " + key);
  }
}

bool is_allowed_param(ModelStepType type, const std::string& key)
{
  switch (type) {
    case ModelStepType::PathLoss:
      return key == "path_loss_db";
    case ModelStepType::Awgn:
      return key == "snr_db" || key == "noise_power";
    case ModelStepType::Phase:
      return key == "phase_rad";
    case ModelStepType::Cfo:
      return key == "cfo_hz" || key == "phase_rad";
    case ModelStepType::Tdl:
      // tdl carries its tap data in `taps`, not in `params`. No scalar params
      // are valid for it today; the future `fading` sub-config will arrive as
      // a per-step block (one max-Doppler frequency per link) with per-tap
      // sub-ray angle / LOS / Rician-K seeds derived from it -- see HTML
      // section 19. It is not a flat parameter at the step level.
      return false;
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
  RadioNodeConfig* current_node = nullptr;
  // Indent of the `- ` that opens a radio node. Port entries sit deeper, which
  // is how the two levels of `- ` items under `radio_nodes:` are told apart.
  int radio_node_indent = -1;
  PortList current_port_list = PortList::None;
  LinkConfig* current_link = nullptr;
  ModelConfig* current_model = nullptr;
  ModelStep* current_step = nullptr;
  // Set when the parser is inside a `taps:` block of `current_step`. Subsequent
  // `- ` lines at the tap indent open new taps; key-value lines at a deeper
  // indent continue the most recent tap. Reset on every new step / model.
  bool current_step_in_taps = false;
  TapSpec* current_tap = nullptr;
  // Set when the parser is inside a `fading:` block of `current_step`. Subsequent
  // key-value lines at indent 10 fill the step's fading sub-config fields. The
  // `fading:` block and the `taps:` block are siblings under a chain step and
  // are mutually exclusive at any one moment in the parse.
  bool current_step_in_fading = false;
  // Set inside a model's `fixed_mimo:` block, and again inside its nested
  // `coefficients:` list.
  bool current_model_in_fixed_mimo = false;
  // M3: `spatial_correlation:` and `los_matrix:` are two more indent-4 blocks
  // under a model, each with its own nested list. Tracked as separate states
  // for the same reason as the taps/fading pair: the branch that owns a line
  // is decided by which block is open at that indent, not by the key alone.
  bool current_model_in_spatial = false;
  int current_correlation_side = 0; // 0 none, 1 rx, 2 tx
  CorrelationEntry* current_correlation_entry = nullptr;
  bool current_model_in_los = false;
  bool current_model_in_los_coefficients = false;
  LosCoefficient* current_los_coefficient = nullptr;
  bool current_model_in_coefficients = false;
  MimoCoefficient* current_coefficient = nullptr;

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
      current_node = nullptr;
      radio_node_indent = -1;
      current_port_list = PortList::None;
      current_link = nullptr;
      current_model = nullptr;
      current_step = nullptr;
      current_step_in_taps = false;
      current_tap = nullptr;
      current_step_in_fading = false;
      current_model_in_fixed_mimo = false;
      current_model_in_coefficients = false;
      current_coefficient = nullptr;
      if (line == "runtime:") {
        section = Section::Runtime;
      } else if (line == "devices:") {
        section = Section::Devices;
      } else if (line == "radio_nodes:") {
        section = Section::RadioNodes;
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

    if (section == Section::RadioNodes) {
      const bool is_item = line.rfind("- ", 0) == 0;
      if (is_item && (radio_node_indent < 0 || indent <= radio_node_indent)) {
        config.radio_nodes.emplace_back();
        current_node = &config.radio_nodes.back();
        radio_node_indent = indent;
        current_port_list = PortList::None;
        auto [key, value] = split_key_value(trim(line.substr(2)));
        apply_radio_node(*current_node, key, value);
      } else if (is_item) {
        if (current_port_list == PortList::None || current_node == nullptr) {
          throw std::runtime_error("radio_nodes list item outside tx_ports/rx_ports at line " +
                                   std::to_string(line_number));
        }
        const std::string port = trim(line.substr(2));
        // A port entry is a bare Device id. A `key: value` here would mean the
        // author indented a node field into the port list by mistake.
        if (port.empty() || port.find(':') != std::string::npos) {
          throw std::runtime_error("radio_nodes port entry must be a bare device id at line " +
                                   std::to_string(line_number) + ": " + port);
        }
        (current_port_list == PortList::Tx ? current_node->tx_ports : current_node->rx_ports)
            .push_back(port);
      } else if (current_node != nullptr) {
        auto [key, value] = split_key_value(line);
        if (key == "tx_ports" || key == "rx_ports") {
          if (!value.empty()) {
            throw std::runtime_error("radio_nodes." + key +
                                     " must be a block list, not an inline value, at line " +
                                     std::to_string(line_number));
          }
          current_port_list = key == "tx_ports" ? PortList::Tx : PortList::Rx;
        } else {
          current_port_list = PortList::None;
          apply_radio_node(*current_node, key, value);
        }
      } else {
        throw std::runtime_error("malformed radio_nodes section at line " + std::to_string(line_number));
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
      // Three structural levels live below `models:` — `chain:` lists steps,
      // a `tdl` step may carry a nested `taps:` list AND a nested `fading:`
      // sub-config block. The parser keys structural decisions off the indent
      // column so a `- delay_samples:` line at the tap level is not misread
      // as a new step at the step level. `taps:` and `fading:` are sibling
      // blocks at indent 8 and are mutually exclusive at any one moment in
      // the parse.
      if (indent == 2 && line.ends_with(':')) {
        std::string model_id = line.substr(0, line.size() - 1);
        ModelConfig model;
        model.id = model_id;
        auto [it, _] = config.models.emplace(model_id, std::move(model));
        current_model = &it->second;
        current_step = nullptr;
        current_step_in_taps = false;
        current_tap = nullptr;
        current_step_in_fading = false;
        current_model_in_fixed_mimo = false;
        current_model_in_coefficients = false;
        current_coefficient = nullptr;
        current_model_in_spatial = false;
        current_correlation_side = 0;
        current_correlation_entry = nullptr;
        current_model_in_los = false;
        current_model_in_los_coefficients = false;
        current_los_coefficient = nullptr;
      } else if (current_model != nullptr && line == "chain:") {
        current_step = nullptr;
        current_step_in_taps = false;
        current_tap = nullptr;
        current_step_in_fading = false;
        current_model_in_fixed_mimo = false;
        current_model_in_coefficients = false;
        current_coefficient = nullptr;
        current_model_in_spatial = false;
        current_correlation_side = 0;
        current_correlation_entry = nullptr;
        current_model_in_los = false;
        current_model_in_los_coefficients = false;
        current_los_coefficient = nullptr;
      } else if (current_model != nullptr && indent == 4 && line == "fixed_mimo:") {
        // Sibling of `chain:`. Marking it declared here lets the validator
        // reject an empty block rather than silently treating the model as
        // scalar.
        current_model_in_fixed_mimo = true;
        current_model_in_coefficients = false;
        current_coefficient = nullptr;
        current_step = nullptr;
        current_step_in_taps = false;
        current_step_in_fading = false;
        current_model->fixed_mimo_declared = true;
        current_model_in_spatial = false;
        current_correlation_side = 0;
        current_correlation_entry = nullptr;
        current_model_in_los = false;
        current_model_in_los_coefficients = false;
        current_los_coefficient = nullptr;
      } else if (current_model_in_fixed_mimo && indent == 6 && line == "coefficients:") {
        current_model_in_coefficients = true;
        current_coefficient = nullptr;
      } else if (current_model_in_coefficients && indent == 8 && line.rfind("- ", 0) == 0) {
        current_model->fixed_mimo.emplace_back();
        current_coefficient = &current_model->fixed_mimo.back();
        auto [key, value] = split_key_value(trim(line.substr(2)));
        apply_mimo_coefficient(*current_coefficient, key, value);
      } else if (current_coefficient != nullptr && current_model_in_coefficients && indent == 10) {
        auto [key, value] = split_key_value(line);
        apply_mimo_coefficient(*current_coefficient, key, value);
      } else if (current_model != nullptr && indent == 4 && line == "spatial_correlation:") {
        // Sibling of `chain:` and `fixed_mimo:`. Declared here so the validator
        // can reject a block that says nothing rather than silently running iid.
        current_model_in_spatial = true;
        current_correlation_side = 0;
        current_correlation_entry = nullptr;
        current_model_in_los = false;
        current_model_in_los_coefficients = false;
        current_los_coefficient = nullptr;
        current_model_in_fixed_mimo = false;
        current_model_in_coefficients = false;
        current_coefficient = nullptr;
        current_step = nullptr;
        current_step_in_taps = false;
        current_step_in_fading = false;
        current_model->spatial_correlation.declared = true;
      } else if (current_model_in_spatial && indent == 6 && line == "rx:") {
        current_correlation_side = 1;
        current_correlation_entry = nullptr;
      } else if (current_model_in_spatial && indent == 6 && line == "tx:") {
        current_correlation_side = 2;
        current_correlation_entry = nullptr;
      } else if (current_model_in_spatial && indent == 6) {
        auto [key, value] = split_key_value(line);
        if (key != "kind") {
          throw std::runtime_error("unknown spatial_correlation key: " + key);
        }
        current_model->spatial_correlation.kind = parse_spatial_correlation_kind(value);
        current_correlation_side = 0;
        current_correlation_entry = nullptr;
      } else if (current_correlation_side != 0 && indent == 8 && line.rfind("- ", 0) == 0) {
        auto& side = current_correlation_side == 1 ? current_model->spatial_correlation.rx
                                                   : current_model->spatial_correlation.tx;
        side.emplace_back();
        current_correlation_entry = &side.back();
        auto [key, value] = split_key_value(trim(line.substr(2)));
        apply_correlation_entry(*current_correlation_entry, key, value);
      } else if (current_correlation_entry != nullptr && current_correlation_side != 0 &&
                 indent == 10) {
        auto [key, value] = split_key_value(line);
        apply_correlation_entry(*current_correlation_entry, key, value);
      } else if (current_model != nullptr && indent == 4 && line == "los_matrix:") {
        current_model_in_los = true;
        current_model_in_los_coefficients = false;
        current_los_coefficient = nullptr;
        current_model_in_spatial = false;
        current_correlation_side = 0;
        current_correlation_entry = nullptr;
        current_model_in_fixed_mimo = false;
        current_model_in_coefficients = false;
        current_coefficient = nullptr;
        current_step = nullptr;
        current_step_in_taps = false;
        current_step_in_fading = false;
        current_model->los_matrix.declared = true;
      } else if (current_model_in_los && indent == 6 && line == "coefficients:") {
        current_model_in_los_coefficients = true;
        current_los_coefficient = nullptr;
      } else if (current_model_in_los_coefficients && indent == 8 && line.rfind("- ", 0) == 0) {
        current_model->los_matrix.coefficients.emplace_back();
        current_los_coefficient = &current_model->los_matrix.coefficients.back();
        auto [key, value] = split_key_value(trim(line.substr(2)));
        apply_los_coefficient(*current_los_coefficient, key, value);
      } else if (current_los_coefficient != nullptr && current_model_in_los_coefficients &&
                 indent == 10) {
        auto [key, value] = split_key_value(line);
        apply_los_coefficient(*current_los_coefficient, key, value);
      } else if (current_model != nullptr && indent == 6 && line.rfind("- ", 0) == 0) {
        current_model->chain.emplace_back();
        current_step = &current_model->chain.back();
        current_step_in_taps = false;
        current_tap = nullptr;
        current_step_in_fading = false;
        auto [key, value] = split_key_value(trim(line.substr(2)));
        apply_step(*current_step, key, value);
      } else if (current_step != nullptr && indent == 8 && line == "taps:") {
        // Open the nested tap list. Subsequent `- ` lines at indent 10 push
        // new TapSpec entries onto current_step->taps. Record that a `taps:`
        // key appeared on this step so the validator can reject an empty
        // block on a non-tdl step (the parser cannot, because step.type may
        // be declared on a later line).
        current_step_in_taps = true;
        current_tap = nullptr;
        current_step_in_fading = false;
        current_step->taps_declared = true;
      } else if (current_step != nullptr && indent == 8 && line == "fading:") {
        // Open the fading sub-config block. Subsequent key-value lines at
        // indent 10 fill the step's fading_* fields. Marking
        // fading_enabled = true here lets the validator catch a fading block
        // declared on a non-tdl step even if the body is empty.
        current_step_in_fading = true;
        current_step_in_taps = false;
        current_tap = nullptr;
        current_step->fading_enabled = true;
      } else if (current_step != nullptr && current_step_in_taps && indent == 10 &&
                 line.rfind("- ", 0) == 0) {
        current_step->taps.emplace_back();
        current_tap = &current_step->taps.back();
        auto [key, value] = split_key_value(trim(line.substr(2)));
        apply_tap(*current_tap, key, value);
      } else if (current_tap != nullptr && current_step_in_taps && indent == 12) {
        auto [key, value] = split_key_value(line);
        apply_tap(*current_tap, key, value);
      } else if (current_step != nullptr && current_step_in_fading && indent == 10) {
        auto [key, value] = split_key_value(line);
        apply_fading_key(*current_step, key, value);
      } else if (current_step != nullptr && indent == 8 && !current_step_in_taps &&
                 !current_step_in_fading) {
        auto [key, value] = split_key_value(line);
        apply_step(*current_step, key, value);
      } else {
        throw std::runtime_error("malformed models section at line " + std::to_string(line_number));
      }
    }
  }

  fold_link_leading_delays(config);
  // After the delay fold, so a fixed_mimo model that also carries a composed
  // leading delay expands from the already-delayed clone.
  expand_fixed_mimo_models(config);

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
  // Two batches is the floor: one is the producer's run-ahead bound and the
  // second is the slack that lets a full batch land while the REP worker is
  // still draining the previous one. One batch of capacity would cap the
  // producer at partial pushes for the whole run.
  if (config.runtime.rx_ring_batches < 2) {
    errors.emplace_back("runtime.rx_ring_batches must be at least 2, got " +
                        std::to_string(config.runtime.rx_ring_batches));
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
    if (!device.rx_model.empty() && find_model(config, device.rx_model) == nullptr) {
      errors.emplace_back("device " + device.id + " rx_model does not exist: " + device.rx_model);
    }
    if (device.tx_timing_offset_samples < 0.0) {
      errors.emplace_back("device " + device.id + " tx_timing_offset_samples must be non-negative");
    }
  }

  // ---- radio_nodes (M1) ---------------------------------------------------
  //
  // The block is optional. When absent every Device lowers to an implicit
  // singleton node, which is the pre-M1 behaviour and needs no validation here.
  if (!config.radio_nodes.empty()) {
    std::set<std::string> node_ids;
    std::map<std::string, std::string> port_owner; // device id -> owning node id
    for (const auto& node : config.radio_nodes) {
      if (node.id.empty()) {
        errors.emplace_back("radio node id must not be empty");
        continue;
      }
      if (!node_ids.insert(node.id).second) {
        errors.emplace_back("duplicate radio node id: " + node.id);
      }
      // A shared id would make `links.from` ambiguous between a node and a
      // device, and the broker resolves link endpoints to nodes.
      if (device_ids.count(node.id) != 0) {
        errors.emplace_back("radio node id collides with a device id: " + node.id);
      }
      if (node.tx_ports.empty() && node.rx_ports.empty()) {
        errors.emplace_back("radio node " + node.id + " declares no ports");
      }

      for (const auto* list : {&node.tx_ports, &node.rx_ports}) {
        const bool is_tx = list == &node.tx_ports;
        const std::string which = is_tx ? "tx_ports" : "rx_ports";
        std::set<std::string> seen;
        for (const auto& port : *list) {
          if (device_ids.count(port) == 0) {
            errors.emplace_back("radio node " + node.id + "." + which +
                                " references an unknown device: " + port);
            continue;
          }
          // Two matrix indices for one port would make `lane = r * Nt + t`
          // ambiguous.
          if (!seen.insert(port).second) {
            errors.emplace_back("radio node " + node.id + "." + which +
                                " lists device " + port + " twice");
          }
        }
      }

      // One TX ring must not have two nodes advancing cursors into it.
      std::set<std::string> claimed;
      for (const auto* list : {&node.tx_ports, &node.rx_ports}) {
        for (const auto& port : *list) {
          claimed.insert(port);
        }
      }
      for (const auto& port : claimed) {
        auto [it, inserted] = port_owner.emplace(port, node.id);
        if (!inserted && it->second != node.id) {
          errors.emplace_back("device " + port + " is claimed by radio nodes " + it->second +
                              " and " + node.id);
        }
      }

      // A node owns one sample epoch, so its ports must agree on rate; and the
      // sibling TX-start offsets must agree or the ports' sequence indices do
      // not denote the same PHY instant (MIMO_MILESTONES.md section 1.3).
      const DeviceConfig* reference = nullptr;
      for (const auto& port : claimed) {
        const DeviceConfig* device = find_device(config, port);
        if (device == nullptr) {
          continue;
        }
        if (reference == nullptr) {
          reference = device;
          continue;
        }
        if (device->sample_rate_hz != reference->sample_rate_hz) {
          errors.emplace_back("radio node " + node.id + " ports disagree on sample_rate_hz: " +
                              reference->id + "=" + std::to_string(reference->sample_rate_hz) +
                              ", " + device->id + "=" + std::to_string(device->sample_rate_hz));
        }
        if (device->tx_timing_offset_samples != reference->tx_timing_offset_samples) {
          errors.emplace_back("radio node " + node.id +
                              " ports disagree on tx_timing_offset_samples: " + reference->id +
                              " and " + device->id);
        }
        // The receiver model is applied once per output row by a single
        // process_superposition call, so it is a node property. Ports that
        // disagree would leave which one wins up to port ordering.
        if (device->rx_model != reference->rx_model) {
          errors.emplace_back("radio node " + node.id + " ports disagree on rx_model: " +
                              reference->id + " and " + device->id);
        }
      }
    }

    // All-or-nothing: a half-declared topology would leave the rest as implicit
    // singletons whose matrix index depends on parse order, not on the author.
    for (const auto& device : config.devices) {
      if (!device.id.empty() && port_owner.count(device.id) == 0) {
        errors.emplace_back("device " + device.id +
                            " is not claimed by any radio node; declaring radio_nodes requires "
                            "declaring all of them");
      }
    }

    // With nodes declared, links join nodes, not devices.
    for (const auto& link : config.links) {
      for (const auto* endpoint : {&link.from, &link.to}) {
        if (!endpoint->empty() && node_ids.count(*endpoint) == 0) {
          errors.emplace_back("link endpoint " + *endpoint +
                              " does not name a radio node (radio_nodes is declared)");
        }
      }
    }
  }

  // Resolves a link endpoint to a Device carrying the endpoint's sample rate.
  // Without radio_nodes an endpoint IS a device; with radio_nodes it is a node,
  // and any of its ports serves as the representative because the block above
  // already rejects a node whose ports disagree on the rate.
  const auto endpoint_device = [&config](const std::string& id) -> const DeviceConfig* {
    if (config.radio_nodes.empty()) {
      return find_device(config, id);
    }
    const auto* node = find_radio_node(config, id);
    if (node == nullptr) {
      return nullptr;
    }
    for (const auto* list : {&node->tx_ports, &node->rx_ports}) {
      for (const auto& port : *list) {
        if (const auto* device = find_device(config, port)) {
          return device;
        }
      }
    }
    return nullptr;
  };
  const std::string endpoint_kind = config.radio_nodes.empty() ? "device" : "radio node";

  for (const auto& link : config.links) {
    const auto* source = endpoint_device(link.from);
    const auto* destination = endpoint_device(link.to);
    if (source == nullptr) {
      errors.emplace_back("link source " + endpoint_kind + " does not exist: " + link.from);
    }
    if (destination == nullptr) {
      errors.emplace_back("link destination " + endpoint_kind + " does not exist: " + link.to);
    }
    if (find_model(config, link.model) == nullptr) {
      errors.emplace_back("link model does not exist: " + link.model);
    }
    if (source != nullptr && destination != nullptr && source->sample_rate_hz != destination->sample_rate_hz) {
      errors.emplace_back("link " + link.from + "->" + link.to +
                          " has mixed sample rates but no resampler is implemented");
    }
    if (link.propagation_delay_samples < 0.0) {
      errors.emplace_back("link " + link.from + "->" + link.to +
                          " propagation_delay_samples must be non-negative");
    }
  }

  // The broker relays both directions for every device: it pulls each device's
  // TX and feeds each device's RX. A device that is no link's source has its
  // pulled IQ discarded; one that is no link's destination could only be
  // zero-filled. Reject both rather than silently mishandle such a device.
  //
  // Links name radio nodes once radio_nodes is declared, so the reachability
  // question is asked of the device's OWNING node in that case. Asking it of
  // the device id directly would flag every port of a perfectly well-formed
  // multi-port topology.
  const auto endpoint_for = [&config](const DeviceConfig& device) {
    for (const auto& node : config.radio_nodes) {
      for (const auto* list : {&node.tx_ports, &node.rx_ports}) {
        if (std::find(list->begin(), list->end(), device.id) != list->end()) {
          return node.id;
        }
      }
    }
    return device.id;
  };
  for (const auto& device : config.devices) {
    const std::string endpoint = endpoint_for(device);
    const bool is_source = std::any_of(config.links.begin(), config.links.end(),
                                       [&](const LinkConfig& link) { return link.from == endpoint; });
    const bool is_destination = std::any_of(config.links.begin(), config.links.end(),
                                            [&](const LinkConfig& link) { return link.to == endpoint; });
    if (!is_source) {
      errors.emplace_back("device " + device.id + " is not the source of any link");
    }
    if (!is_destination) {
      errors.emplace_back("device " + device.id + " is not the destination of any link");
    }
  }

  // ---- fixed_mimo (M1) ----------------------------------------------------
  for (const auto& [model_id, model] : config.models) {
    if (!model.fixed_mimo_declared) {
      continue;
    }
    if (model.fixed_mimo.empty()) {
      errors.emplace_back("model " + model_id + " declares fixed_mimo with no coefficients");
      continue;
    }
    std::size_t tdl_index = model.chain.size();
    for (std::size_t i = 0; i != model.chain.size(); ++i) {
      if (model.chain[i].type == ModelStepType::Tdl) {
        tdl_index = i;
        break;
      }
    }
    if (tdl_index >= model.chain.size()) {
      errors.emplace_back("model " + model_id +
                          " declares fixed_mimo but its chain has no tdl step to weight");
      continue;
    }
    const std::size_t tap_count = model.chain[tdl_index].taps.size();
    std::set<std::tuple<int, int, int>> seen;
    bool any_nonzero = false;
    for (const auto& c : model.fixed_mimo) {
      if (c.tap < 0 || static_cast<std::size_t>(c.tap) >= tap_count) {
        errors.emplace_back("model " + model_id + " fixed_mimo tap index " +
                            std::to_string(c.tap) + " is outside its " +
                            std::to_string(tap_count) + " tap(s)");
      }
      if (c.rx < 0 || c.tx < 0) {
        errors.emplace_back("model " + model_id + " fixed_mimo rx/tx indices must be non-negative");
      }
      // Last-one-wins on a duplicate would make the matrix depend on write
      // order rather than on what it says.
      if (!seen.insert({c.tap, c.rx, c.tx}).second) {
        errors.emplace_back("model " + model_id + " fixed_mimo has a duplicate entry for tap " +
                            std::to_string(c.tap) + " rx " + std::to_string(c.rx) + " tx " +
                            std::to_string(c.tx));
      }
      if (c.real != 0.0 || c.imag != 0.0) {
        any_nonzero = true;
      }
    }
    if (!any_nonzero) {
      errors.emplace_back("model " + model_id +
                          " fixed_mimo is entirely zero, so every lane using it would vanish");
    }
  }

  // A fixed_mimo coefficient must address a lane the topology actually has.
  if (!config.radio_nodes.empty()) {
    for (const auto& link : config.links) {
      const auto* model = find_model(config, link.model);
      const auto* source = find_radio_node(config, link.from);
      const auto* destination = find_radio_node(config, link.to);
      if (model == nullptr || !model->fixed_mimo_declared || source == nullptr ||
          destination == nullptr) {
        continue;
      }
      const int nt = static_cast<int>(source->tx_ports.size());
      const int nr = static_cast<int>(destination->rx_ports.size());
      for (const auto& c : model->fixed_mimo) {
        if (c.tx >= nt || c.rx >= nr) {
          errors.emplace_back("link " + link.from + "->" + link.to + " is " + std::to_string(nr) +
                              "x" + std::to_string(nt) + " but model " + link.model +
                              " addresses rx " + std::to_string(c.rx) + " tx " +
                              std::to_string(c.tx));
        }
      }
    }
  }

  // ---- spatial_correlation / los_matrix (M3) ------------------------------
  //
  // Model-scope checks first (what the block says on its own), then per-link
  // checks (whether it fits the dimensions of the links that use it), because
  // one model can be shared by links of different shapes.
  for (const auto& [model_id, model] : config.models) {
    const auto& correlation = model.spatial_correlation;
    const bool correlated = correlation.declared &&
                            correlation.kind != SpatialCorrelationKind::Iid;
    if (correlation.declared && !correlated &&
        (!correlation.rx.empty() || !correlation.tx.empty())) {
      errors.emplace_back("model " + model_id +
                          " declares spatial_correlation entries with kind: iid, which ignores them");
    }
    if (correlated && correlation.rx.empty() && correlation.tx.empty()) {
      errors.emplace_back("model " + model_id +
                          " declares spatial_correlation kind: kronecker with no rx or tx entries");
    }
    // Correlation is a property of a stochastic channel. On a chain with no
    // fading there is nothing to correlate, and a block that does nothing is
    // worse than one that is rejected.
    bool has_los_tap = false;
    for (const auto& step : model.chain) {
      if (step.type != ModelStepType::Tdl || !step.fading_enabled) {
        continue;
      }
      for (const auto& tap : step.taps) {
        if (tap.is_los) {
          has_los_tap = true;
        }
      }
    }
    // The correlated step is the chain-LEADING one: it is the propagation step,
    // the channel itself, and it is the only one the CUDA backend runs as a tdl
    // at all. Requiring it here means "the correlated step" and "step 0" are the
    // same thing by construction, so neither backend has to search for it.
    const bool leads_with_fading_tdl = !model.chain.empty() &&
                                       model.chain.front().type == ModelStepType::Tdl &&
                                       model.chain.front().fading_enabled;
    if (correlated && !leads_with_fading_tdl) {
      errors.emplace_back("model " + model_id +
                          " declares spatial_correlation but its chain does not lead with a fading tdl step");
    }
    // fixed_mimo says what H IS; spatial_correlation says the covariance of a
    // random H. Both at once states H twice, and fixed_mimo also deletes the
    // zero lanes the correlation is defined over.
    if (correlated && model.fixed_mimo_declared) {
      errors.emplace_back("model " + model_id +
                          " declares both fixed_mimo and spatial_correlation; a model states H one way");
    }
    const auto check_side = [&](const std::vector<CorrelationEntry>& entries, const char* side) {
      std::set<std::pair<int, int>> seen;
      for (const auto& e : entries) {
        if (e.i < 0 || e.j < 0) {
          errors.emplace_back("model " + model_id + " spatial_correlation " + side +
                              " indices must be non-negative");
          continue;
        }
        // Only the upper triangle is writable: the diagonal is 1 by definition
        // and the lower triangle is the conjugate mirror, so a Hermitian
        // violation cannot be expressed rather than being caught later.
        if (e.i >= e.j) {
          errors.emplace_back("model " + model_id + " spatial_correlation " + side +
                              " entry (" + std::to_string(e.i) + ", " + std::to_string(e.j) +
                              ") must have i < j; the diagonal is 1 and the lower triangle is mirrored");
        }
        if (!seen.insert({e.i, e.j}).second) {
          errors.emplace_back("model " + model_id + " spatial_correlation " + side +
                              " has a duplicate entry for (" + std::to_string(e.i) + ", " +
                              std::to_string(e.j) + ")");
        }
        if (std::sqrt(e.re * e.re + e.im * e.im) > 1.0 + 1e-12) {
          errors.emplace_back("model " + model_id + " spatial_correlation " + side +
                              " entry (" + std::to_string(e.i) + ", " + std::to_string(e.j) +
                              ") has magnitude above 1, which no unit-diagonal correlation can have");
        }
      }
    };
    if (correlation.declared) {
      check_side(correlation.rx, "rx");
      check_side(correlation.tx, "tx");
    }

    if (model.los_matrix.declared) {
      if (!has_los_tap) {
        errors.emplace_back("model " + model_id +
                            " declares los_matrix but its chain has no fading tdl tap with is_los");
      }
      std::set<std::pair<int, int>> seen;
      for (const auto& c : model.los_matrix.coefficients) {
        if (c.rx < 0 || c.tx < 0) {
          errors.emplace_back("model " + model_id + " los_matrix indices must be non-negative");
          continue;
        }
        if (!seen.insert({c.rx, c.tx}).second) {
          errors.emplace_back("model " + model_id + " los_matrix has a duplicate entry for rx " +
                              std::to_string(c.rx) + " tx " + std::to_string(c.tx));
        }
      }
    }
  }

  // Per-link: do the declared matrices fit the dimensions of the radios, and is
  // the correlation actually a valid covariance? The PSD test runs the same
  // factorisation the backends will use, so validation and use cannot disagree
  // about what is acceptable.
  if (!config.radio_nodes.empty()) {
    for (const auto& link : config.links) {
      const auto* model = find_model(config, link.model);
      const auto* source = find_radio_node(config, link.from);
      const auto* destination = find_radio_node(config, link.to);
      if (model == nullptr || source == nullptr || destination == nullptr) {
        continue;
      }
      const int nt = static_cast<int>(source->tx_ports.size());
      const int nr = static_cast<int>(destination->rx_ports.size());
      const std::string where = "link " + link.from + "->" + link.to + " (" +
                                std::to_string(nr) + "x" + std::to_string(nt) + ") model " +
                                link.model;
      const auto& correlation = model->spatial_correlation;
      if (correlation.declared && correlation.kind != SpatialCorrelationKind::Iid) {
        bool in_range = true;
        for (const auto& e : correlation.rx) {
          if (e.i >= nr || e.j >= nr) {
            errors.emplace_back(where + " rx correlation addresses index " + std::to_string(e.j) +
                                " outside its " + std::to_string(nr) + " RX port(s)");
            in_range = false;
          }
        }
        for (const auto& e : correlation.tx) {
          if (e.i >= nt || e.j >= nt) {
            errors.emplace_back(where + " tx correlation addresses index " + std::to_string(e.j) +
                                " outside its " + std::to_string(nt) + " TX port(s)");
            in_range = false;
          }
        }
        if (nt * nr > kMaxCorrelatedLanes) {
          errors.emplace_back(where + " has " + std::to_string(nt * nr) +
                              " lanes, above the " + std::to_string(kMaxCorrelatedLanes) +
                              " a correlated link supports");
          in_range = false;
        }
        if (in_range) {
          std::vector<CplxD> mixing;
          std::string error;
          if (!lane_mixing_matrix(correlation, nt, nr, mixing, error)) {
            errors.emplace_back(where + ": " + error);
          }
        }
      }
      if (model->los_matrix.declared) {
        // A declared LOS matrix must name every lane. Filling the gaps with a
        // default would make a half-written matrix mean something, silently.
        std::set<std::pair<int, int>> seen;
        bool in_range = true;
        for (const auto& c : model->los_matrix.coefficients) {
          if (c.rx >= nr || c.tx >= nt) {
            errors.emplace_back(where + " los_matrix addresses rx " + std::to_string(c.rx) +
                                " tx " + std::to_string(c.tx) + ", which it does not have");
            in_range = false;
            continue;
          }
          seen.insert({c.rx, c.tx});
        }
        if (in_range && seen.size() != static_cast<std::size_t>(nt) * static_cast<std::size_t>(nr)) {
          errors.emplace_back(where + " los_matrix declares " + std::to_string(seen.size()) +
                              " of " + std::to_string(nt * nr) +
                              " lanes; declare every lane or none");
        }
      }
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
      // tdl-specific: tap array shape and per-tap sanity. The processor will
      // collapse a single-tap tdl into the equivalent gain/delay path, but the
      // tap data itself must round-trip cleanly through the schema.
      if (step.type == ModelStepType::Tdl) {
        if (step.taps.empty()) {
          errors.emplace_back("model " + model_id + " tdl step must have at least one tap");
        }
        // Cap tap count so a YAML typo cannot launch a kernel with 10000
        // reads per output sample. 3GPP CDL profiles top out at 23 taps;
        // 64 leaves generous headroom for custom profiles and the LOS-plus-
        // scatterer-clusters compositions that show up in TR 38.901.
        constexpr std::size_t kMaxTdlTaps = 64;
        if (step.taps.size() > kMaxTdlTaps) {
          errors.emplace_back("model " + model_id + " tdl has " +
                              std::to_string(step.taps.size()) +
                              " taps; the validator caps tdl at " +
                              std::to_string(kMaxTdlTaps) + " taps per step");
        }
        // Cap per-tap delay so a YAML typo cannot demand a multi-GB delay-line
        // ring. 1e6 samples is ~43 ms at 23.04 MS/s -- far past any cellular
        // delay spread, but still bounded so a kernel sizing pass cannot blow
        // up.
        constexpr double kMaxTapDelaySamples = 1.0e6;
        std::set<double> seen_delays;
        for (const auto& tap : step.taps) {
          if (tap.delay_samples < 0.0) {
            errors.emplace_back("model " + model_id +
                                " tdl tap delay_samples must be non-negative");
          }
          if (tap.delay_samples > kMaxTapDelaySamples) {
            errors.emplace_back("model " + model_id +
                                " tdl tap delay_samples=" +
                                std::to_string(tap.delay_samples) +
                                " exceeds the validator cap of " +
                                std::to_string(kMaxTapDelaySamples) + " samples");
          }
          if (!seen_delays.insert(tap.delay_samples).second) {
            errors.emplace_back("model " + model_id +
                                " tdl has duplicate tap delay_samples=" +
                                std::to_string(tap.delay_samples) +
                                "; collapse into a single tap with summed complex gain");
          }
          if (tap.is_los && !step.fading_enabled) {
            errors.emplace_back("model " + model_id +
                                " tdl tap is_los is set but the step has no fading sub-config;"
                                " a Rician specular component needs fading enabled");
          }
          if (tap.is_los && tap.los_k_db <= 0.0) {
            // K-factor in dB: K (linear) = 10^(K_db/10). A LOS tap means the
            // specular dominates the diffuse component, so K > 1 (K_db > 0).
            // K_db = 0 (specular and Rayleigh equal-power) is a defaulted
            // value, not a physically reasonable LOS spec; TR 38.901 publishes
            // 13.3 dB for TDL-D and 22 dB for TDL-E. Reject so a forgotten
            // los_k_db field doesn't silently degrade a profile.
            errors.emplace_back("model " + model_id +
                                " tdl tap is_los requires los_k_db > 0 dB (got " +
                                std::to_string(tap.los_k_db) +
                                "); TR 38.901 publishes 13.3 dB / 22 dB for the LOS profiles");
          }
        }
        // Fading sub-config sanity: f_d_max_hz non-negative and bounded so a
        // YAML typo cannot blow up the coarse-grid sinusoid count; grid stride
        // strictly positive so the linear interpolation has a defined step.
        if (step.fading_enabled) {
          constexpr double kMaxFdMaxHz = 1.0e5;  // 100 kHz -- 10x faster than any plausible UE
          if (step.fading_f_d_max_hz < 0.0) {
            errors.emplace_back("model " + model_id +
                                " tdl fading f_d_max_hz must be non-negative");
          }
          if (step.fading_f_d_max_hz > kMaxFdMaxHz) {
            errors.emplace_back("model " + model_id +
                                " tdl fading f_d_max_hz=" +
                                std::to_string(step.fading_f_d_max_hz) +
                                " exceeds the validator cap of " +
                                std::to_string(kMaxFdMaxHz) + " Hz");
          }
          if (!(step.fading_grid_us > 0.0)) {
            errors.emplace_back("model " + model_id +
                                " tdl fading grid_us must be strictly positive");
          }
        }
      } else if (!step.taps.empty() || step.taps_declared) {
        // taps_declared catches the parser-silent case where a non-tdl step
        // carries a `taps:` key with no items underneath; step.taps alone is
        // empty so the non-empty branch above would miss it.
        errors.emplace_back("model " + model_id + " step " + to_string(step.type) +
                            " has a taps block but only tdl steps may carry one");
      }
      // fading sub-config is meaningful only on a tdl step (the LOS / Rician /
      // Doppler-spectrum semantics are all multipath-tap-level concepts).
      if (step.fading_enabled && step.type != ModelStepType::Tdl) {
        errors.emplace_back("model " + model_id + " step " + to_string(step.type) +
                            " has a fading block but only tdl steps may carry one");
      }
    }
  }

  return errors;
}

namespace {

// Index of the chain step the coefficients apply to: the first `tdl`. Returns
// chain.size() when the model has none.
std::size_t fixed_mimo_step_index(const ModelConfig& model)
{
  for (std::size_t i = 0; i != model.chain.size(); ++i) {
    if (model.chain[i].type == ModelStepType::Tdl) {
      return i;
    }
  }
  return model.chain.size();
}

// True when lane (rx, tx) has at least one non-zero coefficient. A lane whose
// coefficients are all zero carries no signal, so it is dropped from the lane
// table entirely rather than shaped and multiplied by zero -- that is exact,
// and a tap gain of exactly zero is not expressible in dB.
bool fixed_mimo_lane_is_live(const ModelConfig& model, int rx_port, int tx_port)
{
  for (const auto& c : model.fixed_mimo) {
    if (c.rx == rx_port && c.tx == tx_port && (c.real != 0.0 || c.imag != 0.0)) {
      return true;
    }
  }
  return false;
}

} // namespace

void expand_fixed_mimo_models(TopologyConfig& config)
{
  const bool any = std::any_of(config.models.begin(), config.models.end(),
                               [](const auto& entry) { return entry.second.fixed_mimo_declared; });
  if (!any) {
    return;
  }
  // resolve_topology names the clone for each lane; it does not need the clone
  // to exist. So enumerate the surviving lanes first, then materialize exactly
  // those clones -- no lane gets a model that was never built, and no clone is
  // built for a lane that was dropped as zero.
  const ResolvedTopology resolved = resolve_topology(config);
  for (const auto& lane : resolved.lanes) {
    if (config.models.count(lane.model_id) != 0) {
      continue;
    }
    const std::string& base_id = config.links[lane.link_index].model;
    auto base_it = config.models.find(base_id);
    if (base_it == config.models.end() || !base_it->second.fixed_mimo_declared) {
      continue; // validate_config reports a missing base model
    }
    const ModelConfig& base = base_it->second;
    const std::size_t step_index = fixed_mimo_step_index(base);
    if (step_index >= base.chain.size()) {
      continue; // validate_config reports fixed_mimo without a tdl step
    }

    ModelConfig clone = base;
    clone.id = lane.model_id;
    clone.fixed_mimo.clear();
    clone.fixed_mimo_declared = false;

    // Keep only the taps this lane actually carries, folding each one's
    // coefficient into the tap it already owns: a tap is a.e^(j phi), so a
    // complex weight is a gain in dB plus a phase in radians. A tap with no
    // coefficient is dropped rather than given a very negative gain, because
    // dropping it is exactly zero.
    std::vector<TapSpec> taps;
    const auto& source_taps = base.chain[step_index].taps;
    for (std::size_t k = 0; k != source_taps.size(); ++k) {
      const MimoCoefficient* found = nullptr;
      for (const auto& c : base.fixed_mimo) {
        if (c.tap == static_cast<int>(k) && c.rx == lane.rx_port && c.tx == lane.tx_port) {
          found = &c;
          break;
        }
      }
      if (found == nullptr || (found->real == 0.0 && found->imag == 0.0)) {
        continue;
      }
      TapSpec tap = source_taps[k];
      const double magnitude = std::hypot(found->real, found->imag);
      // A unit coefficient adds exactly 0 dB and 0 rad, so an identity or a
      // permutation matrix leaves the taps bit-identical.
      tap.gain_db += 20.0 * std::log10(magnitude);
      tap.phase_rad += std::atan2(found->imag, found->real);
      taps.push_back(tap);
    }
    clone.chain[step_index].taps = std::move(taps);
    clone.chain[step_index].taps_declared = true;
    config.models.emplace(clone.id, std::move(clone));
  }
}

void fold_link_leading_delays(TopologyConfig& config)
{
  // Cache by (total_delay, base_model_id) so two links with the same composed
  // delay and same base model share one synthesized clone. The composed delay
  // is `device.tx_timing_offset_samples + link.propagation_delay_samples`; we
  // key on the sum because the leading-delay step doesn't care which knob
  // contributed which sample — both effects look identical at the receiver.
  std::map<std::pair<double, std::string>, std::string> synthesized;
  // With radio_nodes declared a link names a node, not a device. Resolve to any
  // of the node's ports: the validator has already rejected a node whose ports
  // disagree on tx_timing_offset_samples, so every port carries the same value.
  // This stays correct after M1.4 expands links into lanes, because a lane's
  // source device is one of exactly those ports.
  const auto source_device_id = [&config](const std::string& from) {
    if (const auto* node = find_radio_node(config, from)) {
      for (const auto* list : {&node->tx_ports, &node->rx_ports}) {
        if (!list->empty()) {
          return list->front();
        }
      }
    }
    return from;
  };
  for (auto& link : config.links) {
    const std::string from_device = source_device_id(link.from);
    auto src_it = std::find_if(config.devices.begin(), config.devices.end(),
                               [&](const DeviceConfig& d) { return d.id == from_device; });
    const double tx_timing_offset =
        src_it == config.devices.end() ? 0.0 : src_it->tx_timing_offset_samples;
    const double propagation_delay = link.propagation_delay_samples;
    const double total_delay = tx_timing_offset + propagation_delay;
    if (total_delay == 0.0) {
      continue;
    }
    const std::string base = link.model;
    const auto cache_key = std::make_pair(total_delay, base);
    auto cached = synthesized.find(cache_key);
    if (cached != synthesized.end()) {
      link.model = cached->second;
      continue;
    }
    auto base_it = config.models.find(base);
    if (base_it == config.models.end()) {
      continue; // validate_config will report the missing base model
    }
    // Name the synthesized clone so it carries which physical effects it
    // composes. Logs and error messages will show this name, so make it
    // self-documenting rather than opaque.
    std::ostringstream id_oss;
    id_oss << "__ocg_lead_delay__" << base;
    if (tx_timing_offset != 0.0 && propagation_delay != 0.0) {
      id_oss << "__txoff_" << tx_timing_offset << "__prop_" << propagation_delay;
    } else if (tx_timing_offset != 0.0) {
      id_oss << "__txoff_" << tx_timing_offset;
    } else {
      id_oss << "__prop_" << propagation_delay;
    }
    const std::string effective_id = id_oss.str();

    ModelConfig clone = base_it->second;
    clone.id = effective_id;
    if (!clone.chain.empty() && clone.chain.front().type == ModelStepType::Tdl) {
      // Existing leading tdl: compose by shifting every tap's delay. This is
      // the physically correct merge -- a chain-leading propagation delay
      // affects all multipath taps uniformly.
      for (auto& tap : clone.chain.front().taps) {
        tap.delay_samples += total_delay;
      }
      clone.chain.front().taps_declared = true;
    } else {
      // No leading tdl in the source chain: prepend a single-tap tdl with the
      // composed delay and unit gain. tdl is now the only canonical leading-
      // propagation step (Phase 1.3 removed integer_delay and fractional_delay).
      ModelStep step;
      step.type = ModelStepType::Tdl;
      step.taps.push_back(
          TapSpec{.delay_samples = total_delay, .gain_db = 0.0, .phase_rad = 0.0});
      step.taps_declared = true;
      clone.chain.insert(clone.chain.begin(), step);
    }
    config.models.emplace(effective_id, std::move(clone));
    synthesized[cache_key] = effective_id;
    link.model = effective_id;
  }
}

std::string to_string(Backend backend)
{
  return backend == Backend::Cuda ? "cuda" : "cpu";
}

std::string to_string(ModelStepType type)
{
  switch (type) {
    case ModelStepType::PathLoss:
      return "path_loss";
    case ModelStepType::Awgn:
      return "awgn";
    case ModelStepType::Phase:
      return "phase";
    case ModelStepType::Cfo:
      return "cfo";
    case ModelStepType::Tdl:
      return "tdl";
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

ModelStepType parse_model_step_type(const std::string& value)
{
  if (value == "path_loss") {
    return ModelStepType::PathLoss;
  }
  if (value == "awgn") {
    return ModelStepType::Awgn;
  }
  if (value == "phase") {
    return ModelStepType::Phase;
  }
  if (value == "cfo") {
    return ModelStepType::Cfo;
  }
  if (value == "tdl") {
    return ModelStepType::Tdl;
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

std::string lane_key(const std::string& base_link_key, int rx_port, int tx_port, int nt, int nr)
{
  if (nt == 1 && nr == 1) {
    return base_link_key;
  }
  return base_link_key + "#r" + std::to_string(rx_port) + "t" + std::to_string(tx_port);
}

std::string rx_state_key(const std::string& node_id, int rx_port, int nr)
{
  if (nr == 1) {
    return node_id + ">rx";
  }
  return node_id + ">rx#r" + std::to_string(rx_port);
}

namespace {

// One round of the splitmix64 finalizer: a full-avalanche 64-bit mix, so two
// seeds that differ in one bit share no structure.
std::uint64_t seed_mix(std::uint64_t x)
{
  x += 0x9e3779b97f4a7c15ULL;
  x = (x ^ (x >> 30)) * 0xbf58476d1ce4e5b9ULL;
  x = (x ^ (x >> 27)) * 0x94d049bb133111ebULL;
  return x ^ (x >> 31);
}

} // namespace

std::uint64_t physical_link_seed(const std::string& base_link_key)
{
  // FNV-1a over the identity, then avalanche. Written out rather than taken
  // from std::hash so the value is fixed by this source file and not by the
  // standard library the binary happened to be built against.
  std::uint64_t h = 1469598103934665603ULL;
  for (const unsigned char c : base_link_key) {
    h ^= static_cast<std::uint64_t>(c);
    h *= 1099511628211ULL;
  }
  return seed_mix(h);
}

std::uint64_t lane_fading_seed(std::uint64_t link_seed, int rx_port, int tx_port,
                               int step_index)
{
  // Integer mixing only: no string is built here, so no key-format change can
  // reach a realisation. The three odd multipliers keep (r, t, step) from
  // aliasing onto each other -- (1, 0, 0) and (0, 1, 0) must not collide.
  std::uint64_t v = link_seed;
  v = seed_mix(v ^ (0x9e3779b97f4a7c15ULL *
                    (static_cast<std::uint64_t>(rx_port) + 1ULL)));
  v = seed_mix(v ^ (0xc2b2ae3d27d4eb4fULL *
                    (static_cast<std::uint64_t>(tx_port) + 1ULL)));
  v = seed_mix(v ^ (0x165667b19e3779f9ULL *
                    (static_cast<std::uint64_t>(step_index) + 1ULL)));
  return v;
}

std::string fixed_mimo_model_id(const std::string& base_model_id, int rx_port, int tx_port)
{
  return "__ocg_mimo__" + base_model_id + "__r" + std::to_string(rx_port) + "t" +
         std::to_string(tx_port);
}

ResolvedTopology resolve_topology(const TopologyConfig& config)
{
  ResolvedTopology resolved;

  if (config.radio_nodes.empty()) {
    // Implicit lowering: one singleton node per Device, node id == device id.
    resolved.nodes.reserve(config.devices.size());
    for (const auto& device : config.devices) {
      ResolvedNode node;
      node.id = device.id;
      node.tx_ports.push_back(device.id);
      node.rx_ports.push_back(device.id);
      node.sample_rate_hz = device.sample_rate_hz;
      node.rx_model = device.rx_model;
      node.implicit = true;
      resolved.nodes.push_back(std::move(node));
    }
  } else {
    resolved.nodes.reserve(config.radio_nodes.size());
    for (const auto& declared : config.radio_nodes) {
      ResolvedNode node;
      node.id = declared.id;
      node.tx_ports = declared.tx_ports;
      node.rx_ports = declared.rx_ports;
      node.implicit = false;
      // Any port is representative: validate_config rejects a node whose ports
      // disagree on sample rate or rx_model.
      const std::string* representative =
          !declared.tx_ports.empty() ? &declared.tx_ports.front()
          : !declared.rx_ports.empty() ? &declared.rx_ports.front()
                                       : nullptr;
      if (representative != nullptr) {
        if (const auto* device = find_device(config, *representative)) {
          node.sample_rate_hz = device->sample_rate_hz;
          node.rx_model = device->rx_model;
        }
      }
      resolved.nodes.push_back(std::move(node));
    }
  }

  std::map<std::string, const ResolvedNode*> by_id;
  for (const auto& node : resolved.nodes) {
    by_id.emplace(node.id, &node);
  }

  // Expand every link into its Nt x Nr lanes, emitting in lane = r * Nt + t
  // order so a row's lanes are already adjacent before the sort below.
  for (std::size_t i = 0; i != config.links.size(); ++i) {
    const auto& link = config.links[i];
    auto src_it = by_id.find(link.from);
    auto dst_it = by_id.find(link.to);
    if (src_it == by_id.end() || dst_it == by_id.end()) {
      throw std::runtime_error("link endpoint resolves to no radio node: " + link_key(link));
    }
    const ResolvedNode& src = *src_it->second;
    const ResolvedNode& dst = *dst_it->second;
    const int nt = static_cast<int>(src.tx_ports.size());
    const int nr = static_cast<int>(dst.rx_ports.size());
    if (nt == 0 || nr == 0) {
      throw std::runtime_error("link " + link_key(link) +
                               " needs a TX port on its source and an RX port on its destination");
    }
    const auto* base_model = find_model(config, link.model);
    const bool has_fixed_mimo = base_model != nullptr && base_model->fixed_mimo_declared;
    const std::string base = link_key(link);
    for (int r = 0; r != nr; ++r) {
      for (int t = 0; t != nt; ++t) {
        // A zero lane of a fixed matrix is simply absent: it contributes
        // nothing to its row, and omitting it is exactly zero where a folded
        // gain could only approach it.
        if (has_fixed_mimo && !fixed_mimo_lane_is_live(*base_model, r, t)) {
          continue;
        }
        LaneConfig lane;
        lane.key = lane_key(base, r, t, nt, nr);
        lane.src_node = src.id;
        lane.dst_node = dst.id;
        lane.src_device = src.tx_ports[static_cast<std::size_t>(t)];
        lane.dst_device = dst.rx_ports[static_cast<std::size_t>(r)];
        lane.tx_port = t;
        lane.rx_port = r;
        lane.nt = nt;
        lane.nr = nr;
        lane.model_id = has_fixed_mimo ? fixed_mimo_model_id(link.model, r, t) : link.model;
        lane.link_index = i;
        // The physical link, not the lane: `base` carries the author's model
        // id even when the lane runs a synthesized fixed_mimo clone, so the
        // link's stochastic identity survives a matrix edit.
        lane.physical_link_key = base;
        resolved.lanes.push_back(std::move(lane));
      }
    }
  }

  // Group a destination node's lanes by row. Stable, so within a row the lanes
  // keep link order (and within a link, t order) -- a fixed summation order.
  std::stable_sort(resolved.lanes.begin(), resolved.lanes.end(),
                   [](const LaneConfig& a, const LaneConfig& b) {
                     if (a.dst_node != b.dst_node) {
                       return a.dst_node < b.dst_node;
                     }
                     return a.rx_port < b.rx_port;
                   });

  // Per-link grouping, built AFTER the sort so the recorded positions are the
  // final ones. Row-major ordering leaves a link's lanes scattered through the
  // array whenever a node has more than one incoming link, and M3's cross-lane
  // mixing needs them together -- deriving this in each backend instead is the
  // silent-drift failure ResolvedTopology exists to prevent.
  std::map<std::string, std::size_t> group_of;
  for (std::size_t position = 0; position != resolved.lanes.size(); ++position) {
    const LaneConfig& lane = resolved.lanes[position];
    auto [it, inserted] = group_of.try_emplace(lane.physical_link_key, resolved.link_groups.size());
    if (inserted) {
      LinkLaneGroup group;
      group.physical_link_key = lane.physical_link_key;
      group.nt = lane.nt;
      group.nr = lane.nr;
      group.lane_index.assign(static_cast<std::size_t>(lane.nt) * lane.nr, -1);
      resolved.link_groups.push_back(std::move(group));
    }
    LinkLaneGroup& group = resolved.link_groups[it->second];
    group.lane_index[static_cast<std::size_t>(lane.rx_port) * lane.nt + lane.tx_port] =
        static_cast<int>(position);
  }
  return resolved;
}

const RadioNodeConfig* find_radio_node(const TopologyConfig& config, const std::string& id)
{
  auto it = std::find_if(config.radio_nodes.begin(), config.radio_nodes.end(),
                         [&](const RadioNodeConfig& n) { return n.id == id; });
  return it == config.radio_nodes.end() ? nullptr : &*it;
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
