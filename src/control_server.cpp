#include "ocudu_gpu_channel/control_server.h"

#include <zmq.h>

#include <cmath>
#include <cstring>
#include <iostream>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace ocg {

namespace {

// ────────────────────────────────────────────────────────────────────────
// Minimal JSON parser/encoder. v1 needed flat objects with string/number/
// bool values; v2 extended with nested objects + arrays for the
// profile_swap message shape (taps array + fading sub-config). Escapes
// supported: \", \\, \n. Unicode escapes unsupported — adequate for the
// control message shapes we own; if v3 wants richer payloads, swap to a
// real library.
// ────────────────────────────────────────────────────────────────────────

struct JsonValue {
  enum class Kind { String, Number, Bool, Object, Array };
  Kind kind = Kind::String;
  std::string s;
  double n = 0.0;
  bool b = false;
  std::unordered_map<std::string, JsonValue> obj;  // when kind == Object
  std::vector<JsonValue> arr;                       // when kind == Array
};

class JsonParseError : public std::runtime_error {
public:
  using std::runtime_error::runtime_error;
};

class JsonReader {
public:
  explicit JsonReader(std::string_view src) : src_(src) {}

  std::unordered_map<std::string, JsonValue> read_flat_object()
  {
    skip_ws();
    expect('{');
    std::unordered_map<std::string, JsonValue> out;
    skip_ws();
    if (peek() == '}') { ++i_; return out; }
    while (true) {
      skip_ws();
      const std::string key = read_string();
      skip_ws();
      expect(':');
      skip_ws();
      out.emplace(key, read_value());
      skip_ws();
      const char c = take();
      if (c == ',') continue;
      if (c == '}') return out;
      throw JsonParseError("expected ',' or '}' between fields");
    }
  }

private:
  std::string_view src_;
  std::size_t i_ = 0;

  char peek() const
  {
    if (i_ >= src_.size()) throw JsonParseError("unexpected end of input");
    return src_[i_];
  }

  char take()
  {
    if (i_ >= src_.size()) throw JsonParseError("unexpected end of input");
    return src_[i_++];
  }

  void expect(char c)
  {
    if (take() != c) {
      throw JsonParseError(std::string("expected '") + c + "'");
    }
  }

  void skip_ws()
  {
    while (i_ < src_.size()) {
      const char c = src_[i_];
      if (c == ' ' || c == '\t' || c == '\n' || c == '\r') ++i_;
      else break;
    }
  }

  std::string read_string()
  {
    expect('"');
    std::string out;
    while (true) {
      const char c = take();
      if (c == '"') return out;
      if (c == '\\') {
        const char e = take();
        if (e == '"') out.push_back('"');
        else if (e == '\\') out.push_back('\\');
        else if (e == 'n') out.push_back('\n');
        else throw JsonParseError("unsupported escape");
      } else {
        out.push_back(c);
      }
    }
  }

