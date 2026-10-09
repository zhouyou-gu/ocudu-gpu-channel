#!/usr/bin/env bash
# Robot-fight glue for the native multi-UE gate's OCUDU_NATIVE_MUE_ROOT_EXEC hook (R4b).
#
# Runs in the gate's stack namespace (where ogstun 10.45.1.1, the broker and
# the Sionna bridge live). Waits until every UE's modem (started by the gate's
# UE_EXEC hook, see ue_exec.sh) has bound its unix socket, then runs fights
# back to back until the gate tears this group down: one arena process per
# fight (its per-robot unix sockets are what the modems relay into) and one
# brain per robot bound to the UPF-side gateway address so replies route back
# over GTP. Positions are published on the ipc endpoint the bridge subscribes
# to. Nobody waits for anybody inside a fight; between fights the bridge keeps
# the last position (flagged stale) until the next arena binds.
#
# Usage (as the gate expands it):
#   RF_PYTHON=<robot venv python> [RF_* knobs] bash root_exec.sh \
#       {run_dir} {log_dir} '{ue_ids}' '{ue_ips}' {ue_gateway}
#
# Knobs: RF_PYTHON (required), RF_TIME_LIMIT (20 s), RF_FIGHTS (0 = until
# killed), RF_SEED (1), RF_POS_ENDPOINT (ipc://<run_dir>/arena/positions.sock),
# RF_PORT_BASE (6000: robot i listens on RF_PORT_BASE+i at its UE address),
# RF_MODEM_WAIT_S (240), RF_RATE_HZ (100), RF_STATE_HZ (200), RF_TTL_MS (60),
# RF_POLICY (reactive), RF_BOT (unset = the arena's default bot; `balance`
# selects the R2c self-balancing bot, whose brain policy is `balance`),
# RF_RING_RADIUS (2.0), RF_STALE_POLICY (coast),
# RF_ARENA_EXTRA / RF_BRAIN_EXTRA (extra CLI words).
# RF_POLICY_0 / RF_POLICY_1 override RF_POLICY per robot for paired R7 runs.
set -euo pipefail

