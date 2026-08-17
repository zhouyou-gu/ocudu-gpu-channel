#!/usr/bin/env python3
"""Strict post-run verifier for the native rank-1 4x1/1x4 live gate (R3)."""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


MAX_COUNTER = (1 << 63) - 1
COUNTER_NAMES = (
    "tx_pulls",
    "rx_requests",
    "rx_starvations",
    "tx_queue_overflows",
    "tx_sequence_gaps",
    "zmq_errors",
)
# The superseded MIMO attempt routed every slot through a RadioNodeCoordinator
# and this verifier asserted on its group_prepares / group_commits /
# group_aborts / partial_group_aborts counters. MIMO_MILESTONES.md section 0.2
# discards that coordinator outright -- the producer model gives the same
# cursor-alignment invariant structurally, with one thread per node instead of
# a generation barrier over Nr request-driven threads -- so those counters do
# not exist and cannot be made to exist without reintroducing the architecture
# M0 was created to remove. The assertions they carried are dropped rather than
# satisfied. Everything else in this file is unchanged.
EXPECTED_SOURCE_COMMITS = {
    "ocudu": "a1916edcdbcd70ba6e0af47ee87be061dad5a4e4",
    "srsran4g": "eea87b1d893ae58e0b08bc381730c502024ae71f",
    "open5gs": "d9d3abdd480be96fac3bc8a997e83446648763ca",
}


def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def require_string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    require(type(value) is str, f"summary key {key} is missing or not a string")
    return value


def require_uint(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    require(
        type(value) is int and 0 <= value <= MAX_COUNTER,
        f"summary key {key} is missing or outside signed uint range",
    )
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path, label: str) -> dict[str, Any]:
    require(path.is_file() and not path.is_symlink(), f"{label} is invalid")
    with path.open("r", encoding="utf-8", errors="strict") as stream:
        value = json.load(stream, object_pairs_hook=reject_duplicates)
    require(type(value) is dict, f"{label} root is not an object")
    return value


def parse_counter_map(line: str) -> dict[str, str]:
    counters: dict[str, str] = {}
    for token in line.split()[1:]:
        key, separator, value = token.partition("=")
        require(separator == "=" and bool(key), "malformed token in broker stop line")
        require(key not in counters, f"duplicate stop counter: {key}")
        counters[key] = value
    return counters


def stop_uint(counters: dict[str, str], key: str) -> int:
    value = counters.get(key)
    require(
        value is not None and re.fullmatch(r"0|[1-9][0-9]*", value) is not None,
        f"stop counter {key} is missing or not canonical decimal",
    )
    parsed = int(value, 10)
    require(parsed <= MAX_COUNTER, f"stop counter {key} exceeds signed uint range")
    return parsed


