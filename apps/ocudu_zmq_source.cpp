#include "app_support.h"
#include <algorithm>
#include "ocudu_gpu_channel/iq.h"
#include <chrono>
#include <cmath>
#include <cstring>
#include <iostream>
#include <stdexcept>
#include <string>
#include <thread>
#include <zmq.h>

namespace {

void usage()
{
  std::cout << "usage: ocudu-zmq-source --endpoint tcp://*:2000 [--batch-samples 23040] "
               "[--duration 60s] [--sample-rate-hz 23040000] [--silent]\n";
}

} // namespace

int main(int argc, char** argv)
{
  std::string endpoint = "tcp://*:2000";
  std::size_t batch_samples = 23040;
  // 0 = answer every request immediately, which is what a throughput test
  // wants. Non-zero paces replies at that sample rate, which is what standing
  // in for a lock-step radio requires: an unpaced peer feeds a real gNB faster
  // than its PHY consumes and the RF layer overflows continuously.
  double sample_rate_hz = 0.0;
  bool silent = false;
  std::chrono::milliseconds duration{0};

  for (int i = 1; i < argc; ++i) {
    std::string arg = argv[i];
    if (arg == "--help" || arg == "-h") {
      usage();
      return 0;
    }
    if (arg == "--endpoint" && i + 1 < argc) {
      endpoint = argv[++i];
    } else if (arg == "--batch-samples" && i + 1 < argc) {
      batch_samples = static_cast<std::size_t>(std::stoull(argv[++i]));
    } else if (arg == "--sample-rate-hz" && i + 1 < argc) {
      sample_rate_hz = std::stod(argv[++i]);
    } else if (arg == "--silent") {
      silent = true;
    } else if (arg == "--duration" && i + 1 < argc) {
      duration = ocg::app::parse_duration(argv[++i]);
    } else {
      std::cerr << "unknown or incomplete argument: " << arg << "\n";
      usage();
      return 2;
    }
  }

  void* context = zmq_ctx_new();
  void* socket = zmq_socket(context, ZMQ_REP);
  int timeout = 10;
  int linger = 0;
  zmq_setsockopt(socket, ZMQ_RCVTIMEO, &timeout, sizeof(timeout));
  zmq_setsockopt(socket, ZMQ_SNDTIMEO, &timeout, sizeof(timeout));
  zmq_setsockopt(socket, ZMQ_LINGER, &linger, sizeof(linger));
  if (zmq_bind(socket, endpoint.c_str()) != 0) {
    std::cerr << "event=fatal error=\"" << zmq_strerror(zmq_errno()) << "\"\n";
    return 1;
  }

  ocg::IqBuffer samples(batch_samples);
  std::uint64_t seq = 0;
  const auto start = std::chrono::steady_clock::now();
  // Next instant a reply may leave, when pacing is on. Advancing it by one
  // batch period per reply -- rather than sleeping a fixed amount -- keeps the
  // long-run rate exact instead of drifting by the service time of each batch.
  auto next_reply_at = start;
  const auto batch_period =
      sample_rate_hz > 0.0
          ? std::chrono::duration_cast<std::chrono::steady_clock::duration>(
                std::chrono::duration<double>(static_cast<double>(batch_samples) / sample_rate_hz))
          : std::chrono::steady_clock::duration::zero();
  auto running = [&] { return duration.count() == 0 || std::chrono::steady_clock::now() - start < duration; };
  while (running()) {
    std::uint8_t dummy = 0;
    const int received = zmq_recv(socket, &dummy, sizeof(dummy), 0);
    if (received < 0) {
      std::this_thread::sleep_for(std::chrono::microseconds(100));
      continue;
    }

    if (silent) {
      // Standing in for a radio that is powered on but transmitting nothing,
      // which is what an unlaunched UE actually contributes to a cell.
      std::fill(samples.begin(), samples.end(), ocg::IqSample{});
    } else {
      for (std::size_t i = 0; i != samples.size(); ++i) {
        const float phase = static_cast<float>((seq + i) % 4096) / 4096.0F * 6.283185307F;
        samples[i] = {std::cos(phase), std::sin(phase)};
      }
    }
    seq += samples.size();

    if (batch_period != std::chrono::steady_clock::duration::zero()) {
      const auto now = std::chrono::steady_clock::now();
      if (next_reply_at < now - batch_period) {
        next_reply_at = now; // fell far behind; rebase rather than sprint to catch up
      }
      if (next_reply_at > now) {
        std::this_thread::sleep_for(next_reply_at - now);
      }
      next_reply_at += batch_period;
    }

    // A request was accepted; the REP socket must answer it. Retry a timed-out
    // send so the socket never gets stuck expecting a reply mid-transaction.
    const auto reply_bytes = samples.size() * sizeof(ocg::IqSample);
    while (running() && zmq_send(socket, samples.data(), reply_bytes, 0) < 0) {
      std::this_thread::sleep_for(std::chrono::microseconds(100));
    }
  }

  zmq_close(socket);
  zmq_ctx_shutdown(context);
  zmq_ctx_destroy(context);
  std::cout << "event=stop samples_sent=" << seq << "\n";
  return 0;
}
