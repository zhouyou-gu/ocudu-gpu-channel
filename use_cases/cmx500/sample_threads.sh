#!/bin/bash
# Per-thread CPU (top, 1 s window) and current core of srsUE, broker and bridge while run_cmx_loop.sh is running.
# Usage: sample_threads.sh [first_delay_s] [count]
sleep ${1:-8}
for i in $(seq ${2:-3}); do
  echo "---- $(date +%T)"
  for p in $(pgrep -f "srsue/src/srsue|ocudu-gpu-channel --config|build/usrp_zmq_bridge"); do
    name=$(ps -o comm= -p $p)
    top -H -b -n 2 -d 1 -p $p | awk -v n="$name" '/^top -/{blk++} blk==2 && $1 ~ /^[0-9]+$/ && $9+0 > 3 {print "  " n, $1, $12, "cpu=" $9 "%"}' |
      while read n tid comm cpu; do echo "  $n $comm $cpu core=$(ps -o psr= -L -p $p | awk -v t=$tid 'BEGIN{} {print}' >/dev/null; cat /proc/$p/task/$tid/stat 2>/dev/null | awk '{print $39}')"; done
  done
  sleep 2
done
