#!/bin/bash
# phase 4: what bounds the real broker -- ZMQ I/O threads, transport (tcp loopback vs ipc), hop-trace/control-endpoint cost.
# stdin: "label ues srate scs io transport hop ctrl spin"; usage: phase4.sh <out.csv> <phase>
BD=${BD:-/workspace/ocudu-spark-ranb/builds/ocudu-gpu-channel-single-cell}; W=${W:-$(pwd)}; out=$1; phase=$2
DUR=${DUR:-20}; BIG=${BIG:-5-14}; LITTLE=${LITTLE:-15-19}
[ -f $out ] || echo "phase,label,ues,srate_hz,scs_khz,io_threads,transport,hop,ctrl,spin,ports,sink_ratio_min,sink_ratio_mean,sink_ratio_max,rx_starvations,broker_rc,broker_cpu_pct,room_us,data_us,process_us,throttle_us" > $out
while read -r label ues srate scs io tr hop ctrl spin; do
  [ -z "$label" ] && continue; case $label in \#*) continue;; esac
  d=$W/p4-$label; rm -rf $d; mkdir -p $d; y=$d/topo.yaml; slot_us=$(python3 -c "print(int(1000*15/$scs))")
  python3 $W/gen.py --ues $ues --srate-hz $srate --scs-khz $scs --taps 8 --transport $tr --base-port 20000 --out $y > $d/gen.log || { echo GEN_FAIL; continue; }
  nports=$(awk '/ports/{print $3}' $d/gen.log); batch=$(awk '{print $7}' $d/gen.log); rm -f /tmp/ocgb-*
  bind() { [ $tr = tcp ] && echo "tcp://*:$1" || echo "ipc:///tmp/ocgb-$1"; }; conn() { [ $tr = tcp ] && echo "tcp://127.0.0.1:$1" || echo "ipc:///tmp/ocgb-$1"; }
  echo "== $label start=$(date -u +%T) ues=$ues srate=$srate io=$io tr=$tr hop=$hop ctrl=$ctrl spin=$spin"
  pids=(); for i in $(seq 0 $((nports-1))); do taskset -c ${SRC_CPUS:-$LITTLE} $BD/ocudu-zmq-source --endpoint $(bind $((20000+2*i))) --batch-samples $batch --duration $((DUR+6))s > $d/src-$i.log 2>&1 & pids+=($!); done
  sleep 1; extra=""; [ "$ctrl" = 1 ] && extra="--control-endpoint tcp://127.0.0.1:5601"; henv=""; [ "$hop" = 1 ] && { mkdir -p $d/hop; henv="OCG_HOP_TRACE_DIR=$d/hop"; }
  for try in 1 2 3 4; do
    env $henv OCG_BROKER_SPIN=$spin OCG_ZMQ_IO_THREADS=$io taskset -c $BIG $BD/ocudu-gpu-channel --config $y --duration $((DUR+4))s $extra > $d/broker.log 2>&1 & bpid=$!
    sleep 2; grep -q "event=fatal" $d/broker.log || break
    echo "   broker start failed (try $try): $(grep -m1 -o 'error=.*' $d/broker.log | cut -c1-80); MemFree=$(awk '/MemFree/{print $2}' /proc/meminfo) kB"; wait $bpid; sleep 5
  done
  for i in $(seq 0 $((nports-1))); do taskset -c ${SINK_CPUS:-$LITTLE} $BD/ocudu-zmq-sink --endpoint $(conn $((20001+2*i))) --duration ${DUR}s --request-interval-us $slot_us > $d/sink-$i.log 2>&1 & pids+=($!); done
  sleep $((DUR/2)); bcpu=$(ps -o %cpu= -p $bpid | tr -d ' '); wait $bpid; brc=$?; for q in "${pids[@]}"; do wait $q 2>/dev/null; done
  read -r rmin rmean rmax < <(python3 - "$d" "$srate" "$DUR" <<'PY'
import sys,glob,re
d,srate,dur=sys.argv[1],float(sys.argv[2]),float(sys.argv[3]); r=[]
for f in sorted(glob.glob(d+"/sink-*.log")):
    m=re.search(r"samples_received=(\d+)",open(f).read()); r.append(int(m.group(1))/(srate*dur) if m else 0.0)
print(f"{min(r):.4f} {sum(r)/len(r):.4f} {max(r):.4f}" if r else "0 0 0")
PY
)
  starv=$(grep -m1 "event=stop" $d/broker.log | grep -o "rx_starvations=[0-9]*" | cut -d= -f2)
  st=$(python3 $W/stagestat.py $d/broker.log | tail -1 | awk '{print $3","$5","$7","$8}')
  echo "   sink min/mean/max=$rmin/$rmean/$rmax starv=$starv brc=$brc cpu=$bcpu% stages[room,data,process,throttle]=$st"
  echo "$phase,$label,$ues,$srate,$scs,$io,$tr,$hop,$ctrl,$spin,$nports,$rmin,$rmean,$rmax,$starv,$brc,$bcpu,$st" >> $out
  pkill -f "ocudu-zmq-source --endpoint" 2>/dev/null; pkill -f "ocudu-zmq-sink --endpoint" 2>/dev/null; sleep 2
done
