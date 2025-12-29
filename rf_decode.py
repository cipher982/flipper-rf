#!/usr/bin/env python3
"""
RF Decode - Protocol classification and semantic labeling
Analyzes timing patterns to identify signal types
"""

import asyncio
import json
import re
import time
import serial
import threading
import queue
import hashlib
from datetime import datetime
from collections import defaultdict, deque
from http.server import HTTPServer, SimpleHTTPRequestHandler
import websockets

# Config
FLIPPER_PORT = '/dev/cu.usbmodemflip_Ly0p11'
WS_PORT = 8766
HTTP_PORT = 8765
CAPTURE_DURATION = 0.8

# Shared state
data_queue = queue.Queue()
clients = set()

# Signal database
signal_db = {}  # fp -> full signal info
recent_signals = deque(maxlen=1000)
band_stats = defaultdict(lambda: {'signals': deque(maxlen=200), 'protocols': defaultdict(int)})


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


def capture_thread():
    """Capture and decode signals."""
    print("Capture thread starting...")

    try:
        ser = serial.Serial(FLIPPER_PORT, 230400, timeout=0.3)
        time.sleep(0.3)
        ser.read(ser.in_waiting)
        print(f"Flipper connected: {FLIPPER_PORT}")
    except Exception as e:
        print(f"Flipper error: {e}")
        data_queue.put({'type': 'error', 'msg': str(e)})
        return

    frequencies = [
        (315000000, 315.0),
        (433920000, 433.92),
        (868000000, 868.0),
        (915000000, 915.0),
    ]

    cycle = 0

    while True:
        cycle += 1
        cycle_start = time.time()

        all_signals = []
        freq_results = []
        new_signals = []
        decoded_count = 0

        for freq_hz, freq_mhz in frequencies:
            # Capture
            ser.write(f'subghz rx_raw {freq_hz}\r\n'.encode())

            raw = b''
            start = time.time()
            while time.time() - start < CAPTURE_DURATION:
                time.sleep(0.02)
                if ser.in_waiting:
                    raw += ser.read(ser.in_waiting)

            ser.write(b'\x03')
            time.sleep(0.05)
            raw += ser.read(ser.in_waiting or 2048)

            # Parse
            text = raw.decode('utf-8', errors='replace')
            timings = [int(t) for t in re.findall(r'([+-]\d+)', text)]

            # Extract and analyze bursts
            bursts = extract_bursts(timings)

            freq_signals = []
            for burst in bursts:
                analysis = analyze_timing_pattern(burst)
                if not analysis:
                    continue

                fp = compute_fingerprint(burst)
                if not fp:
                    continue

                # Classify protocol
                proto = classify_protocol(analysis, freq_mhz)

                signal = {
                    'fp': fp,
                    'freq': freq_mhz,
                    'ts': time.time(),
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
                        'first_seen': time.time(),
                        'last_seen': time.time(),
                        'count': 1,
                        'freq': freq_mhz,
                        'protocol': proto,
                        'label': signal['label'],
                    }
                    new_signals.append(signal)
                else:
                    signal_db[fp]['last_seen'] = time.time()
                    signal_db[fp]['count'] += 1

                # Add to recent
                recent_signals.append(signal)

                # Band stats
                band_stats[freq_mhz]['signals'].append(signal)
                if proto:
                    band_stats[freq_mhz]['protocols'][proto['protocol']] += 1

            all_signals.extend(freq_signals)

            # Frequency result
            proto_summary = {}
            for s in freq_signals:
                p = s.get('protocol', {}).get('protocol', 'unknown')
                proto_summary[p] = proto_summary.get(p, 0) + 1

            freq_results.append({
                'freq': freq_mhz,
                'n_signals': len(freq_signals),
                'n_transitions': len(timings),
                'protocols': proto_summary,
                'best_signal': freq_signals[0] if freq_signals else None,
            })

            # Stream immediately
            data_queue.put({'type': 'freq', 'data': freq_results[-1]})

        # Cycle summary
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

        # Log
        new_str = f" +{len(new_signals)} NEW" if new_signals else ""
        print(f"[{datetime.now().strftime('%H:%M:%S')}] C{cycle}: "
              f"{len(all_signals)} signals ({decoded_count} decoded), "
              f"{len(signal_db)} unique{new_str}")


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


