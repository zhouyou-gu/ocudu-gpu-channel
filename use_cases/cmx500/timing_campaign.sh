#!/bin/bash
# Timing-margin measurement campaign (transmits in runs A and B). Run directories are listed in runs/timing_campaign.txt.
#   C: dry, idle, PHY log warning, 30 s     A: connected, PHY log warning, 70 s     B: connected, PHY log info, 50 s
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
L=/workspace/cmx-loop
cd $L
OUT=$L/runs/timing_campaign.txt
echo "# $(date -u +%FT%TZ) load $(cut -d' ' -f1-3 /proc/loadavg)" > $OUT
export UE_BUILD=/workspace/builds/srsran4g-minwoo-cmx TIMING=1
run() { # label dry phy_log dur attempts
  for a in $(seq $5); do
    DRY=$2 PHY_LOG=$3 DUR=$4 "${script_dir}/run_cmx_loop.sh" > /dev/null 2>&1
    R=$(ls -td runs/*/ | head -1); R=${R%/}
    if [ "$2" = 1 ] || grep -q "Handling Registration Accept" $R/srsue.log; then
      echo "$1 $R attempt=$a load=$(cut -d' ' -f1 /proc/loadavg)" >> $OUT; return
    fi
    echo "$1-failed $R attempt=$a" >> $OUT
    sleep 5
  done
}
run C 1 warning 30 1
run A 0 warning 70 3
run B 0 info 50 3
cat $OUT
