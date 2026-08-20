#!/usr/bin/env python3
"""Verify the native workspace's locked inputs without mutating it."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def command(*args: str) -> str:
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT).strip()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument(
        "--allow-missing-cache",
        action="store_true",
        help="validate lock syntax and present files while listing missing cache inputs",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    repo_root = args.repo_root.resolve()
    with args.lock.open("r", encoding="utf-8") as source:
        lock = json.load(source, object_pairs_hook=reject_duplicate_keys)
    require(type(lock) is dict and lock.get("schema") == 1, "unsupported lock schema")

    platform_lock = lock["platform"]
    os_release: dict[str, str] = {}
    for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            os_release[key] = value.strip('"')
    require(os_release.get("ID") == platform_lock["os_id"], "host OS id mismatch")
    require(os_release.get("VERSION_ID") == platform_lock["version_id"], "host OS version mismatch")
    require(os_release.get("VERSION_CODENAME") == platform_lock["codename"], "host codename mismatch")
    require(command("dpkg", "--print-architecture") == platform_lock["architecture"], "host dpkg architecture mismatch")
    require(platform.machine() == platform_lock["machine"], "host machine mismatch")

    overlay = lock["debian_overlay"]
    manifest = repo_root / overlay["manifest"]
    require(manifest.is_file(), f"missing Debian manifest: {manifest}")
    require(manifest.stat().st_size == overlay["bytes"], "Debian manifest size mismatch")
    require(sha256(manifest) == overlay["sha256"], "Debian manifest checksum mismatch")
    lines = manifest.read_text(encoding="utf-8").splitlines()
    require(
        lines and lines[0] == "# package\tversion\tarchitecture\tbytes\tsha256\trepository_path\tcache_path",
        "Debian manifest header mismatch",
    )
    rows = [line.split("\t") for line in lines[1:]]
    require(len(rows) == overlay["rows"], "Debian manifest row count mismatch")
    require(all(len(row) == 7 for row in rows), "Debian manifest has a non-seven-column row")
    identities: set[tuple[str, str, str]] = set()
    cache_paths: set[str] = set()
    missing: list[str] = []
    for package, version, architecture, size, digest, _, cache_path in rows:
        identity = (package, version, architecture)
        require(identity not in identities, f"duplicate Debian identity: {identity}")
        require(cache_path not in cache_paths, f"duplicate Debian cache path: {cache_path}")
        identities.add(identity)
        cache_paths.add(cache_path)
        path = root / cache_path
        if not path.is_file():
            missing.append(cache_path)
            continue
        require(path.stat().st_size == int(size), f"Debian archive size mismatch: {cache_path}")
        require(sha256(path) == digest, f"Debian archive checksum mismatch: {cache_path}")
        fields = [
            command("dpkg-deb", "-f", str(path), field)
            for field in ("Package", "Version", "Architecture")
        ]
        require(fields == [package, version, architecture], f"Debian archive identity mismatch: {cache_path}")

    for archive in lock["archives"]:
        path = root / archive["cache_path"]
        if not path.is_file():
            missing.append(archive["cache_path"])
            continue
        require(path.stat().st_size == archive["bytes"], f"archive size mismatch: {archive['name']}")
        require(sha256(path) == archive["sha256"], f"archive checksum mismatch: {archive['name']}")
        sidecar = archive.get("sha256_sidecar")
        if sidecar:
            sidecar_path = root / sidecar["cache_path"]
            if not sidecar_path.is_file():
                missing.append(sidecar["cache_path"])
            else:
                require(sidecar_path.stat().st_size == sidecar["bytes"], "archive sidecar size mismatch")
                require(sha256(sidecar_path) == sidecar["sha256"], "archive sidecar checksum mismatch")

    for source in lock["git_sources"]:
        path = root / source["path"]
        if not (path / ".git").exists():
            missing.append(source["path"])
            continue
        require(command("git", "-C", str(path), "rev-parse", "HEAD") == source["commit"], f"Git revision mismatch: {source['name']}")
        require(command("git", "-C", str(path), "status", "--porcelain") == "", f"Git checkout is dirty: {source['name']}")
        missing_objects = command(
            "git", "-C", str(path), "rev-list", "--objects", "--missing=print", "HEAD"
        )
        require(
            not any(line.startswith("?") for line in missing_objects.splitlines()),
            f"Git checkout has missing objects: {source['name']}",
        )

    if missing and not args.allow_missing_cache:
        raise ValueError("missing locked inputs: " + ", ".join(missing))
    print(f"lock_validation=ok debs={len(rows)} archives={len(lock['archives'])} git_sources={len(lock['git_sources'])}")
    for value in missing:
        print(f"lock_missing={value}")
    print(f"claim_boundary={lock['claim_boundary']['not_claimed']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, subprocess.CalledProcessError, ValueError, json.JSONDecodeError) as error:
        print(f"lock_validation=failed error={error}", file=sys.stderr)
        raise SystemExit(1)
