#!/usr/bin/env python3
"""One line per campaign run (runs.tsv): attach status per UE and the real-time ratio from the broker heartbeats on the gNB port."""
import json, re, statistics, sys, pathlib
L = pathlib.Path("/workspace/gpuch/bound/oaimue"); R = pathlib.Path("/workspace/ocudu-spark")
for line in (L / "runs.tsv").read_text().splitlines():
    tag, bw, n, gnb, rc, ts = line.split("\t")
    out = f"{tag:12s} bw={bw:>3s} ue={n} gnb={gnb:4s} rc={rc}"
    if ts == "-": print(out + " (no report)"); continue
    rep, logs, cfg = R / "results/reports/ocudu-oai-multi-ue" / ts, R / "results/logs/ocudu-oai-multi-ue" / ts, R / "configs/ocudu-oai-multi-ue-native" / ts
    try: a = json.load(open(rep / "attach-summary.json")); st = a.get("status"); per = a.get("per_ue", {})
    except Exception: st = "?"; per = {}
    ues = " ".join(f"{u}:{'A' if v.get('pdu_session_established') else '-'}{'P' if v.get('ping_ok') else '-'}" for u, v in sorted(per.items()))
    srate = None
    if (cfg / "topology.yaml").is_file():
        m = re.search(r"sample_rate_hz:\s*(\d+)", (cfg / "topology.yaml").read_text()); srate = int(m.group(1)) if m else None
    hb = []
    if (logs / "broker.log").is_file():
        for l in open(logs / "broker.log", errors="replace"):
            if "event=heartbeat" in l and ("dev=gnb0_p0" in l or "dev=gnb0 " in l):
                t = re.search(r"\bt=(\d+)", l); q = re.search(r"acquired=(\d+)", l)
                if t and q: hb.append((int(t.group(1)), int(q.group(1))))
    rt = None
    if len(hb) > 5 and srate:
        rates = [(hb[i][1]-hb[i-1][1])/(hb[i][0]-hb[i-1][0])/srate for i in range(1, len(hb)) if hb[i][0] > hb[i-1][0]]
        up = [i for i, r in enumerate(rates) if r > 0.3]
        if up:
            steady = rates[up[0]+5:] if len(rates) > up[0]+10 else rates[up[0]:]
            rt = (round(statistics.median(steady), 3), round(min(steady), 3), len(steady))
    starv = a.get("rx_starvations") if isinstance(a, dict) else None
    print(out + f" status={st} ues[{ues}] srate={srate} rt(median,min,n)={rt} starv={starv} ts={ts}")
