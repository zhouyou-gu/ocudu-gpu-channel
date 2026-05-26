// ControlServer test — two halves:
//   C3a: handle_message() exercised directly (no ZMQ). Covers JSON parse,
//        whitelist validation, range checks, shadow → seqno write-back.
//   C3b: full loop exercised through a real ZMQ REQ client over inproc://
//        or tcp://127.0.0.1 — verifies the REP socket binding, the
//        background thread lifecycle, and the wire-format round-trip.

#include "ocudu_gpu_channel/control_server.h"
#include "ocudu_gpu_channel/runtime_control.h"

#include <zmq.h>

#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdlib>
#include <iostream>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace {

void require(bool condition, const char* message)
{
  if (!condition) {
    std::cerr << "FAIL: " << message << "\n";
    std::exit(1);
  }
}

bool contains(const std::string& haystack, const std::string& needle)
{
  return haystack.find(needle) != std::string::npos;
}

bool nearly(float a, float b, float tol = 1e-6F)
{
  return std::fabs(a - b) <= tol;
}

}  // namespace

int main()
{
  // Allocate two link controls — heap-allocated so the link_map can hold
  // stable pointers across re-binds.
  auto ctl_a = std::make_unique<ocg::BrokerLinkControl>();
  auto ctl_b = std::make_unique<ocg::BrokerLinkControl>();

  ocg::ControlServer::LinkMap link_map = {
      {"ue0-gnb0", ctl_a.get()},
      {"ue1-gnb0", ctl_b.get()},
  };

  // Capture log lines so C4 assertions can grep them.
  std::vector<std::string> log_lines;
  std::mutex log_mu;
  auto recording_logger = [&](std::string_view line) {
    std::lock_guard<std::mutex> g(log_mu);
    log_lines.emplace_back(line);
  };

  ocg::ControlServerConfig cfg;
  cfg.endpoint = "inproc://test-c3a-not-bound";
  cfg.recv_timeout_ms = 100;
  cfg.logger = recording_logger;
  ocg::ControlServer server(std::move(cfg), std::move(link_map));

  // ── Case 1: well-formed REQ updates shadow + bumps seqno + returns seqno
  {
    const std::string reply = server.handle_message(
        R"({"link_id":"ue0-gnb0","param":"path_loss_db","value":-12.5})");
    require(contains(reply, "\"ok\":true"), "expected ok:true for valid request");
    require(contains(reply, "\"seqno\":1"), "expected seqno to be 1 after first apply");
    require(nearly(ctl_a->shadow.path_loss_db, -12.5F), "shadow.path_loss_db should be -12.5");
    require(ctl_a->seqno.load() == 1, "seqno on ctl_a should be 1");
    require(ctl_b->seqno.load() == 0, "ctl_b untouched");
  }

  // ── Case 2: subsequent valid REQ on same link advances seqno
  {
    const std::string reply = server.handle_message(
        R"({"link_id":"ue0-gnb0","param":"cfo_hz","value":250.0})");
    require(contains(reply, "\"ok\":true"), "expected ok:true for second valid request");
    require(contains(reply, "\"seqno\":2"), "expected seqno to be 2");
    require(nearly(ctl_a->shadow.cfo_hz, 250.0F), "shadow.cfo_hz should be 250");
    require(ctl_a->seqno.load() == 2, "seqno on ctl_a should be 2");
  }

  // ── Case 3: separate link, independent seqno counter
  {
    const std::string reply = server.handle_message(
        R"({"link_id":"ue1-gnb0","param":"los_k_db","value":9.0})");
    require(contains(reply, "\"ok\":true"), "expected ok:true");
    require(contains(reply, "\"seqno\":1"), "ctl_b's seqno tracks independently");
    require(nearly(ctl_b->shadow.los_k_db, 9.0F), "ctl_b shadow updated");
  }

  // ── Case 4: fractional tap0_delay accepted (v1-fin-C — used to be int-only)
  {
    const std::string reply = server.handle_message(
        R"({"link_id":"ue0-gnb0","param":"tap0_delay_samples","value":3.5})");
    require(contains(reply, "\"ok\":true"), "fractional tap0_delay should be accepted");
    require(nearly(ctl_a->shadow.tap0_delay_samples, 3.5F),
            "shadow.tap0_delay_samples should be 3.5");
  }

  // ── Case 5: integer value still accepted (no regression)
  {
    const std::string reply = server.handle_message(
        R"({"link_id":"ue0-gnb0","param":"tap0_delay_samples","value":7})");
    require(contains(reply, "\"ok\":true"), "integer value should still work");
    require(nearly(ctl_a->shadow.tap0_delay_samples, 7.0F),
            "shadow.tap0_delay_samples should be 7.0");
  }

  // ── Case 6: out-of-range → rejected with informative message
  {
    const std::string reply = server.handle_message(
        R"({"link_id":"ue0-gnb0","param":"path_loss_db","value":9999.0})");
    require(contains(reply, "\"ok\":false"), "out-of-range should fail");
    require(contains(reply, "out of range"), "error should say out of range");
    require(contains(reply, "[-200"), "error should include the valid range");
  }

  // ── Case 7: unknown param → rejected
  {
    const std::string reply = server.handle_message(
        R"({"link_id":"ue0-gnb0","param":"reverse_polarity","value":1.0})");
    require(contains(reply, "\"ok\":false"), "unknown param should fail");
    require(contains(reply, "unknown param"), "error should say unknown param");
  }

  // ── Case 8: unknown link_id → rejected
  {
    const std::string reply = server.handle_message(
        R"({"link_id":"nope","param":"path_loss_db","value":-5.0})");
    require(contains(reply, "\"ok\":false"), "unknown link should fail");
    require(contains(reply, "unknown link_id"), "error should say unknown link_id");
  }

  // ── Case 9: missing field → rejected
  {
    const std::string reply = server.handle_message(
        R"({"link_id":"ue0-gnb0","param":"path_loss_db"})");
    require(contains(reply, "\"ok\":false"), "missing field should fail");
    require(contains(reply, "missing required field"), "error should mention missing field");
  }

  // ── Case 10: malformed JSON → rejected
  {
    const std::string reply = server.handle_message("not json at all");
    require(contains(reply, "\"ok\":false"), "garbage input should fail");
    require(contains(reply, "malformed JSON"), "error should mention JSON parse");
  }

  // ── Case 11: type mismatch (value as string) → rejected
  {
    const std::string reply = server.handle_message(
        R"({"link_id":"ue0-gnb0","param":"path_loss_db","value":"oops"})");
    require(contains(reply, "\"ok\":false"), "string value should be rejected");
    require(contains(reply, "type error"), "error should mention type");
  }

  // ── Case 12: counters reflect all attempted REQs
  {
    const auto s = server.stats();
    require(s.msgs_received == 11, "msgs_received should equal total handled messages");
    require(s.updates_applied == 5, "5 successful updates: cases 1, 2, 3, 4, 5");
    require(s.updates_rejected == 6, "6 rejections: cases 6, 7, 8, 9, 10, 11");
  }

  // ── Case 13 (C4): log lines reconstruct the experiment trace
  {
    std::lock_guard<std::mutex> g(log_mu);
    require(log_lines.size() == 11, "every REQ must emit exactly one log line");

    // 5 control_update + 6 control_error
    std::size_t updates = 0, errors = 0;
    for (const auto& line : log_lines) {
      if (line.rfind("event=control_update", 0) == 0) ++updates;
      else if (line.rfind("event=control_error", 0) == 0) ++errors;
    }
    require(updates == 5, "should see 5 event=control_update lines");
    require(errors == 6, "should see 6 event=control_error lines");

    // First successful update should carry the before-and-after values.
    require(contains(log_lines[0], "event=control_update"), "first line is an update");
    require(contains(log_lines[0], "link_id=ue0-gnb0"), "first update names link");
    require(contains(log_lines[0], "param=path_loss_db"), "first update names param");
    require(contains(log_lines[0], "old=0"), "first update reports old=0 (YAML default)");
    require(contains(log_lines[0], "new=-12.5"), "first update reports the new value");
    require(contains(log_lines[0], "seqno=1"), "first update reports seqno=1");
  }

  // ── v2.0-F2: profile_swap message type ──────────────────────────────────

  // Case 14: valid profile_swap (2 taps, no fading) → accepted, shadow populated
  {
    const std::string reply = server.handle_message(R"({
      "type":"profile_swap",
      "link_id":"ue0-gnb0",
      "taps":[
        {"delay_samples":0.0,"gain_db":-3.0,"phase_rad":0.0,"is_los":true,"los_k_db":9.0,"los_angle_rad":0.0},
        {"delay_samples":2.5,"gain_db":-6.0,"phase_rad":1.0}
      ]
    })");
    require(contains(reply, "\"ok\":true"), "valid profile_swap should be accepted");
    require(ctl_a->profile_pending, "profile_pending should be set");
    require(ctl_a->shadow_profile.n_taps == 2, "shadow_profile.n_taps should be 2");
    require(nearly(static_cast<float>(ctl_a->shadow_profile.taps[0].gain_db), -3.0F),
            "shadow_profile.taps[0].gain_db should be -3.0");
    require(nearly(static_cast<float>(ctl_a->shadow_profile.taps[1].delay_samples), 2.5F),
            "shadow_profile.taps[1].delay_samples should be 2.5");
    require(ctl_a->shadow_profile.taps[0].is_los, "tap 0 should be LOS");
    require(!ctl_a->shadow_profile.fading_enabled, "fading default off when omitted");
  }

  // Case 15: valid profile_swap with fading sub-config
  {
    const std::string reply = server.handle_message(R"({
      "type":"profile_swap",
      "link_id":"ue1-gnb0",
      "taps":[{"delay_samples":0.0,"gain_db":0.0}],
      "fading":{"enabled":true,"f_d_max_hz":350.0,"spectrum":"jakes","grid_us":100.0}
    })");
    require(contains(reply, "\"ok\":true"), "profile_swap with fading should be accepted");
    require(ctl_b->shadow_profile.fading_enabled, "fading_enabled should be true");
    require(nearly(ctl_b->shadow_profile.fading_f_d_max_hz, 350.0F),
            "fading_f_d_max_hz should be 350");
  }

  // Case 16: > kDeviceMaxTaps taps → rejected
  {
    std::string req = R"({"type":"profile_swap","link_id":"ue0-gnb0","taps":[)";
    for (int i = 0; i < ocg::kDeviceMaxTaps + 1; ++i) {
      if (i) req += ",";
      req += R"({"delay_samples":0,"gain_db":0})";
    }
    req += "]}";
    const std::string reply = server.handle_message(req);
    require(contains(reply, "\"ok\":false"), ">max-taps should be rejected");
    require(contains(reply, "exceeds kDeviceMaxTaps"), "error names the limit");
  }

  // Case 17: empty taps array → rejected
  {
    const std::string reply = server.handle_message(
        R"({"type":"profile_swap","link_id":"ue0-gnb0","taps":[]})");
    require(contains(reply, "\"ok\":false"), "empty taps should be rejected");
    require(contains(reply, "must not be empty"), "error names the constraint");
  }

  // Case 18: profile_swap missing 'taps' → rejected
  {
    const std::string reply = server.handle_message(
        R"({"type":"profile_swap","link_id":"ue0-gnb0"})");
    require(contains(reply, "\"ok\":false"), "missing taps should be rejected");
    require(contains(reply, "missing or non-array"), "error names the issue");
  }

  // Case 19: profile_swap with non-jakes spectrum → rejected
  {
    const std::string reply = server.handle_message(R"({
      "type":"profile_swap","link_id":"ue0-gnb0",
      "taps":[{"delay_samples":0,"gain_db":0}],
      "fading":{"spectrum":"flat"}
    })");
    require(contains(reply, "\"ok\":false"), "non-jakes spectrum should be rejected");
    require(contains(reply, "jakes"), "error names the supported spectrum");
  }

  // Case 20: scalar update with explicit "type":"scalar" works (back-compat)
  {
    const std::string reply = server.handle_message(
        R"({"type":"scalar","link_id":"ue0-gnb0","param":"cfo_hz","value":-150.0})");
    require(contains(reply, "\"ok\":true"), "explicit type=scalar should work");
    require(nearly(ctl_a->shadow.cfo_hz, -150.0F), "shadow.cfo_hz should update");
  }

  // Case 21: unknown type → rejected
  {
    const std::string reply = server.handle_message(
        R"({"type":"future","link_id":"ue0-gnb0"})");
    require(contains(reply, "\"ok\":false"), "unknown type should be rejected");
    require(contains(reply, "unknown message type"), "error names the dispatch failure");
  }

  // ── v2.1: take_effect_at_slot in REQ + applied_at_slot in REP ─────────

  // Case 22: scalar update with explicit take_effect_at_slot — REP echoes
  {
    const std::string reply = server.handle_message(
        R"({"link_id":"ue0-gnb0","param":"cfo_hz","value":-200.0,"take_effect_at_slot":42})");
    require(contains(reply, "\"ok\":true"), "scalar+take_effect should be accepted");
    require(contains(reply, "\"applied_at_slot\":42"),
            "REP should echo the take_effect_at_slot");
    require(ctl_a->take_effect_at_slot == 42,
            "ctl.take_effect_at_slot persisted");
  }

  // Case 23: scalar update with no take_effect_at_slot — applied_at_slot = 0
  // (current_slot of an unused link is 0).
  {
    const std::string reply = server.handle_message(
        R"({"link_id":"ue1-gnb0","param":"path_loss_db","value":3.0})");
    require(contains(reply, "\"ok\":true"), "scalar without take_effect should work");
    require(contains(reply, "\"applied_at_slot\":"),
            "REP should still include applied_at_slot field");
  }

  // Case 24: negative take_effect_at_slot → rejected
  {
    const std::string reply = server.handle_message(
        R"({"link_id":"ue0-gnb0","param":"path_loss_db","value":0.0,"take_effect_at_slot":-1})");
    require(contains(reply, "\"ok\":false"), "negative take_effect should be rejected");
    require(contains(reply, "take_effect_at_slot must be >= 0"),
            "error names the constraint");
  }

  // Case 25: counters after v2.1 cases
  {
    const auto s = server.stats();
    // 13 priors + 8 v2.0 (14-21) + 3 v2.1 (22-24) = 24
    require(s.msgs_received == 22, "msgs_received tracks all v2.1 cases");
    // Successes: 1,2,3,4,5 (=5) + 14,15,20 (=3) + 22,23 (=2) = 10
    require(s.updates_applied == 10, "updates_applied should be 10");
    // Rejections: 6,7,8,9,10,11 (=6) + 16,17,18,19,21 (=5) + 24 (=1) = 12
    require(s.updates_rejected == 12, "updates_rejected should be 12");
  }

  // ── C3b: full end-to-end over real ZMQ REQ ↔ REP on a localhost TCP port
  // Uses a port the OS picks via tcp://127.0.0.1:0 isn't supported on bind
  // for the binding side in older zmq builds, so pick a high port unlikely
  // to clash. If this test ever flakes from port collisions, switch to
  // ipc://${TMPDIR}/<unique>.
  {
    auto ctl_z = std::make_unique<ocg::BrokerLinkControl>();
    ocg::ControlServer::LinkMap zmq_map = {{"ue9-gnb9", ctl_z.get()}};
    ocg::ControlServerConfig zmq_cfg;
    zmq_cfg.endpoint = "tcp://127.0.0.1:5570";
    zmq_cfg.recv_timeout_ms = 50;
    ocg::ControlServer zmq_server(std::move(zmq_cfg), std::move(zmq_map));
    zmq_server.start();

    // Construct a REQ client and send one update. Tiny retry/sleep because
    // the server binds asynchronously after start() returns.
    void* ctx = zmq_ctx_new();
    require(ctx != nullptr, "zmq_ctx_new for client");
    void* sock = zmq_socket(ctx, ZMQ_REQ);
    require(sock != nullptr, "zmq_socket REQ");
    const int snd_timeout = 1000;
    const int rcv_timeout = 1000;
    zmq_setsockopt(sock, ZMQ_SNDTIMEO, &snd_timeout, sizeof(snd_timeout));
    zmq_setsockopt(sock, ZMQ_RCVTIMEO, &rcv_timeout, sizeof(rcv_timeout));

    // Retry connect for up to ~500ms while the REP socket binds.
    bool connected = false;
    for (int attempt = 0; attempt < 10; ++attempt) {
      if (zmq_connect(sock, "tcp://127.0.0.1:5570") == 0) { connected = true; break; }
      std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }
    require(connected, "REQ client should connect to server");

    const std::string req = R"({"link_id":"ue9-gnb9","param":"path_loss_db","value":-15.0})";
    require(zmq_send(sock, req.data(), req.size(), 0) > 0, "REQ send should succeed");

    char buf[1024];
    const int rcvd = zmq_recv(sock, buf, sizeof(buf), 0);
    require(rcvd > 0, "REP recv should succeed");
    const std::string reply(buf, static_cast<std::size_t>(rcvd));
    require(contains(reply, "\"ok\":true"), "ZMQ round-trip should return ok:true");

    // Verify the shadow actually updated.
    require(nearly(ctl_z->shadow.path_loss_db, -15.0F),
            "shadow should reflect ZMQ-delivered update");
    require(ctl_z->seqno.load() == 1, "seqno bumped via the wire path");

    zmq_close(sock);
    zmq_ctx_term(ctx);
    // ControlServer dtor stops the loop + joins the thread.
  }

  std::cout << "test_control_server OK\n";
  return 0;
}
