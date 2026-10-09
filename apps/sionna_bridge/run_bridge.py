#!/usr/bin/env python3
"""Drive an OCUDU channel graph and antenna matrix from Sionna RT.

The two gNBs remain fixed. UE0 and UE1 follow configurable bounded horizontal
trajectories. At a low control-plane cadence, Sionna RT
recomputes all bidirectional gNB/UE paths plus UE-to-UE crosstalk, and this
process atomically sends the resulting ten TDL profiles to the existing C++
broker. OCUDU is not
imported or modified; it only sees the impaired IQ emitted by the broker.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import math
import os
import pathlib
import signal
import struct
import sys
import tempfile
import time
import traceback
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    from channel_adapter import (  # type: ignore
        LaneProfile,
        MatrixProfile,
        Ray,
        Tap,
        ZmqControlClient,
        channel_status,
        control_link_id,
        make_matrix_profile_swap,
        rays_to_taps,
    )
else:
    from .channel_adapter import (
        LaneProfile,
        MatrixProfile,
        Ray,
        Tap,
        ZmqControlClient,
        channel_status,
        control_link_id,
        make_matrix_profile_swap,
        rays_to_taps,
    )


MODEL_ID = "sionna_rt"
NODE_IDS = ("gnb0", "gnb1", "ue0", "ue1")
DOWNLINK_LINKS = tuple(
    (source, destination)
    for source in ("gnb0", "gnb1")
    for destination in ("ue0", "ue1")
)
UPLINK_LINKS = tuple(
    (source, destination)
    for source in ("ue0", "ue1")
    for destination in ("gnb0", "gnb1")
)
# No UE<->UE edge by default: the fixtures are FDD cells, where a UE never
# hears another UE's uplink carrier. A scenario may still declare a
# `crosstalk` link explicitly for a shared-carrier (TDD) study.
CROSSTALK_LINKS: tuple[tuple[str, str], ...] = ()
LINKS = DOWNLINK_LINKS + UPLINK_LINKS + CROSSTALK_LINKS
ONE_GNB_ONE_UE_NODE_IDS = ("gnb0", "ue0")
ONE_GNB_ONE_UE_DOWNLINK_LINKS = (("gnb0", "ue0"),)
ONE_GNB_ONE_UE_UPLINK_LINKS = (("ue0", "gnb0"),)
ONE_GNB_ONE_UE_CROSSTALK_LINKS: tuple[tuple[str, str], ...] = ()
ONE_GNB_ONE_UE_LINKS = (
    ONE_GNB_ONE_UE_DOWNLINK_LINKS + ONE_GNB_ONE_UE_UPLINK_LINKS
)

# The tracked OCUDU example uses NR band 3 with DL NR-ARFCN 368500. Its
# downlink center is 1842.5 MHz and the paired uplink is 95 MHz lower. Keep
# these as bridge defaults rather than changing OCUDU's radio configuration.
DEFAULT_DOWNLINK_FREQUENCY_HZ = 1_842_500_000.0
DEFAULT_UPLINK_FREQUENCY_HZ = 1_747_500_000.0
DEFAULT_ROAD_WIDTH_M = 14.0
DEFAULT_ROUTE_X_M = (-82.0, 82.0)
DEFAULT_GNB_HEIGHT_M = 60.0
CAR_SPEED_MPS = 50.0 / 3.6
PEDESTRIAN_SPEED_MPS = 1.4
# Live position feed (external publisher -> bridge). The message shape is owned by the
# arena side and mirrored here; see ExternalPositionSource.
POSITION_MESSAGE_EVENT = "positions"
POSITION_MESSAGE_FRAME = "arena"
DEFAULT_POSITION_TIMEOUT_S = 1.0


@dataclass(frozen=True)
class Motion:
    """Where a node is at a given elapsed time, in Sionna scene metres.

    Three shapes, in order of generality. A constant `velocity` is a straight
    drift. Adding `x_bounds` makes it bounce along the x axis, which is what
    the built-in street-canyon layouts use. `waypoints` replaces both with a
    polyline walked at `speed_mps`, either turning back at the ends
    (``pingpong``) or closing the ring (``loop``) — the latter is what lets a
    pedestrian circle a building, which no axis-aligned route can express.
    """

    start: tuple[float, float, float]
    velocity: tuple[float, float, float]
    x_bounds: tuple[float, float] | None = None
    mobility: str = "static"
    waypoints: tuple[tuple[float, float, float], ...] = ()
    route_mode: str = "pingpong"
    speed_mps: float = 0.0

    def _route(self) -> tuple[tuple[tuple[float, float, float], ...], list[float], float]:
        """The walked polyline, its cumulative lengths and its total length."""

        points = list(self.waypoints)
        if self.route_mode == "loop" and points[0] != points[-1]:
            points.append(points[0])
        lengths = [0.0]
        for previous, current in zip(points, points[1:]):
            lengths.append(
                lengths[-1] + math.dist(previous, current)
            )
        return tuple(points), lengths, lengths[-1]

    def _travelled(self, elapsed_seconds: float, total: float) -> tuple[float, float]:
        """Distance along the polyline and the direction of travel (±1)."""

        if total <= 0.0 or self.speed_mps == 0.0:
            return 0.0, 1.0
        distance = self.speed_mps * elapsed_seconds
        if self.route_mode == "loop":
            return distance % total, 1.0
        phase = distance % (2.0 * total)
        return (phase, 1.0) if phase <= total else (2.0 * total - phase, -1.0)

    def position_at(self, elapsed_seconds: float) -> tuple[float, float, float]:
        if len(self.waypoints) >= 2:
            points, lengths, total = self._route()
            travelled, _ = self._travelled(elapsed_seconds, total)
            for index in range(len(points) - 1):
                if travelled <= lengths[index + 1] or index == len(points) - 2:
                    segment = lengths[index + 1] - lengths[index]
                    ratio = 0.0 if segment <= 0.0 else (travelled - lengths[index]) / segment
                    ratio = min(1.0, max(0.0, ratio))
                    return tuple(  # type: ignore[return-value]
                        a + (b - a) * ratio
                        for a, b in zip(points[index], points[index + 1])
                    )
            return points[-1]
        position = tuple(
            origin + elapsed_seconds * speed
            for origin, speed in zip(self.start, self.velocity)
        )
        if self.x_bounds is None or self.velocity[0] == 0.0:
            return position
        low, high = self.x_bounds
        span = high - low
        phase = ((self.start[0] - low) + elapsed_seconds * self.velocity[0]) % (
            2.0 * span
        )
        x = low + phase if phase <= span else high - (phase - span)
        return (x, position[1], position[2])

    def velocity_at(self, elapsed_seconds: float) -> tuple[float, float, float]:
        if len(self.waypoints) >= 2:
            points, lengths, total = self._route()
            travelled, direction = self._travelled(elapsed_seconds, total)
            for index in range(len(points) - 1):
                if travelled <= lengths[index + 1] or index == len(points) - 2:
                    segment = lengths[index + 1] - lengths[index]
                    if segment <= 0.0:
                        return (0.0, 0.0, 0.0)
                    return tuple(  # type: ignore[return-value]
                        (b - a) / segment * self.speed_mps * direction
                        for a, b in zip(points[index], points[index + 1])
                    )
            return (0.0, 0.0, 0.0)
        if self.x_bounds is None or self.velocity[0] == 0.0:
            return self.velocity
        low, high = self.x_bounds
        span = high - low
        phase = ((self.start[0] - low) + elapsed_seconds * self.velocity[0]) % (
            2.0 * span
        )
        direction = 1.0 if phase < span else -1.0
        return (self.velocity[0] * direction, self.velocity[1], self.velocity[2])


@dataclass(frozen=True)
class ArraySpec:
    rows: int = 1
    cols: int = 1
    pattern: str = "iso"
    polarization: str = "V"

    @property
    def antenna_count(self) -> int:
        return self.rows * self.cols

    def label(self) -> str:
        return (
            f"{self.rows}x{self.cols} {self.pattern}, "
            f"{self.polarization} polarization"
        )


@dataclass(frozen=True)
class ScenarioNode:
    motion: Motion
    tx_array: ArraySpec
    rx_array: ArraySpec


@dataclass(frozen=True)
class ScenarioLink:
    source: str
    destination: str
    direction: str
    model: str = MODEL_ID


@dataclass(frozen=True)
class ScenarioDefinition:
    name: str
    nodes: dict[str, ScenarioNode]
    links: tuple[ScenarioLink, ...]
    scene: str | None = None
    # None means "leave the --simple-road flag alone". A scene that ships its
    # own ground and roads has to be able to say so, because splitting a
    # floor it does not have would abort the run.
    simple_road: bool | None = None
    # Solver settings the scenario pins. Only the keys it names are applied,
    # so an example can record what it was traced with — which mechanisms
    # were on decides how many paths exist at all — without freezing the rest.
    solver: dict[str, Any] | None = None


def _array_spec(value: Any, *, where: str) -> ArraySpec:
    if value is None:
        return ArraySpec()
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be an object")
    rows = value.get("rows", 1)
    cols = value.get("cols", 1)
    if not isinstance(rows, int) or isinstance(rows, bool) or rows <= 0:
        raise ValueError(f"{where}.rows must be a positive integer")
    if not isinstance(cols, int) or isinstance(cols, bool) or cols <= 0:
        raise ValueError(f"{where}.cols must be a positive integer")
    pattern = value.get("pattern", "iso")
    polarization = value.get("polarization", "V")
    if not isinstance(pattern, str) or not pattern:
        raise ValueError(f"{where}.pattern must be a non-empty string")
    if not isinstance(polarization, str) or not polarization:
        raise ValueError(f"{where}.polarization must be a non-empty string")
    return ArraySpec(rows, cols, pattern, polarization)


def _finite_vector(value: Any, *, where: str) -> tuple[float, float, float]:
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError(f"{where} must be a three-number array")
    result = tuple(float(item) for item in value)
    if not all(math.isfinite(item) for item in result):
        raise ValueError(f"{where} values must be finite")
    return result  # type: ignore[return-value]


def load_scenario_config(path: pathlib.Path) -> ScenarioDefinition:
    """Load the dependency-free JSON contract shared by launcher and bridge."""

    try:
        root = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load scenario config {path}: {exc}") from exc
    if not isinstance(root, dict):
        raise ValueError("scenario config root must be an object")
    raw_nodes = root.get("nodes")
    raw_links = root.get("links")
    if not isinstance(raw_nodes, dict) or not raw_nodes:
        raise ValueError("scenario config requires a non-empty nodes object")
    if not isinstance(raw_links, list) or not raw_links:
        raise ValueError("scenario config requires a non-empty links array")

    nodes: dict[str, ScenarioNode] = {}
    for node_id, value in raw_nodes.items():
        if not isinstance(node_id, str) or not node_id or not isinstance(value, dict):
            raise ValueError("scenario node ids must map to objects")
        common_array = value.get("array")
        tx_array = _array_spec(
            value.get("tx_array", common_array), where=f"nodes.{node_id}.tx_array"
        )
        rx_array = _array_spec(
            value.get("rx_array", common_array), where=f"nodes.{node_id}.rx_array"
        )
        route = value.get("route_x_m")
        x_bounds: tuple[float, float] | None = None
        if route is not None:
            if not isinstance(route, list) or len(route) != 2:
                raise ValueError(f"nodes.{node_id}.route_x_m must be [min,max]")
            x_bounds = (float(route[0]), float(route[1]))
            if not all(math.isfinite(item) for item in x_bounds) or x_bounds[0] >= x_bounds[1]:
                raise ValueError(f"nodes.{node_id}.route_x_m must have finite min < max")
        raw_route = value.get("route_m")
        waypoints: tuple[tuple[float, float, float], ...] = ()
        route_mode = str(value.get("route_mode", "pingpong"))
        speed_mps = 0.0
        if raw_route is not None:
            if not isinstance(raw_route, list) or len(raw_route) < 2:
                raise ValueError(f"nodes.{node_id}.route_m needs at least two points")
            waypoints = tuple(
                _finite_vector(point, where=f"nodes.{node_id}.route_m[{index}]")
                for index, point in enumerate(raw_route)
            )
            if route_mode not in ("pingpong", "loop"):
                raise ValueError(
                    f"nodes.{node_id}.route_mode must be 'pingpong' or 'loop'"
                )
            speed = value.get("speed_mps")
            if not isinstance(speed, (int, float)) or isinstance(speed, bool):
                raise ValueError(f"nodes.{node_id}.speed_mps must accompany route_m")
            speed_mps = float(speed)
            if not math.isfinite(speed_mps) or speed_mps <= 0.0:
                raise ValueError(f"nodes.{node_id}.speed_mps must be positive")
            if x_bounds is not None:
                raise ValueError(
                    f"nodes.{node_id} cannot set both route_m and route_x_m"
                )
        # A routed node starts on its route, so start_m is optional there and
        # the first waypoint stands in for it.
        raw_start = value.get("start_m")
        if raw_start is None and waypoints:
            start = waypoints[0]
        else:
            start = _finite_vector(raw_start, where=f"nodes.{node_id}.start_m")
        motion = Motion(
            start,
            _finite_vector(
                value.get("velocity_mps", [0.0, 0.0, 0.0]),
                where=f"nodes.{node_id}.velocity_mps",
            ),
            x_bounds,
            str(value.get("mobility", "static")),
            waypoints,
            route_mode,
            speed_mps,
        )
        nodes[node_id] = ScenarioNode(motion, tx_array, rx_array)

    links: list[ScenarioLink] = []
    seen_links: set[tuple[str, str, str]] = set()
    for index, value in enumerate(raw_links):
        if not isinstance(value, dict):
            raise ValueError(f"links[{index}] must be an object")
        source = value.get("from")
        destination = value.get("to")
        direction = value.get("direction")
        model = value.get("model", MODEL_ID)
        if source not in nodes or destination not in nodes:
            raise ValueError(f"links[{index}] references an unknown node")
        if direction not in ("downlink", "uplink", "crosstalk"):
            raise ValueError(f"links[{index}].direction is invalid")
        if not isinstance(model, str) or not model:
            raise ValueError(f"links[{index}].model must be a non-empty string")
        key = (source, destination, model)
        if key in seen_links:
            raise ValueError(f"links[{index}] duplicates {source}>{destination}:{model}")
        seen_links.add(key)
        lane_count = (
            nodes[source].tx_array.antenna_count
            * nodes[destination].rx_array.antenna_count
        )
        if lane_count > 16:
            raise ValueError(
                f"links[{index}] has {lane_count} lanes; broker limit is 16"
            )
        links.append(ScenarioLink(source, destination, direction, model))

    name = root.get("name", path.stem)
    scene = root.get("scene")
    if not isinstance(name, str) or not name:
        raise ValueError("scenario name must be a non-empty string")
    if scene is not None and (not isinstance(scene, str) or not scene):
        raise ValueError("scenario scene must be a non-empty string")
    simple_road = root.get("simple_road")
    if simple_road is not None and not isinstance(simple_road, bool):
        raise ValueError("scenario simple_road must be true or false")
    solver = solver_settings(root.get("solver"))
    return ScenarioDefinition(name, nodes, tuple(links), scene, simple_road, solver)


# Scenario `solver` keys, mapped to the argparse destination each one sets.
SOLVER_INTEGER_KEYS = {
    "max_depth": "max_depth",
    "samples_per_source": "samples_per_src",
    "seed": "seed",
    "path_polylines": "path_polylines",
}
SOLVER_PROPAGATION_KEYS = (
    "los", "specular_reflection", "diffuse_reflection", "refraction",
    "diffraction",
)


def solver_settings(raw: Any) -> dict[str, Any] | None:
    """Validate a scenario's `solver` block into argparse destinations."""

    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("scenario solver must be an object")
    settings: dict[str, Any] = {}
    for key, destination in SOLVER_INTEGER_KEYS.items():
        if key not in raw:
            continue
        value = raw[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"solver.{key} must be a non-negative integer")
        if key != "path_polylines" and value <= 0:
            raise ValueError(f"solver.{key} must be positive")
        settings[destination] = value
    propagation = raw.get("propagation")
    if propagation is not None:
        if not isinstance(propagation, dict):
            raise ValueError("solver.propagation must be an object")
        for key, value in propagation.items():
            if key not in SOLVER_PROPAGATION_KEYS:
                allowed = ", ".join(SOLVER_PROPAGATION_KEYS)
                raise ValueError(
                    f"unknown solver.propagation key {key!r}; allowed: {allowed}"
                )
            if not isinstance(value, bool):
                raise ValueError(f"solver.propagation.{key} must be true or false")
            settings[key] = value
    unknown = set(raw) - set(SOLVER_INTEGER_KEYS) - {"propagation"}
    if unknown:
        raise ValueError(f"unknown solver keys: {', '.join(sorted(unknown))}")
    return settings