  JsonValue read_value()
  {
    skip_ws();
    JsonValue v;
    if (peek() == '"') {
      v.kind = JsonValue::Kind::String;
      v.s = read_string();
      return v;
    }
    if (peek() == '{') {
      v.kind = JsonValue::Kind::Object;
      v.obj = read_flat_object();   // recursive — nested objects allowed
      return v;
    }
    if (peek() == '[') {
      v.kind = JsonValue::Kind::Array;
      ++i_;                          // consume '['
      skip_ws();
      if (peek() == ']') { ++i_; return v; }
      while (true) {
        skip_ws();
        v.arr.push_back(read_value());
        skip_ws();
        const char c = take();
        if (c == ',') continue;
        if (c == ']') return v;
        throw JsonParseError("expected ',' or ']' in array");
      }
    }
    // bool
    if (src_.compare(i_, 4, "true") == 0) {
      i_ += 4; v.kind = JsonValue::Kind::Bool; v.b = true; return v;
    }
    if (src_.compare(i_, 5, "false") == 0) {
      i_ += 5; v.kind = JsonValue::Kind::Bool; v.b = false; return v;
    }
    // number
    v.kind = JsonValue::Kind::Number;
    const std::size_t start = i_;
    if (peek() == '-' || peek() == '+') ++i_;
    while (i_ < src_.size()) {
      const char c = src_[i_];
      const bool is_num = (c >= '0' && c <= '9') || c == '.' || c == 'e' || c == 'E' || c == '+' || c == '-';
      if (!is_num) break;
      ++i_;
    }
    if (i_ == start) throw JsonParseError("expected value");
    try {
      v.n = std::stod(std::string(src_.substr(start, i_ - start)));
    } catch (...) {
      throw JsonParseError("invalid number");
    }
    return v;
  }
};

std::string json_escape(const std::string& s)
{
  std::string out;
  out.reserve(s.size() + 2);
  for (char c : s) {
    if (c == '"' || c == '\\') { out.push_back('\\'); out.push_back(c); }
    else if (c == '\n') { out += "\\n"; }
    else out.push_back(c);
  }
  return out;
}

std::string format_double(double v)
{
  std::ostringstream o;
  o.precision(15);
  o << v;
  return o.str();
}

// ────────────────────────────────────────────────────────────────────────
// Param whitelist + range validation (decision #2 in the plan).
// ────────────────────────────────────────────────────────────────────────

struct ParamSpec {
  const char* name;
  double min;
  double max;
  bool is_int;
};

constexpr ParamSpec kParamSpecs[] = {
  // name                       min          max          is_int
  {"path_loss_db",              -200.0,       50.0,       false},
  {"awgn_snr_db",                 -30.0,      120.0,       false},
  {"los_k_db",                   -30.0,       40.0,       false},
  {"cfo_hz",                  -50000.0,    50000.0,       false},
  {"tap0_delay_samples",           0.0,     1023.0,       false},
  {"tap0_gain_db",              -100.0,       20.0,       false},
  {"tap0_phase_rad",             -3.1415926535897932,
                                   3.1415926535897932,    false},
};

const ParamSpec* find_param_spec(const std::string& name)
{
  for (const auto& spec : kParamSpecs) {
    if (name == spec.name) return &spec;
  }
  return nullptr;
}

// Read the current shadow value for a param (used for the old= field in
// the event=control_update log line). Returns NaN for an unknown param.
double read_shadow(const BrokerLinkControl& ctl, const ParamSpec& spec)
{
  if (spec.name == std::string_view("path_loss_db"))       return ctl.shadow.path_loss_db;
  if (spec.name == std::string_view("awgn_snr_db"))         return ctl.shadow.awgn_snr_db;
  if (spec.name == std::string_view("los_k_db"))           return ctl.shadow.los_k_db;
  if (spec.name == std::string_view("cfo_hz"))             return ctl.shadow.cfo_hz;
  if (spec.name == std::string_view("tap0_delay_samples")) return ctl.shadow.tap0_delay_samples;
  if (spec.name == std::string_view("tap0_gain_db"))       return ctl.shadow.tap0_gain_db;
  if (spec.name == std::string_view("tap0_phase_rad"))     return ctl.shadow.tap0_phase_rad;
  return std::nan("");
}

// Apply a validated update to the shadow (MutableParams field selected by
// name) and bump the seqno with release semantics. Returns true on success.
bool apply_update(BrokerLinkControl& ctl, const ParamSpec& spec, double value)
{
  if (spec.name == std::string_view("path_loss_db"))       ctl.shadow.path_loss_db = static_cast<float>(value);
  else if (spec.name == std::string_view("awgn_snr_db"))    ctl.shadow.awgn_snr_db  = static_cast<float>(value);
  else if (spec.name == std::string_view("los_k_db"))      ctl.shadow.los_k_db     = static_cast<float>(value);
  else if (spec.name == std::string_view("cfo_hz"))        ctl.shadow.cfo_hz       = static_cast<float>(value);
  else if (spec.name == std::string_view("tap0_delay_samples")) ctl.shadow.tap0_delay_samples = static_cast<float>(value);
  else if (spec.name == std::string_view("tap0_gain_db"))  ctl.shadow.tap0_gain_db = static_cast<float>(value);
  else if (spec.name == std::string_view("tap0_phase_rad"))ctl.shadow.tap0_phase_rad = static_cast<float>(value);
  else return false;

  // Release-store pairs with the server thread's acquire-load in
  // snap_mutable_params(). The shadow write above must be visible whenever
  // the bumped seqno is observed.
  ctl.seqno.fetch_add(1, std::memory_order_release);
  return true;
}

// Default log sink: one line to std::cout per applied update or rejection.
// Matches the existing broker convention (k=v on a single line), not the
// JSON shape the plan originally sketched — the plan was updated to follow
// the codebase. Strings with whitespace are quoted to keep the line
// awk-parseable.
void default_logger(std::string_view line)
{
  std::cout << line << '\n';
}

std::string make_error_reply(const std::string& msg)
{
  return std::string("{\"ok\":false,\"error\":\"") + json_escape(msg) + "\"}";
}

std::string make_success_reply(std::uint32_t seqno)
{
  return std::string("{\"ok\":true,\"seqno\":") + std::to_string(seqno) + "}";
}

}  // namespace

