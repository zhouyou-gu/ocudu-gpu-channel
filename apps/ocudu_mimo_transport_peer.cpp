#include "app_support.h"
#include "ocudu_gpu_channel/iq.h"

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cmath>
#include <csignal>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <memory>
#include <mutex>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <thread>
#include <utility>
#include <zmq.h>

namespace {

using Clock = std::chrono::steady_clock;

volatile std::sig_atomic_t signal_stop = 0;

void handle_signal(int)
{
  signal_stop = 1;
}

struct Options {
  std::array<std::string, 2> tx_endpoints{
      "tcp://*:2101", "tcp://*:2103"};
  std::array<std::string, 2> rx_endpoints{
      "tcp://127.0.0.1:2100", "tcp://127.0.0.1:2102"};
  std::array<std::size_t, 2> tx_chunk_samples{23040, 23040};
  std::array<std::chrono::microseconds, 2> tx_reply_delays{};
  std::array<std::chrono::microseconds, 2> rx_request_offsets{};
  std::chrono::milliseconds duration{30000};
  std::string summary_json;
  bool self_test = false;
};

struct TxPortResult {
  std::uint64_t transactions = 0;
  std::uint64_t samples = 0;
  std::uint64_t marker_start = 0;
  std::uint64_t marker_next = 0;
  std::uint64_t marker_wraps = 0;
  std::uint64_t request_size_errors = 0;
  std::uint64_t short_sends = 0;
  std::uint64_t zmq_errors = 0;
};

struct RxPortResult {
  std::uint64_t transactions = 0;
  std::uint64_t samples = 0;
  std::uint64_t cumulative_start = 0;
  std::uint64_t cumulative_next = 0;
  std::uint64_t malformed_messages = 0;
  std::uint64_t nonfinite_samples = 0;
  std::uint64_t nonzero_samples = 0;
  std::uint64_t short_requests = 0;
  std::uint64_t zmq_errors = 0;
};

struct RxGroupResult {
  std::uint64_t completed = 0;
  std::uint64_t sibling_size_mismatches = 0;
  std::uint64_t partial_reply_groups = 0;
  std::uint64_t shutdown_outstanding_groups = 0;
};

struct SelfTestResult {
  std::uint64_t completed_groups = 0;
  std::uint64_t marker_checks = 0;
  std::uint64_t marker_mismatches = 0;
  std::uint64_t distinct_reply_sizes = 0;
};

struct SharedState {
  std::atomic_bool stop{false};
  std::atomic<unsigned> ready_workers{0};
  mutable std::mutex error_mutex;
  std::string first_error;

  void fail(std::string message)
  {
    {
      std::lock_guard<std::mutex> lock(error_mutex);
      if (first_error.empty()) {
        first_error = std::move(message);
      }
    }
    stop.store(true, std::memory_order_release);
  }

