#!/bin/bash
# phase 3: real broker(s) + synthetic radios, with hop trace and optional Sionna-rate channel updates; K brokers = K CUDA contexts.
# stdin: "label ues srate scs taps brokers upd_hz"; usage: phase3.sh <out.csv> <phase>
BD=${BD:-/workspace/ocudu-spark-ranb/builds/ocudu-gpu-channel-single-cell}; W=${W:-$(pwd)}; out=$1; phase=$2
DUR=${DUR:-20}; BIG=${BIG:-5-14}; LITTLE=${LITTLE:-15-19}; AUX=${AUX:-0-4}; PY=${PY:-python3}
[ -f $out ] || echo "phase,label,ues,srate_hz,scs_khz,taps,brokers,upd_hz,ports_per_broker,sink_ratio_min,sink_ratio_mean,sink_ratio_max,per_broker_mean,starv_total,broker_rcs,broker_cpu_pct_sum,proc_us_p50,proc_us_p99,busy_share_mean,busy_share_max,upd_ok,upd_rej,upd_err,upd_commit_ms_p50,upd_commit_ms_p99" > $out
while read -r label ues srate scs taps K upd; do
  [ -z "$label" ] && continue; case $label in \#*) continue;; esac
  d=$W/p3-$label; rm -rf $d; mkdir -p $d; slot_us=$(python3 -c "print(int(1000*15/$scs))")
  echo "== $label start=$(date -u +%T) ues=$ues srate=$srate brokers=$K upd=$upd gpu_apps_before=$(nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader 2>/dev/null | tr "\n" ";")"
  pids=(); bpids=(); nports=0
  for k in $(seq 0 $((K-1))); do
    base=$((20000+2000*k)); y=$d/topo-b$k.yaml
    python3 $W/gen.py --ues $ues --srate-hz $srate --scs-khz $scs --taps $taps --base-port $base --out $y > $d/gen-b$k.log || { echo GEN_FAIL; continue 2; }
    nports=$(awk '/ports/{print $3}' $d/gen-b$k.log); batch=$(awk '{print $7}' $d/gen-b$k.log)
    for i in $(seq 0 $((nports-1))); do p=$((base+2*i)); taskset -c $LITTLE $BD/ocudu-zmq-source --endpoint tcp://*:$p --batch-samples $batch --duration $((DUR+8))s > $d/src-b$k-$i.log 2>&1 & pids+=($!); done
  done
  sleep 1; t0=$(date +%s.%N)
  for k in $(seq 0 $((K-1))); do mkdir -p $d/hop-b$k
    OCG_HOP_TRACE_DIR=$d/hop-b$k OCG_BROKER_SPIN=${OCG_BROKER_SPIN:-0} OCG_ZMQ_IO_THREADS=${OCG_ZMQ_IO_THREADS:-2} taskset -c $BIG $BD/ocudu-gpu-channel --config $d/topo-b$k.yaml --duration $((DUR+5))s --control-endpoint tcp://127.0.0.1:$((5600+k)) > $d/broker-b$k.log 2>&1 & bpids+=($!); done
  sleep 2
  for k in $(seq 0 $((K-1))); do base=$((20000+2000*k))
    for i in $(seq 0 $((nports-1))); do p=$((base+1+2*i)); taskset -c $LITTLE $BD/ocudu-zmq-sink --endpoint tcp://127.0.0.1:$p --duration ${DUR}s --request-interval-us $slot_us > $d/sink-b$k-$i.log 2>&1 & pids+=($!); done
    if [ "$upd" != "0" ]; then sleep 1; taskset -c $AUX $PY $W/updater.py --endpoint tcp://127.0.0.1:$((5600+k)) --ues $ues --rate-hz $upd --duration $((DUR-3)) --taps $taps --srate-hz $srate > $d/upd-b$k.log 2>&1 & pids+=($!); fi
  done
  sleep $((DUR/2)); echo "   gpu_apps_during=$(nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader 2>/dev/null | tr "\n" ";")"; bcpu=$(ps -o %cpu= -p $(IFS=,; echo "${bpids[*]}") | awk '{s+=$1} END{print s}')
  rcs=""; for b in "${bpids[@]}"; do wait $b; rcs="$rcs$?/"; done; t1=$(date +%s.%N)
  for q in "${pids[@]}"; do wait $q 2>/dev/null; done
  $PY - "$d" "$srate" "$DUR" "$K" > $d/sinks.txt <<'PY'
import sys,glob,re
d,srate,dur,K=sys.argv[1],float(sys.argv[2]),float(sys.argv[3]),int(sys.argv[4]); allr=[]; per=[]
for k in range(K):
    r=[]
    for f in sorted(glob.glob(f"{d}/sink-b{k}-*.log")):
        m=re.search(r"samples_received=(\d+)",open(f).read()); r.append(int(m.group(1))/(srate*dur) if m else 0.0)
    allr+=r; per.append(f"{sum(r)/len(r):.3f}" if r else "0")
print(f"{min(allr):.4f} {sum(allr)/len(allr):.4f} {max(allr):.4f} {'|'.join(per)}" if allr else "0 0 0 0")
PY
  read -r rmin rmean rmax per < $d/sinks.txt
  starv=0; for k in $(seq 0 $((K-1))); do s=$(grep -m1 "event=stop" $d/broker-b$k.log | grep -o "rx_starvations=[0-9]*" | cut -d= -f2); starv=$((starv+${s:-0})); done
  hop=$(for k in $(seq 0 $((K-1))); do $PY $W/hopstat.py $d/hop-b$k/hop-trace.csv 2>/dev/null; done | awk '{for(i=1;i<=NF;i++){split($i,a,"="); v[a[1]]=(a[1] ~ /max/ && v[a[1]]>a[2])?v[a[1]]:a[2]}} END{print v["proc_us_p50"]","v["proc_us_p99"]","v["busy_share_mean"]","v["busy_share_max"]}')
  u=$(cat $d/upd-b*.log 2>/dev/null | grep -h "^updates" | awk '{for(i=1;i<=NF;i++){split($i,a,"="); s[a[1]]+=a[2]; m[a[1]]=(a[2]>m[a[1]])?a[2]:m[a[1]]}} END{print s["ok"]","s["rejected"]","s["errors"]","m["commit_ms_p50"]","m["commit_ms_p99"]}'); [ -z "$u" ] && u=",,,,"
  echo "   sink min/mean/max=$rmin/$rmean/$rmax per_broker=$per starv=$starv rcs=$rcs cpu=$bcpu% hop[p50,p99,busy_mean,busy_max]=$hop upd[ok,rej,err,p50,p99]=$u"
  echo "$phase,$label,$ues,$srate,$scs,$taps,$K,$upd,$nports,$rmin,$rmean,$rmax,$per,$starv,$rcs,$bcpu,$hop,$u" >> $out
  pkill -f "ocudu-zmq-source --endpoint tcp://\*:2" 2>/dev/null; pkill -f "ocudu-zmq-sink --endpoint tcp://127.0.0.1:2" 2>/dev/null; sleep 2
done
