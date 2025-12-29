#!/usr/bin/env python3
"""
RF Decode - Protocol classification and semantic labeling
Analyzes timing patterns to identify signal types
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


def build_config() -> AppConfig:
    parser = argparse.ArgumentParser(description="RF Decode - protocol classification dashboard")
    parser.add_argument('--port', default=os.environ.get('FLIPPER_PORT') or DEFAULT_FLIPPER_PORT,
                        help='Serial port path (or "auto")')
    parser.add_argument('--ws-port', type=int, default=DEFAULT_WS_PORT, help='WebSocket port')
    parser.add_argument('--http-port', type=int, default=DEFAULT_HTTP_PORT, help='HTTP port')
    parser.add_argument('--capture-duration', type=float, default=DEFAULT_CAPTURE_DURATION,
                        help='Capture duration per frequency (seconds)')
    parser.add_argument('--freqs', default=','.join(str(hz_to_mhz(f)) for f in DEFAULT_FREQS_HZ),
                        help='Comma-separated frequencies (MHz or Hz), e.g. 433.92 or 433920000')
    parser.add_argument('--work-dir', default=str(DEFAULT_WORK_DIR),
                        help='Directory to write and serve decode.html from')
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
        'icon': '🚗',
    },
    'came_12bit': {
        'pulse_range': (250, 400),
        'gap_short': (250, 400),
        'gap_long': (500, 800),
        'min_pulses': 12,
        'bits': 12,
        'desc': 'CAME gate/garage',
        'category': 'gate',
        'icon': '🚧',
    },
    'nice_flo': {
        'pulse_range': (600, 900),
        'gap_short': (600, 900),
        'gap_long': (1200, 1800),
        'min_pulses': 12,
        'bits': 12,
        'desc': 'Nice FLO remote',
        'category': 'gate',
        'icon': '🚧',
    },
    'keeloq': {
        'pulse_range': (300, 500),
        'te': 400,  # Time element ~400µs
        'min_pulses': 60,
        'bits': 66,
        'desc': 'Rolling code (KeeLoq)',
        'category': 'secure',
        'icon': '🔐',
    },
    'oregon_v2': {
        'pulse_range': (400, 700),
        'gap_range': (400, 700),
        'min_pulses': 100,
        'desc': 'Oregon Scientific weather',
        'category': 'sensor',
        'icon': '🌡️',
    },
    'honeywell': {
        'pulse_range': (400, 600),
        'gap_short': (400, 600),
        'min_pulses': 40,
        'desc': 'Honeywell security',
        'category': 'alarm',
        'icon': '🚨',
    },
    'amb_weather': {
        'pulse_range': (450, 600),
        'gap_range': (900, 1200),
        'min_pulses': 30,
        'desc': 'Ambient weather sensor',
        'category': 'sensor',
        'icon': '🌤️',
    },
    'tpms': {
        'pulse_range': (40, 80),
        'min_pulses': 50,
        'desc': 'Tire pressure sensor',
        'category': 'automotive',
        'icon': '🛞',
    },
    'smart_meter': {
        'pulse_range': (15, 50),
        'min_pulses': 100,
        'desc': 'Smart utility meter',
        'category': 'utility',
        'icon': '⚡',
    },
    'doorbell': {
        'pulse_range': (200, 400),
        'gap_long': (5000, 15000),
        'min_pulses': 20,
        'desc': 'Wireless doorbell',
        'category': 'home',
        'icon': '🔔',
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

    # Distinct pulse widths (quantized to 50µs)
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
    duration_us = sum(abs(t) for t in burst)

    return {
        'n_pulses': len(on_pulses),
        'n_transitions': len(burst),
        'duration_ms': round(duration_us / 1000, 1),
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
            'icon': '📶',
        }
    elif pulse_mean > 5000:
        return {
            'protocol': 'slow_signal',
            'confidence': 30,
            'desc': 'Slow/status beacon',
            'category': 'beacon',
            'icon': '📡',
        }
    elif cv < 0.2:  # Very consistent pulse widths
        return {
            'protocol': 'fixed_code',
            'confidence': 50,
            'desc': 'Fixed code signal',
            'category': 'remote',
            'icon': '📻',
        }
    elif n_distinct > 5:
        return {
            'protocol': 'complex_signal',
            'confidence': 35,
            'desc': 'Complex modulation',
            'category': 'unknown',
            'icon': '❓',
        }
    else:
        return {
            'protocol': 'ook_signal',
            'confidence': 25,
            'desc': 'OOK signal',
            'category': 'unknown',
            'icon': '📻',
        }


def compute_fingerprint(burst):
    """Stable fingerprint from timing pattern."""
    if len(burst) < 6:
        return None

    on_pulses = [t for t in burst if t > 0]
    if len(on_pulses) < 3:
        return None

    # Quantize to 50µs for stability
    quantized = [t // 50 * 50 for t in on_pulses[:30]]
    pattern = ','.join(str(t) for t in quantized)
    return hashlib.md5(pattern.encode()).hexdigest()[:8]


def get_signal_label(signal):
    """Generate human-readable label for signal."""
    proto = signal.get('protocol', {})
    if not proto:
        return 'Unknown Signal'

    icon = proto.get('icon', '📻')
    desc = proto.get('desc', 'Signal')
    conf = proto.get('confidence', 0)

    if conf >= 60:
        return f"{icon} {desc}"
    elif conf >= 40:
        return f"{icon} {desc}?"
    else:
        return f"{icon} {desc} (weak match)"


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

                freq_results.append({
                    'freq': freq_mhz,
                    'n_signals': len(freq_signals),
                    'n_transitions': len(timings),
                    'protocols': build_proto_summary(freq_signals),
                    'best_signal': freq_signals[0] if freq_signals else None,
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
        try:
            ser = serial.Serial(config.flipper_port, 230400, timeout=0.3)
            time.sleep(0.3)
            ser.read(ser.in_waiting)
            print(f"Flipper connected: {config.flipper_port}")
            data_queue.put({'type': 'status', 'connected': True, 'mock': False, 'port': config.flipper_port, 'ts': time.time()})
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            print(f"Flipper error: {msg}")
            data_queue.put({'type': 'error', 'msg': msg, 'ts': time.time()})
            data_queue.put({'type': 'status', 'connected': False, 'mock': False, 'ts': time.time()})
            time.sleep(2.0)
            continue

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

                    freq_results.append({
                        'freq': freq_mhz,
                        'n_signals': len(freq_signals),
                        'n_transitions': len(timings),
                        'protocols': build_proto_summary(freq_signals),
                        'best_signal': freq_signals[0] if freq_signals else None,
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
    """HTTP server."""

    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    handler = partial(QuietHandler, directory=str(config.work_dir))
    HTTPServer(('localhost', config.http_port), handler).serve_forever()


# ============ DASHBOARD HTML ============

DASHBOARD = '''<!DOCTYPE html>
<html><head>
<meta charset="UTF-8"><title>RF Decode</title>
<style>
:root{--bg:#0a0e14;--surface:#12171f;--surface2:#1a2029;--border:#2a3140;--text:#e6edf3;--dim:#6e7681;--green:#3fb950;--yellow:#d29922;--red:#f85149;--blue:#58a6ff;--purple:#a371f7;--cyan:#56d4dd;--orange:#f0883e}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'SF Mono',Consolas,monospace;background:var(--bg);color:var(--text);font-size:12px;overflow-x:hidden}
.app{display:grid;grid-template-columns:1fr 380px;grid-template-rows:auto 1fr;height:100vh;gap:1px;background:var(--border)}
header{grid-column:1/-1;display:flex;justify-content:space-between;align-items:center;padding:10px 16px;background:var(--surface)}
h1{font-size:14px;font-weight:600;display:flex;align-items:center;gap:8px}
.status{display:flex;gap:20px;font-size:11px;color:var(--dim)}
.status span{display:flex;align-items:center;gap:4px}
.dot{width:6px;height:6px;border-radius:50%;background:var(--dim);transition:background .2s, box-shadow .2s}
.dot.live{background:var(--green);box-shadow:0 0 8px var(--green)}
.dot.error{background:var(--red);box-shadow:0 0 8px var(--red)}
.main{display:flex;flex-direction:column;gap:1px;background:var(--border);overflow-y:auto}
.sidebar{display:flex;flex-direction:column;gap:1px;background:var(--border);overflow-y:auto}
.panel{background:var(--surface);padding:12px}
.section{font-size:10px;font-weight:600;color:var(--dim);text-transform:uppercase;letter-spacing:.5px;margin-bottom:8px;display:flex;justify-content:space-between}
.freq-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:6px}
.freq-card{background:var(--surface2);border-radius:4px;padding:8px;text-align:center;border:1px solid transparent;transition:border-color .2s}
.freq-card.active{border-color:var(--green)}
.freq-label{font-size:10px;color:var(--dim)}
.freq-value{font-size:22px;font-weight:700;font-family:inherit;transition:color .25s ease}
.freq-bar{height:2px;background:var(--border);border-radius:1px;margin-top:6px;overflow:hidden}
.freq-fill{height:100%;transition:width .3s}
.freq-protos{font-size:9px;color:var(--dim);margin-top:4px;min-height:14px}
.proto-stats{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.proto-chip{background:var(--surface2);border-radius:3px;padding:3px 6px;font-size:10px;display:flex;align-items:center;gap:4px}
.proto-chip .icon{font-size:12px}
.proto-chip .count{color:var(--dim)}
.wave-panel{height:80px;background:var(--bg);border-radius:4px;margin-top:8px;position:relative}
.wave-panel svg{width:100%;height:100%}
.wave-panel path{fill:none;stroke-width:1.5}
.wave-label{position:absolute;top:4px;left:8px;font-size:9px;color:var(--dim);background:var(--bg);padding:2px 6px;border-radius:2px}
.timeline{height:100px;background:var(--bg);border-radius:4px;margin-top:8px}
.timeline svg{width:100%;height:100%}
.signal-list{display:flex;flex-direction:column;gap:4px;max-height:400px;overflow-y:auto}
.signal-item{background:var(--surface2);border-radius:4px;padding:8px 10px;display:grid;grid-template-columns:auto 1fr auto;gap:8px;align-items:center;border-left:3px solid transparent}
.signal-item.new{border-left-color:var(--green);animation:flash .5s}
@keyframes flash{0%,100%{background:var(--surface2)}50%{background:#3fb95020}}
.signal-icon{font-size:16px;width:24px;text-align:center}
.signal-info{display:flex;flex-direction:column;gap:2px}
.signal-label{font-weight:500;font-size:11px}
.signal-meta{font-size:9px;color:var(--dim);display:flex;gap:8px}
.signal-fp{font-family:inherit;color:var(--blue)}
.signal-stats{text-align:right}
.signal-count{font-size:12px;font-weight:600}
.signal-age{font-size:9px;color:var(--dim)}
.mini-hist{display:flex;gap:1px;height:14px;margin-top:4px}
.mini-hist span{flex:1;background:var(--purple);border-radius:1px;align-self:flex-end;min-width:3px;transition:height .25s ease}
.protocol-breakdown{margin-top:8px}
.proto-row{display:flex;align-items:center;gap:8px;padding:4px 0;border-bottom:1px solid var(--border)}
.proto-row:last-child{border:none}
.proto-icon{font-size:14px;width:20px}
.proto-name{flex:1;font-size:11px}
.proto-bar{width:100px;height:4px;background:var(--border);border-radius:2px;overflow:hidden}
.proto-fill{height:100%;background:var(--blue);transition:width .3s ease}
.proto-count{font-size:11px;color:var(--dim);min-width:30px;text-align:right}
.log{font-size:10px;max-height:120px;overflow-y:auto;background:var(--bg);border-radius:4px;padding:6px;margin-top:8px}
.log-entry{padding:2px 0;border-bottom:1px solid var(--border);display:flex;gap:8px}
.log-ts{color:var(--dim);min-width:55px}
.log-new{color:var(--green)}
.alert{position:fixed;top:60px;right:16px;background:var(--green);color:var(--bg);padding:6px 12px;border-radius:4px;font-size:11px;font-weight:600;animation:slideIn .3s}
@keyframes slideIn{from{transform:translateX(100%);opacity:0}to{transform:translateX(0);opacity:1}}
</style></head><body>
<div class="app">
<header>
<h1>📡 RF Decode</h1>
<div class="status">
<span><div class="dot" id="dot"></div><span id="conn">Connecting</span></span>
<span id="cycle">--</span>
<span id="unique">0 unique</span>
<span id="decoded">0 decoded</span>
</div>
</header>
<div class="main">
<div class="panel">
<div class="section">Frequency Bands</div>
<div class="freq-grid" id="freqGrid"></div>
<div class="proto-stats" id="protoStats"></div>
</div>
<div class="panel">
<div class="section">Waveform<span id="waveMeta">--</span></div>
<div class="wave-panel" id="wave"><div class="wave-label" id="waveLabel"></div></div>
</div>
<div class="panel">
<div class="section">Activity Timeline</div>
<div class="timeline" id="timeline"></div>
</div>
<div class="panel">
<div class="section">Event Log</div>
<div class="log" id="log"></div>
</div>
</div>
<div class="sidebar">
<div class="panel">
<div class="section">Live Signals<span id="sigCount"></span></div>
<div class="signal-list" id="signals"></div>
</div>
<div class="panel">
<div class="section">Protocol Distribution</div>
<div class="protocol-breakdown" id="protocols"></div>
</div>
</div>
</div>
<script>
const WS_PORT=__WS_PORT__;
const WS_URL=`${location.protocol==='https:'?'wss':'ws'}://${location.hostname}:${WS_PORT}`;

let ws;
const freqs=__FREQS__;
const baseColors={315:'#3fb950',433.92:'#58a6ff',868:'#d29922',915:'#f85149'};
const palette=['#3fb950','#58a6ff','#d29922','#f85149','#56d4dd','#a371f7','#f0883e'];
const colors={};
freqs.forEach((f,i)=>{colors[f]=baseColors[f]||palette[i%palette.length]});
const protoColors={princeton:'#3fb950',came_12bit:'#58a6ff',nice_flo:'#d29922',keeloq:'#f85149',oregon_v2:'#56d4dd',smart_meter:'#f0883e',tpms:'#a371f7',fixed_code:'#8b949e',unknown:'#6e7681'};
const protoIcons={princeton:'🚗',came_12bit:'🚧',nice_flo:'🚧',keeloq:'🔐',oregon_v2:'🌡️',smart_meter:'⚡',tpms:'🛞',fixed_code:'📻',fsk_signal:'📶',slow_signal:'📡',complex_signal:'❓',ook_signal:'📻',unknown:'❓'};

const state={freq:{},hist:[],maxSig:1,pending:[],renderScheduled:false,signalsReady:false,protocolsReady:false};

const el={
dot:document.getElementById('dot'),
conn:document.getElementById('conn'),
cycle:document.getElementById('cycle'),
unique:document.getElementById('unique'),
decoded:document.getElementById('decoded'),
freqGrid:document.getElementById('freqGrid'),
wave:document.getElementById('wave'),
waveMeta:document.getElementById('waveMeta'),
waveLabel:document.getElementById('waveLabel'),
timeline:document.getElementById('timeline'),
log:document.getElementById('log'),
signals:document.getElementById('signals'),
sigCount:document.getElementById('sigCount'),
protocols:document.getElementById('protocols'),
};

const freqEls={};
const signalEls=new Map();
const protocolEls=new Map();
let wavePath=null;

function setConn(text,live,error){
el.dot.classList.toggle('live',!!live);
el.dot.classList.toggle('error',!!error);
el.conn.textContent=text;
}

function connect(){
ws=new WebSocket(WS_URL);
ws.onopen=()=>setConn('Live',true,false);
ws.onclose=()=>{setConn('Reconnecting',false,false);setTimeout(connect,1000)};
ws.onmessage=e=>enqueue(e.data);
}

function enqueue(raw){
try{state.pending.push(JSON.parse(raw))}catch{return}
if(!state.renderScheduled){
state.renderScheduled=true;
requestAnimationFrame(flush);
}
}

function flush(){
state.renderScheduled=false;
while(state.pending.length)applyMessage(state.pending.shift());
}

function applyMessage(m){
if(m.type==='freq')handleFreq(m.data);
else if(m.type==='cycle')handleCycle(m);
else if(m.type==='init'){renderSignals(m.top_signals);renderProtocols(m.protocol_stats);if(typeof m.total_unique==='number')el.unique.textContent=m.total_unique+' unique'}
else if(m.type==='status')handleStatus(m);
else if(m.type==='error')handleError(m);
}

function handleStatus(m){
if(m.connected===false)setConn('Flipper offline',true,true);
else if(m.connected===true)setConn(m.mock?'Mock':'Flipper',true,false);
}

function showToast(text,color){
const a=document.createElement('div');
a.className='alert';
if(color)a.style.background=color;
a.textContent=text;
document.body.appendChild(a);
setTimeout(()=>a.remove(),3500);
}

function handleError(m){
setConn('Error',true,true);
showToast('ERROR: '+(m.msg||'Unknown error'),'var(--red)');
addLog({ts:m.ts||Date.now()/1000,total_signals:0,decoded_signals:0,new_signals:[],_msg:(m.msg||'')});
}

function buildFreqGrid(){
freqs.forEach(f=>{
const card=document.createElement('div');
card.className='freq-card';
const label=document.createElement('div');
label.className='freq-label';
label.textContent=f+' MHz';
const value=document.createElement('div');
value.className='freq-value';
value.textContent='0';
value.style.color='var(--dim)';
const bar=document.createElement('div');
bar.className='freq-bar';
const fill=document.createElement('div');
fill.className='freq-fill';
fill.style.width='0%';
fill.style.background=colors[f]||'var(--dim)';
bar.appendChild(fill);
const protos=document.createElement('div');
protos.className='freq-protos';
protos.textContent='--';
card.appendChild(label);
card.appendChild(value);
card.appendChild(bar);
card.appendChild(protos);
el.freqGrid.appendChild(card);
freqEls[f]={card,value,fill,protos};
});
}

function buildWave(){
const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
svg.setAttribute('viewBox','0 0 400 80');
svg.setAttribute('preserveAspectRatio','none');
wavePath=document.createElementNS('http://www.w3.org/2000/svg','path');
svg.appendChild(wavePath);
el.wave.appendChild(svg);
}

function handleFreq(d){
state.freq[d.freq]=d;
const newMax=Math.max(1,...Object.values(state.freq).map(f=>f.n_signals||0));
const maxChanged=newMax!==state.maxSig;
state.maxSig=newMax;
if(maxChanged)freqs.forEach(f=>updateFreqCard(f,state.freq[f]||{freq:f,n_signals:0,protocols:{}}));
else updateFreqCard(d.freq,d);
if(d.best_signal&&d.best_signal.timings)renderWave(d.best_signal);
}

function updateFreqCard(freqMhz,d){
const els=freqEls[freqMhz];
if(!els)return;
const n=d.n_signals||0;
const pct=Math.min((n/state.maxSig)*100,100);
const c=colors[freqMhz]||'var(--green)';
els.card.classList.toggle('active',n>0);
els.value.textContent=String(n);
els.value.style.color=n>0?c:'var(--dim)';
els.fill.style.width=pct+'%';
els.fill.style.background=c;
const protos=Object.entries(d.protocols||{}).slice(0,2).map(([p])=>p.replace(/_/g,' ')).join(', ');
els.protos.textContent=protos||'--';
}

function handleCycle(d){
el.cycle.textContent='C'+d.cycle+' ('+d.duration+'s)';
el.unique.textContent=d.total_unique+' unique';
el.decoded.textContent=d.decoded_signals+'/'+d.total_signals+' decoded';
state.hist.push({ts:d.ts,n:d.total_signals,decoded:d.decoded_signals});
if(state.hist.length>80)state.hist.shift();
renderTimeline();
renderSignals(d.top_signals);
renderProtocols(d.protocol_stats);
addLog(d);
if(d.new_signals&&d.new_signals.length)showAlerts(d.new_signals);
}

function renderWave(sig){
const t=sig.timings;
if(!t||t.length<4||!wavePath)return;
el.waveLabel.textContent=sig.label||'Signal';
el.waveMeta.textContent=sig.freq+' MHz · '+sig.n_pulses+' pulses · '+sig.duration_ms+'ms';
const w=400,h=80,p=8;
const xs=(w-p*2)/Math.min(t.length,80);
let x=p;
let d='M '+p+' '+(h/2);
t.slice(0,80).forEach(v=>{const y=v>0?p:h-p;d+=' L '+x+' '+y;x+=xs;d+=' L '+x+' '+y});
const col=colors[sig.freq]||'var(--green)';
wavePath.setAttribute('d',d);
wavePath.setAttribute('style','fill:none;stroke-width:1.5;stroke:'+col);
}

function renderTimeline(){
const c=el.timeline;
const hist=state.hist;
if(hist.length<2){c.innerHTML='';return}
const w=c.clientWidth||400,h=100,p={t:10,r:10,b:20,l:30};
const maxV=Math.max(...hist.map(h=>h.n),1);
const xs=(w-p.l-p.r)/(hist.length-1),ys=(h-p.t-p.b)/maxV;
let line='',dline='';
hist.forEach((pt,i)=>{
const x=p.l+i*xs,y=h-p.b-pt.n*ys,dy=h-p.b-pt.decoded*ys;
line+=(i===0?'M':'L')+' '+x+' '+y;
dline+=(i===0?'M':'L')+' '+x+' '+dy;
});
const area=line+' L '+(p.l+(hist.length-1)*xs)+' '+(h-p.b)+' L '+p.l+' '+(h-p.b)+' Z';
c.innerHTML='<svg viewBox="0 0 '+w+' '+h+'" preserveAspectRatio="none"><path d="'+area+'" fill="var(--green)" opacity=".1"/><path d="'+line+'" fill="none" stroke="var(--green)" stroke-width="1.5"/><path d="'+dline+'" fill="none" stroke="var(--blue)" stroke-width="1.5" stroke-dasharray="3,2"/><text x="'+(p.l-4)+'" y="'+(p.t+4)+'" fill="var(--dim)" font-size="9" text-anchor="end">'+maxV+'</text><text x="'+(w-p.r)+'" y="'+(h-4)+'" fill="var(--dim)" font-size="9" text-anchor="end">signals / decoded</text></svg>';
}

function renderSignals(sigs){
if(!sigs||!sigs.length){
state.signalsReady=false;
el.sigCount.textContent='';
el.signals.innerHTML='<div style="color:var(--dim);padding:12px">Waiting for signals...</div>';
signalEls.clear();
return;
}

if(!state.signalsReady){
state.signalsReady=true;
el.signals.textContent='';
}

el.sigCount.textContent=sigs.length+' active';
const seen=new Set();
sigs.forEach(s=>{
if(!s||!s.fp)return;
seen.add(s.fp);
const node=upsertSignal(s);
el.signals.appendChild(node);
});

[...signalEls.entries()].forEach(([fp,parts])=>{
if(!seen.has(fp)){
parts.root.remove();
signalEls.delete(fp);
}
});
updateSignalAges();
}

function upsertSignal(s){
let parts=signalEls.get(s.fp);
if(!parts){
const root=document.createElement('div');
root.className='signal-item';
root.dataset.fp=s.fp;

const icon=document.createElement('div');
icon.className='signal-icon';

const info=document.createElement('div');
info.className='signal-info';

const label=document.createElement('div');
label.className='signal-label';

const meta=document.createElement('div');
meta.className='signal-meta';
const fpEl=document.createElement('span');
fpEl.className='signal-fp';
const freqEl=document.createElement('span');
const pulsesEl=document.createElement('span');
meta.appendChild(fpEl);
meta.appendChild(freqEl);
meta.appendChild(pulsesEl);

const mini=document.createElement('div');
mini.className='mini-hist';
const bars=[];
for(let i=0;i<8;i++){
const b=document.createElement('span');
b.style.height='10%';
mini.appendChild(b);
bars.push(b);
}

info.appendChild(label);
info.appendChild(meta);
info.appendChild(mini);

const stats=document.createElement('div');
stats.className='signal-stats';
const count=document.createElement('div');
count.className='signal-count';
const age=document.createElement('div');
age.className='signal-age';
stats.appendChild(count);
stats.appendChild(age);

root.appendChild(icon);
root.appendChild(info);
root.appendChild(stats);

parts={root,icon,label,fpEl,freqEl,pulsesEl,count,age,bars};
signalEls.set(s.fp,parts);
}
updateSignal(parts,s);
return parts.root;
}

function updateSignal(parts,s){
const proto=s.protocol||{};
parts.icon.textContent=proto.icon||'📻';
parts.label.textContent=s.label||'Unknown';
parts.fpEl.textContent=s.fp;
parts.freqEl.textContent=(s.freq||'--')+' MHz';
parts.pulsesEl.textContent=(s.n_pulses||'--')+' pulses';
parts.root.dataset.ts=s.ts||'';
parts.count.textContent='×'+(s.count||1);

const hist=s.histogram||[];
const maxH=Math.max(...hist,1);
for(let i=0;i<parts.bars.length;i++){
const v=hist[i]||0;
parts.bars[i].style.height=((v/maxH)*100)+'%';
}
}

function updateSignalAges(){
const now=Date.now()/1000;
signalEls.forEach(parts=>{
const ts=parseFloat(parts.root.dataset.ts||'0');
if(!ts)return;
const age=now-ts;
parts.age.textContent=age<60?Math.round(age)+'s':Math.round(age/60)+'m';
parts.root.classList.toggle('new',age<5);
});
}

function renderProtocols(stats){
const container=el.protocols;
if(!stats||!Object.keys(stats).length){
state.protocolsReady=false;
container.innerHTML='<div style="color:var(--dim);padding:8px">No protocols detected</div>';
protocolEls.clear();
return;
}

if(!state.protocolsReady){
state.protocolsReady=true;
container.textContent='';
}

const total=Object.values(stats).reduce((a,b)=>a+b,0)||1;
const entries=Object.entries(stats);
const seen=new Set();
entries.forEach(([p,n])=>{
seen.add(p);
let row=protocolEls.get(p);
if(!row){
const root=document.createElement('div');
root.className='proto-row';
const icon=document.createElement('div');
icon.className='proto-icon';
icon.textContent=protoIcons[p]||'📻';
const name=document.createElement('div');
name.className='proto-name';
name.textContent=p.replace(/_/g,' ');
const bar=document.createElement('div');
bar.className='proto-bar';
const fill=document.createElement('div');
fill.className='proto-fill';
bar.appendChild(fill);
const count=document.createElement('div');
count.className='proto-count';
root.appendChild(icon);
root.appendChild(name);
root.appendChild(bar);
root.appendChild(count);
row={root,fill,count};
protocolEls.set(p,row);
}
const pct=(n/total)*100;
row.fill.style.width=pct+'%';
row.fill.style.background=protoColors[p]||'var(--blue)';
row.count.textContent=String(n);
container.appendChild(row.root);
});

[...protocolEls.entries()].forEach(([p,row])=>{
if(!seen.has(p)){
row.root.remove();
protocolEls.delete(p);
}
});
}

function addLog(d){
const l=el.log;
const ts=new Date((d.ts||Date.now()/1000)*1000).toLocaleTimeString();
const newSigs=d.new_signals||[];
const newStr=newSigs.length?' <span class="log-new">+'+newSigs.length+' NEW</span>':'';
const msg=d._msg?' '+String(d._msg):'';
const e=document.createElement('div');
e.className='log-entry';
e.innerHTML='<span class="log-ts">'+ts+'</span><span>'+(d.total_signals||0)+' sig ('+(d.decoded_signals||0)+' decoded)'+newStr+msg+'</span>';
l.insertBefore(e,l.firstChild);
while(l.children.length>40)l.removeChild(l.lastChild);
}

function showAlerts(sigs){
sigs.forEach((s,i)=>{
setTimeout(()=>{
const a=document.createElement('div');
a.className='alert';
a.textContent='NEW: '+s.label;
a.style.top=(60+i*40)+'px';
document.body.appendChild(a);
setTimeout(()=>a.remove(),3000);
},i*200);
});
}

buildFreqGrid();
buildWave();
connect();
setInterval(updateSignalAges,1000);
window.addEventListener('resize',renderTimeline);
</script></body></html>'''


async def main():
    print("=" * 50)
    print("RF Decode - Protocol Classification")
    print("=" * 50)

    config = build_config()
    config.work_dir.mkdir(parents=True, exist_ok=True)

    (config.work_dir / 'decode.html').write_text(
        DASHBOARD
        .replace('__WS_PORT__', str(config.ws_port))
        .replace('__FREQS__', json.dumps([hz_to_mhz(f) for f in config.freqs_hz])),
        encoding='utf-8',
    )

    threading.Thread(target=http_thread, args=(config,), daemon=True).start()
    threading.Thread(target=capture_thread, args=(config,), daemon=True).start()

    print(f"HTTP: http://localhost:{config.http_port}/decode.html")
    print(f"WebSocket: ws://localhost:{config.ws_port}")
    if config.mock:
        print("Mode: mock (synthetic signals)")
    print("Press Ctrl+C to stop\n")

    await websockets.serve(ws_handler, 'localhost', config.ws_port)
    await broadcast_loop()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped")
