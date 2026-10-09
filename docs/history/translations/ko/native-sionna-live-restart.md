# Native Sionna 1x1 live demo 재시작

> Historical record; its claims apply to the original date, revision and setup. Korean original retained; current English guidance is in the documentation navigation. Migrated from `docs/native-sionna-live-restart.md` at `58d3156` without changing recorded measurements.


이 문서는 Docker와 `sudo` 없이 실행하는 OCUDU gNB, srsUE, Open5GS,
GPU Channel, Sionna RT bridge, Web UI 전체 스택을 안전하게 다시 시작하는
순서다.

`error: another native 1x1 run is active`는 별도의 `.lock` 파일이 남았다는
뜻이 아니다. 런처가 `scripts/native/run-ocudu-legacy-1x1.sh` 파일 자체에
`flock`을 보유하고 있다는 뜻이다. 따라서 lock 파일을 지우지 말고 lock을
보유한 런처와 그 실행에서 남은 프로세스를 종료해야 한다.

## 1. 경로와 실행 환경 설정

아래 명령은 모두 같은 터미널에서 순서대로 실행한다.

```bash
cd /home/ubuntu/OCUDU/ocudu-gpu-channel

export OCUDU_REPO=/home/ubuntu/OCUDU/ocudu-gpu-channel
export OCUDU_NATIVE_ROOT=/home/ubuntu/ocudu-native-workspace
export CUDACXX=/opt/conda/envs/torch/bin/nvcc
export OCUDU_NATIVE_GPU_DEVICE=0
```

## 2. 현재 남아 있는 프로세스와 lock 확인

먼저 lock 파일을 사용하는 실제 PID를 직접 확인한다. `pgrep`에서 프로세스
이름을 추측하는 방식은 사용하지 않는다. Open5GS의 `5gc` 실행 파일은 여러
`open5gs-...d` 자식 데몬을 만들며, 이 자식들이 런처의 lock file descriptor를
상속할 수 있기 때문이다.

```bash
export OCUDU_LOCK_PATH="$OCUDU_REPO/scripts/native/run-ocudu-legacy-1x1.sh"

OCUDU_LOCK_HOLDERS="$(fuser "$OCUDU_LOCK_PATH" 2>/dev/null || true)"
if [[ -n "$OCUDU_LOCK_HOLDERS" ]]; then
  echo "native_1x1_lock=held pids=$OCUDU_LOCK_HOLDERS"
  for OCUDU_PROCESS_ID in $OCUDU_LOCK_HOLDERS; do
    printf 'PID %s: ' "$OCUDU_PROCESS_ID"
    tr '\0' ' ' < "/proc/$OCUDU_PROCESS_ID/cmdline" 2>/dev/null || true
    printf '\n'
  done
else
  echo 'native_1x1_lock=no_fuser_holder'
fi

if flock -n "$OCUDU_LOCK_PATH" -c true; then
  echo 'native_1x1_lock=free'
else
  echo 'native_1x1_lock=held'
fi
```

이 블록은 lock이 사용 중이어도 마지막 `echo`가 성공하므로 터미널 실행기가
단순히 `execution failed`라고 표시하지 않는다. `native_1x1_lock=free`가
출력되면 3단계를 건너뛰고 4단계부터 실행해도 된다.

## 3. 이전 실행의 고아 프로세스 정리

### 3.1 lock을 실제로 보유한 프로세스 종료

다음 블록은 `fuser`가 현재 lock 파일의 사용자로 보고한 PID 중 현재 사용자
소유인 프로세스만 종료한다. 먼저 `TERM`을 보내고 최대 10초를 기다린 뒤,
계속 lock을 보유한 프로세스에만 `KILL`을 보낸다.

