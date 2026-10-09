#!/usr/bin/env python3
"""Build a Sionna RT scene from OpenStreetMap building and road geometry.

The output is a plain Mitsuba scene directory that `run_bridge.py --scene`
loads by name:

    use_cases/configs/sionna/scenes/<name>/
        scene.xml          Mitsuba scene, ITU radio materials, relative meshes
        meshes/*.ply       ground, road strips, extruded buildings
        osm.json           the raw Overpass response the scene was built from
        manifest.json      what each mesh is, in scene metres, for scenario
                           configs and for anyone auditing the geometry

`osm.json` is kept so a rebuild is reproducible and works offline: pass
`--offline` to rebuild from it without touching the network. OpenStreetMap
data is ODbL 1.0 and the attribution is recorded in the manifest and in the
generated XML.

Coordinates are a local east/north/up frame in metres about `--center`, which
is what Sionna scenes use. East is +x, north is +y, up is +z.

Example:

    python3 apps/sionna_bridge/build_osm_scene.py \
        --name sionna_SUTD_test --center 1.34125 103.96345 --half-extent-m 230
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import re
import struct
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, Iterable, Sequence


OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OSM_ATTRIBUTION = "© OpenStreetMap contributors, ODbL 1.0"
EARTH_METRES_PER_DEGREE_LAT = 110_574.0

# Storey height used when a building carries `building:levels` but no
# `height`, and the fallback for a building that carries neither. Both are
# deliberately generic: a wrong height changes which reflections exist, so it
# is recorded per building in the manifest rather than hidden here.
METRES_PER_LEVEL = 3.5
DEFAULT_BUILDING_HEIGHT_M = 12.0

# Carriageway widths by OSM highway class. A road is drawn as a flat strip,
# which is all a ray tracer needs from it.
ROAD_WIDTHS_M = {
    "motorway": 14.0, "trunk": 12.0, "primary": 11.0, "secondary": 9.0,
    "tertiary": 8.0, "unclassified": 6.5, "residential": 6.5, "service": 4.5,
    "living_street": 5.0,
}
ROAD_SURFACE_Z_M = 0.03      # lifted off the ground plane to avoid z-fighting

# OSM sometimes states the facade material outright. When it does, that beats
# any guess from the building class, so it is checked first.
OSM_MATERIAL_TAGS = {
    "brick": "brick", "brick_block": "brick", "concrete": "concrete",
    "cement_block": "concrete", "glass": "glass", "metal": "metal",
    "steel": "metal", "wood": "wood", "timber_framing": "wood",
    "stone": "marble", "marble": "marble", "granite": "marble",
    "plaster": "plasterboard", "plasterboard": "plasterboard",
}

# ITU P.2040 material per OSM building tag, used when no material is stated.
# Anything unrecognised is concrete, which is the usual assumption for an
# urban RT scene — and, since most OSM buildings carry only `building=yes`,
# it is what most of a scene will end up being.
BUILDING_MATERIALS = {
    "glass": "glass", "commercial": "glass", "office": "glass",
    "retail": "glass", "university": "glass", "school": "concrete",
    "apartments": "concrete", "dormitory": "concrete", "residential": "concrete",
    "house": "brick", "semidetached_house": "brick", "detached": "brick",
    "terrace": "brick", "industrial": "metal", "warehouse": "metal",
    "train_station": "concrete", "transportation": "concrete",
    "container": "metal", "hangar": "metal", "roof": "metal",
    "guardhouse": "concrete", "civic": "concrete", "public": "concrete",
    "hospital": "concrete", "college": "glass",
}
DEFAULT_BUILDING_MATERIAL = "concrete"

# Ground cover, keyed by the OSM tag that identifies it. The first element is
# the scene kind (which drives the id prefix and therefore the web UI's
# styling); the second is the ITU material, because a lawn and a pond do not
# reflect alike and the solver has to know which is which.
AREA_FEATURES: dict[tuple[str, str], tuple[str, str]] = {
    ("natural", "water"): ("water", "wet_ground"),
    ("natural", "wetland"): ("water", "wet_ground"),
    ("waterway", "riverbank"): ("water", "wet_ground"),
    ("waterway", "dock"): ("water", "wet_ground"),
    ("landuse", "reservoir"): ("water", "wet_ground"),
    ("landuse", "basin"): ("water", "wet_ground"),
    ("leisure", "swimming_pool"): ("water", "wet_ground"),
    ("landuse", "grass"): ("grass", "medium_dry_ground"),
    ("landuse", "meadow"): ("grass", "medium_dry_ground"),
    ("landuse", "village_green"): ("grass", "medium_dry_ground"),
    ("landuse", "recreation_ground"): ("grass", "medium_dry_ground"),
    ("landuse", "cemetery"): ("grass", "medium_dry_ground"),
    ("natural", "grassland"): ("grass", "medium_dry_ground"),
    ("natural", "scrub"): ("scrub", "medium_dry_ground"),
    ("natural", "heath"): ("scrub", "medium_dry_ground"),
    ("landuse", "forest"): ("wood", "medium_dry_ground"),
    ("natural", "wood"): ("wood", "medium_dry_ground"),
    ("natural", "sand"): ("sand", "very_dry_ground"),
    ("natural", "beach"): ("sand", "very_dry_ground"),
    ("leisure", "park"): ("park", "medium_dry_ground"),
    ("leisure", "garden"): ("park", "medium_dry_ground"),
    ("leisure", "golf_course"): ("grass", "medium_dry_ground"),
    ("leisure", "pitch"): ("pitch", "medium_dry_ground"),
    ("leisure", "track"): ("pitch", "medium_dry_ground"),
}

# Ground cover sits just above the base plane and just below the carriageways.
# The gaps are millimetres so they cannot matter at 1.8 GHz, but coplanar
# surfaces would make the solver's choice of first hit arbitrary.
AREA_SURFACE_Z_M = {"water": 0.02}
DEFAULT_AREA_Z_M = 0.01
GROUND_MATERIAL = "medium_dry_ground"
ROAD_MATERIAL = "concrete"


# --------------------------------------------------------------------------
# Overpass


def overpass_query(south: float, west: float, north: float, east: float) -> str:
    box = f"{south},{west},{north},{east}"
    # Relations matter: an OSM building with a courtyard — SUTD's Building 1
    # and Building 2 among them — is a multipolygon relation, not a way. So is
    # a river or a park of any size.
    return f"""[out:json][timeout:90];