DEFAULT_MOTION = {
    "gnb0": Motion(
        (32.5, 10.5, DEFAULT_GNB_HEIGHT_M),
        (0.0, 0.0, 0.0),
        mobility="fixed",
    ),
    "gnb1": Motion(
        (32.5, 45.0, DEFAULT_GNB_HEIGHT_M),
        (0.0, 0.0, 0.0),
        mobility="fixed",
    ),
    "ue0": Motion(
        (-70.0, 0.0, 1.5),
        (CAR_SPEED_MPS, 0.0, 0.0),
        DEFAULT_ROUTE_X_M,
        "car",
    ),
    "ue1": Motion(
        (20.0, 8.0, 1.5),
        (-PEDESTRIAN_SPEED_MPS, 0.0, 0.0),
        DEFAULT_ROUTE_X_M,
        "pedestrian",
    ),
}


def link_layout(
    layout: str,
) -> tuple[
    tuple[str, ...],
    tuple[tuple[str, str], ...],
    tuple[tuple[str, str], ...],
    tuple[tuple[str, str], ...],
    tuple[tuple[str, str], ...],
]:
    if layout == "1x1":
        return (
            ONE_GNB_ONE_UE_NODE_IDS,
            ONE_GNB_ONE_UE_DOWNLINK_LINKS,
            ONE_GNB_ONE_UE_UPLINK_LINKS,
            ONE_GNB_ONE_UE_CROSSTALK_LINKS,
            ONE_GNB_ONE_UE_LINKS,
        )
    if layout == "2x2":
        return NODE_IDS, DOWNLINK_LINKS, UPLINK_LINKS, CROSSTALK_LINKS, LINKS
    raise ValueError(f"unsupported link layout: {layout}")


def configured_motion(args: argparse.Namespace) -> dict[str, Motion]:
    configured = getattr(args, "scenario_definition", None)
    if configured is not None:
        return {
            node_id: node.motion for node_id, node in configured.nodes.items()
        }
    motion = {
        "gnb0": Motion(
            (
                DEFAULT_MOTION["gnb0"].start[0],
                DEFAULT_MOTION["gnb0"].start[1],
                args.gnb_height_m,
            ),
            (0.0, 0.0, 0.0),
            mobility="fixed",
        ),
        "gnb1": Motion(
            (
                DEFAULT_MOTION["gnb1"].start[0],
                DEFAULT_MOTION["gnb1"].start[1],
                args.gnb_height_m,
            ),
            (0.0, 0.0, 0.0),
            mobility="fixed",
        ),
        "ue0": Motion(
            args.ue0_start, args.ue0_velocity, args.ue0_route_x, "car"
        ),
        "ue1": Motion(
            args.ue1_start, args.ue1_velocity, args.ue1_route_x, "pedestrian"
        ),
    }
    node_ids, *_ = link_layout(args.layout)
    return {node_id: motion[node_id] for node_id in node_ids}


def effective_scenario(args: argparse.Namespace) -> ScenarioDefinition:
    configured = getattr(args, "scenario_definition", None)
    if configured is not None:
        return configured
    node_ids, downlink, uplink, crosstalk, _ = link_layout(args.layout)
    motion = configured_motion(args)
    nodes = {
        node_id: ScenarioNode(motion[node_id], ArraySpec(), ArraySpec())
        for node_id in node_ids
    }
    links = tuple(
        [ScenarioLink(source, destination, "downlink") for source, destination in downlink]
        + [ScenarioLink(source, destination, "uplink") for source, destination in uplink]
        + [ScenarioLink(source, destination, "crosstalk") for source, destination in crosstalk]
    )
    return ScenarioDefinition(args.layout, nodes, links, args.scene)


def scenario_environment(args: argparse.Namespace) -> dict[str, Any]:
    """Describe the effective Sionna setup in a UI-friendly stable schema."""

    definition = effective_scenario(args)
    motion = {node_id: node.motion for node_id, node in definition.nodes.items()}
    link_groups = {
        direction: sum(link.direction == direction for link in definition.links)
        for direction in ("downlink", "uplink", "crosstalk")
    }
    tx_labels = {node.tx_array.label() for node in definition.nodes.values()}
    rx_labels = {node.rx_array.label() for node in definition.nodes.values()}
    environment = {
        "layout": definition.name,
        "scenario_config": (
            str(args.scenario_config) if args.scenario_config is not None else None
        ),
        "scene": definition.scene or args.scene,
        "simple_road": {
            "enabled": args.simple_road,
            "width_m": args.road_width_m if args.simple_road else None,
            "surface_material": "ITU concrete",
        },
        "solver": {
            "name": "PathSolver",
            "max_depth": args.max_depth,
            "samples_per_source": args.samples_per_src,
            "seed": args.seed,
            "synthetic_array": True,
            "propagation": {
                "los": args.los,
                "specular_reflection": args.specular_reflection,
                "diffuse_reflection": args.diffuse_reflection,
                "refraction": args.refraction,
                "diffraction": args.diffraction,
            },
        },
        "antenna": {
            "tx": next(iter(tx_labels)) if len(tx_labels) == 1 else "per-node configuration",
            "rx": next(iter(rx_labels)) if len(rx_labels) == 1 else "per-node configuration",
        },
        "sample_rate_hz": args.sample_rate_hz,
        "update_rate_hz": args.update_hz,
        "gain_offset_db": args.gain_offset_db,
        "frequencies_hz": {
            "downlink": args.downlink_frequency_hz,
            "uplink": args.uplink_frequency_hz,
        },
        "nodes": {
            node_id: {
                "start_m": item.start,
                "velocity_mps": item.velocity,
                "speed_mps": math.sqrt(sum(value * value for value in item.velocity)),
                "mobility": item.mobility,
                "route_x_m": item.x_bounds,
                "tx_array": definition.nodes[node_id].tx_array.label(),
                "rx_array": definition.nodes[node_id].rx_array.label(),
                "tx_antennas": definition.nodes[node_id].tx_array.antenna_count,
                "rx_antennas": definition.nodes[node_id].rx_array.antenna_count,
            }
            for node_id, item in motion.items()
        },
        "link_count": len(definition.links),
        "matrix_lane_count": sum(
            definition.nodes[link.source].tx_array.antenna_count
            * definition.nodes[link.destination].rx_array.antenna_count
            for link in definition.links
        ),
        "link_groups": {
            "downlink": link_groups["downlink"],
            "uplink": link_groups["uplink"],
            "ue_crosstalk": link_groups["crosstalk"],
        },
        "control_endpoint": None if args.dry_run else args.control_endpoint,
        "dry_run": args.dry_run,
        "position_endpoint": getattr(args, "position_endpoint", None),
        "position_frame_offset_m": list(getattr(args, "position_frame_offset", (0.0, 0.0, 0.0))),
        "position_timeout_s": getattr(args, "position_timeout_s", DEFAULT_POSITION_TIMEOUT_S),
    }
    # Only the opt-in benchmark features add keys, so every existing gate's
    # environment record stays as it was.
    timeline = getattr(args, "timeline", "wallclock")
    fanout = getattr(args, "fanout_control_endpoint", [])
    if timeline != "wallclock" or fanout:
        environment["timeline"] = timeline
        environment["hold_until_file"] = (
            str(args.hold_until_file) if getattr(args, "hold_until_file", None) else None
        )
        environment["fanout_control_endpoints"] = [
            {"endpoint": endpoint, "rename": rename} for endpoint, rename in fanout
        ]
    return environment


