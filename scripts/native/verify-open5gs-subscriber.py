#!/usr/bin/env python3
"""Fail-closed verification of the one native legacy Open5GS subscriber."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any


EXPECTED = {
    "imsi": "001010123456780",
    "k": "00112233445566778899aabbccddeeff",
    "opc": "63bfa50ee6523365ff14c1f45f88737d",
    "amf": "8000",
    "qci": 9,
    "ipv4": "10.45.1.2",
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def verify_document(document: dict[str, Any]) -> None:
    require(type(document) is dict, "subscriber document is not an object")
    require(document.get("imsi") == EXPECTED["imsi"], "subscriber IMSI mismatch")

    security = document.get("security")
    require(type(security) is dict, "subscriber security object is absent")
    require(security.get("k") == EXPECTED["k"], "subscriber K mismatch")
    require(security.get("amf") == EXPECTED["amf"], "subscriber AMF mismatch")
    require(security.get("opc") == EXPECTED["opc"], "subscriber OPc mismatch")
    require(security.get("op") is None, "subscriber must use OPc, not OP")

    slices = document.get("slice")
    require(type(slices) is list and len(slices) == 1, "expected exactly one slice")
    legacy_slice = slices[0]
    require(type(legacy_slice) is dict, "slice entry is not an object")
    require(legacy_slice.get("sst") == 1, "subscriber SST is not 1")
    require(
        legacy_slice.get("default_indicator") is True,
        "subscriber slice is not the default",
    )
    sessions = legacy_slice.get("session")
    require(
        type(sessions) is list and len(sessions) == 2,
        "expected exactly internet and IMS sessions",
    )
    internet = [item for item in sessions if item.get("name") == "internet"]
    ims = [item for item in sessions if item.get("name") == "ims"]
    require(len(internet) == 1 and len(ims) == 1, "session names are not exact")
    internet_session = internet[0]
    require(internet_session.get("type") == 3, "internet session is not IPv4")
    qos = internet_session.get("qos")
    require(type(qos) is dict, "internet QoS object is absent")
    require(qos.get("index") == EXPECTED["qci"], "internet QCI mismatch")
    ue = internet_session.get("ue")
    require(type(ue) is dict, "static UE address object is absent")
    require(ue.get("ipv4") == EXPECTED["ipv4"], "static UE IPv4 mismatch")


def validate_csv(path: Path) -> None:
    require(path.is_absolute(), "subscriber CSV path must be absolute")
    require(path.is_file() and not path.is_symlink(), "subscriber CSV is missing or symlinked")
    lines = path.read_text(encoding="utf-8", errors="strict").splitlines()
    records = [line for line in lines if not line.startswith("#")]
    expected = (
        "legacy-ue,001010123456780,00112233445566778899aabbccddeeff,"
        "opc,63bfa50ee6523365ff14c1f45f88737d,8000,9,10.45.1.2"
    )
    require(records == [expected], "subscriber CSV is not the audited single UE record")


def fixture() -> dict[str, Any]:
    return {
        "imsi": EXPECTED["imsi"],
        "security": {
            "k": EXPECTED["k"],
            "amf": EXPECTED["amf"],
            "op": None,
            "opc": EXPECTED["opc"],
        },
        "slice": [
            {
                "sst": 1,
                "default_indicator": True,
                "session": [
                    {
                        "name": "internet",
                        "type": 3,
                        "qos": {"index": EXPECTED["qci"]},
                        "ue": {"ipv4": EXPECTED["ipv4"]},
                    },
                    {"name": "ims", "type": 3},
                ],
            }
        ],
    }


def self_test() -> None:
    verify_document(fixture())
    broken = fixture()
    broken["slice"][0]["session"][0]["ue"]["ipv4"] = "10.45.1.3"
    try:
        verify_document(broken)
    except ValueError:
        pass
    else:
        raise AssertionError("subscriber verifier did not fail closed")
    print("event=native_open5gs_subscriber_self_test result=pass")


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
        parser.error("the native legacy gate fixes MongoDB to 127.0.0.1:27017")
    if args.subscriber_csv is None:
        parser.error("--subscriber-csv is required")
    validate_csv(args.subscriber_csv)

    try:
        import pymongo
    except ImportError as error:
        raise RuntimeError("pymongo is unavailable in the native environment") from error

    client = pymongo.MongoClient(
        args.mongodb_host,
        args.mongodb_port,
        serverSelectionTimeoutMS=1000,
        connectTimeoutMS=1000,
    )
    deadline = time.monotonic() + 10.0
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            client.admin.command("ping")
            collection = client["open5gs"]["subscribers"]
            total = collection.count_documents({})
            require(total == 1, f"expected exactly one subscriber document, found {total}")
            document = collection.find_one({"imsi": EXPECTED["imsi"]})
            require(document is not None, "audited subscriber IMSI is absent")
            verify_document(document)
            print(
                "event=native_open5gs_subscriber_verified "
                f"imsi={EXPECTED['imsi']} ipv4={EXPECTED['ipv4']}"
            )
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
