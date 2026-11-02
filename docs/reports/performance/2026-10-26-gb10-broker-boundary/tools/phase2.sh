#!/bin/bash
# Phase 2: the real broker end to end with synthetic radios (ocudu-zmq-source blasting, broker pacing at the
# nominal sample rate, ocudu-zmq-sink requesting once per slot). Channel-only real-time ratio = samples the
# sinks received / (sample rate x sink window), with no gNB or UE in the loop.
# stdin lines: "label ues srate scs taps gnbp uep cells"; usage: phase2.sh <out.csv> <phase>
BD=${BD:-/workspace/ocudu-spark-ranb/builds/ocudu-gpu-channel-single-cell}; W=${W:-$(pwd)}; out=$1; phase=$2
DUR=${DUR:-20}; BIG=${BIG:-5-9,15-19}; LITTLE=${LITTLE:-0-4,10-14}
[ -f $out ] || echo "phase,label,ues,srate_hz,scs_khz,taps,gnb_ports,ue_ports,cells,ports,sink_ratio_min,sink_ratio_mean,sink_ratio_max,tx_pulls,rx_requests,rx_starvations,tx_queue_overflows,zmq_errors,broker_rc,broker_cpu_pct,wall_s" > $out
while read -r label ues srate scs taps gnbp uep cells; do
  [ -z "$label" ] && continue; case $label in \#*) continue;; esac
  y=$W/topo2-$label.yaml; python3 $W/gen.py --ues $ues --srate-hz $srate --scs-khz $scs --taps $taps --gnb-ports $gnbp --ue-ports $uep --cells $cells --base-port 20000 --out $y > $W/gen2-$label.log || { echo GEN_FAIL $label; continue; }
  nports=$(awk '/ports/{print $3}' $W/gen2-$label.log); batch=$(awk '{print $7}' $W/gen2-$label.log); slot_us=$(python3 -c "print(int(1000*15/$scs))")
  d=$W/p2-$label; rm -rf $d; mkdir -p $d
  echo "== $label start=$(date -u +%T) ports=$nports batch=$batch slot_us=$slot_us"
  pids=()
  for i in $(seq 0 $((nports-1))); do p=$((20000+2*i))
    taskset -c $LITTLE $BD/ocudu-zmq-source --endpoint tcp://*:$p --batch-samples $batch --duration $((DUR+6))s > $d/src-$i.log 2>&1 & pids+=($!); done
  sleep 1; t0=$(date +%s.%N)
  OCG_BROKER_SPIN=${OCG_BROKER_SPIN:-2} OCG_ZMQ_IO_THREADS=${OCG_ZMQ_IO_THREADS:-2} taskset -c $BIG $BD/ocudu-gpu-channel --config $y --duration $((DUR+4))s > $d/broker.log 2>&1 & bpid=$!
  sleep 2
  for i in $(seq 0 $((nports-1))); do p=$((20001+2*i))
    taskset -c $LITTLE $BD/ocudu-zmq-sink --endpoint tcp://127.0.0.1:$p --duration ${DUR}s --request-interval-us ${REQ_US:-$slot_us} > $d/sink-$i.log 2>&1 & pids+=($!); done
  sleep $((DUR/2)); bcpu=$(ps -o %cpu= -p $bpid | tr -d ' '); 
  wait $bpid; brc=$?; t1=$(date +%s.%N)
  for q in "${pids[@]}"; do wait $q 2>/dev/null; done
  python3 - "$d" "$srate" "$DUR" > $d/sinks.txt <<'PY'
import sys,glob,re
d,srate,dur=sys.argv[1],float(sys.argv[2]),float(sys.argv[3]); r=[]
for f in sorted(glob.glob(d+"/sink-*.log")):
    m=re.search(r"samples_received=(\d+)",open(f).read()); r.append(int(m.group(1))/(srate*dur) if m else 0.0)
print(f"{min(r):.4f} {sum(r)/len(r):.4f} {max(r):.4f}" if r else "0 0 0")
PY
  read -r rmin rmean rmax < $d/sinks.txt
  stop=$(grep -m1 "event=stop" $d/broker.log); g(){ echo "$stop" | grep -o "$1=[0-9]*" | cut -d= -f2; }
  wall=$(python3 -c "print(round($t1-$t0,1))")
  echo "   sink_ratio min/mean/max=$rmin/$rmean/$rmax starv=$(g rx_starvations) overflow=$(g tx_queue_overflows) zmq_err=$(g zmq_errors) brc=$brc cpu=$bcpu% wall=$wall"
  echo "$phase,$label,$ues,$srate,$scs,$taps,$gnbp,$uep,$cells,$nports,$rmin,$rmean,$rmax,$(g tx_pulls),$(g rx_requests),$(g rx_starvations),$(g tx_queue_overflows),$(g zmq_errors),$brc,$bcpu,$wall" >> $out
  pkill -f "ocudu-zmq-source --endpoint tcp://\*:200" 2>/dev/null; pkill -f "ocudu-zmq-sink --endpoint tcp://127.0.0.1:200" 2>/dev/null; sleep 2
done
