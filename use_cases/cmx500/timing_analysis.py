#!/usr/bin/env python3
"""Timing-margin statistics of one run_cmx_loop.sh run made with TIMING=1.

Usage: timing_analysis.py RUN_DIR [--json OUT.json]

Reads the bridge CSVs (ul_blocks, dl_requests, tx_events, rx_lag), broker.log, srsue.log and metrics.csv of the run
and prints percentiles of every stage that bounds the UL timing margin. All times in microseconds.
"""
import csv
import json
import os
import re
import statistics
import sys

run = sys.argv[1].rstrip("/")
out_json = sys.argv[sys.argv.index("--json") + 1] if "--json" in sys.argv else None
MIN_LEAD_US = 200.0
PCT = (1, 5, 10, 50, 90, 95, 99)
# UHD async event codes
EVENTS = {0x2: "underflow", 0x4: "seq_error", 0x8: "late", 0x10: "underflow_in_packet", 0x20: "seq_error_in_burst"}


def pct(values, ps=PCT):
    v = sorted(values)
    if not v:
        return {}
    res = {f"p{p}": round(v[min(len(v) - 1, int(round(p / 100.0 * (len(v) - 1))))], 1) for p in ps}
    res.update({"min": round(v[0], 1), "max": round(v[-1], 1), "mean": round(statistics.mean(v), 1), "n": len(v)})
    return res


def read_csv(name):
    path = os.path.join(run, name)
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return list(csv.DictReader(f))


res = {"run": os.path.basename(run)}

