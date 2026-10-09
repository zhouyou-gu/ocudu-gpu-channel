#!/usr/bin/env python3
"""Dependency-free tests for the Sionna CIR → runtime TDL adapter."""

from __future__ import annotations

import cmath
import math
import pathlib
import sys
import tempfile
import unittest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "apps" / "sionna_bridge"))

from channel_adapter import (  # noqa: E402
    LaneProfile,
    MatrixProfile,
    Ray,
    Tap,
    ZmqControlClient,
    channel_status,
    control_link_id,
    make_profile_swap,
    make_matrix_profile_swap,
    rays_to_taps,
)
from run_bridge import (  # noqa: E402
    CAR_SPEED_MPS,
    CROSSTALK_LINKS,
    DEFAULT_DOWNLINK_FREQUENCY_HZ,
    DEFAULT_GNB_HEIGHT_M,
    DEFAULT_ROAD_WIDTH_M,
    DEFAULT_UPLINK_FREQUENCY_HZ,
    DOWNLINK_LINKS,
    LINKS,
    MODEL_ID,
    Motion,
    ONE_GNB_ONE_UE_CROSSTALK_LINKS,
    ONE_GNB_ONE_UE_DOWNLINK_LINKS,
    ONE_GNB_ONE_UE_LINKS,
    ONE_GNB_ONE_UE_NODE_IDS,
    ONE_GNB_ONE_UE_UPLINK_LINKS,
    PEDESTRIAN_SPEED_MPS,
    SionnaScenario,
    UPLINK_LINKS,
    configured_motion,
    effective_scenario,
    link_layout,
    load_scenario_config,
    parse_args,
    prepare_scene_with_simple_road,
    scenario_environment,
    path_polylines,
    path_slice,
    SCENE_ALIASES,
    resolve_scene,
    scene_geometry,
    scene_mesh,
    solver_settings,
    scene_mesh_path,
    write_rectangle_ply,
)


