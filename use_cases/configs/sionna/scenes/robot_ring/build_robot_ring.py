#!/usr/bin/env python3
"""Generate the robot-fight ring scene (R track, ROBOT_FIGHT_MILESTONES.md R1).

The scene is deliberately tiny so a Sionna RT solve stays inside the 10 Hz
update budget: a concrete floor, a low metal fence ring, three concrete
pillars along the ring's centre line, and a mast for the gNB east of the
ring. The gNB looks west across the ring, so the pillars shadow the western
half (NLOS pockets behind each pillar) while the eastern half is LOS.

    python3 build_robot_ring.py            # writes scene.xml + meshes/ here

The output is what `resolve_scene()` in scripts/sionna_rt/run_bridge.py
picks up as scene "robot_ring": `use_cases/configs/sionna/scenes/robot_ring/scene.xml`
with binary little-endian PLY meshes, the same layout `build_osm_scene.py`
produces. Nothing here imports Sionna; the geometry is plain arithmetic.
"""

from __future__ import annotations

import json
import math
import pathlib
import struct
from typing import Sequence

HERE = pathlib.Path(__file__).resolve().parent

# --- layout (Sionna XYZ metres, z up) --------------------------------------
GROUND_HALF_EXTENT_M = 30.0
RING_INNER_RADIUS_M = 4.0        # 8 m diameter fighting surface
FENCE_THICKNESS_M = 0.1
FENCE_HEIGHT_M = 0.3             # below the UE antenna height (0.4 m)
FENCE_SEGMENTS = 48
PILLAR_SIDE_M = 0.8
PILLAR_HEIGHT_M = 3.0
PILLAR_CENTRES_M = ((0.0, -1.6), (0.0, 0.0), (0.0, 1.6))
MAST_XY_M = (15.0, 0.0)
MAST_SIDE_M = 0.2
MAST_HEIGHT_M = 7.5             # the gNB antenna is at z = 8.0, half a metre clear
GNB_ANTENNA_Z_M = 8.0           # of the mast top: a source ON a mesh face sees nothing

MATERIALS = {
    # id: (ITU type, thickness m)
    "concrete": ("concrete", 0.2),
    "metal": ("metal", 0.01),
}


def write_ply(path: pathlib.Path, vertices: Sequence[Sequence[float]],
              faces: Sequence[Sequence[int]]) -> None:
    """Binary little-endian PLY, the format the other repository scenes use."""

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


def box(cx: float, cy: float, sx: float, sy: float, z_min: float, z_max: float):
    """Walls plus roof of an axis-aligned box; the floor is under the ground."""

    hx, hy = 0.5 * sx, 0.5 * sy
    ring = [(cx - hx, cy - hy), (cx + hx, cy - hy), (cx + hx, cy + hy), (cx - hx, cy + hy)]
    return extrude(ring, z_min, z_max, closed_top=True)


def extrude(ring: Sequence[tuple[float, float]], z_min: float, z_max: float,
            *, closed_top: bool):
    n = len(ring)
    vertices = [[x, y, z_min] for x, y in ring] + [[x, y, z_max] for x, y in ring]
    faces: list[list[int]] = []
    for i in range(n):
        j = (i + 1) % n
        faces.append([i, j, n + j])
        faces.append([i, n + j, n + i])
    if closed_top:
        for i in range(1, n - 1):
            faces.append([n, n + i, n + i + 1])
    return vertices, faces


