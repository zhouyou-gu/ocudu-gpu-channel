# Jetson AGX Orin에서 CUDA 가속 OCUDU 검증 마일스톤

> Historical record; its claims apply to the original date, revision and setup. Migrated from `JETSON_MILESTONES.md` at `58d3156` without changing recorded measurements.


**최종 목표: WG1 CUDA OCUDU(`nvcuda_accel_02`)가 Jetson AGX Orin에서 빌드·자체 검증·라이브 attach·BLER 정합을 통과하는지, 통과하지 못하면 그 이유를 소스 수준까지 확정한다.**

[`CUDA_MILESTONES.md`](CUDA_MILESTONES.md)(RTX 5090, C0–C7)와 **독립된 트랙**이다. 5090 트랙의 파일·lock·패치·빌드는 이 트랙이 건드리지 않는다. 단계 구조와 판정 규율은 C 트랙을 그대로 따르되, 단계 번호는 `J`로 구분한다.

## 왜 Orin인가 — 세 번째 메모리 모델

벤더 코드의 메모리 정책은 CUDA 장치 속성 네 개로 갈린다. 지금까지 본 두 플랫폼과 Orin은 전부 다르다.

| 속성 | GB10 (WG 기준) | RTX 5090 (C 트랙) | **Jetson AGX Orin** |
|---|---|---|---|
| `cudaDevAttrIntegrated` | 1 | 0 | **1** |
| `ConcurrentManagedAccess` | 1 | 1 | **0** |
| `PageableMemoryAccessUsesHostPageTables` | 1 | 0 | **0** |
| `DirectManagedMemAccessFromHost` | 0 | 0 | **0** |
| 출처 | 벤더 속성 표(**추론**, 실측 아님) | 실측 09-10 | **실측 09-24** |

**핵심 위험:** `resource_grid_cuda_visible_impl.h:1648`은 `integrated=1`이면 `ul_cuda_visible_grid_mode: auto`에서 **managed를 고른다**. 그런데 `ConcurrentManagedAccess=0`인 장치에서는 GPU 작업이 진행 중인 동안 CPU가 managed 페이지에 접근하면 **SIGBUS/segfault**가 날 수 있다(스트림 attach로 범위를 좁히지 않은 경우). 벤더는 "통합형이면 동시 접근도 된다"는 조합(GB10)에서만 검증했고, 5090은 디스크리트라 이 분기를 타지 않았다. **Orin은 벤더 코드의 `integrated → managed` 가정을 처음으로 시험하는 플랫폼이다.** 이 트랙의 가장 가치 있는 산출물은 이 경로의 판정이다.

**벤더 주석과 코드가 서로 어긋난다 (소스 확인 09-24, 실행 확인 전).** `phy_acceleration_runtime_options.h:117-121`의 속성 표는 `CMA=0`인 "older Jetson" 행에 **"auto uses pinned"**라고 적었지만, 실제 선택 함수 `should_use_managed_grid_auto()`(`resource_grid_cuda_visible_impl.h:1646-1653`)는 `cudaDevAttrIntegrated`만 보고 `ConcurrentManagedAccess`를 보지 않는다. 즉 Orin(`integrated=1, CMA=0`)에서 auto는 문서가 말한 pinned가 아니라 **managed**를 고를 것이다. 이 어긋남은 J4에서 실행으로 판정하며 결함 후보 D6이다.

`fully_coherent`(셋 다 1) 불만족이므로 lower-PHY TX direct 경로는 5090·GB10과 마찬가지로 **DEGRADED가 예상치**다(C4 추적 결론 참조). 이것은 이 트랙에서 새로 판정할 항목이 아니라 재확인 항목이다.

## 플랫폼 (실측 2026-09-24)

| 축 | WG (GB10) | 5090 워크스테이션 | **Jetson** |
|---|---|---|---|
| 모듈 | DGX Spark | — | **AGX Orin 64GB**, L4T R36.4.4 (JetPack 6.x), Ubuntu 22.04.3 |
| GPU | GB10, `sm_121` | RTX 5090, `sm_120` | **Orin, `sm_87`, SM 8개** |
| CUDA / 드라이버 | 13.0.88 / 580.95.05 | 12.8.93 / 595.71.05 | **12.6.68 / 540.4.0** |
| CPU | Cortex-X925/A725 | Core Ultra 9 285K | **Cortex-A78AE 12코어 중 8개 online** |
| 메모리 | 통합 128 GB | 32 GB VRAM + host | **통합 64 GB** (CUDA 보고 65.9 GB) |
| GPU 클럭 | — | — | **cur 306 MHz / max 612 MHz** (전원 모드 제한 상태로 보임) |

접속: 워크스테이션에서 `ssh jetson-minwoo` (키 인증, `~/.ssh/config`). 대상은 Jetson 본체가 아니라 **Jetson 위의 컨테이너**(호스트명 `ocudu-minwoo` — 워크스테이션의 5090 컨테이너와 이름이 같으니 혼동 주의), 사용자 `dev`, sudo 가능.

### 확인된 제약 (J0에서 해소할 것)

1. **네트워크 네임스페이스 불가.** 컨테이너 `CapBnd=0xa80425fb`(도커 기본) — `CAP_NET_ADMIN` 없음, `iproute2` 미설치. 게이트는 srsUE를 netns에 넣으므로 이대로는 J3 이후 전부 불가. **컨테이너를 `NET_ADMIN`·`SYS_NICE`·`/dev/net/tun`을 붙여 다시 만들어야 하며 Jetson 호스트 측 작업이다(J0-1).**
2. **전원 모드.** CPU 8/12 코어, GPU max 612 MHz. 측정 전 **호스트에서** `sudo nvpmodel -m 0 && sudo jetson_clocks`(MAXN). 컨테이너에는 `nvpmodel`이 없다. 모드는 모든 증거에 라벨로 붙인다.
3. **디스크.** 저장장치는 eMMC 59 GB 하나뿐이고 NVMe는 없다(`lsblk`, 09-24). `/home/dev`·`/workspace` 여유 29 GB. 전체 예산 ~10 GB로 들어간다(J0-3).
4. **의존성 거의 없음.** `libzmq3-dev`만 확인. `ninja`, fftw, mbedtls, yaml-cpp, sctp, boost, gtest, mongodb 미설치. `nsys` 없음.
5. **upstream 브랜치가 이동했다.** `nvcuda_accel_02` HEAD = `900d8d0e`(09-23), 우리 핀 `5830c9cb` 이후 4커밋(PRACH VkFFT 커널 캐시·warm-up·geometry 분리, 코드 포맷·SPDX 헤더). 이슈 #2의 결함 D1–D5 수정은 없음. **`d2579af2` 패치는 새 HEAD에 그대로 적용되지 않는다** — `srs_estimator_cuda_impl.cpp:355`에서 hunk 실패(`git apply --check`, 09-24). 원인은 upstream의 공백 정렬 변경이고 의미 충돌은 아니다. **이 트랙은 `5830c9cb`로 고정**하고(5090 결과와 비교 가능성), 새 HEAD로의 재기반은 J7에서 한다.

## 절대 제약 (C 트랙 제약을 그대로 상속 + Jetson 추가분)

1. **5090 트랙 무수정.** `cuda-workspace.lock.json`, `patches/c1-ocudu-cuda-discrete-and-config.patch`(`d2579af2`), 기존 `scripts/cuda/*` 동작은 바이트 동일. Jetson용은 **신규 파일**(`scripts/cuda/jetson/`, `cuda-workspace.jetson.lock.json`)로만 추가하거나, 기존 스크립트에 **기본값이 현재 값과 같은** 환경변수 노브를 추가한다(`CUDA_ARCH` 기본 120, sysroot triplet 기본 `x86_64-linux-gnu`).
2. **패치는 층으로 쌓는다.** J1–J2에서 Orin 고유 결함이 나오면 `d2579af2` 위에 얹는 **별도 패치 파일**(`patches/j2-ocudu-cuda-orin.patch`)로 만들고 해시락한다. 기존 패치를 고쳐 쓰지 않는다.
3. **수정마다 음성 대조군 exit≠0 + 의도한 메커니즘이 로그에 찍혔는지 확인.** exit≠0만으로 합격시키지 않는다(C1 NC-D5 교훈).
4. **판정은 "무엇이 실제로 돌았는가"로.** 생존(attach·ping)은 증거가 아니다. `verify-stage-backends.py`의 selected / degraded / fallback 구분을 그대로 쓰고, **managed/pinned 중 무엇이 선택됐는지**를 매 런 기록한다.
5. **모든 증거에 라벨:** 전원 모드(nvpmodel ID, `jetson_clocks` 여부), GPU/CPU 클럭, online 코어, 벤더 커밋, 패치 해시, 빌드 해시.

## 성능 주장 경계

WG 수치는 GB10 것이고 Orin은 SM 8개로 훨씬 작다. **J6 이전에는 어떤 속도 주장도 하지 않는다.** 단, Orin은 CPU도 약하므로(A78AE) 5090 트랙과 달리 20 MHz 1-layer에서도 GPU가 CPU보다 빠를 **가능성은 있다** — 그것은 J6에서 CPU·GPU 교차 실행으로만 말한다.

## 단계

