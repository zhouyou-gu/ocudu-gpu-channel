#!/usr/bin/env python3
"""Validate the native actual-OCUDU two-port transport evidence bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--summary", required=True, type=Path)
    parser.add_argument("--broker-log", required=True, type=Path)
    parser.add_argument("--peer-summary", required=True, type=Path)
    parser.add_argument("--peer-selftest", required=True, type=Path)
    parser.add_argument("--gnb-console-log", required=True, type=Path)
    parser.add_argument("--gnb-internal-log", required=True, type=Path)
    parser.add_argument("--gnb-version-log", required=True, type=Path)
    parser.add_argument("--broker-status", required=True, type=int)
    parser.add_argument("--peer-status", required=True, type=int)
    parser.add_argument("--gnb-status", required=True, type=int)
    parser.add_argument("--gnb-reached-running", required=True, type=int)
    parser.add_argument("--gnb-alive-before-shutdown", required=True, type=int)
    parser.add_argument("--duration", required=True, type=int)
    parser.add_argument("--audited-commit", required=True)
    parser.add_argument("--actual-commit", required=True)
    parser.add_argument("--source-evidence", required=True, type=Path)
    parser.add_argument("--endpoint-map", required=True, type=Path)
    parser.add_argument("--channel-source-manifest", required=True, type=Path)
    parser.add_argument("--gnb-fixture", required=True, type=Path)
    parser.add_argument("--resolved-gnb-config", required=True, type=Path)
    parser.add_argument("--topology", required=True, type=Path)
    parser.add_argument("--log-dir", required=True, type=Path)
    parser.add_argument("--report-dir", required=True, type=Path)
    parser.add_argument("--capture-dir", required=True, type=Path)
    parser.add_argument("--matrix-report", required=True, type=Path)
    parser.add_argument("--matrix-status", required=True, type=int)
    return parser.parse_args()


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as source:
        value = json.load(source, object_pairs_hook=reject_duplicate_keys)
    if not isinstance(value, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    args = parse_args()
    errors: list[str] = []

    def require(condition: bool, message: str) -> None:
        if not condition:
            errors.append(message)

    broker_log = args.broker_log.read_text(encoding="utf-8", errors="replace")
    gnb_console_log = args.gnb_console_log.read_text(
        encoding="utf-8", errors="replace"
    )
    gnb_internal_log = args.gnb_internal_log.read_text(
        encoding="utf-8", errors="replace"
    )
    gnb_log = gnb_console_log + "\n" + gnb_internal_log
    gnb_version_log = args.gnb_version_log.read_text(
        encoding="utf-8", errors="replace"
    )
    peer = load_json(args.peer_summary)
    selftest = load_json(args.peer_selftest)
    source_evidence = load_json(args.source_evidence)
    endpoint_map = load_json(args.endpoint_map)
    gnb_fixture_sha256 = sha256_file(args.gnb_fixture)
    resolved_gnb_sha256 = sha256_file(args.resolved_gnb_config)
    topology_sha256 = sha256_file(args.topology)

    require(
        source_evidence.get("schema") == "ocudu-mimo-2port-source-evidence/v1",
        "source evidence schema mismatch",
    )
    require(
        source_evidence.get("audited_ocudu_commit") == args.audited_commit
        and source_evidence.get("actual_ocudu_commit") == args.actual_commit
        and source_evidence.get("audit_revision_match") is True,
        "source evidence revision mismatch",
    )
    require(source_evidence.get("docker_used") is False, "source evidence used Docker")
    require(
        source_evidence.get("core_mode") == "ocudu_no_core",
        "source evidence core mode mismatch",
    )
    require(
        isinstance(source_evidence.get("gnb_sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", source_evidence["gnb_sha256"]) is not None,
        "source evidence gNB SHA-256 is invalid",
    )
    for key in [
        "broker_sha256",
        "channel_tracked_diff_sha256",
        "channel_source_manifest_sha256",
    ]:
        require(
            isinstance(source_evidence.get(key), str)
            and re.fullmatch(r"[0-9a-f]{64}", source_evidence[key]) is not None,
            f"source evidence {key} is invalid",
        )
    require(
        source_evidence.get("channel_source_manifest_sha256")
        == sha256_file(args.channel_source_manifest),
        "channel source manifest differs from source evidence",
    )
    require(
        isinstance(source_evidence.get("channel_head"), str)
        and re.fullmatch(r"[0-9a-f]{40}", source_evidence["channel_head"])
        is not None,
        "source evidence channel HEAD is invalid",
    )
    require(
        source_evidence.get("gnb_fixture_sha256") == gnb_fixture_sha256,
        "preserved gNB fixture differs from source evidence",
    )
    require(
        source_evidence.get("resolved_gnb_sha256") == resolved_gnb_sha256,
        "resolved gNB config differs from source evidence",
    )
    require(
        source_evidence.get("topology_sha256") == topology_sha256,
        "preserved channel topology differs from source evidence",
    )
    require(
        gnb_fixture_sha256
        == "6e0378b9969c3e12a3105ed58726b9aaa92236b2d8ce8247d9e25df9536c80b9",
        "gNB fixture is not the audited native two-port configuration",
    )
    require(
        topology_sha256
        # M5.3: re-taken after adapting the audited file to the post-M1 schema
        # (`role:` off radio_nodes, `rx_ports`/`tx_ports` off fixed_mimo --
        # dimensions are stated once, in the node declaration). Coefficients,
        # endpoints, antenna counts and port order are unchanged.
        # previous: 5bdf3ecada9c07d651bae3f52efc9bcffb2562de79b1025af92deb13d0be2a6e
        == "ced8f0250c0a724ec015f5a2c1c31164325f0dcbafeb9b556605ea044e0eb6f5",
        "channel topology is not the audited dense native two-port configuration",
    )
    fixture_text = args.gnb_fixture.read_text(encoding="utf-8")
    resolved_text = args.resolved_gnb_config.read_text(encoding="utf-8")
    fixture_lines = fixture_text.splitlines()
    resolved_lines = resolved_text.splitlines()
    require(
        len(fixture_lines) == len(resolved_lines),
        "resolved gNB config changed fixture line cardinality",
    )
    differing_lines = [
        (source, resolved)
        for source, resolved in zip(fixture_lines, resolved_lines)
        if source != resolved
    ]
    require(
        len(differing_lines) == 1
        and differing_lines[0][0]
        == "  filename: /tmp/ocudu-native-2port-gnb.log"
        and differing_lines[0][1].startswith("  filename: ")
        and differing_lines[0][1].endswith("/gnb-internal.log"),
        "resolved gNB config changed more than the evidence log path",
    )

    expected_endpoints = [
        ("gnb0", 0, 2000, 2001),
        ("gnb0", 1, 2002, 2003),
        ("peer0", 0, 2101, 2100),
        ("peer0", 1, 2103, 2102),
    ]
    require(
        endpoint_map.get("schema") == "ocudu-mimo-2port-endpoints/v1",
        "endpoint map schema mismatch",
    )
    require(
        endpoint_map.get("network_namespace") == "native_loopback",
        "endpoint map is not native loopback",
    )
    require(endpoint_map.get("ordered_ports") == [0, 1], "endpoint port order mismatch")
    require(endpoint_map.get("transport_only") is True, "endpoint map claim mismatch")
    require(endpoint_map.get("rank2_claim") is False, "endpoint map made a rank-2 claim")
    endpoint_ports = endpoint_map.get("ports")
    require(isinstance(endpoint_ports, list), "endpoint map ports is not a list")
    if isinstance(endpoint_ports, list):
        require(len(endpoint_ports) == 4, "endpoint map does not contain four ports")
        for index, (node, port, tx, rx) in enumerate(expected_endpoints):
            if index >= len(endpoint_ports) or not isinstance(endpoint_ports[index], dict):
                continue
            value = endpoint_ports[index]
            require(value.get("node") == node, f"endpoint {index} node mismatch")
            require(value.get("port") == port, f"endpoint {index} port mismatch")
            require(
                value.get("peer_tx_rep") == f"tcp://127.0.0.1:{tx}"
                and value.get("broker_tx_req") == f"tcp://127.0.0.1:{tx}"
                and value.get("broker_rx_rep") == f"tcp://127.0.0.1:{rx}"
                and value.get("peer_rx_req") == f"tcp://127.0.0.1:{rx}",
                f"endpoint {index} is not the audited loopback mapping",
            )

    require(
        re.search(r"OCUDU 5G gNB version .*\(a1916edcd\)", gnb_version_log)
        is not None,
        "gNB version output does not identify the audited revision",
    )
    require(
        re.search(r"^Available radio types: .*\bzmq\b", gnb_console_log, re.MULTILINE)
        is not None,
        "gNB runtime did not expose the ZMQ RF plugin",
    )

    require(
        broker_log.count("event=start backend=cuda ") == 1,
        "broker must start exactly once with CUDA",
    )
    require(
        len(re.findall(r"^event=hardware_probe ok=true device=0 ", broker_log, re.MULTILINE))
        == 1,
        "broker must report one successful logical CUDA device-0 probe",
    )
    require(
        "cuda_device_channel_fallback" not in broker_log,
        "broker used a CUDA device-channel fallback",
    )
    expected_radio_lines = {
        "event=radio_node_resolved id=gnb0 tx[0]=gnb0_p0 tx[1]=gnb0_p1 rx[0]=gnb0_p0 rx[1]=gnb0_p1 implicit=false",
        "event=radio_node_resolved id=peer0 tx[0]=peer0_p0 tx[1]=peer0_p1 rx[0]=peer0_p0 rx[1]=peer0_p1 implicit=false",
    }
    actual_radio_lines = {
        line for line in broker_log.splitlines() if line.startswith("event=radio_node_resolved ")
    }
    require(
        actual_radio_lines == expected_radio_lines,
        "broker resolved RadioNodes differ from the exact explicit two-node overlay",
    )

    stop_lines = [
        line for line in broker_log.splitlines() if line.startswith("event=stop ")
    ]
    require(len(stop_lines) == 1, "broker must emit exactly one event=stop line")
    stop_line = stop_lines[0] if len(stop_lines) == 1 else ""

    counter_names = [
        "tx_pulls",
        "rx_requests",
        "rx_starvations",
        "tx_queue_overflows",
        "tx_sequence_gaps",
        "zmq_errors",
    ]
    counters: dict[str, int] = {}
    for name in counter_names:
        match = re.search(rf"(?:^| ){re.escape(name)}=([0-9]+)(?: |$)", stop_line)
        require(match is not None, f"broker stop line is missing {name}")
        counters[name] = int(match.group(1)) if match else -1

    # The `server[serves=... data_spin=...]` group this used to read belonged to
    # the pre-M0 broker, which had one request-driven server thread per device.
    # M0 split that thread into a per-node producer and a per-port REP worker
    # and renamed the summary accordingly, so the old pattern matched nothing
    # and every check below it silently measured an empty dict. `serves` is the
    # REP worker's reply count, which is what the old `serves` counted; the
    # producer's slot count is a per-node quantity and is deliberately not read
    # as a per-port one. `acquired` is new: the cumulative sample count a
    # port's puller has taken off its peer.
    worker_pattern = re.compile(
        r"^event=worker_summary dev=([^ ]+) "
        r"puller\[pulls=([0-9]+) idle=[0-9]+ room_stall=[0-9]+ acquired=([0-9]+)\] "
        r"producer\[slots=[0-9]+ stall=[0-9]+\] "
        r"rep\[replies=([0-9]+) idle=[0-9]+ row_spin=[0-9]+\]$",
        re.MULTILINE,
    )
    workers: dict[str, dict[str, int]] = {}
    for device, pulls, acquired, serves in worker_pattern.findall(broker_log):
        require(device not in workers, f"duplicate worker summary for {device}")
        workers[device] = {
            "pulls": int(pulls),
            "acquired": int(acquired),
            "serves": int(serves),
        }

    expected_devices = ["gnb0_p0", "gnb0_p1", "peer0_p0", "peer0_p1"]
    require(set(workers) == set(expected_devices), "worker summaries do not match all four ports")
    for device in expected_devices:
        if device in workers:
            require(workers[device]["pulls"] > 0, f"{device} has zero TX pulls")
            require(workers[device]["serves"] > 0, f"{device} has zero RX serves")

    require(args.broker_status == 0, f"broker exited {args.broker_status}")
    require(args.peer_status == 0, f"peer exited {args.peer_status}")
    require(args.gnb_status == 0, f"gNB exited {args.gnb_status}")
    require(args.gnb_reached_running == 1, "gNB never reached running state")
    require(
        args.gnb_alive_before_shutdown == 1,
        "gNB was not alive at the end of the measured interval",
    )
    require(
        args.actual_commit == args.audited_commit,
        "actual OCUDU revision differs from the audited revision",
    )
    for name in [
        "rx_starvations",
        "tx_queue_overflows",
        "tx_sequence_gaps",
        "zmq_errors",
    ]:
        require(counters.get(name, -1) == 0, f"broker {name} is not zero")
    require(counters.get("tx_pulls", 0) > 0, "broker performed no TX pulls")
    require(counters.get("rx_requests", 0) > 0, "broker served no RX requests")
    # M5.4: the group_prepares / group_commits / group_aborts accounting and the
    # event=radio_group_abort diagnostics are gone, and are not replaced by
    # equivalents, because the thing that emitted them is gone. They were
    # RadioNodeCoordinator counters: a generation barrier that admitted a group,
    # gathered every sibling reply, then committed or rolled back. M0 discarded
    # that coordinator outright -- one producer thread per RadioNode now selects
    # ONE window and writes every row from it, so a group that could be
    # partially committed is not a state this design can reach.
    #
    # What replaced the barrier is checked below instead: the per-port worker
    # summaries must show a node's sibling RX ports served the SAME number of
    # times, and the global counters must equal the sum of the per-port ones.
    # That is the same property the commit barrier existed to guarantee, read
    # off the structure that now guarantees it rather than off a counter.
    if set(workers) == set(expected_devices):
        require(
            counters.get("tx_pulls") == sum(value["pulls"] for value in workers.values()),
            "global TX pulls differ from per-port summaries",
        )
        require(
            counters.get("rx_requests") == sum(value["serves"] for value in workers.values()),
            "global RX requests differ from per-port summaries",
        )
        # Sibling serve counts. The two REP workers are independent threads and
        # each can have one request in flight, so a snapshot taken at shutdown
        # can legitimately catch one sibling a single reply ahead. What must not
        # appear is a DRIFT: the windows they serve come from one producer, so
        # the counts cannot separate. One reply is the structural bound; the
        # measured difference is reported either way.
        for node in ("gnb0", "peer0"):
            difference = abs(workers[f"{node}_p0"]["serves"] - workers[f"{node}_p1"]["serves"])
            print(f"measured {node}_sibling_serve_difference={difference} bound=1")
            require(difference <= 1, f"{node} sibling RX transaction counts drifted apart")
        # Sibling TX acquisition skew. The pullers are independent threads, so
        # this is not exactly zero by construction the way the serve counts are:
        # a summary can catch one sibling having just landed a message the other
        # has not. What it must not show is a DRIFT -- the two ports of one
        # radio carry one sample epoch, so their cumulative acquisitions cannot
        # separate by more than the run's message granularity. One batch is that
        # bound -- the largest window the node ever publishes.
        #
        # This is a drift guard, not the detector for the 2026-08-15 freeze: the
        # skew that run left behind was 11 776 samples, inside this bound, and
        # the freeze was caught by the peer status and the broker counters. The
        # number is reported on every run so the next reader sees the actual
        # margin rather than only the verdict.
        for node in ("gnb0", "peer0"):
            skew = abs(workers[f"{node}_p0"]["acquired"] - workers[f"{node}_p1"]["acquired"])
            print(f"measured {node}_sibling_acquired_skew_samples={skew} bound=23040")
            require(skew <= 23040, f"{node} sibling TX acquisition drifted apart")

    require(peer.get("schema") == "ocudu-mimo-transport-peer/v1", "peer summary schema mismatch")
    require(peer.get("status") == "passed", "peer summary did not pass")
    require(peer.get("transport_only") is True, "peer omitted transport-only label")
    require(peer.get("rank2_claim") is False, "peer made a rank-2 claim")
    tx_ports = peer.get("tx_ports") if isinstance(peer.get("tx_ports"), list) else []
    rx_ports = peer.get("rx_ports") if isinstance(peer.get("rx_ports"), list) else []
    require(len(tx_ports) == 2, "peer did not report two TX ports")
    require(len(rx_ports) == 2, "peer did not report two RX ports")
    expected_peer_tx = [
        ("tcp://127.0.0.1:2101", 23040, 0),
        ("tcp://127.0.0.1:2103", 23040, 73),
    ]
    expected_peer_rx = [
        ("tcp://127.0.0.1:2100", 109),
        ("tcp://127.0.0.1:2102", 0),
    ]
    for index, value in enumerate(tx_ports):
        require(value.get("port") == index, f"peer TX port {index} ordering mismatch")
        if index < len(expected_peer_tx):
            endpoint, chunk_samples, reply_delay_us = expected_peer_tx[index]
            require(value.get("endpoint") == endpoint, f"peer TX port {index} endpoint mismatch")
            require(
                value.get("chunk_samples") == chunk_samples,
                f"peer TX port {index} chunk size mismatch",
            )
            require(
                value.get("reply_delay_us") == reply_delay_us,
                f"peer TX port {index} delay mismatch",
            )
        require(value.get("transactions", 0) > 0, f"peer TX port {index} has zero transactions")
        require(value.get("samples", 0) > 0, f"peer TX port {index} has zero samples")
        require(value.get("marker_start") == 0, f"peer TX port {index} marker did not start at zero")
        require(
            value.get("marker_next") == value.get("samples"),
            f"peer TX port {index} cumulative marker/sample accounting differs",
        )
        for key in ["request_size_errors", "short_sends", "zmq_errors"]:
            require(value.get(key) == 0, f"peer TX port {index} {key} is not zero")
    for index, value in enumerate(rx_ports):
        require(value.get("port") == index, f"peer RX port {index} ordering mismatch")
        if index < len(expected_peer_rx):
            endpoint, request_offset_us = expected_peer_rx[index]
            require(value.get("endpoint") == endpoint, f"peer RX port {index} endpoint mismatch")
            require(
                value.get("request_offset_us") == request_offset_us,
                f"peer RX port {index} request offset mismatch",
            )
        require(value.get("transactions", 0) > 0, f"peer RX port {index} has zero transactions")
        require(value.get("samples", 0) > 0, f"peer RX port {index} has zero samples")
        require(
            value.get("nonzero_samples", 0) > 0,
            f"peer RX port {index} received only zero-valued IQ",
        )
        require(
            value.get("cumulative_start") == 0,
            f"peer RX port {index} cumulative count did not start at zero",
        )
        require(
            value.get("cumulative_next") == value.get("samples"),
            f"peer RX port {index} cumulative sample accounting differs",
        )
        for key in ["malformed_messages", "nonfinite_samples", "short_requests", "zmq_errors"]:
            require(value.get(key) == 0, f"peer RX port {index} {key} is not zero")

    rx_groups = peer.get("rx_groups") if isinstance(peer.get("rx_groups"), dict) else {}
    require(rx_groups.get("completed", 0) > 0, "peer completed no RX groups")
    require(rx_groups.get("sibling_size_mismatches") == 0, "peer observed sibling reply size mismatch")
    require(rx_groups.get("partial_reply_groups") == 0, "peer observed a partial reply group")
    require(
        rx_groups.get("shutdown_outstanding_groups", 0) <= 1,
        "peer left more than one shutdown-only outstanding group",
    )
    if len(rx_ports) == 2:
        require(
            rx_ports[0].get("transactions")
            == rx_groups.get("completed")
            == rx_ports[1].get("transactions"),
            "peer per-port RX/group transaction accounting differs",
        )
        require(
            rx_ports[0].get("samples") == rx_ports[1].get("samples"),
            "peer cumulative sibling sample counts differ",
        )
    if set(workers) == set(expected_devices) and len(rx_ports) == 2:
        require(
            rx_groups.get("completed") == workers["peer0_p0"]["serves"],
            "peer completed groups differ from broker peer0 serves",
        )
    if set(workers) == set(expected_devices) and len(tx_ports) == 2:
        for index in range(2):
            pulls = workers[f"peer0_p{index}"]["pulls"]
            replies = tx_ports[index].get("transactions", -100)
            require(
                0 <= replies - pulls <= 1,
                f"peer TX port {index} replies differ from broker pulls by more than shutdown allowance",
            )

    require(
        selftest.get("schema") == "ocudu-mimo-transport-peer/v1",
        "peer self-test schema mismatch",
    )
    require(selftest.get("status") == "passed", "peer functional self-test failed")
    require(selftest.get("self_test") is True, "peer self-test flag missing")
    selftest_result = selftest.get("self_test_result", {})
    require(selftest_result.get("completed_groups") == 64, "peer self-test did not complete all groups")
    require(selftest_result.get("marker_checks", 0) > 0, "peer self-test checked no cumulative markers")
    require(selftest_result.get("marker_mismatches") == 0, "peer self-test found marker discontinuity")
    require(
        selftest_result.get("distinct_reply_sizes", 0) >= 2,
        "peer self-test did not exercise split/coalesced reply sizes",
    )

    failure_pattern = re.compile(
        r"Real-time failure in RF|\boverflow\b|\bunderflow\b|"
        r"segmentation fault|assertion failed|fatal[^\n]*(?:radio|zmq)|"
        r"zmq[^\n]*(?:fail|error)",
        re.IGNORECASE,
    )
    gnb_runtime_failures = len(failure_pattern.findall(gnb_log))
    require(
        gnb_runtime_failures == 0,
        f"OCUDU gNB log has {gnb_runtime_failures} transport/runtime failures",
    )
    require(
        "PCAP files successfully closed." in gnb_internal_log,
        "OCUDU gNB internal log lacks the clean-shutdown token",
    )

    # The matrix row of the exit gate. Everything above this point judges what
    # the relay MOVED; a broker that passed each port straight through would
    # satisfy all of it. This folds in the independent check that what left the
    # emulator is the declared H applied to what entered it.
    matrix_report: dict = {}
    if args.matrix_report.exists():
        matrix_report = json.loads(args.matrix_report.read_text(encoding="utf-8"))
    require(
        matrix_report.get("schema") == "ocudu-mimo-matrix-capture/v1",
        "matrix capture report is missing or has the wrong schema",
    )
    require(args.matrix_status == 0, "matrix capture verification failed")
    require(
        matrix_report.get("status") == "passed",
        "matrix capture report did not pass",
    )
    matrix_measurements = matrix_report.get("measurements") or {}
    # An instrument that measured nothing satisfies every threshold, so assert
    # the sample counts before trusting the verdict above them.
    for key in ("gnb0->peer0_compared_samples", "peer0->gnb0_compared_samples"):
        require(
            int(matrix_measurements.get(key, 0)) > 0,
            f"matrix capture compared no samples for {key.split('_')[0]}",
        )
    for port in ("peer0_p0", "peer0_p1"):
        require(
            int(matrix_measurements.get(f"marker_{port}_samples", 0)) > 0,
            f"matrix capture checked no analytic markers on {port}",
        )

    # Real-time budget. The broker samples its channel-processor stage timings
    # once a second and reports the slot that happened to be last, so these are
    # per-second samples of individual slots, NOT a percentile over every slot;
    # the field names say so. The comparison budget is the batch duration
    # (23040 samples at 23.04 MS/s = 1 ms, the 15 kHz SCS slot this fixture
    # configures), with the 500 us half-slot noted for a 30 kHz deployment.
    gpu_samples = [
        {name: float(value) for name, value in re.findall(r"(h2d_us|kernel_us|d2h_us)=([0-9.]+)", line)}
        for line in broker_log.splitlines()
        if line.startswith("event=gpu_timings ")
    ]
    process_samples = [
        float(match)
        for match in re.findall(r"^event=cpu_stage_timings .* process_us=([0-9.]+)", broker_log, re.MULTILINE)
    ]
    realtime_budget = {
        "slot_budget_us": 1000.0,
        "half_slot_budget_us": 500.0,
        "sampling": "one slot per second, sampled by the broker heartbeat",
        "gpu_timing_samples": len(gpu_samples),
        "process_us_samples": len(process_samples),
    }
    for stage in ("h2d_us", "kernel_us", "d2h_us"):
        values = [sample[stage] for sample in gpu_samples if stage in sample]
        if values:
            realtime_budget[f"{stage}_max"] = max(values)
            realtime_budget[f"{stage}_median"] = sorted(values)[len(values) // 2]
    if process_samples:
        realtime_budget["process_us_max"] = max(process_samples)
        realtime_budget["process_us_median"] = sorted(process_samples)[len(process_samples) // 2]
    require(len(gpu_samples) > 0, "broker emitted no event=gpu_timings samples")
    require(len(process_samples) > 0, "broker emitted no event=cpu_stage_timings samples")
    # The typical slot is what the real-time claim rests on, and it is gated.
    # The observed maximum is recorded and NOT gated: `MIMO_MILESTONES.md` S4
    # settled that the tail on this host is scheduling / IRQ / driver submit-or-
    # sync, not the MIMO compute path, and classified an observed-max miss as an
    # environment gate. Turning it into a hard gate here would reopen a question
    # that was closed with evidence, and would make this gate score the host.
    require(
        realtime_budget.get("process_us_median", 1e9) < realtime_budget["slot_budget_us"],
        "median channel-processor slot time exceeded the 1 ms slot budget",
    )
    if realtime_budget.get("process_us_max", 0.0) >= realtime_budget["slot_budget_us"]:
        print(
            "note environment_tail process_us_max={} exceeds the {} us slot budget; "
            "recorded, not gated (MIMO_MILESTONES.md S4)".format(
                realtime_budget["process_us_max"], realtime_budget["slot_budget_us"]
            )
        )
    print(
        "measured process_us_max={} process_us_median={} slot_budget_us=1000".format(
            realtime_budget.get("process_us_max"), realtime_budget.get("process_us_median")
        )
    )

    summary = {
        "schema": "ocudu-mimo-2port-transport/v2",
        "matrix_capture": {
            "status": matrix_report.get("status", "missing"),
            "report": str(args.matrix_report),
            "capture_dir": str(args.capture_dir),
            "measurements": matrix_measurements,
        },
        "realtime_budget": realtime_budget,
        "status": "passed" if not errors else "failed",
        "transport_only": True,
        "rank2_claim": False,
        "ue_decode": False,
        "core_mode": "ocudu_no_core",
        "docker_used": False,
        "duration_seconds": args.duration,
        "audited_ocudu_commit": args.audited_commit,
        "actual_ocudu_commit": args.actual_commit,
        "process_status": {
            "broker": args.broker_status,
            "peer": args.peer_status,
            "gnb": args.gnb_status,
            "gnb_reached_running": args.gnb_reached_running,
            "gnb_alive_before_shutdown": args.gnb_alive_before_shutdown,
        },
        "broker_counters": counters,
        "port_transactions": workers,
        "peer_summary": str(args.peer_summary),
        "peer_selftest_summary": str(args.peer_selftest),
        "source_evidence": str(args.source_evidence),
        "endpoint_map": str(args.endpoint_map),
        "channel_source_manifest": str(args.channel_source_manifest),
        "gnb_fixture": str(args.gnb_fixture),
        "resolved_gnb_config": str(args.resolved_gnb_config),
        "topology": str(args.topology),
        "gnb_runtime_failures": gnb_runtime_failures,
        "errors": errors,
        "log_dir": str(args.log_dir),
        "report_dir": str(args.report_dir),
    }
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"summary={args.summary}")
    print(f"status={summary['status']}")
    for error in errors:
        print(f"gate_error={error}")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
