#!/usr/bin/env bash
# NC-D5 on the Spark: with the D5 hook disabled, a gNB with every acceleration
# enabled on a ZMQ radio must abort during upper-PHY construction.
#
# Same edit as scripts/cuda/d5-negative-control.sh (the substitution is disabled,
# the declarations stay so the file compiles). The gNB is started directly with
# the config the runner rendered for a passing `all` run -- the runner itself
# cannot be used, because its resolver rejects a source that differs from the
# locked patch, which is exactly what this control does. The defect is in
# upper-PHY construction, so no core or UE is needed to observe it.
#
# A nonzero exit is necessary but not sufficient: the console must carry the
# fatal error the missing executor produces. The patch is restored and the gNB
# rebuilt afterwards, and the restored diff is compared with the lock.
set -uo pipefail
root=/workspace/ocudu-spark
repo="$(cd "$(dirname "$0")/../../.." && pwd)"
src="$root/src/ocudu-cuda-c1"
build="$root/builds/s2-cuda-patched-sm121"
patch_file="$repo/integrations/ocudu/patches/c1-ocudu-cuda-discrete-and-config.patch"
cfg="${1:?rendered gnb.yaml of a passing all-acceleration run}"
expected='Accelerated PDSCH block processor requested but no block-processor capable PDSCH configuration is active'
out="$root/results/s3-nc-d5-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$out"
grep -q 'pdsch_acceleration_mode: enabled' "$cfg" || { echo "config does not enable PDSCH acceleration"; exit 2; }

restore() { git -C "$src" checkout -q -- . && git -C "$src" apply "$patch_file"; }
restore
python3 - "$src" <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]) / 'lib/phy/upper/upper_phy_factories.cpp'
s = p.read_text()
anchor = '  if (pdsch_acceleration_requested && !pdsch_codeblock_executor.is_valid() &&'
assert s.count(anchor) == 1, 'NC-D5 anchor not found'
p.write_text(s.replace(anchor, '  if (false /* NEGATIVE CONTROL */ && pdsch_acceleration_requested && !pdsch_codeblock_executor.is_valid() &&'))
PY
cmake --build "$build" --target gnb -j16 > "$out/build-nc.log" 2>&1 || { echo "NC build failed"; restore; exit 2; }

# Run from a scratch copy so relative log/pcap paths land in $out.
sed "s#/workspace/ocudu-spark/results/cuda-rebuild/[^/]*/logs#$out#g" "$cfg" > "$out/gnb.yaml"
cd "$out" && timeout 120 "$build/apps/gnb/gnb" -c "$out/gnb.yaml" > "$out/gnb-console.log" 2>&1
status=$?
mech=absent; grep -qF "$expected" "$out/gnb-console.log" "$out"/*.log 2>/dev/null && mech=present
verdict='*** CONTROL BROKEN ***'
[[ $status -ne 0 && $mech == present ]] && verdict='CONTROL OK'
[[ $status -ne 0 && $mech != present ]] && verdict='*** failed, but not by the restored mechanism ***'
printf 'NC-D5 pdsch-executor  exit=%s mechanism=%s %s\n' "$status" "$mech" "$verdict" | tee "$out/nc-summary.txt"

restore
cmake --build "$build" --target gnb -j16 > "$out/build-restore.log" 2>&1
echo "restored_diff_matches_lock=$([[ "$(git -C "$src" -c core.abbrev=7 diff --binary HEAD | sha256sum | cut -d' ' -f1)" == "$(sha256sum "$patch_file" | cut -d' ' -f1)" ]] && echo yes || echo NO)" | tee -a "$out/nc-summary.txt"
echo "evidence=$out"