| 단계 | 내용 | Exit 게이트 | 상태 |
|---|---|---|---|
| **J0** | **플랫폼 준비** — 컨테이너 권한, 전원 모드, 작업 디스크, 의존성, 속성 프로브 스크립트 | `ip netns add` 성공, MAXN + `jetson_clocks` 상태에서 12코어 online·GPU max 클럭 기록, 의존성 설치 목록 기록, 속성 프로브(위 표) JSON 저장. 작업 루트 여유 공간 기록 | **완료 2026-09-24 (게이트 3 전원 모드만 보류 — user-a 합의 후)** |
| **J1** | **벤더 원본 빌드 + 자체 검증 (패치 없이)** — `5830c9cb`, `sm_87`, CUDA 12.6 | 빌드 성공(실패 시 원인을 소스·툴체인 수준까지 규명 — CUDA 13 전용 API, `sm_87` 미지원 커널 등). WG 12 PHY + OFH 테스트를 문서 정규식 그대로 실행, **테스트별 판정 + `verify-phy-log.py` 채점**. 실패마다 메커니즘 규명. **managed 경로 테스트가 `ConcurrentManagedAccess=0`에서 어떻게 되는지** 별도 기록 | **중단 2026-09-24** — 빌드 성공, PHY 7/12·OFH 14/16, D6 규명(lower-PHY 2·PDSCH 크래시·OFH 2), SRS 2건 미해결 |
| **J2** | **C1 패치 적용 재검증** — `d2579af2` 그대로 | PHY 14 / OFH 16 판정, 추가 회귀 2건, **NC-D1–D4 exit≠0 재현**. D1(`supports_device_grid_reading()` 분기)이 Orin에서 managed 쪽을 타는지 확인. Orin 고유 실패가 있으면 결함 ID를 D6부터 매기고 별도 패치 + 대조군 | **완료 2026-09-25** — J2: PHY 13/14, OFH 16/16. NC-D6 재현. SRS는 C1로 해결(D6 아님). 남은 1건 D7 → **J2b(D6+D7)에서 PHY 14/14, OFH 16/16, 판정 pass** |
| **J3** | **aarch64 스택 + CPU 기준선** — Open5GS, srsRAN_4G(srsUE), CPU OCUDU, 직결 ZMQ 러너 | CPU gNB로 직결 ZMQ attach/PDU/ping 3/3. 이어서 CUDA gNB **전 모드 `disabled`**로 3/3 (parity 대조군, C3와 같은 논리) | **라이브 통과 2026-09-25** — cpu/disabled/all 각 1회 attach·ping 통과(Orin 전 가속 첫 통과). all은 D8/D9로 BLER FAIL → J2c(D8+D9) 검증 중. GPU PUSCH p50 1.67 ms(CPU 0.54 ms) |
| **J4** | **단계별 활성화** — `low_phy_rx` → `low_phy_tx` → `pusch` → `pdsch(enabled)` → `prach` → `srs` → `all`, 각 단계를 `ul_cuda_visible_grid_mode` **`auto`(=managed)와 `pinned` 두 벌**로 | 단계마다 3/3 통과 또는 실패 메커니즘. `verify-stage-backends.py`로 selected/degraded/fallback 판정, silent fallback 0. late/dropped slot 0. **managed 런에서 SIGBUS/segfault/`cudaErrorIllegalAddress` 여부를 명시 판정** — 나면 재현 절차·스택·원인 접근 위치 | **1차 2026-09-25** — managed 명시: 5단계 통과(크래시 0). auto(=pinned, D7a): lower-PHY RX 가속 불가로 첫 단계 FATAL(설계된 동작) — 교환 관계 기록 |
| **J5** | **정합 — BLER/SINR** — 트래픽을 실은 페어 런(`c4-bler-run.sh` 방식), 16QAM에 도달하는 직결 fixture | CPU 대비 BLER 차 ≤1%p, SINR 차 ≤0.5 dB. **HANDOVER 이슈 6(가속 PUSCH에서 16QAM/TBS528 CRC 실패율 상승) 재현 여부** — 재현되면 디스크리트 고유가 아니라 PUSCH 경로 문제, 재현 안 되면 플랫폼 의존 | 미착수 |
| **J6** | **측정** — 같은 fixture로 CPU·CUDA 교차 실행 | 슬롯 처리 지연 분포, `OCUDU_PUSCH_ACCELERATION_TIMING` 단계별 타이밍, `tegrastats` GPU/CPU/전력, late/dropped. 전원 모드 두 개 이상(MAXN + 기본). **여기서부터만 Orin에서의 속도 비교 서술 가능** | 미착수 |
| **J7** | **정리·제보** — 결과를 `docs/`에 보고서로, 새 upstream HEAD(`900d8d0e`) 재확인 | 보고서(한/영). upstream HEAD에서 J1–J2 재실행 결과. **이슈 #2 후속 코멘트는 minwoo 승인 후에만** 게시 | 미착수 |
| **J8** | **OAI nrUE 라이브(1×1, 2×2)와 CPU 배치** — `integration-0928`, CPU gNB, zero-copy 브로커(`auto`), 패치 ZMQ 모듈과 패치 UE(MMSE int16 수정) 기본값 | OAI 1×1·2×2가 Orin에서 붙는지, 실시간 배율, 막히는 곳, Orin용 CPU 배치 프로파일이 도움이 되는지 | **완료 2026-09-28** — 1×1 통과(Orin 첫 OAI attach), 배치 프로파일로 0.58–0.60× → **0.80–0.81×**. 2×2 rank 2 통과(NACK 1.1–1.3%, y=Hx 통과) 하지만 **0.13–0.28×**. 30 W에서는 둘 다 실시간 미달 |
| **J9** | **D10 적용(J2d = J2c + D10)** — GB10(S13)에서 찾은 GPU TB 인코더의 filler 0 결함 | 수정 전 재현, 수정 후 PHY 14 + OFH 16, 음성 대조군, 짧은 라이브 CPU 대 CUDA 정합 | **완료 2026-09-28** — 아래 J9 절 |

J2까지가 "Orin에서 벤더 코드가 맞게 도는가", J4가 "managed 가정이 버티는가", J5가 "결과가 맞는가", J6이 "Orin에서 쓸 만한가"다.

## 단계별 상세

### J0 — 플랫폼 준비

J0는 코드를 한 줄도 빌드하지 않는다. 끝나면 "이 Jetson 위에서 J1–J6을 돌릴 수 있고, 그 조건이 기록돼 있다"가 성립해야 한다.

#### J0 시작 시점 상태 (실측 09-24)

| 항목 | 값 | J0 이후 목표 |
|---|---|---|
| 컨테이너 런타임 | containerd 스냅샷(overlay), PID 1 = `sshd -D -e`, 포트 2202 | 동일 |
| 영속 마운트 | `/home/dev`, `/workspace` — 둘 다 호스트 eMMC `mmcblk0p1` 바인드 마운트 | 동일 (재생성해도 남음) |
| 권한 | `CapBnd=0xa80425fb` (도커 기본, `NET_ADMIN` 없음) | `NET_ADMIN`, `SYS_ADMIN`, `SYS_NICE` 추가 |
| `/dev/net/tun` | 확인 안 됨 | 컨테이너에 전달 |
| 전원 모드 | CPU 0–7만 online(8/12), CPU max 1.728 GHz(하드웨어 2.2016), GPU max 612 MHz(하드웨어 1300.5) → **30W 모드로 추정** | MAXN(모드 0), `jetson_clocks` |
| 저장장치 | eMMC 59 GB 하나, NVMe 없음, `/home/dev` 여유 29 GB | 여유 ≥ 15 GB 유지 |
| 의존성 | `libzmq3-dev`, cmake 4.4, gcc 12.3, git, tmux만 | 아래 목록 전부 |

**호스트 접근 (09-24 확인, 갱신):** 호스트 계정은 **`user-a`**(uid 1000, 호스트의 유일한 일반 계정, `sudo` 그룹, **sudo에 비밀번호 필요**, `docker` 그룹 아님 → `docker`도 sudo 필요). 워크스테이션에서 `ssh jetson-host`(키 인증, 호스트 키 `SHA256:XVzmSESV…` 대조 후 등록)로 접속된다. 계정 소유자에게 키 등록 사실을 알릴 것. **Claude는 sudo를 못 쓰므로 sudo 명령은 minwoo가 호스트 터미널에서 실행**한다.

**호스트 구성 (user-a의 `~/SETUP.md`, `~/jetson-dev-image/Dockerfile`):**
- 이 Jetson은 **공용**이다. `ocudu-user-a` 컨테이너(포트 2201, 5559/5560 공개)가 이 워크스테이션과 LAN 직결(`192.168.50.1`↔`.2`)로 **Sionna→채널 에뮬레이터 분산 구성**을 돌린다. 09-24 14:30 기준 GPU 부하 0(미실행).
- 컨테이너 이미지 `ocudu-jetson-dev:latest` = `l4t-jetpack:r36.4.0` + gcc-12 기본 + pip cmake≥3.28 + `dev` NOPASSWD sudo + sshd 비밀번호 인증. **우리 컨테이너도 같은 이미지로 보인다**(확인은 `docker inspect` 필요).
- `/home/dev`, `/workspace`는 **named volume**이다(컨테이너 안에서 `mmcblk0p1`로 보인 이유: 볼륨이 호스트 루트 fs에 있음). 재생성해도 남는다.
- 전원 모드 **`MODE_30W`(ID 2)** — 추정이 맞았다. MAXN으로 바꾸면 user-a의 브로커 측정 조건도 바뀐다 → **변경 전 user-a와 합의**.

#### J0-1. 컨테이너 재생성 (Jetson 호스트, 관리자)

**왜:** srsUE는 netns 안에서 돌고(`ip netns`에 `NET_ADMIN` 필요), Open5GS UPF는 TUN 인터페이스 `ogstun`을 만든다(`/dev/net/tun` + `NET_ADMIN`). gNB 워커 스레드의 실시간 우선순위에는 `SYS_NICE`와 `rtprio` ulimit이 필요하다. 셋 다 실행 중인 컨테이너에 나중에 붙일 수 없으므로 재생성해야 한다.

1. 현재 설정을 먼저 기록한다. 재생성 후 빠진 게 없는지 대조하는 기준이다.
   ```bash
   docker ps -a --format '{{.Names}}\t{{.Image}}\t{{.Status}}'        # 컨테이너 이름 확인 (<name>)
   docker inspect <name> > ~/ocudu-container-before.json
   docker inspect <name> --format '{{json .HostConfig.Binds}} {{json .HostConfig.PortBindings}} {{.HostConfig.Runtime}} {{json .HostConfig.CapAdd}} {{json .HostConfig.Devices}}'
   ```
   compose나 스크립트로 띄웠다면 그 파일을 고치는 것이 정답이다. `nerdctl`이면 명령어만 바꾼다.
2. 기존 설정을 유지한 채 다음을 추가해 다시 만든다.
   ```bash
   docker stop <name> && docker rename <name> <name>-old     # 삭제하지 말고 남겨 둔다
   docker run -d --name <name> \
     <기존 --runtime nvidia / 이미지 / -p 2202:22 / -v 바인드 / 호스트명 / 기타 인자 그대로> \
     --cap-add NET_ADMIN --cap-add SYS_ADMIN --cap-add SYS_NICE \
     --device /dev/net/tun \
     --ulimit rtprio=99 --ulimit memlock=-1 \
     --sysctl net.ipv4.ip_forward=1 \
     <이미지>
   ```
   `--privileged`는 쓰지 않는다. 필요한 권한만 명시해야 J4 이후 문제가 권한 때문인지 가려진다. 위 조합으로 막히는 것이 나오면 그때 원인과 함께 추가한다.
