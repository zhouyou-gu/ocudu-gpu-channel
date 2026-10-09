#include "ocudu_gpu_channel/config.h"
#include "ocudu_gpu_channel/mutable_params.h"
#include "ocudu_gpu_channel/runtime_control.h"

#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <iostream>

namespace {

void require(bool condition, const char* message)
{
  if (!condition) {
    std::cerr << "FAIL: " << message << "\n";
    std::exit(1);
  }
}

bool nearly(float a, float b, float tol = 1e-6F)
{
  return std::fabs(a - b) <= tol;
}

ocg::ModelStep make_path_loss_step(double db)
{
  ocg::ModelStep s;
  s.type = ocg::ModelStepType::PathLoss;
  s.params["path_loss_db"] = db;
  return s;
}

ocg::ModelStep make_cfo_step(double hz)
{
  ocg::ModelStep s;
  s.type = ocg::ModelStepType::Cfo;
  s.params["cfo_hz"] = hz;
  return s;
}

ocg::ModelStep make_awgn_step(double snr_db)
{
  ocg::ModelStep s;
  s.type = ocg::ModelStepType::Awgn;
  s.params["snr_db"] = snr_db;
  return s;
}

ocg::ModelStep make_tdl_step(double delay_samples, double gain_db, double phase_rad, double los_k_db)
{
  ocg::ModelStep s;
  s.type = ocg::ModelStepType::Tdl;
  ocg::TapSpec tap;
  tap.delay_samples = delay_samples;
  tap.gain_db = gain_db;
  tap.phase_rad = phase_rad;
  tap.los_k_db = los_k_db;
  s.taps.push_back(tap);
  return s;
}

}  // namespace