// ────────────────────────────────────────────────────────────────────────
// ControlServer
// ────────────────────────────────────────────────────────────────────────

ControlServer::ControlServer(ControlServerConfig config, LinkMap link_map)
    : config_(std::move(config)), link_map_(std::move(link_map))
{
  if (!config_.logger) {
    config_.logger = default_logger;
  }
}

ControlServer::~ControlServer()
{
  stop();
}

void ControlServer::start()
{
  if (running_.exchange(true)) return;
  stop_requested_.store(false, std::memory_order_relaxed);
  thread_ = std::thread([this] { run_loop(); });
}

void ControlServer::stop()
{
  if (!running_.exchange(false)) return;
  stop_requested_.store(true, std::memory_order_relaxed);
  if (thread_.joinable()) thread_.join();
}

ControlServer::Stats ControlServer::stats() const
{
  Stats s;
  s.msgs_received    = msgs_received_.load(std::memory_order_relaxed);
  s.updates_applied  = updates_applied_.load(std::memory_order_relaxed);
  s.updates_rejected = updates_rejected_.load(std::memory_order_relaxed);
  return s;
}

// ────────────────────────────────────────────────────────────────────────
// v2.0-F2 helpers — message-type dispatch + profile_swap parsing.
// Eligibility checks (force flag vs. has-leading-tdl on the chain) move
// to F3's snap path, where the backend has the chain shape on hand.
// ────────────────────────────────────────────────────────────────────────

