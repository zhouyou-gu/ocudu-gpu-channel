#include "ocudu_gpu_channel/config.h"
#include <algorithm>
#include <cctype>
#include <cmath>
#include <fstream>
#include <map>
#include <set>
#include <sstream>
#include <stdexcept>
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

void apply_tap(TapSpec& tap, const std::string& key, const std::string& value)
{
  if (key == "delay_samples") {
    tap.delay_samples = parse_double(value, key);
  } else if (key == "gain_db") {
    tap.gain_db = parse_double(value, key);
  } else if (key == "phase_rad") {
    tap.phase_rad = parse_double(value, key);
  } else {
    throw std::runtime_error("unknown tap key: " + key);
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
  LinkConfig* current_link = nullptr;
  ModelConfig* current_model = nullptr;
  ModelStep* current_step = nullptr;
  // Set when the parser is inside a `taps:` block of `current_step`. Subsequent
  // `- ` lines at the tap indent open new taps; key-value lines at a deeper
  // indent continue the most recent tap. Reset on every new step / model.
  bool current_step_in_taps = false;
  TapSpec* current_tap = nullptr;

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
      current_step_in_taps = false;
      current_tap = nullptr;
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
      // Two structural levels live below `models:` — `chain:` lists steps, and
      // a `tdl` step may carry its own nested `taps:` list. The parser keys
      // structural decisions off the indent column so a `- delay_samples:` line
      // at the tap level is not misread as a new step at the step level.
      if (indent == 2 && line.ends_with(':')) {
        std::string model_id = line.substr(0, line.size() - 1);
        ModelConfig model;
        model.id = model_id;
        auto [it, _] = config.models.emplace(model_id, std::move(model));
        current_model = &it->second;
        current_step = nullptr;
        current_step_in_taps = false;
        current_tap = nullptr;
      } else if (current_model != nullptr && line == "chain:") {
        current_step = nullptr;
        current_step_in_taps = false;
        current_tap = nullptr;
      } else if (current_model != nullptr && indent == 6 && line.rfind("- ", 0) == 0) {
        current_model->chain.emplace_back();
        current_step = &current_model->chain.back();
        current_step_in_taps = false;
        current_tap = nullptr;
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
        current_step->taps_declared = true;
      } else if (current_step != nullptr && current_step_in_taps && indent == 10 &&
                 line.rfind("- ", 0) == 0) {
        current_step->taps.emplace_back();
        current_tap = &current_step->taps.back();
        auto [key, value] = split_key_value(trim(line.substr(2)));
        apply_tap(*current_tap, key, value);
      } else if (current_tap != nullptr && current_step_in_taps && indent == 12) {
        auto [key, value] = split_key_value(line);
        apply_tap(*current_tap, key, value);
      } else if (current_step != nullptr && indent == 8 && !current_step_in_taps) {
        auto [key, value] = split_key_value(line);
        apply_step(*current_step, key, value);
      } else {
        throw std::runtime_error("malformed models section at line " + std::to_string(line_number));
      }
    }
  }

  fold_link_leading_delays(config);

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
    if (!device.rx_model.empty() && find_model(config, device.rx_model) == nullptr) {
      errors.emplace_back("device " + device.id + " rx_model does not exist: " + device.rx_model);
    }
    if (device.tx_timing_offset_samples < 0.0) {
      errors.emplace_back("device " + device.id + " tx_timing_offset_samples must be non-negative");
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
    if (link.propagation_delay_samples < 0.0) {
      errors.emplace_back("link " + link.from + "->" + link.to +
                          " propagation_delay_samples must be non-negative");
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
        }
      } else if (!step.taps.empty() || step.taps_declared) {
        // taps_declared catches the parser-silent case where a non-tdl step
        // carries a `taps:` key with no items underneath; step.taps alone is
        // empty so the non-empty branch above would miss it.
        errors.emplace_back("model " + model_id + " step " + to_string(step.type) +
                            " has a taps block but only tdl steps may carry one");
      }
    }
  }

  return errors;
}

void fold_link_leading_delays(TopologyConfig& config)
{
  // Cache by (total_delay, base_model_id) so two links with the same composed
  // delay and same base model share one synthesized clone. The composed delay
  // is `device.tx_timing_offset_samples + link.propagation_delay_samples`; we
  // key on the sum because the leading-delay step doesn't care which knob
  // contributed which sample — both effects look identical at the receiver.
  std::map<std::pair<double, std::string>, std::string> synthesized;
  for (auto& link : config.links) {
    auto src_it = std::find_if(config.devices.begin(), config.devices.end(),
                               [&](const DeviceConfig& d) { return d.id == link.from; });
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
    } else if (!clone.chain.empty() &&
               (clone.chain.front().type == ModelStepType::IntegerDelay ||
                clone.chain.front().type == ModelStepType::FractionalDelay)) {
      // Legacy compose path -- kept until the Gain/IntegerDelay/FractionalDelay
      // enum values are deleted (commit C of the Phase 1.3 sequence). YAMLs in
      // the repo are migrated off this path by commit B.
      auto& first = clone.chain.front();
      auto existing_it = first.params.find("delay_samples");
      const double existing = existing_it == first.params.end() ? 0.0 : existing_it->second;
      const double combined = existing + total_delay;
      first.params["delay_samples"] = combined;
      const double combined_frac = combined - std::floor(combined);
      if (combined_frac > 0.0) {
        first.type = ModelStepType::FractionalDelay;
      }
    } else {
      // No leading propagation step in the source chain: prepend a single-tap
      // tdl with the composed delay and unit gain. This is the new canonical
      // form -- a leading tdl plays the role the legacy integer/fractional
      // delay step used to.
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