def validate_artifacts(results_root: Path, summary_path: Path, now: float) -> dict[str, int]:
    require(results_root.is_absolute(), "results root is not absolute")
    require(summary_path.is_absolute(), "summary path is not absolute")
    require(results_root.is_dir() and not results_root.is_symlink(), "results root is invalid")
    require(summary_path.is_file() and not summary_path.is_symlink(), "summary file is invalid")
    results_real = results_root.resolve(strict=True)
    summary_real = summary_path.resolve(strict=True)
    require(results_real == results_root, "results root is not canonical")

    summary = load_json(summary_path, "summary")

    timestamp = require_string(summary, "timestamp")
    parsed_timestamp = datetime.datetime.strptime(timestamp, "%Y%m%dT%H%M%SZ")
    require(
        parsed_timestamp.strftime("%Y%m%dT%H%M%SZ") == timestamp,
        "summary timestamp is not a canonical UTC calendar value",
    )
    require(abs(now - summary_path.stat().st_mtime) <= 300, "summary file is not fresh")
    require(require_string(summary, "status") == "passed", "summary status is not passed")
    require(require_uint(summary, "duration_seconds") == 20, "duration is not fixed to 20 seconds")
    require(require_string(summary, "gate") == "rank1-4x1", "summary does not identify the rank1-4x1 gate")
    require(
        require_string(summary, "srsran_ref") == "release_23_11",
        "srsRAN reference is not pinned",
    )
    require(
        require_string(summary, "runtime_mode") == "rootless_user_net_mount_namespace",
        "summary does not identify the isolated native runtime",
    )
    require(summary.get("docker_used") is False, "summary does not prove Docker-free execution")
    require(summary.get("primitive_probe_passed") is True, "primitive/CUDA probe evidence is absent")

    log_dir = require_string(summary, "log_dir")
    report_dir = require_string(summary, "report_dir")
    expected_log_dir = results_root / "logs/rank1-4x1" / timestamp
    expected_report_dir = results_root / "reports/rank1-4x1" / timestamp
    require(log_dir == str(expected_log_dir), "summary log_dir is outside the run artifact root")
    require(report_dir == str(expected_report_dir), "summary report_dir is outside the run artifact root")
    require(summary_path == expected_report_dir / "attach-summary.json", "summary path/timestamp mismatch")
    require(
        os.path.commonpath((str(results_real), str(summary_real))) == str(results_real),
        "summary resolves outside the results root",
    )
    require(
        summary_real == results_real / "reports/rank1-4x1" / timestamp / "attach-summary.json",
        "summary canonical path does not match its timestamp",
    )

    evidence_path = expected_report_dir / "source-evidence.json"
    manifest_path = expected_report_dir / "channel-source-manifest.tsv"
    evidence = load_json(evidence_path, "source evidence")
    require(
        evidence.get("schema") == "ocudu-native-rank1-4x1-source-evidence/v1",
        "source evidence schema mismatch",
    )
    require(evidence.get("docker_used") is False, "source evidence used Docker")
    require(evidence.get("source_commits") == EXPECTED_SOURCE_COMMITS, "source commit evidence mismatch")
    require(
        type(evidence.get("channel_head")) is str
        and re.fullmatch(r"[0-9a-f]{40}", evidence["channel_head"]) is not None,
        "channel HEAD evidence is invalid",
    )
    for key in ("channel_tracked_diff_sha256", "channel_source_manifest_sha256"):
        require(
            type(evidence.get(key)) is str
            and re.fullmatch(r"[0-9a-f]{64}", evidence[key]) is not None,
            f"source evidence hash is invalid: {key}",
        )
    require(
        evidence["channel_source_manifest_sha256"] == sha256_file(manifest_path),
        "channel source manifest hash mismatch",
    )
    binary_hashes = evidence.get("binary_sha256")
    require(
        type(binary_hashes) is dict
        and set(binary_hashes) == {"gnb", "srsue", "open5gs_5gc", "mongod", "broker"},
        "binary provenance set mismatch",
    )
    require(
        all(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) for value in binary_hashes.values()),
        "binary provenance contains an invalid SHA-256",
    )
    config_hashes = evidence.get("config_sha256")
    expected_configs = {"gnb.yaml", "topology.yaml", "open5gs.yaml", "srsue.conf", "subscriber.csv"}
    require(type(config_hashes) is dict and set(config_hashes) == expected_configs, "config provenance set mismatch")
    for name in expected_configs:
        path = expected_report_dir / "configs" / name
        require(config_hashes[name] == sha256_file(path), f"preserved config hash mismatch: {name}")
    require(
        evidence.get("claim_boundary") == {
            "rank1_4x1_attach": True,
            "miso_simo": True,
            "pdu_session": True,
            "gateway_ping": True,
            "rank2": False,
            "strict_realtime": False,
        },
        "source evidence claim boundary mismatch",
    )

    require(require_uint(summary, "broker_status") == 0, "broker exit status is nonzero")
    require(require_uint(summary, "rrc_connected") == 1, "RRC attach evidence is absent")
    require(
        require_uint(summary, "pdu_session_established") == 1,
        "PDU session evidence is absent",
    )
    require(require_uint(summary, "ping_ok") == 1, "UE namespace ping evidence is absent")
    require(require_uint(summary, "gnb_alive_at_broker_stop") == 1, "gNB exited before Broker stop")
    require(
        require_uint(summary, "open5gs_alive_at_broker_stop") == 1,
        "Open5GS exited before Broker stop",
    )
    require(require_uint(summary, "srsue_alive_at_broker_stop") == 1, "srsUE exited before Broker stop")

    summary_counters = {
        key: require_uint(summary, key)
        for key in (
            "rx_starvations",
            "tx_queue_overflows",
            "tx_sequence_gaps",
            "zmq_errors",
        )
    }
    broker_log = expected_log_dir / "broker.log"
    require(broker_log.is_file() and not broker_log.is_symlink(), "broker log is invalid")
    broker_real = broker_log.resolve(strict=True)
    require(
        os.path.commonpath((str(results_real), str(broker_real))) == str(results_real),
        "broker log resolves outside the results root",
    )
    require(
        broker_real == results_real / "logs/rank1-4x1" / timestamp / "broker.log",
        "broker log canonical path does not match its timestamp",
    )
    lines = broker_log.read_text(encoding="utf-8", errors="strict").splitlines()
    broker_text = "\n".join(lines) + "\n"
    require(broker_text.count("event=start backend=cuda ") == 1, "broker did not start exactly once with CUDA")
    require(
        len(re.findall(r"^event=hardware_probe ok=true device=0 ", broker_text, re.MULTILINE)) == 1,
        "broker lacks one successful logical CUDA device-0 probe",
    )
    require("cuda_device_channel_fallback" not in broker_text, "broker used a CUDA channel fallback")
    stop_lines = [line for line in lines if line.startswith("event=stop ")]
    require(len(stop_lines) == 1, "broker log must contain exactly one event=stop line")
    expected_nodes = {
        "event=radio_node_resolved id=gnb0 tx[0]=gnb0_p0 tx[1]=gnb0_p1 tx[2]=gnb0_p2 tx[3]=gnb0_p3 rx[0]=gnb0_p0 rx[1]=gnb0_p1 rx[2]=gnb0_p2 rx[3]=gnb0_p3 implicit=false",
        "event=radio_node_resolved id=ue0 tx[0]=ue0_p0 rx[0]=ue0_p0 implicit=false",
    }
    node_lines = [line for line in lines if line.startswith("event=radio_node_resolved ")]
    require(
        len(node_lines) == 2 and set(node_lines) == expected_nodes,
        "topology did not resolve the explicit 4-port gNB node and 1-port UE node",
    )

    raw = parse_counter_map(stop_lines[0])
    counters = {key: stop_uint(raw, key) for key in COUNTER_NAMES}
    require(counters["tx_pulls"] > 0, "broker observed no TX pulls")
    require(counters["rx_requests"] > 0, "broker observed no RX requests")
    for key in (
        "tx_queue_overflows",
        "tx_sequence_gaps",
        "zmq_errors",
    ):
        require(counters[key] == 0, f"nonzero strict counter: {key}")
    for key, value in summary_counters.items():
        require(value == counters[key], f"summary counter disagrees with broker log: {key}")

    gnb_console = expected_log_dir / "gnb-console.log"
    gnb_internal = expected_log_dir / "gnb-internal.log"
    require(gnb_console.is_file() and not gnb_console.is_symlink(), "gNB console log is invalid")
    require(gnb_internal.is_file() and not gnb_internal.is_symlink(), "gNB internal log is invalid")
    console_text = gnb_console.read_text(encoding="utf-8", errors="replace")
    internal_text = gnb_internal.read_text(encoding="utf-8", errors="replace")
    require(bool(internal_text.strip()), "gNB internal log is empty")
    require(
        re.search(r"^Available radio types: .*\bzmq\b", console_text, re.MULTILINE) is not None,
        "gNB runtime did not expose the ZMQ RF plugin",
    )
    failure_pattern = re.compile(
        r"Real-time failure in RF|\boverflow\b|\bunderflow\b|"
        r"segmentation fault|assertion failed|fatal[^\n]*(?:radio|zmq)|"
        r"zmq[^\n]*(?:fail|error)",
        re.IGNORECASE,
    )
    failures = failure_pattern.findall(console_text + "\n" + internal_text)
    require(not failures, f"OCUDU gNB log has {len(failures)} transport/runtime failures")
    require(
        "PCAP files successfully closed." in internal_text,
        "OCUDU gNB internal log lacks the clean-shutdown token",
    )
    srsue_log = expected_log_dir / "srsue.log"
    ping_log = expected_log_dir / "ue-ping.log"
    subscriber_log = expected_log_dir / "subscriber-verify.log"
    primitive_log = expected_log_dir / "postbuild-primitive-probe.log"
    for path, label in (
        (srsue_log, "srsUE log"),
        (ping_log, "UE ping log"),
        (subscriber_log, "subscriber verification log"),
        (primitive_log, "postbuild primitive probe log"),
    ):
        require(path.is_file() and not path.is_symlink(), f"{label} is invalid")
    srsue_text = srsue_log.read_text(encoding="utf-8", errors="replace")
    require("RRC Connected" in srsue_text, "srsUE log lacks RRC attach evidence")
    require(
        "PDU Session Establishment successful" in srsue_text,
        "srsUE log lacks PDU session evidence",
    )
    ping_text = ping_log.read_text(encoding="utf-8", errors="replace")
    require(
        re.search(r"60 packets transmitted, 60 received, 0% packet loss", ping_text) is not None,
        "UE ping log does not prove three successful gateway replies",
    )
    require(
        subscriber_log.read_text(encoding="utf-8", errors="strict").strip()
        == "event=native_open5gs_subscriber_verified imsi=001010123456780 ipv4=10.45.1.2",
        "subscriber verification evidence is missing or ambiguous",
    )
    primitive_text = primitive_log.read_text(encoding="utf-8", errors="replace")
    require(
        primitive_text.count("event=native_userns_primitive_probe result=pass") == 1
        and primitive_text.count("event=hardware_probe ok=true device=0 ") == 1,
        "postbuild rootless/CUDA primitive probe evidence is missing or ambiguous",
    )
    return counters


