#!/usr/bin/env python3
"""Convert Sionna CIR paths into ocudu-gpu-channel profile swaps.

This module deliberately depends only on the Python standard library so its
conversion and JSON-contract tests run on machines without Sionna or pyzmq.
The live bridge imports those optional dependencies at runtime.
"""

from __future__ import annotations

import cmath
import json
import math
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping, Sequence


@dataclass(frozen=True)
class Ray:
    """One valid baseband-equivalent Sionna propagation path."""

    delay_seconds: float
    coefficient: complex
    doppler_hz: float = 0.0


@dataclass(frozen=True)
class Tap:
    """One tap accepted by the emulator's runtime ``profile_swap``."""

    delay_samples: float
    gain_db: float
    phase_rad: float


@dataclass(frozen=True)
class LaneProfile:
    """One row-major (rx_port, tx_port) entry of a MIMO channel matrix."""

    rx_port: int
    tx_port: int
    taps: tuple[Tap, ...]


@dataclass(frozen=True)
class MatrixProfile:
    """All Nr x Nt lane profiles for one physical directed link."""

    nt: int
    nr: int
    lanes: tuple[LaneProfile, ...]


def control_link_id(source: str, destination: str, model: str) -> str:
    """Return the canonical C++ ``link_key`` used by the control server."""

    return f"{source}>{destination}:{model}"


def rays_to_taps(
    rays: Iterable[Ray],
    *,
    sample_rate_hz: float,
    gain_offset_db: float = 0.0,
    max_taps: int = 32,
    max_delay_samples: float = 1023.0,
    min_gain_db: float = -100.0,
    max_gain_db: float = 20.0,
    delay_quantum_samples: float = 1.0 / 16.0,
    outage_gain_db: float = -100.0,
) -> list[Tap]:
    """Quantize, coherently merge, prune, and convert Sionna CIR paths.

    Sionna can return many paths and arbitrarily close delays. The emulator
    accepts at most 32 unique delays, so paths in the same fractional-sample
    bin are summed as complex coefficients before the strongest bins are
    retained. ``gain_offset_db`` is a fixed calibration offset between
    Sionna's physical field scale and the normalized IQ scale used by the
    software radios; it preserves all relative and temporal path changes.
    """

    if sample_rate_hz <= 0.0:
        raise ValueError("sample_rate_hz must be positive")
    if max_taps <= 0:
        raise ValueError("max_taps must be positive")
    if delay_quantum_samples <= 0.0:
        raise ValueError("delay_quantum_samples must be positive")
    if not (min_gain_db <= outage_gain_db <= max_gain_db):
        raise ValueError("outage_gain_db must be inside the accepted gain range")

    bins: dict[int, complex] = {}
    for ray in rays:
        delay = float(ray.delay_seconds) * sample_rate_hz
        coeff = complex(ray.coefficient)
        if not math.isfinite(delay) or delay < 0.0 or delay > max_delay_samples:
            continue
        if not (math.isfinite(coeff.real) and math.isfinite(coeff.imag)):
            continue
        if abs(coeff) == 0.0:
            continue
        bin_index = int(round(delay / delay_quantum_samples))
        quantized_delay = bin_index * delay_quantum_samples
        if quantized_delay > max_delay_samples:
            continue
        bins[bin_index] = bins.get(bin_index, 0.0j) + coeff

    candidates: list[tuple[int, complex, float]] = []
    for bin_index, coeff in bins.items():
        magnitude = abs(coeff)
        if magnitude == 0.0:
            continue
        gain_db = 20.0 * math.log10(magnitude) + gain_offset_db
        if gain_db < min_gain_db:
            continue
        candidates.append((bin_index, coeff, min(gain_db, max_gain_db)))

    # Tap count is bounded by the device ABI. Select by power, then restore
    # causal delay ordering for the profile sent to the C++ backend.
    candidates.sort(key=lambda item: abs(item[1]), reverse=True)
    candidates = candidates[:max_taps]
    candidates.sort(key=lambda item: item[0])

    taps = [
        Tap(
            delay_samples=round(bin_index * delay_quantum_samples, 9),
            gain_db=round(gain_db, 9),
            phase_rad=round(cmath.phase(coeff), 9),
        )
        for bin_index, coeff, gain_db in candidates
    ]
    if taps:
        return taps

    # profile_swap requires a non-empty tap list. -100 dB is the quietest
    # channel currently representable by its public schema.
    return [Tap(delay_samples=0.0, gain_db=outage_gain_db, phase_rad=0.0)]