run_dir="$1"; log_dir="$2"; ue_ids_text="$3"; ue_ips_text="$4"; gateway="$5"
read -r -a ue_ids <<<"${ue_ids_text}"
read -r -a ue_ips <<<"${ue_ips_text}"
[[ "${#ue_ids[@]}" -eq 2 && "${#ue_ips[@]}" -eq 2 ]] || { echo "error: need exactly two UEs, got '${ue_ids_text}'" >&2; exit 2; }
python="${RF_PYTHON:?set RF_PYTHON to the robot venv interpreter}"
[[ -x "${python}" ]] || { echo "error: RF_PYTHON not executable: ${python}" >&2; exit 2; }
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
pkg_dir="$(dirname "${script_dir}")"
time_limit="${RF_TIME_LIMIT:-20}"
fights="${RF_FIGHTS:-0}"
seed="${RF_SEED:-1}"
port_base="${RF_PORT_BASE:-6000}"
modem_wait_s="${RF_MODEM_WAIT_S:-240}"
arena_dir="${run_dir}/arena"
pos_endpoint="${RF_POS_ENDPOINT:-ipc://${arena_dir}/positions.sock}"
fights_dir="${log_dir}/fights"
mkdir -p "${arena_dir}" "${fights_dir}"
if [[ "${pos_endpoint}" == ipc://* ]]; then
  mkdir -p "$(dirname "${pos_endpoint#ipc://}")"
fi

now_ms() { date -u +%s%3N; }
log() { printf '%s event=%s\n' "$(now_ms)" "$*"; }

children=()
arena_pid=""
brain_pids=()
stopping=0
stop_children()
{
  stopping=1
  local pid
  for pid in "${brain_pids[@]:-}" "${arena_pid}"; do
    [[ -n "${pid}" ]] && kill -s TERM "${pid}" 2>/dev/null || true
  done
  for pid in "${brain_pids[@]:-}" "${arena_pid}"; do
    [[ -n "${pid}" ]] && wait "${pid}" 2>/dev/null || true
  done
}
trap 'log stop signal=TERM; stop_children; exit 143' TERM
trap 'log stop signal=INT; stop_children; exit 130' INT

log start run_dir="${run_dir}" ue_ids="${ue_ids[*]}" ue_ips="${ue_ips[*]}" gateway="${gateway}" \
  pos_endpoint="${pos_endpoint}" time_limit="${time_limit}" fights="${fights}"

# --- spawn-position placeholder while the UEs attach ---------------------
# Until the first arena binds, the bridge would trace the scenario's scripted
# route, which on the ring scene walks through the pillar shadow while the UEs
# are still in initial access. Publish the two spawn positions (arena frame,
# the arena's default +-0.5*R on x) on the same endpoint until the modems are
# up, so the UEs attach where the robots will start. RF_PREPUB=0 disables it.
prepub_pid=""
if [[ "${RF_PREPUB:-1}" == "1" ]]; then
  "${python}" - "${pos_endpoint}" "${ue_ids[0]}" "${ue_ids[1]}" "${RF_RING_RADIUS:-2.0}" "${RF_ANTENNA_HEIGHT:-0.3}" <<'PY' >"${log_dir}/arena-prepub.log" 2>&1 &
import json, sys, time, zmq
endpoint, a, b, radius, z = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4]), float(sys.argv[5])
ctx = zmq.Context.instance()
pub = ctx.socket(zmq.PUB)
pub.setsockopt(zmq.SNDHWM, 10)
pub.bind(endpoint)
spread = 0.5 * radius
try:
    while True:
        msg = {"event": "positions", "t_unix_ms": int(time.time() * 1000), "frame": "arena",
               "nodes": {a: {"position_m": [-spread, 0.0, z], "velocity_mps": [0.0, 0.0, 0.0]},
                         b: {"position_m": [spread, 0.0, z], "velocity_mps": [0.0, 0.0, 0.0]}}}
        pub.send_string(json.dumps(msg, separators=(",", ":")))
        time.sleep(0.2)
finally:
    pub.close(linger=0)
PY
  prepub_pid="$!"
  log prepub_start pid="${prepub_pid}"
fi

# --- wait for the modems (they appear after each UE's PDU session + ping) ---
modem_paths=()
for id in "${ue_ids[@]}"; do modem_paths+=("${arena_dir}/${id}.sock.${id}.modem"); done
deadline=$((SECONDS + modem_wait_s))
while :; do
  ready=1
  for path in "${modem_paths[@]}"; do [[ -S "${path}" ]] || ready=0; done
  [[ "${ready}" -eq 1 ]] && break
  if [[ "${SECONDS}" -ge "${deadline}" ]]; then
    log modem_timeout waited_s="${modem_wait_s}" missing="$(for p in "${modem_paths[@]}"; do [[ -S "$p" ]] || printf '%s ' "$p"; done)"
    [[ -n "${prepub_pid}" ]] && kill "${prepub_pid}" 2>/dev/null || true
    exit 3
  fi
  sleep 0.5
done
log modems_ready t_wait_s="$((SECONDS))"
if [[ -n "${prepub_pid}" ]]; then
  kill "${prepub_pid}" 2>/dev/null || true
  wait "${prepub_pid}" 2>/dev/null || true
  sleep 0.2
fi

robot_unix="${ue_ids[0]}=${arena_dir}/${ue_ids[0]}.sock,${ue_ids[1]}=${arena_dir}/${ue_ids[1]}.sock"
fight=0
while [[ "${fights}" -eq 0 || "${fight}" -lt "${fights}" ]]; do
  fight=$((fight + 1))
  fdir="${fights_dir}/$(printf 'f%03d' "${fight}")"
  mkdir -p "${fdir}"
  fseed=$((seed + fight - 1))
  t_start="$(now_ms)"
  log fight_start n="${fight}" seed="${fseed}" dir="${fdir}"
  # shellcheck disable=SC2086
  "${python}" "${pkg_dir}/arena.py" --node-ids "${ue_ids[0]},${ue_ids[1]}" \
    --robot-unix "${robot_unix}" --positions-endpoint "${pos_endpoint}" \
    --time-limit "${time_limit}" --seed "${fseed}" \
    --state-hz "${RF_STATE_HZ:-200}" --ring-radius "${RF_RING_RADIUS:-2.0}" \
    --stale-policy "${RF_STALE_POLICY:-coast}" ${RF_BOT:+--bot "${RF_BOT}"} \
    --log "${fdir}/arena.jsonl" --result "${fdir}/arena.json" ${RF_ARENA_EXTRA:-} \
    >"${fdir}/arena.out" 2>&1 &
  arena_pid="$!"
  # The arena binds its unix sockets and the PUB at startup; give it a moment.
  bind_deadline=$((SECONDS + 15))
  while [[ "${SECONDS}" -lt "${bind_deadline}" ]]; do
    [[ -S "${arena_dir}/${ue_ids[0]}.sock" && -S "${arena_dir}/${ue_ids[1]}.sock" ]] && break
    kill -0 "${arena_pid}" 2>/dev/null || break
    sleep 0.05
  done
  if ! kill -0 "${arena_pid}" 2>/dev/null; then
    log arena_died n="${fight}"; tail -5 "${fdir}/arena.out" || true
    sleep 2
    continue
  fi
  brain_pids=()
  for index in 0 1; do
    policy_var="RF_POLICY_${index}"
    policy="${!policy_var:-${RF_POLICY:-reactive}}"
    # shellcheck disable=SC2086
    "${python}" "${pkg_dir}/brain.py" --robot-id "${index}" \
      --robot "${ue_ips[index]}:$((port_base + index))" --bind "${gateway}:0" \
      --rate-hz "${RF_RATE_HZ:-100}" --ttl-ms "${RF_TTL_MS:-60}" --policy "${policy}" \
      --seed "$((fseed * 10 + index))" --max-seconds "$(awk -v t="${time_limit}" 'BEGIN{print t+20}')" \
      --log "${fdir}/brain${index}.jsonl" --result "${fdir}/brain${index}.json" ${RF_BRAIN_EXTRA:-} \
      >"${fdir}/brain${index}.out" 2>&1 &
    brain_pids+=("$!")
  done
  set +e
  wait "${arena_pid}"; arena_status="$?"
  set -e
  arena_pid=""
  # Brains exit on the over flag; bound the wait in case the flag was lost.
  brain_deadline=$((SECONDS + 10))
  for pid in "${brain_pids[@]}"; do
    while kill -0 "${pid}" 2>/dev/null && [[ "${SECONDS}" -lt "${brain_deadline}" ]]; do sleep 0.1; done
    kill -s TERM "${pid}" 2>/dev/null || true
    wait "${pid}" 2>/dev/null || true
  done
  brain_pids=()
  t_end="$(now_ms)"
  [[ "${stopping}" -eq 1 ]] && break
  /usr/bin/python3 - "${fdir}" "${fight}" "${fseed}" "${t_start}" "${t_end}" "${arena_status}" \
    "${fights_dir}/summary.jsonl" <<'PY'
import json, pathlib, sys
fdir, n, seed, t0, t1, status, out = sys.argv[1:]
fdir = pathlib.Path(fdir)
row = {"fight": int(n), "seed": int(seed), "t_start_unix_ms": int(t0), "t_end_unix_ms": int(t1),
       "arena_status": int(status), "dir": str(fdir)}
try:
    arena = json.loads((fdir / "arena.json").read_text())
    row.update({k: arena.get(k) for k in ("winner", "winner_node", "reason", "rtf", "late_loops",
                                            "max_lag_ms", "sim_time_s", "wall_time_s", "lockstep")})
    row["robots"] = arena.get("robots")
except Exception as exc:  # noqa: BLE001
    row["arena_error"] = str(exc)
brains = []
for i in (0, 1):
    try:
        b = json.loads((fdir / f"brain{i}.json").read_text())
        brains.append({k: b.get(k) for k in ("robot_id", "policy", "link_est", "outcome", "ticks", "cmds_sent", "states_received",
                                             "deadline_misses", "ticks_without_fresh_state", "state_seq_gaps",
                                             "rtt_us", "state_one_way_us", "reflexes")})
    except Exception as exc:  # noqa: BLE001
        brains.append({"robot_id": i, "error": str(exc)})
row["brains"] = brains
with open(out, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(row, separators=(",", ":")) + "\n")
print(json.dumps({"fight": row["fight"], "winner": row.get("winner_node"), "reason": row.get("reason"),
                  "rtf": row.get("rtf"), "rtt_p50_us": [b.get("rtt_us", {}).get("p50") if isinstance(b.get("rtt_us"), dict) else None for b in brains]}))
PY
  log fight_end n="${fight}" arena_status="${arena_status}"
  sleep 0.5
done
log done fights="${fight}"