def fence_ring():
    """An annular wall: outer wall, inner wall and the flat top between them."""

    r_in = RING_INNER_RADIUS_M
    r_out = r_in + FENCE_THICKNESS_M
    n = FENCE_SEGMENTS
    outer = [(r_out * math.cos(2 * math.pi * k / n), r_out * math.sin(2 * math.pi * k / n))
             for k in range(n)]
    inner = [(r_in * math.cos(2 * math.pi * k / n), r_in * math.sin(2 * math.pi * k / n))
             for k in range(n)]
    vertices: list[list[float]] = []
    faces: list[list[int]] = []
    for ring in (outer, inner):
        v, f = extrude(ring, 0.0, FENCE_HEIGHT_M, closed_top=False)
        offset = len(vertices)
        vertices += v
        faces += [[offset + i for i in face] for face in f]
    # Top annulus: outer top vertices are [n, 2n), inner top are [3n, 4n).
    for k in range(n):
        j = (k + 1) % n
        o0, o1 = n + k, n + j
        i0, i1 = 3 * n + k, 3 * n + j
        faces.append([o0, o1, i1])
        faces.append([o0, i1, i0])
    return vertices, faces


def ground():
    e = GROUND_HALF_EXTENT_M
    return [[-e, -e, 0.0], [e, -e, 0.0], [e, e, 0.0], [-e, e, 0.0]], [[0, 1, 2], [0, 2, 3]]


def main() -> int:
    meshes = HERE / "meshes"
    meshes.mkdir(exist_ok=True)
    shapes: list[tuple[str, str]] = []   # (id, material)

    def emit(object_id: str, material: str, geometry) -> None:
        vertices, faces = geometry
        write_ply(meshes / f"{object_id}.ply", vertices, faces)
        shapes.append((object_id, material))

    emit("ground", "concrete", ground())
    emit("fence", "metal", fence_ring())
    for index, (cx, cy) in enumerate(PILLAR_CENTRES_M):
        emit(f"building_pillar_{index}", "concrete",
             box(cx, cy, PILLAR_SIDE_M, PILLAR_SIDE_M, 0.0, PILLAR_HEIGHT_M))
    emit("building_mast", "metal",
         box(MAST_XY_M[0], MAST_XY_M[1], MAST_SIDE_M, MAST_SIDE_M, 0.0, MAST_HEIGHT_M))

    lines = ["<?xml version='1.0' encoding='utf-8'?>", '<scene version="2.1.0">']
    for material_id, (itu_type, thickness) in MATERIALS.items():
        lines += [
            f'  <bsdf type="itu-radio-material" id="{material_id}">',
            f'    <string name="type" value="{itu_type}" />',
            f'    <float name="thickness" value="{thickness}" />',
            "  </bsdf>",
        ]
    for object_id, material in shapes:
        lines += [
            f'  <shape type="ply" id="mesh-{object_id}">',
            f'    <string name="filename" value="meshes/{object_id}.ply" />',
            '    <boolean name="face_normals" value="true" />',
            f'    <ref id="{material}" name="bsdf" />',
            "  </shape>",
        ]
    lines.append("</scene>")
    (HERE / "scene.xml").write_text("\n".join(lines) + "\n", encoding="utf-8")

    manifest = {
        "name": "robot_ring",
        "units": "Sionna XYZ metres, z up; ring centre at the origin",
        "ring": {"inner_radius_m": RING_INNER_RADIUS_M, "fence_height_m": FENCE_HEIGHT_M,
                 "fence_material": "metal", "segments": FENCE_SEGMENTS},
        "pillars": [{"centre_m": list(c), "side_m": PILLAR_SIDE_M, "height_m": PILLAR_HEIGHT_M,
                     "material": "concrete"} for c in PILLAR_CENTRES_M],
        "mast": {"xy_m": list(MAST_XY_M), "height_m": MAST_HEIGHT_M, "material": "metal",
                 "gnb_antenna_m": [MAST_XY_M[0], MAST_XY_M[1], GNB_ANTENNA_Z_M]},
        "ground": {"half_extent_m": GROUND_HALF_EXTENT_M, "material": "concrete"},
        "los_side": "+x (towards the mast)", "nlos_side": "-x (behind the pillars)",
        "shapes": [{"id": object_id, "material": material} for object_id, material in shapes],
    }
    (HERE / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {HERE / 'scene.xml'} with {len(shapes)} shapes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
