#!/usr/bin/env python3
"""
IR Server (FastAPI + pigpio)

COGs-friendly GET endpoints (no headers/body required):
  Health:
    GET /cogs/health

  Send named code (remote/key from DB):
    GET /cogs/ir/send?remote=Fireplace&key=Power[&carrier=38]

  Send raw burst (milliseconds; accepts signed +/- or unsigned CSV):
    GET /cogs/ir/send_raw?ms=900,-450,900,-450[&carrier=38]

  Hold / repeat (start/stop/stop_all):
    GET /cogs/ir/hold/start?remote=Samsung_TV&key=VOL_UP&interval_ms=110&hold_id=x[&carrier=38]
    GET /cogs/ir/hold/start?ms=560,-560,...&interval_ms=110&hold_id=x[&carrier=38]
    GET /cogs/ir/hold/stop?hold_id=x
    GET /cogs/ir/hold/stop_all
    (Alias) GET /cogs/ir/stop     # stops all holds

  Learn (two-step):
    GET /cogs/ir/learn/start
    GET /cogs/ir/learn/stop/{remote}/{key}

  Manage learned codes:
    GET /cogs/ir/list
    GET /cogs/ir/delete/{code_name}

Environment (systemd Environment=...):
  IR_TX_GPIO         (default 18)
  IR_RX_GPIO         (default 23)
  IR_CARRIER_KHZ     (default 38.0)
  IR_CODES_DB        (default /opt/ir-server/ir_codes.json)
  IR_DEBUG           (default 0)

Notes:
- Stored codes are treated as **microseconds**; both signed (+/-) and unsigned
  arrays are accepted. send_raw takes **milliseconds** in the URL, but we convert
  to microseconds internally.
"""

import os, json, time, threading, signal
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

import pigpio

# ---- Config from env ----
IR_TX_GPIO = int(os.environ.get("IR_TX_GPIO", "18"))
IR_RX_GPIO = int(os.environ.get("IR_RX_GPIO", "23"))
IR_CARRIER_KHZ = float(os.environ.get("IR_CARRIER_KHZ", "38.0"))
IR_CODES_DB = os.environ.get("IR_CODES_DB", "/opt/ir-server/ir_codes.json")
IR_DEBUG = int(os.environ.get("IR_DEBUG", "0"))

# ---- pigpio robust handle/reconnect ----
_PI = None
_PI_LOCK = threading.Lock()

def get_pi():
    """(Re)connect to local pigpiod; return healthy pigpio.pi()."""
    global _PI
    with _PI_LOCK:
        if _PI is None:
            _PI = pigpio.pi()
        try:
            _PI.get_current_tick()
        except Exception:
            try:
                _PI.stop()
            except Exception:
                pass
            _PI = pigpio.pi()
        if not _PI.connected:
            raise RuntimeError("pigpiod not connected")
        return _PI

def _retry(fn, n=1, delay=0.05):
    """Run fn(), retrying once after forcing reconnect if it fails."""
    global _PI
    for i in range(n + 1):
        try:
            return fn()
        except Exception:
            if i < n:
                try:
                    _PI.stop()
                except Exception:
                    pass
                _PI = None
                time.sleep(delay)
            else:
                raise