3. `<name>-old`는 J0 exit 게이트를 통과한 뒤에 지운다.

주의: 이미지 레이어에 설치한 것(apt 패키지 등)은 재생성하면 사라진다. **J0-4의 apt 설치는 재생성 뒤에** 하거나, 이미지를 다시 빌드하는 Dockerfile에 넣는다. `/home/dev`와 `/workspace`는 바인드 마운트라 남는다.


**스크립트 (09-24 작성):** `scripts/cuda/jetson/Dockerfile` + `scripts/cuda/jetson/recreate-container.sh`. 호스트에서 `OLD=<컨테이너 이름> bash recreate-container.sh`로 계획만 출력(dry run), `APPLY=1`로 실행한다. 기존 컨테이너의 볼륨·포트·호스트명·restart 정책·nvidia 런타임을 `docker inspect`에서 그대로 옮기고, 위 권한만 추가하며, 기존 컨테이너는 `<이름>-pre-j0`로 남긴다.
- **`SYS_ADMIN`이 필요한 이유:** `ip netns add`는 `unshare(CLONE_NEWNET)` 뒤 `/run/netns`에 bind mount를 한다. 워크스테이션 컨테이너(`~/create-ocudu-minwoo.sh`)도 같은 이유로 넣었다. 처음 목록에서 빠뜨렸다.
- **`--network host`는 쓰지 않는다.** 워크스테이션은 host 네트워크를 쓰지만, 공용 Jetson에서 그렇게 하면 netns·iptables·라우팅 변경이 호스트(= user-a의 LAN 직결 구성)에 닿는다. 기본 bridge에서는 `NET_ADMIN`이 컨테이너 자기 네트워크 안에서만 작동한다.
- 이미지는 user-a의 `ocudu-jetson-dev:latest`를 **기반으로만** 쓴다(원본 Dockerfile 무수정). 의존성 55개는 jammy arm64에 전부 있음을 컨테이너에서 `apt-cache policy`로 확인했다(09-24). `CMD`가 매 기동마다 `/run/netns`를 만든다.
- 재생성 후 `dev` 비밀번호는 다시 설정해야 한다(컨테이너 레이어의 `/etc/shadow`에 있었음). ssh 키(`/home/dev` 볼륨)와 컨테이너 호스트 키(이미지에 포함)는 그대로다.

#### J0-2. 전원·클럭 (Jetson 호스트, 관리자)

```bash
sudo nvpmodel -q --verbose > ~/nvpmodel-before.txt     # 현재 모드 기록
sudo nvpmodel -m 0                                     # MAXN — 재부팅 후에도 유지됨
sudo jetson_clocks                                     # 클럭 고정 — 재부팅하면 풀림
sudo jetson_clocks --show > ~/jetson-clocks-after.txt
```

- MAXN 전환은 CPU 코어를 켜기 위해 **재부팅을 요구할 수 있다**. 요구하면 재부팅 후 `jetson_clocks`를 다시 실행한다.
- 발열: MAXN + `jetson_clocks`는 팬을 최대로 돌린다. 방열판·팬 상태 확인.
- **J6에서는 30W 모드도 측정**하므로 모드 번호와 이름(`nvpmodel -q`)을 매 런 라벨로 남긴다. 이것 때문에 J0에서 "원래 모드가 무엇이었는지"를 기록해 두는 것이다.
- 벤더의 `scripts/ocudu_performance`는 CPU governor를 performance로 바꾸는 스크립트인데, Jetson에서는 `jetson_clocks`가 같은 일을 하므로 쓰지 않는다.

#### J0-3. 작업 디렉터리와 디스크 예산 (컨테이너, 여기서 ssh로)

작업 루트는 영속 마운트인 `/workspace` 아래에 둔다.

```
/workspace/ocudu-cuda-rebuild/        이 레포 (git bundle로 전송, 브랜치 cuda-rebuild)
/workspace/ocudu-jetson/
  src/      ocudu-cuda(5830c9cb), ocudu(a1916edc, CPU 기준선), srsRAN_4G(eea87b1d), open5gs(d9d3abdd)
  builds/   c1-cuda-patched-sm87 등
  install/  open5gs, srsran4g, mongodb
  tools/    다운로드 캐시
  results/  j0-…, j1-… 증거 (Git 밖)
```

| 항목 | 예상 크기 | 근거 |
|---|---:|---|
| apt 패키지 | ~1.5 GB | 워크스테이션 오버레이 94개 기준 |
| 벤더 소스 + CUDA 빌드(테스트 포함) | ~3 GB | 5090 빌드 849 MB, `BUILD_TESTING=ON` 여유 |
| CPU OCUDU 소스 + 빌드 | ~2 GB | |
| srsRAN_4G + Open5GS + MongoDB | ~2 GB | |
| 결과·로그 | ~2 GB | IQ 캡처는 J0–J5에서 안 함 |
| **합계** | **~10 GB** | 여유 29 GB → 끝나도 ~19 GB |

J0 게이트는 이 예산을 확인만 하고, 실제 사용량은 단계마다 `df`로 기록한다. 여유가 10 GB 아래로 내려가면 다음 단계 전에 정리한다.

#### J0-4. 의존성 (컨테이너, 재생성 이후)

워크스테이션 네이티브 워크스페이스의 데비안 오버레이 목록(`scripts/native/locks/ubuntu-noble-amd64.debs.lock.tsv`, 94행)을 **Ubuntu 22.04(jammy) arm64 패키지 이름으로 옮긴 것**이다. 워크스테이션은 sudo 없이 user-space sysroot에 풀었지만, Jetson 컨테이너는 sudo가 되므로 시스템 apt로 설치한다. `t64` 접미사는 noble 전용이라 jammy에서는 뺀다.

```bash
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
  ninja-build iproute2 iputils-ping pkg-config \
  autoconf automake libtool bison flex m4 meson gettext \
  libfftw3-dev libmbedtls-dev libyaml-cpp-dev libsctp-dev libboost-program-options-dev \
  libgtest-dev googletest libconfig++-dev libzmq3-dev \
  libgnutls28-dev libgcrypt20-dev libssl-dev libidn-dev libtalloc-dev libyaml-dev \
  libmicrohttpd-dev libcurl4-gnutls-dev libnghttp2-dev libtins-dev libmongoc-dev libbson-dev \
  libpcap-dev libnl-3-dev libnl-route-3-dev libdbus-1-dev libsnappy-dev libzstd-dev zlib1g-dev \
  python3-pymongo python3-yaml
```

- **MongoDB (Open5GS용):** 워크스테이션은 `mongodb-linux-x86_64-ubuntu2204-6.0.29.tgz`를 sha256 사이드카와 함께 쓴다. Jetson은 같은 버전의 **`mongodb-linux-aarch64-ubuntu2204-6.0.29.tgz`**를 fastdl에서 받아 사이드카로 검증하고 `install/`에 푼다. MongoDB aarch64는 ARMv8.2-A 이상을 요구하는데 A78AE는 충족한다.
- **gnutls 3.7.3 / bison 3.8.2:** 워크스테이션은 소스 빌드했는데, 그 이유는 noble 쪽 사정이었다. jammy의 시스템 gnutls(3.7.3)로 Open5GS가 빌드되는지 J3에서 먼저 보고, 안 될 때만 같은 소스 아카이브를 쓴다.
- **nsys:** J0에서는 설치하지 않는다. J6에서 필요하면 JetPack의 Nsight Systems(Jetson용)를 넣는다.
- 설치가 끝나면 `dpkg-query -W`로 **설치된 버전 목록**을 결과에 저장한다.

#### J0-5. 레포 전송

`origin`이 워크스테이션 로컬 트리라 Jetson에서 직접 클론할 수 없다.

```bash
# 워크스테이션
git -C ~/ocudu-work/ocudu-cuda-rebuild bundle create /tmp/cuda-rebuild.bundle cuda-rebuild
scp /tmp/cuda-rebuild.bundle jetson-minwoo:/workspace/
# Jetson
git clone -b cuda-rebuild /workspace/cuda-rebuild.bundle /workspace/ocudu-cuda-rebuild
```

벤더 소스는 Jetson에서 GitLab으로 직접 받는다(연결 확인됨, 09-24): `git clone -b nvcuda_accel_02 … && git checkout 5830c9cb`.

#### J0-6. 플랫폼 프로브 스크립트 (신규 `scripts/cuda/jetson/probe-platform.sh`)

J1 이후 모든 결과 디렉터리에 같은 JSON을 넣기 위한 것이다. 수집 항목:

- `/etc/nv_tegra_release`, `/proc/device-tree/model`, `uname -r`
- `nvpmodel -q` 결과는 컨테이너 안에서 못 얻으므로, 대신 **sysfs에서 실제 값을 읽는다**: online 코어(`/sys/devices/system/cpu/online`), CPU `scaling_max_freq`·`cur_freq`, GPU devfreq(`/sys/class/devfreq/17000000.gpu/{cur,max}_freq`). 모드 이름은 J0-2에서 호스트가 적은 값을 인자로 받는다.
- CUDA: `nvcc --version`, 드라이버 버전, 작은 `.cu` 프로브로 `integrated`, `ConcurrentManagedAccess`, `PageableMemoryAccessUsesHostPageTables`, `DirectManagedMemAccessFromHost`, SM 수, compute capability
- 권한: `CapEff`/`CapBnd`, `ip netns add`/`del` 시도 결과, `/dev/net/tun` 존재, `ulimit -r`
- 디스크: `df -B1 /workspace /home/dev`

출력: `/workspace/ocudu-jetson/results/j0-<UTC>/platform.json`.

#### J0-7. lock 파일 (신규 `scripts/cuda/cuda-workspace.jetson.lock.json`)

