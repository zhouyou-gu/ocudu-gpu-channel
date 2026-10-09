// usrp_zmq_bridge: puts a live gNB that is reached over RF through a USRP (e.g. an R&S CMX500) into the
// ocudu-gpu-channel broker as a ZMQ "gnb" device, so the channel emulator sits in the IQ path between the real gNB
// and a ZMQ srsUE.
//
//   DL: the USRP receives the gNB continuously; samples are indexed by USRP time and served on a ZMQ REP socket that
//       the broker pulls as the gNB TX stream (1-byte request, cf32 reply).
//   UL: samples are requested from the broker's gNB RX endpoint (ZMQ REQ). UL sample u belongs to the same instant as
//       DL sample (dl_base + u), so it is transmitted at that USRP time minus the TX path advance. Blocks that would
//       be late are dropped and counted instead of being sent late.
//
// The UE stream starts at the first DL sample the broker pulls, so the bridge has to be running before the broker.

#include <uhd/types/tune_request.hpp>
#include <uhd/usrp/multi_usrp.hpp>
#include <zmq.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <complex>
#include <csignal>
#include <cstdio>
#include <cstring>
#include <iostream>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

using cf32 = std::complex<float>;

namespace {

std::atomic<bool> g_stop{false};
void on_signal(int) { g_stop = true; }

struct args_t {
  std::string usrp_args   = "type=x300,addr=192.168.10.2,master_clock_rate=184.32e6";
  std::string subdev      = "A:0";
  double      rate        = 11.52e6;
  double      rx_freq     = 1842.5e6;
  double      tx_freq     = 1747.5e6;
  double      freq_offset = 0.0; // added to both, compensates the USRP reference error
  double      rx_gain     = 30.0;
  double      tx_gain     = 10.0;
  std::string dl_endpoint = "tcp://*:2000";          // REP, pulled by the broker (gNB TX)
  std::string ul_endpoint = "tcp://127.0.0.1:2001";  // REQ, served by the broker (gNB RX)
  size_t      batch       = 11520;                   // samples per DL reply (1 ms at 11.52 MS/s)
  long        tx_adv      = 120;                     // TX path advance in samples (X310 at 11.52 MS/s)
  long        ul_offset   = 0;                       // extra UL alignment correction in samples (+ = later)
  double      min_lead_us = 200.0;                   // UL blocks with less lead than this are dropped
  double      max_backlog_ms = 2.0;                  // DL backlog above this is skipped to bound the latency
  double      jump_align_ms  = 0.0;                  // skip whole multiples of this (10 = radio frame), 0 = any amount
  bool        no_tx       = false;                   // DL only, never transmit
  bool        dry_tx      = false;                   // run the UL path and its timing, but never transmit
  std::string timing_dir;                            // per-event timing CSVs for offline analysis
};

void usage()
{
  std::cout << "usrp_zmq_bridge [--usrp-args A] [--subdev A:0] [--rate 11.52e6] [--rx-freq F] [--tx-freq F]\n"
               "                [--freq-offset Hz] [--rx-gain dB] [--tx-gain dB] [--dl-endpoint tcp://*:2000]\n"
               "                [--ul-endpoint tcp://127.0.0.1:2001] [--batch 11520] [--tx-adv 120] [--ul-offset 0]\n"
               "                [--min-lead-us 200] [--max-backlog-ms 2] [--jump-align-ms 0] [--no-tx] [--dry-tx]\n"
               "                [--timing-dir DIR]\n";
}

bool parse(int argc, char** argv, args_t& a)
{
  for (int i = 1; i < argc; ++i) {
    std::string k = argv[i];
    auto        v = [&]() -> std::string {
      if (i + 1 >= argc) {
        throw std::runtime_error("missing value for " + k);
      }
      return argv[++i];
    };
    if (k == "--usrp-args") a.usrp_args = v();
    else if (k == "--subdev") a.subdev = v();
    else if (k == "--rate") a.rate = std::stod(v());
    else if (k == "--rx-freq") a.rx_freq = std::stod(v());
    else if (k == "--tx-freq") a.tx_freq = std::stod(v());
    else if (k == "--freq-offset") a.freq_offset = std::stod(v());
    else if (k == "--rx-gain") a.rx_gain = std::stod(v());
    else if (k == "--tx-gain") a.tx_gain = std::stod(v());
    else if (k == "--dl-endpoint") a.dl_endpoint = v();
    else if (k == "--ul-endpoint") a.ul_endpoint = v();
    else if (k == "--batch") a.batch = std::stoul(v());
    else if (k == "--tx-adv") a.tx_adv = std::stol(v());
    else if (k == "--ul-offset") a.ul_offset = std::stol(v());
    else if (k == "--min-lead-us") a.min_lead_us = std::stod(v());
    else if (k == "--max-backlog-ms") a.max_backlog_ms = std::stod(v());
    else if (k == "--jump-align-ms") a.jump_align_ms = std::stod(v());
    else if (k == "--no-tx") a.no_tx = true;
    else if (k == "--dry-tx") a.dry_tx = true;
    else if (k == "--timing-dir") a.timing_dir = v();
    else if (k == "-h" || k == "--help") return false;
    else throw std::runtime_error("unknown option " + k);
  }
  return true;
}

// Single-producer / single-consumer sample ring indexed by absolute sample number.
class sample_ring
{
public:
  explicit sample_ring(size_t size) : buf(size) {}
  size_t size() const { return buf.size(); }
  void   write(uint64_t idx, const cf32* in, size_t n)
  {
    for (size_t i = 0; i < n; ++i) {
      buf[(idx + i) % buf.size()] = in[i];
    }
  }
  void zero(uint64_t idx, size_t n)
  {
    for (size_t i = 0; i < n; ++i) {
      buf[(idx + i) % buf.size()] = {};
    }
  }
  void read(uint64_t idx, cf32* out, size_t n) const
  {
    for (size_t i = 0; i < n; ++i) {
      out[i] = buf[(idx + i) % buf.size()];
    }
  }

private:
  std::vector<cf32> buf;
};

struct stats_t {
  std::atomic<uint64_t> rx_overflows{0}, rx_gap_samples{0}, rx_errors{0};
  std::atomic<uint64_t> dl_replies{0}, dl_jumps{0};
  std::atomic<int64_t>  dl_backlog_max{0};
  std::atomic<uint64_t> ul_blocks{0}, ul_samples{0}, ul_late_drops{0}, ul_trimmed{0}, ul_sent{0}, ul_timeouts{0};
  std::atomic<int64_t>  ul_lead_min_us{1 << 30}, ul_lead_max_us{-(1 << 30)};
  // Same, only for blocks that carry a UE transmission (the rest is the zero padding srsRAN writes up to its RX time)
  std::atomic<uint64_t> ul_data_blocks{0}, ul_data_late{0};
  // Signal levels (mean power over the second, peak magnitude), full scale = 1.0
  std::atomic<double>   dl_pwr_acc{0.0}, ul_data_pwr_acc{0.0}, ul_data_peak{0.0};
  std::atomic<uint64_t> dl_pwr_n{0}, ul_data_pwr_n{0};
  std::atomic<int64_t>  ul_data_lead_min_us{1 << 30}, ul_data_lead_max_us{-(1 << 30)};
  std::atomic<uint64_t> tx_underflows{0}, tx_late{0}, tx_seq_err{0}, tx_other{0};
};

void set_int(void* s, int opt, int v) { zmq_setsockopt(s, opt, &v, sizeof(v)); }

int64_t wall_us()
{
  return std::chrono::duration_cast<std::chrono::microseconds>(std::chrono::system_clock::now().time_since_epoch())
      .count();
}

// One timing CSV, written by a single thread (--timing-dir)
struct csv_t {
  FILE* f = nullptr;
  void  open(const std::string& dir, const char* name, const char* header)
  {
    if (dir.empty()) {
      return;
    }
    f = std::fopen((dir + "/" + name).c_str(), "w");
    if (f != nullptr) {
      std::setvbuf(f, nullptr, _IOFBF, 1 << 20);
      std::fputs(header, f);
    }
  }
  ~csv_t()
  {
    if (f != nullptr) {
      std::fclose(f);
    }
  }
};

} // namespace

