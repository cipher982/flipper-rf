#!/usr/bin/env python3
"""
RF Intelligence Stream - Real signal analysis, not just counting
Fast capture + fingerprinting + multi-sense
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
CAPTURE_DURATION = 0.6  # Faster captures

# Shared state
data_queue = queue.Queue()
clients = set()

# Signal intelligence state
fingerprint_db = {}  # hash -> {first_seen, last_seen, count, freq, avg_duration, pulses}
recent_fingerprints = deque(maxlen=500)  # Last 500 fingerprints with timestamps
band_stats = defaultdict(lambda: {'bursts': deque(maxlen=100), 'fingerprints': set()})


def extract_bursts(timings, gap_threshold=50000):
    """Split timing array into distinct bursts based on long gaps."""
    if not timings:
        return []

    bursts = []
    current = []

    for t in timings:
        if abs(t) > gap_threshold and current:
            if len(current) > 3:  # Min 3 transitions for a real burst
                bursts.append(current)
            current = []
        else:
            current.append(t)

    if len(current) > 3:
        bursts.append(current)

    return bursts


def compute_fingerprint(burst):
    """Compute stable fingerprint from burst timing pattern."""
    if len(burst) < 4:
        return None

    # Get on-pulses only, normalize
    on_pulses = [t for t in burst if t > 0]
    if len(on_pulses) < 2:
        return None

    # Quantize to 50µs buckets for stability
    quantized = [t // 50 * 50 for t in on_pulses]

    # Create fingerprint from pattern
    pattern = ','.join(str(t) for t in quantized[:20])  # First 20 pulses
    fp_hash = hashlib.md5(pattern.encode()).hexdigest()[:8]

    return fp_hash


def analyze_burst(burst):
    """Extract rich features from a burst."""
    on_pulses = [t for t in burst if t > 0]
    off_gaps = [abs(t) for t in burst if t < 0]

    if not on_pulses:
        return None

    # Compute stats
    duration_us = sum(abs(t) for t in burst)

    return {
        'n_transitions': len(burst),
        'n_pulses': len(on_pulses),
        'duration_ms': round(duration_us / 1000, 1),
        'pulse_min': min(on_pulses),
        'pulse_max': max(on_pulses),
        'pulse_mean': sum(on_pulses) // len(on_pulses),
        'pulse_median': sorted(on_pulses)[len(on_pulses)//2],
        'gap_mean': sum(off_gaps) // len(off_gaps) if off_gaps else 0,
        # Pulse width histogram buckets
        'histogram': compute_histogram(on_pulses),
    }


def compute_histogram(pulses, buckets=8):
    """Compute pulse width histogram."""
    if not pulses:
        return [0] * buckets

    # Log-scale buckets: <100, 100-300, 300-1k, 1k-3k, 3k-10k, 10k-30k, 30k-100k, >100k
    ranges = [100, 300, 1000, 3000, 10000, 30000, 100000, float('inf')]
    hist = [0] * buckets

    for p in pulses:
        for i, threshold in enumerate(ranges):
            if p < threshold:
                hist[i] += 1
                break

    return hist


def compute_band_health(freq_mhz, window_sec=60):
    """Compute band health metrics from recent data."""
    stats = band_stats[freq_mhz]
    now = time.time()

    # Filter to window
    recent = [b for b in stats['bursts'] if now - b['ts'] < window_sec]

    if not recent:
        return {
            'burst_rate': 0,
            'unique_per_min': 0,
            'entropy': 0,
            'duty_cycle': 0,
        }

    # Burst rate
    burst_rate = len(recent) / window_sec * 60  # per minute

    # Unique fingerprints in window
    window_fps = set(b['fp'] for b in recent if b.get('fp'))
    unique_per_min = len(window_fps)

    # Entropy (diversity of fingerprints)
    fp_counts = defaultdict(int)
    for b in recent:
        if b.get('fp'):
            fp_counts[b['fp']] += 1

    total = sum(fp_counts.values())
    entropy = 0
    if total > 0:
        import math
        for count in fp_counts.values():
            p = count / total
            if p > 0:
                entropy -= p * math.log2(p)

    # Duty cycle (rough estimate)
    total_duration = sum(b.get('duration_ms', 0) for b in recent)
    duty_cycle = min(100, total_duration / (window_sec * 10))  # Normalize

    return {
        'burst_rate': round(burst_rate, 1),
        'unique_per_min': unique_per_min,
        'entropy': round(entropy, 2),
        'duty_cycle': round(duty_cycle, 1),
    }


def get_top_fingerprints(limit=8):
    """Get top fingerprints by recent activity."""
    now = time.time()

    # Score by recency and frequency
    scores = defaultdict(float)
    details = {}

    for fp_data in recent_fingerprints:
        fp = fp_data.get('fp')
        if not fp:
            continue

        age = now - fp_data['ts']
        recency_score = max(0, 1 - age / 300)  # Decay over 5 min
        scores[fp] += recency_score

        if fp not in details or fp_data['ts'] > details[fp]['last_seen']:
            details[fp] = {
                'fp': fp,
                'freq': fp_data['freq'],
                'last_seen': fp_data['ts'],
                'duration_ms': fp_data.get('duration_ms', 0),
                'n_pulses': fp_data.get('n_pulses', 0),
                'histogram': fp_data.get('histogram', []),
            }

    # Merge with DB for counts
    for fp in details:
        if fp in fingerprint_db:
            details[fp]['count'] = fingerprint_db[fp]['count']
            details[fp]['first_seen'] = fingerprint_db[fp]['first_seen']
        else:
            details[fp]['count'] = 1

    # Sort by score
    top = sorted(scores.items(), key=lambda x: -x[1])[:limit]

    return [details[fp] for fp, _ in top if fp in details]


def get_device_info(ser):
    """Get Flipper device telemetry."""
    try:
        ser.write(b'uptime\r\n')
        time.sleep(0.1)
        data = ser.read(ser.in_waiting or 256).decode('utf-8', errors='replace')

        uptime_match = re.search(r'Uptime:\s*(\S+)', data)
        uptime = uptime_match.group(1) if uptime_match else '--'

        # Could add: battery, temp, etc. via 'info power'
        return {
            'uptime': uptime,
            'connected': True,
            'port': FLIPPER_PORT.split('/')[-1],
        }
    except:
        return {'uptime': '--', 'connected': False, 'port': '--'}


def capture_thread():
    """Fast capture with intelligence extraction."""
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
    last_telemetry = 0

    while True:
        cycle += 1
        cycle_start = time.time()

        all_bursts = []
        freq_results = []
        new_signals = []

        for freq_hz, freq_mhz in frequencies:
            # Fast capture
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

            # Parse timings
            text = raw.decode('utf-8', errors='replace')
            timings = [int(t) for t in re.findall(r'([+-]\d+)', text)]

            # Extract bursts
            bursts = extract_bursts(timings)

            # Analyze each burst
            freq_bursts = []
            for burst in bursts:
                analysis = analyze_burst(burst)
                if not analysis:
                    continue

                fp = compute_fingerprint(burst)
                analysis['fp'] = fp
                analysis['freq'] = freq_mhz
                analysis['ts'] = time.time()
                analysis['timings'] = burst[:60]  # Keep some raw data

                freq_bursts.append(analysis)

                # Track fingerprint
                if fp:
                    is_new = fp not in fingerprint_db

                    if is_new:
                        fingerprint_db[fp] = {
                            'first_seen': time.time(),
                            'last_seen': time.time(),
                            'count': 1,
                            'freq': freq_mhz,
                        }
                        new_signals.append({
                            'fp': fp,
                            'freq': freq_mhz,
                            'ts': time.time(),
                        })
                    else:
                        fingerprint_db[fp]['last_seen'] = time.time()
                        fingerprint_db[fp]['count'] += 1

                    # Add to recent
                    recent_fingerprints.append({
                        'fp': fp,
                        'freq': freq_mhz,
                        'ts': time.time(),
                        'duration_ms': analysis['duration_ms'],
                        'n_pulses': analysis['n_pulses'],
                        'histogram': analysis['histogram'],
                    })

                    # Track band stats
                    band_stats[freq_mhz]['bursts'].append({
                        'ts': time.time(),
                        'fp': fp,
                        'duration_ms': analysis['duration_ms'],
                    })
                    band_stats[freq_mhz]['fingerprints'].add(fp)

            all_bursts.extend(freq_bursts)

            # Freq summary
            freq_results.append({
                'freq': freq_mhz,
                'n_bursts': len(freq_bursts),
                'n_transitions': len(timings),
                'health': compute_band_health(freq_mhz),
                'best_burst': freq_bursts[0] if freq_bursts else None,
            })

            # Stream freq update immediately
            data_queue.put({
                'type': 'freq',
                'data': freq_results[-1],
            })

        # Cycle summary
        cycle_data = {
            'type': 'cycle',
            'cycle': cycle,
            'ts': time.time(),
            'duration': round(time.time() - cycle_start, 2),
            'total_bursts': len(all_bursts),
            'total_transitions': sum(r['n_transitions'] for r in freq_results),
            'freqs': freq_results,
            'top_fingerprints': get_top_fingerprints(8),
            'new_signals': new_signals,
            'total_unique': len(fingerprint_db),
        }

        data_queue.put(cycle_data)

        # Telemetry every 10 cycles
        if time.time() - last_telemetry > 10:
            data_queue.put({
                'type': 'telemetry',
                'data': get_device_info(ser),
            })
            last_telemetry = time.time()

        # Log
        active = sum(1 for r in freq_results if r['n_bursts'] > 0)
        new_count = len(new_signals)
        new_str = f" +{new_count} NEW" if new_count else ""
        print(f"[{datetime.now().strftime('%H:%M:%S')}] C{cycle}: "
              f"{len(all_bursts)} bursts, {active}/4 active, "
              f"{len(fingerprint_db)} unique{new_str}")


async def broadcast_loop():
    """Fast broadcast to clients."""
    while True:
        try:
            msgs = []
            # Drain queue quickly
            while not data_queue.empty():
                try:
                    msgs.append(data_queue.get_nowait())
                except queue.Empty:
                    break

            if msgs and clients:
                for msg in msgs:
                    payload = json.dumps(msg)
                    await asyncio.gather(
                        *[c.send(payload) for c in clients],
                        return_exceptions=True
                    )

            await asyncio.sleep(0.02)  # 50Hz tick
        except Exception as e:
            print(f"Broadcast error: {e}")
            await asyncio.sleep(0.5)


async def ws_handler(websocket, path=None):
    """WebSocket handler."""
    clients.add(websocket)
    print(f"Client connected ({len(clients)})")

    # Send current state
    await websocket.send(json.dumps({
        'type': 'init',
        'total_unique': len(fingerprint_db),
        'top_fingerprints': get_top_fingerprints(8),
    }))

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


# Dashboard HTML - embedded
DASHBOARD = '''<!DOCTYPE html>
<html><head>
<meta charset="UTF-8"><title>RF Intel</title>
<style>
:root{--bg:#0d1117;--surface:#161b22;--border:#30363d;--text:#e6edf3;--dim:#7d8590;--green:#3fb950;--yellow:#d29922;--red:#f85149;--blue:#58a6ff;--purple:#a371f7}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,system-ui,sans-serif;background:var(--bg);color:var(--text);font-size:13px;overflow-x:hidden}
.container{display:grid;grid-template-columns:1fr 320px;grid-template-rows:auto 1fr;height:100vh;gap:1px;background:var(--border)}
.panel{background:var(--surface);padding:12px;overflow:hidden}
header{grid-column:1/-1;display:flex;justify-content:space-between;align-items:center;padding:8px 16px;background:var(--surface)}
h1{font-size:14px;font-weight:600;display:flex;align-items:center;gap:8px}
.status{display:flex;gap:16px;font-size:11px;color:var(--dim)}
.status-item{display:flex;align-items:center;gap:4px}
.dot{width:6px;height:6px;border-radius:50%;background:var(--dim)}
.dot.live{background:var(--green)}
.main{display:flex;flex-direction:column;gap:1px;background:var(--border)}
.sidebar{display:flex;flex-direction:column;gap:1px;background:var(--border);overflow-y:auto}
.section-title{font-size:10px;font-weight:600;color:var(--dim);text-transform:uppercase;letter-spacing:.5px;margin-bottom:8px;display:flex;justify-content:space-between}
.freq-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.freq-card{background:var(--bg);border-radius:4px;padding:8px;text-align:center}
.freq-label{font-size:10px;color:var(--dim)}
.freq-value{font-size:18px;font-weight:600;font-family:ui-monospace,monospace}
.freq-bar{height:3px;background:var(--border);border-radius:2px;margin-top:4px;overflow:hidden}
.freq-fill{height:100%;transition:width .2s}
.freq-meta{font-size:9px;color:var(--dim);margin-top:4px}
.waveform{height:70px;background:var(--bg);border-radius:4px;margin-top:8px}
.waveform svg{width:100%;height:100%}
.waveform path{fill:none;stroke-width:1.5}
.histogram{display:flex;gap:1px;height:40px;background:var(--bg);border-radius:4px;padding:4px;margin-top:8px}
.hist-bar{flex:1;background:var(--blue);border-radius:1px;transition:height .2s;align-self:flex-end}
.timeline{height:80px;background:var(--bg);border-radius:4px;margin-top:8px}
.timeline svg{width:100%;height:100%}
.fp-list{display:flex;flex-direction:column;gap:4px;max-height:300px;overflow-y:auto}
.fp-item{background:var(--bg);border-radius:4px;padding:8px;display:flex;gap:8px;align-items:center}
.fp-item.new{border-left:2px solid var(--green)}
.fp-hash{font-family:ui-monospace,monospace;font-size:11px;color:var(--blue);min-width:60px}
.fp-freq{font-size:10px;color:var(--dim);min-width:50px}
.fp-count{font-size:11px;font-weight:600;min-width:30px}
.fp-mini-hist{display:flex;gap:1px;height:16px;flex:1}
.fp-mini-hist span{flex:1;background:var(--purple);border-radius:1px;align-self:flex-end}
.fp-age{font-size:9px;color:var(--dim)}
.health-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:4px}
.health-item{background:var(--bg);border-radius:4px;padding:6px;text-align:center}
.health-value{font-size:14px;font-weight:600;font-family:ui-monospace,monospace}
.health-label{font-size:9px;color:var(--dim)}
.device-strip{display:flex;gap:12px;font-size:10px;color:var(--dim)}
.log{font-family:ui-monospace,monospace;font-size:10px;max-height:120px;overflow-y:auto;background:var(--bg);border-radius:4px;padding:6px;margin-top:8px}
.log-entry{padding:2px 0;border-bottom:1px solid var(--border);display:flex;gap:8px}
.log-ts{color:var(--dim);min-width:55px}
.log-new{color:var(--green)}
.alert{background:var(--green);color:var(--bg);padding:4px 8px;border-radius:4px;font-size:10px;font-weight:600;animation:pulse .5s}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.7}}
</style></head><body>
<div class="container">
<header>
<h1><span>📡</span> RF Intel</h1>
<div class="status">
<div class="status-item"><div class="dot" id="dot"></div><span id="connStatus">Connecting</span></div>
<div class="status-item" id="cycleStatus">--</div>
<div class="status-item" id="uniqueStatus">0 unique</div>
<div class="device-strip" id="device"></div>
</div>
</header>
<div class="main">
<div class="panel">
<div class="section-title">Frequency Bands</div>
<div class="freq-grid" id="freqGrid"></div>
<div class="waveform" id="waveform"></div>
</div>
<div class="panel">
<div class="section-title">Pulse Histogram<span id="histMeta"></span></div>
<div class="histogram" id="histogram"></div>
</div>
<div class="panel">
<div class="section-title">Activity Timeline</div>
<div class="timeline" id="timeline"></div>
</div>
<div class="panel">
<div class="section-title">Log</div>
<div class="log" id="log"></div>
</div>
</div>
<div class="sidebar">
<div class="panel">
<div class="section-title">Top Signals<span id="fpCount"></span></div>
<div class="fp-list" id="fpList"></div>
</div>
<div class="panel">
<div class="section-title">Band Health (60s window)</div>
<div class="health-grid" id="health"></div>
</div>
</div>
</div>
<script>
const WS='ws://localhost:8766';
let ws,freq={},hist=[],maxBursts=1,uniqueCount=0;
const colors={315:'#3fb950',433.92:'#58a6ff',868:'#d29922',915:'#f85149'};
const freqOrder=[315,433.92,868,915];

function connect(){
ws=new WebSocket(WS);
ws.onopen=()=>{$('dot').classList.add('live');$('connStatus').textContent='Live'};
ws.onclose=()=>{$('dot').classList.remove('live');$('connStatus').textContent='Reconnecting';setTimeout(connect,1000)};
ws.onmessage=e=>handle(JSON.parse(e.data));
}

function $(id){return document.getElementById(id)}
function handle(m){
if(m.type==='freq')updateFreq(m.data);
else if(m.type==='cycle')updateCycle(m);
else if(m.type==='telemetry')updateDevice(m.data);
else if(m.type==='init'){uniqueCount=m.total_unique;renderFpList(m.top_fingerprints)}
}

function updateFreq(d){
freq[d.freq]=d;
maxBursts=Math.max(maxBursts,...Object.values(freq).map(f=>f.n_bursts||0),1);
renderFreqGrid();
if(d.best_burst&&d.best_burst.timings)renderWaveform(d.best_burst,d.freq);
if(d.best_burst&&d.best_burst.histogram)renderHistogram(d.best_burst.histogram,d.freq);
}

function updateCycle(d){
$('cycleStatus').textContent='C'+d.cycle+' · '+d.duration+'s';
uniqueCount=d.total_unique;
$('uniqueStatus').textContent=uniqueCount+' unique';
hist.push({ts:d.ts,n:d.total_bursts,active:d.freqs.filter(f=>f.n_bursts>0).length});
if(hist.length>100)hist.shift();
renderTimeline();
renderFpList(d.top_fingerprints);
renderHealth(d.freqs);
addLog(d);
if(d.new_signals&&d.new_signals.length)showNewAlert(d.new_signals);
}

function updateDevice(d){
$('device').innerHTML='<span>⏱ '+d.uptime+'</span><span>📟 '+d.port+'</span>';
}

function renderFreqGrid(){
$('freqGrid').innerHTML=freqOrder.map(f=>{
const d=freq[f]||{n_bursts:0,n_transitions:0,health:{}};
const pct=Math.min((d.n_bursts/maxBursts)*100,100);
const c=colors[f];
const h=d.health||{};
return '<div class="freq-card"><div class="freq-label">'+f+' MHz</div><div class="freq-value" style="color:'+(d.n_bursts?c:'var(--dim)')+'">'+d.n_bursts+'</div><div class="freq-bar"><div class="freq-fill" style="width:'+pct+'%;background:'+c+'"></div></div><div class="freq-meta">'+(h.unique_per_min||0)+' uniq/min · H'+((h.entropy||0).toFixed(1))+'</div></div>';
}).join('');
}

function renderWaveform(burst,freqMhz){
const c=$('waveform');
const t=burst.timings;
if(!t||t.length<3)return;
const w=c.clientWidth,h=70,p=4;
const xs=(w-p*2)/t.length;
let path='M '+p+' '+h/2,x=p;
t.forEach(v=>{const y=v>0?p:h-p;path+=' L '+x+' '+y;x+=xs;path+=' L '+x+' '+y});
c.innerHTML='<svg viewBox="0 0 '+w+' '+h+'"><path d="'+path+'" style="stroke:'+(colors[freqMhz]||'var(--green)')+'"/></svg>';
}

function renderHistogram(hist,freqMhz){
const c=$('histogram');
const max=Math.max(...hist,1);
c.innerHTML=hist.map(v=>'<div class="hist-bar" style="height:'+((v/max)*100)+'%;background:'+(colors[freqMhz]||'var(--blue)')+'"></div>').join('');
$('histMeta').textContent=hist.reduce((a,b)=>a+b,0)+' pulses';
}

function renderTimeline(){
const c=$('timeline');
if(hist.length<2)return;
const w=c.clientWidth,h=80,p={t:8,r:8,b:12,l:24};
const maxV=Math.max(...hist.map(h=>h.n),1);
const xs=(w-p.l-p.r)/(hist.length-1),ys=(h-p.t-p.b)/maxV;
let line='';
hist.forEach((pt,i)=>{const x=p.l+i*xs,y=h-p.b-pt.n*ys;line+=(i===0?'M':'L')+' '+x+' '+y});
const area=line+' L '+(p.l+(hist.length-1)*xs)+' '+(h-p.b)+' L '+p.l+' '+(h-p.b)+' Z';
c.innerHTML='<svg viewBox="0 0 '+w+' '+h+'"><path d="'+area+'" fill="var(--green)" opacity=".15"/><path d="'+line+'" fill="none" stroke="var(--green)" stroke-width="1.5"/><text x="'+(p.l-4)+'" y="'+(p.t+4)+'" fill="var(--dim)" font-size="9" text-anchor="end">'+maxV+'</text></svg>';
}

function renderFpList(fps){
if(!fps||!fps.length){$('fpList').innerHTML='<div style="color:var(--dim);padding:8px">Waiting for signals...</div>';return}
$('fpCount').textContent=fps.length+' active';
const now=Date.now()/1000;
$('fpList').innerHTML=fps.map(fp=>{
const age=now-(fp.last_seen||now);
const ageStr=age<60?Math.round(age)+'s':Math.round(age/60)+'m';
const isNew=age<5;
const hist=fp.histogram||[];
const maxH=Math.max(...hist,1);
return '<div class="fp-item'+(isNew?' new':'')+'"><span class="fp-hash">'+fp.fp+'</span><span class="fp-freq">'+(fp.freq||'--')+' MHz</span><span class="fp-count">×'+(fp.count||1)+'</span><div class="fp-mini-hist">'+hist.map(v=>'<span style="height:'+((v/maxH)*100)+'%"></span>').join('')+'</div><span class="fp-age">'+ageStr+'</span></div>';
}).join('');
}

function renderHealth(freqs){
$('health').innerHTML=freqs.map(f=>{
const h=f.health||{};
return '<div class="health-item"><div class="health-value" style="color:'+(colors[f.freq]||'var(--dim)')+'">'+(h.burst_rate||0).toFixed(1)+'</div><div class="health-label">'+f.freq+' MHz burst/min</div></div><div class="health-item"><div class="health-value">'+(h.entropy||0).toFixed(1)+'</div><div class="health-label">entropy</div></div>';
}).join('');
}

function addLog(d){
const l=$('log');
const ts=new Date(d.ts*1000).toLocaleTimeString();
const newStr=d.new_signals&&d.new_signals.length?' <span class="log-new">+'+d.new_signals.length+' NEW</span>':'';
const e=document.createElement('div');
e.className='log-entry';
e.innerHTML='<span class="log-ts">'+ts+'</span><span>'+d.total_bursts+' bursts · '+d.freqs.filter(f=>f.n_bursts>0).length+'/4'+newStr+'</span>';
l.insertBefore(e,l.firstChild);
while(l.children.length>30)l.removeChild(l.lastChild);
}

function showNewAlert(signals){
signals.forEach(s=>{
const a=document.createElement('div');
a.className='alert';
a.textContent='NEW: '+s.fp+' @ '+s.freq+' MHz';
a.style.position='fixed';
a.style.top='60px';
a.style.right='16px';
document.body.appendChild(a);
setTimeout(()=>a.remove(),3000);
});
}

renderFreqGrid();
connect();
window.onresize=renderTimeline;
</script></body></html>'''


async def main():
    print("=" * 50)
    print("RF Intelligence Stream")
    print("=" * 50)

    with open('/tmp/flipper_explore/intel.html', 'w') as f:
        f.write(DASHBOARD)

    # Start threads
    threading.Thread(target=http_thread, daemon=True).start()
    threading.Thread(target=capture_thread, daemon=True).start()

    print(f"HTTP: http://localhost:{HTTP_PORT}/intel.html")
    print(f"WebSocket: ws://localhost:{WS_PORT}")
    print("Press Ctrl+C to stop\n")

    await websockets.serve(ws_handler, 'localhost', WS_PORT)
    await broadcast_loop()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped")
