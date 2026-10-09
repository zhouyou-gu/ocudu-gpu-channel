#!/usr/bin/env python3
"""Dependency-free tests for the read-only Sionna status web service."""

from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "apps" / "dashboard"))

from server import (  # noqa: E402
    DEFAULT_HISTORY_WINDOW_MS,
    GNB_UE_REPORT_STALE_SECONDS,
    GPU_HISTORY_RETAIN_MS,
    HARDWARE_REFRESH_INTERVAL_SECONDS,
    NvmlResourceSampler,
    PCIE_REFRESH_INTERVAL_SECONDS,
    PROCESS_UTILIZATION_INTERVAL_SECONDS,
    RESOURCE_SAMPLE_INTERVAL_SECONDS,
    SIONNA_JSONL_POLL_SECONDS,
    TELEMETRY_HISTORY_RETAIN_MS,
    SionnaJsonlTail,
    StatusStore,
    _NvmlProcessInfo,
    _NvmlProcessUtilizationSample,
    _NvmlUtilization,
    build_resource_payload,
    delivery_status as raw_delivery_status,
    parse_cpu_hardware,
    parse_nvidia_compute_apps,
    parse_nvidia_dmon_pcie,
    parse_nvidia_gpu_inventory,
    parse_nvidia_gpu_map,
    parse_nvidia_pmon,
    parse_proc_cpu_total,
    parse_proc_meminfo,
    parse_proc_pid_ticks,
    parse_proc_status_rss,
    parse_telemetry_frame,
    sample_gpu_usage,
    slice_history,
    trim_history,
    extract_ue_rows,
    is_scheduler_report,
    parse_ws_endpoint,
    parse_args,
    gnb_metrics_loop,
)


def delivery_status(sionna, telemetry):
    # These direct correlation fixtures represent frames just received.
    return raw_delivery_status(sionna, telemetry, {key: 0.0 for key in telemetry})