  [[nodiscard]] std::string error() const
  {
    std::lock_guard<std::mutex> lock(error_mutex);
    return first_error;
  }
};

struct SocketDeleter {
  void operator()(void* socket) const
  {
    if (socket != nullptr) {
      zmq_close(socket);
    }
  }
};

using SocketPtr = std::unique_ptr<void, SocketDeleter>;

void usage()
{
  std::cout
      << "usage: ocudu-mimo-transport-peer [options]\n"
      << "  --tx0-endpoint tcp://*:2101       peer port-0 TX REP bind\n"
      << "  --tx1-endpoint tcp://*:2103       peer port-1 TX REP bind\n"
      << "  --rx0-endpoint tcp://127.0.0.1:2100  peer port-0 RX REQ connect\n"
      << "  --rx1-endpoint tcp://127.0.0.1:2102  peer port-1 RX REQ connect\n"
      << "  --tx0-chunk-samples 23040         independent TX reply size\n"
      << "  --tx1-chunk-samples 23040         independent TX reply size\n"
      << "  --tx0-reply-delay-us 0            delay after a port-0 pull\n"
      << "  --tx1-reply-delay-us 0            delay after a port-1 pull\n"
      << "  --rx0-request-offset-us 0         port-0 request offset per group\n"
      << "  --rx1-request-offset-us 0         port-1 request offset per group\n"
      << "  --duration 30s                    zero is not accepted\n"
      << "  --summary-json PATH               write a machine-readable result\n"
      << "  --self-test                       run an in-process four-socket oracle\n"
      << "\n"
      << "The peer owns two TX REP and two RX REQ sockets. It validates only\n"
      << "raw cf32 multi-port transport and grouped continuity; it does not\n"
      << "decode a UE, layers, rank, or modulation.\n";
}

std::uint64_t parse_u64(std::string_view text, const char* name)
{
  if (text.empty() || text.front() == '-') {
    throw std::invalid_argument(std::string(name) + " must be an unsigned integer");
  }
  std::size_t used = 0;
  std::uint64_t value = 0;
  try {
    value = std::stoull(std::string(text), &used);
  } catch (const std::exception&) {
    throw std::invalid_argument(std::string(name) + " must be an unsigned integer");
  }
  if (used != text.size()) {
    throw std::invalid_argument(std::string(name) + " contains trailing characters");
  }
  return value;
}

std::size_t parse_samples(std::string_view text, const char* name)
{
  const auto value = parse_u64(text, name);
  constexpr std::uint64_t max_samples =
      static_cast<std::uint64_t>(std::numeric_limits<int>::max()) /
      sizeof(ocg::IqSample);
  if (value == 0 || value > max_samples ||
      value > std::numeric_limits<std::size_t>::max()) {
    throw std::invalid_argument(std::string(name) + " is outside the ZMQ message range");
  }
  return static_cast<std::size_t>(value);
}

std::chrono::microseconds parse_delay(std::string_view text, const char* name)
{
  constexpr std::uint64_t max_delay_us = 5000000;
  const auto value = parse_u64(text, name);
  if (value > max_delay_us) {
    throw std::invalid_argument(std::string(name) + " exceeds 5000000 us");
  }
  return std::chrono::microseconds(value);
}

Options parse_options(int argc, char** argv)
{
  Options options;
  for (int i = 1; i < argc; ++i) {
    const std::string arg = argv[i];
    auto require_value = [&]() -> std::string {
      if (i + 1 >= argc) {
        throw std::invalid_argument("missing value for " + arg);
      }
      return argv[++i];
    };
    if (arg == "--help" || arg == "-h") {
      usage();
      std::exit(0);
    } else if (arg == "--tx0-endpoint") {
      options.tx_endpoints[0] = require_value();
    } else if (arg == "--tx1-endpoint") {
      options.tx_endpoints[1] = require_value();
    } else if (arg == "--rx0-endpoint") {
      options.rx_endpoints[0] = require_value();
    } else if (arg == "--rx1-endpoint") {
      options.rx_endpoints[1] = require_value();
    } else if (arg == "--tx0-chunk-samples") {
      options.tx_chunk_samples[0] =
          parse_samples(require_value(), "--tx0-chunk-samples");
    } else if (arg == "--tx1-chunk-samples") {
      options.tx_chunk_samples[1] =
          parse_samples(require_value(), "--tx1-chunk-samples");
    } else if (arg == "--tx0-reply-delay-us") {
      options.tx_reply_delays[0] =
          parse_delay(require_value(), "--tx0-reply-delay-us");
    } else if (arg == "--tx1-reply-delay-us") {
      options.tx_reply_delays[1] =
          parse_delay(require_value(), "--tx1-reply-delay-us");
    } else if (arg == "--rx0-request-offset-us") {
      options.rx_request_offsets[0] =
          parse_delay(require_value(), "--rx0-request-offset-us");
    } else if (arg == "--rx1-request-offset-us") {
      options.rx_request_offsets[1] =
          parse_delay(require_value(), "--rx1-request-offset-us");
    } else if (arg == "--duration") {
      options.duration = ocg::app::parse_duration(require_value());
      if (options.duration.count() <= 0) {
        throw std::invalid_argument("--duration must be positive");
      }
    } else if (arg == "--summary-json") {
      options.summary_json = require_value();
      if (options.summary_json.empty()) {
        throw std::invalid_argument("--summary-json must not be empty");
      }
    } else if (arg == "--self-test") {
      options.self_test = true;
    } else {
      throw std::invalid_argument("unknown argument: " + arg);
    }
  }
  for (const auto& endpoint : options.tx_endpoints) {
    if (endpoint.empty()) {
      throw std::invalid_argument("TX endpoints must not be empty");
    }
  }
  for (const auto& endpoint : options.rx_endpoints) {
    if (endpoint.empty()) {
      throw std::invalid_argument("RX endpoints must not be empty");
    }
  }
  if (options.tx_endpoints[0] == options.tx_endpoints[1] ||
      options.rx_endpoints[0] == options.rx_endpoints[1]) {
    throw std::invalid_argument("each peer port requires a unique endpoint");
  }
  if (options.self_test) {
    options.tx_endpoints = {"inproc://ocg-mimo-peer-selftest-tx0",
                            "inproc://ocg-mimo-peer-selftest-tx1"};
    options.rx_endpoints = {"inproc://ocg-mimo-peer-selftest-rx0",
                            "inproc://ocg-mimo-peer-selftest-rx1"};
    options.tx_chunk_samples = {127, 251};
    options.tx_reply_delays = {std::chrono::microseconds(0),
                               std::chrono::microseconds(31)};
    options.rx_request_offsets = {std::chrono::microseconds(43),
                                  std::chrono::microseconds(0)};
    options.duration = std::chrono::milliseconds(1000);
  }
  return options;
}

SocketPtr make_socket(void* context, int type)
{
  SocketPtr socket(zmq_socket(context, type));
  if (!socket) {
    throw std::runtime_error(std::string("zmq_socket failed: ") +
                             zmq_strerror(zmq_errno()));
  }
  const int timeout_ms = 100;
  const int linger = 0;
  if (zmq_setsockopt(socket.get(), ZMQ_RCVTIMEO, &timeout_ms,
                     sizeof(timeout_ms)) != 0 ||
      zmq_setsockopt(socket.get(), ZMQ_SNDTIMEO, &timeout_ms,
                     sizeof(timeout_ms)) != 0 ||
      zmq_setsockopt(socket.get(), ZMQ_LINGER, &linger, sizeof(linger)) != 0) {
    throw std::runtime_error(std::string("zmq_setsockopt failed: ") +
                             zmq_strerror(zmq_errno()));
  }
  if (type == ZMQ_REP) {
    const int unlimited = 0;
    if (zmq_setsockopt(socket.get(), ZMQ_SNDHWM, &unlimited,
                       sizeof(unlimited)) != 0) {
      throw std::runtime_error(std::string("setting REP SNDHWM failed: ") +
                               zmq_strerror(zmq_errno()));
    }
  }
  return socket;
}

bool is_retryable_zmq_error()
{
  return zmq_errno() == EAGAIN || zmq_errno() == EINTR;
}

bool is_shutdown_zmq_error(const SharedState& shared)
{
  return shared.stop.load(std::memory_order_acquire) &&
         (zmq_errno() == ETERM || zmq_errno() == EINTR || zmq_errno() == EAGAIN);
}

// The marker stays in a normalised cf32 range. Port sign distinguishes the two
// lanes; two exact power-of-two fractions carry the low/high 16-bit words of
// the absolute sample ordinal. The encoding repeats only after 2^32 samples,
// while marker_wraps keeps that boundary explicit in the summary.
ocg::IqSample cumulative_marker(std::size_t port, std::uint64_t ordinal)
{
  constexpr float scale = 1.0F / 131072.0F;
  const auto low = static_cast<std::uint32_t>(ordinal & 0xffffU);
  const auto high = static_cast<std::uint32_t>((ordinal >> 16U) & 0xffffU);
  const float sign = port == 0 ? 1.0F : -1.0F;
  return {sign * (0.25F + static_cast<float>(low) * scale),
          -0.25F + static_cast<float>(high) * scale};
}

bool marker_equal(ocg::IqSample lhs, ocg::IqSample rhs)
{
  return lhs.i == rhs.i && lhs.q == rhs.q;
}

void interruptible_delay(std::chrono::microseconds delay,
                         const SharedState& shared)
{
  const auto deadline = Clock::now() + delay;
  while (!shared.stop.load(std::memory_order_acquire) &&
         Clock::now() < deadline) {
    const auto remaining = deadline - Clock::now();
    std::this_thread::sleep_for(std::min(
        remaining, std::chrono::duration_cast<Clock::duration>(
                       std::chrono::microseconds(100))));
  }
}

void run_tx_port(void* context,
                 const Options& options,
                 std::size_t port,
                 SharedState& shared,
                 TxPortResult& result)
{
  try {
    auto socket = make_socket(context, ZMQ_REP);
    if (zmq_bind(socket.get(), options.tx_endpoints[port].c_str()) != 0) {
      throw std::runtime_error("TX bind failed on port " +
                               std::to_string(port) + ": " +
                               zmq_strerror(zmq_errno()));
    }
    ocg::IqBuffer payload(options.tx_chunk_samples[port]);
    shared.ready_workers.fetch_add(1, std::memory_order_release);

    while (!shared.stop.load(std::memory_order_acquire)) {
      std::array<std::byte, 64> request{};
      const int bytes = zmq_recv(socket.get(), request.data(), request.size(), 0);
      if (bytes < 0) {
        if (is_shutdown_zmq_error(shared)) {
          break;
        }
        if (is_retryable_zmq_error()) {
          continue;
        }
        ++result.zmq_errors;
        throw std::runtime_error("TX receive failed on port " +
                                 std::to_string(port) + ": " +
                                 zmq_strerror(zmq_errno()));
      }
      if (bytes != 1) {
        ++result.request_size_errors;
      }

      const std::uint64_t first = result.marker_next;
      for (std::size_t n = 0; n != payload.size(); ++n) {
        payload[n] = cumulative_marker(port, first + n);
      }
      interruptible_delay(options.tx_reply_delays[port], shared);
      if (shared.stop.load(std::memory_order_acquire)) {
        break;
      }

      const auto payload_bytes = payload.size() * sizeof(ocg::IqSample);
      bool sent = false;
      while (!sent && !shared.stop.load(std::memory_order_acquire)) {
        const int sent_bytes =
            zmq_send(socket.get(), payload.data(), payload_bytes, 0);
        if (sent_bytes == static_cast<int>(payload_bytes)) {
          sent = true;
        } else if (sent_bytes >= 0) {
          ++result.short_sends;
          throw std::runtime_error("short TX reply on port " +
                                   std::to_string(port));
        } else if (is_shutdown_zmq_error(shared)) {
          break;
        } else if (!is_retryable_zmq_error()) {
          ++result.zmq_errors;
          throw std::runtime_error("TX send failed on port " +
                                   std::to_string(port) + ": " +
                                   zmq_strerror(zmq_errno()));
        }
      }
      if (!sent) {
        break;
      }
      ++result.transactions;
      result.samples += payload.size();
      result.marker_next += payload.size();
      result.marker_wraps = result.marker_next >> 32U;
    }
  } catch (const std::exception& e) {
    shared.fail("TX port " + std::to_string(port) + ": " + e.what());
  } catch (...) {
    shared.fail("TX port " + std::to_string(port) +
                ": non-standard exception");
  }
}

void inspect_rx_message(zmq_msg_t& message,
                        int bytes,
                        RxPortResult& result)
{
  if (bytes <= 0 ||
      static_cast<std::size_t>(bytes) % sizeof(ocg::IqSample) != 0) {
    ++result.malformed_messages;
    return;
  }
  const auto count = static_cast<std::size_t>(bytes) / sizeof(ocg::IqSample);
  const auto* samples =
      static_cast<const ocg::IqSample*>(zmq_msg_data(&message));
  for (std::size_t n = 0; n != count; ++n) {
    if (!std::isfinite(samples[n].i) || !std::isfinite(samples[n].q)) {
      ++result.nonfinite_samples;
    }
    if (samples[n].i != 0.0F || samples[n].q != 0.0F) {
      ++result.nonzero_samples;
    }
  }
  ++result.transactions;
  result.samples += count;
  result.cumulative_next += count;
}

void run_rx_group(void* context,
                  const Options& options,
                  SharedState& shared,
                  std::array<RxPortResult, 2>& results,
                  RxGroupResult& group)
{
  try {
    std::array<SocketPtr, 2> sockets;
    for (std::size_t port = 0; port != sockets.size(); ++port) {
      sockets[port] = make_socket(context, ZMQ_REQ);
      if (zmq_connect(sockets[port].get(),
                      options.rx_endpoints[port].c_str()) != 0) {
        throw std::runtime_error("RX connect failed on port " +
                                 std::to_string(port) + ": " +
                                 zmq_strerror(zmq_errno()));
      }
    }
    shared.ready_workers.fetch_add(1, std::memory_order_release);

    while (!shared.stop.load(std::memory_order_acquire)) {
      const auto group_start = Clock::now();
      std::array<std::size_t, 2> order{0, 1};
      if (options.rx_request_offsets[1] < options.rx_request_offsets[0]) {
        std::swap(order[0], order[1]);
      }
      std::array<bool, 2> requested{};
      for (const auto port : order) {
        const auto request_time = group_start + options.rx_request_offsets[port];
        while (!shared.stop.load(std::memory_order_acquire) &&
               Clock::now() < request_time) {
          std::this_thread::sleep_until(std::min(
              request_time, Clock::now() + std::chrono::microseconds(100)));
        }
        bool sent = false;
        while (!sent && !shared.stop.load(std::memory_order_acquire)) {
          const std::uint8_t request = 0;
          const int bytes =
              zmq_send(sockets[port].get(), &request, sizeof(request), 0);
          if (bytes == static_cast<int>(sizeof(request))) {
            sent = true;
            requested[port] = true;
          } else if (bytes >= 0) {
            ++results[port].short_requests;
            throw std::runtime_error("short RX request on port " +
                                     std::to_string(port));
          } else if (is_shutdown_zmq_error(shared)) {
            break;
          } else if (!is_retryable_zmq_error()) {
            ++results[port].zmq_errors;
            throw std::runtime_error("RX request failed on port " +
                                     std::to_string(port) + ": " +
                                     zmq_strerror(zmq_errno()));
          }
        }
      }
      if (!requested[0] || !requested[1]) {
        if (requested[0] || requested[1]) {
          ++group.shutdown_outstanding_groups;
        }
        break;
      }

      std::array<bool, 2> awaiting{true, true};
      std::array<int, 2> reply_bytes{-1, -1};
      std::size_t remaining = 2;
      while (remaining != 0 &&
             !shared.stop.load(std::memory_order_acquire)) {
        zmq_pollitem_t items[2] = {
            {sockets[0].get(), 0,
             static_cast<short>(awaiting[0] ? ZMQ_POLLIN : 0), 0},
            {sockets[1].get(), 0,
             static_cast<short>(awaiting[1] ? ZMQ_POLLIN : 0), 0}};
        const int ready = zmq_poll(items, 2, 100);
        if (ready < 0) {
          if (is_shutdown_zmq_error(shared)) {
            break;
          }
          if (zmq_errno() == EINTR) {
            continue;
          }
          throw std::runtime_error(std::string("RX poll failed: ") +
                                   zmq_strerror(zmq_errno()));
        }
        if (ready == 0) {
          continue;
        }
        for (std::size_t port = 0; port != sockets.size(); ++port) {
          if (!awaiting[port] || (items[port].revents & ZMQ_POLLIN) == 0) {
            continue;
          }
          zmq_msg_t message;
          zmq_msg_init(&message);
          const int bytes =
              zmq_msg_recv(&message, sockets[port].get(), ZMQ_DONTWAIT);
          if (bytes >= 0) {
            reply_bytes[port] = bytes;
            inspect_rx_message(message, bytes, results[port]);
            awaiting[port] = false;
            --remaining;
          } else if (!is_retryable_zmq_error() &&
                     !is_shutdown_zmq_error(shared)) {
            ++results[port].zmq_errors;
            const std::string error = zmq_strerror(zmq_errno());
            zmq_msg_close(&message);
            throw std::runtime_error("RX reply failed on port " +
                                     std::to_string(port) + ": " + error);
          }
          zmq_msg_close(&message);
        }
      }
      if (remaining != 0) {
        const std::size_t received = 2 - remaining;
        if (received != 0) {
          ++group.partial_reply_groups;
        } else {
          ++group.shutdown_outstanding_groups;
        }
        break;
      }
      ++group.completed;
      if (reply_bytes[0] != reply_bytes[1]) {
        ++group.sibling_size_mismatches;
      }
    }
  } catch (const std::exception& e) {
    shared.fail(std::string("RX group: ") + e.what());
  } catch (...) {
    shared.fail("RX group: non-standard exception");
  }
}

void run_self_test_remote(void* context,
                          const Options& options,
                          SharedState& shared,
                          SelfTestResult& result)
{
  try {
    std::array<SocketPtr, 2> tx_requests;
    std::array<SocketPtr, 2> rx_replies;
    for (std::size_t port = 0; port != 2; ++port) {
      rx_replies[port] = make_socket(context, ZMQ_REP);
      if (zmq_bind(rx_replies[port].get(),
                   options.rx_endpoints[port].c_str()) != 0) {
        throw std::runtime_error("self-test RX bind failed on port " +
                                 std::to_string(port) + ": " +
                                 zmq_strerror(zmq_errno()));
      }
      tx_requests[port] = make_socket(context, ZMQ_REQ);
      if (zmq_connect(tx_requests[port].get(),
                      options.tx_endpoints[port].c_str()) != 0) {
        throw std::runtime_error("self-test TX connect failed on port " +
                                 std::to_string(port) + ": " +
                                 zmq_strerror(zmq_errno()));
      }
    }

    constexpr std::uint64_t target_groups = 64;
    constexpr std::array<std::size_t, 4> reply_sizes{17, 409, 83, 251};
    std::array<std::uint64_t, 2> expected_tx_offset{};
    std::array<ocg::IqBuffer, 2> rx_payloads;
    for (std::uint64_t generation = 0;
         generation != target_groups &&
         !shared.stop.load(std::memory_order_acquire);
         ++generation) {
      for (std::size_t port = 0; port != 2; ++port) {
        const std::uint8_t request = 0;
        bool sent = false;
        while (!sent && !shared.stop.load(std::memory_order_acquire)) {
          const int bytes = zmq_send(tx_requests[port].get(), &request,
                                     sizeof(request), 0);
          if (bytes == static_cast<int>(sizeof(request))) {
            sent = true;
          } else if (bytes >= 0) {
            throw std::runtime_error("self-test short TX request");
          } else if (!is_retryable_zmq_error() &&
                     !is_shutdown_zmq_error(shared)) {
            throw std::runtime_error(std::string("self-test TX request failed: ") +
                                     zmq_strerror(zmq_errno()));
          }
        }

        bool received = false;
        while (!received && !shared.stop.load(std::memory_order_acquire)) {
          zmq_msg_t message;
          zmq_msg_init(&message);
          const int bytes = zmq_msg_recv(&message, tx_requests[port].get(), 0);
          if (bytes >= 0) {
            const auto expected_bytes =
                options.tx_chunk_samples[port] * sizeof(ocg::IqSample);
            if (static_cast<std::size_t>(bytes) != expected_bytes) {
              zmq_msg_close(&message);
              throw std::runtime_error("self-test TX reply size mismatch");
            }
            const auto* samples =
                static_cast<const ocg::IqSample*>(zmq_msg_data(&message));
            for (std::size_t n = 0; n != options.tx_chunk_samples[port]; ++n) {
              ++result.marker_checks;
              if (!marker_equal(samples[n], cumulative_marker(
                                                 port,
                                                 expected_tx_offset[port] + n))) {
                ++result.marker_mismatches;
              }
            }
            expected_tx_offset[port] += options.tx_chunk_samples[port];
            received = true;
          } else if (!is_retryable_zmq_error() &&
                     !is_shutdown_zmq_error(shared)) {
            const std::string error = zmq_strerror(zmq_errno());
            zmq_msg_close(&message);
            throw std::runtime_error("self-test TX reply failed: " + error);
          }
          zmq_msg_close(&message);
        }
      }

      const std::size_t reply_count =
          reply_sizes[static_cast<std::size_t>(generation) % reply_sizes.size()];
      for (std::size_t port = 0; port != 2; ++port) {
        bool received = false;
        while (!received && !shared.stop.load(std::memory_order_acquire)) {
          std::array<std::byte, 64> request{};
          const int bytes = zmq_recv(rx_replies[port].get(), request.data(),
                                     request.size(), 0);
          if (bytes == 1) {
            received = true;
          } else if (bytes >= 0) {
            throw std::runtime_error("self-test RX request size mismatch");
          } else if (!is_retryable_zmq_error() &&
                     !is_shutdown_zmq_error(shared)) {
            throw std::runtime_error(std::string("self-test RX receive failed: ") +
                                     zmq_strerror(zmq_errno()));
          }
        }
        rx_payloads[port].resize(reply_count);
        for (std::size_t n = 0; n != reply_count; ++n) {
          rx_payloads[port][n] = {
              static_cast<float>(port + 1) * 0.25F,
              static_cast<float>((generation + n) % 127U) / 127.0F};
        }
        const std::size_t reply_bytes = reply_count * sizeof(ocg::IqSample);
        bool sent = false;
        while (!sent && !shared.stop.load(std::memory_order_acquire)) {
          const int bytes = zmq_send(rx_replies[port].get(),
                                     rx_payloads[port].data(), reply_bytes, 0);
          if (bytes == static_cast<int>(reply_bytes)) {
            sent = true;
          } else if (bytes >= 0) {
            throw std::runtime_error("self-test short RX reply");
          } else if (!is_retryable_zmq_error() &&
                     !is_shutdown_zmq_error(shared)) {
            throw std::runtime_error(std::string("self-test RX reply failed: ") +
                                     zmq_strerror(zmq_errno()));
          }
        }
      }
      ++result.completed_groups;
    }
    result.distinct_reply_sizes = reply_sizes.size();
    if (result.completed_groups != target_groups || result.marker_checks == 0 ||
        result.marker_mismatches != 0) {
      throw std::runtime_error("self-test continuity oracle failed");
    }
  } catch (const std::exception& e) {
    shared.fail(std::string("self-test remote: ") + e.what());
  } catch (...) {
    shared.fail("self-test remote: non-standard exception");
  }
}

std::string json_escape(std::string_view value)
{
  std::ostringstream out;
  for (const unsigned char ch : value) {
    switch (ch) {
      case '\"': out << "\\\""; break;
      case '\\': out << "\\\\"; break;
      case '\b': out << "\\b"; break;
      case '\f': out << "\\f"; break;
      case '\n': out << "\\n"; break;
      case '\r': out << "\\r"; break;
      case '\t': out << "\\t"; break;
      default:
        if (ch < 0x20U) {
          out << "\\u" << std::hex << std::setw(4) << std::setfill('0')
              << static_cast<unsigned>(ch) << std::dec;
        } else {
          out << static_cast<char>(ch);
        }
    }
  }
  return out.str();
}

bool result_ok(const std::array<TxPortResult, 2>& tx,
               const std::array<RxPortResult, 2>& rx,
               const RxGroupResult& group,
               const SelfTestResult& self_test,
               bool self_test_enabled,
               const std::string& error)
{
  if (!error.empty() || group.completed == 0 ||
      group.sibling_size_mismatches != 0 || group.partial_reply_groups != 0) {
    return false;
  }
  for (std::size_t port = 0; port != 2; ++port) {
    if (tx[port].transactions == 0 || tx[port].samples == 0 ||
        tx[port].marker_start != 0 ||
        tx[port].marker_next != tx[port].samples ||
        tx[port].request_size_errors != 0 || tx[port].short_sends != 0 ||
        tx[port].zmq_errors != 0 || rx[port].transactions == 0 ||
        rx[port].samples == 0 || rx[port].cumulative_start != 0 ||
        rx[port].cumulative_next != rx[port].samples ||
        rx[port].nonzero_samples == 0 ||
        rx[port].malformed_messages != 0 ||
        rx[port].nonfinite_samples != 0 || rx[port].short_requests != 0 ||
        rx[port].zmq_errors != 0 ||
        rx[port].transactions != group.completed) {
      return false;
    }
  }
  if (self_test_enabled &&
      (self_test.completed_groups != 64 || self_test.marker_checks == 0 ||
       self_test.marker_mismatches != 0 ||
       self_test.distinct_reply_sizes < 2)) {
    return false;
  }
  return true;
}

std::string make_summary_json(const Options& options,
                              const std::array<TxPortResult, 2>& tx,
                              const std::array<RxPortResult, 2>& rx,
                              const RxGroupResult& group,
                              const SelfTestResult& self_test,
                              const std::string& error,
                              bool ok)
{
  std::ostringstream out;
  out << "{\n"
      << "  \"schema\": \"ocudu-mimo-transport-peer/v1\",\n"
      << "  \"status\": \"" << (ok ? "passed" : "failed") << "\",\n"
      << "  \"transport_only\": true,\n"
      << "  \"rank2_claim\": false,\n"
      << "  \"self_test\": " << (options.self_test ? "true" : "false")
      << ",\n"
      << "  \"duration_ms\": " << options.duration.count() << ",\n"
      << "  \"error\": \"" << json_escape(error) << "\",\n"
      << "  \"tx_ports\": [\n";
  for (std::size_t port = 0; port != 2; ++port) {
    const auto& value = tx[port];
    out << "    {\"port\": " << port
        << ", \"endpoint\": \"" << json_escape(options.tx_endpoints[port])
        << "\", \"chunk_samples\": " << options.tx_chunk_samples[port]
        << ", \"reply_delay_us\": " << options.tx_reply_delays[port].count()
        << ", \"transactions\": " << value.transactions
        << ", \"samples\": " << value.samples
        << ", \"marker_start\": " << value.marker_start
        << ", \"marker_next\": " << value.marker_next
        << ", \"marker_wraps\": " << value.marker_wraps
        << ", \"request_size_errors\": " << value.request_size_errors
        << ", \"short_sends\": " << value.short_sends
        << ", \"zmq_errors\": " << value.zmq_errors << "}"
        << (port == 0 ? "," : "") << "\n";
  }
  out << "  ],\n  \"rx_ports\": [\n";
  for (std::size_t port = 0; port != 2; ++port) {
    const auto& value = rx[port];
    out << "    {\"port\": " << port
        << ", \"endpoint\": \"" << json_escape(options.rx_endpoints[port])
        << "\", \"request_offset_us\": "
        << options.rx_request_offsets[port].count()
        << ", \"transactions\": " << value.transactions
        << ", \"samples\": " << value.samples
        << ", \"cumulative_start\": " << value.cumulative_start
        << ", \"cumulative_next\": " << value.cumulative_next
        << ", \"malformed_messages\": " << value.malformed_messages
        << ", \"nonfinite_samples\": " << value.nonfinite_samples
        << ", \"nonzero_samples\": " << value.nonzero_samples
        << ", \"short_requests\": " << value.short_requests
        << ", \"zmq_errors\": " << value.zmq_errors << "}"
        << (port == 0 ? "," : "") << "\n";
  }
  out << "  ],\n"
      << "  \"rx_groups\": {\"completed\": " << group.completed
      << ", \"sibling_size_mismatches\": "
      << group.sibling_size_mismatches
      << ", \"partial_reply_groups\": " << group.partial_reply_groups
      << ", \"shutdown_outstanding_groups\": "
      << group.shutdown_outstanding_groups << "}\n"
      << "  ,\"self_test_result\": {\"completed_groups\": "
      << self_test.completed_groups << ", \"marker_checks\": "
      << self_test.marker_checks << ", \"marker_mismatches\": "
      << self_test.marker_mismatches << ", \"distinct_reply_sizes\": "
      << self_test.distinct_reply_sizes << "}\n"
      << "}\n";
  return out.str();
}

void write_summary(const std::string& path, const std::string& json)
{
  if (path.empty()) {
    return;
  }
  std::ofstream output(path, std::ios::out | std::ios::trunc);
  if (!output) {
    throw std::runtime_error("unable to open summary path: " + path);
  }
  output << json;
  if (!output) {
    throw std::runtime_error("failed to write summary path: " + path);
  }
}

} // namespace

