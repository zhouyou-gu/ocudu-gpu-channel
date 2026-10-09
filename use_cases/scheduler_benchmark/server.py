#!/usr/bin/env python3
"""Scheduler-benchmark web UI server.

Serves use_cases/scheduler_benchmark/index.html and a read-only JSON API over
the benchmark runs under <runs-root>/results/logs/ocudu-scheduler-benchmark:

  /api/runs                 runs, newest first
  /api/run?id=TS            the run's configuration (benchmark.json, scenario,
                            run parameters, gate summary, software versions)
  /api/report?id=TS         metrics: the saved final report, or a live
                            analysis of the logs so far (refreshed <= 1/s)
  /api/live?id=TS           live extras: resource grid, Sionna timeline tail
  /api/scene_mesh?id=TS     the Sionna scene triangles (bridge sidecar file)
  /api/system               this host's GPU and CPU, and the run's processes

It only reads files and /proc; it never signals or reconfigures a process.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Sequence

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import analyze  # noqa: E402
import definitions as bench  # noqa: E402

VENDOR = HERE.parents[1] / "apps" / "dashboard" / "vendor"
INDEX = HERE / "index.html"
RUN_ID = re.compile(r"^\d{8}T\d{6}Z$")
REPORT_REFRESH_S = 1.0
SYSTEM_REFRESH_S = 1.0
# Processes the system panel attributes to a cell, by command line.
PROCESS_PATTERNS = (
    ("gnb0", re.compile(r"/gnb\b.*\bgnb0\.yaml")),
    ("gnb1", re.compile(r"/gnb\b.*\bgnb1\.yaml")),
    ("broker a", re.compile(r"ocudu-gpu-channel\b.*topology-a\.yaml")),
    ("broker b", re.compile(r"ocudu-gpu-channel\b.*topology-b\.yaml")),
    ("ue0", re.compile(r"srsue\b.*srsue-ue0\.conf")),
    ("ue1", re.compile(r"srsue\b.*srsue-ue1\.conf")),
    ("ue2", re.compile(r"srsue\b.*srsue-ue2\.conf")),
    ("ue3", re.compile(r"srsue\b.*srsue-ue3\.conf")),
    ("sionna", re.compile(r"run_bridge\.py\b.*--timeline grid")),
    ("traffic", re.compile(r"scheduler_benchmark/traffic\.py\b")),
)
PROCESS_CELL = {"gnb0": "a", "broker a": "a", "ue0": "a", "ue1": "a",
                "gnb1": "b", "broker b": "b", "ue2": "b", "ue3": "b"}


def read_json(path: pathlib.Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


class Runs:
    def __init__(self, root: pathlib.Path) -> None:
        self.root = root
        self.logs = root / "results/logs/ocudu-scheduler-benchmark"
        self.reports = root / "results/reports/ocudu-scheduler-benchmark"
        self.configs = root / "configs/ocudu-scheduler-benchmark-native"
        self.analyzers: dict[str, tuple[analyze.RunAnalyzer, float, dict[str, Any] | None]] = {}
        self.lock = threading.Lock()

    def ids(self) -> list[str]:
        if not self.logs.is_dir():
            return []
        return sorted((p.name for p in self.logs.iterdir() if RUN_ID.match(p.name)), reverse=True)

    def paths(self, run_id: str) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
        if not RUN_ID.match(run_id or ""):
            raise KeyError("bad run id")
        return self.logs / run_id, self.configs / run_id, self.reports / run_id

    def is_live(self, run_id: str) -> bool:
        log_dir, _, report_dir = self.paths(run_id)
        if (report_dir / "benchmark-report.json").exists():
            return False
        status = log_dir / "sionna-status.jsonl"
        return status.exists() and time.time() - status.stat().st_mtime < 30

    def summary(self, run_id: str) -> dict[str, Any]:
        log_dir, config_dir, report_dir = self.paths(run_id)
        params = read_json(report_dir / "run-parameters.json") or {}
        gate = read_json(report_dir / "gate-summary.json") or {}
        live = self.is_live(run_id)
        has_report = (report_dir / "benchmark-report.json").exists()
        return {
            "id": run_id, "seed": params.get("seed"), "schedulers": params.get("schedulers"),
            "profile": params.get("traffic_profile"), "measure_seconds": params.get("measure_seconds"),
            "state": "live" if live else ("complete" if has_report else ("failed" if gate else "incomplete")),
            "gate_status": gate.get("status"),
            "configured": (config_dir / "benchmark.json").exists(),
        }

    def run(self, run_id: str) -> dict[str, Any]:
        log_dir, config_dir, report_dir = self.paths(run_id)
        benchmark = read_json(config_dir / "benchmark.json")
        if benchmark is None:
            raise KeyError("run has no benchmark.json yet")
        scenario = read_json(config_dir / "scenario.json") or {}
        environment = None
        status = log_dir / "sionna-status.jsonl"
        if status.exists():
            with status.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if '"sionna_rt_update"' in line:
                        try:
                            environment = json.loads(line).get("environment")
                        except json.JSONDecodeError:
                            pass
                        break
        return {
            "id": run_id,
            "benchmark": benchmark,
            "scenario": scenario,
            "parameters": read_json(report_dir / "run-parameters.json"),
            "gate": read_json(report_dir / "gate-summary.json"),
            "policy_check": read_json(log_dir / "policy-check.json"),
            "sionna_environment": environment,
            "schedulers": bench.SCHEDULERS,
            "five_qi": bench.FIVE_QI,
            "software": {
                "ocudu_gnb": "OCUDU gNB a1916edcdb (pinned)",
                "srsue": "srsRAN_4G eea87b1d89 + repository RACH patch (zhouyou-gu fork)",
                "open5gs": "Open5GS v2.7.6 (d9d3abdd48)",
                "sionna": "Sionna RT PathSolver (bridge apps/sionna_bridge/run_bridge.py)",
            },
            "live": self.is_live(run_id),
        }

    def analyzer(self, run_id: str) -> tuple[analyze.RunAnalyzer, dict[str, Any]]:
        log_dir, config_dir, report_dir = self.paths(run_id)
        with self.lock:
            entry = self.analyzers.get(run_id)
            if entry is None:
                entry = (analyze.RunAnalyzer(log_dir, config_dir), 0.0, None)
            analyzer, stamp, report = entry
            if report is None or time.monotonic() - stamp >= REPORT_REFRESH_S:
                analyzer.poll()
                saved = read_json(report_dir / "benchmark-report.json")
                report = saved if saved is not None else analyzer.report(live=self.is_live(run_id))
                stamp = time.monotonic()
            self.analyzers[run_id] = (analyzer, stamp, report)
            # Keep memory bounded: only the runs viewed recently stay parsed.
            if len(self.analyzers) > 3:
                oldest = min(self.analyzers, key=lambda key: self.analyzers[key][1])
                if oldest != run_id:
                    del self.analyzers[oldest]
            return analyzer, report

    def report(self, run_id: str) -> dict[str, Any]:
        return self.analyzer(run_id)[1]

    def live(self, run_id: str, since: int) -> dict[str, Any]:
        analyzer, report = self.analyzer(run_id)
        with self.lock:
            timeline = [s for s in analyzer.timeline() if (s["grid_index"] or 0) > since]
            return {
                "grid": analyzer.resource_grid(),
                "timeline": timeline[-600:],
                "progress": {key: report.get(key) for key in (
                    "segments_complete", "segments_valid", "measure_seconds", "anchor_unix_ms", "live")},
            }

    def scene_mesh(self, run_id: str) -> bytes:
        log_dir, _, _ = self.paths(run_id)
        path = log_dir / "sionna-status-scene-mesh.json"
        return path.read_bytes()


class SystemSampler:
    """GPU via nvidia-smi, CPU via /proc; refreshed on a background thread."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.static = self._static()
        self.prev_cpu: dict[int, tuple[int, int]] = {}
        self.prev_proc: dict[int, tuple[int, float]] = {}
        self.lock = threading.Lock()

    def _static(self) -> dict[str, Any]:
        out: dict[str, Any] = {"cpus": []}
        try:
            # No CLUSTER column: on aarch64 lscpu prints it as a bare `-`, which is not JSON.
            info = json.loads(subprocess.run(["lscpu", "-J", "-e=CPU,CORE,MAXMHZ,MODELNAME"],
                                             capture_output=True, text=True, timeout=5).stdout or "{}")
            for row in info.get("cpus", []):
                out["cpus"].append({"cpu": int(row.get("cpu")), "model": row.get("modelname"),
                                    "max_mhz": row.get("maxmhz")})
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        models: dict[str, list[int]] = {}
        for cpu in out["cpus"]:
            models.setdefault(cpu["model"] or "unknown", []).append(cpu["cpu"])
        out["cpu_models"] = [{"model": model, "cpus": cpus, "count": len(cpus)} for model, cpus in models.items()]
        try:
            text = subprocess.run(["nvidia-smi"], capture_output=True, text=True, timeout=5).stdout
            match = re.search(r"CUDA Version:\s*([0-9.]+)", text)
            out["cuda_version"] = match.group(1) if match else None
        except (OSError, subprocess.SubprocessError):
            out["cuda_version"] = None
        try:
            out["kernel"] = os.uname().release
            out["hostname"] = os.uname().nodename
        except OSError:
            pass
        mem = self._meminfo()
        out["memory_total_gib"] = mem.get("MemTotal", 0) / 1024 / 1024 if mem else None
        return out

    @staticmethod
    def _meminfo() -> dict[str, int]:
        values = {}
        try:
            for line in pathlib.Path("/proc/meminfo").read_text().splitlines():
                key, _, rest = line.partition(":")
                values[key] = int(rest.split()[0])
        except (OSError, ValueError, IndexError):
            pass
        return values

    def _gpu(self) -> list[dict[str, Any]]:
        fields = ("name", "driver_version", "utilization.gpu", "memory.used", "memory.total", "temperature.gpu",
                  "power.draw", "clocks.sm", "clocks.max.sm")
        try:
            text = subprocess.run(["nvidia-smi", f"--query-gpu={','.join(fields)}", "--format=csv,noheader,nounits"],
                                  capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        gpus = []
        for line in text.strip().splitlines():
            values = [v.strip() for v in line.split(",")]
            row: dict[str, Any] = {}
            for key, value in zip(fields, values):
                try:
                    row[key] = float(value) if key != "name" and key != "driver_version" else value
                except ValueError:
                    row[key] = None  # "[N/A]": GB10 has no dedicated GPU memory to report
            gpus.append(row)
        return gpus

    def _cpu_usage(self) -> list[float | None]:
        usage: list[float | None] = []
        try:
            lines = pathlib.Path("/proc/stat").read_text().splitlines()
        except OSError:
            return usage
        for line in lines:
            match = re.match(r"^cpu(\d+)\s+(.*)$", line)
            if not match:
                continue
            cpu = int(match.group(1))
            values = [int(v) for v in match.group(2).split()]
            idle = values[3] + (values[4] if len(values) > 4 else 0)
            total = sum(values[:8])
            prev = self.prev_cpu.get(cpu)
            self.prev_cpu[cpu] = (idle, total)
            while len(usage) <= cpu:
                usage.append(None)
            if prev and total > prev[1]:
                usage[cpu] = 100.0 * (1.0 - (idle - prev[0]) / (total - prev[1]))
        return usage

    def _processes(self) -> list[dict[str, Any]]:
        ticks = os.sysconf("SC_CLK_TCK")
        now = time.monotonic()
        found = []
        seen = set()
        for entry in pathlib.Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            try:
                cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            except OSError:
                continue
            label = next((name for name, pattern in PROCESS_PATTERNS if pattern.search(cmdline)), None)
            if label is None or "nsenter" in cmdline.split(" ")[0] or "taskset" in cmdline.split(" ")[0]:
                continue
            try:
                stat = (entry / "stat").read_text()
                fields = stat[stat.rfind(")") + 2:].split()
                cpu_ticks = int(fields[11]) + int(fields[12])
                status = (entry / "status").read_text()
                allowed = re.search(r"Cpus_allowed_list:\s*(\S+)", status)
                threads = re.search(r"Threads:\s*(\d+)", status)
            except (OSError, ValueError, IndexError):
                continue
            seen.add(pid)
            prev = self.prev_proc.get(pid)
            self.prev_proc[pid] = (cpu_ticks, now)
            cpu = None
            if prev and now > prev[1]:
                cpu = 100.0 * (cpu_ticks - prev[0]) / ticks / (now - prev[1])
            found.append({"label": label, "pid": pid, "cell": PROCESS_CELL.get(label), "cpu_percent": cpu,
                          "cpus_allowed": allowed.group(1) if allowed else None,
                          "threads": int(threads.group(1)) if threads else None})
        for pid in list(self.prev_proc):
            if pid not in seen:
                del self.prev_proc[pid]
        # The traffic tool runs one process per role; show them as one row.
        merged: dict[str, dict[str, Any]] = {}
        for row in found:
            if row["label"] == "traffic":
                agg = merged.setdefault("traffic", {**row, "pid": None, "cpu_percent": 0.0, "count": 0})
                agg["cpu_percent"] += row["cpu_percent"] or 0.0
                agg["count"] += 1
            else:
                merged.setdefault(f"{row['label']}#{row['pid']}", row)
        order = [name for name, _ in PROCESS_PATTERNS]
        return sorted(merged.values(), key=lambda r: order.index(r["label"]))

    def sample(self) -> None:
        mem = self._meminfo()
        try:
            load = [float(v) for v in pathlib.Path("/proc/loadavg").read_text().split()[:3]]
        except (OSError, ValueError):
            load = []
        data = {
            "time_unix_ms": int(time.time() * 1000),
            "static": self.static,
            "gpus": self._gpu(),
            "cpu_percent": self._cpu_usage(),
            "load": load,
            "memory_used_gib": (mem.get("MemTotal", 0) - mem.get("MemAvailable", 0)) / 1024 / 1024 if mem else None,
            "processes": self._processes(),
        }
        with self.lock:
            self.data = data

    def loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.sample()
            except Exception as error:  # the panel shows the error rather than the server dying
                with self.lock:
                    self.data = {"error": f"{type(error).__name__}: {error}", "static": self.static}
            stop.wait(SYSTEM_REFRESH_S)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return dict(self.data)


def make_handler(runs: Runs, system: SystemSampler, follow_latest: bool):
    index_bytes = INDEX.read_bytes()

    class Handler(BaseHTTPRequestHandler):
        server_version = "ocudu-scheduler-benchmark/1"

        def log_message(self, fmt: str, *args: Any) -> None:  # quiet
            return

        def send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
            body = json.dumps(payload, separators=(",", ":"), default=analyze._json_default).encode()
            self.send(status, body, "application/json")

        def do_GET(self) -> None:  # noqa: N802
            url = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(url.query)
            run_id = (query.get("id") or [""])[0]
            try:
                if url.path in ("/", "/index.html"):
                    body = INDEX.read_bytes() if os.environ.get("SB_UI_DEV") else index_bytes
                    return self.send(HTTPStatus.OK, body, "text/html; charset=utf-8")
                if url.path.startswith("/vendor/"):
                    name = url.path[len("/vendor/"):]
                    path = (VENDOR / name).resolve()
                    if path.parent != VENDOR.resolve() or path.suffix != ".js" or not path.is_file():
                        return self.send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
                    return self.send(HTTPStatus.OK, path.read_bytes(), "text/javascript; charset=utf-8")
                if url.path == "/api/runs":
                    ids = runs.ids()
                    return self.send_json({"runs": [runs.summary(i) for i in ids[:50]],
                                           "follow_latest": follow_latest})
                if url.path == "/api/system":
                    return self.send_json(system.snapshot())
                if not run_id and follow_latest:
                    ids = runs.ids()
                    run_id = ids[0] if ids else ""
                if url.path == "/api/run":
                    return self.send_json(runs.run(run_id))
                if url.path == "/api/report":
                    return self.send_json(runs.report(run_id))
                if url.path == "/api/live":
                    since = int((query.get("since") or ["-1"])[0])
                    return self.send_json(runs.live(run_id, since))
                if url.path == "/api/scene_mesh":
                    return self.send(HTTPStatus.OK, runs.scene_mesh(run_id), "application/json")
            except KeyError as error:
                return self.send_json({"error": str(error)}, HTTPStatus.NOT_FOUND)
            except FileNotFoundError:
                return self.send_json({"error": "not available yet"}, HTTPStatus.NOT_FOUND)
            except Exception as error:  # report, do not crash the server thread silently
                return self.send_json({"error": f"{type(error).__name__}: {error}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            self.send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")

    return Handler


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--runs-root", type=pathlib.Path, default=pathlib.Path(os.environ.get(
        "OCUDU_NATIVE_ROOT", "/home/dev/ocudu-spark")))
    parser.add_argument("--follow-latest", action="store_true",
                        help="requests without ?id= use the newest run")
    args = parser.parse_args(argv)
    runs = Runs(args.runs_root)
    system = SystemSampler()
    stop = threading.Event()
    threading.Thread(target=system.loop, args=(stop,), daemon=True).start()
    server = ThreadingHTTPServer((args.bind, args.port), make_handler(runs, system, args.follow_latest))
    print(f"event=scheduler_benchmark_ui url=http://{args.bind}:{args.port} runs_root={args.runs_root}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