5090용 `cuda-workspace.lock.json`과 같은 스키마로, 값만 Jetson에 맞춘다: 벤더 커밋 `5830c9cb`, 패치 경로·sha256(`d2579af2`, 5090과 동일), `build_dir` `builds/c1-cuda-patched-sm87`, `cuda_architectures` `"87"`, 그리고 `platform` 블록(L4T R36.4.4, CUDA 12.6.68, aarch64). 기존 lock 파일은 건드리지 않는다.

#### J0 Exit 게이트

전부 `platform.json`과 J0 결과 디렉터리의 로그로 증명한다.

1. `sudo ip netns add j0probe && sudo ip netns del j0probe` → exit 0
2. `/dev/net/tun` 존재, `ulimit -r` = 99
3. online CPU = `0-11`, GPU `max_freq` = 1300500000 (MAXN + `jetson_clocks`)
4. CUDA 프로브: `integrated=1`, `sm_87`, 네 속성 값 기록 (09-24 값과 다르면 J1 전에 원인 확인)
5. 의존성 설치 목록(`dpkg-query`) 저장, `ninja --version` 성공
6. `/workspace` 여유 ≥ 15 GB
7. 레포가 Jetson에서 `cuda-rebuild` HEAD와 같은 커밋
8. 이전 컨테이너 설정(`ocudu-container-before.json`)과 새 설정의 차이가 J0-1에서 추가한 항목뿐

#### J0 결과 — 2026-09-24

호스트 계정 `minwoo`(uid 1001, `sudo` 비밀번호 필요, `docker` 그룹, `nvpmodel`·`jetson_clocks`만 NOPASSWD)를 user-a 계정으로 만들었다. 워크스테이션 `ssh jetson-host`는 이제 `minwoo`로 접속한다. user-a 계정에 등록했던 워크스테이션 키는 필요 없어졌으므로 지울 것.

컨테이너 재생성: 호스트 `~/j0/`에서 `OLD=ocudu-minwoo APPLY=1 bash recreate-container.sh`, exit 0. 이미지 `ocudu-jetson-minwoo:j0`(베이스 `ocudu-jetson-dev:latest` = `sha256:ea9c5994…`). 이전 설정 백업 `~/j0/ocudu-minwoo-before-j0.json`(sha256 `9a48a063…`), 이전 컨테이너는 `ocudu-minwoo-pre-j0`(정지). 재생성 전 `docker diff`로 볼륨 밖 변경이 nvidia 런타임 주입 파일과 `/etc/shadow`뿐임을 확인했다. `ocudu-user-a`는 계속 `Up 5 days`.

| # | 게이트 | 결과 |
|---|---|---|
| 1 | `ip netns add` → netns 안 `lo` ping → `del` | ✅ `NETNS_OK` |
| 2 | `/dev/net/tun`, `ulimit -r` 99 | ✅ tun 생성·삭제 `TUN_OK`, rtprio 99, memlock unlimited, `ip_forward=1` |
| 3 | 12코어 online, GPU max 1.3 GHz | ❌ **`MODE_30W` 유지** — MAXN은 user-a 합의 후. J1–J5는 30W로 진행 가능 |
| 4 | CUDA 프로브 | ✅ `Orin sm_87 integrated=1 SMs=8`, CMA/hostPT/directMgd `0/0/0` (09-24 첫 측정과 동일) |
| 5 | 의존성 | ✅ 이미지에 포함, `ninja 1.10.1`, 설치 패키지 838개 버전 목록 `dpkg.tsv` |
| 6 | `/workspace` 여유 ≥ 15 GB | ✅ 27 GB (소스·MongoDB 받은 뒤) |
| 7 | 레포 전송 | ✅ `git bundle` → `/workspace/ocudu-cuda-rebuild` HEAD `a2479069` = 워크스테이션 `cuda-rebuild`. **J 트랙 신규 파일은 아직 미커밋이라 tar로 따로 복사했다** — 커밋 후 다시 맞출 것 |
| 8 | 설정 차이가 추가 항목뿐 | ✅ `CapAdd` NET_ADMIN/SYS_ADMIN/SYS_NICE, `/dev/net/tun`, ulimit 2개, sysctl 1개, 네트워크 `bridge`; 볼륨·포트·호스트명·restart 동일 |

소스·아카이브(`scripts/cuda/jetson/`이 아닌 일회성 `tools/j0-fetch.sh`, 로그 `results/j0-fetch.log`): `ocudu-cuda` `5830c9cb`, `ocudu` `a1916edc`, `srsRAN_4G` `eea87b1d`, `open5gs` `d9d3abdd` — 전부 lock 커밋과 일치. MongoDB `mongodb-linux-aarch64-ubuntu2204-6.0.29.tgz` 공식 sha256 사이드카 검증 OK(`81003080…`), `mongod --version` = `v6.0.29`로 A78AE에서 실행 확인. Open5GS meson 서브프로젝트(freeDiameter 등)는 J3 빌드 때 받는다.

플랫폼 기록: `results/j0-20260924T075207Z/platform.json` + `dpkg.tsv` (`scripts/cuda/jetson/probe-platform.sh`, `NVP_MODE`는 워크스테이션에서 `ssh jetson-host sudo -n nvpmodel -q`로 넘김). Jetson lock: `scripts/cuda/cuda-workspace.jetson.lock.json`.

컨테이너 ssh 호스트 키는 이미지에 들어 있어 바뀌지 않았다(`known_hosts` 경고 없음). `dev` 비밀번호는 다시 설정해야 한다(`docker exec -it ocudu-minwoo passwd dev`, 호스트).

### J1 — 벤더 원본

```bash
cmake -S <wg-src> -B <build> -G Ninja -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=ON \
  -DENABLE_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=87 -DENABLE_ZEROMQ=ON
```

- 먼저 벤더 CMake·문서에서 **`sm_87`/CUDA 12.6 지원 선언 여부**를 확인한다. 문서의 아키 표에 87이 없으면 그 사실 자체가 기록 대상.
- 빌드 병렬도는 메모리로 제한(nvcc 프로세스당 수 GB) — OOM으로 죽으면 `-j` 낮춤을 기록.
- ctest는 C0와 같은 정규식, `-j1`. C0에서 `pusch_gpu_cpu_comparison_test`가 5090에서 875초였다 — Orin에서는 더 길 수 있으니 `--timeout`을 넉넉히, tmux에서.
- **C0 교훈 적용:** OFH 테스트가 `_NOT_BUILT` 플레이스홀더만 매칭되어 아무것도 안 돌고 green이 되는 경우(`gtest_discover_tests` POST_BUILD)를 배제 — 등록된 테스트 개수를 기록한다.

#### J1 결과 (진행 중) — 2026-09-24

실행: `scripts/cuda/jetson/j1-build-and-validate.sh`, 증거 `results/j1-20260924T075419Z/`. 전원 `MODE_30W`.

**빌드: 패치 없이 성공.** configure → gNB+`ocudu_phy_cuda` 1434 타깃 **34분**(`-j6`, 메모리 여유 52 GB 유지) → 테스트 타깃 1분. gNB `f740cb4f…`, `26.04 (5830c9cb78)`, `--help`에 `GPU acceleration:` 있음. **벤더 코드는 CUDA 12.6 / `sm_87`에서 컴파일된다** — WG 문서의 아키 목록에 87은 없지만 막히는 것은 없었다. PHY 12개 등록.

| # | 테스트 | 결과 |
|---|---|---|
| 1 | `ofdm_demodulator_cuda_test` | **Aborted** — `Accelerated lower-PHY RX demodulation failed.` (5.9 s) |
| 2 | `ofdm_prach_demodulator_cuda_test` | **Aborted** — `Accelerated lower-PHY PRACH demodulation failed.` (25.9 s) |
| 3–6 | `pdxch_baseband_modulator` / `ldpc_encoder` / `ldpc_decoder` / `prach_detector` | Passed |
| 7 | `pusch_gpu_cpu_comparison_test` | Passed, 3450.4 s — **아래 진단 실행과 GPU를 공유**했으므로 WG·5090(875.5 s) 시간과 비교 불가 |
| 8 | `pdsch_gpu_e2e_test` | **SegFault** (0.43 s) — `PdschGpuIssueProfile.ManagedGridMatchesHostAcrossHarqRvsAndRepeatedSlots` |
| 9 | `pusch_e2e_pipeline_test` | Passed, 71.2 s |
| 10 | `pusch_resident_dematch_scramble_test` | Passed, 0.17 s |
| 11 | `srs_estimator_gpu_latency_baseline_4x4_n4` | **Aborted** (0.35 s) |
| 12 | `srs_estimator_gpu_sensitivity_baseline_4x4_n4` | **Aborted** (0.35 s) — `srs_estimator_gpu_benchmark_helpers.h:510` RX 포트 수 불일치(5090 C0의 D1과 같은 assertion), 단 `grid=visible`(managed) |

**PHY 7/12 (`phy_ctest_exit=8`, 3575 s), OFH 14/16 (`ofh_ctest_exit=8`), 로그 판정기 `phy_log_verdict=fail`.** OFH 16개 등록(5090과 같음). 5090 C0는 PHY 9/12, OFH 16/16이었다.

**중단 시점 상태 (09-24, minwoo 지시로 Spark 작업으로 전환):**
- OFH 실패 2건(`cuda_direct_grid_transient_failure_retries_on_fresh_handle/*`): D6 완화(그리드·PRACH prefetch 끔 + shim) 하에서 **4/4 통과** → D6으로 설명됨. 상세 메커니즘은 미기록.
- SRS 2건: **미해결.** 무처리에서 latency는 SIGSEGV(rc 139), sensitivity는 D1 assertion(rc 134). D6 완화 하에서는 **둘 다 SIGSEGV**. 5090의 D1(pinned 그리드에 snapshot API 없음)과 달리 여기서는 managed 그리드(`grid=visible`)를 탄다 — CMA=0 크래시 경로일 가능성이 높으나 추적 전.
- J1 남은 일: SRS 추적, OFH 메커니즘 기록, 테스트 7 시간 재측정(진단과 GPU 공유 없이).

##### D6 — `ConcurrentManagedAccess=0`에서 managed 메모리 힌트 API가 `cudaErrorInvalidDevice`(101)를 반환하는데 벤더 코드가 이를 실패로 처리하거나 지우지 않는다

