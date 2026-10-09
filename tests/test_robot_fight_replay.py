"""render_replay.py: frames from a synthetic run, one- and two-cell layouts."""

import pathlib
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "use_cases" / "robot_fight"))

try:
    import matplotlib  # noqa: F401
    import numpy  # noqa: F401
except ImportError:  # pragma: no cover
    matplotlib = None

if matplotlib is not None:
    import render_replay as rr


@unittest.skipIf(matplotlib is None, "matplotlib/numpy not installed")
class ReplayTest(unittest.TestCase):
    def test_single_cell_frames_and_summary(self):
        with tempfile.TemporaryDirectory() as td:
            root = rr.make_synthetic_run(pathlib.Path(td) / "run", seconds=4.0, two_cell=False, bot="balance")
            run = rr.load_run(root)
            self.assertEqual(len(run.fights), 1)
            f = run.fights[0]
            self.assertEqual(f.bot, "balance")
            self.assertEqual(f.reason, "fall")
            self.assertEqual(len(f.stale.get(1, [])), 1)
            self.assertEqual([r.side for r in run.robots], ["A", "B"])
            self.assertIn("default", run.brokers)
            self.assertTrue(run.brokers["default"]["has_starv"])
            self.assertEqual(len(run.ue_metrics["ue0"]), 30)  # `#eof` row skipped
            self.assertEqual([k for _, k in run.ue_events["ue1"]], ["out_of_sync"])
            # 2 s at 25 fps = 50 frames, 1280x720 RGB
            frames = list(rr.iter_frames(run, run.fights, fps=25, speed=1.0, hold_s=0.5, max_seconds=2.0))
            self.assertEqual(len(frames), 50)
            self.assertEqual(frames[0][2].shape, (720, 1280, 3))
            self.assertEqual(frames[-1][1], 49 / 25)
            # frames differ (the robots move)
            self.assertFalse((frames[0][2] == frames[-1][2]).all())
            out = pathlib.Path(td) / "summary.png"
            rr.summary_png(run, out)
            self.assertGreater(out.stat().st_size, 10_000)

    def test_two_cell_layout_protected_is_side_a(self):
        with tempfile.TemporaryDirectory() as td:
            root = rr.make_synthetic_run(pathlib.Path(td) / "run", seconds=2.0, two_cell=True)
            run = rr.load_run(root)
            self.assertEqual(sorted(run.brokers), ["a", "b"])
            self.assertEqual(run.robots[0].sched, "protected")
            self.assertEqual(run.robots[1].sched, "plain")
            self.assertEqual(run.robots[0].side, "A")
            self.assertIsNotNone(run.contention_start_ms)
            self.assertIn("hog 200", rr.contention_label(run.params))
            frames = list(rr.iter_frames(run, run.fights, fps=10, speed=2.0, hold_s=0.0, max_seconds=1.0))
            self.assertEqual(len(frames), 10)
            self.assertEqual(frames[-1][1], 9 * 2.0 / 10)

    def test_3d_frames_osmesa(self):
        import os
        os.environ.setdefault("MUJOCO_GL", "osmesa")
        try:
            import mujoco  # noqa: F401
            probe = mujoco.Renderer(mujoco.MjModel.from_xml_string(
                '<mujoco><worldbody><geom type="box" size="1 1 1"/></worldbody></mujoco>'), 16, 16)
            probe.close()
        except Exception as exc:  # noqa: BLE001  (no OSMesa/EGL on this host)
            self.skipTest(f"MuJoCo offscreen rendering unavailable: {exc}")
        with tempfile.TemporaryDirectory() as td:
            root = rr.make_synthetic_run(pathlib.Path(td) / "run", seconds=2.0, two_cell=True, bot="balance")
            run = rr.load_run(root)
            f = run.fights[0]
            scene = rr.Scene3D(run, f)
            raw = scene.render(f.t0_ms + 500, f.poses[10][1])
            self.assertEqual(raw.shape, (480, 640, 3))
            self.assertGreater(raw.std(), 10)  # not a blank frame
            scene.close()
            frames = list(rr.iter_frames(run, run.fights, fps=10, speed=1.0, hold_s=0.0, max_seconds=1.0, view="both"))
            self.assertEqual(len(frames), 10)
            self.assertEqual(frames[0][2].shape, (720, 1280, 3))
            frames3 = list(rr.iter_frames(run, run.fights, fps=10, speed=1.0, hold_s=0.0, max_seconds=0.3, view="3d"))
            self.assertEqual(len(frames3), 3)

    def test_offset_parsing(self):
        self.assertEqual(rr.parse_offset({}, "2.0,0,0"), (2.0, 0.0, 0.0))
        self.assertEqual(rr.parse_offset({"root_exec": "RF_OFFSET=1,2,3 bash x"}, None), (1.0, 2.0, 3.0))
        self.assertEqual(rr.parse_offset({}, None), (0.0, 0.0, 0.0))


if __name__ == "__main__":
    unittest.main()