(
  way["building"]({box});
  relation["building"]({box});
  way["highway"]({box});
  way["landuse"]({box});
  way["natural"]({box});
  way["leisure"]({box});
  way["waterway"]({box});
  relation["landuse"]({box});
  relation["natural"]({box});
  relation["leisure"]({box});
);
out geom;"""


def fetch_osm(bbox: tuple[float, float, float, float], *, url: str) -> dict[str, Any]:
    query = overpass_query(*bbox)
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode({"data": query}).encode("utf-8"),
        headers={"User-Agent": "ocudu-gpu-channel scene builder"},
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            payload = response.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Overpass request failed: {exc}") from exc
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        head = payload[:200].decode("utf-8", "replace")
        raise RuntimeError(f"Overpass returned non-JSON: {head!r}") from exc


# --------------------------------------------------------------------------
# Geometry helpers


class Projection:
    """Equirectangular lat/lon → local east/north metres.

    Exact enough well past the few hundred metres a ray-tracing scene spans,
    and it keeps the scene axis-aligned with north, which is what makes the
    manifest coordinates readable.
    """

    def __init__(self, latitude: float, longitude: float) -> None:
        self.latitude = latitude
        self.longitude = longitude
        self.metres_per_degree_lon = (
            EARTH_METRES_PER_DEGREE_LAT * math.cos(math.radians(latitude))
        )

    def to_xy(self, latitude: float, longitude: float) -> tuple[float, float]:
        return (
            (longitude - self.longitude) * self.metres_per_degree_lon,
            (latitude - self.latitude) * EARTH_METRES_PER_DEGREE_LAT,
        )


def signed_area(ring: Sequence[tuple[float, float]]) -> float:
    total = 0.0
    for (x1, y1), (x2, y2) in zip(ring, list(ring[1:]) + [ring[0]]):
        total += x1 * y2 - x2 * y1
    return 0.5 * total


def counter_clockwise(ring: list[tuple[float, float]]) -> list[tuple[float, float]]:
    return ring if signed_area(ring) >= 0.0 else ring[::-1]


def centroid(ring: Sequence[tuple[float, float]]) -> tuple[float, float]:
    area = signed_area(ring)
    if abs(area) < 1e-9:
        return (
            sum(point[0] for point in ring) / len(ring),
            sum(point[1] for point in ring) / len(ring),
        )
    cx = cy = 0.0
    for (x1, y1), (x2, y2) in zip(ring, list(ring[1:]) + [ring[0]]):
        cross = x1 * y2 - x2 * y1
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    return (cx / (6.0 * area), cy / (6.0 * area))


def convex_hull(points: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    """Andrew's monotone chain, counter-clockwise, without the repeated end."""

    ordered = sorted(set(points))
    if len(ordered) < 3:
        return list(ordered)

    def half(sequence: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
        chain: list[tuple[float, float]] = []
        for point in sequence:
            while len(chain) >= 2:
                (ax, ay), (bx, by) = chain[-2], chain[-1]
                if (bx - ax) * (point[1] - ay) - (by - ay) * (point[0] - ax) > 0:
                    break
                chain.pop()
            chain.append(point)
        return chain

    return half(ordered)[:-1] + half(ordered[::-1])[:-1]


def point_in_ring(point: tuple[float, float], ring: Sequence[tuple[float, float]]) -> bool:
    x, y = point
    inside_ring = False
    for index in range(len(ring)):
        x1, y1 = ring[index]
        x2, y2 = ring[(index + 1) % len(ring)]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside_ring = not inside_ring
    return inside_ring


def segments_cross(
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
    d: tuple[float, float],
) -> bool:
    """Do segments ab and cd meet? Touching counts as meeting.

    A walk that grazes a wall is a walk through it once the footprint is
    extruded to its height, so there is no reason to be lenient here.
    """

    def turn(p, q, r) -> float:
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    turns = (turn(a, b, c), turn(a, b, d), turn(c, d, a), turn(c, d, b))
    if (turns[0] > 0.0) != (turns[1] > 0.0) and (turns[2] > 0.0) != (turns[3] > 0.0):
        return True

    def between(p, q, r) -> bool:
        return (min(p[0], q[0]) <= r[0] <= max(p[0], q[0])
                and min(p[1], q[1]) <= r[1] <= max(p[1], q[1]))

    return any(
        value == 0.0 and between(*points)
        for value, points in zip(turns, ((a, b, c), (a, b, d), (c, d, a), (c, d, b)))
    )


def walk_enters_ring(
    walk: Sequence[tuple[float, float]],
    ring: Sequence[tuple[float, float]],
) -> bool:
    """Does the closed walk pass inside `ring` anywhere along its length?"""

    if any(point_in_ring(point, ring) for point in walk):
        return True
    edges = zip(walk, list(walk[1:]) + [walk[0]])
    wall_edges = list(zip(ring, list(ring[1:]) + [ring[0]]))
    return any(
        segments_cross(a, b, c, d)
        for a, b in edges
        for c, d in wall_edges
    )


def offset_hull(
    hull: Sequence[tuple[float, float]], margin_m: float
) -> list[tuple[float, float]]:
    """The hull's corners pushed `margin_m` outward along their radials.

    Every corner moves away from the centroid, so each edge of the result
    spans a triangle that contains the edge it replaces: the whole walk clears
    the hull, not only the corners it is made of.
    """

    cx, cy = centroid(hull)
    walk: list[tuple[float, float]] = []
    for x, y in hull:
        dx, dy = x - cx, y - cy
        distance = math.hypot(dx, dy)
        if distance < 1e-6:
            continue
        scale = (distance + margin_m) / distance
        walk.append((cx + dx * scale, cy + dy * scale))
    return walk


def outward_ring(
    ring: Sequence[tuple[float, float]],
    margin_m: float,
    z: float,
    obstacles: Sequence[Sequence[tuple[float, float]]] = (),
) -> list[list[float]]:
    """A closed walk that goes round a footprint, `margin_m` clear of it.

    The hull comes first and the offset second. Pushing the footprint's own
    corners outward along their radials looks tempting, but a real campus
    building is L- or U-shaped, and on a concave corner that route folds back
    through itself — a pedestrian would be walking through a wall. The convex
    hull cannot do that, and "walks round the outside of the building" is
    exactly what it describes.

    A neighbour can stand where the offset hull wants to go. Exiling that one
    corner along its radial until it lands in the open — what this used to do —
    is wrong twice over: only corners were ever tested, so the two legs that
    reach the exiled corner still cut through the neighbour, and it is the
    exile that drags them there. On the SUTD campus that walked building_2's
    pedestrian 138 m through building_1. A neighbour in the way instead joins
    the hull and the walk rounds the whole cluster, which is the only closed
    route outside all of them, and is what a pedestrian does where two
    buildings abut. A neighbour the walk merely encircles is left alone.
    """

    pending = [list(obstacle) for obstacle in obstacles]
    cluster = list(ring)
    # Absorbing is the only move, so the obstacle count bounds the passes.
    for _ in range(len(pending) + 1):
        hull = convex_hull(cluster)
        if len(hull) < 3:
            return []
        walk = offset_hull(hull, margin_m)
        blocking = [other for other in pending if walk_enters_ring(walk, other)]
        if not blocking:
            return [[round(x, 2), round(y, 2), z] for x, y in walk]
        for other in blocking:
            cluster.extend(other)
            pending.remove(other)
    return []


def stitch_rings(
    ways: Sequence[Sequence[tuple[float, float]]]
) -> list[list[tuple[float, float]]]:
    """Join multipolygon member ways end-to-end into closed rings.

    Overpass hands back a relation's members as separate open ways in no
    particular order or direction, so a ring has to be walked out by matching
    endpoints. Fragments that never close are dropped.
    """

    remaining = [list(way) for way in ways if len(way) >= 2]
    rings: list[list[tuple[float, float]]] = []
    while remaining:
        ring = remaining.pop(0)
        extended = True
        while extended and ring[0] != ring[-1]:
            extended = False
            for index, candidate in enumerate(remaining):
                if candidate[0] == ring[-1]:
                    ring += candidate[1:]
                elif candidate[-1] == ring[-1]:
                    ring += candidate[-2::-1]
                elif candidate[-1] == ring[0]:
                    ring = candidate[:-1] + ring
                elif candidate[0] == ring[0]:
                    ring = candidate[:0:-1] + ring
                else:
                    continue
                remaining.pop(index)
                extended = True
                break
        if ring[0] == ring[-1] and len(ring) >= 4:
            rings.append(ring[:-1])
    return rings


def _is_ear(
    ring: Sequence[tuple[float, float]], previous: int, current: int, nxt: int,
    indices: Sequence[int],
) -> bool:
    ax, ay = ring[previous]
    bx, by = ring[current]
    cx, cy = ring[nxt]
    cross = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
    if cross <= 0.0:                       # reflex corner on a CCW ring
        return False
    for index in indices:
        if index in (previous, current, nxt):
            continue
        px, py = ring[index]
        d1 = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
        d2 = (cx - bx) * (py - by) - (cy - by) * (px - bx)
        d3 = (ax - cx) * (py - cy) - (ay - cy) * (px - cx)
        if d1 >= 0.0 and d2 >= 0.0 and d3 >= 0.0:
            return False                   # another vertex is inside the ear
    return True


def triangulate(ring: Sequence[tuple[float, float]]) -> list[tuple[int, int, int]]:
    """Ear-clip a simple counter-clockwise polygon into triangles.

    A fan would do for the convex boxes of a synthetic scene, but real OSM
    footprints are L- and U-shaped, and a fan across a reflex corner puts
    roof triangles outside the building.
    """

    indices = list(range(len(ring)))
    triangles: list[tuple[int, int, int]] = []
    guard = 0
    while len(indices) > 3 and guard < len(ring) * len(ring) + 16:
        guard += 1
        for position, current in enumerate(indices):
            previous = indices[position - 1]
            nxt = indices[(position + 1) % len(indices)]
            if _is_ear(ring, previous, current, nxt, indices):
                triangles.append((previous, current, nxt))
                indices.pop(position)
                break
        else:
            break                          # self-intersecting: stop cleanly
    if len(indices) == 3:
        triangles.append((indices[0], indices[1], indices[2]))
    return triangles


def building_material(
    tags: dict[str, str],
    overrides: dict[str, str] | None = None,
    mesh_id: str | None = None,
) -> tuple[str, str]:
    """The ITU material for a building, and where the choice came from.

    Material decides how a surface reflects, so how each one was arrived at is
    recorded rather than hidden: OSM rarely states a material at all, and a
    scene where almost everything is concrete should say so plainly instead of
    looking varied.
    """

    if overrides:
        for key in (mesh_id, tags.get("name"), str(tags.get("building", ""))):
            if key and key in overrides:
                return overrides[key], "override"
    for key in ("building:material", "material", "building:facade:material"):
        stated = OSM_MATERIAL_TAGS.get(str(tags.get(key, "")).lower())
        if stated:
            return stated, key
    building = tags.get("building", "")
    if building in BUILDING_MATERIALS:
        return BUILDING_MATERIALS[building], "building tag"
    return DEFAULT_BUILDING_MATERIAL, "default"


def building_height(tags: dict[str, str]) -> tuple[float, str]:
    raw_height = tags.get("height")
    if raw_height:
        try:
            return float(str(raw_height).split()[0]), "height tag"
        except ValueError:
            pass
    levels = tags.get("building:levels")
    if levels:
        try:
            return float(levels) * METRES_PER_LEVEL, f"{levels} levels"
        except ValueError:
            pass
    return DEFAULT_BUILDING_HEIGHT_M, "default"


# --------------------------------------------------------------------------
# PLY output


def write_ply(path: pathlib.Path, vertices: Sequence[Sequence[float]],
              faces: Sequence[Sequence[int]]) -> None:
    """Write a binary little-endian PLY, the format the Sionna scenes use."""

    header = (
        "ply\n"
        "format binary_little_endian 1.0\n"
        f"element vertex {len(vertices)}\n"
        "property float x\nproperty float y\nproperty float z\n"
        f"element face {len(faces)}\n"
        "property list uchar int vertex_indices\n"
        "end_header\n"
    ).encode("ascii")
    body = bytearray(header)
    for vertex in vertices:
        body += struct.pack("<3f", *vertex)
    for face in faces:
        body += struct.pack("<B", len(face)) + struct.pack(f"<{len(face)}i", *face)
    path.write_bytes(body)


def extrusion(
    ring: Sequence[tuple[float, float]], z_min: float, z_max: float
) -> tuple[list[list[float]], list[list[int]]]:
    """Walls plus a roof for a footprint wound counter-clockwise.

    The floor is left off: it sits under the ground plane, so it can never be
    the first thing a ray meets, and leaving it out halves nothing that
    matters while keeping the mesh small.
    """

    count = len(ring)
    vertices = [[x, y, z_min] for x, y in ring] + [[x, y, z_max] for x, y in ring]
    faces: list[list[int]] = []
    for index in range(count):
        nxt = (index + 1) % count
        # Counter-clockwise footprint → this winding faces away from the
        # building, so the wall normal points at the street.
        faces.append([index, nxt, count + nxt])
        faces.append([index, count + nxt, count + index])
    for a, b, c in triangulate(ring):
        faces.append([count + a, count + b, count + c])
    return vertices, faces


def clip_polygon(
    ring: Sequence[tuple[float, float]], half_extent_m: float
) -> list[tuple[float, float]]:
    """Sutherland-Hodgman clip of a ring to the square window.

    Ground cover is tagged over whole city blocks, so a park or a river caught
    by the window usually extends far past it. Clipping keeps the scene edge
    a clean square instead of leaving green and blue hanging off the ground.
    """

    limit = half_extent_m
    output = list(ring)
    for axis, sign in ((0, 1), (0, -1), (1, 1), (1, -1)):
        if not output:
            return []
        inside_edge = lambda point: point[axis] * sign <= limit  # noqa: E731
        clipped: list[tuple[float, float]] = []
        for current, following in zip(output, output[1:] + output[:1]):
            current_in, following_in = inside_edge(current), inside_edge(following)
            if current_in:
                clipped.append(current)
            if current_in != following_in:
                span = following[axis] - current[axis]
                if abs(span) > 1e-12:
                    ratio = (sign * limit - current[axis]) / span
                    crossing = [0.0, 0.0]
                    crossing[axis] = sign * limit
                    other = 1 - axis
                    crossing[other] = current[other] + (following[other] - current[other]) * ratio
                    clipped.append((round(crossing[0], 3), round(crossing[1], 3)))
        output = clipped
    return output


def clip_polyline(
    line: Sequence[tuple[float, float]], half_extent_m: float
) -> list[list[tuple[float, float]]]:
    """Cut a centreline to the square window, keeping the pieces inside it.

    Without this a way that merely clips the corner of the window is drawn in
    full, and its carriageway runs off the edge of the ground plane — which is
    what made the campus look like it had roads floating in space.
    """

    limit = half_extent_m
    pieces: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    for start, end in zip(line, line[1:]):
        # Liang-Barsky against the axis-aligned window.
        t0, t1 = 0.0, 1.0
        dx, dy = end[0] - start[0], end[1] - start[1]
        clipped = True
        for p_value, q_value in (
            (-dx, start[0] + limit), (dx, limit - start[0]),
            (-dy, start[1] + limit), (dy, limit - start[1]),
        ):
            if abs(p_value) < 1e-12:
                if q_value < 0.0:
                    clipped = False
                    break
                continue
            ratio = q_value / p_value
            if p_value < 0.0:
                t0 = max(t0, ratio)
            else:
                t1 = min(t1, ratio)
        if not clipped or t0 > t1:
            if current:
                pieces.append(current)
                current = []
            continue
        a = (start[0] + dx * t0, start[1] + dy * t0)
        b = (start[0] + dx * t1, start[1] + dy * t1)
        if current and math.dist(current[-1], a) < 1e-6:
            current.append(b)
        else:
            if current:
                pieces.append(current)
            current = [a, b]
    if current:
        pieces.append(current)
    return [piece for piece in pieces if len(piece) >= 2]


def road_strip(
    line: Sequence[tuple[float, float]], width_m: float, z: float
) -> tuple[list[list[float]], list[list[int]]]:
    """One quad per segment of a road centreline, `width_m` wide.

    Corners are left unmitred: the small notches on the outside of a bend are
    invisible at scene scale and cost nothing in the solver.
    """

    vertices: list[list[float]] = []
    faces: list[list[int]] = []
    quads: list[tuple[int, tuple[float, float]]] = []
    half = 0.5 * width_m
    for (x1, y1), (x2, y2) in zip(line, line[1:]):
        dx, dy = x2 - x1, y2 - y1
        length = math.hypot(dx, dy)
        if length < 1e-6:
            continue
        nx, ny = -dy / length * half, dx / length * half
        base = len(vertices)
        vertices += [
            [x1 + nx, y1 + ny, z], [x1 - nx, y1 - ny, z],
            [x2 - nx, y2 - ny, z], [x2 + nx, y2 + ny, z],
        ]
        faces.append([base, base + 1, base + 2])
        faces.append([base, base + 2, base + 3])
        quads.append((base, (x1, y1)))
    # Each pair of segments leaves a wedge open on the outside of the bend.
    # Two triangles through the shared vertex close it; the one on the inside
    # of the turn is degenerate and costs nothing.
    for (previous_base, _), (next_base, joint) in zip(quads, quads[1:]):
        centre = len(vertices)
        vertices.append([joint[0], joint[1], z])
        faces.append([centre, previous_base + 3, next_base])
        faces.append([centre, next_base + 1, previous_base + 2])
    return vertices, faces


# A carriageway is clipped by its centreline, so a road running along the edge
# of the window still reaches half its width past it. The ground carries a
# skirt wide enough for the widest road, or those strips hang over nothing.
GROUND_SKIRT_M = 0.5 * max(ROAD_WIDTHS_M.values()) + 2.0


def ground_plane(half_extent_m: float) -> tuple[list[list[float]], list[list[int]]]:
    extent = half_extent_m + GROUND_SKIRT_M
    vertices = [
        [-extent, -extent, 0.0], [extent, -extent, 0.0],
        [extent, extent, 0.0], [-extent, extent, 0.0],
    ]
    return vertices, [[0, 1, 2], [0, 2, 3]]


# --------------------------------------------------------------------------
# Scene assembly


def slug(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return cleaned or "unnamed"


def assign_building_ids(buildings: Sequence[dict[str, Any]]) -> list[str]:
    """Mesh ids that a person can recognise in the web UI.

    A building OSM knows by name keeps it — "Building 2" becomes
    `building_2` — so a scenario config and the rendered label agree with the
    campus signage. Everything else falls back to `building_<n>`, skipping
    any number a name already claimed. The `building` prefix is what the web
    UI keys its building styling off, so every id has to carry it.
    """

    ids: list[str | None] = [None] * len(buildings)
    taken: set[str] = set()
    for index, item in enumerate(buildings):
        name = item["tags"].get("name")
        if not name:
            continue
        base = slug(name)
        candidate = base if base.startswith("building") else f"building_{base}"
        if item["outer_ring_count"] > 1:
            candidate = f"{candidate}_part{sum(1 for i in taken if i.startswith(candidate)) + 1}"
        unique = candidate
        suffix = 2
        while unique in taken:
            unique = f"{candidate}_{suffix}"
            suffix += 1
        ids[index] = unique
        taken.add(unique)
    counter = 1
    for index, value in enumerate(ids):
        if value is not None:
            continue
        while f"building_{counter}" in taken:
            counter += 1
        ids[index] = f"building_{counter}"
        taken.add(f"building_{counter}")
    return ids  # type: ignore[return-value]


def inside(points: Iterable[tuple[float, float]], half_extent_m: float) -> bool:
    return any(
        abs(x) <= half_extent_m and abs(y) <= half_extent_m for x, y in points
    )


def build(
    osm: dict[str, Any],
    *,
    projection: Projection,
    half_extent_m: float,
    output: pathlib.Path,
    name: str,
    walk_margin_m: float,
    material_overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    meshes = output / "meshes"
    meshes.mkdir(parents=True, exist_ok=True)
    for stale in meshes.glob("*.ply"):
        stale.unlink()

    elements = osm.get("elements", [])
    buildings: list[dict[str, Any]] = []
    roads: list[dict[str, Any]] = []
    areas: list[dict[str, Any]] = []

    def project(geometry: Sequence[dict[str, float]]) -> list[tuple[float, float]]:
        return [
            (round(x, 3), round(y, 3))
            for x, y in (
                projection.to_xy(node["lat"], node["lon"]) for node in geometry
            )
        ]

    def area_feature(tags: dict[str, str]) -> tuple[str, str] | None:
        for key, value in tags.items():
            found = AREA_FEATURES.get((key, str(value)))
            if found:
                return found
        return None

    def add_area(osm_id: int, tags: dict[str, str],
                 ring: list[tuple[float, float]]) -> None:
        feature = area_feature(tags)
        if feature is None or len(ring) < 3:
            return
        ring = clip_polygon(ring, half_extent_m)
        if len(ring) < 3:
            return
        kind, material = feature
        areas.append({
            "osm_id": osm_id, "tags": tags,
            "ring": counter_clockwise(ring), "kind": kind, "material": material,
            "area_m2": abs(signed_area(ring)),
        })

    def add_building(osm_id: int, tags: dict[str, str],
                     ring: list[tuple[float, float]], parts: int) -> None:
        if len(ring) < 3 or not inside(ring, half_extent_m):
            return
        ring = counter_clockwise(ring)
        height, source = building_height(tags)
        buildings.append({
            "osm_id": osm_id, "tags": tags, "ring": ring,
            "height_m": height, "height_source": source,
            "area_m2": abs(signed_area(ring)), "outer_ring_count": parts,
        })

    for element in elements:
        tags = element.get("tags", {})
        if element.get("type") == "relation":
            # Multipolygon. For a building only the outer rings are extruded:
            # the courtyard is filled in, which keeps the roof a single simple
            # polygon and is the usual envelope simplification for RT. Ground
            # cover — a park or a river — comes the same way.
            outers = [
                project(member["geometry"])
                for member in element.get("members", [])
                if member.get("role") == "outer" and member.get("geometry")
            ]
            rings = stitch_rings(outers)
            if "building" in tags:
                for ring in rings:
                    add_building(element["id"], tags, ring, len(rings))
            else:
                for ring in rings:
                    add_area(element["id"], tags, ring)
            continue
        geometry = element.get("geometry")
        if not geometry:
            continue
        points = project(geometry)
        if "building" in tags:
            if not inside(points, half_extent_m):
                continue
            add_building(
                element["id"], tags,
                points[:-1] if points[0] == points[-1] else points, 1,
            )
        elif "highway" in tags:
            width = ROAD_WIDTHS_M.get(tags["highway"])
            if width is None:
                continue      # footways, cycleways and steps are not surfaces
            for piece in clip_polyline(points, half_extent_m):
                roads.append({
                    "osm_id": element["id"], "tags": tags,
                    "line": [(round(x, 3), round(y, 3)) for x, y in piece],
                    "width_m": width,
                })
        elif points[0] == points[-1] and len(points) >= 4:
            add_area(element["id"], tags, points[:-1])

    # Largest footprint first, so the unnamed fallback numbering starts with
    # the dominant structures and stays stable when OSM gains or loses a shed.
    buildings.sort(key=lambda item: (-item["area_m2"], item["osm_id"]))
    mesh_ids = assign_building_ids(buildings)

    shapes: list[tuple[str, str, str]] = []      # (mesh stem, shape id, material)
    manifest_buildings: list[dict[str, Any]] = []
    for index, item in enumerate(buildings):
        mesh_id = mesh_ids[index]
        vertices, faces = extrusion(item["ring"], 0.0, item["height_m"])
        write_ply(meshes / f"{mesh_id}.ply", vertices, faces)
        material, material_source = building_material(
            item["tags"], material_overrides, mesh_id
        )
        shapes.append((mesh_id, mesh_id, material))
        cx, cy = centroid(item["ring"])
        manifest_buildings.append({
            "id": mesh_id,
            "osm_id": item["osm_id"],
            "osm_name": item["tags"].get("name"),
            "osm_building": item["tags"].get("building"),
            "material": material,
            "material_source": material_source,
            "height_m": round(item["height_m"], 2),
            "height_source": item["height_source"],
            "footprint_area_m2": round(item["area_m2"], 1),
            "outer_ring_count": item["outer_ring_count"],
            "centroid_m": [round(cx, 2), round(cy, 2)],
            "roof_center_m": [round(cx, 2), round(cy, 2), round(item["height_m"], 2)],
            "footprint_m": [[x, y] for x, y in item["ring"]],
        })

    # A ready-made pedestrian route around each building, at head height, for
    # a scenario config to copy verbatim. It needs every other footprint, so
    # it waits until they are all known.
    all_rings = [item["ring"] for item in buildings]
    for index, entry in enumerate(manifest_buildings):
        entry["walk_ring_m"] = outward_ring(
            buildings[index]["ring"],
            walk_margin_m,
            1.5,
            [ring for position, ring in enumerate(all_rings) if position != index],
        )

    manifest_roads: list[dict[str, Any]] = []
    for index, item in enumerate(sorted(roads, key=lambda r: r["osm_id"]), start=1):
        vertices, faces = road_strip(item["line"], item["width_m"], ROAD_SURFACE_Z_M)
        if not faces:
            continue
        mesh_id = f"road_{index}"
        write_ply(meshes / f"{mesh_id}.ply", vertices, faces)
        shapes.append((mesh_id, mesh_id, ROAD_MATERIAL))
        manifest_roads.append({
            "id": mesh_id,
            "osm_way": item["osm_id"],
            "osm_name": item["tags"].get("name"),
            "highway": item["tags"]["highway"],
            "service": item["tags"].get("service"),
            "width_m": item["width_m"],
            "centerline_m": [[x, y] for x, y in item["line"]],
        })

    # Ground cover is drawn largest first so a pitch inside a park is written
    # after the park and therefore wins the tie on the way to the screen.
    areas.sort(key=lambda item: (-item["area_m2"], item["osm_id"]))
    manifest_areas: list[dict[str, Any]] = []
    counters: dict[str, int] = {}
    for item in areas:
        kind = item["kind"]
        counters[kind] = counters.get(kind, 0) + 1
        mesh_id = f"{kind}_{counters[kind]}"
        z = AREA_SURFACE_Z_M.get(kind, DEFAULT_AREA_Z_M)
        ring = item["ring"]
        vertices = [[x, y, z] for x, y in ring]
        faces = [[a, b, c] for a, b, c in triangulate(ring)]
        if not faces:
            continue
        write_ply(meshes / f"{mesh_id}.ply", vertices, faces)
        shapes.append((mesh_id, mesh_id, item["material"]))
        manifest_areas.append({
            "id": mesh_id,
            "kind": kind,
            "osm_id": item["osm_id"],
            "osm_name": item["tags"].get("name"),
            "material": item["material"],
            "area_m2": round(item["area_m2"], 1),
            "surface_z_m": z,
        })

    vertices, faces = ground_plane(half_extent_m)
    write_ply(meshes / "ground.ply", vertices, faces)
    shapes.append(("ground", "ground", GROUND_MATERIAL))

    write_scene_xml(output / "scene.xml", shapes)

    material_counts: dict[str, int] = {}
    for entry in manifest_buildings:
        material_counts[entry["material"]] = material_counts.get(entry["material"], 0) + 1

    return {
        "name": name,
        "attribution": OSM_ATTRIBUTION,
        # Stated up front because it is the honest answer to "why does every
        # building look the same": OpenStreetMap rarely carries a material.
        "building_materials": dict(sorted(material_counts.items())),
        "license": "OpenStreetMap data is licensed ODbL 1.0",
        "origin_lat_lon": [projection.latitude, projection.longitude],
        "half_extent_m": half_extent_m,
        "coordinate_system": "local east/north/up metres about origin_lat_lon",
        "metres_per_level": METRES_PER_LEVEL,
        "default_building_height_m": DEFAULT_BUILDING_HEIGHT_M,
        "walk_margin_m": walk_margin_m,
        "ground": {
            "material": GROUND_MATERIAL,
            "half_extent_m": half_extent_m + GROUND_SKIRT_M,
        },
        "buildings": manifest_buildings,
        "roads": manifest_roads,
        "areas": manifest_areas,
    }


def write_scene_xml(path: pathlib.Path, shapes: Sequence[tuple[str, str, str]]) -> None:
    root = ET.Element("scene", {"version": "2.1.0"})
    for material in sorted({material for _, _, material in shapes}):
        bsdf = ET.SubElement(
            root, "bsdf", {"type": "itu-radio-material", "id": material}
        )
        ET.SubElement(bsdf, "string", {"name": "type", "value": material})
        ET.SubElement(bsdf, "float", {"name": "thickness", "value": "0.2"})
    for mesh_stem, shape_id, material in shapes:
        shape = ET.SubElement(root, "shape", {"type": "ply", "id": f"mesh-{shape_id}"})
        # Relative, so the scene directory can be moved or copied into a
        # container without rewriting it.
        ET.SubElement(
            shape, "string", {"name": "filename", "value": f"meshes/{mesh_stem}.ply"}
        )
        ET.SubElement(shape, "boolean", {"name": "face_normals", "value": "true"})
        ET.SubElement(shape, "ref", {"id": material, "name": "bsdf"})
    ET.indent(root, space="  ")
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--name", required=True, help="scene directory name")
    parser.add_argument("--center", nargs=2, type=float, metavar=("LAT", "LON"),
                        help="scene origin; required unless --offline")
    parser.add_argument("--half-extent-m", type=float, default=230.0,
                        help="half the side of the square scene window")
    parser.add_argument("--walk-margin-m", type=float, default=12.0,
                        help="clearance of the suggested pedestrian ring")
    parser.add_argument(
        "--materials", type=pathlib.Path,
        help=(
            "JSON object overriding inferred ITU materials, keyed by mesh id "
            '("building_2"), OSM name or building tag value, e.g. '
            '\'{"building_2": "glass", "yes": "brick"}\'. OSM almost '
            "never states a facade material, so this is how a scene gets one "
            "that is actually known."
        ),
    )
    parser.add_argument("--output-root", type=pathlib.Path,
                        default=(pathlib.Path.cwd() if __package__ else pathlib.Path(__file__).resolve().parents[2])
                        / "use_cases" / "configs" / "sionna" / "scenes")
    parser.add_argument("--overpass-url", default=OVERPASS_URL)
    parser.add_argument("--offline", action="store_true",
                        help="rebuild from the scene's saved osm.json")
    args = parser.parse_args(argv)
    if args.half_extent_m <= 0.0:
        parser.error("half-extent-m must be positive")
    if args.center is None and not args.offline:
        parser.error("--center is required unless --offline")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    output = args.output_root / args.name
    output.mkdir(parents=True, exist_ok=True)
    raw_path = output / "osm.json"

    if args.offline:
        if not raw_path.is_file():
            raise SystemExit(f"--offline needs a previous {raw_path}")
        saved = json.loads(raw_path.read_text(encoding="utf-8"))
        center = args.center or saved["__scene__"]["center"]
        half_extent = (
            args.half_extent_m
            if args.half_extent_m != 230.0
            else saved["__scene__"]["half_extent_m"]
        )
        osm = saved
    else:
        latitude, longitude = args.center
        projection = Projection(latitude, longitude)
        # Fetch a margin beyond the window so a building straddling the edge
        # still arrives whole.
        margin = args.half_extent_m * 1.25
        south = latitude - margin / EARTH_METRES_PER_DEGREE_LAT
        north = latitude + margin / EARTH_METRES_PER_DEGREE_LAT
        west = longitude - margin / projection.metres_per_degree_lon
        east = longitude + margin / projection.metres_per_degree_lon
        osm = fetch_osm((south, west, north, east), url=args.overpass_url)
        osm["__scene__"] = {
            "center": [latitude, longitude],
            "half_extent_m": args.half_extent_m,
            "bbox": [south, west, north, east],
        }
        raw_path.write_text(json.dumps(osm, separators=(",", ":")), encoding="utf-8")
        center = [latitude, longitude]
        half_extent = args.half_extent_m

    manifest = build(
        osm,
        projection=Projection(center[0], center[1]),
        half_extent_m=half_extent,
        output=output,
        name=args.name,
        walk_margin_m=args.walk_margin_m,
        material_overrides=(
            json.loads(args.materials.read_text(encoding="utf-8"))
            if args.materials is not None
            else None
        ),
    )
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({
        "scene": str(output / "scene.xml"),
        "buildings": len(manifest["buildings"]),
        "roads": len(manifest["roads"]),
        "areas": len(manifest["areas"]),
        "attribution": OSM_ATTRIBUTION,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
