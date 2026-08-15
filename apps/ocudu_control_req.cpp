// Minimal control-plane client: sends one JSON REQ to a running broker's
// --control-endpoint and prints the reply.
//
// It exists because until M4.6 there was no way to exercise the control plane
// outside a unit test -- every gate called the handler directly, so the wire
// path itself (REQ in, shadow written, snap at the slot boundary, output
// changed) had never been run end to end. A validation script needs a client,
// and so does anyone operating the emulator.
#include <zmq.h>
#include <cstdio>
#include <cstring>
#include <string>

namespace {

void usage()
{
  std::fprintf(stderr,
               "usage: ocudu-control-req --endpoint tcp://127.0.0.1:5559 --message '<json>'\n"
               "  Sends one control REQ and prints the reply. Exit 0 on a reply that\n"
               "  reports ok, 1 otherwise, so a script can gate on it.\n");
}

} // namespace

int main(int argc, char** argv)
{
  std::string endpoint;
  std::string message;
  int timeout_ms = 3000;
  for (int i = 1; i < argc; ++i) {
    const std::string arg = argv[i];
    if (arg == "--endpoint" && i + 1 < argc) {
      endpoint = argv[++i];
    } else if (arg == "--message" && i + 1 < argc) {
      message = argv[++i];
    } else if (arg == "--timeout-ms" && i + 1 < argc) {
      timeout_ms = std::atoi(argv[++i]);
    } else {
      usage();
      return 2;
    }
  }
  if (endpoint.empty() || message.empty()) {
    usage();
    return 2;
  }

  void* ctx = zmq_ctx_new();
  void* sock = zmq_socket(ctx, ZMQ_REQ);
  zmq_setsockopt(sock, ZMQ_RCVTIMEO, &timeout_ms, sizeof(timeout_ms));
  zmq_setsockopt(sock, ZMQ_SNDTIMEO, &timeout_ms, sizeof(timeout_ms));
  // Without this a pending reply keeps the process alive on exit.
  int linger = 0;
  zmq_setsockopt(sock, ZMQ_LINGER, &linger, sizeof(linger));

  int status = 1;
  if (zmq_connect(sock, endpoint.c_str()) != 0) {
    std::fprintf(stderr, "event=fatal error=\"connect %s: %s\"\n", endpoint.c_str(),
                 zmq_strerror(zmq_errno()));
  } else if (zmq_send(sock, message.data(), message.size(), 0) < 0) {
    std::fprintf(stderr, "event=fatal error=\"send: %s\"\n", zmq_strerror(zmq_errno()));
  } else {
    char buffer[8192];
    const int n = zmq_recv(sock, buffer, sizeof(buffer) - 1, 0);
    if (n < 0) {
      std::fprintf(stderr, "event=fatal error=\"recv: %s\"\n", zmq_strerror(zmq_errno()));
    } else {
      buffer[n] = '\0';
      std::printf("%s\n", buffer);
      status = std::strstr(buffer, "\"ok\":true") != nullptr ? 0 : 1;
    }
  }
  zmq_close(sock);
  zmq_ctx_term(ctx);
  return status;
}
