# Flipper RF - Agent Instructions

## Overview

RF signal analysis tools using Flipper Zero's Sub-GHz radio. Real-time streaming, fingerprinting, and protocol classification.

## Quick Start

```bash
# Setup
uv venv && source .venv/bin/activate
uv pip install pyserial websockets

# Find your Flipper port
ls /dev/cu.usbmodem*  # macOS
ls /dev/ttyACM*       # Linux

# Edit FLIPPER_PORT in rf_decode.py (line 21)

# Run
python rf_decode.py
# Open http://localhost:8765/decode.html
```

## Key Files

| File | Purpose |
|------|---------|
| `rf_decode.py` | Main tool - protocol classification + dashboard |
| `rf_intel.py` | Fingerprinting-focused alternative |
| `flipper_tool.py` | Python wrapper for Flipper CLI |

## Architecture

- **Capture thread** - Serial reads from Flipper at 230400 baud
- **Queue** - Thread-safe handoff to async loop
- **WebSocket broadcast** - Push to browser clients at 30Hz
- **Embedded HTML** - Dashboard generated at runtime

## Protocol Classification

Timing-based analysis in `classify_protocol()`:
- Pulse width ranges
- Gap ratios (short vs long)
- Pulse counts (bit lengths)
- Frequency band hints

## Ports

- HTTP: 8765 (dashboard)
- WebSocket: 8766 (data stream)

## Dependencies

- pyserial
- websockets
