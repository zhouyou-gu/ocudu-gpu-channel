#pragma once

#include <chrono>
#include <string>

// Small shared helpers for the command-line tools. Header-only so the apps can
// include it without a separate translation unit.
namespace ocg::app {

// Parses a duration string: "500ms", "60s", or a bare integer (milliseconds).
inline std::chrono::milliseconds parse_duration(const std::string& value)
{
  if (value.ends_with("ms")) {
    return std::chrono::milliseconds(std::stoll(value.substr(0, value.size() - 2)));
  }
  if (value.ends_with('s')) {
    return std::chrono::seconds(std::stoll(value.substr(0, value.size() - 1)));
  }
  return std::chrono::milliseconds(std::stoll(value));
}

} // namespace ocg::app
