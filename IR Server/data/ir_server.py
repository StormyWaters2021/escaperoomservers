#!/usr/bin/env python3
"""
IR Server (FastAPI + pigpio) — COGs-friendly GET endpoints.

Health
  GET /cogs/health

Send a named code (from DB)
  GET /cogs/ir/send?remote=...&key=...[&carrier=38][&scale=1.0][&repeat=1][&gap_us=7200]

Send a raw burst (milliseconds CSV in URL; accepts signed or unsigned)
  GET /cogs/ir/send_raw?ms=900,-450,900,-450[&carrier=38][&scale=1.0][&repeat=1][&gap_us=7200]

Press-and-hold (repeat in a loop until stopped)
  GET /cogs/ir/hold/start?remote=...&key=...&interval_ms=110&hold_id=H[&carrier=38][&scale=1.0]
  GET /cogs/ir/hold/start?ms=CSV&interval_ms=110&hold_id=H[&carrier=38][&scale=1.0]
  GET /cogs/ir/hold/stop?hold_id=H
  GET /cogs/ir/hold/stop_all
  GET /cogs/ir/stop              # alias for stop_all

Learn
  GET /cogs/ir/learn/start
  GET /cogs/ir/learn/stop/{remote}/{key}

Manage learned codes
  GET /cogs/ir/list
  GET /cogs/ir/delete/{Remote_Key}    # use "Remote/Key" (slash) or underscore in name and it will be spaced

Environment (systemd Environment=...)
  IR_TX_GPIO       (default 18)   # BCM
  IR_RX_GPIO       (default 23)   # BCM
  IR_CARRIER_KHZ   (default 38.0)
  IR_CODES_DB      (default /opt/ir-server/ir_codes.json)
  IR_DEBUG         (default 0)
"""

import os, json, time, threading
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
import pigpio

# ---- config from env ---------------------------------------------------------
IR_TX_GPIO = int(os.environ.get("IR_TX_GPIO", "18"))
IR_RX_GPIO = int(os.environ.get("IR_RX_GPIO", "23"))
IR_CARRIER_KHZ = float(os.environ.get("IR_CARRIER_KHZ", "38.0"))
IR_CODES_DB = os.environ.get("IR_CODES_DB", "/opt/ir-server/ir_codes.json")
IR_DEBUG = int(os.environ.get("IR_DEBUG", "0"))

# ---- robust pigpio handle/reconnect -----------------------------------------
_PI = None
_PI_LOCK = threading.Lock()

def get_pi():
    """(Re)connect to local pigpiod and return a healthy handle."""
    global _PI
    with _PI_LOCK:
        if _PI is None:
            _PI = pigpio.pi()
        try:
            _PI.get_current_tick()    # cheap ping
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
                try: _PI.stop()
                except Exception: pass
                _PI = None
                time.sleep(delay)
            else:
                raise

# ---- DB helpers --------------------------------------------------------------
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

