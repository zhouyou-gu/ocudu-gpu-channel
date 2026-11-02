#!/usr/bin/env python3
"""NxM topology for the OAI 2x2 gate family: gNB with N ports (loopback 2000+2i / 2001+2i), one OAI nrUE with M ports
over the veth pair (UE REP TX 2101+2i, broker REP RX 2100+2i), fixed MIMO matrices taken from the 4x4 DFT matrix so
the DL (M x N) rows and the UL (N x M) columns are orthogonal (well conditioned, rank min(N, M)); column norm 0.8."""
import argparse, cmath, math
ap = argparse.ArgumentParser(); ap.add_argument("--gnb-ant", type=int, required=True); ap.add_argument("--ue-ant", type=int, required=True); ap.add_argument("--out", required=True)
a = ap.parse_args(); N, M = a.gnb_ant, a.ue_ant
W = [[cmath.exp(-2j * math.pi * r * c / 4) / 2 for c in range(4)] for r in range(4)]  # unitary 4x4
def coeffs(rows, cols, name):
    scale = 0.8 / math.sqrt(rows) / 0.5  # column norm 0.8
    out = [f"  {name}:\n    fixed_mimo:\n      coefficients:\n"]
    for r in range(rows):
        for c in range(cols):
            z = W[r][c] * scale
            out.append(f"        - tap: 0\n          rx: {r}\n          tx: {c}\n          real: {z.real:.4f}\n          imag: {z.imag:.4f}\n")
    out.append("    chain:\n      - type: tdl\n        taps:\n          - delay_samples: 0.0\n            gain_db: 0.0\n            phase_rad: 0.0\n")
    return "".join(out)
dev = []
for i in range(N):
    dev.append(f"  - id: gnb0_p{i}\n    role: port\n    sample_rate_hz: 23040000\n    tx_endpoint: tcp://127.0.0.1:{2000 + 2 * i}\n    rx_endpoint: tcp://127.0.0.1:{2001 + 2 * i}\n")
for i in range(M):
    dev.append(f"  - id: ue0_p{i}\n    role: port\n    sample_rate_hz: 23040000\n    tx_endpoint: tcp://10.201.0.2:{2101 + 2 * i}\n    rx_endpoint: tcp://10.201.0.1:{2100 + 2 * i}\n")
def node(nid, k):
    ports = "".join(f"      - {nid}_p{i}\n" for i in range(k))
    return f"  - id: {nid}\n    tx_ports:\n{ports}    rx_ports:\n{ports}"
y = (f"# {N}x{M} topology for the OAI gate family (local): gNB {N} ports, OAI nrUE {M} ports, DFT-based fixed MIMO.\n"
     "runtime:\n  backend: cuda\n  gpu_device: 0\n  batch_samples: 23040\n  queue_samples: 2457600\n\n"
     f"devices:\n{''.join(dev)}\nradio_nodes:\n{node('gnb0', N)}{node('ue0', M)}\n"
     f"links:\n  - from: gnb0\n    to: ue0\n    model: dl_{N}x{M}\n  - from: ue0\n    to: gnb0\n    model: ul_{M}x{N}\n\n"
     f"models:\n{coeffs(M, N, f'dl_{N}x{M}')}\n{coeffs(N, M, f'ul_{M}x{N}')}")
open(a.out, "w").write(y); print(a.out, "gnb_ports", N, "ue_ports", M)
