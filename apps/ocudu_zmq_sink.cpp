#include "app_support.h"
#include "ocudu_gpu_channel/iq.h"
#include <chrono>
#include <cstring>
#include <iostream>
#include <string>
#include <thread>
#include <zmq.h>

namespace {

void usage()
{
  std::cout << "usage: ocudu-zmq-sink --endpoint tcp://127.0.0.1:2001 [--duration 60s] [--request-interval-us 1000]\n";
}

} // namespace

int main(int argc, char** argv)
{
  std::string endpoint = "tcp://127.0.0.1:2001";
  std::chrono::milliseconds duration = std::chrono::seconds(10);
  std::chrono::microseconds request_interval{0};

  for (int i = 1; i < argc; ++i) {
    std::string arg = argv[i];
    if (arg == "--help" || arg == "-h") {
      usage();
      return 0;
    }
    if (arg == "--endpoint" && i + 1 < argc) {
      endpoint = argv[++i];
    } else if (arg == "--duration" && i + 1 < argc) {
      duration = ocg::app::parse_duration(argv[++i]);
    } else if (arg == "--request-interval-us" && i + 1 < argc) {
      request_interval = std::chrono::microseconds(std::stoll(argv[++i]));
    } else {
      std::cerr << "unknown or incomplete argument: " << arg << "\n";
      usage();
      return 2;
    }
  }

  void* context = zmq_ctx_new();
  void* socket = zmq_socket(context, ZMQ_REQ);
  int timeout = 10;
  int linger = 0;
  zmq_setsockopt(socket, ZMQ_RCVTIMEO, &timeout, sizeof(timeout));
  zmq_setsockopt(socket, ZMQ_SNDTIMEO, &timeout, sizeof(timeout));
  zmq_setsockopt(socket, ZMQ_LINGER, &linger, sizeof(linger));
  if (zmq_connect(socket, endpoint.c_str()) != 0) {
    std::cerr << "event=fatal error=\"" << zmq_strerror(zmq_errno()) << "\"\n";
    return 1;
  }

  std::uint64_t requests = 0;
  std::uint64_t samples_received = 0;
  double power_sum = 0.0;
  const auto start = std::chrono::steady_clock::now();
  const auto end = start + duration;
  // Deadline-based pacing: the interval is measured request-to-request, not
  // added on top of per-request work, so the synthetic consumer actually
  // sustains the real-time request rate and drains the broker at line rate.
  auto deadline = start;
  bool awaiting_reply = false;
  while (std::chrono::steady_clock::now() < end) {
    // Keep exactly one request outstanding: only send a new request once the
    // previous reply has arrived. Sending again before the reply would push
    // the REQ socket into the EFSM error state if a peer is slow to start.
    if (!awaiting_reply) {
      std::uint8_t dummy = 0;
      if (zmq_send(socket, &dummy, sizeof(dummy), 0) < 0) {
        std::this_thread::sleep_for(std::chrono::microseconds(100));
        continue;
      }
      awaiting_reply = true;
    }

    zmq_msg_t msg;
    zmq_msg_init(&msg);
    const int nbytes = zmq_msg_recv(&msg, socket, 0);
    if (nbytes < 0) {
      zmq_msg_close(&msg);
      continue; // reply not ready yet; keep waiting on the same request
    }
    awaiting_reply = false;
    if (nbytes > 0 && static_cast<std::size_t>(nbytes) % sizeof(ocg::IqSample) == 0) {
      const auto* samples = static_cast<const ocg::IqSample*>(zmq_msg_data(&msg));
      const std::size_t count = static_cast<std::size_t>(nbytes) / sizeof(ocg::IqSample);
      for (std::size_t i = 0; i != count; ++i) {
        power_sum += ocg::power(samples[i]);
      }
      samples_received += count;
      ++requests;
    }
    zmq_msg_close(&msg);
    if (request_interval.count() > 0) {
      deadline += request_interval;
      std::this_thread::sleep_until(deadline);
    }
  }

  zmq_close(socket);
  zmq_ctx_shutdown(context);
  zmq_ctx_destroy(context);

  const double avg_power = samples_received == 0 ? 0.0 : power_sum / static_cast<double>(samples_received);
  std::cout << "event=stop requests=" << requests << " samples_received=" << samples_received
            << " avg_power=" << avg_power << "\n";
  return 0;
}