```bash
OCUDU_RUN_UID="$(id -u)"

ocudu_owned_lock_pids() {
  local ocudu_process_id ocudu_process_uid
  for ocudu_process_id in $(fuser "$OCUDU_LOCK_PATH" 2>/dev/null || true); do
    [[ "$ocudu_process_id" =~ ^[1-9][0-9]*$ ]] || continue
    ocudu_process_uid="$(
      awk '/^Uid:/ {print $2}' "/proc/$ocudu_process_id/status" 2>/dev/null
    )"
    if [[ "$ocudu_process_uid" == "$OCUDU_RUN_UID" ]]; then
      printf '%s\n' "$ocudu_process_id"
    else
      printf '다른 사용자의 lock PID는 종료하지 않음: %s uid=%s\n' \
        "$ocudu_process_id" "${ocudu_process_uid:-unknown}" >&2
    fi
  done
}

mapfile -t OCUDU_LOCK_PIDS < <(ocudu_owned_lock_pids)
if ((${#OCUDU_LOCK_PIDS[@]})); then
  printf 'TERM lock holder PID: %s\n' "${OCUDU_LOCK_PIDS[*]}"
  kill -TERM "${OCUDU_LOCK_PIDS[@]}" 2>/dev/null || true
fi

for OCUDU_WAIT_STEP in {1..10}; do
  mapfile -t OCUDU_LOCK_PIDS < <(ocudu_owned_lock_pids)
  ((${#OCUDU_LOCK_PIDS[@]} == 0)) && break
  sleep 1
done

mapfile -t OCUDU_LOCK_PIDS < <(ocudu_owned_lock_pids)
if ((${#OCUDU_LOCK_PIDS[@]})); then
  printf 'KILL lock holder PID: %s\n' "${OCUDU_LOCK_PIDS[*]}"
  kill -KILL "${OCUDU_LOCK_PIDS[@]}" 2>/dev/null || true
  sleep 1
fi
```

### 3.2 lock 외에 남은 이 demo의 프로세스 종료

lock을 상속하지 않은 Broker, gNB, srsUE, Sionna, Web UI 또는 MongoDB가
남아 있으면 다음 실행에서 포트나 MongoDB 데이터 디렉터리가 충돌할 수 있다.
다음 블록은 현재 사용자의 `/proc`를 검사한다. 전체 명령행 문자열을
검색하지 않고 실제 `/proc/PID/exe` 또는 NUL로 분리한 개별 argv가 이 demo의
실행 파일·스크립트 경로와 일치하는 프로세스만 찾는다. 따라서 명령 내용을
인자로 가지고 있는 터미널 실행기나 `bash -lc` wrapper를 잘못 종료하지 않는다.

```bash
ocudu_runtime_pids() {
  local ocudu_proc ocudu_process_id ocudu_process_uid ocudu_executable
  local ocudu_argument ocudu_matched
  local -a ocudu_arguments
  for ocudu_proc in /proc/[1-9]*; do
    ocudu_process_id="${ocudu_proc#/proc/}"
    ocudu_process_uid="$(
      awk '/^Uid:/ {print $2}' "$ocudu_proc/status" 2>/dev/null
    )"
    [[ "$ocudu_process_uid" == "$OCUDU_RUN_UID" ]] || continue
    ocudu_executable="$(readlink -f "$ocudu_proc/exe" 2>/dev/null || true)"
    ocudu_matched=0
    case "$ocudu_executable" in
      "$OCUDU_NATIVE_ROOT/builds/ocudu-gpu-channel-cuda-release/ocudu-gpu-channel" | \
      "$OCUDU_NATIVE_ROOT/builds/srsran4g-zmq-release/srsue/src/srsue" | \
      "$OCUDU_NATIVE_ROOT/builds/ocudu-zmq-release/apps/gnb/gnb" | \
      "$OCUDU_NATIVE_ROOT/builds/open5gs-v2.7.6/tests/app/5gc" | \
      "$OCUDU_NATIVE_ROOT/builds/open5gs-v2.7.6/src/"*/open5gs-*d | \
      "$OCUDU_NATIVE_ROOT/install/mongodb-6.0.29/bin/mongod")
        ocudu_matched=1
        ;;
    esac
    ocudu_arguments=()
    while IFS= read -r -d '' ocudu_argument; do
      ocudu_arguments+=("$ocudu_argument")
    done < "$ocudu_proc/cmdline" 2>/dev/null || true
    if ((ocudu_matched == 0)); then
      for ocudu_argument in "${ocudu_arguments[@]}"; do
        case "$ocudu_argument" in
          "$OCUDU_REPO/scripts/native/run-ocudu-sionna-1x1.sh" | \
          "$OCUDU_REPO/scripts/native/run-ocudu-legacy-1x1.sh" | \
          "$OCUDU_REPO/scripts/native/run-ocudu-legacy-1x1-inner.sh" | \
          "$OCUDU_REPO/apps/sionna_bridge/run_bridge.py" | \
          "$OCUDU_REPO/apps/dashboard/server.py" | \
          ./scripts/native/run-ocudu-sionna-1x1.sh | \
          ./scripts/native/run-ocudu-legacy-1x1.sh)
            ocudu_matched=1
            break
            ;;
        esac
      done
    fi
    ((ocudu_matched == 1)) && printf '%s\n' "$ocudu_process_id"
  done | sort -un
}

mapfile -t OCUDU_REMAINING_PIDS < <(ocudu_runtime_pids)
if ((${#OCUDU_REMAINING_PIDS[@]})); then
  printf 'TERM remaining PID: %s\n' "${OCUDU_REMAINING_PIDS[*]}"
  kill -TERM "${OCUDU_REMAINING_PIDS[@]}" 2>/dev/null || true
fi

for OCUDU_WAIT_STEP in {1..10}; do
  mapfile -t OCUDU_REMAINING_PIDS < <(ocudu_runtime_pids)
  ((${#OCUDU_REMAINING_PIDS[@]} == 0)) && break
  sleep 1
done

mapfile -t OCUDU_REMAINING_PIDS < <(ocudu_runtime_pids)
if ((${#OCUDU_REMAINING_PIDS[@]})); then
  printf 'KILL remaining PID: %s\n' "${OCUDU_REMAINING_PIDS[*]}"
  kill -KILL "${OCUDU_REMAINING_PIDS[@]}" 2>/dev/null || true
  sleep 1
fi
```

