# launch — robot fight over the emulated link (R4b)

Glue between the native multi-UE gate's side-process hooks and the arena
package. Nothing here changes the arena, brains or modem; it only starts them
in the right namespaces, in the right order, and keeps fights running
back-to-back for the length of the gate.

| File | Hook | Runs where | Does |
|---|---|---|---|
| `root_exec.sh` | `OCUDU_NATIVE_MUE_ROOT_EXEC` | the stack namespace (ogstun 10.45.1.1, broker, bridge) | publishes the two spawn positions on the bridge's ipc endpoint while the UEs attach (`RF_PREPUB`), waits for both modems, then loops: one `arena.py` per fight (unix socket per robot, positions PUB) + one `brain.py` per robot bound to the gateway address; appends `fights/summary.jsonl` |
| `ue_exec.sh` | `OCUDU_NATIVE_MUE_UE_EXEC` | the UE's network namespace, after its ping passed | `modem.py` bound to the UE's tun address (`RF_PORT_BASE`+index), relaying to the arena's unix socket |
| `../analyze_run.py` | — | anywhere | joins a run's `fights/`, `sionna-status.jsonl`, `broker.log`, srsUE metrics/logs into a per-fight table (markdown + JSON) |

## Spark recipe (what R4b ran)

```
sudo env HOME=/root PATH=$PATH OCUDU_NATIVE_ROOT=/workspace/ocudu-spark \
  OCUDU_NATIVE_CHANNEL_BUILD=/workspace/ocudu-spark/builds/gpuch-r4b-release CUDACXX=/usr/local/cuda/bin/nvcc \
  OCUDU_NATIVE_SKIP_WORKSPACE_LOCK=1 \
  OCUDU_NATIVE_SIONNA_PYTHON=/workspace/sionna-venv/bin/python \
  DRJIT_LIBOPTIX_PATH=/usr/lib/aarch64-linux-gnu/libnvoptix.so.1 \
  OCUDU_NATIVE_SIONNA_SCENARIO=$tree/use_cases/configs/sionna/scenarios/robot_ring/robot-ring-walk.json \
  OCUDU_NATIVE_SIONNA_POSITION_ENDPOINT=ipc:///workspace/ocudu-spark/run/arena/positions.sock \
  OCUDU_NATIVE_SIONNA_POSITION_OFFSET=2.0,0,0 \
  OCUDU_NATIVE_SIONNA_TX_POWER_UL=3.0e2 \
  OCUDU_NATIVE_MUE_DURATION_SECONDS=300 OCUDU_NATIVE_MUE_STRICT_REALTIME=0 \
  OCUDU_NATIVE_MUE_ROOT_EXEC="RF_PYTHON=/workspace/robot-venv/bin/python RF_TIME_LIMIT=20 \
      RF_POS_ENDPOINT=ipc:///workspace/ocudu-spark/run/arena/positions.sock \
      bash $tree/use_cases/robot_fight/launch/root_exec.sh {run_dir} {log_dir} '{ue_ids}' '{ue_ips}' {ue_gateway}" \
  OCUDU_NATIVE_MUE_UE_EXEC="bash $tree/use_cases/robot_fight/launch/ue_exec.sh {ue_id} {ue_ip} {ue_index} {run_dir} {log_dir}" \
  bash scripts/native/run-ocudu-sionna-multi-ue.sh
```

Notes.

- The position endpoint must be `ipc://` (the bridge runs in the gate's
  network namespace); `root_exec.sh` binds it in the stack namespace, which
  shares the filesystem. Bridge offset `2.0,0,0` puts the 4 m ring east of the
  ring scene's pillars, in line of sight of the mast; `0,0,0` centres it on
  the pillars and the robots then cross the shadow (see the R4b record for
  what that does to the link).
- `RF_BOT` / `RF_POLICY` pass `--bot` / `--policy` through unchanged (unset =
  package defaults). The robot venv on the Spark is `/workspace/robot-venv`
  (mujoco, numpy, pyzmq, pytest); `modem.py` runs on the system python3.
- Modem sockets live under `{run_dir}/arena/` (short paths: AF_UNIX limit).
  Each fight is a new arena process; the modems and brains re-learn addresses
  from the first datagram, so nothing needs restarting between fights.
