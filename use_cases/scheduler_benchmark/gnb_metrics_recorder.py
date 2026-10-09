#!/usr/bin/env python3
"""Record one gNB's JSON metrics feed to a JSON Lines file.

Subscribes to the OCUDU remote-control WebSocket (through the gate's relay
socket, `ws+unix://<run dir>/gnb-metrics-<cell>.sock`) and appends every
message as `{"rx_unix_ms": ..., "payload": <message>}`. The receive time is
the recorder's clock, which the analyzer maps onto the benchmark segments.

The WebSocket client, endpoint parser and subscribe command are the web UI
server's own (scripts/web_ui/server.py), not a second implementation.
Reconnects with backoff: the gNB starts and stops independently of this.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import signal
import sys
import threading
import time
from typing import Sequence

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts" / "web_ui"))

from server import (  # noqa: E402
    GNB_METRICS_SUBSCRIBE,
    WebSocketClient,
    WebSocketError,
    parse_ws_endpoint,
)


def record(endpoint: str, out: pathlib.Path, stop: threading.Event) -> int:
    host, port, path, unix_path = parse_ws_endpoint(endpoint)
    messages = 0
    backoff = 0.5
    with out.open("a", encoding="utf-8") as handle:
        while not stop.is_set():
            client = None
            try:
                client = WebSocketClient(host, port, path, unix_path=unix_path)
                client.send_text(GNB_METRICS_SUBSCRIBE)
                client.monitor_connection(stop)
                print(json.dumps({"event": "gnb_metrics_recorder_subscribed", "endpoint": endpoint}), flush=True)
                backoff = 0.5
                while not stop.is_set():
                    message = client.recv_message()
                    if message is None:
                        break
                    try:
                        payload = json.loads(message)
                    except ValueError:
                        continue
                    handle.write(json.dumps({"rx_unix_ms": time.time_ns() // 1_000_000, "payload": payload},
                                            separators=(",", ":")) + "\n")
                    handle.flush()
                    messages += 1
            except (OSError, WebSocketError, ValueError) as error:
                if not stop.is_set():
                    print(json.dumps({"event": "gnb_metrics_recorder_disconnected", "error": str(error)}), flush=True)
            finally:
                if client is not None:
                    client.close()
            if stop.wait(backoff):
                break
            backoff = min(backoff * 2.0, 5.0)
    print(json.dumps({"event": "gnb_metrics_recorder_done", "messages": messages}), flush=True)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--endpoint", required=True, help="ws://host:port or ws+unix:///abs/socket")
    parser.add_argument("--out", type=pathlib.Path, required=True)
    args = parser.parse_args(argv)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    return record(args.endpoint, args.out, stop)


if __name__ == "__main__":
    raise SystemExit(main())