def make_profile_swap(
    link_id: str,
    taps: Sequence[Tap],
    *,
    batch_id: str | None = None,
    take_effect_at_slot: int | None = None,
) -> dict[str, Any]:
    """Build one v2 control-plane profile-swap request."""

    if not link_id:
        raise ValueError("link_id must not be empty")
    if not taps:
        raise ValueError("profile_swap requires at least one tap")

    message: dict[str, Any] = {
        "type": "profile_swap",
        "link_id": link_id,
        "taps": [asdict(tap) for tap in taps],
        # Sionna already supplied instantaneous complex path coefficients.
        # Enabling the emulator's stochastic Jakes process would double-model
        # mobility, so the bridge explicitly disables it.
        "fading": {
            "enabled": False,
            "f_d_max_hz": 0.0,
            "spectrum": "jakes",
            "grid_us": 100.0,
        },
    }
    if batch_id is not None:
        message["batch_id"] = batch_id
    if take_effect_at_slot is not None:
        message["take_effect_at_slot"] = int(take_effect_at_slot)
    return message


def make_matrix_profile_swap(
    link_id: str,
    profile: MatrixProfile,
    *,
    batch_id: str | None = None,
    take_effect_at_slot: int | None = None,
) -> dict[str, Any]:
    """Build one physical-link-atomic Nr x Nt profile replacement."""

    if not link_id:
        raise ValueError("link_id must not be empty")
    if profile.nt <= 0 or profile.nr <= 0:
        raise ValueError("matrix profile dimensions must be positive")
    expected = profile.nt * profile.nr
    if len(profile.lanes) != expected:
        raise ValueError("matrix profile requires exactly Nr x Nt lanes")
    positions = {(lane.rx_port, lane.tx_port) for lane in profile.lanes}
    required = {
        (rx_port, tx_port)
        for rx_port in range(profile.nr)
        for tx_port in range(profile.nt)
    }
    if positions != required or any(not lane.taps for lane in profile.lanes):
        raise ValueError("matrix profile lanes must cover every port pair exactly once")

    message: dict[str, Any] = {
        "type": "matrix_profile_swap",
        "link_id": link_id,
        "nt": profile.nt,
        "nr": profile.nr,
        "lanes": [
            {
                "rx_port": lane.rx_port,
                "tx_port": lane.tx_port,
                "taps": [asdict(tap) for tap in lane.taps],
            }
            for lane in sorted(
                profile.lanes, key=lambda item: item.rx_port * profile.nt + item.tx_port
            )
        ],
    }
    if batch_id is not None:
        message["batch_id"] = batch_id
    if take_effect_at_slot is not None:
        message["take_effect_at_slot"] = int(take_effect_at_slot)
    return message