int main(int argc, char** argv)
{
  try {
    const Options options = parse_options(argc, argv);
    std::signal(SIGINT, handle_signal);
    std::signal(SIGTERM, handle_signal);

    void* context = zmq_ctx_new();
    if (context == nullptr) {
      throw std::runtime_error(std::string("zmq_ctx_new failed: ") +
                               zmq_strerror(zmq_errno()));
    }
    SharedState shared;
    std::array<TxPortResult, 2> tx_results;
    std::array<RxPortResult, 2> rx_results;
    RxGroupResult group_result;
    SelfTestResult self_test_result;

    std::thread self_test_remote;
    std::thread tx0;
    std::thread tx1;
    std::thread rx;
    auto shutdown_and_join = [&] {
      shared.stop.store(true, std::memory_order_release);
      zmq_ctx_shutdown(context);
      if (tx0.joinable()) {
        tx0.join();
      }
      if (tx1.joinable()) {
        tx1.join();
      }
      if (rx.joinable()) {
        rx.join();
      }
      if (self_test_remote.joinable()) {
        self_test_remote.join();
      }
      zmq_ctx_term(context);
    };
    try {
      if (options.self_test) {
        self_test_remote = std::thread(run_self_test_remote, context,
                                       std::cref(options), std::ref(shared),
                                       std::ref(self_test_result));
      }
      tx0 = std::thread(run_tx_port, context, std::cref(options), 0,
                        std::ref(shared), std::ref(tx_results[0]));
      tx1 = std::thread(run_tx_port, context, std::cref(options), 1,
                        std::ref(shared), std::ref(tx_results[1]));
      rx = std::thread(run_rx_group, context, std::cref(options),
                       std::ref(shared), std::ref(rx_results),
                       std::ref(group_result));
    } catch (...) {
      shutdown_and_join();
      throw;
    }

    const auto start = Clock::now();
    const auto deadline = start + options.duration;
    const auto ready_deadline = start + std::chrono::seconds(5);
    while (!shared.stop.load(std::memory_order_acquire) &&
           signal_stop == 0 && Clock::now() < deadline) {
      if (shared.ready_workers.load(std::memory_order_acquire) == 3) {
        break;
      }
      if (Clock::now() >= ready_deadline) {
        shared.fail("peer workers did not become ready within 5 seconds");
        break;
      }
      std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    if (!shared.stop.load(std::memory_order_acquire) && signal_stop == 0 &&
        shared.ready_workers.load(std::memory_order_acquire) == 3) {
      std::cout << "event=ready transport_only=1 tx_ports=2 rx_ports=2\n";
      std::cout.flush();
      while (!shared.stop.load(std::memory_order_acquire) &&
             signal_stop == 0 && Clock::now() < deadline) {
        std::this_thread::sleep_for(std::chrono::milliseconds(20));
      }
    }

    shutdown_and_join();

    const std::string error = shared.error();
    const bool ok = result_ok(tx_results, rx_results, group_result,
                              self_test_result, options.self_test, error);
    const std::string summary = make_summary_json(
        options, tx_results, rx_results, group_result, self_test_result, error,
        ok);
    write_summary(options.summary_json, summary);

    std::cout << "event=stop transport_only=1 rank2_claim=0 status="
              << (ok ? "passed" : "failed")
              << " tx0_transactions=" << tx_results[0].transactions
              << " tx1_transactions=" << tx_results[1].transactions
              << " tx0_samples=" << tx_results[0].samples
              << " tx1_samples=" << tx_results[1].samples
              << " tx0_marker_next=" << tx_results[0].marker_next
              << " tx1_marker_next=" << tx_results[1].marker_next
              << " rx0_transactions=" << rx_results[0].transactions
              << " rx1_transactions=" << rx_results[1].transactions
              << " rx0_samples=" << rx_results[0].samples
              << " rx1_samples=" << rx_results[1].samples
              << " rx_groups=" << group_result.completed
              << " sibling_size_mismatches="
              << group_result.sibling_size_mismatches
              << " partial_reply_groups=" << group_result.partial_reply_groups
              << " shutdown_outstanding_groups="
              << group_result.shutdown_outstanding_groups
              << " self_test=" << (options.self_test ? 1 : 0)
              << " self_test_marker_checks=" << self_test_result.marker_checks
              << " self_test_marker_mismatches="
              << self_test_result.marker_mismatches << "\n";
    if (!error.empty()) {
      std::cerr << "event=fatal error=\"" << json_escape(error) << "\"\n";
    }
    return ok ? 0 : 1;
  } catch (const std::invalid_argument& e) {
    std::cerr << "error: " << e.what() << "\n";
    usage();
    return 2;
  } catch (const std::exception& e) {
    std::cerr << "event=fatal error=\"" << json_escape(e.what()) << "\"\n";
    return 1;
  }
}
