#!/usr/bin/env bash
# Live rank-1 gate: 4x1 DL MISO + 1x4 UL SIMO.
#
# A real OCUDU 4T4R gNB and a real srsUE (nof_antennas = 1) attach through the
# CUDA broker, complete a PDU session and pass user-plane traffic, while the
# same run captures the wire and scores y = Hx against the declared topology.
#
# Runs on the containerised harness in scripts/remote/, which is the supported
# path: it provisions its own Docker network and 5GC, so it does not need the
# hand-built native workspace that scripts/native/ expects.
#
# Claim boundary. The UL column is genuinely multi-branch: each of the gNB's
# four receive ports takes its own coefficient and is scored on its own row.
# The DL row is NOT. srsRAN radiates SSB and common channels on port 0 only,
# and with CSI-RS disabled it precodes rank-1 PDSCH as [1, 0, 0, 0], so gNB TX ports 1, 2 and 3
# carries no signal in this configuration. That is declared below with
# --allow-silent-source: it is a recorded measurement of the RAN stack's
# behaviour, not a relaxation of the check. The live DL row therefore proves
# y = h0*x0 and that the silent port contributes exactly zero -- it does not
# demonstrate live multi-branch downlink combining. That evidence comes from
# the synthetic gates and the oracle-precoding experiment.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

OCUDU_ATTACH_GATE_NAME="${OCUDU_ATTACH_GATE_NAME:-rank1-4x1}" \
OCUDU_ATTACH_GNB_CONFIG="${OCUDU_ATTACH_GNB_CONFIG:-examples/ocudu/gnb_zmq_b210_fdd_4t4r_rank1_srsue.yaml}" \
OCUDU_ATTACH_TOPOLOGY="${OCUDU_ATTACH_TOPOLOGY:-examples/topology.ocudu-docker.rank1-4x1.cuda.yaml}" \
OCUDU_ATTACH_GNB_TX_PORTS="${OCUDU_ATTACH_GNB_TX_PORTS:-2000,2002,2004,2006}" \
OCUDU_ATTACH_MATRIX="${OCUDU_ATTACH_MATRIX:-1}" \
OCUDU_ATTACH_MATRIX_ALLOW_SILENT="${OCUDU_ATTACH_MATRIX_ALLOW_SILENT:-gnb0->ue0:1,gnb0->ue0:2,gnb0->ue0:3}" \
OCUDU_ATTACH_PING_COUNT="${OCUDU_ATTACH_PING_COUNT:-250}" \
OCUDU_ATTACH_DURATION_SECONDS="${OCUDU_ATTACH_DURATION_SECONDS:-60}" \
  exec bash "${script_dir}/ocudu-attach-smoke.sh" "$@"
