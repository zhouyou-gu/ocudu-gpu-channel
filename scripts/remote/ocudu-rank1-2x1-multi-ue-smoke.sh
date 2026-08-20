#!/usr/bin/env bash
# Live rank-1 gate with TWO srsUEs on one 2T2R cell.
#
# This is the multi-user counterpart of ocudu-rank1-2x1-smoke.sh. The gNB keeps
# two antennas; each srsUE keeps one (nof_antennas = 1). Per user the downlink
# is a 1x2 row and the uplink a 2x1 column, so every claim stays rank-1
# MISO/SIMO -- nothing here is 2x2.
#
# What it adds over the single-UE gate is superposition at a MULTI-PORT
# receiver. Both uplinks arrive on the same two gNB receive ports, so each gNB
# RX row is the sum of both users' columns. The matrix checker reconstructs
# every row from all incoming links and then requires that removing either user
# breaks the match, so a relay that quietly served only one of them cannot pass.
#
# Claim boundary: two single-layer users sharing a cell in time. This is NOT
# same-PRB MU-MIMO -- the emulator superposes whatever the scheduler actually
# transmits, and does not make the gNB serve both users on the same resources.
#
# Note on the downlink: as in every rank-1 gate here, srsRAN radiates SSB and
# common channels on port 0 only and precodes rank-1 PDSCH as [1, 0], so the
# live downlink rows are single-branch. The multi-branch downlink evidence comes
# from the synthetic gates and the oracle-precoding experiment.
# Two UEs attach later than one: ue1 is deliberately staggered behind ue0's RRC
# connection so they RACH on different occasions and get distinct C-RNTIs. The
# capture window is therefore placed well after both are up and the background
# uplink traffic is flowing -- 1,036,800,000 samples is 45 s at 23.04 MS/s --
# and records 200 ms of it.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

OCUDU_MUE_GATE_NAME="${OCUDU_MUE_GATE_NAME:-rank1-2x1-multi-ue}" \
OCUDU_MUE_GNB_CONFIG="${OCUDU_MUE_GNB_CONFIG:-examples/ocudu/gnb_zmq_b210_fdd_2t2r_rank1_srsue.yaml}" \
OCUDU_MUE_TOPOLOGY="${OCUDU_MUE_TOPOLOGY:-examples/topology.ocudu-docker.rank1-2x1-multi-ue.cuda.yaml}" \
OCUDU_MUE_GNB_TX_PORTS="${OCUDU_MUE_GNB_TX_PORTS:-2000,2002}" \
OCUDU_MUE_MATRIX="${OCUDU_MUE_MATRIX:-1}" \
OCUDU_MUE_MATRIX_ARGS="${OCUDU_MUE_MATRIX_ARGS:---allow-silent-source gnb0->ue0:1 --allow-silent-source gnb0->ue1:1}" \
OCUDU_MUE_PING_COUNT="${OCUDU_MUE_PING_COUNT:-30}" \
OCUDU_MUE_DURATION_SECONDS="${OCUDU_MUE_DURATION_SECONDS:-95}" \
OCUDU_MUE_CAPTURE_SKIP="${OCUDU_MUE_CAPTURE_SKIP:-1036800000}" \
OCUDU_MUE_CAPTURE_SAMPLES="${OCUDU_MUE_CAPTURE_SAMPLES:-4608000}" \
  exec bash "${script_dir}/ocudu-multi-ue-smoke.sh" "$@"