def parse_vector(text: str) -> tuple[float, float, float]:
    try:
        values = tuple(float(piece.strip()) for piece in text.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected x,y,z numeric vector") from exc
    if len(values) != 3 or not all(math.isfinite(value) for value in values):
        raise argparse.ArgumentTypeError("expected exactly three finite values: x,y,z")
    return values  # type: ignore[return-value]


def parse_range(text: str) -> tuple[float, float]:
    try:
        values = tuple(float(piece.strip()) for piece in text.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected min,max numeric range") from exc
    if (
        len(values) != 2
        or not all(math.isfinite(value) for value in values)
        or values[0] >= values[1]
    ):
        raise argparse.ArgumentTypeError("expected two finite values with min < max")
    return values  # type: ignore[return-value]


# --- Reproducible timeline and fan-out (scheduler benchmark, opt-in) ---------
#
# The default ("wallclock") loop places nodes where they are `time.monotonic()
# - start` seconds into the run, so two bridges, or two runs, sample the same
# route at different instants. The "grid" timeline places them at
# `grid_index / update_hz` instead: the channel of grid point k depends only on
# the scenario (and its solver seed), never on when the solve happened to run.
# Wall time only decides WHICH grid point is solved next, and lateness is
# bounded to one step (see GridTimeline.next_point).
TIMELINE_MODES = ("wallclock", "grid")


def parse_fanout(text: str) -> tuple[str, dict[str, str]]:
    """`ENDPOINT[=old:new,old:new]` -> (endpoint, node rename map).

    The rename map rewrites the node ids inside each link id, so one solved
    batch can drive a second broker whose topology names the same radios
    differently (cell b's gnb1/ue2/ue3 mirroring cell a's gnb0/ue0/ue1).
    """

    endpoint, separator, mapping = text.partition("=")
    endpoint = endpoint.strip()
    if not endpoint:
        raise argparse.ArgumentTypeError("fan-out endpoint must not be empty")
    rename: dict[str, str] = {}
    if separator:
        for item in mapping.split(","):
            old, colon, new = item.strip().partition(":")
            if not colon or not old or not new:
                raise argparse.ArgumentTypeError(
                    f"fan-out rename {item!r} must be old:new"
                )
            if old in rename:
                raise argparse.ArgumentTypeError(f"fan-out renames {old!r} twice")
            rename[old] = new
        if len(set(rename.values())) != len(rename):
            raise argparse.ArgumentTypeError("fan-out rename targets must be distinct")
    return endpoint, rename


def remap_link_id(link_id: str, rename: Mapping[str, str]) -> str:
    """Rename the nodes of a `source>destination:model` control link id."""

    nodes, colon, model = link_id.partition(":")
    source, arrow, destination = nodes.partition(">")
    if not colon or not arrow or not source or not destination:
        raise ValueError(f"not a control link id: {link_id!r}")
    return (
        f"{rename.get(source, source)}>{rename.get(destination, destination)}"
        f":{model}"
    )


def profiles_digest(profiles: Mapping[str, MatrixProfile]) -> str:
    """SHA-256 of a profile batch exactly as the control messages carry it.

    Link order is canonicalised, so the digest identifies the channel, not
    the dict order. Equal digests at equal grid indices are the evidence that
    two runs (or two cells) were driven by the same channel.
    """

    digest = hashlib.sha256()
    for link_id in sorted(profiles):
        message = make_matrix_profile_swap(link_id, profiles[link_id])
        digest.update(json.dumps(message, sort_keys=True, separators=(",", ":")).encode())
    return digest.hexdigest()


class GridTimeline:
    """Scenario clock on a fixed grid of `1 / update_hz` steps.

    `anchor()` fixes grid point 0 to a wall instant. `next_point()` then
    returns the next grid point to solve: the one after the last, or, when
    the loop has fallen a full step behind, the newest point already due --
    a slow solve skips grid points instead of replaying a backlog, so the
    scenario clock never trails wall time by more than one step.
    """

    def __init__(self, update_hz: float) -> None:
        if not (math.isfinite(update_hz) and update_hz > 0.0):
            raise ValueError("update_hz must be positive")
        self.update_hz = float(update_hz)
        self.anchor_monotonic: float | None = None
        self.anchor_unix_ms: int | None = None
        self.last_index: int | None = None
        self.skipped = 0

    @property
    def anchored(self) -> bool:
        return self.anchor_monotonic is not None

    def anchor(self, monotonic_now: float, unix_ms_now: int) -> None:
        self.anchor_monotonic = monotonic_now
        self.anchor_unix_ms = unix_ms_now
        self.last_index = None
        self.skipped = 0

    def due(self, index: int) -> float:
        if self.anchor_monotonic is None:
            raise RuntimeError("timeline is not anchored")
        return self.anchor_monotonic + index / self.update_hz

    def scenario_time(self, index: int) -> float:
        return index / self.update_hz

    def next_point(self, monotonic_now: float) -> int:
        if self.anchor_monotonic is None:
            raise RuntimeError("timeline is not anchored")
        following = 0 if self.last_index is None else self.last_index + 1
        current = math.floor((monotonic_now - self.anchor_monotonic) * self.update_hz)
        index = max(following, current)
        self.skipped += index - following
        self.last_index = index
        return index


def send_fanout(
    clients: Sequence[tuple[str, dict[str, str], ZmqControlClient]],
    profiles: Mapping[str, MatrixProfile],
    *,
    batch_id: str,
    executor: concurrent.futures.Executor,
) -> list[dict[str, Any]]:
    """Send one batch to every broker concurrently; one record per endpoint.

    Each client is used by exactly one worker per call (REQ sockets are not
    shared between threads). The endpoints are dispatched together rather
    than in a fixed order so no cell is systematically the first to switch.
    """

    def one(item: tuple[str, dict[str, str], ZmqControlClient]) -> dict[str, Any]:
        endpoint, rename, client = item
        renamed = {remap_link_id(link_id, rename): profile for link_id, profile in profiles.items()}
        started = time.monotonic()
        reply = client.send_matrix_profiles(renamed, batch_id=batch_id)
        return {
            "endpoint": endpoint,
            "rename": rename,
            "control_transaction_ms": (time.monotonic() - started) * 1000.0,
            "control_ack_unix_ms": time.time_ns() // 1_000_000,
            "reply": reply,
        }

    return list(executor.map(one, clients))


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-endpoint", default="tcp://127.0.0.1:5559")
    parser.add_argument(
        "--fanout-control-endpoint",
        action="append",
        type=parse_fanout,
        default=[],
        metavar="ENDPOINT[=old:new,...]",
        help=(
            "send every solved batch to this broker as well, renaming node ids "
            "in the link ids (repeatable). All endpoints, including "
            "--control-endpoint, receive the batch concurrently"
        ),
    )
    parser.add_argument(
        "--timeline",
        choices=TIMELINE_MODES,
        default="wallclock",
        help=(
            "wallclock: nodes move with elapsed wall time (default). grid: the "
            "scenario time of update k is k/update_hz, so a seed reproduces "
            "the same channel sequence"
        ),
    )
    parser.add_argument(
        "--hold-until-file",
        type=pathlib.Path,
        help=(
            "grid timeline only: solve and send scenario time 0 once, hold it "
            "until this file exists, then anchor grid point 0 to that instant"
        ),
    )
    parser.add_argument(
        "--profile-digest",
        action="store_true",
        help="record the SHA-256 of every sent profile batch in the status record",
    )
    parser.add_argument(
        "--scenario-config",
        type=pathlib.Path,
        help="JSON node/link/antenna configuration; overrides --layout motion and arrays",
    )
    parser.add_argument(
        "--layout",
        choices=("2x2", "1x1"),
        default="2x2",
        help="emulator graph driven by Sionna RT (default: 2x2)",
    )
    parser.add_argument(
        "--scene",
        default="sionna_simple_test",
        help=(
            "scene XML path, a directory name under "
            "use_cases/configs/sionna/scenes, or a built-in Sionna scene"
        ),
    )
    parser.add_argument("--duration", type=float, default=30.0,
                        help="wall-clock seconds; 0 runs until interrupted")
    parser.add_argument("--iterations", type=int, default=0,
                        help="optional update-count cap; 0 means no cap")
    parser.add_argument("--update-hz", type=float, default=500.0)
    parser.add_argument(
        "--profile-timing", action="store_true",
        help="record host wall-time stages of channel generation; no extra GPU synchronization",
    )
    parser.add_argument("--sample-rate-hz", type=float, default=23_040_000.0)
    parser.add_argument(
        "--downlink-frequency-hz",
        type=float,
        default=DEFAULT_DOWNLINK_FREQUENCY_HZ,
        help="Sionna carrier for gNB-to-UE links (default: band-3 DL)",
    )
    parser.add_argument(
        "--uplink-frequency-hz",
        type=float,
        default=DEFAULT_UPLINK_FREQUENCY_HZ,
        help="Sionna carrier for UE-to-gNB links (default: band-3 UL)",
    )
    parser.add_argument(
        "--carrier-frequency-hz",
        type=float,
        help="compatibility/TDD override: use one carrier for both directions",
    )
    parser.add_argument("--gain-offset-db", type=float, default=60.0,
                        help="fixed Sionna-field to normalized-IQ calibration")
    parser.add_argument("--max-depth", type=int, default=3)
    parser.add_argument("--samples-per-src", type=int, default=200_000)
    parser.add_argument("--seed", type=int, default=42)
    # The propagation mechanisms the solver is allowed to use. The defaults
    # are the ones every existing gate ran with; they are flags now so a
    # scenario can record what it was traced with instead of leaving it to
    # whoever types the command.
    for mechanism, enabled, help_text in (
        ("los", True, "direct line-of-sight paths"),
        ("specular-reflection", True, "mirror reflections off surfaces"),
        ("diffuse-reflection", False, "scattering off rough surfaces"),
        ("refraction", False, "transmission through surfaces"),
        ("diffraction", False, "paths bending around edges"),
    ):
        parser.add_argument(
            f"--{mechanism}",
            action=argparse.BooleanOptionalAction,
            default=enabled,
            help=help_text,
        )
    parser.add_argument(
        "--path-polylines",
        type=int,
        default=6,
        metavar="N",
        help=(
            "per link, publish the interaction points of the N strongest rays "
            "so the web UI can draw real multipath. UI-only: never part of a "
            "control message. 0 disables it, which also skips the component "
            "build Sionna does on first access to paths.vertices."
        ),
    )
    parser.add_argument(
        "--simple-road",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="split the built-in floor into a visible road and two ground strips",
    )
    parser.add_argument("--road-width-m", type=float, default=DEFAULT_ROAD_WIDTH_M)
    parser.add_argument(
        "--gnb-height-m",
        type=float,
        default=DEFAULT_GNB_HEIGHT_M,
        help="shared gNB antenna height; default clears the 51 m rooftops",
    )
    parser.add_argument("--ue0-start", type=parse_vector,
                        default=DEFAULT_MOTION["ue0"].start)
    parser.add_argument("--ue1-start", type=parse_vector,
                        default=DEFAULT_MOTION["ue1"].start)
    parser.add_argument("--ue0-velocity", type=parse_vector,
                        default=DEFAULT_MOTION["ue0"].velocity)
    parser.add_argument("--ue1-velocity", type=parse_vector,
                        default=DEFAULT_MOTION["ue1"].velocity)
    parser.add_argument("--ue0-route-x", type=parse_range,
                        default=DEFAULT_ROUTE_X_M,
                        help="car ping-pong route bounds in meters: min,max")
    parser.add_argument("--ue1-route-x", type=parse_range,
                        default=DEFAULT_ROUTE_X_M,
                        help="pedestrian ping-pong route bounds in meters: min,max")
    parser.add_argument("--status-jsonl", type=pathlib.Path,
                        help="append full per-update channel status as JSON Lines")
    # Live positions. An external publisher (a simulator, a motion log) publishes where the
    # nodes are; the scripted Motion routes stay the fallback for every node
    # the publisher does not mention and for the time before its first message.
    parser.add_argument(
        "--position-endpoint",
        help=(
            "ZMQ PUB endpoint to subscribe to for live node positions "
            f"({POSITION_MESSAGE_EVENT!r} messages in the arena frame); "
            "unset keeps the scripted routes"
        ),
    )
    parser.add_argument(
        "--position-frame-offset",
        type=parse_vector,
        default=(0.0, 0.0, 0.0),
        metavar="X,Y,Z",
        help="arena-frame origin expressed in scene metres, added to every live position",
    )
    parser.add_argument(
        "--position-timeout-s",
        type=float,
        default=DEFAULT_POSITION_TIMEOUT_S,
        help=(
            "after this long without a live message the last known positions "
            "are kept and the status record flags them stale; the update loop "
            "never waits for a message"
        ),
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="trace and print control JSON without connecting to ZMQ")
    args = parser.parse_args(argv)
    if args.hold_until_file is not None and args.timeline != "grid":
        parser.error("--hold-until-file needs --timeline grid")
    if args.timeline == "grid" and args.position_endpoint:
        # A live feed moves nodes by wall time, which is exactly what the
        # grid timeline exists to exclude.
        parser.error("--timeline grid cannot follow a live --position-endpoint")
    fanout_endpoints = [endpoint for endpoint, _ in args.fanout_control_endpoint]
    if len(set(fanout_endpoints + [args.control_endpoint])) != len(fanout_endpoints) + 1:
        parser.error("every control endpoint must be distinct")
    if args.position_timeout_s <= 0.0:
        parser.error("--position-timeout-s must be positive")
    if args.duration < 0.0:
        parser.error("--duration must be >= 0")
    if args.iterations < 0:
        parser.error("--iterations must be >= 0")
    if args.update_hz <= 0.0:
        parser.error("--update-hz must be positive")
    if args.carrier_frequency_hz is not None:
        args.downlink_frequency_hz = args.carrier_frequency_hz
        args.uplink_frequency_hz = args.carrier_frequency_hz
    if (
        args.sample_rate_hz <= 0.0
        or args.downlink_frequency_hz <= 0.0
        or args.uplink_frequency_hz <= 0.0
    ):
        parser.error("sample rate and DL/UL carrier frequencies must be positive")
    if args.max_depth < 0 or args.samples_per_src <= 0:
        parser.error("max depth must be non-negative and samples-per-src positive")
    if args.road_width_m <= 0.0:
        parser.error("--road-width-m must be positive")
    if not math.isfinite(args.gnb_height_m) or args.gnb_height_m <= 0.0:
        parser.error("--gnb-height-m must be finite and positive")
    if args.scenario_config is None:
        for node_id in ("ue0", "ue1"):
            start_x = getattr(args, f"{node_id}_start")[0]
            low, high = getattr(args, f"{node_id}_route_x")
            if not low <= start_x <= high:
                parser.error(f"--{node_id}-start x must be inside --{node_id}-route-x")
        args.scenario_definition = None
    else:
        try:
            args.scenario_definition = load_scenario_config(args.scenario_config)
        except ValueError as exc:
            parser.error(str(exc))
        if args.scenario_definition.scene is not None:
            args.scene = args.scenario_definition.scene
        if args.scenario_definition.simple_road is not None:
            args.simple_road = args.scenario_definition.simple_road
        for destination, value in (args.scenario_definition.solver or {}).items():
            setattr(args, destination, value)
    return args


_PLY_SCALAR_FORMATS = {
    "char": "b",
    "int8": "b",
    "uchar": "B",
    "uint8": "B",
    "short": "h",
    "int16": "h",
    "ushort": "H",
    "uint16": "H",
    "int": "i",
    "int32": "i",
    "uint": "I",
    "uint32": "I",
    "float": "f",
    "float32": "f",
    "double": "d",
    "float64": "d",
}


def read_ply(
    path: pathlib.Path, *, want_faces: bool = False
) -> tuple[list[tuple[float, float, float]], list[tuple[int, ...]]]:
    """Read vertex XYZ, and optionally the face index lists, from a PLY mesh.

    ``want_faces`` is opt-in because the bounds callers on the scene setup
    path only need the vertex block: stopping at ``end_header`` plus one
    vertex sweep is what they used to do, and reading the face element for
    them would parse geometry nobody looks at.
    """

    with path.open("rb") as handle:
        if handle.readline().strip() != b"ply":
            raise ValueError(f"not a PLY file: {path}")
        encoding = ""
        # (name, count, [(kind, scalar_or_(count_type, index_type))]) in file order.
        elements: list[tuple[str, int, list[tuple[str, Any]]]] = []
        while True:
            raw = handle.readline()
            if not raw:
                raise ValueError(f"truncated PLY header: {path}")
            line = raw.decode("ascii").strip()
            fields = line.split()
            if fields[:1] == ["format"]:
                encoding = fields[1]
            elif fields[:1] == ["element"] and len(fields) == 3:
                elements.append((fields[1], int(fields[2]), []))
            elif fields[:1] == ["property"] and elements:
                if fields[1] == "list":
                    if len(fields) != 5:
                        raise ValueError(f"unsupported PLY list property: {line}")
                    if fields[2] not in _PLY_SCALAR_FORMATS or fields[3] not in _PLY_SCALAR_FORMATS:
                        raise ValueError(f"unsupported PLY list property: {line}")
                    elements[-1][2].append(("list", (fields[2], fields[3])))
                else:
                    if len(fields) != 3 or fields[1] not in _PLY_SCALAR_FORMATS:
                        raise ValueError(f"unsupported PLY property: {line}")
                    elements[-1][2].append(("scalar", fields[1]))
            elif line == "end_header":
                break

        vertex_element = next((item for item in elements if item[0] == "vertex"), None)
        if vertex_element is None:
            raise ValueError(f"PLY has no XYZ vertices: {path}")
        if vertex_element[1] <= 0 or len(vertex_element[2]) < 3:
            raise ValueError(f"PLY has no XYZ vertices: {path}")
        if encoding == "ascii":
            reader: _PlyReader = _PlyAsciiReader(handle)
        elif encoding in ("binary_little_endian", "binary_big_endian"):
            reader = _PlyBinaryReader(
                handle, "<" if encoding == "binary_little_endian" else ">"
            )
        else:
            raise ValueError(f"unsupported PLY encoding {encoding!r}: {path}")

        vertices: list[tuple[float, float, float]] = []
        faces: list[tuple[int, ...]] = []
        for name, count, properties in elements:
            if name == "vertex":
                for _ in range(count):
                    values = reader.row(properties)
                    vertices.append(
                        (float(values[0]), float(values[1]), float(values[2]))
                    )
            elif name == "face" and want_faces:
                for _ in range(count):
                    values = reader.row(properties)
                    indices = values[0] if isinstance(values[0], (list, tuple)) else values
                    faces.append(tuple(int(index) for index in indices))
            elif not want_faces and vertices:
                # Nothing after the vertex block is needed, and a bounds
                # caller must not pay to walk it.
                break
            else:
                for _ in range(count):
                    reader.row(properties)

    return vertices, faces


class _PlyReader:
    def row(self, properties: list[tuple[str, Any]]) -> Any:
        raise NotImplementedError


class _PlyAsciiReader(_PlyReader):
    def __init__(self, handle: Any) -> None:
        self.handle = handle

    def row(self, properties: list[tuple[str, Any]]) -> Any:
        fields = self.handle.readline().split()
        if not fields:
            raise ValueError("truncated PLY body")
        values: list[Any] = []
        cursor = 0
        for kind, _spec in properties:
            if kind == "list":
                length = int(fields[cursor])
                cursor += 1
                values.append([int(item) for item in fields[cursor : cursor + length]])
                cursor += length
            else:
                values.append(float(fields[cursor]))
                cursor += 1
        return values


class _PlyBinaryReader(_PlyReader):
    def __init__(self, handle: Any, prefix: str) -> None:
        self.handle = handle
        self.prefix = prefix
        self._structs: dict[str, struct.Struct] = {}

    def _struct(self, code: str) -> struct.Struct:
        cached = self._structs.get(code)
        if cached is None:
            cached = struct.Struct(self.prefix + code)
            self._structs[code] = cached
        return cached

    def row(self, properties: list[tuple[str, Any]]) -> Any:
        values: list[Any] = []
        # A run of scalars is unpacked in one read; a list property has to
        # break the run because its length is only known from the file.
        run = ""
        def flush() -> None:
            nonlocal run
            if not run:
                return
            item = self._struct(run)
            values.extend(item.unpack(self.handle.read(item.size)))
            run = ""

        for kind, spec in properties:
            if kind == "list":
                flush()
                count_code, index_code = spec
                counter = self._struct(_PLY_SCALAR_FORMATS[count_code])
                length = counter.unpack(self.handle.read(counter.size))[0]
                item = self._struct(_PLY_SCALAR_FORMATS[index_code] * int(length))
                values.append(list(item.unpack(self.handle.read(item.size))))
            else:
                run += _PLY_SCALAR_FORMATS[spec]
        flush()
        return values


def ply_bounds(path: pathlib.Path) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """Read only the vertex XYZ bounds from an ASCII or binary PLY mesh."""

    vertices, _ = read_ply(path)
    minimum = tuple(min(vertex[index] for vertex in vertices) for index in range(3))
    maximum = tuple(max(vertex[index] for vertex in vertices) for index in range(3))
    return minimum, maximum  # type: ignore[return-value]


def write_rectangle_ply(
    path: pathlib.Path,
    *,
    x_min: float,
    x_max: float,
    y_min: float,
    y_max: float,
    z: float,
) -> None:
    """Write one upward-facing rectangular surface as a tiny ASCII PLY."""

    body = f"""ply
format ascii 1.0
element vertex 4
property float x
property float y
property float z
element face 2
property list uchar int vertex_indices
end_header
{x_min} {y_min} {z}
{x_max} {y_min} {z}
{x_max} {y_max} {z}
{x_min} {y_max} {z}
3 0 1 2
3 0 2 3
"""
    path.write_text(body, encoding="ascii")


def prepare_scene_with_simple_road(
    scene_xml: pathlib.Path,
    output_directory: pathlib.Path,
    road_width_m: float,
) -> pathlib.Path:
    """Replace the scene floor with non-overlapping road/ground surfaces."""

    tree = ET.parse(scene_xml)
    root = tree.getroot()
    source_directory = scene_xml.parent
    floor_shape: ET.Element | None = None
    floor_mesh: pathlib.Path | None = None
    floor_material = "concrete"

    for shape in root.findall("shape"):
        filename = shape.find("./string[@name='filename']")
        if filename is None or "value" not in filename.attrib:
            continue
        absolute = (source_directory / filename.attrib["value"]).resolve()
        filename.set("value", str(absolute))
        if shape.attrib.get("id") == "mesh-floor":
            floor_shape = shape
            floor_mesh = absolute
            material_ref = shape.find("./ref[@name='bsdf']")
            if material_ref is not None:
                floor_material = material_ref.attrib.get("id", floor_material)

    if floor_shape is None or floor_mesh is None:
        raise RuntimeError("--simple-road requires a scene shape named mesh-floor")
    minimum, maximum = ply_bounds(floor_mesh)
    center_y = 0.5 * (minimum[1] + maximum[1])
    road_y_min = center_y - 0.5 * road_width_m
    road_y_max = center_y + 0.5 * road_width_m
    if road_y_min <= minimum[1] or road_y_max >= maximum[1]:
        raise RuntimeError("road width must be smaller than the scene floor")

    root.remove(floor_shape)
    road_material_id = "road-surface"
    road_material = ET.Element(
        "bsdf", {"type": "itu-radio-material", "id": road_material_id}
    )
    ET.SubElement(road_material, "string", {"name": "type", "value": "concrete"})
    ET.SubElement(road_material, "float", {"name": "thickness", "value": "0.1"})
    first_shape_index = next(
        (index for index, child in enumerate(root) if child.tag == "shape"), len(root)
    )
    root.insert(first_shape_index, road_material)

    surfaces = (
        ("ground-south", minimum[1], road_y_min, floor_material),
        ("road", road_y_min, road_y_max, road_material_id),
        ("ground-north", road_y_max, maximum[1], floor_material),
    )
    for name, y_min, y_max, material in surfaces:
        mesh_path = output_directory / f"{name}.ply"
        write_rectangle_ply(
            mesh_path,
            x_min=minimum[0],
            x_max=maximum[0],
            y_min=y_min,
            y_max=y_max,
            z=maximum[2],
        )
        shape = ET.SubElement(root, "shape", {"type": "ply", "id": f"mesh-{name}"})
        ET.SubElement(
            shape, "string", {"name": "filename", "value": str(mesh_path.resolve())}
        )
        ET.SubElement(shape, "boolean", {"name": "face_normals", "value": "true"})
        ET.SubElement(shape, "ref", {"id": material, "name": "bsdf"})

    output_xml = output_directory / "simple_street_canyon_with_road.xml"
    tree.write(output_xml, encoding="utf-8", xml_declaration=True)
    return output_xml


# Shape ids carry their kind as the first token — `building_2`, `road_17`,
# `water_3`, `ground`. The web UI styles by kind, and the solver does not care,
# so this is the one place the naming convention is interpreted.
SURFACE_KINDS = (
    "building", "road", "water", "grass", "park", "pitch", "wood", "scrub",
    "sand",
)


def surface_kind(object_id: str) -> str:
    for kind in SURFACE_KINDS:
        if object_id == kind or object_id.startswith(f"{kind}_"):
            return kind
    return "ground"


def scene_geometry(scene_xml: pathlib.Path) -> dict[str, Any]:
    """Extract compact top-down footprints for the UI, never the GPU control path."""

    root = ET.parse(scene_xml).getroot()
    source_directory = scene_xml.parent
    materials: dict[str, str] = {}
    for material in root.findall("bsdf"):
        material_id = material.attrib.get("id")
        itu_type = material.find("./string[@name='type']")
        if material_id:
            materials[material_id] = (
                itu_type.attrib.get("value", material_id)
                if itu_type is not None
                else material_id
            )

    objects: list[dict[str, Any]] = []
    for shape in root.findall("shape"):
        filename = shape.find("./string[@name='filename']")
        if filename is None or not filename.attrib.get("value"):
            continue
        mesh_path = pathlib.Path(filename.attrib["value"])
        if not mesh_path.is_absolute():
            mesh_path = source_directory / mesh_path
        minimum, maximum = ply_bounds(mesh_path)
        object_id = shape.attrib.get("id", mesh_path.stem).removeprefix("mesh-")
        kind = surface_kind(object_id)
        material_ref = shape.find("./ref[@name='bsdf']")
        material_id = material_ref.attrib.get("id", "") if material_ref is not None else ""
        item: dict[str, Any] = {
            "id": object_id,
            "kind": kind,
            "material": materials.get(material_id, material_id),
            "footprint_xy_m": [
                [minimum[0], minimum[1]],
                [maximum[0], minimum[1]],
                [maximum[0], maximum[1]],
                [minimum[0], maximum[1]],
            ],
            "z_min_m": minimum[2],
            "z_max_m": maximum[2],
        }
        if object_id == "road":
            # Only the canyon's single east-west road has a centreline that a
            # bounding box can describe. An OpenStreetMap carriageway bends,
            # so a line across its box would be drawn somewhere it never runs.
            center_y = 0.5 * (minimum[1] + maximum[1])
            item["centerline_xy_m"] = [
                [minimum[0], center_y],
                [maximum[0], center_y],
            ]
        objects.append(item)
    return {
        "coordinate_system": "Sionna XYZ, meters",
        "projection": "top_down_xy",
        "objects": objects,
    }


SCENE_DIRECTORY = (
    (pathlib.Path.cwd() if __package__ else pathlib.Path(__file__).resolve().parents[2]) / "use_cases" / "configs" / "sionna" / "scenes"
)
# Names the demo uses for scenes that are really Sionna built-ins, so every
# scenario config can name its scene the same way whether the geometry ships
# with Sionna or with this repository.
SCENE_ALIASES = {"sionna_simple_test": "simple_street_canyon"}


def resolve_scene(name: str, rt: Any) -> pathlib.Path:
    """Turn a --scene value into a Mitsuba scene XML path.

    Accepts, in order: a path to an XML file, a scene generated into
    `use_cases/configs/sionna/scenes/<name>/scene.xml`, an alias for a built-in, and
    finally a Sionna built-in scene name.
    """

    candidate = pathlib.Path(name)
    if candidate.suffix == ".xml":
        if not candidate.is_file():
            raise RuntimeError(f"scene XML not found: {candidate}")
        return candidate.resolve()
    generated = SCENE_DIRECTORY / name / "scene.xml"
    if generated.is_file():
        return generated.resolve()
    builtin = SCENE_ALIASES.get(name, name)
    try:
        return pathlib.Path(getattr(rt.scene, builtin))
    except AttributeError as exc:
        known = ", ".join(sorted(SCENE_ALIASES)) or "none"
        raise RuntimeError(
            f"unknown scene {name!r}: not an XML path, not a directory under "
            f"{SCENE_DIRECTORY}, and not a built-in Sionna scene "
            f"(repository aliases: {known})"
        ) from exc


def scene_mesh(scene_xml: pathlib.Path) -> dict[str, Any]:
    """Extract the real triangle mesh of every scene shape, for the 3D UI.

    This is deliberately *not* part of the per-iteration status record: the
    geometry is fixed for the life of the process while that record is
    written twice a second, so the mesh is emitted once to a sidecar file
    and served from there. Like `scene_geometry` it never touches the GPU
    control path.
    """

    root = ET.parse(scene_xml).getroot()
    source_directory = scene_xml.parent
    materials: dict[str, str] = {}
    for material in root.findall("bsdf"):
        material_id = material.attrib.get("id")
        itu_type = material.find("./string[@name='type']")
        if material_id:
            materials[material_id] = (
                itu_type.attrib.get("value", material_id)
                if itu_type is not None
                else material_id
            )

    objects: list[dict[str, Any]] = []
    for shape in root.findall("shape"):
        filename = shape.find("./string[@name='filename']")
        if filename is None or not filename.attrib.get("value"):
            continue
        mesh_path = pathlib.Path(filename.attrib["value"])
        if not mesh_path.is_absolute():
            mesh_path = source_directory / mesh_path
        vertices, faces = read_ply(mesh_path, want_faces=True)
        object_id = shape.attrib.get("id", mesh_path.stem).removeprefix("mesh-")
        kind = surface_kind(object_id)
        material_ref = shape.find("./ref[@name='bsdf']")
        material_id = material_ref.attrib.get("id", "") if material_ref is not None else ""

        positions: list[float] = []
        for vertex in vertices:
            # Millimetre resolution is far finer than anything the viewer
            # can show and keeps the JSON a fraction of its full-float size.
            positions.extend(round(value, 3) for value in vertex)
        indices: list[int] = []
        for face in faces:
            # PLY allows n-gons; a triangle fan is correct for the convex
            # faces Sionna ships and for the rectangles written here.
            for corner in range(1, len(face) - 1):
                indices.extend((face[0], face[corner], face[corner + 1]))
        if not positions or not indices:
            continue
        objects.append(
            {
                "id": object_id,
                "kind": kind,
                "material": materials.get(material_id, material_id),
                "positions": positions,
                "indices": indices,
            }
        )
    mesh: dict[str, Any] = {
        "coordinate_system": "Sionna XYZ, meters",
        "objects": objects,
    }
    # A generated scene records where its geometry came from. OpenStreetMap is
    # ODbL 1.0, and the credit has to reach whoever sees the rendering, so it
    # travels with the mesh to the web UI instead of sitting in the repository.
    manifest_path = scene_xml.with_name("manifest.json")
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest = {}
        if manifest.get("attribution"):
            mesh["source"] = {
                "name": manifest.get("name"),
                "attribution": manifest["attribution"],
                "license": manifest.get("license"),
                "origin_lat_lon": manifest.get("origin_lat_lon"),
            }
    return mesh


def scene_mesh_path(status_jsonl: pathlib.Path | None) -> pathlib.Path | None:
    """Sidecar path the bridge writes and the web UI reads."""

    if status_jsonl is None:
        return None
    return status_jsonl.with_name(f"{status_jsonl.stem}-scene-mesh.json")


def write_scene_mesh(path: pathlib.Path | None, mesh: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    # Replace atomically: the web UI may be polling this file already.
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(mesh, separators=(",", ":")), encoding="utf-8")
    temporary.replace(path)


def import_sionna() -> tuple[Any, Any]:
    try:
        import numpy as np  # type: ignore
        import sionna.rt as rt  # type: ignore
    except ImportError as exc:  # pragma: no cover - live dependency path
        if "sionna" not in str(exc).lower():
            raise RuntimeError(
                f"Sionna RT import failed because a native runtime dependency "
                f"is unavailable: {exc}"
            ) from exc
        raise RuntimeError(
            "Sionna RT is not installed. Run: "
            "python3 -m pip install -r apps/sionna_bridge/requirements.txt"
        ) from exc
    return rt, np


def numpy_value(value: Any, np: Any) -> Any:
    if hasattr(value, "numpy"):
        value = value.numpy()
    return np.asarray(value)


def antenna_slice(
    array: Any,
    rx_index: int,
    tx_index: int,
    rx_port: int,
    tx_port: int,
    *,
    has_time: bool,
) -> Any:
    """Index synthetic-array tensors, with center-device layout fallback."""

    if has_time:
        if array.ndim == 6:
            return array[rx_index, rx_port, tx_index, tx_port, :, 0]
        if array.ndim == 4:
            if rx_port != 0 or tx_port != 0:
                raise RuntimeError("Sionna CIR omitted antenna axes for a multi-antenna link")
            return array[rx_index, tx_index, :, 0]
    else:
        if array.ndim == 5:
            return array[rx_index, rx_port, tx_index, tx_port, :]
        if array.ndim == 3:
            return array[rx_index, tx_index, :]
    raise RuntimeError(f"unexpected Sionna tensor rank {array.ndim}")


# `interactions` uses sionna.rt.constants.InteractionType; 0 means "no
# interaction at this depth", which is how a path shorter than max_depth is
# padded. The constant is inlined so this module keeps importing without
# Sionna present, which the unit tests rely on.
_INTERACTION_NONE = 0
_INTERACTION_LABELS = {
    1: "specular",
    2: "diffuse",
    4: "refraction",
    8: "diffraction",
}


def path_slice(array: Any, rx_index: int, tx_index: int, *, trailing: int) -> Any:
    """Index a ``[max_depth, num_rx, (ant,) num_tx, (ant,) num_paths, ...]`` tensor.

    ``trailing`` is the number of axes after ``num_paths`` — 1 for
    ``paths.vertices`` (the XYZ axis) and 0 for ``paths.interactions``.
    The synthetic-array layout drops both antenna axes, exactly as
    `antenna_slice` handles for the CIR tensors.
    """

    if array.ndim == 6 + trailing:
        return array[:, rx_index, 0, tx_index, 0]
    if array.ndim == 4 + trailing:
        return array[:, rx_index, tx_index]
    raise RuntimeError(f"unexpected Sionna path tensor rank {array.ndim}")


def path_polylines(
    vertices: Any,
    interactions: Any,
    *,
    source_position: Sequence[float],
    destination_position: Sequence[float],
    gains_db: Sequence[float | None],
    limit: int,
) -> list[dict[str, Any]]:
    """Turn per-path interaction points into drawable transmitter→receiver lines.

    ``vertices`` and ``interactions`` are already sliced down to one link:
    ``[max_depth, num_paths, 3]`` and ``[max_depth, num_paths]``. Only the
    ``limit`` strongest paths survive, because this rides the 2 Hz status
    record that the whole log volume is made of.
    """

    max_depth = int(vertices.shape[0]) if vertices.shape else 0
    path_count = int(vertices.shape[1]) if len(vertices.shape) > 1 else 0
    ranked = sorted(
        (
            (gain, index)
            for index, gain in enumerate(gains_db[:path_count])
            if gain is not None
        ),
        key=lambda item: item[0],
        reverse=True,
    )[: max(0, limit)]

    lines: list[dict[str, Any]] = []
    for gain, index in ranked:
        points: list[list[float]] = [[round(float(v), 3) for v in source_position]]
        kinds: list[str] = []
        for depth in range(max_depth):
            kind = int(interactions[depth, index])
            if kind == _INTERACTION_NONE:
                break
            points.append(
                [round(float(value), 3) for value in vertices[depth, index]]
            )
            kinds.append(_INTERACTION_LABELS.get(kind, str(kind)))
        points.append([round(float(v), 3) for v in destination_position])
        lines.append(
            {
                "gain_db": round(gain, 2),
                "bounces": len(kinds),
                "interactions": kinds,
                "points": points,
            }
        )
    return lines


def siso_slice(array: Any, rx_index: int, tx_index: int, *, has_time: bool) -> Any:
    """Backward-compatible scalar wrapper used by existing callers/tests."""

    return antenna_slice(array, rx_index, tx_index, 0, 0, has_time=has_time)


Vector3 = tuple[float, float, float]


@dataclass(frozen=True)
class PositionSample:
    """One accepted live message: per node the arena-frame position/velocity."""

    received_monotonic: float
    t_unix_ms: int | None
    nodes: dict[str, tuple[Vector3, Vector3 | None]]


class ZmqSubscriberTransport:
    """Non-blocking SUB socket; `recv_noblock` returns one frame or None."""

    def __init__(self, endpoint: str) -> None:
        try:
            import zmq  # type: ignore
        except ImportError as exc:  # pragma: no cover - live dependency path
            raise RuntimeError(
                "pyzmq is required for --position-endpoint; install requirements.txt"
            ) from exc
        self._zmq = zmq
        self._socket = zmq.Context.instance().socket(zmq.SUB)
        self._socket.setsockopt(zmq.LINGER, 0)
        self._socket.setsockopt(zmq.RCVHWM, 16)
        self._socket.setsockopt_string(zmq.SUBSCRIBE, "")
        self._socket.connect(endpoint)

    def recv_noblock(self) -> bytes | None:
        try:
            return self._socket.recv(self._zmq.NOBLOCK)
        except self._zmq.Again:
            return None

    def close(self) -> None:
        self._socket.close(linger=0)


class ExternalPositionSource:
    """Latest-wins live positions for the scenario nodes.

    The publisher sends JSON frames::

        {"event": "positions", "t_unix_ms": 1790000000000, "frame": "arena",
         "nodes": {"ue0": {"position_m": [x, y, z], "velocity_mps": [vx, vy, vz]}}}

    Every call to `poll` drains the socket and keeps only the newest valid
    frame, so a slow trace never replays a backlog of stale positions. Nodes
    the frame does not mention keep whatever they had (scripted route or the
    previous live sample); node ids the scenario does not know are ignored
    and reported once. After `timeout_s` without a frame the last sample is
    still used and flagged stale — the update loop never blocks on this feed.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        offset: Vector3 = (0.0, 0.0, 0.0),
        timeout_s: float = DEFAULT_POSITION_TIMEOUT_S,
        known_nodes: Sequence[str] = (),
        transport: Any | None = None,
        clock: Any = time.monotonic,
        warn: Any = None,
    ) -> None:
        self.endpoint = endpoint
        self.offset = tuple(float(value) for value in offset)  # type: ignore[assignment]
        self.timeout_s = float(timeout_s)
        self.known_nodes = tuple(known_nodes)
        self._transport = transport if transport is not None else ZmqSubscriberTransport(endpoint)
        self._clock = clock
        self._warn = warn if warn is not None else (
            lambda text: print(text, file=sys.stderr, flush=True)
        )
        self.messages_received = 0
        self.messages_invalid = 0
        # Valid frames superseded by a newer one inside the same poll.
        self.messages_dropped = 0
        self.ignored_nodes: list[str] = []
        self.last_sample: PositionSample | None = None
        self.last_error: str | None = None

    def close(self) -> None:
        close = getattr(self._transport, "close", None)
        if close is not None:
            close()

    def _parse(self, frame: bytes, now: float) -> PositionSample | None:
        try:
            message = json.loads(frame)
        except (ValueError, UnicodeDecodeError) as exc:
            self.last_error = f"not JSON: {exc}"
            return None
        if not isinstance(message, dict) or message.get("event") != POSITION_MESSAGE_EVENT:
            self.last_error = "not a positions event"
            return None
        frame_name = message.get("frame", POSITION_MESSAGE_FRAME)
        if frame_name != POSITION_MESSAGE_FRAME:
            self.last_error = f"unexpected frame {frame_name!r}"
            return None
        raw_nodes = message.get("nodes")
        if not isinstance(raw_nodes, dict):
            self.last_error = "nodes must be an object"
            return None
        nodes: dict[str, tuple[Vector3, Vector3 | None]] = {}
        for node_id, item in raw_nodes.items():
            if node_id not in self.known_nodes:
                if node_id not in self.ignored_nodes:
                    self.ignored_nodes.append(node_id)
                    self._warn(
                        f"position feed: ignoring unknown node {node_id!r} "
                        f"(scenario nodes: {', '.join(self.known_nodes)})"
                    )
                continue
            if not isinstance(item, dict):
                self.last_error = f"node {node_id!r} must be an object"
                return None
            try:
                position = _finite_vector(item.get("position_m"), where=f"nodes.{node_id}.position_m")
                velocity_raw = item.get("velocity_mps")
                velocity = (
                    None
                    if velocity_raw is None
                    else _finite_vector(velocity_raw, where=f"nodes.{node_id}.velocity_mps")
                )
            except (ValueError, TypeError) as exc:
                self.last_error = str(exc)
                return None
            nodes[node_id] = (position, velocity)
        t_unix_ms = message.get("t_unix_ms")
        if t_unix_ms is not None and not isinstance(t_unix_ms, (int, float)):
            t_unix_ms = None
        return PositionSample(
            received_monotonic=now,
            t_unix_ms=None if t_unix_ms is None else int(t_unix_ms),
            nodes=nodes,
        )

    def poll(self) -> PositionSample | None:
        """Drain the feed; return the newest valid frame received now, if any."""

        now = self._clock()
        newest: PositionSample | None = None
        while True:
            frame = self._transport.recv_noblock()
            if frame is None:
                break
            self.messages_received += 1
            sample = self._parse(frame, now)
            if sample is None:
                self.messages_invalid += 1
                continue
            if newest is not None:
                self.messages_dropped += 1
            newest = sample
        if newest is not None:
            self.last_sample = newest
        return newest

    def age_ms(self) -> float | None:
        if self.last_sample is None:
            return None
        return (self._clock() - self.last_sample.received_monotonic) * 1000.0

    def stale(self) -> bool:
        age = self.age_ms()
        return age is not None and age > self.timeout_s * 1000.0

    def resolve(
        self,
        motion: Mapping[str, Motion],
        elapsed_seconds: float,
    ) -> tuple[dict[str, Vector3], dict[str, Vector3], dict[str, str]]:
        """Positions/velocities for every node plus where each one came from.

        Live nodes get the last sample (offset applied, stale or not); the
        rest follow their scripted route exactly as without a feed.
        """

        positions: dict[str, Vector3] = {}
        velocities: dict[str, Vector3] = {}
        sources: dict[str, str] = {}
        live = self.last_sample.nodes if self.last_sample is not None else {}
        for node_id, item in motion.items():
            if node_id in live:
                position, velocity = live[node_id]
                positions[node_id] = tuple(  # type: ignore[assignment]
                    value + shift for value, shift in zip(position, self.offset)
                )
                velocities[node_id] = velocity if velocity is not None else (0.0, 0.0, 0.0)
                sources[node_id] = "external"
            else:
                positions[node_id] = item.position_at(elapsed_seconds)
                velocities[node_id] = item.velocity_at(elapsed_seconds)
                sources[node_id] = "scripted"
        return positions, velocities, sources

    def status(self, sources: Mapping[str, str]) -> dict[str, Any]:
        sample = self.last_sample
        return {
            "endpoint": self.endpoint,
            "frame": POSITION_MESSAGE_FRAME,
            "frame_offset_m": list(self.offset),
            "timeout_s": self.timeout_s,
            "messages_received": self.messages_received,
            "messages_invalid": self.messages_invalid,
            "messages_dropped": self.messages_dropped,
            "last_sample_t_unix_ms": None if sample is None else sample.t_unix_ms,
            # Since the frame was taken off the socket (this poll), and since
            # the publisher stamped it — the latter is the feed latency the
            # arena sees, and it depends on both clocks being wall-clock.
            "last_sample_age_ms": self.age_ms(),
            "last_sample_publish_age_ms": (
                None
                if sample is None or sample.t_unix_ms is None
                else time.time_ns() // 1_000_000 - sample.t_unix_ms
            ),
            "stale": self.stale(),
            "pending": sample is None,
            "ignored_nodes": list(self.ignored_nodes),
            "last_error": self.last_error,
            "node_sources": dict(sources),
        }


class SionnaScenario:
    # `paths.vertices` builds a component tensor on first access. If that ever
    # fails the run must keep going without the UI extra, so the failure is
    # reported once and the feature switches itself off. The class-level
    # default lets callers that construct a partial scenario reach `profiles`.
    path_polylines_error: str | None = None

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.rt, self.np = import_sionna()
        self._scene_workspace: tempfile.TemporaryDirectory[str] | None = None
        scene_xml = resolve_scene(args.scene, self.rt)
        if args.simple_road:
            self._scene_workspace = tempfile.TemporaryDirectory(
                prefix="ocudu-sionna-road-"
            )
            scene_xml = prepare_scene_with_simple_road(
                scene_xml,
                pathlib.Path(self._scene_workspace.name),
                args.road_width_m,
            )
        self.scene_geometry = scene_geometry(scene_xml)
        self.scene_mesh = scene_mesh(scene_xml)
        self.scene = self.rt.load_scene(str(scene_xml), merge_shapes=True)
        self.scene.frequency = args.downlink_frequency_hz
        self.scene.bandwidth = args.sample_rate_hz
        self.definition = effective_scenario(args)
        self.motion = {
            node_id: node.motion for node_id, node in self.definition.nodes.items()
        }
        self.node_ids = tuple(self.definition.nodes)
        self.downlink_links = tuple(
            link for link in self.definition.links if link.direction == "downlink"
        )
        self.uplink_links = tuple(
            link for link in self.definition.links if link.direction == "uplink"
        )
        self.crosstalk_links = tuple(
            link for link in self.definition.links if link.direction == "crosstalk"
        )
        self.links = self.definition.links
        first_link = self.links[0]
        self.configure_arrays(
            self.definition.nodes[first_link.source].tx_array,
            self.definition.nodes[first_link.destination].rx_array,
        )
        self.current_velocities = {
            node_id: motion.velocity_at(0.0)
            for node_id, motion in self.motion.items()
        }
        self.current_positions = {
            node_id: motion.start for node_id, motion in self.motion.items()
        }
        # Set by main() when --position-endpoint is given; None keeps the
        # scripted routes and leaves update_positions exactly as it was.
        self.position_source: ExternalPositionSource | None = None
        self.position_sources: dict[str, str] = {
            node_id: "scripted" for node_id in self.motion
        }
        self.position_status: dict[str, Any] | None = None

        # Each emulator node appears once as a Sionna transmitter and once as
        # a receiver. FDD runs one solve per direction so frequency-dependent
        # materials and path coefficients match the configured UL/DL carrier.
        for node_id in self.node_ids:
            position = self.motion[node_id].start
            tx = self.rt.Transmitter(name=f"{node_id}_tx", position=position)
            rx = self.rt.Receiver(name=f"{node_id}_rx", position=position)
            tx.velocity = self.motion[node_id].velocity_at(0.0)
            rx.velocity = self.motion[node_id].velocity_at(0.0)
            self.scene.add(tx)
            self.scene.add(rx)

        self.tx_indices = {
            name.removesuffix("_tx"): index
            for index, name in enumerate(self.scene.transmitters.keys())
        }
        self.rx_indices = {
            name.removesuffix("_rx"): index
            for index, name in enumerate(self.scene.receivers.keys())
        }
        self.solver = self.rt.PathSolver()

    def configure_arrays(self, tx: ArraySpec, rx: ArraySpec) -> None:
        self.scene.tx_array = self.rt.PlanarArray(
            num_rows=tx.rows,
            num_cols=tx.cols,
            pattern=tx.pattern,
            polarization=tx.polarization,
        )
        self.scene.rx_array = self.rt.PlanarArray(
            num_rows=rx.rows,
            num_cols=rx.cols,
            pattern=rx.pattern,
            polarization=rx.polarization,
        )

    def update_positions(self, elapsed_seconds: float) -> dict[str, tuple[float, float, float]]:
        positions: dict[str, tuple[float, float, float]] = {}
        if self.position_source is not None:
            self.position_source.poll()
            resolved, velocities, self.position_sources = self.position_source.resolve(
                self.motion, elapsed_seconds
            )
            self.position_status = self.position_source.status(self.position_sources)
        for node_id, motion in self.motion.items():
            if self.position_source is not None:
                position = resolved[node_id]
                velocity = velocities[node_id]
            else:
                position = motion.position_at(elapsed_seconds)
                velocity = motion.velocity_at(elapsed_seconds)
            positions[node_id] = position
            self.current_positions[node_id] = position
            self.current_velocities[node_id] = velocity
            self.scene.get(f"{node_id}_tx").position = position
            self.scene.get(f"{node_id}_rx").position = position
            self.scene.get(f"{node_id}_tx").velocity = velocity
            self.scene.get(f"{node_id}_rx").velocity = velocity
        return positions

    def trace(self, carrier_frequency_hz: float) -> Any:
        self.scene.frequency = carrier_frequency_hz
        return self.solver(
            scene=self.scene,
            max_depth=self.args.max_depth,
            samples_per_src=self.args.samples_per_src,
            synthetic_array=True,
            los=self.args.los,
            specular_reflection=self.args.specular_reflection,
            diffuse_reflection=self.args.diffuse_reflection,
            refraction=self.args.refraction,
            diffraction=self.args.diffraction,
            seed=self.args.seed,
        )

    def profiles(
        self,
        paths: Any,
        links: Sequence[ScenarioLink],
        *,
        direction: str,
        carrier_frequency_hz: float,
        timing: dict[str, float] | None = None,
    ) -> tuple[dict[str, MatrixProfile], list[dict[str, Any]]]:
        started = time.monotonic()
        coefficients, delays = paths.cir(
            sampling_frequency=self.args.update_hz,
            num_time_steps=1,
            normalize_delays=False,
            out_type="numpy",
        )
        if timing is not None:
            timing["cir_numpy"] = (time.monotonic() - started) * 1000.0
        started = time.monotonic()
        coefficients = numpy_value(coefficients, self.np)
        delays = numpy_value(delays, self.np)
        dopplers = numpy_value(paths.doppler, self.np)
        valid = numpy_value(paths.valid, self.np)
        if timing is not None:
            timing["array_export"] = (time.monotonic() - started) * 1000.0
        started = time.monotonic()

        # UI-only ray geometry. `paths.vertices` triggers a component build
        # inside Sionna, so it is read once per solve rather than per link,
        # and never at all when the operator switched the feature off.
        ray_vertices = ray_interactions = None
        if (
            self.args.path_polylines > 0
            and self.path_polylines_error is None
        ):
            try:
                # Property access can build GPU components and fail; keep it
                # inside the optional-visualization error boundary.
                vertices = getattr(paths, "vertices", None)
                if vertices is not None:
                    ray_vertices = numpy_value(vertices, self.np)
                    ray_interactions = numpy_value(paths.interactions, self.np)
            except Exception as exc:
                self.path_polylines_error = f"{type(exc).__name__}: {exc}"
                ray_vertices = ray_interactions = None
                print(
                    "sionna path polylines disabled: "
                    f"{self.path_polylines_error}",
                    file=sys.stderr,
                    flush=True,
                )

        if timing is not None:
            timing["geometry_export"] = (time.monotonic() - started) * 1000.0
        packing_started = time.monotonic()
        visualization_ms = 0.0
        profiles: dict[str, MatrixProfile] = {}
        statuses: list[dict[str, Any]] = []
        for link in links:
            source = link.source
            destination = link.destination
            tx_index = self.tx_indices[source]
            rx_index = self.rx_indices[destination]
            tx_count = self.definition.nodes[source].tx_array.antenna_count
            rx_count = self.definition.nodes[destination].rx_array.antenna_count
            lane_profiles: list[LaneProfile] = []
            lane_statuses: list[dict[str, Any]] = []
            all_rays: list[Ray] = []
            for rx_port in range(rx_count):
                for tx_port in range(tx_count):
                    coeff_slice = antenna_slice(
                        coefficients, rx_index, tx_index, rx_port, tx_port,
                        has_time=True,
                    )
                    delay_slice = antenna_slice(
                        delays, rx_index, tx_index, rx_port, tx_port,
                        has_time=False,
                    )
                    valid_slice = antenna_slice(
                        valid, rx_index, tx_index, rx_port, tx_port,
                        has_time=False,
                    )
                    doppler_slice = antenna_slice(
                        dopplers, rx_index, tx_index, rx_port, tx_port,
                        has_time=False,
                    )
                    rays = [
                        Ray(
                            float(delay),
                            complex(coefficient),
                            float(doppler),
                        )
                        for coefficient, delay, doppler, is_valid in zip(
                            coeff_slice, delay_slice, doppler_slice, valid_slice
                        )
                        if bool(is_valid)
                    ]
                    taps = rays_to_taps(
                        rays,
                        sample_rate_hz=self.args.sample_rate_hz,
                        gain_offset_db=self.args.gain_offset_db,
                    )
                    lane_profiles.append(
                        LaneProfile(rx_port, tx_port, tuple(taps))
                    )
                    lane_status = channel_status(
                        "",
                        rays,
                        taps,
                        sample_rate_hz=self.args.sample_rate_hz,
                    )
                    lane_status.update({"rx_port": rx_port, "tx_port": tx_port})
                    lane_statuses.append(lane_status)
                    all_rays.extend(rays)

            link_id = control_link_id(source, destination, link.model)
            matrix_profile = MatrixProfile(
                nt=tx_count, nr=rx_count, lanes=tuple(lane_profiles)
            )
            profiles[link_id] = matrix_profile
            # Keep the established top-level scalar fields for charts and
            # tables, using lane (0,0), and add the full matrix beside them.
            status = dict(lane_statuses[0])
            status["link_id"] = link_id
            total_power = sum(abs(ray.coefficient) ** 2 for ray in all_rays)
            status["total_path_power_db"] = (
                10.0 * math.log10(total_power) if total_power > 0.0 else None
            )
            status["matrix"] = {"nt": tx_count, "nr": rx_count}
            status["lanes"] = lane_statuses
            status["source"] = source
            status["destination"] = destination
            status["model"] = link.model
            status["direction"] = link.direction
            status["carrier_frequency_hz"] = carrier_frequency_hz
            if ray_vertices is not None and ray_interactions is not None:
                visualization_started = time.monotonic()
                # Lane (0, 0) drives the drawing: with a synthetic array the
                # ray geometry is shared by every antenna pair anyway.
                first_lane_coefficients = antenna_slice(
                    coefficients, rx_index, tx_index, 0, 0, has_time=True
                )
                first_lane_valid = antenna_slice(
                    valid, rx_index, tx_index, 0, 0, has_time=False
                )
                gains_db = [
                    (
                        20.0 * math.log10(abs(complex(coefficient)))
                        if bool(is_valid) and abs(complex(coefficient)) > 0.0
                        else None
                    )
                    for coefficient, is_valid in zip(
                        first_lane_coefficients, first_lane_valid
                    )
                ]
                status["path_polylines"] = path_polylines(
                    path_slice(ray_vertices, rx_index, tx_index, trailing=1),
                    path_slice(ray_interactions, rx_index, tx_index, trailing=0),
                    source_position=self.current_positions[source],
                    destination_position=self.current_positions[destination],
                    gains_db=gains_db,
                    limit=self.args.path_polylines,
                )
                visualization_ms += (time.monotonic() - visualization_started) * 1000.0
            statuses.append(status)
        if timing is not None:
            timing["tap_and_status_pack"] = (time.monotonic() - packing_started) * 1000.0 - visualization_ms
            timing["path_polylines"] = visualization_ms
        return profiles, statuses

    def trace_all_profiles(
        self,
    ) -> tuple[dict[str, MatrixProfile], list[dict[str, Any]], dict[str, float]]:
        """Trace desired/inter-cell and UE-to-UE crosstalk profiles."""

        timings_ms: dict[str, float] = {}
        # Host-observed stages: lazy GPU work may be charged to the first
        # materialization (e.g. cir_numpy), not to path_solver. Do not force
        # synchronization here, which would change the workload being measured.
        self.generation_stages_ms: dict[str, dict[str, float]] = {}
        profiles: dict[str, MatrixProfile] = {}
        statuses: list[dict[str, Any]] = []

        groups: dict[tuple[float, ArraySpec, ArraySpec], list[ScenarioLink]] = {}
        for link in self.links:
            frequency_hz = (
                self.args.downlink_frequency_hz
                if link.direction == "downlink"
                else self.args.uplink_frequency_hz
            )
            key = (
                frequency_hz,
                self.definition.nodes[link.source].tx_array,
                self.definition.nodes[link.destination].rx_array,
            )
            groups.setdefault(key, []).append(link)

        for group_index, ((frequency_hz, tx_array, rx_array), links) in enumerate(groups.items()):
            stages: dict[str, float] | None = {} if self.args.profile_timing else None
            configure_started = time.monotonic()
            self.configure_arrays(tx_array, rx_array)
            if stages is not None:
                stages["configure_arrays"] = (time.monotonic() - configure_started) * 1000.0
            started = time.monotonic()
            paths = self.trace(frequency_hz)
            if stages is not None:
                stages["path_solver"] = (time.monotonic() - started) * 1000.0
            direction_profiles, direction_statuses = self.profiles(
                paths,
                links,
                direction=links[0].direction if len({link.direction for link in links}) == 1 else "mixed",
                carrier_frequency_hz=frequency_hz,
                timing=stages,
            )
            label = f"group_{group_index}_{rx_array.antenna_count}x{tx_array.antenna_count}"
            timings_ms[label] = (time.monotonic() - started) * 1000.0
            if stages is not None:
                stages["total"] = (time.monotonic() - configure_started) * 1000.0
                self.generation_stages_ms[label] = stages
            profiles.update(direction_profiles)
            statuses.extend(direction_statuses)
        return profiles, statuses, timings_ms


def append_status(path: pathlib.Path | None, record: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    configured_links = effective_scenario(args).links
    process_id = os.getpid()
    session_id = f"sionna-rt-{process_id}-{time.time_ns()}"
    process_started_unix_ms = time.time_ns() // 1_000_000
    stop = False

    def publish_runtime(
        phase: str,
        *,
        iteration: int = 0,
        detail: str | None = None,
        next_update_in_ms: float | None = None,
    ) -> None:
        """Publish lifecycle state without sending anything to GPU Channel."""

        append_status(
            args.status_jsonl,
            {
                "event": "sionna_rt_runtime",
                "session_id": session_id,
                "process_id": process_id,
                "process_started_unix_ms": process_started_unix_ms,
                "observed_unix_ms": time.time_ns() // 1_000_000,
                "phase": phase,
                "detail": detail,
                "iteration": iteration,
                "execution_mode": "single_process_repeating_update_loop",
                "scenario_initialized_once": True,
                "channel_trace_per_iteration": True,
                "control_send_per_iteration": not args.dry_run,
                "link_count": len(configured_links),
                "target_update_hz": args.update_hz,
                "configured_duration_seconds": args.duration,
                "configured_iteration_limit": args.iterations,
                "next_update_in_ms": next_update_in_ms,
            },
        )

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    publish_runtime(
        "initializing_scene",
        detail="Sionna scene and PathSolver are being initialized once",
    )
    scenario: SionnaScenario | None = None
    client: ZmqControlClient | None = None
    fanout_clients: list[tuple[str, dict[str, str], ZmqControlClient]] = []
    executor: concurrent.futures.ThreadPoolExecutor | None = None
    start = 0.0
    next_update = 0.0
    iteration = 0
    try:
        scenario = SionnaScenario(args)
        if args.position_endpoint:
            scenario.position_source = ExternalPositionSource(
                args.position_endpoint,
                offset=args.position_frame_offset,
                timeout_s=args.position_timeout_s,
                known_nodes=scenario.node_ids,
            )
        write_scene_mesh(scene_mesh_path(args.status_jsonl), scenario.scene_mesh)
        environment = scenario_environment(args)
        client = None if args.dry_run else ZmqControlClient(args.control_endpoint)
        if client is not None and args.fanout_control_endpoint:
            fanout_clients = [(args.control_endpoint, {}, client)]
            for endpoint, rename in args.fanout_control_endpoint:
                fanout_clients.append((endpoint, rename, ZmqControlClient(endpoint)))
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=len(fanout_clients))
        grid = GridTimeline(args.update_hz) if args.timeline == "grid" else None
        hold_pending = grid is not None and args.hold_until_file is not None
        last_hold_note = 0.0
        start = time.monotonic()
        next_update = start
        if grid is not None and not hold_pending:
            grid.anchor(start, time.time_ns() // 1_000_000)
        publish_runtime(
            "ready",
            detail="scene initialized; entering the repeating update loop",
        )
        while not stop:
            now = time.monotonic()
            if args.duration > 0.0 and now - start >= args.duration:
                break
            if args.iterations > 0 and iteration >= args.iterations:
                break
            grid_index: int | None = None
            timeline_phase: str | None = None
            if grid is None:
                if now < next_update:
                    publish_runtime(
                        "waiting_for_next_update",
                        iteration=iteration,
                        detail="same process is waiting; the scene is not reinitialized",
                        next_update_in_ms=(next_update - now) * 1000.0,
                    )
                    time.sleep(next_update - now)
                    if stop:
                        break
                update_started = time.monotonic()
                update_started_unix_ms = time.time_ns() // 1_000_000
                elapsed = time.monotonic() - start
                scenario_seconds = elapsed
            else:
                if hold_pending and iteration > 0:
                    # Scenario time 0 is already applied; keep it until the
                    # gate says the measurement starts.
                    if args.hold_until_file.exists():
                        hold_pending = False
                        grid.anchor(time.monotonic(), time.time_ns() // 1_000_000)
                        publish_runtime(
                            "anchored",
                            iteration=iteration,
                            detail=f"grid point 0 anchored at unix_ms {grid.anchor_unix_ms}",
                        )
                    else:
                        if now - last_hold_note >= 1.0:
                            publish_runtime(
                                "holding",
                                iteration=iteration,
                                detail="scenario time 0 is applied; waiting for the start file",
                            )
                            last_hold_note = now
                        time.sleep(0.01)
                        continue
                if hold_pending:
                    timeline_phase = "hold"
                    scenario_seconds = 0.0
                else:
                    timeline_phase = "run"
                    following = 0 if grid.last_index is None else grid.last_index + 1
                    due = grid.due(following)
                    now = time.monotonic()
                    if now < due:
                        publish_runtime(
                            "waiting_for_next_update",
                            iteration=iteration,
                            detail="grid timeline: waiting for the next grid point",
                            next_update_in_ms=(due - now) * 1000.0,
                        )
                        time.sleep(due - now)
                        if stop:
                            break
                    grid_index = grid.next_point(time.monotonic())
                    scenario_seconds = grid.scenario_time(grid_index)
                update_started = time.monotonic()
                update_started_unix_ms = time.time_ns() // 1_000_000
                elapsed = scenario_seconds
            positions = scenario.update_positions(scenario_seconds)
            generation_started = time.monotonic()
            publish_runtime(
                "tracing_channels",
                iteration=iteration,
                detail="recomputing all directed Sionna RT paths for this update",
            )
            profiles, statuses, direction_trace_ms = scenario.trace_all_profiles()
            channel_generation_ms = (time.monotonic() - generation_started) * 1000.0
            batch_id = f"sionna-{iteration}"
            fanout_results: list[dict[str, Any]] | None = None

            if client is not None:
                publish_runtime(
                    "sending_control",
                    iteration=iteration,
                    detail=f"sending the {len(configured_links)}-link profile batch and waiting for ACK",
                )
                control_started = time.monotonic()
                if executor is not None:
                    fanout_results = send_fanout(
                        fanout_clients, profiles, batch_id=batch_id, executor=executor
                    )
                    reply = fanout_results[0]["reply"]
                else:
                    reply = client.send_matrix_profiles(profiles, batch_id=batch_id)
                control_transaction_ms: float | None = (
                    time.monotonic() - control_started
                ) * 1000.0
                generate_to_control_ack_ms: float | None = (
                    time.monotonic() - generation_started
                ) * 1000.0
                control_ack_unix_ms: int | None = time.time_ns() // 1_000_000
            else:
                control_transaction_ms = None
                generate_to_control_ack_ms = None
                control_ack_unix_ms = None
                reply = {
                    "ok": True,
                    "dry_run": True,
                    "batch_id": batch_id,
                    "messages": [
                        make_matrix_profile_swap(link_id, profile, batch_id=batch_id)
                        for link_id, profile in profiles.items()
                    ],
                }

            total_update_ms = (time.monotonic() - update_started) * 1000.0

            record = {
                "event": "sionna_rt_update",
                "session_id": session_id,
                "process_id": process_id,
                "iteration": iteration,
                "batch_id": batch_id,
                "elapsed_seconds": elapsed,
                "update_started_unix_ms": update_started_unix_ms,
                "control_ack_unix_ms": control_ack_unix_ms,
                # Keep trace_ms/direction_trace_ms for existing readers.
                "trace_ms": channel_generation_ms,
                "direction_trace_ms": direction_trace_ms,
                "timing_ms": {
                    "channel_generation": channel_generation_ms,
                    "control_transaction": control_transaction_ms,
                    "generate_to_control_ack": generate_to_control_ack_ms,
                    "total_update": total_update_ms,
                    "directions": direction_trace_ms,
                },
                "environment": environment,
                # UI-only scene footprints. They are written to status JSONL
                # and never included in profile_swap control messages.
                "scene_geometry": scenario.scene_geometry,
                "frequencies_hz": {
                    "downlink": args.downlink_frequency_hz,
                    "uplink": args.uplink_frequency_hz,
                },
                "positions": positions,
                "velocities_mps": scenario.current_velocities,
                # "external" once any node follows the live feed; the per-node
                # split and the feed counters live in position_status.
                "position_source": (
                    "external"
                    if "external" in scenario.position_sources.values()
                    else "scripted"
                ),
                "position_status": scenario.position_status,
                "channels": statuses,
                "control_reply": reply,
            }
            if args.profile_timing:
                record["timing_ms"]["generation_stages"] = scenario.generation_stages_ms
            if args.profile_digest:
                record["profile_sha256"] = profiles_digest(profiles)
            if grid is not None:
                record["timeline"] = {
                    "mode": "grid",
                    "phase": timeline_phase,
                    "grid_index": grid_index,
                    "scenario_time_s": scenario_seconds,
                    "anchor_unix_ms": grid.anchor_unix_ms,
                    "skipped_grid_points": grid.skipped,
                    # How far behind its grid instant this update started.
                    "lag_ms": (
                        (update_started - grid.due(grid_index)) * 1000.0
                        if grid_index is not None
                        else None
                    ),
                }
            if fanout_results is not None:
                record["fanout"] = [
                    {key: value for key, value in item.items() if key != "reply"}
                    | {"ok": bool(item["reply"].get("ok"))}
                    for item in fanout_results
                ]
            print(json.dumps(record, separators=(",", ":")), flush=True)
            append_status(args.status_jsonl, record)
            iteration += 1
            if grid is None:
                next_update += 1.0 / args.update_hz
                # A slow trace must not produce a burst of stale updates.
                next_update = max(next_update, time.monotonic())
        publish_runtime(
            "stopped",
            iteration=iteration,
            detail="configured limit reached or stop signal received",
        )
    except Exception as exc:
        publish_runtime(
            "failed",
            iteration=iteration,
            detail=f"{type(exc).__name__}: {exc}",
        )
        raise
    finally:
        if executor is not None:
            executor.shutdown(wait=True)
        for _, _, extra in fanout_clients[1:]:
            extra.close()
        if client is not None:
            client.close()
        if scenario is not None and scenario.position_source is not None:
            scenario.position_source.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(
            json.dumps(
                {
                    "event": "sionna_rt_fatal",
                    "exception_type": type(exc).__name__,
                    "error": str(exc),
                }
            ),
            file=sys.stderr,
            flush=True,
        )
        traceback.print_exc(file=sys.stderr)
        raise SystemExit(1)
