#!/bin/bash
# CMX500 -> USRP -> usrp_zmq_bridge -> GPU channel broker -> ZMQ srsUE (and back), inside the ocudu-quanlong container.
#
#   ./run_cmx_loop.sh                 # full loop, the USRP transmits the UE uplink to the CMX
#   DRY=1 ./run_cmx_loop.sh           # bridge does not transmit (UL timing is still measured)
#   TOPO=/path/to/topology.yaml DUR=60 ./run_cmx_loop.sh
#
# Env: DRY (0), DUR seconds (40), TOPO (topology.cmx-bridge.live.yaml), UL_OFFSET samples (0), TX_GAIN (10),
#      BATCH samples (5760), MAX_BACKLOG_MS (2): DL backlog in the bridge before it skips ahead,
#      JUMP_ALIGN_MS (10): skip whole radio frames only, so the UE keeps its slot timing,
#      CPUS_LINK (0-19; few shared cores delay the USRP RX) / CPUS_UE (5-9,15-19: all fast Cortex-X925 cores),
#      BROKER_ARGS: extra broker options (e.g. --control-endpoint tcp://127.0.0.1:5559),
#      PHY_LOG (info): srsUE PHY/MAC log level; warning keeps the UE fast, KPIs then come from metrics.csv,
#      UE_BUILD (/workspace/builds/srsran4g-cmx): srsUE build directory, e.g. /workspace/builds/srsran4g-minwoo-cmx,
#      TIMING (0): 1 = bridge writes per-event timing CSVs into the run directory
L=/workspace/cmx-loop
B=/workspace/ocudu-gpu-channel/build-sm121
DUR=${DUR:-40}
EX=${EX:-$(cd "$(dirname "$0")/../../use_cases/configs/topologies/cmx" && pwd)} # topologies live in use_cases/configs/topologies/cmx/
TOPO=${TOPO:-$EX/topology.cmx-bridge.live.yaml}
R=$L/runs/$(date +%Y%m%d-%H%M%S)$([ "${DRY:-0}" = 1 ] && echo -dry)
mkdir -p $R
cd $L

pgrep -f "usrp_zmq_bridge|ocudu-gpu-channel --config|srsue/src/srsue" >/dev/null && { echo "loop already running, abort"; exit 1; }
echo "[$(date +%T)] run -> $R (srsUE ${UE_BUILD:-/workspace/builds/srsran4g-cmx}, topology $TOPO, $([ "${DRY:-0}" = 1 ] && echo 'DRY: no transmission' || echo 'TRANSMITTING'))"

BR_ARGS="--batch ${BATCH:-5760} --freq-offset 2555 --rx-gain 30 --tx-gain ${TX_GAIN:-10} --ul-offset ${UL_OFFSET:-0} --max-backlog-ms ${MAX_BACKLOG_MS:-2} --jump-align-ms ${JUMP_ALIGN_MS:-10}"
[ "${DRY:-0}" = 1 ] && BR_ARGS="$BR_ARGS --dry-tx"
[ "${TIMING:-0}" = 1 ] && BR_ARGS="$BR_ARGS --timing-dir $R"
CPUS_LINK=${CPUS_LINK:-0-19}
CPUS_UE=${CPUS_UE:-5-9,15-19}
timeout --signal=INT --kill-after=10s $((DUR + 15))s taskset -c $CPUS_LINK /workspace/usrp-zmq-bridge/build/usrp_zmq_bridge \
  $BR_ARGS > $R/bridge.log 2>&1 &
sleep 4
# srsUE before the broker, so that the DL only starts flowing once every stage is up
date -u +%Y-%m-%dT%H:%M:%S.%6N > $R/t_srsue
timeout --signal=INT --kill-after=10s ${DUR}s taskset -c $CPUS_UE ${UE_BUILD:-/workspace/builds/srsran4g-cmx}/srsue/src/srsue \
  $L/ue-cmx-n3.zmq.conf --log.filename $R/srsue.log --log.all_level warning --log.phy_level ${PHY_LOG:-info} \
  --log.nas_level info --log.rrc_level info --log.mac_level ${PHY_LOG:-info} \
  --general.metrics_csv_enable true --general.metrics_period_secs 1 --general.metrics_csv_filename $R/metrics.csv \
  > $R/srsue-console.txt 2>&1 &
UE=$!
sleep 1
timeout --signal=INT --kill-after=10s $((DUR + 10))s taskset -c $CPUS_LINK $B/ocudu-gpu-channel --config $TOPO $BROKER_ARGS \
  --duration ${DUR}s > $R/broker.log 2>&1 &
wait $UE
sleep 3
pkill -INT -f "ocudu-gpu-channel --config" 2>/dev/null
pkill -INT -f usrp_zmq_bridge 2>/dev/null
wait

S=$R/srsue.log
echo "[$(date +%T)] ---- srsUE"
echo "    cell_found=$(grep -c 'Cell search found' $S) sib1=$(grep -c 'SIB1 received' $S) prach=$(grep -c 'PRACH: Transmitted' $S) rar=$(grep -c 'DL RAPID' $S) msg3=$(grep -c 'New TX for Msg3\|UL CCCH48' $S) contention_ok=$(grep -c 'Received Contention Resolution' $S) rrc_setup=$(grep -c 'Finished Connection Setup successfully' $S) reg_accept=$(grep -c 'Handling Registration Accept' $S) pdu_session=$(grep -c 'PDU Session Establishment successful' $S) pdu_add_failed=$(grep -c 'Adding PDU session failed' $S) pdsch_ok=$(grep -c 'PDSCH: cc=0.*CRC=OK' $S) pdsch_ko=$(grep -c 'PDSCH: cc=0.*CRC=KO' $S) pusch=$(grep -c 'PUSCH: cc=0' $S)"
echo "    sfn_mismatch=$(grep -c 'does not match current SFN' $S) slot_resync=$(grep -c 'resynchronised to' $S) out_of_sync=$(grep -c 'out-of-sync' $S)"
grep -oE "Cell search found .{0,60}|DL RAPID: [0-9]+, Temp C-RNTI: 0x[0-9a-f]+, TA: [0-9]+|PDU Session Establishment successful. IP: [0-9.]+" $S | awk '!seen[$0]++' | head -4 | sed 's/^/    /'
echo "[$(date +%T)] ---- bridge (last status lines)"
grep -E "^bridge: rx" $R/bridge.log | grep -vE "rx 0.00" | tail -3 | sed 's/^/    /'
grep -E "^bridge: (ul data blocks|DL jump)" $R/bridge.log | tail -3 | sed 's/^/    /'
echo "[$(date +%T)] ---- broker"
grep -E "event=stop|process_latency_summary|gpu_timings" $R/broker.log | tail -4 | sed 's/^/    /'
echo "$R"
