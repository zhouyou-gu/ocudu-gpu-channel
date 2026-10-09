# Dashboard

Run one dashboard for the configured gNBs. The dashboard receives broker telemetry and optional scheduler metrics; it does not make a radio connection healthy by displaying it.

## Prerequisites and launch

Install the `dashboard` extra. Start the broker with control and telemetry endpoints, and provide the bridge's status JSONL if displaying Sionna delivery. From the repository root:

```sh
ocudu-dashboard --bind 127.0.0.1 --port 8080 \
  --telemetry-endpoint tcp://127.0.0.1:5560 \
  --status-jsonl /tmp/ocudu-sionna/status.jsonl
```

Open `http://127.0.0.1:8080/` on the execution host; `/healthz` checks the HTTP server. When the dashboard runs on RTX and your browser runs on your own computer, create the tunnel **on your computer**, using your actual SSH identity, port and host settings:

```sh
ssh -N -L 8080:127.0.0.1:8080 USER@RTX_HOST
```

Then open `http://127.0.0.1:8080/` locally. Keep the tunnel running while inspecting the page and stop it with Ctrl-C afterwards. If local port 8080 is occupied, choose another local port in the left-hand side of `-L` and open that port. The dashboard remains bound to the GPU host's loopback interface.

Installed browser assets resolve independently of the working directory. Add repeated `--gnb-metrics-source ID=URL` options using the actual WebSocket metrics URLs configured on each gNB. The [CLI reference](../reference/cli.md) records all options.

## Interpret the display

Use the [telemetry reference](../reference/telemetry.md) for field definitions and freshness. A connected metrics socket can carry stale scheduler rows; a fresh empty report is different from a disconnected source. Broker channel readiness is different again from UE traffic success.

Stopping the dashboard with Ctrl-C leaves independently started broker and radio processes running. The combined Sionna launcher owns and cleans up only its bridge and dashboard children.

If the page loads but data remains stale, inspect the configured telemetry endpoint and status path before the browser. If scheduler rows remain historical after reconnect, wait for a new scheduler report and check the producer logs; heartbeats do not refresh those rows.

Continue to [Sionna](sionna.md) or the [metrics recovery report](../reports/validation/metrics-recovery-validation.md).