# ---- DB helpers ----
def _load_codes():
    try:
        with open(IR_CODES_DB, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_codes(data):
    os.makedirs(os.path.dirname(IR_CODES_DB), exist_ok=True)
    with open(IR_CODES_DB, "w") as f:
        json.dump(data, f, indent=2)

# ---- Burst normalization ----
def normalize_burst_us(seq: List[int]) -> List[int]:
    """
    Accept either:
      - signed +/- microseconds: [+on, -off, +on, -off, ...]
      - OR unsigned microseconds: [on, off, on, off, ...]
    Drop zeros, enforce even length, and correct signs (even idx = +, odd idx = -).
    """
    arr = []
    for x in seq:
        try:
            xi = int(x)
        except Exception:
            continue
        if xi != 0:
            arr.append(xi)
    if not arr:
        raise ValueError("empty IR burst")

    has_negative = any(v < 0 for v in arr)
    out = []
    for i, v in enumerate(arr):
        v = abs(v)
        out.append(v if i % 2 == 0 else -v)

    # If original had negatives they were preserved by rule above.
    # Ensure even count
    if len(out) % 2 == 1:
        out = out[:-1]
    if not out:
        raise ValueError("invalid IR burst after normalization")
    return out

# ---- Carrier burst → pigpio pulses ----
def build_pulses_for_burst(tx_gpio: int, durations_us: List[int], carrier_khz: float):
    """
    Translate [+on, -off, ...] us durations into pigpio.pulse[] with carrier modulation
    during the +on segments using half-period toggles on tx_gpio.
    """
    pi = get_pi()
    pulses = []
    half_period = int(round(1_000_000 / (carrier_khz * 2.0)))  # us

    def mark(us: int):
        # Produce carrier on/off toggles for 'us' microseconds
        remaining = us
        while remaining > 0:
            d = half_period if remaining >= half_period else remaining
            pulses.append(pigpio.pulse(1 << tx_gpio, 0, d))  # ON
            pulses.append(pigpio.pulse(0, 1 << tx_gpio, d))  # OFF
            remaining -= (2 * d)

    def space(us: int):
        pulses.append(pigpio.pulse(0, 0, max(1, us)))  # idle low for 'us'

    it = iter(durations_us)
    for on_us in it:
        if on_us <= 0:
            continue
        mark(on_us)
        off_us = next(it, None)
        if off_us is None:
            break
        off_us = abs(int(off_us))
        if off_us > 0:
            space(off_us)

    return pulses

def send_raw_burst(durations_us: List[int], carrier_khz: float):
    pi = get_pi()
    durations = normalize_burst_us(durations_us)
    pulses = build_pulses_for_burst(IR_TX_GPIO, durations, carrier_khz)

    # Clear any pending transmission and send
    _retry(lambda: (pi.wave_tx_stop(), pi.wave_clear()))
    _retry(lambda: pi.wave_add_generic(pulses))
    wid = _retry(lambda: pi.wave_create())
    try:
        _retry(lambda: pi.wave_send_once(wid))
        while pi.wave_tx_busy():
            time.sleep(0.0015)
    finally:
        _retry(lambda: pi.wave_delete(wid))

# ---- Learn (edge capture on RX) ----
_learn_lock = threading.Lock()
_learn_active = False
_learn_edges: List[int] = []
_learn_cb = None
_learn_start_tick = 0

def _rx_callback(gpio, level, tick):
    # capture edges (level changes); compute microsecond deltas
    global _learn_start_tick
    if _learn_start_tick == 0:
        _learn_start_tick = tick
        return
    _learn_edges.append(pigpio.tickDiff(_learn_start_tick, tick))
    _learn_start_tick = tick

def learn_start():
    global _learn_active, _learn_edges, _learn_cb, _learn_start_tick
    with _learn_lock:
        if _learn_active:
            return
        _learn_active = True
        _learn_edges = []
        _learn_start_tick = 0
        pi = get_pi()
        pi.set_mode(IR_RX_GPIO, pigpio.INPUT)
        pi.set_pull_up_down(IR_RX_GPIO, pigpio.PUD_OFF)
        _learn_cb = pi.callback(IR_RX_GPIO, pigpio.EITHER_EDGE, _rx_callback)

def learn_stop_and_save(remote: str, key: str):
    global _learn_active, _learn_edges, _learn_cb
    with _learn_lock:
        if not _learn_active:
            raise RuntimeError("learn not active")
        try:
            if _learn_cb is not None:
                _learn_cb.cancel()
        finally:
            _learn_cb = None
            _learn_active = False

    # Convert captured edge gaps (unsigned us) to a normalized signed burst
    if IR_DEBUG:
        print(f"[learn] captured edges: {len(_learn_edges)}")
    # Some drivers output a 0 first; normalize handles it
    durations = normalize_burst_us(_learn_edges)
    # Save to DB under microseconds as a flat list (all-positive tolerated)
    db = _load_codes()
    db.setdefault(remote, {})[key] = [abs(int(x)) for x in durations]
    _save_codes(db)
    return durations

# ---- Hold/repeat management ----
_hold_threads = {}
_hold_stop_flags = {}

def _hold_worker(hold_id: str, burst_us: List[int], carrier: float, interval_ms: int):
    try:
        while not _hold_stop_flags.get(hold_id, False):
            send_raw_burst(burst_us, carrier)
            time.sleep(max(0.001, interval_ms / 1000.0))
    finally:
        _hold_stop_flags.pop(hold_id, None)
        _hold_threads.pop(hold_id, None)

def hold_start_from_burst(hold_id: str, burst_us: List[int], carrier: float, interval_ms: int):
    if hold_id in _hold_threads:
        raise RuntimeError(f"hold_id '{hold_id}' already running")
    _hold_stop_flags[hold_id] = False
    t = threading.Thread(target=_hold_worker, args=(hold_id, normalize_burst_us(burst_us), carrier, interval_ms), daemon=True)
    _hold_threads[hold_id] = t
    t.start()

def hold_stop(hold_id: str):
    if hold_id in _hold_threads:
        _hold_stop_flags[hold_id] = True
        return True
    return False

def hold_stop_all():
    for hid in list(_hold_threads.keys()):
        _hold_stop_flags[hid] = True
    return True

# ---- FastAPI app & routes ----
app = FastAPI(title="IR Server")

@app.get("/")
def root():
    return {
        "cogs_endpoints": [
            "/cogs/health",
            "/cogs/ir/send?remote=...&key=...[&carrier=38]",
            "/cogs/ir/send_raw?ms=CSV[&carrier=38]",
            "/cogs/ir/hold/start?remote=...&key=...&interval_ms=110&hold_id=...&carrier=38",
            "/cogs/ir/hold/start?ms=CSV&interval_ms=110&hold_id=...&carrier=38",
            "/cogs/ir/hold/stop?hold_id=...",
            "/cogs/ir/hold/stop_all",
            "/cogs/ir/stop",
            "/cogs/ir/learn/start",
            "/cogs/ir/learn/stop/{remote}/{key}",
            "/cogs/ir/list",
            "/cogs/ir/delete/{code_name}",
        ]
    }

@app.get("/cogs/health")
def cogs_health():
    try:
        get_pi()
        ok = True
    except Exception:
        ok = False
    return {"ok": ok, "tx_gpio": IR_TX_GPIO, "rx_gpio": IR_RX_GPIO, "carrier_khz": IR_CARRIER_KHZ}

# ---- Send named ----
@app.get("/cogs/ir/send")
def cogs_send_named(remote: str, key: str, carrier: Optional[float] = None):
    db = _load_codes()
    if remote not in db or key not in db[remote]:
        raise HTTPException(status_code=404, detail=f"Unknown remote/key: {remote}/{key}")
    durations_us = [int(x) for x in db[remote][key]]
    send_raw_burst(durations_us, carrier or IR_CARRIER_KHZ)
    return {"ok": True, "remote": remote, "key": key, "carrier_khz": carrier or IR_CARRIER_KHZ}

# ---- Send raw (ms CSV in URL) ----
@app.get("/cogs/ir/send_raw")
def cogs_send_raw(ms: str, carrier: Optional[float] = None):
    try:
        parts = [p.strip() for p in ms.split(",") if p.strip() != ""]
        ms_vals = [int(float(p)) for p in parts]
        us_vals = [v * 1000 for v in ms_vals]
        send_raw_burst(us_vals, carrier or IR_CARRIER_KHZ)
        return {"ok": True, "count": len(ms_vals), "carrier_khz": carrier or IR_CARRIER_KHZ}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Bad ms burst: {e}")

# ---- Hold / repeat ----
@app.get("/cogs/ir/hold/start")
def cogs_hold_start(hold_id: str, interval_ms: int, remote: Optional[str] = None, key: Optional[str] = None, ms: Optional[str] = None, carrier: Optional[float] = None):
    if not hold_id:
        raise HTTPException(status_code=400, detail="hold_id required")
    if ms:
        parts = [p.strip() for p in ms.split(",") if p.strip() != ""]
        ms_vals = [int(float(p)) for p in parts]
        us_vals = [v * 1000 for v in ms_vals]
        burst = us_vals
    else:
        if not (remote and key):
            raise HTTPException(status_code=400, detail="remote/key or ms required")
        db = _load_codes()
        if remote not in db or key not in db[remote]:
            raise HTTPException(status_code=404, detail=f"Unknown remote/key: {remote}/{key}")
        burst = [int(x) for x in db[remote][key]]

    hold_start_from_burst(hold_id, burst, carrier or IR_CARRIER_KHZ, interval_ms)
    return {"ok": True, "hold_id": hold_id}

@app.get("/cogs/ir/hold/stop")
def cogs_hold_stop(hold_id: str):
    return {"ok": hold_stop(hold_id), "hold_id": hold_id}

@app.get("/cogs/ir/hold/stop_all")
def cogs_hold_stop_all():
    hold_stop_all()
    return {"ok": True}

# Alias for global stop (COGs convenience)
@app.get("/cogs/ir/stop")
def cogs_stop_all():
    hold_stop_all()
    return {"ok": True}

# ---- Learn ----
@app.get("/cogs/ir/learn/start")
def cogs_learn_start():
    learn_start()
    return {"ok": True, "listening": True}

@app.get("/cogs/ir/learn/stop/{remote}/{key}")
def cogs_learn_stop(remote: str, key: str):
    remote = remote.replace("_", " ").strip()
    key = key.replace("_", " ").strip()
    try:
        durations = learn_stop_and_save(remote, key)
        return {"ok": True, "remote": remote, "key": key, "saved_len": len(durations)}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

# ---- Manage learned codes ----
@app.get("/cogs/ir/list")
def cogs_ir_list():
    data = _load_codes()
    names = []
    for r, keys in data.items():
        for k in keys:
            names.append(f"{r}/{k}")
    names.sort()
    return {"ok": True, "count": len(names), "names": names}

@app.get("/cogs/ir/delete/{code_name}")
def cogs_ir_delete(code_name: str):
    name = code_name.replace("_", " ").strip()
    if "/" in name:
        remote, key = name.split("/", 1)
    else:
        # if only one-level name given, try to delete by key across first matching remote
        raise HTTPException(status_code=400, detail="Use Remote/Key format for delete")
    data = _load_codes()
    if remote not in data or key not in data[remote]:
        raise HTTPException(status_code=404, detail=f"Code '{remote}/{key}' not found")
    data[remote].pop(key, None)
    if not data[remote]:
        data.pop(remote, None)
    _save_codes(data)
    return {"ok": True, "deleted": f"{remote}/{key}"}
