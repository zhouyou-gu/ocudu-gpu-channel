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
// Minimal JSON parser/encoder for the narrow message shape v1 needs.
//
// Accepts flat objects with string keys and string-or-number values:
//   {"link_id": "ue0-gnb0", "param": "path_loss_db", "value": -12.5}
//
// Rejects arrays, nested objects, escapes inside strings (the only escape
// supported is `\"`), and unicode escapes. Adequate for the v1 control
// message shape; if v2 grows JSON complexity, swap to a real library.
// ────────────────────────────────────────────────────────────────────────

struct JsonValue {
  enum class Kind { String, Number, Bool };
  Kind kind = Kind::String;
  std::string s;
  double n = 0.0;
  bool b = false;
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
    JsonValue v;
    if (peek() == '"') {
      v.kind = JsonValue::Kind::String;
      v.s = read_string();
      return v;
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
  {"awgn_sigma",                   0.0,       10.0,       false},
  {"los_k_db",                   -30.0,       40.0,       false},
  {"cfo_hz",                  -50000.0,    50000.0,       false},
  {"tap0_delay_samples",           0.0,     1023.0,       true },
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
  if (spec.name == std::string_view("awgn_sigma"))         return ctl.shadow.awgn_sigma;
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
  else if (spec.name == std::string_view("awgn_sigma"))    ctl.shadow.awgn_sigma   = static_cast<float>(value);
  else if (spec.name == std::string_view("los_k_db"))      ctl.shadow.los_k_db     = static_cast<float>(value);
  else if (spec.name == std::string_view("cfo_hz"))        ctl.shadow.cfo_hz       = static_cast<float>(value);
  else if (spec.name == std::string_view("tap0_delay_samples")) ctl.shadow.tap0_delay_samples = static_cast<std::int32_t>(value);
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

std::string ControlServer::handle_message(const std::string& request_body)
{
  msgs_received_.fetch_add(1, std::memory_order_relaxed);

  // Inner lambda: log the rejection in the existing broker's k=v format,
  // bump the counter, and return the JSON REP body. Centralised so every
  // exit path is consistent.
  auto reject = [this](const std::string& reason) -> std::string {
    updates_rejected_.fetch_add(1, std::memory_order_relaxed);
    if (config_.logger) {
      config_.logger(std::string("event=control_error reason=\"") + reason + "\"");
    }
    return make_error_reply(reason);
  };

  std::unordered_map<std::string, JsonValue> fields;
  try {
    JsonReader r(request_body);
    fields = r.read_flat_object();
  } catch (const std::exception& e) {
    return reject(std::string("malformed JSON: ") + e.what());
  }

  auto it_link  = fields.find("link_id");
  auto it_param = fields.find("param");
  auto it_value = fields.find("value");
  if (it_link == fields.end() || it_param == fields.end() || it_value == fields.end()) {
    return reject("missing required field (need link_id, param, value)");
  }
  if (it_link->second.kind != JsonValue::Kind::String ||
      it_param->second.kind != JsonValue::Kind::String ||
      it_value->second.kind != JsonValue::Kind::Number) {
    return reject("type error: link_id/param must be strings, value must be a number");
  }

  const std::string& link_id = it_link->second.s;
  const std::string& param   = it_param->second.s;
  const double value         = it_value->second.n;

  auto it_ctl = link_map_.find(link_id);
  if (it_ctl == link_map_.end() || it_ctl->second == nullptr) {
    return reject("unknown link_id: " + link_id);
  }

  const ParamSpec* spec = find_param_spec(param);
  if (spec == nullptr) {
    return reject("unknown param: " + param);
  }
  if (!std::isfinite(value) || value < spec->min || value > spec->max) {
    return reject(
        "param '" + param + "' value " + format_double(value) + " out of range ["
        + format_double(spec->min) + ", " + format_double(spec->max) + "]");
  }
  if (spec->is_int && std::trunc(value) != value) {
    return reject("param '" + param + "' requires an integer value");
  }

  // Capture old before the write so the log line reports both values.
  const double old_value = read_shadow(*it_ctl->second, *spec);

  if (!apply_update(*it_ctl->second, *spec, value)) {
    return reject("internal: failed to apply update");
  }

  updates_applied_.fetch_add(1, std::memory_order_relaxed);
  const std::uint32_t observed_seqno =
      it_ctl->second->seqno.load(std::memory_order_relaxed);

  if (config_.logger) {
    std::ostringstream o;
    o << "event=control_update link_id=" << link_id
      << " param=" << param
      << " old=" << format_double(old_value)
      << " new=" << format_double(value)
      << " seqno=" << observed_seqno;
    config_.logger(o.str());
  }

  return make_success_reply(observed_seqno);
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