class WebUiTests(unittest.TestCase):
    def test_delivery_requires_fresh_telemetry(self):
        with mock.patch('server.time.monotonic', return_value=100.0) as clock:
            store = StatusStore()
            record = {'event':'sionna_rt_update','channels':[{'link_id':'a'},{'link_id':'b'}],
                      'control_reply':{'ok':True,'backend':'cuda','link_count':2,
                                       'links':[{'link_id':k,'seqno':4} for k in ('a','b')]}}
            store.update_sionna(record)
            frame = {'seqno':4,'slot':10,'backend':'cuda','profile_active':True,'warmup_until_slot':0}
            for k in ('a','b'): store.update_telemetry(k, dict(frame))
            self.assertTrue(store.snapshot()['delivery']['backend_usable'])
            clock.return_value = 102.0
            self.assertTrue(store.snapshot()['delivery']['backend_usable'])
            clock.return_value = 102.0001
            stale = store.snapshot()
            self.assertFalse(stale['delivery']['backend_usable'])
            self.assertFalse(stale['feeds']['telemetry_connected'])
            self.assertEqual(stale['delivery']['state'], 'stale_backend_telemetry')
            self.assertTrue(stale['delivery']['control_received'])
            self.assertEqual(len(stale['telemetry']), 2)
            store.update_telemetry('a', dict(frame))
            partial = store.snapshot()
            self.assertEqual(partial['delivery']['backend_usable_links'], 1)
            self.assertFalse(partial['delivery']['backend_usable'])
            store.update_telemetry('b', dict(frame))
            self.assertTrue(store.snapshot()['delivery']['backend_usable'])
            # A cached record without a receipt timestamp is not evidence of liveness.
            self.assertFalse(raw_delivery_status(record, {'a':frame,'b':frame})['backend_usable'])
            store._telemetry_seen.pop('b')
            self.assertFalse(store.snapshot()['delivery']['backend_usable'])

    def test_preserved_matrix_history_has_no_ack_invented_warmup(self) -> None:
        record = {
            "event": "sionna_rt_update", "session_id": "preserve", "iteration": 1,
            "control_ack_unix_ms": 1000,
            "channels": [{"link_id": "link"}],
            "control_reply": {
                "ok": True, "backend": "cuda", "link_count": 1,
                "links": [{"link_id": "link", "seqno": 4, "warmup_until_slot": 99}],
            },
        }
        telemetry = {
            "seqno": 4, "slot": 10, "profile_active": True, "backend": "cuda",
            "warmup_until_slot": 0,
            # Latched boundaries describe an earlier reset, not this update.
            "warmup_event_seq": 1, "warmup_profile_seqno": 3,
            "warmup_begin_unix_ns": 800_000_000, "warmup_end_unix_ns": 900_000_000,
        }
        self.assertTrue(delivery_status(record, {"link": telemetry})["backend_usable"])
        for telemetry_first in (False, True):
            with self.subTest(telemetry_first=telemetry_first):
                store = StatusStore()
                if telemetry_first:
                    store.update_telemetry("link", telemetry)
                store.update_sionna(record)
                if not telemetry_first:
                    store.update_telemetry("link", telemetry)
                store.update_gpu_usage({"sampled_unix_ms": 1050, "processes": {}})
                event = store.snapshot()["gpu_usage"]["iterations"][-1]
                self.assertFalse(event["warmup_expected"])
                self.assertIsNone(event["warmup_started_unix_ms"])
                self.assertIsNone(event["warmup_ended_unix_ms"])

    def test_ack_alone_does_not_create_a_warmup_interval(self) -> None:
        store = StatusStore()
        store.update_sionna({
            "event": "sionna_rt_update", "session_id": "pending", "iteration": 0,
            "control_ack_unix_ms": 1000,
            "control_reply": {"links": [{"link_id": "link", "seqno": 2,
                                          "warmup_until_slot": 99}]},
        })
        store.update_gpu_usage({"sampled_unix_ms": 1050, "processes": {}})
        event = store.snapshot()["gpu_usage"]["iterations"][-1]
        self.assertIsNone(event["warmup_started_unix_ms"])
        self.assertIsNone(event["warmup_ended_unix_ms"])

    def test_mixed_reset_and_preserving_links_use_observed_reset_only(self) -> None:
        store = StatusStore()
        record = {
            "event": "sionna_rt_update", "session_id": "mixed", "iteration": 1,
            "control_ack_unix_ms": 1000,
            "control_reply": {"links": [
                {"link_id": link, "seqno": 4, "warmup_until_slot": 99}
                for link in ("preserved", "reset")
            ]},
        }
        store.update_sionna(record)
        observed = {"seqno": 4, "slot": 12, "profile_active": True,
                    "warmup_until_slot": 0, "warmup_event_seq": 2}
        store.update_telemetry("preserved", dict(observed, warmup_profile_seqno=3))
        store.update_telemetry("reset", dict(
            observed, warmup_profile_seqno=4,
            warmup_begin_unix_ns=1_010_000_000, warmup_end_unix_ns=1_020_000_000,
        ))
        store.update_gpu_usage({"sampled_unix_ms": 1050, "processes": {}})
        event = store.snapshot()["gpu_usage"]["iterations"][-1]
        self.assertEqual(event["warmup_started_unix_ms"], 1010)
        self.assertEqual(event["warmup_ended_unix_ms"], 1020)
        self.assertEqual(event["warmup_boundary_source"], "backend")

    def test_telemetry_topic_and_json_are_checked(self) -> None:
        frame = (
            'gnb0>ue0:sionna_rt '
            '{"event":"telemetry","link_id":"gnb0>ue0:sionna_rt","slot":17}'
        )
        topic, payload = parse_telemetry_frame(frame)
        self.assertEqual(topic, "gnb0>ue0:sionna_rt")
        self.assertEqual(payload["slot"], 17)
        with self.assertRaises(ValueError):
            parse_telemetry_frame(
                'gnb0>ue0:sionna_rt '
                '{"event":"telemetry","link_id":"gnb1>ue0:sionna_rt"}'
            )

    def test_store_exposes_read_only_feed_snapshot(self) -> None:
        store = StatusStore()
        store.update_telemetry(
            "ue0>gnb0:sionna_rt",
            {
                "event": "telemetry",
                "link_id": "ue0>gnb0:sionna_rt",
                "slot": 3,
                "matrix_profile_active": True,
                "array": {"nt": 2, "nr": 1},
                "slot_processing": {
                    "deadline_us": 1000.0,
                    "elapsed_us": 750.0,
                    "deadline_met": True,
                    "cumulative": {
                        "processed_slots": 1234,
                        "deadline_misses": 3,
                        "deadline_miss_percent": 0.243,
                        "max_elapsed_us": 1400.0,
                        "p95_elapsed_us": 800.0,
                        "p99_elapsed_us": 950.0,
                    },
                    "nominal": {
                        "slot_samples": 23040,
                        "pending_samples": 1,
                        "pending_estimated_us": 0.01,
                        "completed_slots": 1200,
                        "latest_estimated_us": 830.0,
                        "deadline_us": 1000.0,
                        "usage_percent": 83.0,
                        "deadline_met": True,
                        "deadline_misses": 2,
                        "deadline_miss_percent": 0.167,
                    },
                    "calls": {"processed_calls": 1234},
                    "fragments": {
                        "calls": 34,
                        "call_percent": 2.755,
                        "min_samples": 1,
                        "max_samples": 23039,
                        "fragmented_nominal_slots": 17,
                    },
                },
            },
        )
        store.update_sionna(
            {
                "event": "sionna_rt_update",
                "iteration": 2,
                "positions": {},
                "timing_ms": {
                    "channel_generation": 120.0,
                    "control_transaction": 0.7,
                    "total_update": 121.0,
                    "directions": {"downlink": 58.0, "uplink": 61.0},
                },
                "channels": [
                    {
                        "link_id": "ue0>gnb0:sionna_rt",
                        "direction": "uplink",
                        "total_path_power_db": -72.0,
                        "strongest_tap_gain_db": -72.5,
                        "ray_count": 2,
                        "tap_count": 2,
                        "taps": [{"gain_db": -72.5}],
                    }
                ],
                "scene_geometry": {
                    "objects": [{"id": "road", "kind": "road"}]
                },
            }
        )
        snapshot = store.snapshot()
        self.assertTrue(snapshot["feeds"]["telemetry_connected"])
        self.assertTrue(snapshot["feeds"]["sionna_connected"])
        self.assertEqual(snapshot["telemetry"]["ue0>gnb0:sionna_rt"]["slot"], 3)
        self.assertEqual(snapshot["sionna"]["iteration"], 2)
        self.assertEqual(
            snapshot["sionna"]["scene_geometry"]["objects"][0]["kind"],
            "road",
        )
        self.assertEqual(
            snapshot["history"]["telemetry"][-1]["slot_processing"][
                "elapsed_us"
            ],
            750.0,
        )
        self.assertEqual(
            snapshot["history"]["telemetry"][-1]["slot_processing"][
                "cumulative"
            ]["processed_slots"],
            1234,
        )
        iteration = snapshot["history"]["iterations"][-1]
        self.assertEqual(iteration["timing_ms"]["total_update"], 121.0)
        self.assertEqual(iteration["channels"][0]["total_path_power_db"], -72.0)
        self.assertNotIn("taps", iteration["channels"][0])

    def test_history_is_retained_by_age_not_by_frame_count(self) -> None:
        """500 Hz telemetry must not push the drawn window out of the buffer.

        The old fixed 256-frame cap held a different amount of *time*
        for every topology: ~256 ms for two links, ~32 ms for sixteen,
        which is shorter than the 50 ms window the chart draws once poll
        jitter is included.
        """

        entries = [
            {"observed_unix_ms": stamp, "link_id": "ue0>gnb0"}
            for stamp in range(0, 4_000, 2)  # 500 Hz for four seconds
        ]
        trim_history(entries, "observed_unix_ms", 3_998, 2_000, 16_384)
        self.assertEqual(entries[0]["observed_unix_ms"], 1_998)
        self.assertEqual(entries[-1]["observed_unix_ms"], 3_998)

        capped = [{"observed_unix_ms": stamp} for stamp in range(1_000)]
        trim_history(capped, "observed_unix_ms", 999, 10_000, 100)
        self.assertEqual(len(capped), 100)
        self.assertEqual(capped[0]["observed_unix_ms"], 900)

    def test_trim_keeps_entries_without_a_usable_timestamp(self) -> None:
        """A producer that omits the stamp degrades to count-capping."""

        entries = [{"link_id": "ue0>gnb0"} for _ in range(5)]
        trim_history(entries, "observed_unix_ms", 10_000, 10, 100)
        self.assertEqual(len(entries), 5)

    def test_history_slice_is_measured_from_the_newest_sample(self) -> None:
        """A brief producer stall must not empty the returned window."""

        entries = [{"observed_unix_ms": stamp} for stamp in range(0, 200, 2)]
        window = slice_history(entries, "observed_unix_ms", 50)
        self.assertEqual(window[0]["observed_unix_ms"], 148)
        self.assertEqual(window[-1]["observed_unix_ms"], 198)
        self.assertEqual(
            len(slice_history(entries, "observed_unix_ms", None)), len(entries)
        )
        self.assertEqual(slice_history([], "observed_unix_ms", 50), [])

    def test_status_snapshot_slices_history_to_the_requested_window(
        self,
    ) -> None:
        store = StatusStore()
        for slot in range(50):
            store.update_telemetry(
                "ue0>gnb0:sionna_rt",
                {
                    "event": "telemetry",
                    "slot": slot,
                    "slot_processing": {
                        "nominal": {"latest_estimated_us": 640.0 + slot}
                    },
                },
            )
        full = store.snapshot()["history"]["telemetry"]
        self.assertEqual(len(full), 50)
        # Every frame lands inside the same millisecond in a tight loop,
        # so a zero-width window still keeps the newest sample rather
        # than returning nothing for the chart to draw.
        narrow = store.snapshot(history_ms=0)["history"]["telemetry"]
        self.assertGreaterEqual(len(narrow), 1)
        self.assertLessEqual(len(narrow), len(full))
        self.assertEqual(narrow[-1]["slot"], 49)

    def test_realtime_snapshot_carries_only_the_charted_series(self) -> None:
        """The 1,000 ms chart loop must not pay for the full status document."""

        store = StatusStore()
        store.update_telemetry(
            "ue0>gnb0:sionna_rt",
            {
                "event": "telemetry",
                "slot": 7,
                "live": {"path_loss_db": -80.0, "awgn_snr_db": 21.0},
                "slot_processing": {
                    "elapsed_us": 750.0,
                    "cumulative": {"processed_slots": 1234},
                    "nominal": {
                        "latest_estimated_us": 812.5,
                        "p99_estimated_us": 950.0,
                    },
                },
            },
        )
        store.update_sionna(
            {
                "event": "sionna_rt_runtime",
                "process_id": os.getpid(),
                "target_update_hz": 500.0,
                "phase": "tracing_channels",
            }
        )
        store.update_gpu_usage(
            {
                "sampled_unix_ms": 1_700_000_000_000,
                "system": {"cpu_util_percent": 33.0, "other_os_cpu_cores": 2.1},
                "gpu_compute": {"total_sm_util_percent": 60.0},
                "pcie": {"h2d_mb_s": 1200.0, "d2h_mb_s": 400.0},
                "processes": {"sionna": {"running": True, "cpu_util_percent": 140.0}},
            }
        )

        realtime = store.realtime_snapshot(history_ms=DEFAULT_HISTORY_WINDOW_MS)
        telemetry_row = realtime["history"]["telemetry"][-1]
        self.assertEqual(
            telemetry_row["slot_processing"]["nominal"]["latest_estimated_us"],
            812.5,
        )
        # The projection keeps the shape the chart reads and drops the
        # ~1.3 kB of per-frame detail the chart never touches.
        self.assertNotIn("live", telemetry_row)
        self.assertNotIn("elapsed_us", telemetry_row["slot_processing"])
        self.assertNotIn(
            "p99_estimated_us", telemetry_row["slot_processing"]["nominal"]
        )
        resource_row = realtime["gpu_history"][-1]
        self.assertEqual(
            resource_row["roles"]["sionna"]["cpu_util_percent"], 140.0
        )
        self.assertEqual(resource_row["pcie"]["h2d_mb_s"], 1200.0)
        self.assertEqual(resource_row["system"]["other_os_cpu_cores"], 2.1)
        self.assertNotIn("gpu_compute", resource_row)
        self.assertEqual(realtime["target_update_hz"], 500.0)
        # None of the heavy status sections belong in the fast payload.
        for key in ("sionna", "telemetry", "delivery", "gpu_usage"):
            self.assertNotIn(key, realtime)

    def test_realtime_snapshot_keeps_every_channel_arrival_in_window(self) -> None:
        store = StatusStore()
        for iteration in range(40):
            started = 1_000 + iteration * 10
            store.update_sionna(
                {
                    "event": "sionna_rt_update",
                    "session_id": "arrival-session",
                    "iteration": iteration,
                    "update_started_unix_ms": started,
                    "control_ack_unix_ms": started + 5,
                    "timing_ms": {"total_update": 6.0},
                }
            )

        events = store.realtime_snapshot(history_ms=1_000)["history"]["iterations"]
        self.assertEqual(len(events), 40)
        self.assertEqual(events[0]["channel_arrived_unix_ms"], 1_005)
        self.assertEqual(events[-1]["channel_arrived_unix_ms"], 1_395)

    def test_retention_spans_cover_the_drawn_chart_window(self) -> None:
        """Guard the invariant the empty-chart bug came from."""

        chart_window_ms = 1_000
        self.assertGreater(TELEMETRY_HISTORY_RETAIN_MS, chart_window_ms)
        self.assertGreater(GPU_HISTORY_RETAIN_MS, chart_window_ms)
        self.assertGreaterEqual(DEFAULT_HISTORY_WINDOW_MS, chart_window_ms)
        self.assertEqual(SIONNA_JSONL_POLL_SECONDS, 0.02)

    @mock.patch("server.time.monotonic", return_value=100.0)
    def test_slow_nvml_queries_stay_off_the_fast_sampling_path(self, _clock) -> None:
        """The 10 ms cadence only works if the ~21 ms calls are gated.

        nvmlDeviceGetPcieThroughput integrates over a fixed ~20 ms driver
        window and blocks for it, and nvmlDeviceGetProcessUtilization
        costs ~1.5 ms per GPU for a value this sampler already holds for
        1.2 s. Calling either once per sample makes a 10 ms period
        physically unreachable, so both keep their own cadence and the
        last reading is carried forward.
        """

        calls = {"pcie": 0, "utilization": 0}

        class FakeNvml:
            NVML_SUCCESS = 0

            def nvmlDeviceGetUtilizationRates(self, handle, out):  # noqa: N802
                out._obj.gpu = 55
                return 0

            def nvmlDeviceGetPcieThroughput(self, handle, kind, out):  # noqa: N802
                calls["pcie"] += 1
                out._obj.value = 2048 if kind else 1024
                return 0

        sampler = NvmlResourceSampler.__new__(NvmlResourceSampler)
        sampler._nvml = FakeNvml()
        sampler._closed = False
        sampler._handles = [object()]
        sampler._inventory = [{"gpu_index": 0, "gpu_uuid": "GPU-test"}]
        sampler._inventory_updated = time.monotonic()
        sampler._sm_by_process = {}
        sampler._sm_seen_monotonic = {}
        sampler._sm_warning = None
        sampler._pcie_updated = 0.0
        sampler._pcie_cache = {}
        sampler._process_utilization_updated = 0.0

        def fake_process_utilization(index, handle):
            calls["utilization"] += 1

        sampler._process_utilization = fake_process_utilization
        sampler._process_memory = lambda handle: {4242: 512.0}

        targets = {"sionna": {4242}, "gpu_channel": set(), "web_ui": set()}
        # First sample primes both caches, the rest run inside both
        # refresh intervals and must not repeat either query.
        for _ in range(20):
            _, _, pcie, _ = sampler.sample(targets)

        self.assertEqual(calls["pcie"], 2, "one RX + one TX query for 20 samples")
        self.assertEqual(calls["utilization"], 1)
        # The carried-forward reading still reaches the chart.
        self.assertEqual(pcie[0]["h2d_mb_s"], 2.0)
        self.assertEqual(pcie[0]["d2h_mb_s"], 1.0)
        # Device-wide utilization is cheap, so it stays per-sample.
        self.assertEqual(pcie[0]["gpu_util_percent"], 55.0)

        # Past the interval, the queries run again.
        _clock.return_value += max(
            PCIE_REFRESH_INTERVAL_SECONDS, PROCESS_UTILIZATION_INTERVAL_SECONDS
        ) + 0.01
        sampler.sample(targets)
        self.assertEqual(calls["pcie"], 4)
        self.assertEqual(calls["utilization"], 2)

    def test_ue_rows_are_found_wherever_the_envelope_puts_them(self) -> None:
        """The gNB nests ue_list differently per release.

        The deployed build could not be exercised against the live run (a
        second gNB would connect to the broker's IQ sockets), so the reader
        walks for ue_list instead of hard-coding one path.
        """

        nested = {
            "timestamp": 1.0,
            "cell_metrics": {"cells": [{"pci": 1, "ue_list": [{"rnti": 17921}]}]},
        }
        rows = extract_ue_rows(nested)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["rnti"], 17921)
        # The cell's pci is carried onto the UE row for the table.
        self.assertEqual(rows[0]["pci"], 1)
        # A flatter envelope must work just as well.
        self.assertEqual(len(extract_ue_rows({"ue_list": [{"rnti": 2}]})), 1)
        # And nothing anywhere is simply empty, not an exception.
        self.assertEqual(extract_ue_rows({"other": [1, 2]}), [])
        self.assertEqual(extract_ue_rows(None), [])
        # A UE that already carries pci keeps its own.
        own = extract_ue_rows({"pci": 9, "ue_list": [{"rnti": 1, "pci": 3}]})
        self.assertEqual(own[0]["pci"], 3)

    def test_only_a_scheduler_report_speaks_for_the_ue_table(self) -> None:
        """Other consumers on the shared socket are silent, not empty.

        The gNB fans every metrics consumer out to the same subscribers
        (remote_server.cpp), so a CU-CP or RU notification carries no UE
        vocabulary at all. Recognising it as a report about UEs would
        turn its silence into "zero UEs" every time one landed between
        two scheduler reports.
        """

        self.assertTrue(
            is_scheduler_report({"cells": [{"pci": 1, "ue_list": [{"rnti": 1}]}]})
        )
        # A report whose cells hold no UEs is still a report about UEs:
        # this is the message that legitimately means "none attached".
        self.assertTrue(is_scheduler_report({"cells": [{"pci": 1}]}))
        self.assertTrue(is_scheduler_report({"ue_list": []}))
        # Everything else on the bus is not.
        self.assertFalse(is_scheduler_report({"cu_cp": {"nof_ues": 0}}))
        self.assertFalse(is_scheduler_report({"app_resource_usage": {"cpu": 3.5}}))
        self.assertFalse(is_scheduler_report({"cells": "not-a-list"}))
        self.assertFalse(is_scheduler_report(None))

    def test_foreign_notifications_do_not_blank_the_ue_table(self) -> None:
        """The regression: a live table flickering to "no connected UEs".

        Every notification used to overwrite the whole UE table, so the
        table only survived until the next non-scheduler message, which
        on this socket is most of them.
        """

        store = StatusStore()
        store.update_gnb_metrics(
            {"cells": [{"pci": 1, "ue_list": [{"rnti": 17921, "cqi": 12.4}]}]}
        )
        ran = store.snapshot()["ran"]
        self.assertEqual(ran["ue_reports"], 1)
        first_report_unix_ms = ran["ue_list_unix_ms"]
        self.assertIsNotNone(first_report_unix_ms)

        # An RU notification arrives; it says nothing about UEs.
        store.update_gnb_metrics({"ru": {"average_latency_us": 12.0}})
        ran = store.snapshot()["ran"]
        self.assertEqual(ran["messages"], 2)
        self.assertEqual(ran["ue_reports"], 1)
        self.assertEqual(ran["ue_list"][0]["rnti"], 17921)
        # The UE table keeps its own timestamp, not the feed's.
        self.assertEqual(ran["ue_list_unix_ms"], first_report_unix_ms)

        # A scheduler report with no UEs is the one message that empties
        # the table, because that report always lists every attached UE.
        store.update_gnb_metrics({"cells": [{"pci": 1}]})
        ran = store.snapshot()["ran"]
        self.assertEqual(ran["ue_list"], [])
        self.assertEqual(ran["ue_reports"], 2)

    def test_ue_table_age_is_measured_from_the_scheduler_report(self) -> None:
        """Staleness is judged on the report that owns the rows.

        Foreign notifications keep the feed's own age at zero, so reading
        staleness off `age_seconds` would call a frozen scheduler live.
        """

        store = StatusStore()
        with mock.patch("server.time.monotonic", return_value=1000.0):
            store.update_gnb_metrics(
                {"cells": [{"pci": 1, "ue_list": [{"rnti": 17921}]}]}
            )
        with mock.patch("server.time.monotonic", return_value=1030.0):
            store.update_gnb_metrics({"ru": {"average_latency_us": 12.0}})
            ran = store.snapshot()["ran"]
        self.assertEqual(ran["age_seconds"], 0.0)
        self.assertEqual(ran["ue_list_age_seconds"], 30.0)
        self.assertTrue(ran["ue_list_stale"])
        self.assertGreater(30.0, GNB_UE_REPORT_STALE_SECONDS)

    def test_named_gnb_reports_are_independent(self):
        store = StatusStore()
        report = {"cells": [{"pci": 1, "ue_list": [{"rnti": 17921}]}]}
        with mock.patch("server.time.monotonic", return_value=100.0):
            store.update_gnb_metrics(report, "gnb0")
            store.update_gnb_metrics(report, "gnb1")
        with mock.patch("server.time.monotonic", return_value=105.0):
            self.assertFalse(store.snapshot()["ran_gnbs"]["gnb0"]["ue_list_stale"])
        with mock.patch("server.time.monotonic", return_value=105.0001):
            store.update_gnb_metrics({"ru": {"latency": 12}}, "gnb0")
            store.update_gnb_metrics(report, "gnb1")
            state = store.snapshot()
        a, b = state["ran_gnbs"].values()
        self.assertTrue(a["ue_list_stale"])
        self.assertFalse(b["ue_list_stale"])
        self.assertEqual(a["ue_list"], b["ue_list"])  # Same PCI/RNTI is valid on different sources.
        self.assertEqual(a["ue_reports"], 1)
        self.assertEqual(b["ue_reports"], 2)
        self.assertEqual(state["ran"], a)  # Legacy view follows the first source.
        store.set_gnb_metrics_state("disconnected", "closed", "gnb0")
        self.assertEqual(store.snapshot()["ran_gnbs"]["gnb1"]["state"], "connected")
        store.update_gnb_metrics({"cells": [{"pci": 1}]}, "gnb1")
        state = store.snapshot()["ran_gnbs"]
        self.assertEqual(state["gnb1"]["ue_list"], [])
        self.assertEqual(len(state["gnb0"]["ue_list"]), 1)
        store.update_gnb_metrics(report, "gnb0")
        state = store.snapshot()["ran_gnbs"]
        self.assertEqual(state["gnb0"]["state"], "connected")
        self.assertIsNone(state["gnb0"]["error"])
        self.assertFalse(state["gnb0"]["ue_list_stale"])
        self.assertEqual(state["gnb1"]["ue_list"], [])

    def test_named_gnb_configuration_and_legacy_compatibility(self):
        self.assertEqual(parse_args([]).gnb_metrics_sources, {})
        self.assertEqual(parse_args(["--gnb-metrics-endpoint", "ws://localhost:8001"]).gnb_metrics_sources,
                         {"gnb0": "ws://localhost:8001"})
        named = ["--gnb-metrics-source", "gnb0=ws://localhost:18001",
                 "--gnb-metrics-source", "gnb1=ws+unix:///tmp/gnb1.sock"]
        self.assertEqual(list(parse_args(named).gnb_metrics_sources), ["gnb0", "gnb1"])
        for extra in (["--gnb-metrics-source", "gnb0=ws://localhost:18002"],
                      ["--gnb-metrics-endpoint", "ws://localhost:8001"],
                      ["--gnb-metrics-source", "missing-separator"],
                      ["--gnb-metrics-source", "gnb2=invalid://localhost"],
                      ["--gnb-metrics-source", "=ws://localhost"]):
            with self.subTest(extra=extra), mock.patch("sys.stderr"), self.assertRaises(SystemExit):
                parse_args(named + extra)
        store = StatusStore()
        self.assertEqual(store.snapshot()["ran_gnbs"], {})
        store.set_gnb_metrics_state("connecting", gnb_id="gnb1")
        self.assertIsNone(store.snapshot()["ran_gnbs"]["gnb1"]["ue_list_age_seconds"])

    def test_named_metrics_worker_recovers_and_closes_connections(self):
        import threading
        store = StatusStore()
        store.update_gnb_metrics({"ue_list": [{"rnti": 1}]}, "gnb0")
        stop = threading.Event()
        first, second = mock.Mock(), mock.Mock()
        first.recv_message.side_effect = [json.dumps({"ue_list": [{"rnti": 2}]}), OSError("lost")]
        def finish():
            stop.set()
            return None
        responses = iter([json.dumps({"ue_list": [{"rnti": 3}]})])
        def receive():
            return next(responses) if second.recv_message.call_count == 1 else finish()
        second.recv_message.side_effect = receive
        with mock.patch("server.WebSocketClient", side_effect=[first, second]), mock.patch.object(stop, "wait", return_value=False):
            worker = threading.Thread(target=gnb_metrics_loop, args=("ws://localhost:8001", store, stop, "gnb1"))
            worker.start()
            worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        first.close.assert_called_once()
        second.close.assert_called_once()
        feeds = store.snapshot()["ran_gnbs"]
        self.assertEqual(feeds["gnb1"]["ue_reports"], 2)
        self.assertEqual(feeds["gnb1"]["ue_list"][0]["rnti"], 3)
        self.assertEqual(feeds["gnb0"]["ue_list"][0]["rnti"], 1)
        self.assertEqual(feeds["gnb0"]["state"], "connected")

    def test_metrics_worker_routes_updates_and_disconnect_to_named_source(self):
        store = StatusStore()
        store.update_gnb_metrics({"ue_list": [{"rnti": 1}]}, "gnb0")
        before = store.snapshot()["ran_gnbs"]["gnb0"]
        stop = mock.Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = True  # Exit after the simulated connection fails.
        with mock.patch("server.WebSocketClient") as client:
            client.return_value.recv_message.side_effect = [
                json.dumps({"ue_list": [{"rnti": 2}]}), OSError("test disconnect")]
            gnb_metrics_loop("ws://localhost:8001", store, stop, "gnb1")
            client.return_value.close.assert_called_once()
        state = store.snapshot()["ran_gnbs"]
        self.assertEqual(state["gnb1"]["ue_list"][0]["rnti"], 2)
        self.assertEqual(state["gnb1"]["state"], "disconnected")
        self.assertEqual(state["gnb0"]["messages"], before["messages"])
        self.assertEqual(state["gnb0"]["ue_list"], before["ue_list"])
        self.assertEqual(state["gnb0"]["state"], "connected")

    def test_scheduler_sentinels_are_not_rendered_as_readings(self) -> None:
        """The gNB signals "not reported" as a value, not as an omission.

        `cqi` is -1 when no CSI report arrived in the period, an unreported
        PUSCH RSRP is -inf clamped to -99.9 dB on the way out, and a PUCCH
        SINR mean that any DTX occasion pulled to -inf lands on the same
        floor (ocudu json_generators/du_high/scheduler.cpp and
        lib/scheduler/logging/cell_metrics_handler.cpp:683-687). Printed
        as readings they claim the UE measured the worst link on record.
        """

        page = (PROJECT_ROOT / "apps" / "dashboard" / "index.html").read_text()
        # CQI is guarded on the -1 sentinel rather than formatted blind.
        self.assertIn("['CQI',r=>finite(r.cqi)&&Number(r.cqi)>=0", page)
        # Every dB column goes through the clamp-aware formatter.
        for field in ("pusch_snr_db", "pucch_snr_db", "pusch_rsrp_db"):
            self.assertIn(f"fmtDb(r.{field})", page)
        self.assertIn("const RAN_DB_FLOOR=-99.9,RAN_DB_CEIL=99.9", page)
        # The sentinels that cannot be told apart from readings are named
        # for the reader instead of being silently hidden.
        self.assertIn("falls back to 1 when no rank was reported", page)

    def test_metrics_endpoint_parsing(self) -> None:
        self.assertEqual(
            parse_ws_endpoint("ws://127.0.0.1:8001"),
            ("127.0.0.1", 8001, "/", None),
        )
        # Bare host:port and a default port both resolve.
        self.assertEqual(
            parse_ws_endpoint("127.0.0.1"), ("127.0.0.1", 8001, "/", None)
        )
        # The native gate runs the gNB in its own network namespace, so the
        # dashboard reaches it through a relayed AF_UNIX socket instead.
        self.assertEqual(
            parse_ws_endpoint("ws+unix:///run/s1/gnb-metrics.sock"),
            ("localhost", 0, "/", "/run/s1/gnb-metrics.sock"),
        )
        with self.assertRaises(ValueError):
            parse_ws_endpoint("tcp://127.0.0.1:8001")
        with self.assertRaises(ValueError):
            parse_ws_endpoint("ws+unix://relative.sock")

    def test_ran_block_is_present_and_read_only_by_default(self) -> None:
        store = StatusStore()
        ran = store.snapshot()["ran"]
        # Disabled until the operator passes --gnb-metrics-endpoint.
        self.assertEqual(ran["state"], "disabled")
        self.assertEqual(ran["ue_list"], [])
        self.assertEqual(ran["messages"], 0)

        store.update_gnb_metrics(
            {"cell_metrics": {"cells": [{"pci": 1, "ue_list": [
                {"rnti": 17921, "cqi": 12.4, "dl_brate": 18_400_000,
                 "dl_nof_ok": 1000, "dl_nof_nok": 7, "pusch_snr_db": 24.5,
                 "bsr": 4096, "ta_ns": 520.0, "last_phr": 23}]}]}}
        )
        ran = store.snapshot()["ran"]
        self.assertEqual(ran["state"], "connected")
        self.assertEqual(ran["messages"], 1)
        self.assertEqual(ran["ue_list"][0]["rnti"], 17921)
        self.assertEqual(ran["ue_list"][0]["pusch_snr_db"], 24.5)

        store.set_gnb_metrics_state("disconnected", "boom")
        ran = store.snapshot()["ran"]
        self.assertEqual(ran["state"], "disconnected")
        self.assertEqual(ran["error"], "boom")
        # The last good sample survives a disconnect so the table does not
        # blank out while the feed reconnects.
        self.assertEqual(ran["ue_list"][0]["rnti"], 17921)

    def test_store_exposes_runtime_phase_and_process_targets(self) -> None:
        store = StatusStore()
        store.update_sionna(
            {
                "event": "sionna_rt_runtime",
                "process_id": os.getpid(),
                "phase": "tracing_channels",
                "execution_mode": "single_process_repeating_update_loop",
            }
        )
        store.update_telemetry(
            "gnb0>ue0:sionna_rt",
            {
                "event": "telemetry",
                "link_id": "gnb0>ue0:sionna_rt",
                "process_id": 4321,
            },
        )
        snapshot = store.snapshot()
        self.assertTrue(snapshot["sionna_runtime"]["process_alive"])
        self.assertEqual(snapshot["sionna_runtime"]["phase"], "tracing_channels")
        self.assertEqual(store.process_targets()["sionna"], {os.getpid()})
        self.assertEqual(store.process_targets()["gpu_channel"], {4321})
        self.assertEqual(store.process_targets()["web_ui"], {os.getpid()})
        store.update_sionna(
            {
                "event": "sionna_rt_update",
                "session_id": "test-session",
                "iteration": 17,
                "control_ack_unix_ms": 123456,
            }
        )
        store.update_gpu_usage(
            {
                "sampled_unix_ms": 123500,
                "available": True,
                "processes": {
                    "sionna": {
                        "running": True,
                        "gpu_resident": True,
                        "sm_util_percent": 42.0,
                        "gpu_memory_mib": 1000.0,
                        "cpu_util_percent": 125.0,
                        "ram_rss_mib": 2048.0,
                    },
                    "gpu_channel": {
                        "running": True,
                        "gpu_resident": True,
                        "sm_util_percent": 18.0,
                        "gpu_memory_mib": 500.0,
                        "cpu_util_percent": 75.0,
                        "ram_rss_mib": 256.0,
                    },
                },
                "system": {
                    "cpu_util_percent": 33.0,
                    "used_mib": 8192.0,
                    "total_mib": 32768.0,
                },
                "pcie": {"h2d_mb_s": 1200.0, "d2h_mb_s": 300.0},
            }
        )
        history_snapshot = store.snapshot()["gpu_usage"]
        self.assertEqual(history_snapshot["history"][-1]["iteration"], 17)
        self.assertEqual(
            history_snapshot["history"][-1]["roles"]["sionna"]["sm_util_percent"],
            42.0,
        )
        self.assertEqual(
            history_snapshot["history"][-1]["roles"]["sionna"]["ram_rss_mib"],
            2048.0,
        )
        self.assertEqual(
            history_snapshot["history"][-1]["system"]["cpu_util_percent"], 33.0
        )
        self.assertEqual(
            history_snapshot["history"][-1]["pcie"]["h2d_mb_s"], 1200.0
        )
        self.assertEqual(history_snapshot["iterations"][-1]["iteration"], 17)

    def test_nvidia_process_samples_are_parsed_by_pid_and_gpu(self) -> None:
        gpu_map = parse_nvidia_gpu_map("0, GPU-aaaa\n1, GPU-bbbb\n")
        self.assertEqual(gpu_map, {"GPU-aaaa": 0, "GPU-bbbb": 1})
        apps = parse_nvidia_compute_apps(
            "GPU-bbbb, 1234, /usr/bin/python3, 812.5\n", gpu_map
        )
        self.assertEqual(apps[0]["gpu_index"], 1)
        self.assertEqual(apps[0]["gpu_memory_mib"], 812.5)
        pmon = parse_nvidia_pmon(
            "# gpu pid type sm mem fb command\n"
            "# Idx # C/G % % MB name\n"
            "1 1234 C 37 8 813 python3\n"
        )
        self.assertEqual(pmon[0]["pid"], 1234)
        self.assertEqual(pmon[0]["gpu_index"], 1)
        self.assertEqual(pmon[0]["sm_util_percent"], 37.0)
        self.assertEqual(pmon[0]["memory_util_percent"], 8.0)
        self.assertEqual(pmon[0]["gpu_memory_mib"], 813.0)

    def test_device_hardware_and_pcie_samples_are_parsed(self) -> None:
        inventory = parse_nvidia_gpu_inventory(
            "0, NVIDIA GeForce RTX 5090, GPU-aaaa, 32607, 1812, 5, 4, 62\n"
        )
        self.assertEqual(inventory[0]["name"], "NVIDIA GeForce RTX 5090")
        self.assertEqual(inventory[0]["memory_total_mib"], 32607.0)
        self.assertEqual(inventory[0]["pcie_gen"], 5)
        self.assertEqual(inventory[0]["gpu_util_percent"], 62.0)
        pcie = parse_nvidia_dmon_pcie(
            "# gpu rxpci txpci\n"
            "# Idx MB/s MB/s\n"
            "0 12288 1400\n"
        )
        self.assertEqual(pcie[0]["h2d_mb_s"], 12288.0)
        self.assertEqual(pcie[0]["d2h_mb_s"], 1400.0)

    def test_linux_cpu_and_memory_samples_are_parsed(self) -> None:
        memory = parse_proc_meminfo(
            "MemTotal:       65536 kB\n"
            "MemFree:         4096 kB\n"
            "MemAvailable:   16384 kB\n"
        )
        self.assertEqual(memory["total_mib"], 64.0)
        self.assertEqual(memory["available_mib"], 16.0)
        self.assertEqual(memory["used_mib"], 48.0)
        self.assertEqual(parse_proc_cpu_total("cpu  10 2 3 40 5 0 0 0\n"), (60, 45))
        self.assertEqual(parse_proc_status_rss("Name:\ttest\nVmRSS:\t2048 kB\n"), 2.0)
        pid_stat = "123 (worker with spaces) R 1 2 3 4 5 6 7 8 9 10 120 30 0 0"
        self.assertEqual(parse_proc_pid_ticks(pid_stat), 150)

        cpu_info = parse_cpu_hardware(
            "processor: 0\nphysical id: 0\ncore id: 0\nmodel name: Test CPU\n\n"
            "processor: 1\nphysical id: 0\ncore id: 0\nmodel name: Test CPU\n\n"
            "processor: 2\nphysical id: 0\ncore id: 1\nmodel name: Test CPU\n",
            {0, 1, 2},
        )
        self.assertEqual(cpu_info["model"], "Test CPU")
        self.assertEqual(cpu_info["physical_cores"], 2)
        self.assertEqual(cpu_info["logical_threads"], 3)

    def test_web_ui_uses_measured_latency_and_channel_graphs(self) -> None:
        index = (PROJECT_ROOT / "apps" / "dashboard" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("Current MEM", index)
        self.assertNotIn('id="gpuComputeChart"', index)
        self.assertIn('id="slotLatencyChart"', index)
        self.assertNotIn('id="sionnaIterationChart"', index)
        self.assertNotIn('id="channelPowerChart"', index)
        self.assertIn('id="applicationCpuChart"', index)
        self.assertNotIn('id="memoryChart"', index)
        self.assertIn('id="pcieChart"', index)
        self.assertNotIn('id="computeChart"', index)
        self.assertNotIn("GPU Compute (%)", index)
        self.assertIn("Estimated nominal-slot processing time (µs) · 1 ms reference", index)
        self.assertNotIn("Sionna iteration time", index)
        self.assertNotIn("Sionna channel power history", index)
        self.assertNotIn("Total path power by link (dB)", index)
        self.assertIn("NVML sampled utilization", index)
        self.assertIn("1 ms nominal deadline", index)
        self.assertNotIn("update budget", index)
        self.assertIn("slot_processing?.nominal?.latest_estimated_us", index)
        self.assertIn("nominal.deadline_misses", index)
        self.assertIn("Estimated p95", index)
        self.assertIn("Sample (샘플)", index)
        self.assertIn("Fragment (조각)", index)
        self.assertIn("sample_proportional", index)
        self.assertIn("fixed CUDA launch/transfer overhead", index)
        self.assertNotIn("DL ray trace", index)
        self.assertNotIn("Broker control ACK", index)
        self.assertIn("Array.from({length:8}", index)
        self.assertIn("overlaid matrix-lane tap impulse responses", index)
        self.assertIn("tapPlot(plotLanes,plotColors)", index)
        self.assertNotIn("delayDopplerPlot", index)
        self.assertNotIn("delay_doppler_points", index)
        self.assertNotIn("delay–Doppler–power", index)
        self.assertNotIn("Paths.doppler in Hz", index)
        self.assertNotIn("delay-doppler-stack", index)
        self.assertIn("function laneFrequencyResponse(lane, ratios)", index)
        self.assertIn("function frequencyResponsePlot(lanes, colors, sampleRateHz)", index)
        self.assertIn(
            "frequencyResponsePlot(plotLanes,plotColors,Number(sampleRateHz))",
            index,
        )
        self.assertIn("renderEdgeCards(s?.channels,sampleRateHz)", index)
        self.assertIn("overlaid matrix-lane frequency responses", index)
        # The heading now names the filtered scope, not always "all".
        self.assertIn("Frequency response · |H(f)| over ${scopeText}", index)
        self.assertIn("`all ${lanes.length} lane(s)`", index)
        self.assertIn("Impulse and frequency response by edge link", index)
        self.assertIn("in-band ripple", index)
        self.assertIn("Baseband offset from carrier", index)
        self.assertIn("freq-block", index)
        self.assertIn("Lane (rx${esc(lane.rx_port)}, tx${esc(lane.tx_port)})", index)
        self.assertIn("lane-tap-block", index)
        self.assertIn("rowIndex+1", index)
        self.assertIn("display-only horizontal stem offset", index)
        self.assertNotIn("phaseHue(t.phase_rad)", index)
        self.assertIn("total_path_power_db", index)
        self.assertIn("Application CPU (logical cores)", index)
        self.assertIn("Real-time memory by process", index)
        self.assertIn("Sionna RT / Bridge", index)
        self.assertIn("GPU Channel", index)
        self.assertIn("Web UI", index)
        self.assertIn('style="text-align:right">VRAM', index)
        self.assertIn('style="text-align:right">RSS', index)
        self.assertIn("Resident Set Size", index)
        self.assertIn("renderMemorySummary(usage)", index)
        self.assertIn('id="nodeCards"', index)
        self.assertIn("function renderNodeCards(sionna)", index)
        self.assertIn("matrix_profile_active", index)
        self.assertIn("matrix total", index)
        self.assertIn("Position", index)
        self.assertIn("Antenna", index)
        self.assertIn("antenna.tx===antenna.rx", index)
        self.assertIn("ctx.fillText(`${id} · ${kind}`", index)
        self.assertNotIn('id="envAntenna"', index)
        self.assertNotIn('id="envNodes"', index)
        self.assertNotIn("const speedLabel=", index)
        self.assertNotIn("memory-breakdown", index)
        self.assertIn("minDelay=Math.max(0,Math.min(...delays))", index)
        self.assertIn("tapSpan=maxDelay-minDelay", index)
        self.assertIn("axisPadding=Math.max(.125,tapSpan*.12)", index)
        self.assertIn("plotMinDelay=Math.max(0,Math.floor", index)
        self.assertIn("niceStepFraction=", index)
        self.assertIn("plotMaxDelay-plotMinDelay", index)
        self.assertIn("${fmt(plotMinDelay,axisDigits)} samples", index)
        self.assertIn("${fmt(plotMaxDelay,axisDigits)} samples", index)
        self.assertNotIn(">20 samples</text>", index)
        self.assertIn(
            "automatically frames the earliest and latest taps with a small margin",
            index,
        )
        self.assertIn("sampling frequency (sample rate)", index)
        self.assertIn("nanoseconds", index)
        self.assertIn("1e9/sampleRateHz", index)
        self.assertNotIn("label:'Total GPU'", index)
        self.assertNotIn("label:'Other GPU'", index)
        self.assertIn("label:'Web UI'", index)
        self.assertIn("label:'Other / OS'", index)
        self.assertNotIn("label:'Host CPU'", index)
        self.assertIn("const CHART_WINDOW_MS = 1000;", index)
        self.assertIn("const CHART_POLL_MS = CHART_WINDOW_MS;", index)
        self.assertIn("const CHART_HISTORY_MS = CHART_WINDOW_MS * 2;", index)
        self.assertIn("windowMs=config.windowMs||CHART_WINDOW_MS", index)
        self.assertIn("end=Math.floor(now/windowMs)*windowMs", index)
        self.assertIn("tickDivisions=5", index)
        self.assertIn("const fmtClock = value =>", index)
        self.assertIn("date.getHours()", index)
        self.assertIn("date.getMilliseconds(),3", index)
        self.assertIn("ctx.fillText(fmtClock(time)", index)
        self.assertNotIn("millisecondsAgo", index)
        self.assertNotIn("ms ago", index)
        self.assertIn("item.channel_arrived_unix_ms", index)
        self.assertIn("label:'Sionna channel'", index)
        self.assertIn("color:'#53e58b'", index)
        self.assertIn("markers.forEach(marker=>", index)
        # Per-port lane picker on the impulse/frequency responses.
        self.assertIn("function laneOptions(lanes)", index)
        # One antenna row per edge, 1-based labels over 0-based ports,
        # following whichever matrix axis carries the multi-port node.
        self.assertIn("label:`Antenna ${index+1}`", index)
        self.assertIn(">All ${lanes.length}</button>", index)
        self.assertNotIn("label:`RX ${port}`", index)
        self.assertNotIn("label:`TX ${port}`", index)
        # All selected lanes must be readable without a scroll box: the
        # old 680px cap fit two lanes and hid lane 4 once the gNB had
        # four ports.
        self.assertNotIn("max-height:680px", index)
        self.assertIn(
            ".lane-tables { display:grid; "
            "grid-template-columns:repeat(auto-fit,minmax(300px,1fr));",
            index,
        )
        self.assertIn("const laneSelection={}", index)
        self.assertIn('class="lane-picker" data-link=', index)
        self.assertIn("laneSelection[channel.link_id]??'all'", index)
        self.assertIn("tapPlot(plotLanes,plotColors)", index)
        self.assertIn(
            "frequencyResponsePlot(plotLanes,plotColors,Number(sampleRateHz))",
            index,
        )
        self.assertIn("$('edgeCards').addEventListener('click'", index)
        self.assertIn("laneSelection[linkId]=button.dataset.port", index)
        # The plots must not fall back to the whole lane set once the
        # picker exists, or a selection would silently do nothing.
        self.assertNotIn("tapPlot(lanes,laneColors)", index)
        self.assertNotIn("frequencyResponsePlot(lanes,laneColors", index)
        # RAN KPI panel, and it must be the last section on the page.
        self.assertIn("RAN KPIs by UE · gNB scheduler metrics", index)
        self.assertIn("function renderRanCards(ran, gnbs)", index)
        self.assertIn("renderRanCards(data.ran, data.ran_gnbs)", index)
        for field in ("rnti", "cqi", "dl_ri", "dl_mcs", "dl_brate", "dl_nof_nok",
                      "pusch_snr_db", "pucch_snr_db", "pusch_rsrp_db", "bsr",
                      "ta_ns", "last_phr"):
            self.assertIn(f"r.{field}", index)
        self.assertLess(
            index.index("Impulse and frequency response by edge link"),
            index.index("RAN KPIs by UE"),
            "the RAN panel must sit at the bottom of the page",
        )
        self.assertIn("hardware-card.active", index)
        self.assertIn("started_unix_ms", index)
        self.assertIn("warmup_started_unix_ms", index)
        self.assertIn("maxGapMs", index)
        self.assertIn("warmupMarkerWidth=6", index)
        self.assertIn("const warmupEvents=config.showWarmups===true?events:[]", index)
        self.assertIn("timeKey:'observed_unix_ms',showWarmups:true", index)
        self.assertEqual(index.count("showWarmups:true"), 1)
        self.assertIn("event.warmup_boundary_source==='backend'", index)
        self.assertIn("■ W measured warm-up", index)
        self.assertIn("? timing unavailable", index)
        self.assertIn("unknownWarmups.forEach", index)
        self.assertNotIn("ctx.fillRect(x0,top,Math.max(1,x1-x0),plotH)", index)

    def test_application_outlook_sits_between_bridge_and_scene(self) -> None:
        index = (PROJECT_ROOT / "apps" / "dashboard" / "index.html").read_text(
            encoding="utf-8"
        )
        bridge = index.index("Sionna RT bridge · how the live channel reaches the emulator")
        outlook = index.index("Applications &amp; outlook")
        scene = index.index("Sionna environment · Near top-down 3D mobility")
        self.assertLess(bridge, outlook)
        self.assertLess(outlook, scene)
        self.assertIn('class="outlook-card industry"', index)
        self.assertIn('class="outlook-card academia"', index)
        self.assertIn("Real-time RAN software validation before RF and field testing", index)
        self.assertIn("A research platform for validating simulation algorithms", index)
        self.assertIn(".runtime-grid,.gpu-processes,.outlook-grid", index)

    def test_web_ui_leads_with_rank1_workstream_brief(self) -> None:
        index = (PROJECT_ROOT / "apps" / "dashboard" / "index.html").read_text(
            encoding="utf-8"
        )
        # The brief must sit above the live dashboard, not below it.
        self.assertLess(
            index.index("Rank-1 multi-antenna channel emulation"),
            index.index("Sionna environment · Near top-down 3D mobility"),
        )
        # The architecture figure is inline SVG built from the page palette, not
        # an external raster, and must not depend on marker rendering.
        self.assertIn('<svg class="arch-figure"', index)
        self.assertNotIn("diag-mimo-rank1-architecture", index)
        self.assertNotIn("<img", index)
        self.assertNotIn("marker-end=", index)
        for label in ("OCUDU gNB", "GPU Channel Emulator", "srsUE"):
            self.assertIn(f">{label}</text>", index)
        self.assertIn("y = [ h0  h1  h2  h3 ] · x", index)
        self.assertIn("[ g0 ]", index)
        self.assertIn("nof_antennas = 1 (never changed)", index)
        self.assertIn("4 branches,", index)
        # Downlink uses the dashboard cyan, uplink the dashboard amber.
        self.assertIn('<rect x="333" y="187" width="14" height="14" rx="3" fill="#42d3ff"/>', index)
        self.assertIn('<rect x="333" y="377" width="14" height="14" rx="3" fill="#ffc857"/>', index)
        # Claims, evidence and boundary all travel together.
        self.assertIn("4.66e-05 · 2.14e-05 · 1.68e-05 · 2.78e-05", index)
        self.assertIn("off-diagonal share 0.081", index)
        self.assertIn("Leave-one-out — passing alone is not accepted", index)
        self.assertIn("Claim boundary", index)
        self.assertIn("not claimed: rank &gt; 1 SU-MIMO", index)
        self.assertIn("superpose_kernel · row_begin[r]", index)
        self.assertIn("p50 115 µs · p99.9 675 µs", index)
        self.assertIn("run-ocudu-rank1-4x1.sh", index)
        self.assertIn("Unmatched number of RE (212 != 106)", index)
        self.assertIn("--allow-silent-source", index)

    def test_web_ui_documents_the_sionna_bridge_topology(self) -> None:
        index = (PROJECT_ROOT / "apps" / "dashboard" / "index.html").read_text(
            encoding="utf-8"
        )
        # The bridge diagram follows the rank-1 brief and precedes the live panels.
        self.assertLess(
            index.index("Rank-1 multi-antenna channel emulation"),
            index.index("Sionna RT bridge · how the live channel reaches the emulator"),
        )
        self.assertLess(
            index.index("Sionna RT bridge · how the live channel reaches the emulator"),
            index.index("Sionna environment · Near top-down 3D mobility"),
        )
        # Two inline figures, no raster, no marker dependency.
        self.assertEqual(index.count('<svg class="arch-figure"'), 2)
        # Every plane is named with its real socket type and default endpoint.
        for token in (
            "ZMQ REQ → REP",
            "127.0.0.1:5559",
            "ZMQ PUB → SUB",
            "127.0.0.1:5560",
            "topic = link_id",
            "results/sionna-2gnb-2ue.jsonl",
            "IQ data plane",
        ):
            self.assertIn(token, index)
        # The bridge loop steps must match the functions that actually run.
        for step in (
            "update_positions()",
            "trace_all_profiles()",
            "rays_to_taps()",
            "send_matrix_profiles()",
            "append_status()",
        ):
            self.assertIn(step, index)
        self.assertIn("batch_begin → 10 swaps → batch_commit", index)
        self.assertIn("snap_physical_link() · slot boundary", index)
        # The broker box hangs off the emulator box, so it must keep that box's
        # x span and carry a name that states the containment.
        self.assertIn("Broker — inside GPU Channel Emulator", index)
        self.assertIn('<rect x="470" y="490" width="440"', index)
        self.assertIn('<rect x="470" y="56" width="440"', index)
        self.assertIn("never opens the control socket", index)
        self.assertIn("gnb0&gt;ue0:sionna_rt", index)
        self.assertIn("Sionna RT is never on this path", index)

    def test_store_tracks_iteration_boundaries_and_warmup_interval(self) -> None:
        store = StatusStore()
        store.update_sionna(
            {
                "event": "sionna_rt_runtime",
                "session_id": "timeline-session",
                "iteration": 4,
                "phase": "tracing_channels",
                "observed_unix_ms": 1_000,
            }
        )
        running_event = store.snapshot()["gpu_usage"]
        self.assertIsNone(running_event)

        store.update_sionna(
            {
                "event": "sionna_rt_update",
                "session_id": "timeline-session",
                "iteration": 4,
                "update_started_unix_ms": 1_000,
                "control_ack_unix_ms": 1_200,
                "timing_ms": {"total_update": 250.0},
                "control_reply": {
                    "links": [
                        {
                            "link_id": "gnb0>ue0:sionna_rt",
                            "seqno": 7,
                            "warmup_until_slot": 12,
                        }
                    ]
                },
            }
        )
        store.update_gpu_usage(
            {"sampled_unix_ms": 1_260, "processes": {}, "system": {}, "pcie": {}}
        )
        event = store.snapshot()["gpu_usage"]["iterations"][-1]
        self.assertEqual(event["started_unix_ms"], 1_000)
        self.assertEqual(event["ended_unix_ms"], 1_250)
        self.assertEqual(event["channel_arrived_unix_ms"], 1_200)
        self.assertIsNone(event["warmup_started_unix_ms"])
        self.assertIsNone(event["warmup_ended_unix_ms"])

        with mock.patch("server.time.time_ns", return_value=1_300_000_000):
            store.update_telemetry(
                "gnb0>ue0:sionna_rt",
                {
                    "event": "telemetry",
                    "link_id": "gnb0>ue0:sionna_rt",
                    "seqno": 7,
                    "slot": 12,
                    "profile_active": True,
                    "warmup_until_slot": 0,
                    "warmup_event_seq": 4,
                    "warmup_profile_seqno": 7,
                    "warmup_begin_slot": 11,
                    "warmup_end_slot": 12,
                    "warmup_begin_unix_ns": 1_205_000_000,
                    "warmup_end_unix_ns": 1_213_000_000,
                },
            )
        event = store.snapshot()["gpu_usage"]["iterations"][-1]
        self.assertEqual(event["warmup_started_unix_ms"], 1_205.0)
        self.assertEqual(event["warmup_ended_unix_ms"], 1_213.0)
        self.assertEqual(event["warmup_begin_slots"], {"gnb0>ue0:sionna_rt": 11})
        self.assertEqual(event["warmup_end_slots"], {"gnb0>ue0:sionna_rt": 12})
        self.assertEqual(event["warmup_boundary_source"], "backend")

    def test_store_keeps_observation_fallback_for_legacy_telemetry(self) -> None:
        store = StatusStore()
        store.update_sionna(
            {
                "event": "sionna_rt_update",
                "session_id": "legacy-session",
                "iteration": 1,
                "control_ack_unix_ms": 2_000,
                "control_reply": {
                    "links": [
                        {
                            "link_id": "legacy-link",
                            "seqno": 3,
                            "warmup_until_slot": 8,
                        }
                    ]
                },
            }
        )
        store.update_gpu_usage(
            {"sampled_unix_ms": 2_010, "processes": {}, "system": {}, "pcie": {}}
        )
        # Legacy telemetry has no backend timestamps. Its interval starts
        # only when warming is observed, never at the estimated control ACK.
        with mock.patch("server.time.time_ns", return_value=2_020_000_000):
            store.update_telemetry(
                "legacy-link",
                {
                    "event": "telemetry", "link_id": "legacy-link",
                    "seqno": 3, "slot": 7, "profile_active": True,
                    "warmup_until_slot": 8,
                },
            )
        with mock.patch("server.time.time_ns", return_value=2_050_000_000):
            store.update_telemetry(
                "legacy-link",
                {
                    "event": "telemetry",
                    "link_id": "legacy-link",
                    "seqno": 3,
                    "slot": 8,
                    "profile_active": True,
                    "warmup_until_slot": 0,
                },
            )
        event = store.snapshot()["gpu_usage"]["iterations"][-1]
        self.assertEqual(event["warmup_started_unix_ms"], 2_020)
        self.assertEqual(event["warmup_ended_unix_ms"], 2_050)
        self.assertEqual(event["warmup_boundary_source"], "telemetry_observed")

    def test_resource_sample_combines_gpu_host_and_pcie_metrics(self) -> None:
        class FakeHostSampler:
            def sample(self, _targets: object) -> dict[str, object]:
                return {
                    "hardware": {
                        "model": "Test CPU",
                        "physical_cores": 8,
                        "logical_threads": 16,
                        "ram_total_mib": 32768.0,
                    },
                    "system": {
                        "cpu_util_percent": 25.0,
                        "total_mib": 32768.0,
                        "available_mib": 24576.0,
                        "used_mib": 8192.0,
                    },
                    "processes": {
                        "sionna": {
                            "cpu_util_percent": 110.0,
                            "ram_rss_mib": 2048.0,
                        },
                        "gpu_channel": {
                            "cpu_util_percent": 45.0,
                            "ram_rss_mib": 256.0,
                        },
                    },
                }

        def fake_run(arguments: list[str], **_kwargs: object) -> mock.Mock:
            command = " ".join(arguments)
            if " pmon " in f" {command} ":
                output = (
                    "# gpu pid type sm mem fb command\n"
                    "# Idx # C/G % % MB name\n"
                    "0 1234 C 37 8 813 python3\n"
                    "0 4321 C 12 1 512 channel\n"
                )
            elif " dmon " in f" {command} ":
                output = "# gpu rxpci txpci\n# Idx MB/s MB/s\n0 12000 1500\n"
            else:
                output = "0, Test GPU, GPU-aaaa, 32607, 2048, 5, 4\n"
            return mock.Mock(returncode=0, stdout=output, stderr="")

        with mock.patch("server.subprocess.run", side_effect=fake_run), mock.patch(
            "server.probe_cuda_sm_counts", return_value=({0: 170}, None)
        ), mock.patch("server.process_is_alive", return_value=True):
            payload = sample_gpu_usage(
                {"sionna": {1234}, "gpu_channel": {4321}},
                host_sampler=FakeHostSampler(),  # type: ignore[arg-type]
            )
        self.assertTrue(payload["available"])
        self.assertEqual(payload["hardware"]["gpus"][0]["sm_count"], 170)
        self.assertEqual(payload["pcie"]["h2d_mb_s"], 12000.0)
        self.assertEqual(payload["processes"]["sionna"]["cpu_util_percent"], 110.0)
        self.assertEqual(payload["processes"]["sionna"]["ram_rss_mib"], 2048.0)
        self.assertNotIn("memory_util_percent", payload["processes"]["sionna"])
        self.assertEqual(payload["sampling"]["backend"], "nvidia-smi-fallback")

    def test_fast_resource_payload_uses_nvml_cadence_and_layout(self) -> None:
        host = {
            "hardware": {
                "model": "Test CPU",
                "physical_cores": 8,
                "logical_threads": 16,
                "ram_total_mib": 32768.0,
            },
            "system": {"cpu_util_percent": 20.0},
            "processes": {
                "sionna": {"cpu_util_percent": 25.0, "ram_rss_mib": 1024.0},
                "gpu_channel": {
                    "cpu_util_percent": 50.0,
                    "ram_rss_mib": 256.0,
                },
                "web_ui": {"cpu_util_percent": 10.0, "ram_rss_mib": 128.0},
            },
        }
        payload = build_resource_payload(
            {"sionna": {1234}, "gpu_channel": {4321}, "web_ui": {9999}},
            host,
            [
                {
                    "pid": 1234,
                    "gpu_index": 0,
                    "gpu_uuid": "GPU-aaaa",
                    "gpu_memory_mib": 1800.0,
                    "sm_util_percent": 35.0,
                },
                {
                    "pid": 4321,
                    "gpu_index": 0,
                    "gpu_uuid": "GPU-aaaa",
                    "gpu_memory_mib": 500.0,
                    "sm_util_percent": 15.0,
                },
            ],
            [
                {
                    "gpu_index": 0,
                    "gpu_uuid": "GPU-aaaa",
                    "name": "Test GPU",
                    "memory_total_mib": 32000.0,
                    "memory_used_mib": 2300.0,
                }
            ],
            {
                0: {
                    "gpu_util_percent": 60.0,
                    "h2d_mb_s": 2200.0,
                    "d2h_mb_s": 800.0,
                }
            },
            backend="nvml",
        )
        self.assertEqual(RESOURCE_SAMPLE_INTERVAL_SECONDS, 0.01)
        self.assertEqual(HARDWARE_REFRESH_INTERVAL_SECONDS, 1.0)
        self.assertEqual(payload["sampling"]["backend"], "nvml")
        self.assertEqual(payload["sampling"]["target_interval_ms"], 10)
        self.assertEqual(payload["sampling"]["hardware_interval_ms"], 1000)
        self.assertEqual(payload["processes"]["sionna"]["sm_util_percent"], 35.0)
        self.assertEqual(payload["processes"]["web_ui"]["cpu_util_percent"], 10.0)
        self.assertEqual(payload["gpu_compute"]["total_sm_util_percent"], 60.0)
        self.assertEqual(payload["gpu_compute"]["other_sm_util_percent"], 10.0)
        self.assertAlmostEqual(payload["system"]["busy_cpu_cores"], 3.2)
        self.assertAlmostEqual(payload["system"]["other_os_cpu_cores"], 2.35)
        self.assertEqual(payload["pcie"]["h2d_mb_s"], 2200.0)
        self.assertEqual(_NvmlUtilization.memory.offset, 4)
        self.assertEqual(_NvmlProcessInfo.used_gpu_memory.offset, 8)
        self.assertEqual(_NvmlProcessUtilizationSample.timestamp.offset, 8)

    def test_jsonl_tail_ignores_non_sionna_and_malformed_records(self) -> None:
        store = StatusStore()
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "status.jsonl"
            path.write_text(
                "not-json\n"
                + json.dumps({"event": "something_else"})
                + "\n"
                + json.dumps({"event": "sionna_rt_update", "iteration": 9})
                + "\n",
                encoding="utf-8",
            )
            SionnaJsonlTail(path, store).poll_once()
        self.assertEqual(store.snapshot()["sionna"]["iteration"], 9)

    def test_jsonl_tail_starts_from_recent_window_of_large_file(self) -> None:
        store = StatusStore()
        old = json.dumps(
            {
                "event": "sionna_rt_update",
                "session_id": "old-session",
                "iteration": 999,
                "padding": "x" * 1024,
            }
        ) + "\n"
        recent = (
            json.dumps(
                {
                    "event": "sionna_rt_runtime",
                    "session_id": "current-session",
                    "process_id": os.getpid(),
                    "phase": "tracing_channels",
                    "iteration": 4,
                    "observed_unix_ms": 4_000,
                }
            )
            + "\n"
            + json.dumps(
                {
                    "event": "sionna_rt_update",
                    "session_id": "current-session",
                    "iteration": 4,
                    "update_started_unix_ms": 4_000,
                    "control_ack_unix_ms": 4_050,
                }
            )
            + "\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "large-status.jsonl"
            path.write_text(old + recent, encoding="utf-8")
            SionnaJsonlTail(
                path, store, initial_tail_bytes=len(recent.encode("utf-8")) + 20
            ).poll_once()
        store.update_gpu_usage(
            {"sampled_unix_ms": 4_060, "processes": {}, "system": {}, "pcie": {}}
        )
        snapshot = store.snapshot()
        self.assertEqual(snapshot["sionna"]["session_id"], "current-session")
        self.assertEqual(snapshot["sionna"]["iteration"], 4)
        self.assertEqual(
            {item["session_id"] for item in snapshot["gpu_usage"]["iterations"]},
            {"current-session"},
        )

    def test_new_sionna_session_clears_previous_timeline(self) -> None:
        store = StatusStore()
        for session_id, iteration, observed in (
            ("old-session", 99, 1_000),
            ("new-session", 1, 2_000),
        ):
            store.update_sionna(
                {
                    "event": "sionna_rt_runtime",
                    "session_id": session_id,
                    "process_id": os.getpid(),
                    "phase": "tracing_channels",
                    "iteration": iteration,
                    "observed_unix_ms": observed,
                }
            )
        store.update_gpu_usage(
            {"sampled_unix_ms": 2_010, "processes": {}, "system": {}, "pcie": {}}
        )
        iterations = store.snapshot()["gpu_usage"]["iterations"]
        self.assertEqual(len(iterations), 1)
        self.assertEqual(iterations[0]["session_id"], "new-session")
        self.assertEqual(iterations[0]["iteration"], 1)

    def test_delivery_requires_matching_ack_and_backend_seqnos(self) -> None:
        sionna = {
            "event": "sionna_rt_update",
            "batch_id": "sionna-4",
            "channels": [{"link_id": "gnb0>ue0:sionna_rt"}],
            "control_reply": {
                "ok": True,
                "batch_id": "sionna-4",
                "backend": "cuda",
                "link_count": 1,
                "links": [
                    {"link_id": "gnb0>ue0:sionna_rt", "seqno": 12}
                ],
            },
        }
        waiting = delivery_status(
            sionna,
            {
                "gnb0>ue0:sionna_rt": {
                    "seqno": 11,
                    "slot": 90,
                    "backend": "cuda",
                    "profile_active": True,
                    "matrix_profile_active": True,
                    "array": {"nt": 2, "nr": 1},
                }
            },
        )
        self.assertTrue(waiting["control_received"])
        self.assertFalse(waiting["backend_applied"])
        self.assertEqual(waiting["state"], "awaiting_backend_telemetry")

        confirmed = delivery_status(
            sionna,
            {
                "gnb0>ue0:sionna_rt": {
                    "seqno": 12,
                    "slot": 91,
                    "backend": "cuda",
                    "profile_active": True,
                    "matrix_profile_active": True,
                    "array": {"nt": 2, "nr": 1},
                }
            },
        )
        self.assertTrue(confirmed["backend_applied"])
        self.assertEqual(confirmed["backend_applied_links"], 1)
        self.assertEqual(confirmed["links"][0]["observed_slot"], 91)
        self.assertTrue(confirmed["links"][0]["matrix_profile_active"])
        self.assertEqual(confirmed["links"][0]["array"], {"nt": 2, "nr": 1})

    def test_delivery_does_not_call_dry_run_received(self) -> None:
        status = delivery_status(
            {
                "event": "sionna_rt_update",
                "batch_id": "sionna-0",
                "control_reply": {"ok": True, "dry_run": True},
            },
            {},
        )
        self.assertEqual(status["state"], "dry_run")
        self.assertFalse(status["control_received"])

    def test_delivery_distinguishes_applied_warmup_from_usable(self) -> None:
        sionna = {
            "event": "sionna_rt_update",
            "batch_id": "sionna-warmup",
            "channels": [{"link_id": "gnb0>ue0:sionna_rt"}],
            "control_reply": {
                "ok": True,
                "batch_id": "sionna-warmup",
                "backend": "cuda",
                "link_count": 1,
                "links": [
                    {
                        "link_id": "gnb0>ue0:sionna_rt",
                        "seqno": 13,
                        "warmup_until_slot": 92,
                    }
                ],
            },
        }
        warming = delivery_status(
            sionna,
            {
                "gnb0>ue0:sionna_rt": {
                    "seqno": 13,
                    "slot": 91,
                    "backend": "cuda",
                    "profile_active": True,
                    "warmup_until_slot": 92,
                }
            },
        )
        self.assertTrue(warming["backend_applied"])
        self.assertFalse(warming["backend_usable"])
        self.assertEqual(warming["state"], "backend_warmup")
        self.assertEqual(warming["warmup_links"], 1)
        self.assertTrue(warming["links"][0]["warming_up"])

        usable = delivery_status(
            sionna,
            {
                "gnb0>ue0:sionna_rt": {
                    "seqno": 13,
                    "slot": 92,
                    "backend": "cuda",
                    "profile_active": True,
                    "warmup_until_slot": 0,
                }
            },
        )
        self.assertTrue(usable["backend_usable"])
        self.assertEqual(usable["state"], "backend_applied")


if __name__ == "__main__":
    unittest.main()