gdb(`FinishBreakpoint`로 단계별 반환값)와 `compute-sanitizer --report-api-errors all`로 추적했다. **소스 무수정.** 두 테스트 모두 테스트 코드가 `OCUDU_UL_CUDA_VISIBLE_GRID=managed`와 가속 `enabled`를 강제하므로 auto 정책과 무관하게 managed 경로를 탄다.

세 지점이 같은 뿌리다 — Orin에서 `cudaMemPrefetchAsync`와 `cudaMemAdvise`가 **`cudaErrorNotSupported`가 아니라 `cudaErrorInvalidDevice`(101, "invalid device ordinal")**를 반환한다.

1. **UL 그리드 prefetch.** `phy_acceleration_runtime_options.h:151` `prefetch_by_default = !fully_coherent` → Orin에서 참. `resource_grid_cuda_visible_impl.h` `prefetch_managed_memory()`는 `cudaErrorNotSupported`만 "unsupported"로 보고 나머지는 `failed` → `prepare_device_mapping()` false → `ofdm_demodulator_cuda_impl.cpp:184` fatal. 벤더 주석(`phy_acceleration_runtime_options.h:123-124`)이 바로 이 가정을 적어 두었다: *"`cudaMemPrefetchAsync` returning NotSupported is a later no-op, so turning prefetch on by default is safe on parts that cannot migrate."* Orin은 NotSupported를 반환하지 않는다.
2. **PRACH 버퍼 prefetch.** `prach_buffer_cuda_visible_impl.h:81-84`가 `PageableMemoryAccessUsesHostPageTables==0`이면 prefetch를 켠다(Orin 0) → 같은 분류 로직(413행) → `prepare_device_prach_buffer_mapping()` false. UL 그리드와 **별도의 스위치**(`OCUDU_CUDA_VISIBLE_PRACH_BUFFER_PREFETCH`)다.
3. **`cudaMemAdvise` 오류가 남는다.** 그리드·PRACH 버퍼 생성자의 `advise_managed_memory()`는 `(void)cudaMemAdvise(...)`로 "best-effort"라며 무시하지만 **`cudaGetLastError()`로 지우지 않는다.** 이 101이 남아 있다가, 관계없는 다음 `check_cuda(cudaGetLastError())` — 다중 포트·배치 경로에서는 `low_phy_puxch_rx.cu` `ensure_window_phase()`의 커널 실행 직후 — 에서 걸려 핸들 설정이 실패한다. 단일 포트 경로는 그 사이에 다른 무검사 `cudaGetLastError()`가 먼저 오류를 소비해서 우연히 통과한다.

**대조 실험 (소스 무수정, 진단용 LD_PRELOAD shim `tools/diag/clear_memadvise.so` = 실패한 `cudaMemAdvise` 뒤 `cudaGetLastError()` 호출):**

| 테스트 | 무처리 | prefetch 끔만 | shim만 | 둘 다 |
|---|---|---|---|---|
| `ofdm_demodulator_cuda_test` | FATAL | FATAL (5개 중 단일 포트 2개만 통과) | FATAL | **5/5 PASSED** |
| `ofdm_prach_demodulator_cuda_test` (prefetch 끔 = 그리드+PRACH 버퍼 둘 다) | FATAL | FATAL | FATAL | **5/5 PASSED** |

→ 세 지점이 **전부** 필요하고 **그것으로 충분하다.** 수정 방향(J2): prefetch·advise의 반환이 `cudaErrorInvalidDevice`이고 장치가 `ConcurrentManagedAccess=0`이면 "unsupported"로 분류하고 오류를 소비한다. 또는 CMA=0이면 prefetch 기본값을 끈다. 벤더 표의 "older Jetson … auto uses pinned" 행(117–121)과 실제 `should_use_managed_grid_auto()`의 불일치(위 §Orin)도 같은 제보에 묶는다.

##### D6의 결과로 나는 크래시 — `pdsch_gpu_e2e_test` SIGSEGV (09-24)

**J4에서 걱정한 "CMA=0에서 CPU가 managed 페이지를 건드리면 죽는다"가 벤더 테스트 안에서 실제로 재현됐다.** gdb 백트레이스: `map_ci8_layer_to_port` ← `resource_grid_mapper_impl::map` ← `pdsch_processor_flexible_impl::sync_pdsch_cb_processing`, 명령은 `sturh`(managed 그리드에 bf16 저장). `compute-sanitizer`로 본 연결고리:

1. PDSCH 매퍼가 GPU 쓰기를 위해 `prepare_device_grid_mapping()` 호출 → **D6 지점 1의 prefetch 101** → false
2. 매퍼가 **호스트 매핑으로 폴백**해 CPU가 managed 그리드에 직접 쓴다
3. 그 순간 같은 컨텍스트의 PDSCH TB 인코더 커널(`pdsch_block_processor_gpu_impl::process_batch_gpu`)이 GPU에서 실행 중 → CMA=0 장치에서는 커널 실행 중 CPU의 managed 접근이 허용되지 않음 → SIGSEGV

같은 실행에서 생성자의 `cudaMemAdvise` 101이 지워지지 않고 남아 `gold_seq_get_or_generate`의 `cudaGetLastError()`에서 소비되는 것도 보인다(D6 지점 3).

| 대조 (이 테스트 하나) | 결과 |
|---|---|
| 무처리 | SIGSEGV (rc 139) |
| shim만 | SIGSEGV |
| **그리드 prefetch 끔만** | 세그폴트 없음 → `pdsch_gpu_e2e_test.cpp:746` `compress_device_symbol()` false = **5090 C0의 D2와 같은 지점** |
| prefetch 끔 + shim | 746 동일 |
| `CUDA_LAUNCH_BLOCKING=1` | 세그폴트 없음, 대신 GPU 그리드가 0(`nof_mismatches=45792`) — 실제 구성이 아니므로 참고만 |

→ 크래시는 D6 지점 1이 원인이고, 그것을 막으면 이 테스트는 5090과 같은 D2 실패로 수렴한다. D2는 C1 패치(`d2579af2`)에 이미 수정이 있다. 따라서 J2에서 기대하는 것은 **C1 패치 + D6 수정**으로 통과.

**의미:** 호스트 폴백 자체가 CMA=0에서는 안전하지 않다. 5090(CMA=1)에서는 GPU 경로가 거부돼도 CPU 폴백이 정상 동작했지만, Orin에서는 폴백이 크래시로 바뀐다. D6 수정이 prefetch 오류만 고치면 이 크래시는 사라지지만, **다른 이유로 GPU 매핑이 거부되는 순간 같은 크래시가 다시 날 수 있다** — J4에서 라이브 부하로 확인할 항목.

부수 관찰: 워밍업 중 `cudaGraphKernelNodeGetParams`가 `cudaErrorInvalidDeviceFunction`(98)을 반환하고 그 오류가 다음 `cudaGetLastError()`에서 소비된다(`low_phy_puxch_rx.cu` `enqueue_puxch_demod_ci16`). 워밍업이 흡수하므로 테스트 결과에 영향이 없고, Orin 고유인지는 미확인(5090에서 같은 추적을 안 했다). 같은 "오류를 지우지 않는 best-effort 호출" 패턴이다.

### J2 — C1 + Orin 레이어(D6 수정) 결과 (2026-09-24 실행, 09-25 판정)

실행: `scripts/cuda/jetson/j2-patched-validate.sh`. 소스는 핀 + C1 커밋(`35e206b504`) + `j2-ocudu-cuda-orin.patch`(sha256 `e855164b`)이고, 증거는 `results/j2-20260924T140018Z/`에 있다. 전원 `MODE_30W`.

**PHY 13/14 (`phy_ctest_exit=8`, 3704 s), OFH 16/16, 로그 판정기 `phy_log_verdict=pass`.** J1(벤더 원본)은 PHY 7/12, OFH 14/16이었다.

| 테스트 | J1 | NC-D6 (C1만) | J2 (C1 + D6 수정) |
|---|---|---|---|
| `ofdm_demodulator_cuda_test` | Aborted | **Aborted** | Passed |
| `ofdm_prach_demodulator_cuda_test` | Aborted | **Aborted** | Passed |
| `pdsch_gpu_e2e_test` | SegFault (`ManagedGridMatchesHost…`) | **SegFault (같은 케이스)** | **SegFault (`BackToBackManagedGridSubmissions…`) → D7** |
| `srs_estimator_gpu_latency/sensitivity` | Aborted ×2 | **Passed ×2** | Passed ×2 |
| OFH `…retries_on_fresh_handle/*` | Failed ×2 | **Failed ×2** | Passed ×2 |
| 나머지 (lower-PHY TX, LDPC, PRACH, PUSCH 4종) | Passed | Passed | Passed (`pusch_gpu_cpu_comparison_test`는 NC에서 제외) |

#### NC-D6 — 음성 대조군 (2026-09-25, `results/nc-d6-20260925T033200Z/`)

`scripts/cuda/jetson/j2-nc-d6.sh`는 J2와 같은 베이스 커밋을 J2 diff 없이 별도 체크아웃(`src/ocudu-cuda-nc6`)에서 빌드하고, `pusch_gpu_cpu_comparison_test`를 뺀 13개와 OFH를 돌린다. **PHY 10/13, OFH 14/16.** J2가 고친 lower-PHY 2건, PDSCH, OFH 2건이 모두 J1과 같은 방식으로 다시 실패한다. 따라서 이 다섯 건을 고친 것은 C1이 아니라 J2 diff다.

**SRS 2건은 정정한다.** C1만으로도 통과하므로 D6 때문에 난 실패가 아니었다. J1의 실패는 5090 D1(C1에 수정 있음)과 같은 메커니즘이었다. J1에서 "D6 완화 하에서도 SIGSEGV"였던 것은 C1 없이 D1 경로를 탔기 때문으로 보이지만, 개별 추적은 하지 않았다. "Orin SRS 크래시 미해결"은 닫는다.

##### D7 — CMA=0에서 GPU 작업이 도는 동안 CPU가 managed DL 그리드에 쓴다 (`pdsch_gpu_e2e_test`, 09-25)

**증상:** `PdschGpuIssueProfile.BackToBackManagedGridSubmissionsPreserveEveryTransportBlock`에서 SIGSEGV가 3/3으로 난다. 같은 바이너리의 나머지 56케이스는 통과한다.

