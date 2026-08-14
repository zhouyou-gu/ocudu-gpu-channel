#!/usr/bin/env python3
"""Fail-closed verification of the native multi-UE Open5GS subscribers.

The multi-UE sibling of verify-open5gs-subscriber.py. That file pins a single
audited record as module constants and requires the collection to hold exactly
one document, which is correct for the 1x1 gate and is left untouched.

The document-shape checks here are deliberately identical -- same slice/session
structure, same OPc-not-OP rule, same static-IPv4 requirement. The only change
is where the expected values come from: this reads them out of the rendered
subscriber CSV, so the database is checked against the same file the UEs and
the renderer were configured from, and a mismatch between any two of them is
caught rather than assumed away.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def parse_csv(path: Path) -> list[dict[str, Any]]:
    require(path.is_absolute(), "subscriber CSV path must be absolute")
    require(path.is_file() and not path.is_symlink(), "subscriber CSV is missing or symlinked")
    records: list[dict[str, Any]] = []
    seen_imsi: set[str] = set()
    seen_ipv4: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="strict").splitlines():
        if not line or line.startswith("#"):
            continue
        fields = line.split(",")
        require(len(fields) == 8, f"subscriber record is not eight columns: {line!r}")
        _name, imsi, key, op_type, op_or_opc, amf, qci, ipv4 = fields
        require(op_type == "opc", f"subscriber {imsi} must use OPc, not OP")
        require(imsi not in seen_imsi, f"duplicate subscriber IMSI: {imsi}")
        require(ipv4 not in seen_ipv4, f"duplicate subscriber IPv4: {ipv4}")
        seen_imsi.add(imsi)
        seen_ipv4.add(ipv4)
        records.append(
            {"imsi": imsi, "k": key, "opc": op_or_opc, "amf": amf, "qci": int(qci), "ipv4": ipv4}
        )
    require(len(records) >= 2, "the multi-UE gate needs at least two subscriber records")
    return records


def verify_document(document: dict[str, Any], expected: dict[str, Any]) -> None:
    imsi = expected["imsi"]
    require(type(document) is dict, f"subscriber {imsi} document is not an object")
    require(document.get("imsi") == imsi, f"subscriber {imsi} IMSI mismatch")

    security = document.get("security")
    require(type(security) is dict, f"subscriber {imsi} security object is absent")
    require(security.get("k") == expected["k"], f"subscriber {imsi} K mismatch")
    require(security.get("amf") == expected["amf"], f"subscriber {imsi} AMF mismatch")
    require(security.get("opc") == expected["opc"], f"subscriber {imsi} OPc mismatch")
    require(security.get("op") is None, f"subscriber {imsi} must use OPc, not OP")

    slices = document.get("slice")
    require(type(slices) is list and len(slices) == 1, f"subscriber {imsi}: expected one slice")
    entry = slices[0]
    require(type(entry) is dict, f"subscriber {imsi} slice entry is not an object")
    require(entry.get("sst") == 1, f"subscriber {imsi} SST is not 1")
    require(entry.get("default_indicator") is True, f"subscriber {imsi} slice is not the default")

    sessions = entry.get("session")
    require(
        type(sessions) is list and len(sessions) == 2,
        f"subscriber {imsi}: expected exactly internet and IMS sessions",
    )
    internet = [item for item in sessions if item.get("name") == "internet"]
    ims = [item for item in sessions if item.get("name") == "ims"]
    require(len(internet) == 1 and len(ims) == 1, f"subscriber {imsi} session names are not exact")
    internet_session = internet[0]
    require(internet_session.get("type") == 3, f"subscriber {imsi} internet session is not IPv4")
    qos = internet_session.get("qos")
    require(type(qos) is dict, f"subscriber {imsi} internet QoS object is absent")
    require(qos.get("index") == expected["qci"], f"subscriber {imsi} internet QCI mismatch")
    ue = internet_session.get("ue")
    require(type(ue) is dict, f"subscriber {imsi} static UE address object is absent")
    require(ue.get("ipv4") == expected["ipv4"], f"subscriber {imsi} static UE IPv4 mismatch")


def fixture(imsi: str, ipv4: str) -> dict[str, Any]:
    return {
        "imsi": imsi,
        "security": {
            "k": "00112233445566778899aabbccddeeff",
            "amf": "8000",
            "op": None,
            "opc": "63bfa50ee6523365ff14c1f45f88737d",
        },
        "slice": [
            {
                "sst": 1,
                "default_indicator": True,
                "session": [
                    {"name": "internet", "type": 3, "qos": {"index": 9}, "ue": {"ipv4": ipv4}},
                    {"name": "ims", "type": 3},
                ],
            }
        ],
    }


def self_test() -> None:
    expected = {
        "imsi": "001010123456780",
        "k": "00112233445566778899aabbccddeeff",
        "opc": "63bfa50ee6523365ff14c1f45f88737d",
        "amf": "8000",
        "qci": 9,
        "ipv4": "10.45.1.2",
    }
    verify_document(fixture(expected["imsi"], expected["ipv4"]), expected)
    # A document belonging to the OTHER UE must not satisfy this UE's
    # expectation; that is the failure the single-UE verifier cannot express.
    try:
        verify_document(fixture("001010123456781", "10.45.1.3"), expected)
    except ValueError:
        pass
    else:
        raise AssertionError("multi-UE subscriber verifier did not fail closed")
    print("event=native_open5gs_subscribers_multi_self_test result=pass")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mongodb-host", default="127.0.0.1")
    parser.add_argument("--mongodb-port", type=int, default=27017)
    parser.add_argument("--subscriber-csv", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        if args.subscriber_csv is not None:
            parser.error("--self-test cannot be combined with --subscriber-csv")
        self_test()
        return 0
    if args.mongodb_host != "127.0.0.1" or args.mongodb_port != 27017:
        parser.error("the native multi-UE gate fixes MongoDB to 127.0.0.1:27017")
    if args.subscriber_csv is None:
        parser.error("--subscriber-csv is required")
    expectations = parse_csv(args.subscriber_csv)

    try:
        import pymongo
    except ImportError as error:
        raise RuntimeError("pymongo is unavailable in the native environment") from error

    client = pymongo.MongoClient(
        args.mongodb_host, args.mongodb_port, serverSelectionTimeoutMS=1000, connectTimeoutMS=1000
    )
    deadline = time.monotonic() + 10.0
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            client.admin.command("ping")
            collection = client["open5gs"]["subscribers"]
            total = collection.count_documents({})
            require(
                total == len(expectations),
                f"expected exactly {len(expectations)} subscriber documents, found {total}",
            )
            for expected in expectations:
                document = collection.find_one({"imsi": expected["imsi"]})
                require(document is not None, f"subscriber IMSI is absent: {expected['imsi']}")
                verify_document(document, expected)
            joined = " ".join(f"{e['imsi']}={e['ipv4']}" for e in expectations)
            print(f"event=native_open5gs_subscribers_verified count={len(expectations)} {joined}")
            return 0
        except (pymongo.errors.PyMongoError, ValueError) as error:
            last_error = error
            time.sleep(0.25)
    raise RuntimeError(f"subscriber verification failed: {last_error}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, UnicodeError, ValueError, RuntimeError) as error:
        print(f"Open5GS subscriber verification failed: {error}", file=sys.stderr)
        raise SystemExit(2)
