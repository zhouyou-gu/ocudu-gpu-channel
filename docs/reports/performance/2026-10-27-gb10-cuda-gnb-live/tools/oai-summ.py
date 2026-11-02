#!/usr/bin/env python3
"""One line per lead run dir: gate result, real-time ratio (median of per-second broker heartbeat rates on gnb0 once
the cell is up), rank-summary realtime_factor / health / iperf where the 2x2 gate wrote one."""
import json, re, statistics, sys, pathlib
R = pathlib.Path("/workspace/ocudu-spark/results")
for d in map(pathlib.Path, sys.argv[1:]):
    meta = (d / "meta.txt").read_text() if (d / "meta.txt").is_file() else ""
    m = re.search(r"run=(oai-[12]x[12]/\d+T\d+Z)", meta); run = m.group(1) if m else None
    gate = (d / "gate.log").read_text(errors="replace") if (d / "gate.log").is_file() else ""
    res = re.findall(r"attach_gate result=([a-z]+)", gate); res = res[-1] if res else "-"
    out = {"rt_hb": None, "srate": None, "gnb": "?", "mps": None, "rt_rank": None, "health": None, "dl_air": None, "iperf_dl": None}
    if run:
        logs, rep = R / "logs" / run, R / "reports" / run
        topo = next((p for p in (rep / "configs" / "topology.yaml", rep / "topology.yaml") if p.is_file()), None)
        if topo:
            s = re.search(r"sample_rate_hz:\s*(\d+)", topo.read_text()); out["srate"] = int(s.group(1)) if s else None
        hb = []
        for f in sorted(logs.glob("*broker*.log")):
            for line in f.read_text(errors="replace").splitlines():
                if "event=heartbeat" in line and "dev=gnb0" in line:
                    t = re.search(r"\bt=(\d+)", line); a = re.search(r"acquired=(\d+)", line)
                    if t and a: hb.append((int(t.group(1)), int(a.group(1))))
        if len(hb) > 3 and out["srate"]:
            rates = [(hb[i][1] - hb[i-1][1]) / (hb[i][0] - hb[i-1][0]) / out["srate"] for i in range(1, len(hb)) if hb[i][0] > hb[i-1][0]]
            up = [i for i, r in enumerate(rates) if r > 0.5]
            if up:
                steady = rates[up[0] + 2:] if len(rates) > up[0] + 4 else rates[up[0]:]
                out["rt_hb"] = round(statistics.median(steady), 3); out["n_s"] = len(steady)
        rs = rep / "rank-summary.json"
        if rs.is_file():
            j = json.load(open(rs)); out["rt_rank"] = j.get("realtime_factor"); out["health"] = j.get("health")
            out["dl_air"] = j.get("dl_rx_mbps_air_time"); out["iperf_dl"] = j.get("iperf_dl")
        for name in ("run-params.json",):
            if (rep / name).is_file():
                j = json.load(open(rep / name)); mp = j.get("mps") or {}
                out["mps"] = mp.get("active"); out["gnb"] = "cuda" if mp.get("gnb_uses_cuda") else "cpu"
        if out["gnb"] == "?":
            out["gnb"] = "cuda" if "gnb=cuda" in meta else ("cpu" if "gnb=cpu" in meta else "?")
    def short(v):
        s = json.dumps(v) if not isinstance(v, str) else v
        return s if len(s) < 90 else s[:90] + "…"
    print(f"{d.name:22s} {res:5s} gnb={out['gnb']} mps={out['mps']} srate={out['srate']} rt_hb={out['rt_hb']} rt_rank={out['rt_rank']} "
          f"health={short(out['health'])} dl_air={short(out['dl_air'])} iperf_dl={short(out['iperf_dl'])} run={run}")
