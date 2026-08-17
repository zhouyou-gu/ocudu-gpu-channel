#!/usr/bin/env python3
"""Verify a live MIMO run against the matrix its topology declares.

This is the M5 exit-gate row that the transport checks cannot reach: "each
received row depends on both transmit ports". Counting bytes, transactions and
sibling reply sizes shows that four endpoints exchanged IQ in lock step; none of
it shows that the declared `H` was applied, and a broker that relayed each port
straight through would satisfy every one of those checks.

The evidence used here is the broker's wire capture (`--wire-capture-dir`): the
first N samples each port pulled off its peer's TX, and the first N it replied
with on its own RX. Both are taken at the socket boundary, so this script never
reads the broker's opinion of what it did.

For every link `A -> B` in the topology it recomputes

    y_r[n] = sum_t H[r][t] * x_t[n]

from A's captured TX columns and compares against B's captured RX rows, where
`H` is read from the topology YAML rather than from anything the broker emitted.
It then reports how much of each row's energy came from the OFF-diagonal terms,
which is the quantity that makes "depends on both ports" a measurement instead
of an assertion: a broker running two independent 1x1 lanes scores zero there.

Where a node's transmitted samples are analytically known -- the two-port test
peer emits a cumulative marker whose value is a closed form of (port, ordinal)
-- the captured column is also checked against that closed form. That is the
common-sample-epoch check: it fails if the sibling columns were read from
different origins, even when the matrix arithmetic itself is right.

Exit status is 0 only if every check passed.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys

import numpy as np
import yaml

# The two-port transport peer's TX marker (apps/ocudu_mimo_transport_peer.cpp,
# cumulative_marker). Two exact power-of-two fractions carry the low and high
# 16-bit words of the absolute sample ordinal; the port sign separates the
# columns. Every value is a multiple of 2**-17 inside [0.25, 0.75), so it is
# exactly representable in float32 and the comparison below can be exact.
MARKER_SCALE = 1.0 / 131072.0


def peer_marker(port: int, ordinals: np.ndarray) -> np.ndarray:
    low = (ordinals & 0xFFFF).astype(np.float32)
    high = ((ordinals >> 16) & 0xFFFF).astype(np.float32)
    sign = np.float32(1.0) if port == 0 else np.float32(-1.0)
    real = sign * (np.float32(0.25) + low * np.float32(MARKER_SCALE))
    imag = np.float32(-0.25) + high * np.float32(MARKER_SCALE)
    return real.astype(np.complex64) + 1j * imag.astype(np.complex64)


def read_cf32(path: str) -> np.ndarray:
    raw = np.fromfile(path, dtype=np.float32)
    if raw.size % 2 != 0:
        raise SystemExit(f"capture is not a whole number of cf32 samples: {path}")
    return (raw[0::2] + 1j * raw[1::2]).astype(np.complex64)


class Checks:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.measurements: dict[str, float | int | str] = {}

    def require(self, condition: bool, message: str) -> bool:
        if not condition:
            self.failures.append(message)
        return condition

    def measure(self, name: str, value) -> None:
        self.measurements[name] = value
        print(f"measured {name}={value}")


def matrix_from_model(model: dict, name: str, nr: int, nt: int, checks: Checks):
    """Read the declared H, and refuse to guess when the chain is not identity.

    `fixed_mimo` folds its coefficients into each lane's tap gain and phase, so
    the analytic expectation below is only `y = H x` when the rest of the chain
    is a single zero-delay, 0 dB, 0 rad tap. Anything else (a real TDL profile,
    fading, AWGN, CFO) makes the closed form wrong, and this script says so
    rather than comparing against an expectation it cannot justify.
    """
    fixed = model.get("fixed_mimo")
    if not isinstance(fixed, dict):
        checks.require(False, f"model {name} declares no fixed_mimo matrix")
        return None

    chain = model.get("chain") or []
    if len(chain) != 1 or chain[0].get("type") != "tdl":
        checks.require(False, f"model {name} chain is not a single tdl step")
        return None
    taps = chain[0].get("taps") or []
    if len(taps) != 1:
        checks.require(False, f"model {name} tdl step is not single-tap")
        return None
    tap = taps[0]
    identity = (
        float(tap.get("delay_samples", 0.0)) == 0.0
        and float(tap.get("gain_db", 0.0)) == 0.0
        and float(tap.get("phase_rad", 0.0)) == 0.0
        and "fading" not in chain[0]
    )
    if not checks.require(identity, f"model {name} tap is not an identity carrier"):
        return None

    matrix = np.zeros((nr, nt), dtype=np.complex128)
    seen = set()
    for coefficient in fixed.get("coefficients") or []:
        if int(coefficient.get("tap", 0)) != 0:
            checks.require(False, f"model {name} declares a coefficient beyond tap 0")
            return None
        r = int(coefficient["rx"])
        t = int(coefficient["tx"])
        if not checks.require(
            0 <= r < nr and 0 <= t < nt, f"model {name} coefficient ({r},{t}) is out of range"
        ):
            return None
        seen.add((r, t))
        matrix[r][t] = complex(float(coefficient.get("real", 0.0)), float(coefficient.get("imag", 0.0)))
    checks.measure(f"{name}_declared_coefficients", len(seen))
    return matrix


def check_link(
    checks: Checks,
    label: str,
    matrix: np.ndarray,
    columns: list[np.ndarray],
    rows: list[np.ndarray],
    tolerance: float,
    cross_floor: float,
    allowed_silent: frozenset = frozenset(),
) -> None:
    usable = min([column.size for column in columns] + [row.size for row in rows])
    if not checks.require(usable > 0, f"{label}: nothing was captured on one of the ports"):
        return
    checks.measure(f"{label}_compared_samples", int(usable))

    columns = [column[:usable].astype(np.complex128) for column in columns]
    rows = [row[:usable].astype(np.complex128) for row in rows]

    # The source must actually carry signal, or every downstream number here is
    # a comparison of zero against zero.
    for t, column in enumerate(columns):
        rms = float(np.sqrt(np.mean(np.abs(column) ** 2)))
        checks.measure(f"{label}_tx{t}_rms", round(rms, 6))
        if t in allowed_silent:
            # A declared-silent source (e.g. srsRAN never radiates SSB/common
            # channels or rank-1 PDSCH on ports > 0) is recorded, not failed.
            checks.measure(f"{label}_tx{t}_declared_silent", 1)
            continue
        checks.require(rms > 0.0, f"{label}: source port {t} captured only zeros")

    for r, row in enumerate(rows):
        expected = np.zeros(usable, dtype=np.complex128)
        contributions = []
        for t, column in enumerate(columns):
            term = matrix[r][t] * column
            expected += term
            contributions.append(float(np.sqrt(np.mean(np.abs(term) ** 2))))

        error = np.abs(row - expected)
        max_error = float(error.max())
        expected_rms = float(np.sqrt(np.mean(np.abs(expected) ** 2)))
        row_rms = float(np.sqrt(np.mean(np.abs(row) ** 2)))
        checks.measure(f"{label}_row{r}_rms", round(row_rms, 6))
        checks.measure(f"{label}_row{r}_max_abs_error", float(f"{max_error:.3e}"))
        checks.measure(f"{label}_row{r}_tolerance", tolerance)
        checks.require(
            max_error <= tolerance,
            f"{label}: row {r} differs from the declared matrix product "
            f"(max |y - Hx| = {max_error:.3e} > {tolerance:.3e})",
        )

        # "Depends on both ports", measured: the share of the row's amplitude
        # that came from columns other than the diagonal one. A relay running
        # independent per-port lanes scores 0 here; so does a matrix whose cross
        # coefficients were dropped.
        live_columns = [t for t in range(len(columns)) if t not in allowed_silent]
        if len(live_columns) > 1 and expected_rms > 0.0:
            diagonal = contributions[r] if r < len(contributions) else 0.0
            off_diagonal = math.sqrt(max(0.0, sum(c * c for c in contributions) - diagonal * diagonal))
            share = off_diagonal / expected_rms
            checks.measure(f"{label}_row{r}_off_diagonal_share", round(share, 6))
            checks.require(
                share >= cross_floor,
                f"{label}: row {r} carries {share:.6f} of its amplitude from other transmit "
                f"ports, below the {cross_floor} floor -- this row does not depend on both",
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--capture-dir", required=True)
    parser.add_argument("--topology", required=True)
    parser.add_argument("--marker-node", default="", help="node whose TX is an analytic marker")
    parser.add_argument("--report", default="")
    parser.add_argument(
        "--tolerance",
        type=float,
        default=1e-4,
        help="max |y - Hx|; fixed_mimo folds each coefficient through dB/rad, "
        "so exact equality is not available and the round trip costs ~1e-7",
    )
    parser.add_argument("--cross-floor", type=float, default=0.05)
    parser.add_argument(
        "--allow-silent-source",
        action="append",
        default=[],
        metavar="FROM>TO:TXIDX",
        help="declare that this link's TX column may be all-zero on the wire "
        "(recorded, not failed; excluded from the cross-share check)",
    )
    args = parser.parse_args()

    checks = Checks()

    with open(args.topology, "r", encoding="utf-8") as handle:
        topology = yaml.safe_load(handle)
    manifest_path = os.path.join(args.capture_dir, "wire-capture.json")
    with open(manifest_path, "r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    checks.require(
        manifest.get("schema") == "ocudu-wire-capture/v1", "wire capture manifest schema mismatch"
    )

    nodes = {node["id"]: node for node in topology.get("radio_nodes") or []}
    models = topology.get("models") or {}
    if not checks.require(bool(nodes), "topology declares no radio_nodes"):
        return 1

    captured = {entry["id"]: entry for entry in manifest.get("ports") or []}
    for node_id, node in nodes.items():
        for role in ("tx_ports", "rx_ports"):
            for index, port in enumerate(node.get(role) or []):
                entry = captured.get(port)
                if not checks.require(entry is not None, f"no wire capture for port {port}"):
                    continue
                # The manifest states the membership the BROKER resolved. If it
                # disagrees with the topology, the rest of this script would be
                # binding files to the wrong row or column.
                checks.require(
                    entry["node"] == node_id,
                    f"broker placed {port} in node {entry['node']}, topology says {node_id}",
                )
                checks.require(
                    entry["tx_port" if role == "tx_ports" else "rx_port"] == index,
                    f"broker gave {port} a different {role} index than the topology",
                )

    def load(port: str, direction: str) -> np.ndarray:
        return read_cf32(os.path.join(args.capture_dir, f"{port}.{direction}.cf32"))

    # Analytic-marker check: the peer's transmitted columns are a closed form of
    # (port, ordinal) starting at ordinal 0, so this pins the sample epoch. A
    # sibling column read from a different origin fails here even though the
    # matrix arithmetic on it would still look self-consistent.
    skip = int(manifest.get("skip_samples", 0))
    checks.measure("capture_skip_samples", skip)
    if args.marker_node:
        node = nodes.get(args.marker_node)
        if checks.require(node is not None, f"marker node {args.marker_node} is not declared"):
            for t, port in enumerate(node.get("tx_ports") or []):
                captured_column = load(port, "tx_in")
                # The recorded window starts at absolute wire sample `skip`, so
                # the ordinal of the first captured sample is `skip` too. Using
                # 0 here would turn a correct run into a mismatch on every
                # sample, which is the kind of check that gets a tolerance
                # widened instead of a bug found.
                ordinals = np.arange(skip, skip + captured_column.size, dtype=np.uint64)
                expected = peer_marker(t, ordinals)
                mismatches = int(np.count_nonzero(captured_column != expected))
                checks.measure(f"marker_{port}_samples", int(captured_column.size))
                checks.measure(f"marker_{port}_mismatches", mismatches)
                checks.require(captured_column.size > 0, f"{port} captured no TX samples")
                checks.require(
                    mismatches == 0,
                    f"{port} TX does not match the analytic marker from ordinal 0 "
                    f"({mismatches} of {captured_column.size} samples differ)",
                )

    for link in topology.get("links") or []:
        source_id = link["from"]
        destination_id = link["to"]
        source = nodes.get(source_id)
        destination = nodes.get(destination_id)
        if not checks.require(
            source is not None and destination is not None,
            f"link {source_id}->{destination_id} does not connect two declared nodes",
        ):
            continue
        model_name = link["model"]
        model = models.get(model_name)
        if not checks.require(model is not None, f"link model {model_name} is not declared"):
            continue
        tx_ports = source.get("tx_ports") or []
        rx_ports = destination.get("rx_ports") or []
        matrix = matrix_from_model(model, model_name, len(rx_ports), len(tx_ports), checks)
        if matrix is None:
            continue
        link_label = f"{source_id}->{destination_id}"
        allowed_silent = frozenset(
            int(spec.rsplit(":", 1)[1])
            for spec in args.allow_silent_source
            if spec.rsplit(":", 1)[0] == link_label
        )
        check_link(
            checks,
            link_label,
            matrix,
            [load(port, "tx_in") for port in tx_ports],
            [load(port, "rx_out") for port in rx_ports],
            args.tolerance,
            args.cross_floor,
            allowed_silent,
        )

    passed = not checks.failures
    for failure in checks.failures:
        print(f"FAIL {failure}", file=sys.stderr)
    print(f"matrix_capture_status={'passed' if passed else 'failed'}")

    if args.report:
        with open(args.report, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "schema": "ocudu-mimo-matrix-capture/v1",
                    "status": "passed" if passed else "failed",
                    "topology": os.path.abspath(args.topology),
                    "capture_dir": os.path.abspath(args.capture_dir),
                    "measurements": checks.measurements,
                    "errors": checks.failures,
                },
                handle,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")

    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
