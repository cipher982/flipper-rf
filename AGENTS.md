# RF Observatory - Agent Instructions

Real-time RF signal intelligence using Flipper Zero's Sub-GHz radio.

## Core files
- `rf_app.py`: main entrypoint (protocol classification, fingerprinting, dashboard)
- `flipper_tool.py`: Flipper serial CLI helper (`FlipperTool`)
- `ui/`: dashboard templates (index.html, observatory.css, observatory.js)

## Dev setup
```bash
make deps
```

## Run
```bash
# With Flipper (auto-detect port)
make start

# Mock mode (no Flipper, synthetic signals)
make mock
```

Dashboard: http://localhost:8765/

## Useful flags
- `--port PATH|auto` (or set `FLIPPER_PORT`)
- `--freqs 433.92` (focus a single band) or `--freqs 315,433.92,868,915`
- `--capture-duration 0.4` (per-frequency capture window)
- `--http-port 8875 --ws-port 8876` (avoid conflicts)
- `--work-dir /tmp/flipper_explore` (where dashboard is served from)
- `--mock` (no Flipper required; generates synthetic data)