class AdapterTests(unittest.TestCase):
    def test_control_timeout_names_endpoint_and_missing_broker(self) -> None:
        class FakeAgain(Exception):
            pass

        class FakeZmq:
            Again = FakeAgain

        class FakeSocket:
            def send_string(self, _payload: str) -> None:
                return

            def recv_string(self) -> str:
                raise FakeAgain()

        client = object.__new__(ZmqControlClient)
        client._zmq = FakeZmq()  # type: ignore[attr-defined]
        client._socket = FakeSocket()  # type: ignore[attr-defined]
        client._endpoint = "tcp://127.0.0.1:5559"  # type: ignore[attr-defined]
        client._timeout_ms = 5000  # type: ignore[attr-defined]
        with self.assertRaisesRegex(
            RuntimeError,
            "control request 'batch_begin' timed out.*start ocudu-gpu-channel",
        ):
            client.request({"type": "batch_begin", "id": "sionna-0"})

    def test_two_gnb_two_ue_graph_and_horizontal_motion(self) -> None:
        self.assertEqual(len(LINKS), 8)
        self.assertEqual(len(set(LINKS)), 8)
        self.assertEqual(len(DOWNLINK_LINKS), 4)
        self.assertEqual(len(UPLINK_LINKS), 4)
        self.assertTrue(all(source.startswith("gnb") for source, _ in DOWNLINK_LINKS))
        self.assertTrue(all(source.startswith("ue") for source, _ in UPLINK_LINKS))
        self.assertEqual(CROSSTALK_LINKS, ())  # FDD: no UE<->UE edge by default
        self.assertEqual(MODEL_ID, "sionna_rt")
        self.assertEqual(
            Motion((1.0, 2.0, 3.0), (0.5, 0.0, 0.0)).position_at(4.0),
            (3.0, 2.0, 3.0),
        )

    def test_one_gnb_one_ue_layout_has_only_bidirectional_serving_links(self) -> None:
        args = parse_args(["--layout", "1x1"])
        self.assertEqual(
            link_layout(args.layout),
            (
                ONE_GNB_ONE_UE_NODE_IDS,
                ONE_GNB_ONE_UE_DOWNLINK_LINKS,
                ONE_GNB_ONE_UE_UPLINK_LINKS,
                ONE_GNB_ONE_UE_CROSSTALK_LINKS,
                ONE_GNB_ONE_UE_LINKS,
            ),
        )
        self.assertEqual(set(configured_motion(args)), {"gnb0", "ue0"})
        environment = scenario_environment(args)
        self.assertEqual(environment["layout"], "1x1")
        self.assertEqual(environment["link_count"], 2)
        self.assertEqual(
            environment["link_groups"],
            {"downlink": 1, "uplink": 1, "ue_crosstalk": 0},
        )
        self.assertEqual(set(environment["nodes"]), {"gnb0", "ue0"})

    def test_car_and_pedestrian_defaults_follow_bounded_road_routes(self) -> None:
        args = parse_args([])
        self.assertAlmostEqual(args.ue0_velocity[0], CAR_SPEED_MPS)
        self.assertAlmostEqual(abs(args.ue1_velocity[0]), PEDESTRIAN_SPEED_MPS)
        self.assertEqual(args.road_width_m, DEFAULT_ROAD_WIDTH_M)
        self.assertEqual(args.gnb_height_m, DEFAULT_GNB_HEIGHT_M)
        car = Motion((0.0, 0.0, 1.5), (10.0, 0.0, 0.0), (-1.0, 1.0), "car")
        self.assertAlmostEqual(car.position_at(0.15)[0], 0.5)
        self.assertAlmostEqual(car.velocity_at(0.15)[0], -10.0)

    def test_band3_fdd_frequencies_are_direction_specific(self) -> None:
        args = parse_args([])
        self.assertEqual(args.downlink_frequency_hz, DEFAULT_DOWNLINK_FREQUENCY_HZ)
        self.assertEqual(args.uplink_frequency_hz, DEFAULT_UPLINK_FREQUENCY_HZ)
        self.assertNotEqual(args.downlink_frequency_hz, args.uplink_frequency_hz)

    def test_common_carrier_override_preserves_tdd_compatibility(self) -> None:
        args = parse_args(["--carrier-frequency-hz", "3500000000"])
        self.assertEqual(args.downlink_frequency_hz, 3_500_000_000.0)
        self.assertEqual(args.uplink_frequency_hz, 3_500_000_000.0)

    def test_environment_record_exposes_effective_solver_and_motion(self) -> None:
        args = parse_args(["--max-depth", "4", "--update-hz", "5"])
        environment = scenario_environment(args)
        self.assertEqual(environment["scene"], "sionna_simple_test")
        self.assertEqual(environment["solver"]["name"], "PathSolver")
        self.assertEqual(environment["solver"]["max_depth"], 4)
        self.assertTrue(environment["solver"]["propagation"]["los"])
        self.assertFalse(
            environment["solver"]["propagation"]["diffuse_reflection"]
        )
        self.assertEqual(environment["update_rate_hz"], 5.0)
        self.assertEqual(environment["nodes"]["gnb0"]["velocity_mps"], (0.0, 0.0, 0.0))
        self.assertEqual(environment["nodes"]["ue0"]["mobility"], "car")
        self.assertEqual(environment["nodes"]["ue1"]["mobility"], "pedestrian")
        self.assertEqual(environment["link_count"], 8)
        self.assertEqual(environment["nodes"]["gnb0"]["start_m"][2], 60.0)

    def test_floor_is_split_into_ui_visible_road_and_ground(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "source"
            output = root / "generated"
            source.mkdir()
            output.mkdir()
            write_rectangle_ply(
                source / "floor.ply",
                x_min=-10.0,
                x_max=10.0,
                y_min=-6.0,
                y_max=6.0,
                z=0.0,
            )
            write_rectangle_ply(
                source / "building_1.ply",
                x_min=-8.0,
                x_max=-5.0,
                y_min=2.0,
                y_max=5.0,
                z=2.0,
            )
            (source / "scene.xml").write_text(
                """<scene version="2.1.0">
<bsdf type="itu-radio-material" id="concrete"><string name="type" value="concrete"/></bsdf>
<shape type="ply" id="mesh-building_1"><string name="filename" value="building_1.ply"/><ref id="concrete" name="bsdf"/></shape>
<shape type="ply" id="mesh-floor"><string name="filename" value="floor.ply"/><ref id="concrete" name="bsdf"/></shape>
</scene>""",
                encoding="utf-8",
            )
            generated = prepare_scene_with_simple_road(
                source / "scene.xml", output, 4.0
            )
            geometry = scene_geometry(generated)
            objects = {item["id"]: item for item in geometry["objects"]}
            self.assertNotIn("floor", objects)
            self.assertEqual(objects["road"]["kind"], "road")
            self.assertEqual(
                objects["road"]["footprint_xy_m"],
                [[-10.0, -2.0], [10.0, -2.0], [10.0, 2.0], [-10.0, 2.0]],
            )
            self.assertIn("ground-south", objects)
            self.assertIn("ground-north", objects)
            self.assertEqual(objects["building_1"]["kind"], "building")

    def test_physical_path_conversion(self) -> None:
        coefficient = 0.1 * cmath.exp(1j * 0.75)
        taps = rays_to_taps(
            [Ray(delay_seconds=2.5 / 20_000_000.0, coefficient=coefficient)],
            sample_rate_hz=20_000_000.0,
            gain_offset_db=3.0,
        )
        self.assertEqual(len(taps), 1)
        self.assertAlmostEqual(taps[0].delay_samples, 2.5)
        self.assertAlmostEqual(taps[0].gain_db, -17.0)
        self.assertAlmostEqual(taps[0].phase_rad, 0.75)

    def test_same_delay_bin_is_merged_coherently(self) -> None:
        sample_rate = 16.0
        rays = [
            Ray(1.01 / sample_rate, 1.0 + 0.0j),
            Ray(1.02 / sample_rate, 0.0 + 1.0j),
        ]
        taps = rays_to_taps(rays, sample_rate_hz=sample_rate)
        self.assertEqual(len(taps), 1)
        self.assertAlmostEqual(taps[0].delay_samples, 1.0)
        self.assertAlmostEqual(taps[0].gain_db, 20.0 * math.log10(math.sqrt(2.0)))
        self.assertAlmostEqual(taps[0].phase_rad, math.pi / 4.0)

    def test_strongest_bins_are_capped_and_delay_sorted(self) -> None:
        rays = [Ray(float(index), complex(index + 1, 0.0)) for index in range(5)]
        taps = rays_to_taps(
            rays,
            sample_rate_hz=1.0,
            max_taps=2,
            max_gain_db=20.0,
        )
        self.assertEqual([tap.delay_samples for tap in taps], [3.0, 4.0])

    def test_invalid_or_missing_paths_become_quiet_outage(self) -> None:
        taps = rays_to_taps(
            [Ray(float("nan"), 1.0 + 0.0j), Ray(0.0, 0.0j)],
            sample_rate_hz=23_040_000.0,
        )
        self.assertEqual(taps, [Tap(0.0, -100.0, 0.0)])

    def test_profile_contract_and_canonical_link_id(self) -> None:
        link_id = control_link_id("gnb0", "ue1", "sionna_rt")
        self.assertEqual(link_id, "gnb0>ue1:sionna_rt")
        message = make_profile_swap(
            link_id,
            [Tap(1.5, -12.0, 0.25)],
            batch_id="sionna-7",
        )
        self.assertEqual(message["type"], "profile_swap")
        self.assertEqual(message["batch_id"], "sionna-7")
        self.assertEqual(message["taps"][0]["delay_samples"], 1.5)
        self.assertFalse(message["fading"]["enabled"])
        self.assertNotIn("scene_geometry", message)

    def test_matrix_profile_contract_covers_every_lane_in_row_major_order(self) -> None:
        profile = MatrixProfile(
            nt=2,
            nr=2,
            lanes=(
                LaneProfile(1, 1, (Tap(0.0, -4.0, 0.4),)),
                LaneProfile(0, 1, (Tap(0.0, -2.0, 0.2),)),
                LaneProfile(1, 0, (Tap(0.0, -3.0, 0.3),)),
                LaneProfile(0, 0, (Tap(0.0, -1.0, 0.1),)),
            ),
        )
        message = make_matrix_profile_swap(
            "gnb0>ue0:desired", profile, batch_id="sionna-9"
        )
        self.assertEqual(message["type"], "matrix_profile_swap")
        self.assertEqual((message["nr"], message["nt"]), (2, 2))
        self.assertEqual(
            [(lane["rx_port"], lane["tx_port"]) for lane in message["lanes"]],
            [(0, 0), (0, 1), (1, 0), (1, 1)],
        )

    def test_scenario_config_drives_nodes_links_and_array_dimensions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "scenario.json"
            path.write_text(
                """{
  "name":"dynamic-miso",
  "nodes":{
    "gnb":{"start_m":[0,0,10],"tx_array":{"rows":2,"cols":2}},
    "ue":{"start_m":[10,0,1.5],"rx_array":{"rows":1,"cols":2}}
  },
  "links":[{"from":"gnb","to":"ue","direction":"downlink","model":"h"}]
}""",
                encoding="utf-8",
            )
            definition = load_scenario_config(path)
            self.assertEqual(definition.nodes["gnb"].tx_array.antenna_count, 4)
            self.assertEqual(definition.nodes["ue"].rx_array.antenna_count, 2)
            args = parse_args(["--scenario-config", str(path)])
            self.assertEqual(effective_scenario(args).name, "dynamic-miso")
            environment = scenario_environment(args)
            self.assertEqual(environment["matrix_lane_count"], 8)
            self.assertEqual(environment["nodes"]["gnb"]["tx_antennas"], 4)

    def test_channel_status_exposes_the_exact_ui_tap_values(self) -> None:
        taps = [Tap(2.5, -12.0, math.pi / 2.0)]
        status = channel_status(
            "gnb0>ue0:sionna_rt",
            [Ray(2.5 / 20_000_000.0, 0.25 + 0.0j, -37.5)],
            taps,
            sample_rate_hz=20_000_000.0,
        )
        self.assertEqual(status["tap_count"], 1)
        self.assertEqual(status["taps"][0]["delay_samples"], 2.5)
        self.assertAlmostEqual(status["taps"][0]["delay_ns"], 125.0)
        self.assertEqual(status["taps"][0]["gain_db"], -12.0)
        self.assertAlmostEqual(status["taps"][0]["phase_deg"], 90.0)
        self.assertEqual(status["delay_doppler_point_count"], 1)
        point = status["delay_doppler_points"][0]
        self.assertAlmostEqual(point["delay_samples"], 2.5)
        self.assertAlmostEqual(point["delay_ns"], 125.0)
        self.assertAlmostEqual(point["doppler_hz"], -37.5)
        self.assertAlmostEqual(point["power_db"], 20.0 * math.log10(0.25))

    def test_miso_profiles_broadcast_sionna_path_doppler_to_each_lane(self) -> None:
        class FakeTensor:
            def __init__(self, ndim: int, values: object) -> None:
                self.ndim = ndim
                self.values = values

            def __getitem__(self, key: tuple[object, ...]) -> object:
                if self.ndim == 6:
                    return self.values[key[3]]  # type: ignore[index]
                return self.values

        class FakeNumpy:
            @staticmethod
            def asarray(value: object) -> object:
                return value

        class FakePaths:
            doppler = FakeTensor(3, [-25.0, 40.0])
            valid = FakeTensor(3, [True, True])

            @staticmethod
            def cir(**_kwargs: object) -> tuple[FakeTensor, FakeTensor]:
                return (
                    FakeTensor(
                        6,
                        {
                            0: [0.25 + 0.0j, 0.125 + 0.0j],
                            1: [0.5 + 0.0j, 0.0625 + 0.0j],
                        },
                    ),
                    FakeTensor(3, [1.0e-7, 2.0e-7]),
                )

        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "miso.json"
            path.write_text(
                """{
  "name":"miso",
  "nodes":{
    "gnb":{"start_m":[0,0,10],"tx_array":{"rows":1,"cols":2}},
    "ue":{"start_m":[10,0,1.5]}
  },
  "links":[{"from":"gnb","to":"ue","direction":"downlink","model":"h"}]
}""",
                encoding="utf-8",
            )
            args = parse_args(["--scenario-config", str(path)])
            scenario = object.__new__(SionnaScenario)
            scenario.args = args
            scenario.np = FakeNumpy()
            scenario.definition = effective_scenario(args)
            scenario.tx_indices = {"gnb": 0}
            scenario.rx_indices = {"ue": 0}
            link = scenario.definition.links[0]
            profiles, statuses = scenario.profiles(
                FakePaths(),
                [link],
                direction="downlink",
                carrier_frequency_hz=args.downlink_frequency_hz,
            )
            timing = {}
            timed_profiles, timed_statuses = scenario.profiles(
                FakePaths(), [link], direction="downlink",
                carrier_frequency_hz=args.downlink_frequency_hz, timing=timing,
            )
            self.assertEqual(profiles, timed_profiles)
            self.assertEqual(statuses, timed_statuses)
            self.assertEqual(set(timing), {
                "cir_numpy", "array_export", "geometry_export",
                "tap_and_status_pack", "path_polylines",
            })
            self.assertTrue(all(value >= 0 for value in timing.values()))
            self.assertEqual(timing["path_polylines"], 0.0)

            class BrokenGeometryPaths(FakePaths):
                vertex_reads = 0

                @property
                def vertices(self) -> object:
                    self.vertex_reads += 1
                    raise RuntimeError("geometry component build failed")

            args.path_polylines = 6
            # Missing optional geometry remains supported without disabling it.
            scenario.profiles(
                FakePaths(), [link], direction="downlink",
                carrier_frequency_hz=args.downlink_frequency_hz,
            )
            self.assertIsNone(scenario.path_polylines_error)
            broken_paths = BrokenGeometryPaths()
            for _ in range(2):
                fallback_profiles, fallback_statuses = scenario.profiles(
                    broken_paths, [link], direction="downlink",
                    carrier_frequency_hz=args.downlink_frequency_hz,
                )
                self.assertEqual(profiles, fallback_profiles)
                self.assertEqual(statuses, fallback_statuses)
            self.assertEqual(
                scenario.path_polylines_error,
                "RuntimeError: geometry component build failed",
            )
            self.assertEqual(broken_paths.vertex_reads, 1)

        profile = profiles["gnb>ue:h"]
        self.assertEqual((profile.nr, profile.nt), (1, 2))
        self.assertEqual(len(statuses[0]["lanes"]), 2)
        for lane in statuses[0]["lanes"]:
            self.assertEqual(
                [point["doppler_hz"] for point in lane["delay_doppler_points"]],
                [-25.0, 40.0],
            )


    def test_scene_mesh_carries_real_triangles_not_bounding_boxes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            write_rectangle_ply(
                root / "floor.ply",
                x_min=-10.0, x_max=10.0, y_min=-6.0, y_max=6.0, z=0.0,
            )
            (root / "scene.xml").write_text(
                """<scene version="2.1.0">
<bsdf type="itu-radio-material" id="concrete"><string name="type" value="concrete"/></bsdf>
<shape type="ply" id="mesh-floor"><string name="filename" value="floor.ply"/><ref id="concrete" name="bsdf"/></shape>
</scene>""",
                encoding="utf-8",
            )
            mesh = scene_mesh(root / "scene.xml")

        floor = mesh["objects"][0]
        self.assertEqual(floor["id"], "floor")
        self.assertEqual(floor["kind"], "ground")
        self.assertEqual(floor["material"], "concrete")
        self.assertEqual(len(floor["positions"]), 12)   # four XYZ corners
        self.assertEqual(floor["indices"], [0, 1, 2, 0, 2, 3])

    def test_scene_mesh_path_matches_the_web_ui_side(self) -> None:
        # The bridge writes the sidecar and the web UI reads it; both derive
        # the name from --status-jsonl, so the two must not drift apart.
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "apps" / "dashboard"))
        try:
            import server as web_ui_server
        finally:
            sys.path.pop(0)
        status = pathlib.Path("/tmp/results/sionna-2gnb-2ue.jsonl")
        self.assertEqual(
            scene_mesh_path(status), web_ui_server.scene_mesh_path(status)
        )
        self.assertIsNone(scene_mesh_path(None))

    def test_path_slice_accepts_both_synthetic_and_per_antenna_layouts(self) -> None:
        class Tensor:
            def __init__(self, ndim: int, value: object) -> None:
                self.ndim = ndim
                self.value = value

            def __getitem__(self, key: tuple[object, ...]) -> object:
                return (self.ndim, key, self.value)

        synthetic = Tensor(5, "vertices")
        self.assertEqual(
            path_slice(synthetic, 1, 2, trailing=1)[1],
            (slice(None), 1, 2),
        )
        per_antenna = Tensor(7, "vertices")
        self.assertEqual(
            path_slice(per_antenna, 1, 2, trailing=1)[1],
            (slice(None), 1, 0, 2, 0),
        )
        with self.assertRaises(RuntimeError):
            path_slice(Tensor(3, "x"), 0, 0, trailing=1)

    def test_path_polylines_keep_the_strongest_rays_and_close_both_ends(self) -> None:
        # Two paths: a line of sight and a single specular bounce. Depth 1 of
        # the LoS path is padded with InteractionType.NONE and must not add a
        # vertex to its polyline.
        vertices = [
            [[0.0, 0.0, 0.0], [5.0, 6.0, 7.0]],
            [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0]],
        ]
        interactions = [[0, 1], [0, 0]]

        class Grid:
            def __init__(self, values: list[list[object]]) -> None:
                self.values = values
                self.shape = (len(values), len(values[0]), 3)

            def __getitem__(self, key: tuple[int, int]) -> object:
                return self.values[key[0]][key[1]]

        lines = path_polylines(
            Grid(vertices),
            Grid(interactions),
            source_position=(1.0, 2.0, 30.0),
            destination_position=(40.0, 0.0, 1.5),
            gains_db=[-70.0, -90.0, None],
            limit=5,
        )
        self.assertEqual([line["bounces"] for line in lines], [0, 1])
        self.assertEqual([line["gain_db"] for line in lines], [-70.0, -90.0])
        self.assertEqual(lines[0]["points"], [[1.0, 2.0, 30.0], [40.0, 0.0, 1.5]])
        self.assertEqual(
            lines[1]["points"],
            [[1.0, 2.0, 30.0], [5.0, 6.0, 7.0], [40.0, 0.0, 1.5]],
        )
        self.assertEqual(lines[1]["interactions"], ["specular"])
        # An invalid path contributes no line even though the tensor has room.
        self.assertEqual(len(lines), 2)

        strongest = path_polylines(
            Grid(vertices),
            Grid(interactions),
            source_position=(1.0, 2.0, 30.0),
            destination_position=(40.0, 0.0, 1.5),
            gains_db=[-70.0, -90.0],
            limit=1,
        )
        self.assertEqual([line["gain_db"] for line in strongest], [-70.0])


    def test_loop_route_walks_the_polyline_and_closes_the_ring(self) -> None:
        # A 12 m square walked at 1 m/s: the lap takes 48 s, so the quarter
        # points are the corners and the ring must close back to the start.
        square = [(0.0, 0.0, 1.5), (12.0, 0.0, 1.5), (12.0, 12.0, 1.5), (0.0, 12.0, 1.5)]
        motion = Motion(
            square[0], (0.0, 0.0, 0.0), None, "pedestrian",
            tuple(square), "loop", 1.0,
        )
        for elapsed, expected in (
            (0.0, (0.0, 0.0)), (6.0, (6.0, 0.0)), (12.0, (12.0, 0.0)),
            (24.0, (12.0, 12.0)), (36.0, (0.0, 12.0)), (48.0, (0.0, 0.0)),
        ):
            with self.subTest(elapsed=elapsed):
                position = motion.position_at(elapsed)
                self.assertAlmostEqual(position[0], expected[0], places=6)
                self.assertAlmostEqual(position[1], expected[1], places=6)
                self.assertAlmostEqual(position[2], 1.5, places=6)
        # A lap later the walker is back where it started, still going.
        self.assertAlmostEqual(motion.position_at(54.0)[0], motion.position_at(6.0)[0])
        self.assertEqual(motion.velocity_at(6.0), (1.0, 0.0, 0.0))
        self.assertEqual(motion.velocity_at(18.0), (0.0, 1.0, 0.0))

    def test_pingpong_route_turns_round_at_the_far_end(self) -> None:
        motion = Motion(
            (0.0, 0.0, 1.5), (0.0, 0.0, 0.0), None, "car",
            ((0.0, 0.0, 1.5), (10.0, 0.0, 1.5)), "pingpong", 2.0,
        )
        self.assertAlmostEqual(motion.position_at(2.5)[0], 5.0)
        self.assertAlmostEqual(motion.position_at(5.0)[0], 10.0)
        self.assertAlmostEqual(motion.position_at(7.5)[0], 5.0)   # coming back
        self.assertAlmostEqual(motion.position_at(10.0)[0], 0.0)
        self.assertEqual(motion.velocity_at(2.5), (2.0, 0.0, 0.0))
        self.assertEqual(motion.velocity_at(7.5), (-2.0, 0.0, 0.0))

    def test_route_config_round_trips_and_rejects_contradictions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "route.json"

            def write(node: str) -> None:
                path.write_text(
                    '{"name":"r","nodes":{"gnb":{"start_m":[0,0,10]},"ue":'
                    + node
                    + '},"links":[{"from":"gnb","to":"ue","direction":"downlink"}]}',
                    encoding="utf-8",
                )

            write('{"route_m":[[0,0,1.5],[5,0,1.5]],"route_mode":"loop","speed_mps":1.4}')
            definition = load_scenario_config(path)
            motion = definition.nodes["ue"].motion
            self.assertEqual(motion.route_mode, "loop")
            self.assertEqual(motion.speed_mps, 1.4)
            # start_m is optional on a routed node: the route supplies it.
            self.assertEqual(motion.start, (0.0, 0.0, 1.5))

            write('{"route_m":[[0,0,1.5],[5,0,1.5]],"route_mode":"loop"}')
            with self.assertRaisesRegex(ValueError, "speed_mps"):
                load_scenario_config(path)
            write('{"route_m":[[0,0,1.5],[5,0,1.5]],"speed_mps":1.4,"route_x_m":[0,5]}')
            with self.assertRaisesRegex(ValueError, "route_m and route_x_m"):
                load_scenario_config(path)
            write('{"route_m":[[0,0,1.5]],"speed_mps":1.4}')
            with self.assertRaisesRegex(ValueError, "at least two points"):
                load_scenario_config(path)

    def test_scenario_config_can_switch_off_the_synthetic_road(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "s.json"
            path.write_text(
                '{"name":"s","scene":"sionna_SUTD_test","simple_road":false,'
                '"nodes":{"a":{"start_m":[0,0,1]},"b":{"start_m":[1,0,1]}},'
                '"links":[{"from":"a","to":"b","direction":"downlink"}]}',
                encoding="utf-8",
            )
            definition = load_scenario_config(path)
            self.assertIs(definition.simple_road, False)
            self.assertEqual(definition.scene, "sionna_SUTD_test")
            args = parse_args(["--scenario-config", str(path)])
            # A scene that ships its own ground must be able to turn the
            # floor-splitting step off, or the run aborts looking for a floor.
            self.assertFalse(args.simple_road)
            self.assertEqual(args.scene, "sionna_SUTD_test")

    def test_scene_names_resolve_to_paths_directories_and_builtins(self) -> None:
        class FakeBuiltins:
            simple_street_canyon = "/sionna/simple_street_canyon.xml"

        class FakeRt:
            scene = FakeBuiltins()

        # The demo name for the built-in canyon still lands on Sionna's file.
        self.assertEqual(SCENE_ALIASES["sionna_simple_test"], "simple_street_canyon")
        self.assertEqual(
            resolve_scene("sionna_simple_test", FakeRt()),
            pathlib.Path("/sionna/simple_street_canyon.xml"),
        )
        with tempfile.TemporaryDirectory() as directory:
            explicit = pathlib.Path(directory) / "custom.xml"
            explicit.write_text("<scene/>", encoding="utf-8")
            self.assertEqual(resolve_scene(str(explicit), FakeRt()), explicit.resolve())
        with self.assertRaisesRegex(RuntimeError, "unknown scene"):
            resolve_scene("no_such_scene", FakeRt())
        # The repository's own generated scene resolves without touching Sionna.
        generated = resolve_scene("sionna_SUTD_test", FakeRt())
        self.assertTrue(generated.is_file())
        self.assertEqual(generated.name, "scene.xml")


    def test_scenario_solver_block_pins_only_what_it_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "s.json"

            def write(solver: str) -> None:
                path.write_text(
                    '{"name":"s","solver":' + solver + ','
                    '"nodes":{"gnb0":{"start_m":[0,0,10]},"ue0":{"start_m":[1,0,1]}},'
                    '"links":[{"from":"gnb0","to":"ue0","direction":"downlink"}]}',
                    encoding="utf-8",
                )

            defaults = parse_args([])
            write('{"max_depth":5,"propagation":{"diffraction":true}}')
            args = parse_args(["--scenario-config", str(path)])
            self.assertEqual(args.max_depth, 5)
            self.assertTrue(args.diffraction)
            # Untouched keys keep the command-line defaults, so a scenario
            # pins what it means to pin and nothing else.
            self.assertEqual(args.samples_per_src, defaults.samples_per_src)
            self.assertEqual(args.seed, defaults.seed)
            self.assertTrue(args.los)
            self.assertFalse(args.refraction)

            for bad, expected in (
                ('{"max_depth":0}', "must be positive"),
                ('{"max_depth":"3"}', "non-negative integer"),
                ('{"propagation":{"los":"yes"}}', "must be true or false"),
                ('{"propagation":{"reflection":true}}', "unknown solver.propagation"),
                ('{"depth":3}', "unknown solver keys"),
                ('[]', "must be an object"),
            ):
                write(bad)
                with self.assertRaisesRegex(ValueError, expected):
                    load_scenario_config(path)

    def test_solver_settings_map_to_argparse_destinations(self) -> None:
        self.assertIsNone(solver_settings(None))
        self.assertEqual(
            solver_settings({"samples_per_source": 1000, "path_polylines": 0}),
            {"samples_per_src": 1000, "path_polylines": 0},
        )

    def test_reference_single_cell_example_is_self_contained(self) -> None:
        # The 1 gNB / 1 UE example has to reproduce its own trace settings,
        # not inherit whatever the caller happened to type.
        path = (
            pathlib.Path(__file__).resolve().parents[1]
            / "use_cases" / "configs" / "sionna" / "scenarios" / "sutd" / "ocudu-rank1-sutd.json"
        )
        definition = load_scenario_config(path)
        self.assertEqual(sorted(definition.nodes), ["gnb0", "ue0"])
        self.assertEqual(len(definition.links), 2)
        self.assertEqual(definition.scene, "sionna_SUTD_test")
        self.assertIs(definition.simple_road, False)
        self.assertEqual(definition.nodes["gnb0"].tx_array.antenna_count, 4)
        self.assertEqual(definition.nodes["ue0"].rx_array.antenna_count, 1)
        self.assertEqual(definition.nodes["ue0"].motion.route_mode, "pingpong")
        args = parse_args(["--scenario-config", str(path)])
        self.assertEqual(args.max_depth, 3)
        self.assertEqual(args.samples_per_src, 200_000)
        self.assertEqual(args.path_polylines, 6)
        self.assertTrue(args.los and args.specular_reflection)
        self.assertFalse(args.diffuse_reflection or args.refraction or args.diffraction)


if __name__ == "__main__":
    unittest.main()
