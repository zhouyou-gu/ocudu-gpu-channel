#!/usr/bin/env python3
"""Boundary-sweep topology generator: C cells, each one gNB with P ports and N UEs with Q ports,
K-tap TDL per link, AWGN per receiver. Same YAML shape as render_single_cell.py."""
import argparse, math
ap = argparse.ArgumentParser()
ap.add_argument("--ues", type=int, required=True)
ap.add_argument("--srate-hz", type=int, default=23040000)
ap.add_argument("--batch-samples", type=int, default=0, help="0 = srate * slot (1 ms at 15 kHz, 0.5 ms at 30 kHz)")
ap.add_argument("--scs-khz", type=int, default=15)
ap.add_argument("--taps", type=int, default=8)
ap.add_argument("--gnb-ports", type=int, default=1)
ap.add_argument("--ue-ports", type=int, default=1)
ap.add_argument("--cells", type=int, default=1)
ap.add_argument("--base-port", type=int, default=20000)
ap.add_argument("--out", required=True)
a = ap.parse_args()
slot_s = 1e-3 * 15 / a.scs_khz
batch = a.batch_samples or int(round(a.srate_hz * slot_s))
port = a.base_port
dev, nodes, links, models = [], [], [], []
def add_port(pid, node):
    global port
    dev.append(f"  - id: {pid}\n    role: port\n    sample_rate_hz: {a.srate_hz}\n"
               f"    tx_endpoint: tcp://127.0.0.1:{port}\n    rx_endpoint: tcp://127.0.0.1:{port+1}\n    rx_model: rx_noise_{node}\n")
    port += 2
def node_yaml(n, ports):
    return (f"  - id: {n}\n    tx_ports:\n" + "".join(f"      - {p}\n" for p in ports)
            + "    rx_ports:\n" + "".join(f"      - {p}\n" for p in ports))
for c in range(a.cells):
    g = f"gnb{c}"
    gp = [f"{g}_p{i}" for i in range(a.gnb_ports)]
    for p in gp: add_port(p, g)
    nodes.append(node_yaml(g, gp))
    models.append(f"  rx_noise_{g}:\n    chain:\n      - type: awgn\n        noise_power: 3.0e-02\n")
    for u in range(a.ues):
        n = f"c{c}ue{u}" if a.cells > 1 else f"ue{u}"
        up = [f"{n}_p{i}" for i in range(a.ue_ports)]
        for p in up: add_port(p, n)
        nodes.append(node_yaml(n, up))
        models.append(f"  rx_noise_{n}:\n    chain:\n      - type: awgn\n        noise_power: 1.12e-06\n")
        links.append(f"  - from: {g}\n    to: {n}\n    model: tdl\n  - from: {n}\n    to: {g}\n    model: tdl\n")
# K-tap TDL: delays over ~2.5 us (TDL-C-like span), decaying gains, deterministic phases
span = 2.5e-6 * a.srate_hz
den = max(a.taps - 1, 1)
taps = "".join(f"          - delay_samples: {(k * span / den):.3f}\n            gain_db: {(-3.0 - 17.0 * k / den):.2f}\n"
               f"            phase_rad: {((k * 2.399) % (2 * math.pi)):.4f}\n" for k in range(a.taps))
models.insert(0, f"  tdl:\n    chain:\n      - type: tdl\n        taps:\n{taps}")
y = (f"# boundary sweep: cells={a.cells} ues={a.ues} gnb_ports={a.gnb_ports} ue_ports={a.ue_ports} srate={a.srate_hz} scs={a.scs_khz} taps={a.taps}\n"
     f"runtime:\n  backend: cuda\n  gpu_device: 0\n  batch_samples: {batch}\n  queue_samples: {batch * 32}\n"
     f"devices:\n{''.join(dev)}radio_nodes:\n{''.join(nodes)}links:\n{''.join(links)}models:\n{''.join(models)}")
open(a.out, "w").write(y)
print(a.out, "ports", (port - a.base_port) // 2, "links", 2 * a.ues * a.cells, "batch", batch)