def channel_status(
    link_id: str,
    rays: Sequence[Ray],
    taps: Sequence[Tap],
    *,
    sample_rate_hz: float | None = None,
) -> dict[str, Any]:
    """Return a compact JSON-serializable status record for logging."""

    ray_power = sum(abs(ray.coefficient) ** 2 for ray in rays)
    total_power_db = 10.0 * math.log10(ray_power) if ray_power > 0.0 else None
    delay_doppler_points = []
    for index, ray in enumerate(rays):
        delay_seconds = float(ray.delay_seconds)
        doppler_hz = float(ray.doppler_hz)
        path_power = abs(complex(ray.coefficient)) ** 2
        if not (
            math.isfinite(delay_seconds)
            and math.isfinite(doppler_hz)
            and math.isfinite(path_power)
            and path_power > 0.0
        ):
            continue
        delay_doppler_points.append(
            {
                "path_index": index,
                "delay_samples": (
                    delay_seconds * sample_rate_hz
                    if sample_rate_hz is not None and sample_rate_hz > 0.0
                    else None
                ),
                "delay_ns": delay_seconds * 1.0e9,
                "doppler_hz": doppler_hz,
                # This is raw Sionna path power. The emulator gain offset is
                # deliberately not applied to this diagnostic representation.
                "power_db": 10.0 * math.log10(path_power),
            }
        )
    point_count = len(delay_doppler_points)
    # Keep status JSON bounded if a detailed scene produces many paths. Retain
    # the strongest paths, then restore a stable delay/Doppler display order.
    delay_doppler_points.sort(key=lambda item: item["power_db"], reverse=True)
    delay_doppler_points = delay_doppler_points[:128]
    delay_doppler_points.sort(
        key=lambda item: (item["delay_samples"] or 0.0, item["doppler_hz"])
    )
    delays = [tap.delay_samples for tap in taps]
    tap_rows = []
    for index, tap in enumerate(taps):
        delay_ns = (
            tap.delay_samples / sample_rate_hz * 1.0e9
            if sample_rate_hz is not None and sample_rate_hz > 0.0
            else None
        )
        tap_rows.append(
            {
                "index": index,
                "delay_samples": tap.delay_samples,
                "delay_ns": delay_ns,
                "gain_db": tap.gain_db,
                "phase_rad": tap.phase_rad,
                "phase_deg": math.degrees(tap.phase_rad),
            }
        )
    return {
        "link_id": link_id,
        "ray_count": len(rays),
        "tap_count": len(taps),
        "total_path_power_db": total_power_db,
        "strongest_tap_gain_db": max(tap.gain_db for tap in taps),
        "first_delay_samples": min(delays),
        "last_delay_samples": max(delays),
        "delay_doppler_point_count": point_count,
        "delay_doppler_points": delay_doppler_points,
        # UI/status evidence only. The same Tap objects are serialized into
        # profile_swap separately; scene geometry is never added there.
        "taps": tap_rows,
    }


class ZmqControlClient:
    """Small REQ client with checked JSON replies and atomic profile batches."""

    def __init__(self, endpoint: str, timeout_ms: int = 5000) -> None:
        try:
            import zmq  # type: ignore
        except ImportError as exc:  # pragma: no cover - live dependency path
            raise RuntimeError(
                "pyzmq is required for live control; install requirements.txt"
            ) from exc

        self._zmq = zmq
        self._endpoint = endpoint
        self._timeout_ms = timeout_ms
        self._context = zmq.Context.instance()
        self._socket = self._context.socket(zmq.REQ)
        self._socket.setsockopt(zmq.LINGER, 0)
        self._socket.setsockopt(zmq.RCVTIMEO, timeout_ms)
        self._socket.setsockopt(zmq.SNDTIMEO, timeout_ms)
        self._socket.connect(endpoint)

    def close(self) -> None:
        self._socket.close(linger=0)

    def request(self, message: Mapping[str, Any]) -> dict[str, Any]:
        request_type = message.get("type", "scalar")
        try:
            self._socket.send_string(json.dumps(dict(message), separators=(",", ":")))
            reply = json.loads(self._socket.recv_string())
        except self._zmq.Again as exc:
            raise RuntimeError(
                f"control request {request_type!r} timed out after "
                f"{self._timeout_ms} ms waiting for {self._endpoint}; "
                "start ocudu-gpu-channel with --control-endpoint first"
            ) from exc
        if not isinstance(reply, dict) or not reply.get("ok", False):
            raise RuntimeError(f"control request rejected: {reply!r}")
        return reply

    def send_profiles(
        self, profiles: Mapping[str, Sequence[Tap]], *, batch_id: str
    ) -> dict[str, Any]:
        self.request({"type": "batch_begin", "id": batch_id})
        try:
            for link_id, taps in profiles.items():
                self.request(
                    make_profile_swap(link_id, taps, batch_id=batch_id)
                )
            return self.request({"type": "batch_commit", "id": batch_id})
        except Exception:
            try:
                self.request({"type": "batch_abort", "id": batch_id})
            except Exception:
                pass
            raise

    def send_matrix_profiles(
        self, profiles: Mapping[str, MatrixProfile], *, batch_id: str
    ) -> dict[str, Any]:
        self.request({"type": "batch_begin", "id": batch_id})
        try:
            for link_id, profile in profiles.items():
                self.request(
                    make_matrix_profile_swap(link_id, profile, batch_id=batch_id)
                )
            return self.request({"type": "batch_commit", "id": batch_id})
        except Exception:
            try:
                self.request({"type": "batch_abort", "id": batch_id})
            except Exception:
                pass
            raise