**gdb:** 스택은 `resource_grid_writer_impl::put` ← `resource_grid_mapper_impl::map` ← `dmrs_pdsch_processor_impl::map` ← `pdsch_processor_flexible_impl::map_reference_signals` ← `sync_pdsch_cb_processing`이다. 명령은 managed 그리드에 대한 `str`다. 즉 **호스트가 DMRS를 managed 그리드에 쓰는 중에** 죽는다.

**테스트가 하는 일:** 그리드를 `OCUDU_DL_CUDA_VISIBLE_GRID=managed`로 강제하고, GPU PDSCH 12건을 **사이에 동기화 없이** 연달아 제출한다. `wait_for_completion()`은 GPU 완료를 기다리지 않는다. 그리드를 읽을 때 기록된 이벤트를 기다리는 구조라서, i+1번째 제출에서 CPU가 DMRS를 쓸 때 i번째 PDSCH 커널이 아직 돌고 있다. CMA=0 장치에서는 커널이 실행 중일 때 CPU가 managed 메모리에 접근하는 것이 허용되지 않는다. 그 메모리를 커널이 쓰지 않아도 마찬가지다(`cudaMemAttachGlobal` 기본값).

**대조:** `CUDA_LAUNCH_BLOCKING=1`에서는 **3/3 통과**, 기본에서는 **3/3 SIGSEGV**다. 이 차이는 커널을 동기 실행하게 만든 것뿐이다.

**D6과의 관계:** 별개 결함이다. D6 수정으로 앞 케이스(`ManagedGridMatchesHost…`)의 prefetch 실패 → 호스트 폴백 경로가 사라지자, 다음 케이스에서 이 경로가 드러났다. J1 절의 "호스트 폴백 자체가 CMA=0에서 안전하지 않다"는 우려가 폴백이 아닌 정상 경로(호스트 DMRS 매핑)에서 실현된 것이다.

**라이브 영향:** `should_use_managed_grid_auto()`는 `cudaDevAttrIntegrated`만 보므로 **Orin의 auto는 managed다**(J1 절에서 소스로 본 어긋남). 따라서 J4에서 PDSCH를 켜고 슬롯이 파이프라인으로 겹치면 같은 크래시가 날 수 있다. 실행으로는 확인하지 않았다.

**수정 방향(09-25 J2b로 구현, 아래 J2b 절):**
1. **정책:** `auto`는 `integrated && ConcurrentManagedAccess`일 때만 managed로 한다. 벤더 주석 표의 "older Jetson → pinned"와 맞추는 것이다. 라이브 기본값을 고치는 최소 변경이다.
2. **명시적 managed + CMA=0:** 호스트가 쓰기 전에 문맥의 진행 중 GPU 작업을 기다리거나(정확하지만 파이프라인이 사라진다), 거부하고 pinned로 내린다. 이 테스트는 managed를 강제하므로 1번만으로는 통과하지 않는다. 테스트가 CMA=0에서 skip할지, 라이브러리가 동기화할지 정해야 한다.

#### J2b — D7 수정 포함 (2026-09-25, `results/j2b-20260925T035922Z`)

패치 `scripts/cuda/patches/j2b-ocudu-cuda-orin.patch`(sha256 `f7e3b246`)는 C1 커밋 위의 diff로, J2 diff(D6) + D7 수정이다. 별도 체크아웃 `src/ocudu-cuda-j2b`와 빌드 `builds/j2b-cuda-patched-sm87`을 쓴다. 실행은 `J2_TAG=j2b J2_PATCH=… j2-patched-validate.sh`다(스크립트가 태그와 패치를 받도록 일반화했고, 기본값은 J2 그대로다).

D7 수정 두 가지(`resource_grid_cuda_visible_impl.h`):
1. `should_use_managed_grid_auto()`: `integrated && ConcurrentManagedAccess`일 때만 managed를 고른다. 이제 Orin의 auto는 pinned이고, 벤더 정책표의 "older Jetson → pinned"와 맞는다.
2. `pull_pages_home_locked()`: CMA=0이면 호스트 접근 전에 `cudaDeviceSynchronize()`를 호출한다. 이 그리드의 이벤트만으로는 부족하다. 명시적으로 managed를 요청한 경우의 정확성 보장이고, 대신 그 경우 DL 파이프라인이 직렬화된다.

**결과: PHY 14/14, OFH 16/16, 로그 판정 pass.** Orin에서 벤더 PHY 스위트가 처음으로 전부 통과했다. `pusch_gpu_cpu_comparison_test`는 3785 s가 걸렸다(J3 스택 빌드와 CPU를 나눠 썼다).
- `pdsch_gpu_e2e_test` 단독 3회: 57/57 ×3.
- 음성 대조군은 J2 빌드(D7 수정 없음)다. 같은 케이스에서 SIGSEGV 3/3이 나고, `CUDA_LAUNCH_BLOCKING=1`이면 통과한다(위 D7 절).

### J3 — 스택과 라이브 준비 (2026-09-25)

스택은 `s3-build-stack.sh`로 빌드했다(`STACK_MCPU=cortex-a78ae STACK_MARCH=armv8.2-a S3_BUILD_JOBS=4`, `results/s3-stack-20260925T043519Z`). lock은 `cuda-workspace.jetson-live.lock.json`(CUDA gNB = J2b 빌드, CPU gNB = `a1916edc`)이다. 명령은 다음과 같다.

```bash
L=/workspace/ocudu-cuda-rebuild/scripts/cuda/cuda-workspace.jetson-live.lock.json
ATTACH_SECONDS=400 PLATFORM_ROOT=/workspace/ocudu-jetson SPARK_LOCK=$L S5_ARMS="cpu disabled all" \
  bash /workspace/ocudu-cuda-rebuild/scripts/cuda/spark/s5-arms.sh 1 60
```

라이브까지 차례로 막힌 것들(모두 해결):

| # | 증상 | 원인 | 해결 |
|---|---|---|---|
| 1 | 가입자 등록 실패 | 컨테이너에 `python3-click` 없음 | 설치, Dockerfile에 추가 |
| 2 | gNB N2 `Failed to create SCTP socket` | L4T 커널이 `CONFIG_IP_SCTP` 없이 빌드됨. 모듈도 없음 | **외부 모듈 `sctp.ko`**(`scripts/cuda/jetson/sctp-oot/prepare-sctp-oot.py`). 이 커널의 `struct net`에는 `sctp` 필드가 없어서, 네임스페이스별 상태를 `net_generic()` 저장소로 옮겼다(193곳, 통계 매크로 93곳, 타이머 역포인터, sysctl 템플릿). 호스트에서 `insmod`로만 올렸다(재부팅하면 사라짐, minwoo가 user-a와 합의 후 적재, 09-25). 컨테이너와 새 netns에서 bind/listen/connect 확인 |
| 3 | gNB `Connection refused` (AMF) | 러너가 NRF SBI 포트만 기다리고 gNB를 띄움. Orin에서는 AMF가 늦게 뜸 | 러너가 AMF의 `ngap_server()` 로그를 기다리게 함 |
| 4 | AMF/SMF/UPF/PCF가 `main` 전에 SIGILL | 컨테이너 PATH의 **CMake 4.4.3**으로 meson이 prometheus CMake 서브프로젝트를 변환하면 `link_args: ['-shared']`가 새어 들어간다. 네 데몬이 진입점 0인 공유 객체로 링크됨 | meson native file로 CMake를 `/usr/bin/cmake`(3.22)에 고정(`s3-build-stack.sh`, 3.x 아니면 중단). 처음에는 meson 0.61을 의심해 1.3.2로도 빌드했지만 원인이 아니었다 |
| 5 | RRC 접속 없음(`Waiting PHY to initialize`) | Orin(MODE_30W)에서 srsUE의 PHY 초기화가 **201 s**. 러너의 접속 창은 60 s. gNB 없이 띄운 srsUE는 SIGINT로 멈추지 않아 wisdom을 저장하지 못한다 | 러너에 `OCUDU_CUDA_DIRECT_ATTACH_SECONDS`(래퍼 `ATTACH_SECONDS`) 추가. Jetson은 400 |

관찰(수정 안 함):
- `all`에서 gNB 기동이 **78 s**다. PUSCH GPU 처리 핸들 11개가 각각 최대 크기(273 PRB, 256QAM) 캐시를 미리 만드느라 약 7 s씩 걸린다. Spark에서는 5 s다.
- 렌더러가 `all`에서 UL 그리드를 `managed`로 명시한다. Orin에서는 D7 수정의 장치 전체 동기화 경로를 탄다. J4에서 auto(=pinned)와 비교한다.
- Jetson의 `nvidia-smi --query-compute-apps`는 `[N/A]`라서 `gpu_others_at_start=1`은 의미가 없다.

#### J3 첫 라이브 — Orin에서 전 가속 attach 성공 (2026-09-25, `results/s5-arms-20260925T063415Z`)

CUDA gNB는 J2b(C1 + D6 + D7, D8/D9 없음)이고, 조건마다 1회, 트래픽 60 s, 접속 창 400 s다.

| 조건 | 결과 | gNB 기동 | 백엔드 | 합산 BLER | PHY `t` p50 / p99 / max |
|---|---|---|---|---|---|
| `cpu` (a1916edc) | **통과**, keepalive 299 | 2 s | — | 0.24% | 540 / 1290 / 3355 µs |
| `disabled` | **통과**, keepalive 299 | 1 s | OK | 0.24% | 548 / 1306 / 2034 µs |
| `all` | **통과**, keepalive 299 | 78 s | **OK**(low-phy-rx/tx, PRACH, PDSCH, PUSCH, SRS 모두 CUDA) | **1.60%** | **1666 / 3531 / 4131 µs** |