def http_thread():
    """HTTP server."""
    import os
    os.chdir('/tmp/flipper_explore')

    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    HTTPServer(('localhost', HTTP_PORT), QuietHandler).serve_forever()


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
.dot{width:6px;height:6px;border-radius:50%;background:var(--dim)}
.dot.live{background:var(--green);box-shadow:0 0 8px var(--green)}
.main{display:flex;flex-direction:column;gap:1px;background:var(--border);overflow-y:auto}
.sidebar{display:flex;flex-direction:column;gap:1px;background:var(--border);overflow-y:auto}
.panel{background:var(--surface);padding:12px}
.section{font-size:10px;font-weight:600;color:var(--dim);text-transform:uppercase;letter-spacing:.5px;margin-bottom:8px;display:flex;justify-content:space-between}
.freq-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:6px}
.freq-card{background:var(--surface2);border-radius:4px;padding:8px;text-align:center;border:1px solid transparent;transition:border-color .2s}
.freq-card.active{border-color:var(--green)}
.freq-label{font-size:10px;color:var(--dim)}
.freq-value{font-size:22px;font-weight:700;font-family:inherit}
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
.mini-hist span{flex:1;background:var(--purple);border-radius:1px;align-self:flex-end;min-width:3px}
.protocol-breakdown{margin-top:8px}
.proto-row{display:flex;align-items:center;gap:8px;padding:4px 0;border-bottom:1px solid var(--border)}
.proto-row:last-child{border:none}
.proto-icon{font-size:14px;width:20px}
.proto-name{flex:1;font-size:11px}
.proto-bar{width:100px;height:4px;background:var(--border);border-radius:2px;overflow:hidden}
.proto-fill{height:100%;background:var(--blue)}
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
const WS='ws://localhost:8766';
let ws,freq={},hist=[],maxSig=1,totalDecoded=0;
const colors={315:'#3fb950',433.92:'#58a6ff',868:'#d29922',915:'#f85149'};
const freqs=[315,433.92,868,915];
const protoColors={princeton:'#3fb950',came_12bit:'#58a6ff',nice_flo:'#d29922',keeloq:'#f85149',oregon_v2:'#56d4dd',smart_meter:'#f0883e',tpms:'#a371f7',fixed_code:'#8b949e',unknown:'#6e7681'};

function connect(){
ws=new WebSocket(WS);
ws.onopen=()=>{$('dot').classList.add('live');$('conn').textContent='Live'};
ws.onclose=()=>{$('dot').classList.remove('live');$('conn').textContent='Reconnecting';setTimeout(connect,1000)};
ws.onmessage=e=>handle(JSON.parse(e.data));
}

function $(id){return document.getElementById(id)}
function handle(m){
if(m.type==='freq')updateFreq(m.data);
else if(m.type==='cycle')updateCycle(m);
else if(m.type==='init'){renderSignals(m.top_signals);renderProtocols(m.protocol_stats)}
}

function updateFreq(d){
freq[d.freq]=d;
maxSig=Math.max(maxSig,...Object.values(freq).map(f=>f.n_signals||0),1);
renderFreqGrid();
if(d.best_signal&&d.best_signal.timings)renderWave(d.best_signal);
}

function updateCycle(d){
$('cycle').textContent='C'+d.cycle+' ('+d.duration+'s)';
$('unique').textContent=d.total_unique+' unique';
totalDecoded+=d.decoded_signals;
$('decoded').textContent=d.decoded_signals+'/'+d.total_signals+' decoded';
hist.push({ts:d.ts,n:d.total_signals,decoded:d.decoded_signals});
if(hist.length>80)hist.shift();
renderTimeline();
renderSignals(d.top_signals);
renderProtocols(d.protocol_stats);
addLog(d);
if(d.new_signals&&d.new_signals.length)showAlerts(d.new_signals);
}

function renderFreqGrid(){
$('freqGrid').innerHTML=freqs.map(f=>{
const d=freq[f]||{n_signals:0,protocols:{}};
const pct=Math.min((d.n_signals/maxSig)*100,100);
const c=colors[f];
const protos=Object.entries(d.protocols||{}).slice(0,2).map(([p,n])=>p.replace('_',' ')).join(', ')||'--';
return '<div class="freq-card'+(d.n_signals>0?' active':'')+'"><div class="freq-label">'+f+' MHz</div><div class="freq-value" style="color:'+(d.n_signals?c:'var(--dim)')+'">'+(d.n_signals||0)+'</div><div class="freq-bar"><div class="freq-fill" style="width:'+pct+'%;background:'+c+'"></div></div><div class="freq-protos">'+protos+'</div></div>';
}).join('');
}

function renderWave(sig){
const c=$('wave'),t=sig.timings;
if(!t||t.length<4)return;
$('waveLabel').textContent=sig.label||'Signal';
$('waveMeta').textContent=sig.freq+' MHz · '+sig.n_pulses+' pulses · '+sig.duration_ms+'ms';
const w=c.clientWidth,h=80,p=8;
const xs=(w-p*2)/Math.min(t.length,80);
let path='M '+p+' '+h/2,x=p;
t.slice(0,80).forEach(v=>{const y=v>0?p:h-p;path+=' L '+x+' '+y;x+=xs;path+=' L '+x+' '+y});
const col=colors[sig.freq]||'var(--green)';
c.innerHTML='<svg viewBox="0 0 '+w+' '+h+'"><path d="'+path+'" style="stroke:'+col+'"/></svg><div class="wave-label">'+$('waveLabel').textContent+'</div>';
}

