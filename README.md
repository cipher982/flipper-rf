# Flipper RF

Real-time RF signal intelligence using Flipper Zero's Sub-GHz radio.

Stream, fingerprint, and decode wireless signals on 315/433/868/915 MHz bands.

## Features

- **Real-time streaming** - WebSocket-based live signal feed
- **Signal fingerprinting** - MD5 hash of quantized pulse patterns for tracking unique devices
- **Protocol classification** - Timing-based identification of common protocols:
  - 🚗 Princeton (fixed code remotes)
  - 🚧 CAME / Nice FLO (gate remotes)
  - 🔐 KeeLoq (rolling code)
  - 🌡️ Oregon Scientific (weather sensors)
  - ⚡ Smart meters
  - 🛞 TPMS (tire pressure)
  - And more...
- **Band health metrics** - Entropy, burst rate, unique signals per minute
- **Web dashboard** - Dark-themed real-time visualization

## Requirements

- Flipper Zero with Sub-GHz radio
- Python 3.11+
- macOS/Linux (tested on macOS)

## Setup

```bash
# Create venv
uv venv
source .venv/bin/activate

# Install deps
uv pip install pyserial websockets
```

## Usage

### RF Decode (recommended)

Full protocol classification and semantic labeling:

```bash
python rf_decode.py
# Open http://localhost:8765/decode.html
```

### RF Intel

Fingerprinting-focused view with burst detection:

```bash
python rf_intel.py
# Open http://localhost:8765/intel.html
```

### Flipper Tool

Python wrapper for direct Flipper CLI access:

```python
from flipper_tool import FlipperTool

with FlipperTool('/dev/cu.usbmodemflip_XXX') as f:
    info = f.device_info()
    print(info)

    # Raw capture
    timings = f.subghz_rx_raw(433920000, duration=2.0)
```

## Configuration

Edit the `FLIPPER_PORT` constant in each script to match your device:

```python
FLIPPER_PORT = '/dev/cu.usbmodemflip_XXXXX'  # macOS
# or
FLIPPER_PORT = '/dev/ttyACM0'  # Linux
```

## Architecture

```
┌─────────────┐    Serial     ┌─────────────┐
│ Flipper Zero│◄────────────►│ Python      │
│ (Sub-GHz RX)│   230400 baud │ Capture     │
└─────────────┘               │ Thread      │
                              └──────┬──────┘
                                     │ Queue
                              ┌──────▼──────┐
                              │ WebSocket   │
                              │ Broadcast   │
                              └──────┬──────┘
                                     │ JSON
                              ┌──────▼──────┐
                              │ Browser     │
                              │ Dashboard   │
                              └─────────────┘
```

## Protocol Classification

Signals are classified based on timing analysis:

| Protocol | Pulse Width | Min Pulses | Typical Use |
|----------|-------------|------------|-------------|
| Princeton | 200-500µs | 20 | Car remotes, garage doors |
| CAME | 250-400µs | 12 | European gates |
| Nice FLO | 600-900µs | 12 | European gates |
| KeeLoq | 300-500µs | 60 | Secure rolling code |
| Oregon v2 | 400-700µs | 100 | Weather stations |
| Smart Meter | 15-50µs | 100 | Utility meters |
| TPMS | 40-80µs | 50 | Tire pressure |

## Legal

This tool is for **educational and authorized testing only**. Receiving RF signals is generally legal; transmitting or replaying signals without authorization is not. Know your local laws.

## License

MIT
