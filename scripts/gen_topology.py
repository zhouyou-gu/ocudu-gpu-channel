#!/usr/bin/env python3
"""Emit a block-style YAML topology for the fan-in sweep configs.

Two modes, picked by the first positional argument:
  one-to-n    1 gNB, N UEs, no UE-UE crosstalk
              edges = 2N (N DL + N UL); gNB has N incoming.
  m-to-n      M gNBs, N UEs, fully-connected bipartite (no inter-gNB and
              no inter-UE crosstalk)
              edges = 2*M*N; each gNB has N incoming, each UE has M
              incoming.

Endpoints are loopback placeholders -- the bench never opens the
sockets, so they only need to be unique strings that pass YAML parsing.

Usage:
  gen_topology.py one-to-n N [out.yaml]
  gen_topology.py m-to-n M N [out.yaml]

If out.yaml is omitted, writes to stdout.
"""
import sys


def make_device(dev_id: str, role: str, tx_port: int, rx_port: int) -> str:
    return (
        f"  - id: {dev_id}\n"
        f"    role: {role}\n"
        f"    sample_rate_hz: 23040000\n"
        f"    tx_endpoint: tcp://127.0.0.1:{tx_port}\n"
        f"    rx_endpoint: tcp://*:{rx_port}\n"
    )


def make_link(src: str, dst: str, model: str = "cuda_mvp") -> str:
    return f"  - from: {src}\n    to: {dst}\n    model: {model}\n"


def header(comment: str) -> str:
    return (
        f"# {comment}\n"
        "runtime:\n"
        "  backend: cuda\n"
        "  gpu_device: 0\n"
        "  batch_samples: 23040\n"
        "  queue_samples: 2457600\n"
    )


def model_block() -> str:
    return (
        "models:\n"
        "  cuda_mvp:\n"
        "    chain:\n"
        "      - type: tdl\n"
        "        taps:\n"
        "          - delay_samples: 0.0\n"
        "            gain_db: -3.0\n"
        "            phase_rad: 0.0\n"
        "      - type: phase\n"
        "        phase_rad: 0.125\n"
        "      - type: cfo\n"
        "        cfo_hz: 250\n"
        "        phase_rad: 0\n"
    )


def gen_one_to_n(n: int) -> str:
    parts = [
        header(
            f"Set 1 (1-to-N): 1 gNB + {n} UEs, no UE-UE crosstalk. "
            f"gNB has {n} incoming UL edges (fan-in choke point); "
            f"each UE has 1 incoming DL edge."
        ),
        "devices:\n",
        make_device("gnb0", "gnb", 5000, 5001),
    ]
    base_port = 5100
    for k in range(n):
        parts.append(make_device(f"ue{k}", "ue", base_port + 2 * k, base_port + 2 * k + 1))
    parts.append("links:\n")
    for k in range(n):
        parts.append(make_link("gnb0", f"ue{k}"))   # DL
        parts.append(make_link(f"ue{k}", "gnb0"))   # UL
    parts.append(model_block())
    return "".join(parts)


def gen_m_to_n(m: int, n: int) -> str:
    parts = [
        header(
            f"Set 2 (M-to-N): {m} gNBs + {n} UEs, fully-connected bipartite, "
            f"no inter-gNB and no inter-UE crosstalk. Each gNB has {n} incoming "
            f"UL edges; each UE has {m} incoming DL edges. Total edges = {2 * m * n}."
        ),
        "devices:\n",
    ]
    base_gnb_port = 6000
    for j in range(m):
        parts.append(make_device(f"gnb{j}", "gnb", base_gnb_port + 2 * j, base_gnb_port + 2 * j + 1))
    base_ue_port = 6200
    for k in range(n):
        parts.append(make_device(f"ue{k}", "ue", base_ue_port + 2 * k, base_ue_port + 2 * k + 1))
    parts.append("links:\n")
    for j in range(m):
        for k in range(n):
            parts.append(make_link(f"gnb{j}", f"ue{k}"))   # DL: each gNB to every UE
            parts.append(make_link(f"ue{k}", f"gnb{j}"))   # UL: each UE to every gNB
    parts.append(model_block())
    return "".join(parts)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    mode = argv[1]
    if mode == "one-to-n":
        if len(argv) < 3:
            print("one-to-n requires N", file=sys.stderr)
            return 2
        n = int(argv[2])
        out_path = argv[3] if len(argv) > 3 else None
        yaml_text = gen_one_to_n(n)
    elif mode == "m-to-n":
        if len(argv) < 4:
            print("m-to-n requires M and N", file=sys.stderr)
            return 2
        m, n = int(argv[2]), int(argv[3])
        out_path = argv[4] if len(argv) > 4 else None
        yaml_text = gen_m_to_n(m, n)
    else:
        print(f"unknown mode: {mode}", file=sys.stderr)
        return 2

    if out_path:
        with open(out_path, "w") as f:
            f.write(yaml_text)
    else:
        sys.stdout.write(yaml_text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
