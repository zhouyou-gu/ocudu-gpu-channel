# #21: Dashboard status polling times out on high-rate telemetry

GitHub: [#21](https://github.com/zhouyou-gu/ocudu-gpu-channel/issues/21). Status: **Closed as completed**, 4 November 2026.

**Component:** Dashboard HTTP polling, payload size and scene framing.

**Platform:** Spark synthetic SUTD, ten edges, 500 Hz broker telemetry, SSH viewing path.

## Behavior and reproduction

The browser displayed UI feed errors while `/healthz` confirmed fresh Sionna and all ten telemetry edges. The status poll fetched `history_ms=2000` every 500 ms and aborted after two seconds. One response contained 12,802,436 bytes and took 3.88 seconds through the tunnel. The scene-only response was 141,143 bytes in 0.32 seconds. The status endpoint duplicated history already fetched by the chart loop.

The opening camera also fitted the entire campus, leaving the four active nodes too small to follow. This was observed in the in-app browser, not inferred from HTTP readiness.

## Fix and verification

The status loop requests current state without retained chart history. HTTP responses use gzip when negotiated; chart and scene polls have explicit timeouts. Existing failure reporting remains active. The camera fits node positions and complete declared routes, accounts for viewport aspect ratio, and provides a separate Campus button.

On the same live Spark/SSH path, compressed status was 27,084 bytes in 24 ms and chart history 92,308 bytes in 162 ms. After reload, the rendered page showed Telemetry 10/10 and CUDA READY 10/10; screenshots confirmed the closer scene, moving UEs and visible ray paths. These are individual measurements, not network-latency guarantees.

RTX checks: 44 Python dashboard tests passed, including gzip/identity content integrity and disabled-gzip negotiation. Frontend regressions check disconnect behavior, status polling without raw history, cached chart data surviving status-card rebuilds, and independent RAN feeds. Screenshots and raw run artifacts remain ignored.

## Resolution

Published in [44deafd](https://github.com/zhouyou-gu/ocudu-gpu-channel/commit/44deafd0846180b3870c112090b44d7865514490) on `main`, with fix and verification links recorded in GitHub before closure. Actual disconnects still clear readiness. Synthetic UI checks remain separate from real-radio or strict timing qualification.

## Evidence

[Dashboard client](../../apps/dashboard/index.html), [HTTP server](../../apps/dashboard/server.py), [bridge metadata](../../apps/sionna_bridge/run_bridge.py), [HTTP tests](../../tests/dashboard/test_sionna_web_ui.py), [frontend regression](../../tests/dashboard/test_web_ui_disconnect.mjs).