# ---- burst normalization / scaling ------------------------------------------
def normalize_burst_us(seq: List[int]) -> List[int]:
    """
    Accept unsigned [on,off,on,off,...] OR signed [+on,-off,...].
    Drop zeros, enforce even length, and make even idx positive, odd idx negative.
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

    # enforce ± by index (even = +, odd = -)
    out = []
    for i, v in enumerate(arr):
        v = abs(int(v))
        out.append(v if i % 2 == 0 else -v)

    if len(out) % 2 == 1:
        out = out[:-1]
    if not out:
        raise ValueError("invalid IR burst after normalization")
    return out

def apply_scale_us(durations_us: List[int], scale: float) -> List[int]:
    if scale is None or scale == 1.0:
        return durations_us
    out = []
    for i, v in enumerate(durations_us):
        s = max(1, int(round(abs(int(v)) * float(scale))))
        out.append(s if i % 2 == 0 else -s)
    return out

def extract_first_frame_us(durations_us: List[int], gap_us: int = 7000) -> List[int]:
    """
    Split on an inter-frame gap (OFF >= gap_us). Return only the first frame.
    Default gap 7 ms—adjustable via query 'gap_us'.
    """
    arr = normalize_burst_us(durations_us)
    out = []
    it = iter(arr)
    for on in it:
        off = next(it, None)
        if off is None:
            break
        out += [on, off]
        if abs(off) >= gap_us:
            break
    if len(out) % 2 == 1:
        out = out[:-1]
    return out if out else arr

# ---- pulses/wave building ----------------------------------------------------
def build_pulses_for_burst(tx_gpio: int, durations_us: List[int], carrier_khz: float, duty_pct: float = 33.0):
    """
    Translate [+on,-off,...] µs into pigpio.pulse list, modulating +on with carrier
    at duty cycle (default ≈33%, common for many remotes).
    """
    pi = get_pi()
    pulses = []
    period = max(2, int(round(1_000_000 / (float(carrier_khz) * 1000.0))))
    on_us = max(1, int(round(period * (duty_pct / 100.0))))
    off_us = max(1, period - on_us)

    def mark(us: int):
        remaining = max(1, int(us))
        while remaining > 0:
            t_on = on_us if remaining >= period else min(on_us, remaining)
            t_off = off_us if remaining >= period else max(0, remaining - t_on)
            pulses.append(pigpio.pulse(1 << tx_gpio, 0, t_on))
            if t_off > 0:
                pulses.append(pigpio.pulse(0, 1 << tx_gpio, t_off))
            remaining -= (t_on + t_off)

    def space(us: int):
        pulses.append(pigpio.pulse(0, 1 << tx_gpio, max(1, int(us))))


    it = iter(durations_us)
    for on_dur in it:
        if on_dur <= 0:
            continue
        mark(on_dur)
        off_dur = next(it, None)
        if off_dur is None:
            break
        if off_dur != 0:
            space(abs(int(off_dur)))

    return pulses

def send_raw_burst(durations_us: List[int], carrier_khz: float):
    durations = normalize_burst_us(durations_us)
    pulses = build_pulses_for_burst(IR_TX_GPIO, durations, carrier_khz, duty_pct=33.0)

    MAX_PULSES = 9000
    total = len(pulses)
    if IR_DEBUG:
        print(f"[send] durations={len(durations)} pulses={total}")
        print(f"Durations: {durations[:20]}")
        print(f"First 10 pulses: {[ (p.gpio_on, p.gpio_off, p.delay) for p in pulses[:10] ]}")

    idx = 0
    while idx < total:
        chunk = pulses[idx: idx + MAX_PULSES]
        idx += len(chunk)

        _retry(lambda: (get_pi().wave_tx_stop(), get_pi().wave_clear()))
        _retry(lambda: get_pi().wave_add_generic(chunk))
        wid = _retry(lambda: get_pi().wave_create())
        try:
            _retry(lambda: get_pi().wave_send_once(wid))
            while get_pi().wave_tx_busy():
                time.sleep(0.0015)
        finally:
            try:
                _retry(lambda: get_pi().wave_delete(wid))
            except Exception:
                pass

def send_repeated_frame(durations_us: List[int], carrier_khz: float, repeat: int, gap_us: int):
    """
    Send the first frame 'repeat' times with 'gap_us' silence between frames.
    """
    frame = extract_first_frame_us(durations_us, gap_us=gap_us)
    for i in range(max(1, int(repeat))):
        send_raw_burst(frame, carrier_khz)
        if i != repeat - 1 and gap_us > 0:
            # insert inter-frame silence (space)
            # implement as an ON(1) + long OFF, normalize will fix shape
            send_raw_burst([1, gap_us], carrier_khz)

# ---- learn / capture ---------------------------------------------------------
_learn_lock = threading.Lock()
_learn_active = False
_learn_edges: List[int] = []
_learn_cb = None
_learn_start_tick = 0

def _rx_callback(gpio, level, tick):
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

    if IR_DEBUG:
        print(f"[learn] edges captured: {len(_learn_edges)}")

    # store as unsigned microseconds (send path accepts either)
    raw_burst = _learn_edges

    # Fix leading zero if present
    if raw_burst and raw_burst[0] == 0:
        raw_burst = raw_burst[1:]

    # Normalize the full capture
    normalized = normalize_burst_us(raw_burst)

    # Only keep the first logical frame (split on gap_us)
    first_frame = extract_first_frame_us(normalized, gap_us=7000)

    # Store only the clean frame
    burst = [abs(int(x)) for x in first_frame]

    db = _load_codes()
    db.setdefault(remote, {})[key] = [abs(int(x)) for x in burst]
    _save_codes(db)
    return burst

# ---- hold / repeat threads ---------------------------------------------------
_hold_threads = {}
_hold_stop = {}

def _hold_worker(hold_id: str, burst_us: List[int], carrier: float, interval_ms: int):
    try:
        norm = normalize_burst_us(burst_us)
        while not _hold_stop.get(hold_id, False):
            send_raw_burst(norm, carrier)
            time.sleep(max(0.001, interval_ms / 1000.0))
    finally:
        _hold_stop.pop(hold_id, None)
        _hold_threads.pop(hold_id, None)

def hold_start_from_burst(hold_id: str, burst_us: List[int], carrier: float, interval_ms: int):
    if hold_id in _hold_threads:
        raise RuntimeError(f"hold_id '{hold_id}' already running")
    _hold_stop[hold_id] = False
    t = threading.Thread(target=_hold_worker, args=(hold_id, burst_us, carrier, interval_ms), daemon=True)
    _hold_threads[hold_id] = t
    t.start()

def hold_stop(hold_id: str):
    if hold_id in _hold_threads:
        _hold_stop[hold_id] = True
        return True
    return False

def hold_stop_all():
    for hid in list(_hold_threads.keys()):
        _hold_stop[hid] = True
    return True

# ---- FastAPI app & routes ----------------------------------------------------
app = FastAPI(title="IR Server")

@app.get("/")
def root():
    return {
        "cogs_endpoints": [
            "/cogs/health",
            "/cogs/ir/send?remote=...&key=...[&carrier=38][&scale=1.0][&repeat=1][&gap_us=7200]",
            "/cogs/ir/send_raw?ms=CSV[&carrier=38][&scale=1.0][&repeat=1][&gap_us=7200]",
            "/cogs/ir/hold/start?remote=...&key=...&interval_ms=110&hold_id=...&carrier=38&scale=1.0",
            "/cogs/ir/hold/start?ms=CSV&interval_ms=110&hold_id=...&carrier=38&scale=1.0",
            "/cogs/ir/hold/stop?hold_id=...",
            "/cogs/ir/hold/stop_all",
            "/cogs/ir/stop",
            "/cogs/ir/learn/start",
            "/cogs/ir/learn/stop/{remote}/{key}",
            "/cogs/ir/list",
            "/cogs/ir/delete/{Remote_Key}"
        ]
    }

@app.get("/cogs/health")
def cogs_health():
    ok = True
    try:
        get_pi()
    except Exception:
        ok = False
    return {"ok": ok, "tx_gpio": IR_TX_GPIO, "rx_gpio": IR_RX_GPIO, "carrier_khz": IR_CARRIER_KHZ}

# ---- SEND (named) ------------------------------------------------------------
@app.get("/cogs/ir/send")
def cogs_send_named(
    remote: str,
    key: str,
    carrier: Optional[float] = None,
    scale: Optional[float] = 1.0,
    repeat: int = 1,
    gap_us: int = 7200
):
    db = _load_codes()
    if remote not in db or key not in db[remote]:
        raise HTTPException(status_code=404, detail=f"Unknown remote/key: {remote}/{key}")
    raw = [int(x) for x in db[remote][key]]

    # Trim to a single frame, optionally scale, then repeat/gap as requested
    frame = extract_first_frame_us(raw, gap_us=gap_us)
    frame = apply_scale_us(frame, scale or 1.0)
    if repeat and repeat > 1:
        send_repeated_frame(frame, carrier or IR_CARRIER_KHZ, repeat=repeat, gap_us=gap_us)
    else:
        send_raw_burst(frame, carrier or IR_CARRIER_KHZ)
    return {"ok": True, "remote": remote, "key": key, "repeat": repeat, "gap_us": gap_us, "carrier_khz": (carrier or IR_CARRIER_KHZ), "scale": (scale or 1.0)}

# ---- SEND RAW (ms CSV) -------------------------------------------------------
@app.get("/cogs/ir/send_raw")
def cogs_send_raw(ms: str, carrier: Optional[float] = None, scale: Optional[float] = 1.0, repeat: int = 1, gap_us: int = 7200):
    try:
        parts = [p.strip() for p in ms.split(",") if p.strip() != ""]
        ms_vals = [int(float(p)) for p in parts]
        us_vals = [v * 1000 for v in ms_vals]
        frame = extract_first_frame_us(us_vals, gap_us=gap_us)
        frame = apply_scale_us(frame, scale or 1.0)
        if repeat and repeat > 1:
            send_repeated_frame(frame, carrier or IR_CARRIER_KHZ, repeat=repeat, gap_us=gap_us)
        else:
            send_raw_burst(frame, carrier or IR_CARRIER_KHZ)
        return {"ok": True, "count_ms": len(ms_vals), "repeat": repeat, "gap_us": gap_us, "carrier_khz": (carrier or IR_CARRIER_KHZ), "scale": (scale or 1.0)}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Bad ms burst: {e}")

# ---- HOLD / REPEAT -----------------------------------------------------------
@app.get("/cogs/ir/hold/start")
def cogs_hold_start(
    hold_id: str,
    interval_ms: int,
    remote: Optional[str] = None,
    key: Optional[str] = None,
    ms: Optional[str] = None,
    carrier: Optional[float] = None,
    scale: Optional[float] = 1.0
):
    if not hold_id:
        raise HTTPException(status_code=400, detail="hold_id required")
    if ms:
        parts = [p.strip() for p in ms.split(",") if p.strip() != ""]
        ms_vals = [int(float(p)) for p in parts]
        burst = [v * 1000 for v in ms_vals]
    else:
        if not (remote and key):
            raise HTTPException(status_code=400, detail="remote/key or ms required")
        db = _load_codes()
        if remote not in db or key not in db[remote]:
            raise HTTPException(status_code=404, detail=f"Unknown remote/key: {remote}/{key}")
        burst = [int(x) for x in db[remote][key]]

    frame = extract_first_frame_us(burst)
    frame = apply_scale_us(frame, scale or 1.0)
    hold_start_from_burst(hold_id, frame, carrier or IR_CARRIER_KHZ, interval_ms)
    return {"ok": True, "hold_id": hold_id}

@app.get("/cogs/ir/hold/stop")
def cogs_hold_stop(hold_id: str):
    return {"ok": hold_stop(hold_id), "hold_id": hold_id}

@app.get("/cogs/ir/hold/stop_all")
def cogs_hold_stop_all():
    hold_stop_all()
    return {"ok": True}

@app.get("/cogs/ir/stop")
def cogs_stop_all():
    hold_stop_all()
    return {"ok": True}

# ---- LEARN -------------------------------------------------------------------
@app.get("/cogs/ir/learn/start")
def cogs_learn_start():
    learn_start()
    return {"ok": True, "listening": True}

@app.get("/cogs/ir/learn/stop/{remote}/{key}")
def cogs_learn_stop(remote: str, key: str):
    remote = remote.replace("_", " ").strip()
    key = key.replace("_", " ").strip()
    try:
        burst = learn_stop_and_save(remote, key)
        return {"ok": True, "remote": remote, "key": key, "saved_len": len(burst)}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

# ---- LIST / DELETE -----------------------------------------------------------
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
        raise HTTPException(status_code=400, detail="Use Remote/Key format for delete (with a slash)")
    data = _load_codes()
    if remote not in data or key not in data[remote]:
        raise HTTPException(status_code=404, detail=f"Code '{remote}/{key}' not found")
    data[remote].pop(key, None)
    if not data[remote]:
        data.pop(remote, None)
    _save_codes(data)
    return {"ok": True, "deleted": f"{remote}/{key}"}