function renderTimeline(){
const c=$('timeline');
if(hist.length<2)return;
const w=c.clientWidth,h=100,p={t:10,r:10,b:20,l:30};
const maxV=Math.max(...hist.map(h=>h.n),1);
const xs=(w-p.l-p.r)/(hist.length-1),ys=(h-p.t-p.b)/maxV;
let line='',dline='';
hist.forEach((pt,i)=>{
const x=p.l+i*xs,y=h-p.b-pt.n*ys,dy=h-p.b-pt.decoded*ys;
line+=(i===0?'M':'L')+' '+x+' '+y;
dline+=(i===0?'M':'L')+' '+x+' '+dy;
});
const area=line+' L '+(p.l+(hist.length-1)*xs)+' '+(h-p.b)+' L '+p.l+' '+(h-p.b)+' Z';
c.innerHTML='<svg viewBox="0 0 '+w+' '+h+'"><path d="'+area+'" fill="var(--green)" opacity=".1"/><path d="'+line+'" fill="none" stroke="var(--green)" stroke-width="1.5"/><path d="'+dline+'" fill="none" stroke="var(--blue)" stroke-width="1.5" stroke-dasharray="3,2"/><text x="'+(p.l-4)+'" y="'+(p.t+4)+'" fill="var(--dim)" font-size="9" text-anchor="end">'+maxV+'</text><text x="'+(w-p.r)+'" y="'+(h-4)+'" fill="var(--dim)" font-size="9" text-anchor="end">signals / decoded</text></svg>';
}

function renderSignals(sigs){
if(!sigs||!sigs.length){$('signals').innerHTML='<div style="color:var(--dim);padding:12px">Waiting for signals...</div>';return}
$('sigCount').textContent=sigs.length+' active';
const now=Date.now()/1000;
$('signals').innerHTML=sigs.map(s=>{
const age=now-(s.ts||now);
const ageStr=age<60?Math.round(age)+'s':Math.round(age/60)+'m';
const isNew=age<5;
const proto=s.protocol||{};
const hist=s.histogram||[];
const maxH=Math.max(...hist,1);
return '<div class="signal-item'+(isNew?' new':'')+'"><div class="signal-icon">'+(proto.icon||'📻')+'</div><div class="signal-info"><div class="signal-label">'+(s.label||'Unknown')+'</div><div class="signal-meta"><span class="signal-fp">'+s.fp+'</span><span>'+s.freq+' MHz</span><span>'+(s.n_pulses||'--')+' pulses</span></div><div class="mini-hist">'+hist.map(v=>'<span style="height:'+((v/maxH)*100)+'%"></span>').join('')+'</div></div><div class="signal-stats"><div class="signal-count">×'+(s.count||1)+'</div><div class="signal-age">'+ageStr+'</div></div></div>';
}).join('');
}

function renderProtocols(stats){
if(!stats||!Object.keys(stats).length){$('protocols').innerHTML='<div style="color:var(--dim);padding:8px">No protocols detected</div>';return}
const total=Object.values(stats).reduce((a,b)=>a+b,0);
const icons={princeton:'🚗',came_12bit:'🚧',nice_flo:'🚧',keeloq:'🔐',oregon_v2:'🌡️',smart_meter:'⚡',tpms:'🛞',fixed_code:'📻',fsk_signal:'📶',slow_signal:'📡',complex_signal:'❓',ook_signal:'📻',unknown:'❓'};
$('protocols').innerHTML=Object.entries(stats).map(([p,n])=>{
const pct=(n/total)*100;
return '<div class="proto-row"><div class="proto-icon">'+(icons[p]||'📻')+'</div><div class="proto-name">'+p.replace(/_/g,' ')+'</div><div class="proto-bar"><div class="proto-fill" style="width:'+pct+'%;background:'+(protoColors[p]||'var(--blue)')+'"></div></div><div class="proto-count">'+n+'</div></div>';
}).join('');
}

function addLog(d){
const l=$('log');
const ts=new Date(d.ts*1000).toLocaleTimeString();
const newSigs=d.new_signals||[];
const newStr=newSigs.length?' <span class="log-new">+'+newSigs.length+' NEW ('+newSigs.map(s=>s.label.split(' ')[0]).join(', ')+')</span>':'';
const e=document.createElement('div');
e.className='log-entry';
e.innerHTML='<span class="log-ts">'+ts+'</span><span>'+d.total_signals+' sig ('+d.decoded_signals+' decoded)'+newStr+'</span>';
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

renderFreqGrid();
connect();
window.onresize=renderTimeline;
</script></body></html>'''


async def main():
    print("=" * 50)
    print("RF Decode - Protocol Classification")
    print("=" * 50)

    with open('/tmp/flipper_explore/decode.html', 'w') as f:
        f.write(DASHBOARD)

    threading.Thread(target=http_thread, daemon=True).start()
    threading.Thread(target=capture_thread, daemon=True).start()

    print(f"HTTP: http://localhost:{HTTP_PORT}/decode.html")
    print(f"WebSocket: ws://localhost:{WS_PORT}")
    print("Press Ctrl+C to stop\n")

    await websockets.serve(ws_handler, 'localhost', WS_PORT)
    await broadcast_loop()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped")
