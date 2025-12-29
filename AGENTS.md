# Flipper RF - Agent Instructions

Real-time RF signal intelligence using Flipper Zero's Sub-GHz radio.

## Core files
- `rf_decode.py`: protocol classification dashboard (recommended)
- `rf_intel.py`: fingerprinting + band health dashboard
- `flipper_tool.py`: Flipper serial CLI helper (`FlipperTool`)

## Dev setup (UV)
```bash
uv venv && source .venv/bin/activate
uv pip install websockets pyserial
```

## Run
```bash
# Auto-detect serial port (recommended)
python3 rf_decode.py --port auto
# http://localhost:8765/decode.html

python3 rf_intel.py --port auto
# http://localhost:8765/intel.html
```

## Useful flags
- `--port PATH|auto` (or set `FLIPPER_PORT`)
- `--freqs 433.92` (focus a single band) or `--freqs 315,433.92,868,915`
- `--capture-duration 0.4` (per-frequency capture window)
- `--http-port 8875 --ws-port 8876` (avoid conflicts)
- `--work-dir /tmp/flipper_explore` (where dashboards are written/served)
- `--mock` (no Flipper required; generates synthetic data for UI/dev)
