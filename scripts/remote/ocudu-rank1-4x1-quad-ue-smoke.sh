#!/usr/bin/env bash
# Live rank-1 gate with FOUR srsUEs on one 4T4R cell.
#
# The widest superposition case in the tree. The gNB keeps four antennas; each
# srsUE keeps one (nof_antennas = 1). Per user the downlink is a 1x4 row and the
# uplink a 4x1 column, so every claim stays rank-1 MISO/SIMO -- nothing here is
# 2x2 and nothing is same-PRB MU-MIMO.
#
# All four uplinks arrive on the same four gNB receive ports, so each of the four
# RX rows is the sum of four users' columns. The matrix checker reconstructs
# every row from all incoming links and then requires that removing any ONE user
# breaks the match, so a relay that quietly served a subset cannot pass.
#
# Downlink: as in every rank-1 gate here, srsRAN radiates SSB and common
# channels on port 0 only and precodes rank-1 PDSCH as [1, 0, 0, 0], so gNB TX
# ports 1..3 carry no signal. That is declared per UE with --allow-silent-source
# -- a recorded measurement of the RAN stack's behaviour, not a relaxed check.
#
# Unlike the 2T2R multi-UE gate this uses the plain 4T4R fixture, with no raised
# preamble_trans_max: each UE is given its own PRACH preamble index, so every one
# attaches on its FIRST attempt and the retry budget is never reached.
#
# Four UEs launch staggered behind each other's RRC connection, so the capture
# window is placed well after all of them are up and uplink traffic is flowing --
# 1,843,200,000 samples is 80 s at 23.04 MS/s -- and records 200 ms of it.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

OCUDU_MUE_GATE_NAME="${OCUDU_MUE_GATE_NAME:-rank1-4x1-quad-ue}" \
OCUDU_MUE_GNB_CONFIG="${OCUDU_MUE_GNB_CONFIG:-examples/ocudu/gnb_zmq_b210_fdd_4t4r_rank1_srsue.yaml}" \
OCUDU_MUE_TOPOLOGY="${OCUDU_MUE_TOPOLOGY:-examples/topology.ocudu-docker.rank1-4x1-quad-ue.cuda.yaml}" \
OCUDU_MUE_GNB_TX_PORTS="${OCUDU_MUE_GNB_TX_PORTS:-2000,2002,2004,2006}" \
OCUDU_MUE_MATRIX="${OCUDU_MUE_MATRIX:-1}" \
OCUDU_MUE_UE_COUNT="${OCUDU_MUE_UE_COUNT:-4}" \
OCUDU_MUE_MATRIX_ARGS="${OCUDU_MUE_MATRIX_ARGS:---allow-silent-source gnb0->ue0:1 --allow-silent-source gnb0->ue0:2 --allow-silent-source gnb0->ue0:3 --allow-silent-source gnb0->ue1:1 --allow-silent-source gnb0->ue1:2 --allow-silent-source gnb0->ue1:3 --allow-silent-source gnb0->ue2:1 --allow-silent-source gnb0->ue2:2 --allow-silent-source gnb0->ue2:3 --allow-silent-source gnb0->ue3:1 --allow-silent-source gnb0->ue3:2 --allow-silent-source gnb0->ue3:3}" \
OCUDU_MUE_PING_COUNT="${OCUDU_MUE_PING_COUNT:-30}" \
OCUDU_MUE_DURATION_SECONDS="${OCUDU_MUE_DURATION_SECONDS:-150}" \
OCUDU_MUE_CAPTURE_SKIP="${OCUDU_MUE_CAPTURE_SKIP:-1843200000}" \
OCUDU_MUE_CAPTURE_SAMPLES="${OCUDU_MUE_CAPTURE_SAMPLES:-4608000}" \
  exec bash "${script_dir}/ocudu-multi-ue-smoke.sh" "$@"