int main()
{
  // ── Case 1: full chain — every v1 param present ──────────────────────────
  {
    ocg::ModelConfig m;
    m.id = "full";
    m.chain.push_back(make_tdl_step(/*delay=*/5.5, /*gain_db=*/-3.0,
                                    /*phase_rad=*/0.25, /*los_k_db=*/9.0));
    m.chain.push_back(make_path_loss_step(/*db=*/12.0));
    m.chain.push_back(make_cfo_step(/*hz=*/250.0));
    m.chain.push_back(make_awgn_step(/*snr_db=*/20.0));

    const ocg::MutableParams live =
        ocg::populate_mutable_params_from_yaml(m, /*reference_power=*/1.0,
                                               /*sample_rate_hz=*/23040000);

    require(nearly(live.path_loss_db, 12.0F), "path_loss_db should be 12");
    require(nearly(live.cfo_hz, 250.0F), "cfo_hz should be 250");
    require(nearly(live.tap0_delay_samples, 5.5F),
            "tap0_delay_samples should round-trip the YAML fractional value");
    require(nearly(live.tap0_gain_db, -3.0F), "tap0_gain_db should be -3");
    require(nearly(live.tap0_phase_rad, 0.25F), "tap0_phase_rad should be 0.25");
    require(nearly(live.los_k_db, 9.0F), "los_k_db should be 9");
    // v1-fin-A: AWGN now stored as raw SNR-dB knob; backends derive sigma
    // per-slot from current input power at execute time.
    require(nearly(live.awgn_snr_db, 20.0F), "awgn_snr_db should mirror YAML snr_db");
  }

  // ── Case 2: empty chain — every field defaults to zero ──────────────────
  {
    ocg::ModelConfig m;
    m.id = "empty";
    const ocg::MutableParams live =
        ocg::populate_mutable_params_from_yaml(m, /*reference_power=*/0.0,
                                               /*sample_rate_hz=*/0);
    require(live.path_loss_db == 0.0F, "empty chain → path_loss_db zero");
    require(live.cfo_hz == 0.0F, "empty chain → cfo_hz zero");
    // Empty chain → default snr_db remains the struct default (60 dB,
    // matches the YAML default in populate_mutable_params_from_yaml).
    require(live.awgn_snr_db == 60.0F, "empty chain → awgn_snr_db default 60");
    require(live.tap0_delay_samples == 0.0F, "empty chain → tap0_delay_samples zero");
    require(live.tap0_gain_db == 0.0F, "empty chain → tap0_gain_db zero");
    require(live.tap0_phase_rad == 0.0F, "empty chain → tap0_phase_rad zero");
    require(live.los_k_db == 0.0F, "empty chain → los_k_db zero");
  }

  // ── Case 3: leading non-tdl step → tap0 params stay at default ──────────
  {
    ocg::ModelConfig m;
    m.id = "no-leading-tdl";
    m.chain.push_back(make_path_loss_step(/*db=*/7.0));
    m.chain.push_back(make_tdl_step(/*delay=*/2.0, /*gain_db=*/1.0,
                                    /*phase_rad=*/0.1, /*los_k_db=*/3.0));
    const ocg::MutableParams live =
        ocg::populate_mutable_params_from_yaml(m, /*reference_power=*/0.0,
                                               /*sample_rate_hz=*/0);
    require(nearly(live.path_loss_db, 7.0F), "non-leading-tdl chain still picks up path_loss");
    require(live.tap0_delay_samples == 0.0F, "tap params untouched when tdl is not leading");
    require(live.tap0_gain_db == 0.0F, "tap params untouched when tdl is not leading");
    require(live.los_k_db == 0.0F, "los params untouched when tdl is not leading");
  }

  // ── Case 4: POD size contract ───────────────────────────────────────────
  {
    static_assert(sizeof(ocg::MutableParams) == 32,
                  "MutableParams size locked at 32 bytes for the control-plane "
                  "binary copy contract; field changes must update the assert.");
  }

  // ── Case 5: shadow → live snap mechanism (C2b) ──────────────────────────
  // Models one server-thread serve loop: snap reads shadow, copies into
  // live, advances live_seqno. Subsequent snaps with no shadow change are
  // no-ops.
  {
    ocg::MutableParams live;
    live.path_loss_db = 10.0F;
    live.cfo_hz = 100.0F;
    std::uint32_t live_seqno = 0;

    ocg::BrokerLinkControl ctl;
    ocg::init_broker_link_control(ctl, live);

    // First snap on the initial seqno=0 is a no-op (live already == shadow).
    require(!ocg::snap_mutable_params(live, live_seqno, ctl),
            "first snap with seqno=0 should be a no-op");
    require(nearly(live.path_loss_db, 10.0F), "live untouched on no-op snap");

    // Simulate a control-plane write: update shadow + bump seqno.
    ctl.shadow.path_loss_db = -7.5F;
    ctl.shadow.cfo_hz = 250.0F;
    ctl.seqno.store(1, std::memory_order_release);

    // Second snap picks up the change.
    require(ocg::snap_mutable_params(live, live_seqno, ctl),
            "snap with advanced seqno should report true");
    require(nearly(live.path_loss_db, -7.5F), "live picks up new path_loss");
    require(nearly(live.cfo_hz, 250.0F), "live picks up new cfo_hz");
    require(live_seqno == 1, "live_seqno advanced to 1");

    // Third snap with no further shadow change is a no-op again.
    require(!ocg::snap_mutable_params(live, live_seqno, ctl),
            "third snap without seqno bump should be a no-op");
    require(nearly(live.path_loss_db, -7.5F), "live held at last snapped value");

    // Two writes between snaps: server should see the final value, not the
    // intermediate one.
    ctl.shadow.path_loss_db = -3.0F;
    ctl.seqno.store(2, std::memory_order_release);
    ctl.shadow.path_loss_db = -1.5F;
    ctl.seqno.store(3, std::memory_order_release);
    require(ocg::snap_mutable_params(live, live_seqno, ctl),
            "snap should pick up batched writes");
    require(nearly(live.path_loss_db, -1.5F), "live equals final shadow value");
    require(live_seqno == 3, "live_seqno tracks the observed seqno");
  }

  std::cout << "test_mutable_params OK\n";
  return 0;
}