namespace {

const char* json_kind_name(JsonValue::Kind k)
{
  switch (k) {
    case JsonValue::Kind::String: return "string";
    case JsonValue::Kind::Number: return "number";
    case JsonValue::Kind::Bool:   return "bool";
    case JsonValue::Kind::Object: return "object";
    case JsonValue::Kind::Array:  return "array";
  }
  return "?";
}

// Extract a numeric field from a JSON object with a default. Returns the
// fallback when the key is absent. Throws if present with the wrong type.
double get_number(const std::unordered_map<std::string, JsonValue>& o,
                  const char* key, double fallback)
{
  auto it = o.find(key);
  if (it == o.end()) return fallback;
  if (it->second.kind != JsonValue::Kind::Number) {
    throw JsonParseError(std::string("field '") + key + "' must be a number, got "
                         + json_kind_name(it->second.kind));
  }
  return it->second.n;
}

bool get_bool(const std::unordered_map<std::string, JsonValue>& o,
              const char* key, bool fallback)
{
  auto it = o.find(key);
  if (it == o.end()) return fallback;
  if (it->second.kind != JsonValue::Kind::Bool) {
    throw JsonParseError(std::string("field '") + key + "' must be a bool, got "
                         + json_kind_name(it->second.kind));
  }
  return it->second.b;
}

std::string get_string(const std::unordered_map<std::string, JsonValue>& o,
                       const char* key, const std::string& fallback)
{
  auto it = o.find(key);
  if (it == o.end()) return fallback;
  if (it->second.kind != JsonValue::Kind::String) {
    throw JsonParseError(std::string("field '") + key + "' must be a string");
  }
  return it->second.s;
}

// Per-message context: bundles the ControlServer state the helpers need
// so they can live as free functions in the anonymous namespace (avoiding
// header changes / JsonValue leak).
struct HandlerContext {
  const ControlServer::LinkMap& link_map;
  std::atomic<std::uint64_t>&   updates_applied;
  std::atomic<std::uint64_t>&   updates_rejected;
  const std::function<void(std::string_view)>& logger;
};

std::string emit_rejection(HandlerContext& ctx, const std::string& reason)
{
  ctx.updates_rejected.fetch_add(1, std::memory_order_relaxed);
  if (ctx.logger) {
    ctx.logger(std::string("event=control_error reason=\"") + reason + "\"");
  }
  return make_error_reply(reason);
}

std::string handle_scalar_update(
    HandlerContext& ctx,
    const std::unordered_map<std::string, JsonValue>& fields)
{
  auto it_link  = fields.find("link_id");
  auto it_param = fields.find("param");
  auto it_value = fields.find("value");
  if (it_link == fields.end() || it_param == fields.end() || it_value == fields.end()) {
    return emit_rejection(ctx, "missing required field (need link_id, param, value)");
  }
  if (it_link->second.kind != JsonValue::Kind::String ||
      it_param->second.kind != JsonValue::Kind::String ||
      it_value->second.kind != JsonValue::Kind::Number) {
    return emit_rejection(ctx, "type error: link_id/param must be strings, value must be a number");
  }

  const std::string& link_id = it_link->second.s;
  const std::string& param   = it_param->second.s;
  const double value         = it_value->second.n;

  auto it_ctl = ctx.link_map.find(link_id);
  if (it_ctl == ctx.link_map.end() || it_ctl->second == nullptr) {
    return emit_rejection(ctx, "unknown link_id: " + link_id);
  }

  const ParamSpec* spec = find_param_spec(param);
  if (spec == nullptr) {
    return emit_rejection(ctx, "unknown param: " + param);
  }
  if (!std::isfinite(value) || value < spec->min || value > spec->max) {
    return emit_rejection(ctx,
        "param '" + param + "' value " + format_double(value) + " out of range ["
        + format_double(spec->min) + ", " + format_double(spec->max) + "]");
  }
  if (spec->is_int && std::trunc(value) != value) {
    return emit_rejection(ctx, "param '" + param + "' requires an integer value");
  }

  const double old_value = read_shadow(*it_ctl->second, *spec);
  if (!apply_update(*it_ctl->second, *spec, value)) {
    return emit_rejection(ctx, "internal: failed to apply update");
  }

  ctx.updates_applied.fetch_add(1, std::memory_order_relaxed);
  const std::uint32_t observed_seqno =
      it_ctl->second->seqno.load(std::memory_order_relaxed);

  if (ctx.logger) {
    std::ostringstream o;
    o << "event=control_update link_id=" << link_id
      << " param=" << param
      << " old=" << format_double(old_value)
      << " new=" << format_double(value)
      << " seqno=" << observed_seqno;
    ctx.logger(o.str());
  }
  return make_success_reply(observed_seqno);
}

std::string handle_profile_swap(
    HandlerContext& ctx,
    const std::unordered_map<std::string, JsonValue>& fields)
{
  auto it_link = fields.find("link_id");
  if (it_link == fields.end() || it_link->second.kind != JsonValue::Kind::String) {
    return emit_rejection(ctx, "profile_swap: missing or non-string link_id");
  }
  const std::string& link_id = it_link->second.s;
  auto it_ctl = ctx.link_map.find(link_id);
  if (it_ctl == ctx.link_map.end() || it_ctl->second == nullptr) {
    return emit_rejection(ctx, "unknown link_id: " + link_id);
  }

  auto it_taps = fields.find("taps");
  if (it_taps == fields.end() || it_taps->second.kind != JsonValue::Kind::Array) {
    return emit_rejection(ctx, "profile_swap: missing or non-array 'taps'");
  }
  const auto& taps_arr = it_taps->second.arr;
  if (taps_arr.empty()) {
    return emit_rejection(ctx, "profile_swap: taps array must not be empty");
  }
  if (taps_arr.size() > static_cast<std::size_t>(kDeviceMaxTaps)) {
    return emit_rejection(ctx, "profile_swap: taps array exceeds kDeviceMaxTaps (" +
                                std::to_string(kDeviceMaxTaps) + ")");
  }

  // Stage into a local ProfileShadow; only commit to ctl on full validation.
  ProfileShadow staged{};
  staged.n_taps = static_cast<int>(taps_arr.size());
  for (std::size_t k = 0; k < taps_arr.size(); ++k) {
    const auto& tap_val = taps_arr[k];
    if (tap_val.kind != JsonValue::Kind::Object) {
      return emit_rejection(ctx, "profile_swap: taps[" + std::to_string(k) + "] is not an object");
    }
    const auto& t = tap_val.obj;
    try {
      staged.taps[k].delay_samples = get_number(t, "delay_samples", 0.0);
      staged.taps[k].gain_db       = get_number(t, "gain_db", 0.0);
      staged.taps[k].phase_rad     = get_number(t, "phase_rad", 0.0);
      staged.taps[k].is_los        = get_bool  (t, "is_los", false);
      staged.taps[k].los_k_db      = get_number(t, "los_k_db", 0.0);
      staged.taps[k].los_angle_rad = get_number(t, "los_angle_rad", 0.0);
    } catch (const std::exception& e) {
      return emit_rejection(ctx, std::string("profile_swap: taps[") + std::to_string(k) + "]: " + e.what());
    }
    if (staged.taps[k].delay_samples < 0.0 || staged.taps[k].delay_samples > 1023.0) {
      return emit_rejection(ctx, "profile_swap: taps[" + std::to_string(k) +
                                  "].delay_samples out of range [0, 1023]");
    }
    if (staged.taps[k].gain_db < -100.0 || staged.taps[k].gain_db > 20.0) {
      return emit_rejection(ctx, "profile_swap: taps[" + std::to_string(k) +
                                  "].gain_db out of range [-100, 20]");
    }
  }

  // Optional fading sub-config.
  auto it_fading = fields.find("fading");
  if (it_fading != fields.end()) {
    if (it_fading->second.kind != JsonValue::Kind::Object) {
      return emit_rejection(ctx, "profile_swap: 'fading' must be an object when present");
    }
    const auto& f = it_fading->second.obj;
    try {
      staged.fading_enabled    = get_bool  (f, "enabled", false);
      staged.fading_f_d_max_hz = static_cast<float>(get_number(f, "f_d_max_hz", 0.0));
      staged.fading_grid_us    = static_cast<float>(get_number(f, "grid_us", 100.0));
      const std::string spec   = get_string(f, "spectrum", "jakes");
      if (spec != "jakes") {
        return emit_rejection(ctx, "profile_swap: only fading.spectrum='jakes' is supported in v2");
      }
      staged.fading_spectrum = 0;
    } catch (const std::exception& e) {
      return emit_rejection(ctx, std::string("profile_swap.fading: ") + e.what());
    }
  }

  // Optional take_effect_at_slot. v2.1 will honour this; F2 just stores it.
  double tea = 0.0;
  try {
    tea = get_number(fields, "take_effect_at_slot", 0.0);
  } catch (const std::exception& e) {
    return emit_rejection(ctx, e.what());
  }
  if (tea < 0.0) {
    return emit_rejection(ctx, "profile_swap: take_effect_at_slot must be >= 0");
  }

  // Optional force flag. F3 consumes it at snap time to override the
  // chain-eligibility check (default reject for non-tdl-leading chains).
  try {
    staged.force = get_bool(fields, "force", false);
  } catch (const std::exception& e) {
    return emit_rejection(ctx, e.what());
  }

  // Commit to the shadow. Single writer → no lock; seqno bump publishes
  // the writes with release semantics, paired with the snap's acquire.
  BrokerLinkControl& ctl = *it_ctl->second;
  ctl.shadow_profile      = staged;
  ctl.profile_pending     = true;
  ctl.take_effect_at_slot = static_cast<std::uint64_t>(tea);
  ctl.seqno.fetch_add(1, std::memory_order_release);

  ctx.updates_applied.fetch_add(1, std::memory_order_relaxed);
  const std::uint32_t observed_seqno = ctl.seqno.load(std::memory_order_relaxed);

  if (ctx.logger) {
    std::ostringstream o;
    o << "event=control_update link_id=" << link_id
      << " param=profile_swap"
      << " n_taps=" << staged.n_taps
      << " fading=" << (staged.fading_enabled ? "1" : "0")
      << " seqno=" << observed_seqno;
    ctx.logger(o.str());
  }
  return make_success_reply(observed_seqno);
}

}  // namespace