int main(int argc, char** argv)
{
  args_t a;
  try {
    if (!parse(argc, argv, a)) {
      usage();
      return 0;
    }
  } catch (const std::exception& e) {
    std::cerr << e.what() << "\n";
    usage();
    return 1;
  }
  std::signal(SIGINT, on_signal);
  std::signal(SIGTERM, on_signal);

  csv_t ul_csv, dl_csv, ev_csv, lag_csv;
  ul_csv.open(a.timing_dir, "ul_blocks.csv",
              "wall_us,ul_idx,n,has_data,lead_us,ahead_us,dl_backlog_us,cut,sent,send_us\n");
  dl_csv.open(a.timing_dir, "dl_requests.csv", "wall_us,stream_pos,backlog_us,jumped\n");
  ev_csv.open(a.timing_dir, "tx_events.csv", "wall_us,code,device_time\n");
  lag_csv.open(a.timing_dir, "rx_lag.csv", "wall_us,lag_min_us,lag_max_us\n");

  // ---- USRP ----
  auto usrp = uhd::usrp::multi_usrp::make(a.usrp_args);
  usrp->set_rx_subdev_spec(uhd::usrp::subdev_spec_t(a.subdev));
  usrp->set_tx_subdev_spec(uhd::usrp::subdev_spec_t(a.subdev));
  usrp->set_clock_source("internal");
  usrp->set_rx_rate(a.rate);
  usrp->set_tx_rate(a.rate);
  usrp->set_rx_freq(uhd::tune_request_t(a.rx_freq + a.freq_offset));
  usrp->set_tx_freq(uhd::tune_request_t(a.tx_freq + a.freq_offset));
  usrp->set_rx_gain(a.rx_gain);
  usrp->set_tx_gain(a.tx_gain);
  usrp->set_rx_antenna("RX2");
  usrp->set_tx_antenna("TX/RX");
  for (int i = 0; i < 100 && !usrp->get_rx_sensor("lo_locked").to_bool(); ++i) {
    std::this_thread::sleep_for(std::chrono::milliseconds(10));
  }
  const double fs = usrp->get_rx_rate();
  std::printf("bridge: rate=%.3f MS/s rx=%.6f MHz tx=%.6f MHz rx_gain=%.1f tx_gain=%.1f rx_lo_locked=%d tx=%s\n",
              fs / 1e6, usrp->get_rx_freq() / 1e6, usrp->get_tx_freq() / 1e6, usrp->get_rx_gain(), usrp->get_tx_gain(),
              (int)usrp->get_rx_sensor("lo_locked").to_bool(), a.no_tx ? "off" : (a.dry_tx ? "dry (not transmitting)" : "on"));
  usrp->set_time_now(uhd::time_spec_t(0.0));

  uhd::stream_args_t sargs("fc32", "sc16");
  auto rx_stream = usrp->get_rx_stream(sargs);
  auto tx_stream = usrp->get_tx_stream(sargs);

  sample_ring           ring((size_t)(fs * 0.5)); // 500 ms of history
  std::atomic<uint64_t> rx_head{0};               // samples written so far
  std::atomic<double>   rx_t0{-1.0};               // USRP time of sample 0
  // USRP time minus the time of the newest received sample. Querying the device clock is a network round trip, too
  // slow for every UL block, so the UL path reads the time off the RX stream and the status loop recalibrates this.
  std::atomic<double>   rx_lag{0.0};
  std::atomic<int64_t>  dl_base{-1};               // absolute index of the first DL sample served
  std::atomic<uint64_t> dl_stream_pos{0};          // DL samples served so far
  std::atomic<int64_t>  dl_backlog_now{0};         // DL backlog at the last request
  // DL stream index -> absolute sample index, one entry per (re)start of serving: (stream index, absolute - stream)
  std::mutex                                  seg_mutex;
  std::vector<std::pair<uint64_t, int64_t>>   segs;
  stats_t               st;

  // ---- RX thread ----
  std::thread rx_thread([&] {
    uhd::stream_cmd_t cmd(uhd::stream_cmd_t::STREAM_MODE_START_CONTINUOUS);
    cmd.stream_now = false;
    cmd.time_spec  = uhd::time_spec_t(0.3);
    rx_stream->issue_stream_cmd(cmd);
    std::vector<cf32>  pkt(rx_stream->get_max_num_samps() * 4);
    uhd::rx_metadata_t md;
    while (!g_stop) {
      size_t n = rx_stream->recv(pkt.data(), pkt.size(), md, 0.5);
      if (md.error_code == uhd::rx_metadata_t::ERROR_CODE_TIMEOUT) {
        continue;
      }
      if (md.error_code == uhd::rx_metadata_t::ERROR_CODE_OVERFLOW) {
        st.rx_overflows++;
        continue;
      }
      if (md.error_code != uhd::rx_metadata_t::ERROR_CODE_NONE) {
        st.rx_errors++;
        continue;
      }
      if (n == 0 || !md.has_time_spec) {
        continue;
      }
      if (rx_t0 < 0) {
        rx_t0 = md.time_spec.get_real_secs();
      }
      // Index from the hardware timestamp so that overflows leave a zero gap instead of shifting the time base
      const int64_t idx  = (int64_t)std::llround((md.time_spec.get_real_secs() - rx_t0) * fs);
      const int64_t head = (int64_t)rx_head.load();
      if (idx > head) {
        st.rx_gap_samples += (uint64_t)(idx - head);
        ring.zero((uint64_t)head, (size_t)std::min<int64_t>(idx - head, (int64_t)ring.size()));
      }
      if (idx + (int64_t)n <= head) {
        continue; // duplicate
      }
      const int64_t start = std::max(idx, head);
      ring.write((uint64_t)start, pkt.data() + (start - idx), (size_t)(idx + (int64_t)n - start));
      rx_head = (uint64_t)(idx + (int64_t)n);
    }
    rx_stream->issue_stream_cmd(uhd::stream_cmd_t(uhd::stream_cmd_t::STREAM_MODE_STOP_CONTINUOUS));
  });

  void* zctx = zmq_ctx_new();

  // ---- DL server: broker pulls the gNB TX stream here ----
  std::thread dl_thread([&] {
    void* rep = zmq_socket(zctx, ZMQ_REP);
    set_int(rep, ZMQ_RCVTIMEO, 100);
    set_int(rep, ZMQ_LINGER, 0);
    if (zmq_bind(rep, a.dl_endpoint.c_str()) != 0) {
      std::fprintf(stderr, "bridge: bind %s failed: %s\n", a.dl_endpoint.c_str(), zmq_strerror(zmq_errno()));
      g_stop = true;
      return;
    }
    std::vector<cf32> out(a.batch);
    uint64_t          cursor     = 0;
    uint64_t          stream_pos = 0; // samples served so far
    while (!g_stop) {
      uint8_t req = 0;
      if (zmq_recv(rep, &req, 1, 0) < 0) {
        continue;
      }
      if (dl_base < 0) {
        // Start serving at the newest sample: no backlog, so the UE runs close to the air interface
        while (!g_stop && rx_head.load() < a.batch) {
          std::this_thread::sleep_for(std::chrono::microseconds(50));
        }
        cursor = rx_head.load();
        {
          std::lock_guard<std::mutex> lk(seg_mutex);
          segs.emplace_back(0, (int64_t)cursor);
        }
        dl_base = (int64_t)cursor;
        std::printf("bridge: first DL request, DL base index %lu (USRP time %.6f s)\n", (unsigned long)cursor,
                    rx_t0.load() + (double)cursor / fs);
      }
      while (!g_stop && rx_head.load() < cursor + a.batch) {
        std::this_thread::sleep_for(std::chrono::microseconds(20));
      }
      const int64_t backlog = (int64_t)rx_head.load() - (int64_t)cursor;
      const int64_t max_backlog = std::max<int64_t>((int64_t)(a.max_backlog_ms * 1e-3 * fs), 2 * (int64_t)a.batch);
      const int64_t align       = (int64_t)std::llround(a.jump_align_ms * 1e-3 * fs);
      // Keep the newest batch; with an alignment, skip whole frames only so the UE keeps its slot timing
      int64_t skip = backlog - (int64_t)a.batch;
      if (align > 0) {
        skip -= skip % align;
      }
      bool jumped = false;
      if (backlog > std::min<int64_t>(max_backlog, (int64_t)(ring.size() - 2 * a.batch)) && skip > 0) {
        // The consumer fell behind the air interface: skip ahead (bounded latency) and record the new mapping so
        // that the UL keeps the time alignment for the samples served from now on.
        st.dl_jumps++;
        jumped = true;
        cursor += (uint64_t)skip;
        {
          std::lock_guard<std::mutex> lk(seg_mutex);
          segs.emplace_back(stream_pos, (int64_t)cursor - (int64_t)stream_pos);
        }
        if (st.dl_jumps.load() <= 5) {
          std::printf("bridge: DL jump at stream index %lu (%.1f ms behind, skipped %.1f ms), time base re-anchored\n",
                      (unsigned long)stream_pos, (double)backlog * 1e3 / fs, (double)skip * 1e3 / fs);
        }
      }
      st.dl_backlog_max = std::max<int64_t>(st.dl_backlog_max.load(), backlog);
      dl_backlog_now    = backlog;
      if (dl_csv.f != nullptr) {
        std::fprintf(dl_csv.f, "%ld,%lu,%.1f,%d\n", (long)wall_us(), (unsigned long)stream_pos,
                     (double)backlog * 1e6 / fs, (int)jumped);
      }
      ring.read(cursor, out.data(), a.batch);
      double pwr = 0.0;
      for (const cf32& s : out) {
        pwr += std::norm(s);
      }
      st.dl_pwr_acc = st.dl_pwr_acc.load() + pwr;
      st.dl_pwr_n += a.batch;
      cursor += a.batch;
      stream_pos += a.batch;
      dl_stream_pos = stream_pos;
      zmq_send(rep, out.data(), a.batch * sizeof(cf32), 0);
      st.dl_replies++;
    }
    zmq_close(rep);
  });

  // ---- UL client: broker serves the gNB RX stream here, the USRP transmits it ----
  std::thread ul_thread([&] {
    if (a.no_tx) {
      return;
    }
    void* req = zmq_socket(zctx, ZMQ_REQ);
    // The timeout only lets the loop notice a stop. A request is never re-sent: the broker would still answer the
    // old one, the answer would be discarded and the UL stream index would fall behind the samples it carries.
    set_int(req, ZMQ_RCVTIMEO, 200);
    set_int(req, ZMQ_LINGER, 0);
    if (zmq_connect(req, a.ul_endpoint.c_str()) != 0) {
      std::fprintf(stderr, "bridge: connect %s failed: %s\n", a.ul_endpoint.c_str(), zmq_strerror(zmq_errno()));
      g_stop = true;
      return;
    }
    while (!g_stop && dl_base < 0) {
      std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
    std::vector<cf32> buf(1 << 20);
    uint64_t          ul_idx     = 0;
    bool              requested  = false;
    bool              burst_open = false;
    int64_t           burst_next = 0; // absolute sample index that would continue the open burst
    const auto        close_burst = [&] {
      if (burst_open && !a.dry_tx) {
        uhd::tx_metadata_t eob;
        eob.end_of_burst = true;
        tx_stream->send("", 0, eob);
      }
      burst_open = false;
    };
    while (!g_stop) {
      if (!requested) {
        uint8_t r = 0;
        if (zmq_send(req, &r, 1, 0) < 0) {
          continue;
        }
        requested = true;
      }
      int nbytes = zmq_recv(req, buf.data(), buf.size() * sizeof(cf32), 0);
      if (nbytes < 0) {
        st.ul_timeouts++;
        continue;
      }
      requested = false;
      if ((size_t)nbytes > buf.size() * sizeof(cf32)) {
        std::fprintf(stderr, "bridge: UL payload of %d B truncated, UL timing is lost\n", nbytes);
      }
      const size_t n = std::min((size_t)nbytes, buf.size() * sizeof(cf32)) / sizeof(cf32);
      if (n == 0) {
        continue;
      }
      st.ul_blocks++;
      st.ul_samples += n;
      int64_t delta = dl_base.load();
      {
        std::lock_guard<std::mutex> lk(seg_mutex);
        for (auto it = segs.rbegin(); it != segs.rend(); ++it) {
          if (it->first <= ul_idx) {
            delta = it->second;
            break;
          }
        }
      }
      const double t_tx =
          rx_t0.load() + ((double)ul_idx + (double)delta + (double)a.ul_offset - (double)a.tx_adv) / fs;
      ul_idx += n;
      const double  now     = rx_t0.load() + (double)rx_head.load() / fs + rx_lag.load();
      const int64_t lead_us = (int64_t)std::llround((t_tx - now) * 1e6);
      st.ul_lead_min_us     = std::min<int64_t>(st.ul_lead_min_us.load(), lead_us);
      st.ul_lead_max_us     = std::max<int64_t>(st.ul_lead_max_us.load(), lead_us);
      const bool has_data   = std::any_of(buf.begin(), buf.begin() + n, [](const cf32& s) { return s != cf32{}; });
      const auto log_ul     = [&](size_t cut, bool sent, double send_us) {
        if (ul_csv.f != nullptr) {
          std::fprintf(ul_csv.f, "%ld,%lu,%zu,%d,%ld,%.1f,%.1f,%zu,%d,%.1f\n", (long)wall_us(),
                       (unsigned long)(ul_idx - n), n, (int)has_data, (long)lead_us,
                       ((double)(ul_idx - n) - (double)dl_stream_pos.load()) * 1e6 / fs,
                       (double)dl_backlog_now.load() * 1e6 / fs, cut, (int)sent, send_us);
        }
      };
      if (has_data) {
        if (st.ul_data_blocks.load() < 40) {
          // UL index of the block vs the DL position being served right now (both in UE sample time)
          const auto wall = std::chrono::duration_cast<std::chrono::microseconds>(
                                std::chrono::system_clock::now().time_since_epoch())
                                .count();
          std::printf("bridge: ul data block ul_idx=%lu n=%zu dl_stream=%lu ahead=%.2f ms lead=%ld us dl_backlog=%.2f "
                      "ms wall=%02ld:%02ld.%06ld\n",
                      (unsigned long)(ul_idx - n), n, (unsigned long)dl_stream_pos.load(),
                      ((double)(ul_idx - n) - (double)dl_stream_pos.load()) * 1e3 / fs, (long)lead_us,
                      (double)dl_backlog_now.load() * 1e3 / fs, (long)(wall / 60000000 % 60),
                      (long)(wall / 1000000 % 60), (long)(wall % 1000000));
        }
        st.ul_data_blocks++;
        double pwr = 0.0, peak = 0.0;
        for (size_t i = 0; i < n; ++i) {
          const double p = std::norm(buf[i]);
          pwr += p;
          peak = std::max(peak, p);
        }
        st.ul_data_pwr_acc = st.ul_data_pwr_acc.load() + pwr;
        st.ul_data_pwr_n += n;
        st.ul_data_peak    = std::max(st.ul_data_peak.load(), std::sqrt(peak));
        st.ul_data_lead_min_us = std::min<int64_t>(st.ul_data_lead_min_us.load(), lead_us);
        st.ul_data_lead_max_us = std::max<int64_t>(st.ul_data_lead_max_us.load(), lead_us);
      }
      // Silence needs no transmission: only blocks that carry UE signal go to the radio
      if (!has_data) {
        if ((double)lead_us < a.min_lead_us) {
          st.ul_late_drops++;
        }
        close_burst();
        log_ul(0, false, 0.0);
        continue;
      }
      // A late head is cut off; the rest of the block still goes out at its own time
      size_t skip = 0;
      if ((double)lead_us < a.min_lead_us) {
        skip = std::min(n, (size_t)std::ceil((a.min_lead_us - (double)lead_us) * 1e-6 * fs));
        if (std::any_of(buf.begin(), buf.begin() + skip, [](const cf32& s) { return s != cf32{}; })) {
          st.ul_data_late++; // part of a UE transmission is lost
        }
        close_burst();
        if (skip == n) {
          st.ul_late_drops++;
          log_ul(n, false, 0.0);
          continue;
        }
        st.ul_trimmed++;
      }
      // One burst per contiguous run of blocks. The radio cannot start a new burst on the very sample where the
      // previous one ended (that start is reported late), and after a late packet or an underflow it drops
      // everything until an end of burst, so a run is closed as soon as the UE signal stops or a gap appears.
      const int64_t start = (int64_t)std::llround((double)(ul_idx - n) + (double)delta + (double)a.ul_offset -
                                                  (double)a.tx_adv) + (int64_t)skip;
      if (burst_open && start != burst_next) {
        close_burst();
      }
      uhd::tx_metadata_t md;
      md.start_of_burst = !burst_open;
      md.end_of_burst   = false;
      md.has_time_spec  = !burst_open;
      md.time_spec      = uhd::time_spec_t(t_tx + (double)skip / fs);
      burst_open        = true;
      burst_next        = start + (int64_t)(n - skip);
      if (!a.dry_tx) {
        const double t_est  = rx_t0.load() + (double)rx_head.load() / fs + rx_lag.load();
        const auto   t_wall = std::chrono::steady_clock::now();
        tx_stream->send(buf.data() + skip, n - skip, md, 0.1);
        log_ul(skip, true, std::chrono::duration<double, std::micro>(std::chrono::steady_clock::now() - t_wall).count());
        if (st.ul_sent.load() < 20) {
          std::printf("bridge: tx burst t=%.6f n=%zu est_now=%.6f lead=%.0fus send_took=%.0fus\n", md.time_spec.get_real_secs(),
                      n - skip, t_est, (md.time_spec.get_real_secs() - t_est) * 1e6,
                      std::chrono::duration<double, std::micro>(std::chrono::steady_clock::now() - t_wall).count());
        }
        st.ul_sent++;
      } else {
        log_ul(skip, false, 0.0);
      }
    }
    close_burst();
    zmq_close(req);
  });

  // ---- TX async events ----
  std::thread async_thread([&] {
    if (a.no_tx) {
      return;
    }
    uhd::async_metadata_t am;
    int                   async_logged = 0;
    while (!g_stop) {
      if (!tx_stream->recv_async_msg(am, 0.2)) {
        continue;
      }
      if (ev_csv.f != nullptr && am.event_code != uhd::async_metadata_t::EVENT_CODE_BURST_ACK) {
        std::fprintf(ev_csv.f, "%ld,%u,%.6f\n", (long)wall_us(), (unsigned)am.event_code,
                     am.has_time_spec ? am.time_spec.get_real_secs() : 0.0);
      }
      if (am.event_code != uhd::async_metadata_t::EVENT_CODE_BURST_ACK && async_logged < 20) {
        async_logged++;
        std::printf("bridge: tx event 0x%x has_time=%d t=%.6f\n", (unsigned)am.event_code, (int)am.has_time_spec,
                    am.has_time_spec ? am.time_spec.get_real_secs() : 0.0);
      }
      switch (am.event_code) {
        case uhd::async_metadata_t::EVENT_CODE_UNDERFLOW:
        case uhd::async_metadata_t::EVENT_CODE_UNDERFLOW_IN_PACKET:
          st.tx_underflows++;
          break;
        case uhd::async_metadata_t::EVENT_CODE_TIME_ERROR:
          st.tx_late++;
          break;
        case uhd::async_metadata_t::EVENT_CODE_SEQ_ERROR:
        case uhd::async_metadata_t::EVENT_CODE_SEQ_ERROR_IN_BURST:
          st.tx_seq_err++;
          break;
        case uhd::async_metadata_t::EVENT_CODE_BURST_ACK:
          break;
        default:
          st.tx_other++;
      }
    }
  });

  // ---- Clock calibration: device time vs newest RX sample, off the status path (the query can block) ----
  std::thread clock_thread([&] {
    while (!g_stop) {
      std::this_thread::sleep_for(std::chrono::seconds(1));
      if (rx_t0.load() >= 0) {
        // The time query shares the link with the TX samples and can only arrive late, so keep the smallest of a few
        double lag = 1e9, lag_max = -1e9;
        for (int k = 0; k < 5; ++k) {
          const uint64_t head  = rx_head.load();
          const double   t_now = usrp->get_time_now().get_real_secs();
          const double   l     = t_now - (rx_t0.load() + (double)head / fs);
          lag                  = std::min(lag, l);
          lag_max              = std::max(lag_max, l);
          std::this_thread::sleep_for(std::chrono::milliseconds(3));
        }
        rx_lag = lag;
        if (lag_csv.f != nullptr) {
          std::fprintf(lag_csv.f, "%ld,%.1f,%.1f\n", (long)wall_us(), lag * 1e6, lag_max * 1e6);
        }
      }
    }
  });

  // ---- Once-per-second status ----
  uint64_t last_rx = 0, last_dl = 0, last_ul = 0;
  while (!g_stop) {
    std::this_thread::sleep_for(std::chrono::seconds(1));
    const uint64_t rx = rx_head.load(), dl = st.dl_replies.load(), ul = st.ul_samples.load();
    const int64_t  lead = st.ul_lead_min_us.exchange(1 << 30), lead_max = st.ul_lead_max_us.exchange(-(1 << 30));
    std::printf("bridge: rx %.2f MS/s ovf=%lu gap=%lu lag=%.0fus | dl %lu rep/s backlog_max=%.2f ms jumps=%lu | ul %.2f "
                "MS/s lead_min=%s lead_max=%s late_drop=%lu trim=%lu sent=%lu timeouts=%lu | tx U=%lu L=%lu S=%lu\n",
                (double)(rx - last_rx) / 1e6, (unsigned long)st.rx_overflows.load(),
                (unsigned long)st.rx_gap_samples.load(), rx_lag.load() * 1e6, (unsigned long)(dl - last_dl),
                (double)st.dl_backlog_max.exchange(0) * 1e3 / fs, (unsigned long)st.dl_jumps.load(),
                (double)(ul - last_ul) / 1e6,
                lead == (1 << 30) ? "n/a" : (std::to_string(lead) + "us").c_str(),
                lead_max == -(1 << 30) ? "n/a" : (std::to_string(lead_max) + "us").c_str(),
                (unsigned long)st.ul_late_drops.load(), (unsigned long)st.ul_trimmed.load(),
                (unsigned long)st.ul_sent.load(), (unsigned long)st.ul_timeouts.load(),
                (unsigned long)st.tx_underflows.load(), (unsigned long)st.tx_late.load(),
                (unsigned long)st.tx_seq_err.load());
    const int64_t dmin = st.ul_data_lead_min_us.exchange(1 << 30), dmax = st.ul_data_lead_max_us.exchange(-(1 << 30));
    const auto dbfs = [](double acc, uint64_t n) { return n ? 10.0 * std::log10(acc / (double)n + 1e-30) : -999.0; };
    const double dl_dbfs = dbfs(st.dl_pwr_acc.exchange(0.0), st.dl_pwr_n.exchange(0));
    if (dmin != (1 << 30)) {
      const double ul_dbfs = dbfs(st.ul_data_pwr_acc.exchange(0.0), st.ul_data_pwr_n.exchange(0));
      std::printf("bridge: ul data blocks=%lu cut=%lu lead_min=%ldus lead_max=%ldus power=%.1f dBfs peak=%.3f | dl "
                  "power=%.1f dBfs\n",
                  (unsigned long)st.ul_data_blocks.load(), (unsigned long)st.ul_data_late.load(), (long)dmin, (long)dmax,
                  ul_dbfs, st.ul_data_peak.exchange(0.0), dl_dbfs);
    } else if (dl_dbfs > -999.0) {
      std::printf("bridge: dl power=%.1f dBfs\n", dl_dbfs);
    }
    std::fflush(stdout);
    last_rx = rx;
    last_dl = dl;
    last_ul = ul;
  }

  rx_thread.join();
  dl_thread.join();
  ul_thread.join();
  async_thread.join();
  clock_thread.join();
  zmq_ctx_term(zctx);
  std::printf("bridge: stopped\n");
  return 0;
}
