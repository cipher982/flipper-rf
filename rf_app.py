#!/usr/bin/env python3
"""
RF Observatory - Real-time RF signal intelligence
Protocol classification, fingerprinting, and spectrum analysis for Flipper Zero
"""

import argparse
import asyncio
import json
import re
import os
import time
import sys
import glob
import random
import serial
import threading
import queue
import hashlib
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import datetime
from collections import defaultdict, deque
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
import websockets

# Defaults
DEFAULT_FLIPPER_PORT = '/dev/cu.usbmodemflip_Ly0p11'  # pass --port auto to detect
DEFAULT_WS_PORT = 8766
DEFAULT_HTTP_PORT = 8765
DEFAULT_CAPTURE_DURATION = 0.8
DEFAULT_WORK_DIR = Path('/tmp/flipper_explore')
DEFAULT_FREQS_HZ = (315_000_000, 433_920_000, 868_000_000, 915_000_000)

# Shared state
data_queue = queue.Queue()
clients = set()

# Signal database
signal_db = {}  # fp -> full signal info
recent_signals = deque(maxlen=1000)
band_stats = defaultdict(lambda: {'signals': deque(maxlen=200), 'protocols': defaultdict(int)})


@dataclass(frozen=True)
class AppConfig:
    flipper_port: str
    ws_port: int
    http_port: int
    capture_duration: float
    freqs_hz: tuple[int, ...]
    work_dir: Path
    mock: bool


def hz_to_mhz(freq_hz: int) -> float:
    return round(freq_hz / 1_000_000, 2)


def parse_freqs(freqs: str) -> tuple[int, ...]:
    """Parse a comma-separated list of frequencies (MHz or Hz)."""
    out: list[int] = []
    for part in freqs.split(','):
        s = part.strip()
        if not s:
            continue

        try:
            val = Decimal(s)
        except InvalidOperation as e:
            raise ValueError(f"Invalid frequency: {part!r}") from e

        if val >= Decimal('1000000'):  # treat as Hz
            hz = int(val.to_integral_value(rounding=ROUND_HALF_UP))
        else:  # treat as MHz
            hz = int((val * Decimal('1000000')).to_integral_value(rounding=ROUND_HALF_UP))

        if hz <= 0:
            raise ValueError(f"Invalid frequency: {part!r}")

        out.append(hz)

    if not out:
        raise ValueError("No frequencies provided")

    # de-dupe while keeping order
    seen: set[int] = set()
    uniq = []
    for hz in out:
        if hz not in seen:
            seen.add(hz)
            uniq.append(hz)

    return tuple(uniq)


def detect_flipper_port() -> str | None:
    """Best-effort auto-detect of Flipper Zero serial port."""
    if sys.platform == 'darwin':
        patterns = [
            '/dev/cu.usbmodemflip*',
            '/dev/cu.usbmodem*Flipper*',
            '/dev/cu.usbmodem*flipper*',
        ]
    elif sys.platform.startswith('linux'):
        patterns = [
            '/dev/ttyACM*',
            '/dev/ttyUSB*',
        ]
    else:
        patterns = []

    candidates: list[str] = []
    for pat in patterns:
        candidates.extend(glob.glob(pat))

    candidates = sorted(set(candidates))
    return candidates[0] if candidates else None