std::string ControlServer::handle_message(const std::string& request_body)
{
  msgs_received_.fetch_add(1, std::memory_order_relaxed);

  HandlerContext ctx{link_map_, updates_applied_, updates_rejected_, config_.logger};

  std::unordered_map<std::string, JsonValue> fields;
  try {
    JsonReader r(request_body);
    fields = r.read_flat_object();
  } catch (const std::exception& e) {
    return emit_rejection(ctx, std::string("malformed JSON: ") + e.what());
  }

  // v2 dispatch on message type. Default "scalar" preserves v1 wire
  // compatibility: v1 clients omit the field and hit the scalar path
  // unchanged.
  std::string type_str;
  try {
    type_str = get_string(fields, "type", "scalar");
  } catch (const std::exception& e) {
    return emit_rejection(ctx, e.what());
  }

  if (type_str == "scalar") {
    return handle_scalar_update(ctx, fields);
  }
  if (type_str == "profile_swap") {
    return handle_profile_swap(ctx, fields);
  }
  return emit_rejection(ctx, std::string("unknown message type: ") + type_str);
}


void ControlServer::run_loop()
{
  void* ctx = zmq_ctx_new();
  if (ctx == nullptr) return;
  void* sock = zmq_socket(ctx, ZMQ_REP);
  if (sock == nullptr) { zmq_ctx_term(ctx); return; }
  const int timeout = config_.recv_timeout_ms;
  zmq_setsockopt(sock, ZMQ_RCVTIMEO, &timeout, sizeof(timeout));
  if (zmq_bind(sock, config_.endpoint.c_str()) != 0) {
    zmq_close(sock);
    zmq_ctx_term(ctx);
    return;
  }

  while (!stop_requested_.load(std::memory_order_relaxed)) {
    zmq_msg_t req_msg;
    zmq_msg_init(&req_msg);
    const int rc = zmq_msg_recv(&req_msg, sock, 0);
    if (rc < 0) {
      zmq_msg_close(&req_msg);
      // EAGAIN from the timeout is the normal loop-around path.
      if (zmq_errno() == EAGAIN) continue;
      break;
    }
    const std::string body(static_cast<const char*>(zmq_msg_data(&req_msg)),
                           static_cast<std::size_t>(zmq_msg_size(&req_msg)));
    zmq_msg_close(&req_msg);

    const std::string reply = handle_message(body);
    zmq_send(sock, reply.data(), reply.size(), 0);
  }

  zmq_close(sock);
  zmq_ctx_term(ctx);
}

}  // namespace ocg