정리 결과를 확인한다.

```bash
mapfile -t OCUDU_LOCK_PIDS < <(ocudu_owned_lock_pids)
mapfile -t OCUDU_REMAINING_PIDS < <(ocudu_runtime_pids)

printf 'lock holder PID: %s\n' "${OCUDU_LOCK_PIDS[*]:-none}"
printf 'remaining runtime PID: %s\n' "${OCUDU_REMAINING_PIDS[*]:-none}"

if flock -n "$OCUDU_LOCK_PATH" -c true; then
  echo 'native_1x1_lock=free'
else
  echo 'native_1x1_lock=still_held'
fi
```

`lock holder PID: none`, `remaining runtime PID: none`,
`native_1x1_lock=free`가 모두 출력되어야 한다. 확인 블록은 lock이 남아
있어도 항상 설명을 출력하고 성공 상태로 끝나므로 `execution failed`만 표시되지
않는다. 기존 실행의
timestamp별 로그, report, config와 IPC 디렉터리는 증거 자료이므로 삭제할
필요가 없다. 새 실행은 새로운 UTC timestamp 디렉터리를 사용한다.

## 4. native workspace 검증

```bash
./scripts/native/bootstrap-workspace.sh \
  --verify-only \
  --root "$OCUDU_NATIVE_ROOT"
```

검증에 성공하면 마지막 부분에 workspace lock 검증 결과가 출력된다. 이
단계에서 revision mismatch나 누락 파일 오류가 발생하면 live demo를 시작하지
말고 해당 workspace 오류부터 해결한다.

## 5. 전체 live demo 실행

```bash
./scripts/native/run-ocudu-sionna-1x1.sh
```

이 명령 하나가 다음 구성 요소를 순서대로 시작한다.

1. 격리된 user/network/mount namespace와 TUN 장치
2. MongoDB와 Open5GS 5GC
3. CUDA GPU Channel Broker
4. Sionna RT bridge
5. OCUDU gNB와 srsUE
6. Web UI

다음 두 줄이 출력될 때까지 기다린다.

```text
event=native_sionna_1x1_live_ready ...
Web UI: http://127.0.0.1:8080
```

그 후 같은 호스트의 브라우저에서 <http://127.0.0.1:8080>을 연다.

원격 PC의 브라우저를 사용한다면 원격 PC에서 별도 터미널을 열고 SSH
포트 포워딩을 실행한다.

```bash
ssh -L 8080:127.0.0.1:8080 ubuntu@GPU_HOST
```

그 뒤 원격 PC 브라우저에서 <http://127.0.0.1:8080>을 연다.

## 6. 정상 종료

`run-ocudu-sionna-1x1.sh`를 실행한 터미널에서 `Ctrl-C`를 한 번 누르고,
셸 프롬프트가 돌아올 때까지 기다린다. 런처는 Web UI를 먼저 종료한 뒤
Sionna, Broker, srsUE, gNB, Open5GS, MongoDB와 namespace를 정리한다.

터미널을 강제로 닫았거나 프롬프트가 돌아오기 전에 세션이 끊겼다면 다음
시작 전에 이 문서의 2단계와 3단계를 다시 실행한다.

## 빠른 재실행 순서 요약

이미 환경 검증을 마쳤고 이전 실행이 정상 종료된 경우에는 다음 명령만
필요하다.

```bash
cd /home/ubuntu/OCUDU/ocudu-gpu-channel
export OCUDU_NATIVE_ROOT=/home/ubuntu/ocudu-native-workspace
export CUDACXX=/opt/conda/envs/torch/bin/nvcc
export OCUDU_NATIVE_GPU_DEVICE=0

./scripts/native/run-ocudu-sionna-1x1.sh
```