# ---- UL blocks: margin at the bridge ----
ul = read_csv("ul_blocks.csv")
if ul:
    t_first = int(ul[0]["wall_us"])
    # skip the start-up transient (DL catch-up, first UL padding): from the first data block on, plus 2 s
    first_data = next((int(r["wall_us"]) for r in ul if r["has_data"] == "1"), t_first)
    steady = [r for r in ul if int(r["wall_us"]) >= first_data + 2_000_000]
    data = [r for r in steady if r["has_data"] == "1"]
    zero = [r for r in steady if r["has_data"] == "0"]
    lead = [float(r["lead_us"]) for r in data]
    res["ul_data_blocks"] = len(data)
    res["ul_data_lead_us"] = pct(lead)
    res["ul_data_ahead_us"] = pct([float(r["ahead_us"]) for r in data])
    res["ul_zero_lead_us"] = pct([float(r["lead_us"]) for r in zero])
    res["ul_data_late_pct"] = round(100.0 * sum(1 for x in lead if x < MIN_LEAD_US) / len(lead), 2) if lead else None
    res["ul_data_negative_pct"] = round(100.0 * sum(1 for x in lead if x < 0) / len(lead), 2) if lead else None
    cut = [int(r["cut"]) for r in data]
    n = [int(r["n"]) for r in data]
    res["ul_data_blocks_cut_pct"] = round(100.0 * sum(1 for c in cut if c > 0) / len(cut), 2) if cut else None
    res["ul_data_samples_cut_pct"] = round(100.0 * sum(cut) / sum(n), 2) if n else None
    res["ul_data_dropped_pct"] = (
        round(100.0 * sum(1 for c, k in zip(cut, n) if c >= k) / len(cut), 2) if cut else None)
    send = [float(r["send_us"]) for r in data if r["sent"] == "1"]
    res["uhd_send_us"] = pct(send)
    res["steady_seconds"] = round((int(steady[-1]["wall_us"]) - int(steady[0]["wall_us"])) / 1e6, 1) if steady else 0
    # per-second series of the data-block margin, for plotting
    series = {}
    for r in data:
        s = (int(r["wall_us"]) - first_data) // 1_000_000
        series.setdefault(s, []).append(float(r["lead_us"]))
    res["ul_lead_per_second"] = [
        {"s": s, "p5": pct(v, (5,))["p5"], "p50": pct(v, (50,))["p50"], "late_pct": round(100.0 * sum(1 for x in v if x < MIN_LEAD_US) / len(v), 1)}
        for s, v in sorted(series.items())]
    # histogram of the data-block margin, 250 us bins
    bins = {}
    for x in lead:
        b = int((x // 250) * 250)
        bins[b] = bins.get(b, 0) + 1
    res["ul_lead_histogram_250us"] = sorted(bins.items())

# ---- DL requests: backlog in the bridge when the broker asks for the next block ----
dl = read_csv("dl_requests.csv")
if dl:
    t0 = int(dl[0]["wall_us"])
    dls = [r for r in dl if int(r["wall_us"]) >= t0 + 3_000_000]
    res["dl_backlog_us"] = pct([float(r["backlog_us"]) for r in dls])
    res["dl_jumps"] = sum(int(r["jumped"]) for r in dl)
    res["dl_requests_per_s"] = round(len(dls) / max(1e-9, (int(dls[-1]["wall_us"]) - int(dls[0]["wall_us"])) / 1e6), 1) if dls else 0

# ---- USRP clock vs newest RX sample ----
lag = read_csv("rx_lag.csv")
if lag:
    res["rx_lag_min_us"] = pct([float(r["lag_min_us"]) for r in lag[2:]])
    res["rx_lag_max_us"] = pct([float(r["lag_max_us"]) for r in lag[2:]])

# ---- USRP TX async events ----
ev = read_csv("tx_events.csv")
counts = {}
for r in ev:
    name = EVENTS.get(int(r["code"]), f"code_{r['code']}")
    counts[name] = counts.get(name, 0) + 1
res["tx_events"] = counts

# ---- broker ----
try:
    blog = open(os.path.join(run, "broker.log")).read()
    for m in re.finditer(r"event=process_latency_summary node=(\S+) n=(\d+) p50_us=(\d+) p95_us=(\d+) p99_us=(\d+) "
                         r"p999_us=(\d+) max_us=(\d+)", blog):
        res[f"broker_process_us_{m.group(1)}"] = dict(zip(("n", "p50", "p95", "p99", "p999", "max"),
                                                          map(int, m.groups()[1:])))

    def stage_means(pattern, keys):
        rows = [m.groupdict() for m in re.finditer(pattern, blog)]
        return {k: round(statistics.mean(float(r[k]) for r in rows), 1) for k in keys} if rows else {}

    for node in ("gnb0", "ue0"):
        res[f"broker_cpu_stage_mean_us_{node}"] = stage_means(
            rf"event=cpu_stage_timings t=\d+ node={node} room_us=(?P<room>[\d.]+) align_us=[\d.]+ data_us=(?P<data>[\d.]+) "
            r"read_us=(?P<read>[\d.]+) process_us=(?P<process>[\d.]+) throttle_us=[\d.]+ push_us=(?P<push>[\d.]+)",
            ("room", "data", "read", "process", "push"))
        res[f"broker_rep_stage_mean_us_{node}"] = stage_means(
            rf"event=rep_stage_timings t=\d+ dev={node} wait_req_us=(?P<wait_req>[\d.]+) pop_us=(?P<pop>[\d.]+) "
            r"send_us=(?P<send>[\d.]+)", ("wait_req", "pop", "send"))
        res[f"broker_rx_ring_added_mean_us_{node}"] = stage_means(
            rf"event=rx_ring t=\d+ dev={node} occupancy=\d+ peak=\d+ capacity=\d+ high_water=\d+ "
            r"added_one_way_us=(?P<added>[\d.]+)", ("added",)).get("added")
    res["gpu_mean_us"] = stage_means(
        r"event=gpu_timings t=\d+ h2d_us=(?P<h2d>[\d.]+) kernel_us=(?P<kernel>[\d.]+) d2h_us=(?P<d2h>[\d.]+)",
        ("h2d", "kernel", "d2h"))
except OSError:
    pass

# ---- srsUE ----
try:
    ulog = open(os.path.join(run, "srsue.log"), errors="replace").read()
    pdsch_t = [float(x) for x in re.findall(r"PDSCH: cc=0 pid=\d+ c-rnti=\S+ .*? t_us=(\d+)", ulog)]
    pusch_t = [float(x) for x in re.findall(r"PUSCH: cc=0 .*? t=(\d+) us", ulog)]
    if pdsch_t:
        res["ue_pdsch_decode_us"] = pct(pdsch_t)
    if pusch_t:
        res["ue_pusch_encode_us"] = pct(pusch_t)
    res["ue_slot_resyncs"] = ulog.count("resynchronised to")
    res["ue_out_of_sync"] = ulog.count("out-of-sync")
    res["ue_registered"] = "Handling Registration Accept" in ulog
except OSError:
    pass
mrows = []
try:
    with open(os.path.join(run, "metrics.csv")) as f:
        mrows = [r for r in csv.DictReader((l for l in f if not l.startswith("#")), delimiter=";")]
except OSError:
    pass
conn = [r for r in mrows if float(r.get("dl_brate") or 0) > 1e6]
if conn:
    for k in ("dl_snr", "dl_mcs", "dl_bler", "ul_mcs", "ul_bler", "dl_brate", "ul_brate"):
        res[f"ue_metric_{k}_mean"] = round(statistics.mean(float(r[k]) for r in conn), 3)
    res["ue_connected_seconds"] = len(conn)

if out_json:
    with open(out_json, "w") as f:
        json.dump(res, f, indent=1)
for k, v in res.items():
    if k in ("ul_lead_per_second", "ul_lead_histogram_250us"):
        continue
    print(f"{k}: {v}")