- **Orin에서 모든 가속을 켠 CUDA gNB가 라이브 attach/PDU/ping을 처음으로 통과했다.** D7 수정 경로(UL `managed` 그리드, CMA=0 장치 전체 동기화)도 크래시 없이 돌았다.
- `disabled` vs `cpu`: ΔBLER −0.02%p, ΔSINR −0.00 dB → PASS.
- `all` vs `cpu`: ΔBLER +2.80%p(16QAM `[0,17)` 0.89% vs 3.69%) → FAIL. `s5-la-trace.py`로 보면 **Spark C1과 같은 모양**이다. 1 PRB SINR 10.0 dB 보고(D8)가 17/15 PRB 선택을 부르고, 16QAM `[0,17)`에서 실패가 난다(D9). D8/D9가 하드웨어와 무관한 벤더 결함이라는 뜻이다. D8/D9를 포함한 **J2c**(`j2c-ocudu-cuda-orin-d8-d9.patch`)를 검증 중이다.
- **실시간 여유**: Orin(MODE_30W)의 GPU PUSCH 처리 시간은 CPU의 3배(p50 1.67 ms)이고, p99 3.5 ms는 15 kHz 슬롯(1 ms)을 크게 넘는다. gNB 로그에 late/underflow/overflow는 0건이다. 하지만 **ZMQ 라디오는 실시간이 아니다.** 샘플 흐름이 처리 속도에 맞춰 늦춰지므로 기한 초과가 드러나지 않는다. 그래서 ZMQ 라이브로는 실시간 여유를 판정할 수 없고, 처리 시간 분포로만 말한다. Orin에서 "가속"의 이득은 이 부하에서는 없다.

#### J3 재실행 — J2c(C1 + D6 + D7 + D8 + D9)로 CPU와 정합 (2026-09-25, `results/s5-arms-20260925T072028Z`)

`cpu`/`all` 교차 3회, 6/6 attach·ping 통과, 백엔드 OK.
- **가중 ΔBLER −0.08%p, ΔSINR +0.01 dB, 일치 신규 전송 3566건 → PASS.**
- 16QAM `[0,17)`: CPU 1.66% vs GPU 1.33%. 합산 BLER은 0.46% / 0.51%다(J2b에서는 0.24% / 1.60%).
- D8/D9 수정이 Orin에서도 같은 효과를 낸다. 1 PRB SINR은 오히려 +0.3 dB 높다(8.8 vs 8.5).
- 이 런은 J2c PHY 검증(ctest)과 GPU를 나눠 썼다. 그래서 GPU PUSCH `t` p50 2179 µs는 비교용이 아니다(J2b 단독 1666 µs).

#### J4 — 단계별 사다리, J2c (2026-09-25)

`OCUDU_NATIVE_CUDA_GRID_MODE`로 그리드 정책을 바꾸고 `s4-ladder.sh`를 Jetson에서 돌렸다(`PLATFORM_ROOT`, `SPARK_LOCK`=jetson-live, `ATTACH_SECONDS=400`). 이번 실행도 J2c PHY 검증(ctest)과 GPU를 나눠 썼으므로 기능 판정만 한다.

**(1) 렌더러 정책(lower-PHY 단계부터 UL/DL managed 강제), `results/s4-ladder-20260925T073303Z.txt`: 5단계 모두 통과.**

| 단계 | 결과 | 백엔드 | TX 경로 | BLER |
|---|---|---|---|---|
| low-phy-rx | 통과, keepalive 299 | OK | — | 0.49% |
| low-phy-tx | 통과 | DEGRADED(TX staging) | staging | 0.41% |
| pusch | 통과 | DEGRADED(TX staging) | staging | 0.81% |
| pdsch | 통과 | OK | direct + staging | 0.32% |
| prach | 통과 | OK | direct + staging | 0.32% |

백엔드 모양은 Spark(GB10)와 같다. 명시적 managed 그리드와 CMA=0의 장치 전체 동기화(D7 수정 (b)) 경로에서 **SIGBUS, segfault, `cudaErrorIllegalAddress`는 없었다**. 기동 시간은 PUSCH가 켜진 단계부터 80 s다.

**(2) `auto`(D7 수정 (a) 이후 Orin에서는 pinned), `results/s4-ladder-20260925T074340Z.txt`: 첫 단계부터 gNB가 치명적 오류로 멈춘다.** 메시지는 `OCUDU FATAL ERROR: Accelerated lower-PHY RX demodulation requested but unavailable.`이다. lower-PHY RX 가속은 managed 그리드의 장치 매핑을 요구하는데, pinned 그리드에는 그게 없다(렌더러 주석과 같다). 가속을 요청했는데 쓸 수 없으면 벤더 코드는 폴백 대신 중단한다(설계된 동작).

**의미 — D7 수정 (a)의 대가:** Orin에서 `auto`가 pinned를 고르면 D7의 경쟁 상태는 피하지만, 그 설정으로는 lower-PHY 가속을 켤 수 없다. Orin에서 전 가속을 쓰려면 managed를 명시해야 하고, 그러면 D7 수정 (b)의 장치 전체 동기화 경로를 탄다. (b)가 성능에 주는 비용은 따로 재지 않았다. J3 `all`(J2b, managed 명시)에서 GPU PUSCH p50은 1.67 ms였다. 업스트림 제안에는 "auto=pinned이면 lower-PHY 가속 불가"라는 이 교환 관계를 함께 적어야 한다.

### J4 — managed 경로 판정 방법

managed 런에서 크래시가 나면:
1. 코어 덤프 + `compute-sanitizer`(가능하면)로 접근 위치.
2. 같은 단계를 `pinned`로 돌려 통과하면 → 원인은 managed 동시 접근으로 좁혀진다.
3. 벤더 코드가 `ConcurrentManagedAccess`를 질의는 하지만(`resource_grid_cuda_visible_impl.h:1482`) **auto 선택에 반영하는지**를 소스로 확인. 반영하지 않으면 그것이 결함 후보(D6)이고, 수정 방향은 "integrated이면서 concurrent=0이면 auto → pinned" 같은 정책 분기다.

크래시가 **안** 나면 그것도 판정해야 한다 — 벤더가 `cudaStreamAttachMemAsync` 등으로 이미 대응했는지, 아니면 우연히 접근 타이밍이 겹치지 않았는지. 후자면 부하(J5 트래픽)에서 다시 본다.

### J5 — 이슈 6 대조

5090 직결 fixture와 **같은 설정**(`repro/` 설정, 60초 트래픽, arm: host / `pusch` / `all`)으로 돌려야 비교가 된다. 16QAM/TBS 528 할당의 CRC 실패율을 arm별로 뽑는다.

### J8 — OAI nrUE 라이브(1×1, 2×2)와 CPU 배치 (2026-09-28)

**왜:** OAI nrUE는 워크스테이션(5090)과 Spark(GB10)에서만 돌려 봤다. 2×2 rank 2와 오늘 만든 게이트 기본값(패치 ZMQ 모듈, 패치 UE, 플랫폼 CPU 배치, zero-copy `auto`)이 세 번째 플랫폼인 Orin에서도 동작하는지, 30 W에서 실시간이 되는지, 안 되면 어디서 막히는지를 본다.

**준비.** 트리 `/workspace/gpuch/int0928` = `integration-0928` 번들 클론(로컬 수정 없음. aarch64 노브 `OCUDU_NATIVE_SKIP_WORKSPACE_LOCK`·`OCUDU_NATIVE_CUDA_ARCH=87`은 `30563f3`에 들어 있다). 브로커 빌드 `builds/gpuch-int0928-release`(sm_87, CUDA 12.6). OAI(`2b69bde6`)를 워크스테이션에서 소스째 복사해 `build-oai-ue.sh`로 빌드했다(컨테이너에 `liblapacke-dev libblas-dev libnuma-dev libcap-dev iperf3` 설치, `-j6`, 약 40분). 패치 ZMQ 모듈(`build-oai-zmq-patched.py`, 모듈 sha256 `95702cef…`, `--verify` 통과)과 패치 UE(`build-oai-ue-local.sh`, nr-uesoftmodem sha256 `4d9aaf9e…`)도 같은 절차로 만들었다. 조건: `MODE_30W`(8/12 코어, CPU 최대 1.728 GHz, GPU 612 MHz), `jetson_clocks` 미적용, SCTP 모듈 로드 확인. 모든 실행 시작 시 GPU load 0, 호스트에서 user-a 브로커·gNB 프로세스 없음. 컨테이너에 09-25 실행에서 남은 `5gc` 두 묶음이 떠 있었다(포트 충돌 없음, 이번 실행과 무관, 종료하지 않음).

**1×1 (20 MHz, 15 kHz, 슬롯 1 ms = 실시간 1,000 슬롯/s).** 모든 실행 통과(attach·PDU·ping). 실시간 배율은 브로커 heartbeat의 gNB 쪽 pull 수(t=15→20 s)로 쟀다.

| 실행 | ZMQ 모듈 | 배치 (gNB / 브로커 / UE) | 슬롯/s | 배율 | 브로커 p50/p99 (µs) | rx_starvations |
|---|---|---|---|---|---|---|
| `121739Z` | 패치 | 없음 | 585–610 | 0.59–0.61 | 300 / 925 | 142 |
| `121924Z` | **원본** | 없음 | 256 | **0.26** | 315 / 1075 | **3,178** |
| `122105Z` | 패치 | 없음 | 564 | 0.56 | 285 / 930 | 136 |
| `124159Z` | 패치 | 자유 / 6-7 / 4-5 | 529 | 0.53 | 385 / 1200 | 83 |
| `124251Z`, `124359Z` | 패치 | 0-5 / 6-7 / 자유 | 697, 698 | 0.70 | 265 / 680–695 | 45, 95 |
| `124451Z`, `124652Z`, `124836Z` | 패치 | **0-4 / 6-7 / 5** | 808, 806, 811 | **0.81** | 255–265 / 575–590 | 21, 22, 37 |
| `124559Z`, `124745Z` | 패치 | 없음(교대 대조) | 600, 580 | 0.58–0.60 | 295–320 / 895–1025 | 153, 140 |
| `130034Z` | 패치 | 프로파일 자동 → 0-4 / 6-7 / 5 | 800 | **0.80** | 260 / 585 | 23 |
| `131215Z` | 패치 | 0-4 / 7 / 5-6 | 707 | 0.71 | 230 / 295 | 22 |