def self_test() -> None:
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    with tempfile.TemporaryDirectory(prefix="ocudu-native-verifier.") as temporary:
        root = Path(temporary).resolve()
        log_dir = root / "logs/rank1-4x1" / timestamp
        report_dir = root / "reports/rank1-4x1" / timestamp
        log_dir.mkdir(parents=True)
        report_dir.mkdir(parents=True)
        broker = log_dir / "broker.log"
        broker.write_text(
            "event=radio_node_resolved id=gnb0 tx[0]=gnb0_p0 tx[1]=gnb0_p1 tx[2]=gnb0_p2 tx[3]=gnb0_p3 rx[0]=gnb0_p0 rx[1]=gnb0_p1 rx[2]=gnb0_p2 rx[3]=gnb0_p3 implicit=false\n"
            "event=radio_node_resolved id=ue0 tx[0]=ue0_p0 rx[0]=ue0_p0 implicit=false\n"
            "event=start backend=cuda duration=15s\n"
            "event=hardware_probe ok=true device=0 name=test\n"
            "event=stop tx_pulls=10 rx_requests=10 rx_starvations=2 "
            "tx_queue_overflows=0 tx_sequence_gaps=0 zmq_errors=0\n",
            encoding="utf-8",
        )
        (log_dir / "gnb-console.log").write_text(
            "Available radio types: zmq\n==== gNB started ===\n", encoding="utf-8"
        )
        (log_dir / "gnb-internal.log").write_text(
            "PCAP files successfully closed.\n", encoding="utf-8"
        )
        (log_dir / "srsue.log").write_text(
            "RRC Connected\nPDU Session Establishment successful\n", encoding="utf-8"
        )
        (log_dir / "ue-ping.log").write_text(
            "60 packets transmitted, 60 received, 0% packet loss\n", encoding="utf-8"
        )
        (log_dir / "subscriber-verify.log").write_text(
            "event=native_open5gs_subscriber_verified imsi=001010123456780 ipv4=10.45.1.2\n",
            encoding="utf-8",
        )
        (log_dir / "postbuild-primitive-probe.log").write_text(
            "event=hardware_probe ok=true device=0 name=test\n"
            "event=native_userns_primitive_probe result=pass\n",
            encoding="utf-8",
        )
        summary_path = report_dir / "attach-summary.json"
        summary = {
            "timestamp": timestamp,
            "status": "passed",
            "duration_seconds": 20,
            "gate": "rank1-4x1",
            "srsran_ref": "release_23_11",
            "runtime_mode": "rootless_user_net_mount_namespace",
            "docker_used": False,
            "primitive_probe_passed": True,
            "broker_status": 0,
            "rrc_connected": 1,
            "pdu_session_established": 1,
            "ping_ok": 1,
            "gnb_alive_at_broker_stop": 1,
            "open5gs_alive_at_broker_stop": 1,
            "srsue_alive_at_broker_stop": 1,
            "rx_starvations": 2,
            "tx_queue_overflows": 0,
            "tx_sequence_gaps": 0,
            "zmq_errors": 0,
            "log_dir": str(log_dir),
            "report_dir": str(report_dir),
        }
        summary_path.write_text(json.dumps(summary), encoding="utf-8")
        manifest = report_dir / "channel-source-manifest.tsv"
        manifest.write_text("sha256\tpath\n", encoding="utf-8")
        configs = report_dir / "configs"
        configs.mkdir()
        config_hashes = {}
        for name in ("gnb.yaml", "topology.yaml", "open5gs.yaml", "srsue.conf", "subscriber.csv"):
            path = configs / name
            path.write_text(name, encoding="utf-8")
            config_hashes[name] = sha256_file(path)
        evidence = {
            "schema": "ocudu-native-rank1-4x1-source-evidence/v1",
            "docker_used": False,
            "channel_head": "0" * 40,
            "channel_tracked_diff_sha256": "0" * 64,
            "channel_source_manifest_sha256": sha256_file(manifest),
            "source_commits": EXPECTED_SOURCE_COMMITS,
            "binary_sha256": {
                name: "0" * 64
                for name in ("gnb", "srsue", "open5gs_5gc", "mongod", "broker")
            },
            "config_sha256": config_hashes,
            "claim_boundary": {
                "rank1_4x1_attach": True,
            "miso_simo": True,
                "pdu_session": True,
                "gateway_ping": True,
                "rank2": False,
                "strict_realtime": False,
            },
        }
        (report_dir / "source-evidence.json").write_text(json.dumps(evidence), encoding="utf-8")
        counters = validate_artifacts(root, summary_path, time.time())
        assert counters["rx_starvations"] == 2
    print("event=native_rank1_4x1_artifact_verifier_self_test result=pass")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results-root", type=Path)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        if args.results_root is not None or args.summary is not None:
            parser.error("--self-test cannot be combined with artifact arguments")
        self_test()
        return 0
    if args.results_root is None or args.summary is None:
        parser.error("--results-root and --summary are required")
    counters = validate_artifacts(args.results_root, args.summary, time.time())
    print(
        "event=native_rank1_4x1_attach_gate result=pass "
        f'summary="{args.summary}" '
        + " ".join(f"{key}={counters[key]}" for key in COUNTER_NAMES)
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        print(f"artifact validation failed: {error}", file=sys.stderr)
        raise SystemExit(1)