def wake_and_validate_flipper(ser: serial.Serial, max_attempts: int = 3) -> tuple[bool, str]:
    """
    Wake the Flipper CLI and validate it responds to commands.

    Returns (success, message) tuple.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            # Clear any stale data
            ser.reset_input_buffer()
            ser.reset_output_buffer()

            # Send multiple Ctrl+C to exit any running app/command
            for _ in range(3):
                ser.write(b'\x03')
                time.sleep(0.1)

            # Wait for banner/prompt
            time.sleep(0.5 * attempt)  # Increase wait on retries

            # Drain the banner
            banner = b''
            start = time.time()
            while time.time() - start < 2.0:
                if ser.in_waiting:
                    try:
                        banner += ser.read(ser.in_waiting)
                    except serial.SerialException:
                        break
                    time.sleep(0.05)
                else:
                    time.sleep(0.1)

            if not banner:
                if attempt < max_attempts:
                    continue
                return False, "No response from Flipper CLI (no banner)"

            # Check we got the prompt
            if b'>:' not in banner:
                if attempt < max_attempts:
                    continue
                return False, "Flipper responded but no CLI prompt found"

            # Now test with a real command
            time.sleep(0.2)
            ser.write(b'uptime\r\n')
            time.sleep(0.5)

            response = b''
            start = time.time()
            while time.time() - start < 2.0:
                if ser.in_waiting:
                    try:
                        response += ser.read(ser.in_waiting)
                    except serial.SerialException:
                        break
                    time.sleep(0.05)
                else:
                    time.sleep(0.1)

            # Check for valid uptime response
            resp_text = response.decode('utf-8', errors='replace')
            if 'Uptime:' in resp_text or 'uptime' in resp_text.lower():
                # Extract firmware version from banner for logging
                fw_match = re.search(r'Firmware version:\s*([^\r\n]+)', banner.decode('utf-8', errors='replace'))
                fw_ver = fw_match.group(1).strip() if fw_match else 'unknown'
                return True, f"CLI validated (firmware: {fw_ver})"

            # CLI showed banner but command didn't work - this is the "sleep" issue
            if attempt < max_attempts:
                # Try harder - close and reopen might help
                time.sleep(0.5 * attempt)
                continue

            return False, "Flipper CLI not responding to commands (may need replug)"

        except serial.SerialException as e:
            if attempt < max_attempts:
                time.sleep(0.5)
                continue
            return False, f"Serial error: {e}"
        except Exception as e:
            return False, f"Unexpected error: {e}"

    return False, "Failed to wake Flipper CLI after all attempts"


def validate_flipper_connection(port: str) -> tuple[bool, str, serial.Serial | None]:
    """
    Open serial port and validate Flipper CLI is responsive.

    Returns (success, message, serial_connection).
    If success is False, serial_connection will be None.
    """
    try:
        ser = serial.Serial(port, 230400, timeout=0.5)
    except serial.SerialException as e:
        return False, f"Cannot open {port}: {e}", None
    except Exception as e:
        return False, f"Serial error: {e}", None

    success, msg = wake_and_validate_flipper(ser)
    if not success:
        ser.close()
        return False, msg, None

    return True, msg, ser


def build_config() -> AppConfig:
    parser = argparse.ArgumentParser(description="RF Observatory - real-time RF signal intelligence")
    parser.add_argument('--port', default=os.environ.get('FLIPPER_PORT') or DEFAULT_FLIPPER_PORT,
                        help='Serial port path (or "auto")')
    parser.add_argument('--ws-port', type=int, default=DEFAULT_WS_PORT, help='WebSocket port')
    parser.add_argument('--http-port', type=int, default=DEFAULT_HTTP_PORT, help='HTTP port')
    parser.add_argument('--capture-duration', type=float, default=DEFAULT_CAPTURE_DURATION,
                        help='Capture duration per frequency (seconds)')
    parser.add_argument('--freqs', default=','.join(str(hz_to_mhz(f)) for f in DEFAULT_FREQS_HZ),
                        help='Comma-separated frequencies (MHz or Hz), e.g. 433.92 or 433920000')
    parser.add_argument('--work-dir', default=str(DEFAULT_WORK_DIR),
                        help='Directory to write and serve UI from')
    parser.add_argument('--mock', action='store_true', help='Run without a Flipper; generate synthetic signals')
    args = parser.parse_args()

    freqs_hz = parse_freqs(args.freqs)

    port = args.port
    if port == 'auto' or (port and not os.path.exists(port)):
        detected = detect_flipper_port()
        if detected:
            port = detected

    if not port and not args.mock:
        raise SystemExit("No Flipper port found. Pass --port PATH or use --mock.")

    return AppConfig(
        flipper_port=port or '',
        ws_port=args.ws_port,
        http_port=args.http_port,
        capture_duration=args.capture_duration,
        freqs_hz=freqs_hz,
        work_dir=Path(args.work_dir),
        mock=args.mock,
    )


# ============ PROTOCOL SIGNATURES ============
# Based on common Sub-GHz protocol timing patterns

PROTOCOL_SIGNATURES = {
    # Protocol: (pulse_range_us, gap_range_us, min_pulses, characteristics)
    'princeton': {
        'pulse_range': (200, 500),
        'gap_short': (200, 500),
        'gap_long': (800, 1500),
        'min_pulses': 20,
        'bits': 24,
        'desc': 'Fixed code remote',
        'category': 'remote',
        'icon': '\U0001F697',
    },
    'came_12bit': {
        'pulse_range': (250, 400),
        'gap_short': (250, 400),
        'gap_long': (500, 800),
        'min_pulses': 12,
        'bits': 12,
        'desc': 'CAME gate/garage',
        'category': 'gate',
        'icon': '\U0001F6A7',
    },
    'nice_flo': {
        'pulse_range': (600, 900),
        'gap_short': (600, 900),
        'gap_long': (1200, 1800),
        'min_pulses': 12,
        'bits': 12,
        'desc': 'Nice FLO remote',
        'category': 'gate',
        'icon': '\U0001F6A7',
    },
    'keeloq': {
        'pulse_range': (300, 500),
        'te': 400,  # Time element ~400us
        'min_pulses': 60,
        'bits': 66,
        'desc': 'Rolling code (KeeLoq)',
        'category': 'secure',
        'icon': '\U0001F510',
    },
    'oregon_v2': {
        'pulse_range': (400, 700),
        'gap_range': (400, 700),
        'min_pulses': 100,
        'desc': 'Oregon Scientific weather',
        'category': 'sensor',
        'icon': '\U0001F321\uFE0F',
    },
    'honeywell': {
        'pulse_range': (400, 600),
        'gap_short': (400, 600),
        'min_pulses': 40,
        'desc': 'Honeywell security',
        'category': 'alarm',
        'icon': '\U0001F6A8',
    },
    'amb_weather': {
        'pulse_range': (450, 600),
        'gap_range': (900, 1200),
        'min_pulses': 30,
        'desc': 'Ambient weather sensor',
        'category': 'sensor',
        'icon': '\U0001F324\uFE0F',
    },
    'tpms': {
        'pulse_range': (40, 80),
        'min_pulses': 50,
        'desc': 'Tire pressure sensor',
        'category': 'automotive',
        'icon': '\U0001F6DE',
    },
    'smart_meter': {
        'pulse_range': (15, 50),
        'min_pulses': 100,
        'desc': 'Smart utility meter',
        'category': 'utility',
        'icon': '\u26A1',
    },
    'doorbell': {
        'pulse_range': (200, 400),
        'gap_long': (5000, 15000),
        'min_pulses': 20,
        'desc': 'Wireless doorbell',
        'category': 'home',
        'icon': '\U0001F514',
    },
}

# Frequency-based hints
FREQ_HINTS = {
    315.0: ['automotive', 'tpms', 'garage_us'],
    433.92: ['remote', 'gate', 'sensor', 'doorbell'],
    868.0: ['gate_eu', 'sensor', 'alarm'],
    915.0: ['smart_meter', 'lora', 'industrial'],
}


def extract_bursts(timings, gap_threshold=30000):
    """Split timing array into distinct bursts."""
    if not timings:
        return []

    bursts = []
    current = []

    for t in timings:
        if abs(t) > gap_threshold and current:
            if len(current) >= 6:
                bursts.append(current)
            current = []
        else:
            current.append(t)

    if len(current) >= 6:
        bursts.append(current)

    return bursts


def analyze_timing_pattern(burst):
    """Deep analysis of timing pattern."""
    on_pulses = [t for t in burst if t > 0]
    off_gaps = [abs(t) for t in burst if t < 0]

    if len(on_pulses) < 3:
        return None

    # Basic stats
    pulse_min = min(on_pulses)
    pulse_max = max(on_pulses)
    pulse_mean = sum(on_pulses) // len(on_pulses)
    pulse_median = sorted(on_pulses)[len(on_pulses)//2]

    gap_min = min(off_gaps) if off_gaps else 0
    gap_max = max(off_gaps) if off_gaps else 0
    gap_mean = sum(off_gaps) // len(off_gaps) if off_gaps else 0

    # Pulse width variance (normalized)
    variance = sum((p - pulse_mean)**2 for p in on_pulses) / len(on_pulses)
    std_dev = variance ** 0.5
    cv = std_dev / pulse_mean if pulse_mean > 0 else 0  # Coefficient of variation

    # Distinct pulse widths (quantized to 50us)
    quantized = set(p // 50 * 50 for p in on_pulses)
    n_distinct = len(quantized)

    # Gap ratio (long gaps vs short gaps)
    if off_gaps:
        short_gaps = [g for g in off_gaps if g < gap_mean]
        long_gaps = [g for g in off_gaps if g >= gap_mean]
        gap_ratio = len(long_gaps) / len(off_gaps) if off_gaps else 0
    else:
        gap_ratio = 0

    # Duration
    on_total_us = sum(on_pulses)
    off_total_us = sum(off_gaps) if off_gaps else 0
    duration_us = on_total_us + off_total_us
    on_duty = (on_total_us / duration_us * 100) if duration_us else 0

    return {
        'n_pulses': len(on_pulses),
        'n_transitions': len(burst),
        'duration_ms': round(duration_us / 1000, 1),
        'on_duration_ms': round(on_total_us / 1000, 1),
        'on_duty_cycle': round(on_duty, 1),
        'pulse_min': pulse_min,
        'pulse_max': pulse_max,
        'pulse_mean': pulse_mean,
        'pulse_median': pulse_median,
        'pulse_cv': round(cv, 3),
        'n_distinct_widths': n_distinct,
        'gap_min': gap_min,
        'gap_max': gap_max,
        'gap_mean': gap_mean,
        'gap_ratio': round(gap_ratio, 2),
        'histogram': compute_histogram(on_pulses),
    }


def compute_histogram(pulses, buckets=8):
    """Log-scale pulse width histogram."""
    if not pulses:
        return [0] * buckets

    ranges = [100, 300, 1000, 3000, 10000, 30000, 100000, float('inf')]
    hist = [0] * buckets

    for p in pulses:
        for i, threshold in enumerate(ranges):
            if p < threshold:
                hist[i] += 1
                break

    return hist


def classify_protocol(analysis, freq_mhz):
    """Classify signal based on timing analysis."""
    if not analysis:
        return None

    pulse_mean = analysis['pulse_mean']
    pulse_median = analysis['pulse_median']
    pulse_min = analysis['pulse_min']
    pulse_max = analysis['pulse_max']
    n_pulses = analysis['n_pulses']
    gap_max = analysis['gap_max']
    cv = analysis['pulse_cv']
    n_distinct = analysis['n_distinct_widths']

    candidates = []

    for proto_name, sig in PROTOCOL_SIGNATURES.items():
        score = 0

        # Check pulse range
        pr = sig.get('pulse_range')
        if pr:
            if pr[0] <= pulse_mean <= pr[1]:
                score += 30
            elif pr[0] <= pulse_median <= pr[1]:
                score += 20

        # Check minimum pulses
        min_p = sig.get('min_pulses', 10)
        if n_pulses >= min_p:
            score += 20
        elif n_pulses >= min_p * 0.7:
            score += 10

        # Frequency hint bonus
        hints = FREQ_HINTS.get(freq_mhz, [])
        if sig.get('category') in hints:
            score += 15

        if score >= 30:
            candidates.append((proto_name, score, sig))

    # Sort by score
    candidates.sort(key=lambda x: -x[1])

    if candidates:
        best = candidates[0]
        return {
            'protocol': best[0],
            'confidence': min(best[1], 100),
            'desc': best[2]['desc'],
            'category': best[2]['category'],
            'icon': best[2]['icon'],
        }

    # Heuristic fallback classification
    if pulse_mean < 100:
        return {
            'protocol': 'fsk_signal',
            'confidence': 40,
            'desc': 'FSK/digital signal',
            'category': 'digital',
            'icon': '\U0001F4F6',
        }
    elif pulse_mean > 5000:
        return {
            'protocol': 'slow_signal',
            'confidence': 30,
            'desc': 'Slow/status beacon',
            'category': 'beacon',
            'icon': '\U0001F4E1',
        }
    elif cv < 0.2:  # Very consistent pulse widths
        return {
            'protocol': 'fixed_code',
            'confidence': 50,
            'desc': 'Fixed code signal',
            'category': 'remote',
            'icon': '\U0001F4FB',
        }
    elif n_distinct > 5:
        return {
            'protocol': 'complex_signal',
            'confidence': 35,
            'desc': 'Complex modulation',
            'category': 'unknown',
            'icon': '\u2753',
        }
    else:
        return {
            'protocol': 'ook_signal',
            'confidence': 25,
            'desc': 'OOK signal',
            'category': 'unknown',
            'icon': '\U0001F4FB',
        }


def compute_fingerprint(burst):
    """Stable fingerprint from timing pattern."""
    if len(burst) < 6:
        return None

    on_pulses = [t for t in burst if t > 0]
    if len(on_pulses) < 3:
        return None

    # Quantize to 50us for stability
    quantized = [t // 50 * 50 for t in on_pulses[:30]]
    pattern = ','.join(str(t) for t in quantized)
    return hashlib.md5(pattern.encode()).hexdigest()[:8]


def get_signal_label(signal):
    """Generate human-readable label for signal."""
    proto = signal.get('protocol', {})
    if not proto:
        return 'Unknown signal'

    desc = proto.get('desc', 'Signal')
    conf = proto.get('confidence', 0)

    if conf >= 60:
        return f"{desc}"
    elif conf >= 40:
        return f"{desc}?"
    else:
        return f"{desc} (weak match)"


def decode_timings(timings: list[int], freq_mhz: float) -> tuple[list[dict], list[dict], int]:
    """Decode a raw timing array for a single frequency."""
    bursts = extract_bursts(timings)

    freq_signals: list[dict] = []
    new_signals: list[dict] = []
    decoded_count = 0

    for burst in bursts:
        analysis = analyze_timing_pattern(burst)
        if not analysis:
            continue

        fp = compute_fingerprint(burst)
        if not fp:
            continue

        proto = classify_protocol(analysis, freq_mhz)

        ts = time.time()
        signal = {
            'fp': fp,
            'freq': freq_mhz,
            'ts': ts,
            'protocol': proto,
            'label': get_signal_label({'protocol': proto}),
            **analysis,
            'timings': burst[:80],
        }

        freq_signals.append(signal)

        if proto and proto.get('confidence', 0) >= 30:
            decoded_count += 1

        # Track in database
        is_new = fp not in signal_db
        signal['is_new'] = is_new
        if is_new:
            signal_db[fp] = {
                'first_seen': ts,
                'last_seen': ts,
                'count': 1,
                'freq': freq_mhz,
                'protocol': proto,
                'label': signal['label'],
            }
            new_signals.append(signal)
        else:
            signal_db[fp]['last_seen'] = ts
            signal_db[fp]['count'] += 1

        signal['count'] = signal_db[fp]['count']
        signal['first_seen'] = signal_db[fp]['first_seen']

        recent_signals.append(signal)

        band_stats[freq_mhz]['signals'].append(signal)
        if proto:
            band_stats[freq_mhz]['protocols'][proto['protocol']] += 1

    return freq_signals, new_signals, decoded_count


def build_proto_summary(freq_signals: list[dict]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for s in freq_signals:
        p = s.get('protocol', {}).get('protocol', 'unknown')
        summary[p] = summary.get(p, 0) + 1
    return summary


_MOCK_DEVICE_BASES: dict[tuple[float, str, int], tuple[int, int]] = {}


def mock_timings(freq_mhz: float) -> list[int]:
    """Generate synthetic timing data with burst boundaries."""
    hints = FREQ_HINTS.get(freq_mhz, [])

    eligible = [
        (name, sig)
        for name, sig in PROTOCOL_SIGNATURES.items()
        if sig.get('category') in hints
    ] or list(PROTOCOL_SIGNATURES.items())

    n_bursts = random.choices([0, 1, 2, 3], weights=[2, 5, 3, 1])[0]
    if n_bursts == 0:
        return []

    timings: list[int] = []
    for _ in range(n_bursts):
        proto_name, sig = random.choice(eligible)

        # Pick a stable-ish "device slot" to produce repeatable fingerprints.
        device_slot = random.randint(1, 3)
        key = (freq_mhz, proto_name, device_slot)
        if key not in _MOCK_DEVICE_BASES:
            pr = sig.get('pulse_range', (200, 500))
            pulse = random.randrange(max(50, pr[0] // 50 * 50), pr[1] // 50 * 50 + 1, 50)
            gr = sig.get('gap_short') or sig.get('gap_range') or pr
            gap = random.randrange(max(50, gr[0] // 50 * 50), gr[1] // 50 * 50 + 1, 50)
            _MOCK_DEVICE_BASES[key] = (pulse, gap)

        pulse, gap = _MOCK_DEVICE_BASES[key]

        min_p = sig.get('min_pulses', 12)
        n_pulses = random.randint(min_p, min_p + 12)
        gl = sig.get('gap_long')

        for i in range(n_pulses):
            jitter = random.randint(-20, 20)
            timings.append(max(10, pulse + jitter))

            if gl and i % 8 == 7 and random.random() < 0.4:
                gap_val = random.randint(gl[0], gl[1])
            else:
                gap_val = max(10, gap + random.randint(-20, 20))
            timings.append(-gap_val)

        # Burst separator (must exceed extract_bursts gap_threshold=30000)
        timings.append(-random.randint(45_000, 80_000))

    return timings


def capture_thread(config: AppConfig):
    """Capture and decode signals."""
    if config.mock:
        print("Mock capture thread starting...")
        data_queue.put({'type': 'status', 'connected': True, 'mock': True, 'ts': time.time()})
        cycle = 0
        while True:
            cycle += 1
            cycle_start = time.time()

            all_signals: list[dict] = []
            freq_results: list[dict] = []
            new_signals: list[dict] = []
            decoded_count = 0

            for freq_hz in config.freqs_hz:
                freq_mhz = hz_to_mhz(freq_hz)
                timings = mock_timings(freq_mhz)

                freq_signals, freq_new, freq_decoded = decode_timings(timings, freq_mhz)
                all_signals.extend(freq_signals)
                new_signals.extend(freq_new)
                decoded_count += freq_decoded

                for sig in freq_signals:
                    data_queue.put({'type': 'signal', 'data': sig})

                freq_results.append({
                    'freq': freq_mhz,
                    'ts': time.time(),
                    'n_signals': len(freq_signals),
                    'decoded_signals': freq_decoded,
                    'n_transitions': len(timings),
                    'protocols': build_proto_summary(freq_signals),
                    'best_signal': freq_signals[0] if freq_signals else None,
                    'health': compute_band_health(freq_mhz),
                })

                data_queue.put({'type': 'freq', 'data': freq_results[-1]})
                time.sleep(max(0.05, config.capture_duration))

            cycle_data = {
                'type': 'cycle',
                'cycle': cycle,
                'ts': time.time(),
                'duration': round(time.time() - cycle_start, 2),
                'total_signals': len(all_signals),
                'decoded_signals': decoded_count,
                'freqs': freq_results,
                'new_signals': [{'fp': s['fp'], 'freq': s['freq'], 'label': s['label']} for s in new_signals],
                'total_unique': len(signal_db),
                'top_signals': get_top_signals(10),
                'protocol_stats': get_protocol_stats(),
            }
            data_queue.put(cycle_data)
        return

    print("Capture thread starting...")
    data_queue.put({'type': 'status', 'connected': False, 'mock': False, 'ts': time.time()})

    cycle = 0
    while True:
        # Validate Flipper connection with wake-up sequence
        print(f"Connecting to Flipper at {config.flipper_port}...")
        success, msg, ser = validate_flipper_connection(config.flipper_port)

        if not success:
            print(f"\033[91mFlipper validation failed: {msg}\033[0m")
            data_queue.put({'type': 'error', 'msg': msg, 'ts': time.time()})
            data_queue.put({'type': 'status', 'connected': False, 'mock': False, 'ts': time.time()})
            print("Retrying in 3 seconds... (try unplugging and replugging USB)")
            time.sleep(3.0)
            continue

        print(f"\033[92mFlipper connected: {config.flipper_port}\033[0m")
        print(f"  {msg}")
        data_queue.put({'type': 'status', 'connected': True, 'mock': False, 'port': config.flipper_port, 'ts': time.time()})

        try:
            while True:
                cycle += 1
                cycle_start = time.time()

                all_signals: list[dict] = []
                freq_results: list[dict] = []
                new_signals: list[dict] = []
                decoded_count = 0

                for freq_hz in config.freqs_hz:
                    freq_mhz = hz_to_mhz(freq_hz)

                    ser.write(f'subghz rx_raw {freq_hz}\r\n'.encode())

                    raw = b''
                    start = time.time()
                    while time.time() - start < config.capture_duration:
                        time.sleep(0.02)
                        if ser.in_waiting:
                            raw += ser.read(ser.in_waiting)

                    ser.write(b'\x03')
                    time.sleep(0.05)
                    raw += ser.read(ser.in_waiting or 2048)

                    text = raw.decode('utf-8', errors='replace')
                    timings = [int(t) for t in re.findall(r'([+-]\d+)', text)]

                    freq_signals, freq_new, freq_decoded = decode_timings(timings, freq_mhz)
                    all_signals.extend(freq_signals)
                    new_signals.extend(freq_new)
                    decoded_count += freq_decoded

                    for sig in freq_signals:
                        data_queue.put({'type': 'signal', 'data': sig})

                    freq_results.append({
                        'freq': freq_mhz,
                        'ts': time.time(),
                        'n_signals': len(freq_signals),
                        'decoded_signals': freq_decoded,
                        'n_transitions': len(timings),
                        'protocols': build_proto_summary(freq_signals),
                        'best_signal': freq_signals[0] if freq_signals else None,
                        'health': compute_band_health(freq_mhz),
                    })

                    data_queue.put({'type': 'freq', 'data': freq_results[-1]})

                cycle_data = {
                    'type': 'cycle',
                    'cycle': cycle,
                    'ts': time.time(),
                    'duration': round(time.time() - cycle_start, 2),
                    'total_signals': len(all_signals),
                    'decoded_signals': decoded_count,
                    'freqs': freq_results,
                    'new_signals': [{'fp': s['fp'], 'freq': s['freq'], 'label': s['label']} for s in new_signals],
                    'total_unique': len(signal_db),
                    'top_signals': get_top_signals(10),
                    'protocol_stats': get_protocol_stats(),
                }

                data_queue.put(cycle_data)

                new_str = f" +{len(new_signals)} NEW" if new_signals else ""
                print(f"[{datetime.now().strftime('%H:%M:%S')}] C{cycle}: "
                      f"{len(all_signals)} signals ({decoded_count} decoded), "
                      f"{len(signal_db)} unique{new_str}")
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            print(f"Capture loop error: {msg}")
            data_queue.put({'type': 'error', 'msg': msg, 'ts': time.time()})
            data_queue.put({'type': 'status', 'connected': False, 'mock': False, 'ts': time.time()})
        finally:
            try:
                ser.close()
            except Exception:
                pass
            time.sleep(1.0)


def get_top_signals(limit=10):
    """Get most active signals."""
    now = time.time()
    scores = defaultdict(float)
    details = {}

    for sig in recent_signals:
        fp = sig.get('fp')
        if not fp:
            continue

        age = now - sig['ts']
        recency = max(0, 1 - age / 300)
        scores[fp] += recency

        if fp not in details or sig['ts'] > details[fp]['ts']:
            details[fp] = sig

    # Merge with db
    for fp in details:
        if fp in signal_db:
            details[fp]['count'] = signal_db[fp]['count']
            details[fp]['first_seen'] = signal_db[fp]['first_seen']

    top = sorted(scores.items(), key=lambda x: -x[1])[:limit]
    return [details[fp] for fp, _ in top if fp in details]


def get_protocol_stats():
    """Get protocol distribution stats."""
    proto_counts = defaultdict(int)
    for fp, info in signal_db.items():
        proto = info.get('protocol', {}).get('protocol', 'unknown')
        proto_counts[proto] += 1

    return dict(sorted(proto_counts.items(), key=lambda x: -x[1])[:8])


def compute_band_health(freq_mhz: float, window_sec: int = 60) -> dict[str, float | int]:
    """Compute per-band health metrics over a rolling window."""
    stats = band_stats[freq_mhz]
    now = time.time()

    recent = [s for s in stats['signals'] if now - s.get('ts', now) < window_sec]
    if not recent:
        return {
            'signal_rate': 0.0,
            'unique_per_min': 0,
            'entropy': 0.0,
            'duty_cycle': 0.0,
        }

    signal_rate = len(recent) / window_sec * 60  # per minute

    fps = [s.get('fp') for s in recent if s.get('fp')]
    unique_per_min = len(set(fps))

    # Entropy of fingerprints (diversity)
    fp_counts = defaultdict(int)
    for fp in fps:
        fp_counts[fp] += 1

    total = sum(fp_counts.values())
    entropy = 0.0
    if total > 0:
        import math
        for count in fp_counts.values():
            p = count / total
            if p > 0:
                entropy -= p * math.log2(p)

    # Duty cycle (approx: sum of on-time over the window)
    on_total_ms = sum(float(s.get('on_duration_ms', 0) or 0) for s in recent)
    duty_cycle = min(100.0, (on_total_ms / (window_sec * 1000)) * 100) if window_sec > 0 else 0.0

    return {
        'signal_rate': round(signal_rate, 1),
        'unique_per_min': unique_per_min,
        'entropy': round(entropy, 2),
        'duty_cycle': round(duty_cycle, 1),
    }


async def broadcast_loop():
    """Broadcast to clients."""
    while True:
        try:
            msgs = []
            while not data_queue.empty():
                try:
                    msgs.append(data_queue.get_nowait())
                except queue.Empty:
                    break

            if msgs and clients:
                for msg in msgs:
                    payload = json.dumps(msg, default=str)
                    await asyncio.gather(
                        *[c.send(payload) for c in clients],
                        return_exceptions=True
                    )

            await asyncio.sleep(0.03)
        except Exception as e:
            print(f"Broadcast error: {e}")
            await asyncio.sleep(0.5)


async def ws_handler(websocket, path=None):
    """WebSocket handler."""
    clients.add(websocket)
    print(f"Client connected ({len(clients)})")

    await websocket.send(json.dumps({
        'type': 'init',
        'total_unique': len(signal_db),
        'top_signals': get_top_signals(10),
        'protocol_stats': get_protocol_stats(),
    }, default=str))

    try:
        async for _ in websocket:
            pass
    except:
        pass
    finally:
        clients.discard(websocket)
        print(f"Client disconnected ({len(clients)})")


def http_thread(config: AppConfig):
    """HTTP server with index.html redirect."""

    class IndexHandler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def end_headers(self):
            self.send_header('Cache-Control', 'no-store, max-age=0')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Expires', '0')
            return super().end_headers()

        def do_GET(self):
            # Redirect / to /index.html
            if self.path == '/':
                self.send_response(302)
                self.send_header('Location', '/index.html')
                self.end_headers()
                return
            return super().do_GET()

    handler = partial(IndexHandler, directory=str(config.work_dir))
    HTTPServer(('localhost', config.http_port), handler).serve_forever()


# ============ UI Assets ============


def write_dashboard_files(config: AppConfig) -> None:
    ui_dir = Path(__file__).resolve().parent / 'ui'
    html_template = (ui_dir / 'index.html').read_text(encoding='utf-8')
    css = (ui_dir / 'observatory.css').read_text(encoding='utf-8')
    js = (ui_dir / 'observatory.js').read_text(encoding='utf-8')
    icons = (ui_dir / 'icons.svg').read_text(encoding='utf-8')

    ui_config = {
        'ws_port': config.ws_port,
        'http_port': config.http_port,
        'freqs_mhz': [hz_to_mhz(f) for f in config.freqs_hz],
        'capture_duration': config.capture_duration,
        'work_dir': str(config.work_dir),
        'mock': config.mock,
    }

    html = html_template.replace('__CONFIG__', json.dumps(ui_config))

    (config.work_dir / 'index.html').write_text(html, encoding='utf-8')
    (config.work_dir / 'observatory.css').write_text(css, encoding='utf-8')
    (config.work_dir / 'observatory.js').write_text(js, encoding='utf-8')
    (config.work_dir / 'icons.svg').write_text(icons, encoding='utf-8')


async def main():
    print("=" * 50)
    print("RF Observatory")
    print("Real-time RF signal intelligence")
    print("=" * 50)

    config = build_config()
    config.work_dir.mkdir(parents=True, exist_ok=True)

    # Upfront validation for real Flipper mode
    if not config.mock:
        print(f"\nValidating Flipper connection at {config.flipper_port}...")
        success, msg, ser = validate_flipper_connection(config.flipper_port)
        if ser:
            ser.close()  # Close test connection; capture thread will reopen

        if not success:
            print(f"\n\033[91mERROR: Flipper validation failed\033[0m")
            print(f"  {msg}")
            print("\nTroubleshooting:")
            print("  1. Unplug and replug the USB cable")
            print("  2. Make sure no app is open on the Flipper screen")
            print("  3. Try rebooting the Flipper (hold back → Reboot)")
            print("  4. Or use --mock for synthetic signals")
            sys.exit(1)

        print(f"\033[92m✓ {msg}\033[0m\n")

    write_dashboard_files(config)

    threading.Thread(target=http_thread, args=(config,), daemon=True).start()
    threading.Thread(target=capture_thread, args=(config,), daemon=True).start()

    print(f"Dashboard: http://localhost:{config.http_port}/")
    print(f"WebSocket: ws://localhost:{config.ws_port}")
    if config.mock:
        print("Mode: mock (synthetic signals)")
    else:
        print(f"Flipper: {config.flipper_port}")
    print("Press Ctrl+C to stop\n")

    await websockets.serve(ws_handler, 'localhost', config.ws_port)
    await broadcast_loop()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped")