- **ZMQ 패치는 Orin에서도 필요하다:** 원본 모듈 0.26× → 패치 0.56–0.61×, starvation 3,178 → 136–142. S9와 같은 원인이다.
- **배치:** 두 클러스터(0-3, 4-7)를 나눠 브로커에 6-7을 통째로 주고 UE에 코어 하나를 주면 0.58–0.60× → 0.80–0.81×(+35%), 브로커 p99 895–1025 → 575–590 µs. 교대 2쌍으로 재현했다. 이 배치를 `platform-profiles.json`의 `jetson-orin-30w`로 넣었고(`8787ac3`), 노브 없이 돌린 `130034Z`에서 자동 적용을 확인했다.
- **gNB를 4코어에 가두면 라디오가 시작하지 않는다.** gNB 0-3(`122246Z`, 시작 대기 60 s로 늘린 `122852Z`)과 4-7(`123452Z`) 모두 F1 셋업과 `Cell scheduling was activated`까지 가고 `gNB started`가 나오지 않았다(브로커 pull 0). `taskset 0-7`(`124039Z`)은 통과하므로 고정 방식이 아니라 코어 수 문제다. gNB의 라디오 폴링 스레드가 코어 하나를 100% 쓰는 것과 관련된 것으로 보이지만 원인은 확인하지 않았다. 그래서 프로파일은 gNB에 5코어를 준다.
- **막히는 곳(홉 추적, `OCG_HOP_TRACE_DIR`, `131215Z`):** 브로커 emulator 호출(`produce`) p50 263–271 µs, 방향별 중계(gNB TX 수신 → UE RX 송신) p50 400–425 µs, 장치가 응답을 받고 다음 요청을 보내기까지(`rx_turn`, 장치 처리 + 전송) p50 810–850 µs. 포화된 스레드는 gNB 라디오 폴러(항상 100%)뿐이다. 슬롯마다 두 번 지나는 emulator(약 530 µs)와 장치 쪽 턴이 1 ms 슬롯을 넘는다. 30 W의 GPU 612 MHz에서는 emulator 호출이 GB10(zero-copy 약 26–40 µs)보다 한 자릿수 크다(Z6에서 mvp 183 µs).

**2×2 (유니터리 H, back-off 20 dB, rank 2, 패치 UE, DL UDP 200M 제시, 창 120 s).** 모두 attach·rank 2·행렬 검증 통과.

| 실행 | 배치 (gNB / 브로커 / UE) | 실시간 배율(요약기) | ri=2 새 전송 | NACK | 브로커 p50/p99 (µs) | DL 수신(벽시계) | y=Hx |
|---|---|---|---|---|---|---|---|
| `130143Z` | 프로파일 0-4 / 6-7 / 5 | 0.191 | 2,234 | 1.25% | 475 / 1100 | 51.6 Mb/s | 통과 |
| `130444Z` | 없음 | 0.135 | 2,623 | 1.22% | 550 / 1665 | 43.9 | 통과 |
| `130701Z` | 0-4 / 7 / 5-6 | 0.267 | 3,064 | 1.08% | 335 / 440 | 47.5 | 통과 |
| `130957Z` | 0-4 / 7 / 5-6 (+홉 추적) | 0.276 | 3,129 | 1.29% | — | 46.0 | 통과 |

- **Orin에서 rank 2가 처음으로 복호됐다.** 패치 UE로 20 dB에서 NACK 1.1–1.3%다. 행렬 검증은 DL 두 행 최대 오차 1.0–3.7e-8(허용 1e-4), UL은 둘째 포트가 선언대로 무음이다.
- **하지만 실시간과 거리가 멀다(0.13–0.28×).** 1×1 프로파일(UE 1코어)에서는 UE 코어가 99% 차서(`130143Z`, DL actor·스레드 풀이 전부 코어 5에) 0.19×다. UE에 2코어를 주면 0.27–0.28×가 된다. 1×1은 반대로 이 배치에서 0.71×로 떨어진다. 그래서 프로파일은 1×1 기준으로 두고, 2×2는 `OCUDU_NATIVE_NRUE_CPUS=5-6 OCUDU_NATIVE_BROKER_CPUS=7`을 명시해서 돌린다.
- **2×2에서 막히는 곳(홉 추적 포트 0, `130957Z`):** TX 메시지 약 325/s, emulator 호출 p50 448–473 µs(1×1의 1.7배), 방향별 중계 p50 1.6 ms(DL)·2.1 ms(UL), 장치 턴 p50 1.26 ms(gNB)·1.48 ms(UE). 포화된 스레드는 없고 CPU는 64–80% 바쁘다. 즉 한 곳이 아니라 고리 전체가 1×1의 2–4배로 길어졌다. 하나의 원인으로 좁히지 못했다.
- 벽시계 DL 수신(44–52 Mb/s)은 배치와 상관이 약하다. iperf 15 s 창이 정상 상태와 맞지 않았을 수 있어 판정에 쓰지 않는다. 요약기의 `rt_factor`는 attach 구간을 포함한 값이다.

**판정:** 오늘 기본값은 Orin에서 그대로 동작한다(수정 없이 게이트 통과, 프로파일 자동 적용, 패치 모듈·UE 해시 검증). OAI 1×1은 30 W에서 0.8×, 2×2는 0.28×가 한계다. 실시간 여부는 MAXN(user-a 합의 필요)에서 다시 봐야 한다.

**남은 것:** MAXN에서 1×1·2×2 재측정, gNB 4코어 정지의 원인, 2×2 고리가 길어지는 원인 분리, 2×2 전용 배치를 게이트별 프로파일로 둘지 결정.

원자료: 게이트 결과 Jetson `results/{logs,reports}/oai-1x1|oai-2x2/<ts>`(2×2 wire capture는 `130143Z`만 보존), 실행 스크립트·표본 `/workspace/gpuch/int-*.sh`, `samp-*.txt`, `j8-hop-*.txt`, `j8-matrix-verify.txt`.

### J9 — D10 적용, J2d (2026-09-28, `results/j2d-20260928T161122Z`)

**배경:** S13(Spark)에서 벤더 `lib/phy/cuda/src/transport_block.cu`의 `tb_encoder_configure`·`tb_batch_encoder_configure`가 CPU 세그멘터의 LDPC filler 0을 "값 없음"으로 보고 GPU 자체 값을 써서, filler 0인 다중 CB TB(9,474 B = BG1 9 CB)를 틀린 K로 부호화한다는 것을 찾았다(D10). 하드웨어와 무관한 WG1 코드라 J2c에도 있어야 한다.

**패치:** `scripts/cuda/patches/j2d-ocudu-cuda-orin-d8-d9-d10.patch` = J2c(C1 커밋 위 D6+D7+D8+D9) + D10(`lib/` 두 곳 + `pdsch_gpu_e2e_test` 강제 TBS 9,474 B 1·2포트, 10,247 B F>0 대조), sha256 `ab384cef…`. `j2-patched-validate.sh`를 `J2_TAG=j2d`로 돌려 체크아웃 `src/ocudu-cuda-j2d`, 빌드 `builds/j2d-cuda-patched-sm87`(`-j4`, 48분)을 만들었다. 라이브 lock은 `cuda-workspace.jetson-d10.lock.json`이다. j2d 체크아웃의 로컬 C1 커밋은 `2dbd249`로 j2c의 `35e206b`와 해시만 다르다(커밋 시각). 부모는 둘 다 pin `5830c9cb`다.

**오프라인:** PHY 14/14(`pusch_gpu_cpu_comparison_test` 3,514 s), OFH 16/16, 로그 판정 pass. `pdsch_gpu_e2e_test` 60/60.

**결함 재현 겸 음성 대조군:** j2d 체크아웃에서 D10의 `lib/` 부분만 되돌리고(= J2c 코드) `pdsch_gpu_e2e_test`만 다시 빌드하니 9,474 B 두 케이스만 실패하고 58개가 통과했다(10,247 B 대조 포함). 되살리니 60/60. 소스 diff는 다시 lock 해시 `ab384cef…`와 같고, gNB 바이너리(`1378e4d1…`)는 전후 그대로다.

**라이브(짧게, MODE_30W, srsUE 1×1 직결 ZMQ, 트래픽 60 s, 접속 창 400 s):** CPU gNB와 CUDA J2d(`all`) 각 1회, 둘 다 RRC·PDU·ping 통과, 백엔드 OK, CUDA gNB 기동 80 s. PUSCH 일치 신규 전송 1,126건 비교: **ΔBLER −0.18%p, ΔSINR −0.00 dB → PASS**(`s5-compare-arms.py`). 이 게이트는 ping 수준 DL이라 9,474 B TB를 거의 만들지 않는다. D10 자체의 증거는 위 오프라인 재현과 음성 대조군이고, 라이브는 회귀 없음 확인이다.

**막힌 것:**
- 첫 라이브 시도는 두 arm 모두 곧바로 끝났다. 러너가 root로 `git -C src/ocudu-cuda-j2d rev-parse`를 부르는데 체크아웃이 dev 소유라 git이 `dubious ownership`으로 거부했다. CPU arm도 `src/ocudu`에서 같은 이유로 막혔다. 컨테이너 root의 `~/.gitconfig`에 두 경로를 `safe.directory`로 추가했다.
- 다른 사용자: 호스트에 user-a의 데스크톱 세션만 있고 브로커는 없었다. 전원 모드 `MODE_30W` 그대로, SCTP 모듈 적재 상태.

**디스크:** 실행마다 남는 MongoDB `data/`를 지우고, 217 MB짜리 `srsue-internal.log`는 gzip으로 줄였다(두 실행 합계 27 MB). j2d 소스·빌드(약 0.9 GB)가 남아 사용률은 93%다(여유 4.0 GB). 09-25의 옛 `5gc` 두 묶음은 여전히 돌고 있다(J8에 적은 대로).

## 이 트랙이 주는 것

1. 벤더 코드의 **세 번째 메모리 모델**(integrated + non-concurrent) 판정 — WG1에 새로운 정보.
2. 이슈 6이 플랫폼 의존인지 여부.
3. 에지 폼팩터에서 CUDA gNB가 실시간으로 도는지의 첫 실측.

## 참고

- 5090 트랙: [`CUDA_MILESTONES.md`](CUDA_MILESTONES.md). WG1 제보(이슈 #2, 아래 링크)
- 업스트림 이슈 #2: https://gitlab.com/ocudu/work_groups/wg1_hw_accel/cuda_accelerated_ocudu/-/work_items/2
- 벤더 소스(워크스테이션): `~/ocudu-work/ocudu-native-workspace/src/ocudu-cuda` @ `5830c9cb`