- Teardown: the gate stops `ue-exec-*` and `root-exec` groups; `root_exec.sh`
  forwards TERM to the running arena and brains. After a run
  `pgrep -af "arena.py|brain.py|modem.py"` should print nothing.
- Analyse: `python3 use_cases/robot_fight/analyze_run.py --log-dir <log_dir>
  --report r.json --markdown r.md`.

## R7 paired controller runs

`root_exec.sh` accepts `RF_POLICY_0` and `RF_POLICY_1`, falling back to
`RF_POLICY` for each unset value. Fight summaries retain each brain's policy
and link estimate. Both controllers receive the same strategy/gains by default;
`balance_comp` changes prediction, not the robot or packet protocol.
The brain's default seeded speed jitter is 10%; use `RF_PARAM_JITTER=0` to
remove that variation in a controlled comparison. Strategy noise and physical
positions still vary, so retain the side swap and matched seeds.

On an idle Spark, from a separate validation checkout:

```bash
RF_POLICY_0=balance_comp RF_POLICY_1=balance RF_SEED=7000 RF_DURATION=180 \
  bash use_cases/robot_fight/launch/run_r7_spark.sh base-comp0
RF_POLICY_0=balance RF_POLICY_1=balance_comp RF_SEED=7000 RF_DURATION=180 \
  bash use_cases/robot_fight/launch/run_r7_spark.sh base-comp1
```

Use unique tags; the runner refuses to overwrite an earlier log. Native
experiment overrides must be positional `OCUDU_NATIVE_...=value` arguments.
Inherited native overrides are rejected because `sudo` can otherwise erase
them silently. Check the resulting run report and rendered gNB configurations,
not just the requested label. The runner preserves the native gate's exit
status and records GPU processes before/after. Its default is two protected
brokers, no contention, LOS, strict realtime off. This does not certify a
strict real-time gate. Override `R7_CHANNEL_BUILD` for a separate build path
and `R7_OUTPUT_ROOT` for experiment artifacts; the default paths target Spark.

Run side swaps sequentially with the same seed range and link treatment.
Compare the intersection of completed seeds: fixed-duration runs need not
finish the same number of fights. Preserve and exclude interrupted final
fights, missing results, lockstep fights and out-of-range RTF explicitly.

For the radio characterization matrix (UDP echo probes, without a fighting
arena), run `bash use_cases/robot_fight/launch/run_r7_probe_sweep.sh UNIQUE_PREFIX`.
It executes a fresh baseline and eight AWGN/RAN conditions sequentially, with
160-second gates by default. Requested overrides are saved in a TSV manifest;
failed radio gates are retained as results. Scripted scenario positions replace
the arena feed in this mode. Its offset and uplink power follow the R7 runner,
so use the new baseline when comparing these probes with earlier experiments.

For a fresh-seed contention repeat with speed jitter disabled, run
`bash use_cases/robot_fight/launch/run_r7_repeat.sh UNIQUE_PREFIX`. Defaults are
seed9000 and 300seconds per assignment; `RF_SEED` and `RF_DURATION` override
those values. This runner uses both plain brokers and the same 200us-request
MPS GPU hog as the first contention comparison.

The pinned gNB accepts K1/K2 only in `[1,4]`, with both defaults4. The historical
K2=8 and K1=7/K2=8 sweep entries deliberately preserve the unsupported-condition
result; they do not create a valid higher-latency radio condition. The sweep
continues after native configuration rejection and stops on wrapper/GPU refusal.
Use `R7_PROBE_CASES=sr40,k1k2,retx1` with a fresh prefix to run a subset.

`collect_r7_results.py` streams selected completed runs as a small-artifact tar
archive, including failed runners and incomplete fight directories. It excludes
subscriber configs and large internal logs. For example, from the local checkout:

```bash
mkdir -p results/robot-fight/r7-collected
ssh spark-minwoo 'python3 - --prefix MY_PREFIX' \
  < use_cases/robot_fight/collect_r7_results.py | \
  tar -xf - -C results/robot-fight/r7-collected
```

Check `collection.json` for missing artifacts; an early dry-run failure can have
no run timestamp in its console output. Audit conditions with
`use_cases/robot_fight/audit-r7-artifacts.py`: reported SNR alone is insufficient; the
helper checks rendered noise powers, RX model wiring, and both gNB cell settings.
