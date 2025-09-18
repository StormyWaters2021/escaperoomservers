#!/usr/bin/env python3
import json, os, threading, time
from typing import List, Dict, Optional
import pigpio
from fastapi import FastAPI, HTTPException, Query, Body

# ------------------ CONFIG (env-overridable) ------------------
TX_GPIO = int(os.environ.get("IR_TX_GPIO", "18"))   # PWM-capable is nice but not required
RX_GPIO = int(os.environ.get("IR_RX_GPIO", "23"))   # demodulated IR receiver input (optional)
DEFAULT_CARRIER_KHZ = float(os.environ.get("IR_CARRIER_KHZ", "38.0"))
CODES_DB_PATH = os.environ.get("IR_CODES_DB", "/opt/ir-server/ir_codes.json")
# --------------------------------------------------------------

app = FastAPI(title="IR Server", version="1.1")

pi = pigpio.pi()
if not pi.connected:
    raise SystemExit("pigpio daemon not running. Try: sudo systemctl start pigpiod")

pi.set_mode(TX_GPIO, pigpio.OUTPUT)
pi.write(TX_GPIO, 0)

# ---------- Wave builder ----------
def send_raw_burst(mark_space_us: List[int], carrier_khz: float = DEFAULT_CARRIER_KHZ, duty_cycle=0.33):
    if not mark_space_us:
        return
    carrier_hz = int(round(carrier_khz * 1000))
    if carrier_hz <= 0:
        raise HTTPException(status_code=400, detail="carrier_khz must be > 0")

    period_us = int(round(1_000_000 / carrier_hz))
    on_us = max(1, int(period_us * duty_cycle))
    off_us = max(1, period_us - on_us)

    pulses = []
    set_high = pigpio.pulse(1 << TX_GPIO, 0, on_us)
    set_low  = pigpio.pulse(0, 1 << TX_GPIO, off_us)

    def add_mark(dur):
        dur = int(dur)
        if dur <= 0: return
        cycles = dur // period_us
        rem = dur % period_us
        for _ in range(int(cycles)):
            pulses.append(set_high); pulses.append(set_low)
        if rem > 0:
            high = min(on_us, rem)
            low = max(0, rem - high)
            if high > 0: pulses.append(pigpio.pulse(1 << TX_GPIO, 0, high))
            if low  > 0: pulses.append(pigpio.pulse(0, 1 << TX_GPIO, low))

    def add_space(dur):
        dur = int(dur)
        if dur > 0:
            pulses.append(pigpio.pulse(0, 1 << TX_GPIO, dur))

    pi.wave_tx_stop(); pi.wave_clear()
    pulses.append(pigpio.pulse(0, 1 << TX_GPIO, 50))  # ensure low

    it = iter(mark_space_us)
    for mark in it:
        add_mark(mark)
        space = next(it, 0)
        add_space(space)

    pi.wave_add_generic(pulses)
    wid = pi.wave_create()
    if wid < 0: raise HTTPException(status_code=500, detail=f"wave_create failed: {wid}")
    pi.wave_send_once(wid)
    while pi.wave_tx_busy(): time.sleep(0.001)
    pi.wave_delete(wid)
    pi.write(TX_GPIO, 0)

# ---------- DB helpers ----------
def load_db() -> Dict:
    if not os.path.exists(CODES_DB_PATH): return {}
    with open(CODES_DB_PATH, "r") as f: return json.load(f)

def save_db(db: Dict):
    os.makedirs(os.path.dirname(CODES_DB_PATH), exist_ok=True)
    with open(CODES_DB_PATH, "w") as f: json.dump(db, f, indent=2)

# ---------- Hold/repeat manager ----------
_hold_lock = threading.Lock()
_hold_threads: Dict[str, threading.Thread] = {}
_hold_flags: Dict[str, threading.Event] = {}

def _hold_sender(key: str, mark_space_us: List[int], interval_ms: int, carrier_khz: float):
    stop_evt = _hold_flags[key]
    send_raw_burst(mark_space_us, carrier_khz)
    next_time = time.time() + (interval_ms / 1000.0)
    while not stop_evt.is_set():
        now = time.time()
        if now >= next_time:
            send_raw_burst(mark_space_us, carrier_khz)
            next_time = now + (interval_ms / 1000.0)
        time.sleep(0.005)

def start_hold(hold_id: str, ms: List[int], interval_ms: int, carrier_khz: float):
    with _hold_lock:
        if hold_id in _hold_threads:
            raise HTTPException(status_code=409, detail=f"Hold already active: {hold_id}")
        evt = threading.Event()
        _hold_flags[hold_id] = evt
        t = threading.Thread(target=_hold_sender, args=(hold_id, ms, interval_ms, carrier_khz), daemon=True)
        _hold_threads[hold_id] = t
        t.start()

def stop_hold(hold_id: str):
    with _hold_lock:
        if hold_id not in _hold_threads:
            raise HTTPException(status_code=404, detail=f"No active hold: {hold_id}")
        _hold_flags[hold_id].set()
        _hold_threads[hold_id].join(timeout=2.0)
        del _hold_flags[hold_id]; del _hold_threads[hold_id]

def stop_all_holds():
    with _hold_lock:
        for k in list(_hold_threads.keys()):
            try: stop_hold(k)
            except Exception: pass

# ---- Timed hold helper ----
def start_hold_for_duration(hold_id: str, ms_list: List[int], interval_ms: int, duration_ms: int, carrier_khz: float):
    if duration_ms <= 0:
        raise HTTPException(status_code=400, detail="duration_ms must be > 0")
    # start the repeating hold
    start_hold(hold_id, ms_list, interval_ms, carrier_khz)
    # schedule an automatic stop in a background thread
    def _auto_stop():
        time.sleep(duration_ms / 1000.0)
        try:
            stop_hold(hold_id)
        except Exception:
            pass
    threading.Thread(target=_auto_stop, daemon=True).start()


# ---------- Receiver (optional learn via JSON endpoints) ----------
class IRRecorder:
    def __init__(self, rx_gpio: int):
        self.rx_gpio = rx_gpio
        self._cb = None
        self._last_tick = None
        self._edges = []
        pi.set_mode(self.rx_gpio, pigpio.INPUT)
        pi.set_pull_up_down(self.rx_gpio, pigpio.PUD_OFF)

    def _cb_func(self, gpio, level, tick):
        if self._last_tick is None:
            self._last_tick = tick; return
        dt = pigpio.tickDiff(self._last_tick, tick)
        self._last_tick = tick
        self._edges.append((level, dt))

    def start(self):
        self._edges.clear(); self._last_tick = None
        self._cb = pi.callback(self.rx_gpio, pigpio.EITHER_EDGE, self._cb_func)

    def stop(self) -> List[int]:
        if self._cb is None: return []
        self._cb.cancel(); self._cb = None
        if not self._edges: return []
        durations = [e[1] for e in self._edges]
        levels = [e[0] for e in self._edges]
        raw = durations[:]
        if levels and levels[0] != 0: raw.insert(0, 0)
        if len(raw) % 2 == 1: raw.append(0)
        return [int(x) for x in raw]

recorder = IRRecorder(RX_GPIO)

# -------------------- /cogs/* URL-ONLY ENDPOINTS --------------------
@app.get("/cogs/health")
def cogs_health():
    return {"status":"ok","tx_gpio":TX_GPIO,"rx_gpio":RX_GPIO}

@app.get("/cogs/ir/send")
def cogs_send_named(remote: str = Query(...), key: str = Query(...), carrier: float = Query(DEFAULT_CARRIER_KHZ)):
    db = load_db()
    if remote not in db or key not in db[remote]:
        raise HTTPException(status_code=404, detail="remote/key not found")
    send_raw_burst(db[remote][key], carrier)
    return {"ok": True, "remote": remote, "key": key, "carrier_khz": carrier}

@app.get("/cogs/ir/send_raw")
def cogs_send_raw(ms: str = Query(..., description="CSV microseconds, e.g. 9000,4500,560,560"), carrier: float = Query(DEFAULT_CARRIER_KHZ)):
    try:
        mark_space = [int(x.strip()) for x in ms.split(",") if x.strip()]
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid ms CSV")
    send_raw_burst(mark_space, carrier)
    return {"ok": True, "len": len(mark_space), "carrier_khz": carrier}

@app.get("/cogs/ir/hold/start")
def cogs_hold_start(
    remote: Optional[str] = None,
    key: Optional[str] = None,
    ms: Optional[str] = None,  # CSV alternative
    interval_ms: int = 110,
    hold_id: Optional[str] = None,
    carrier: float = DEFAULT_CARRIER_KHZ
):
    if ms:
        try:
            mark_space = [int(x.strip()) for x in ms.split(",") if x.strip()]
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid ms CSV")
        hid = hold_id or "raw_hold"
        start_hold(hid, mark_space, interval_ms, carrier)
        return {"ok": True, "hold_id": hid, "carrier_khz": carrier, "interval_ms": interval_ms}

    if not (remote and key):
        raise HTTPException(status_code=400, detail="Provide remote&key OR ms CSV")
    db = load_db()
    if remote not in db or key not in db[remote]:
        raise HTTPException(status_code=404, detail="remote/key not found")
    hid = hold_id or f"{remote}:{key}"
    start_hold(hid, db[remote][key], interval_ms, carrier)
    return {"ok": True, "hold_id": hid, "carrier_khz": carrier, "interval_ms": interval_ms}

@app.get("/cogs/ir/hold/stop")
def cogs_hold_stop(hold_id: str = Query(...)):
    stop_hold(hold_id)
    return {"ok": True, "stopped": hold_id}

@app.get("/cogs/ir/hold/stop_all")
def cogs_hold_stop_all():
    stop_all_holds()
    return {"ok": True}


# ---- COGS: press-and-hold for a duration (GET only) ----
@app.get("/cogs/ir/hold/press")
def cogs_hold_press(
    remote: Optional[str] = None,
    key: Optional[str] = None,
    ms: Optional[str] = None,           # CSV alternative to (remote,key)
    duration_ms: int = Query(...),      # how long to hold, in milliseconds
    interval_ms: int = 110,             # repeat cadence between frames
    hold_id: Optional[str] = None,      # optional custom id for this hold
    carrier: float = DEFAULT_CARRIER_KHZ
):
    # get the mark/space list
    if ms:
        try:
            mark_space = [int(x.strip()) for x in ms.split(",") if x.strip()]
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid ms CSV")
        hid = hold_id or "raw_press"
    else:
        if not (remote and key):
            raise HTTPException(status_code=400, detail="Provide remote&key OR ms CSV")
        db = load_db()
        if remote not in db or key not in db[remote]:
            raise HTTPException(status_code=404, detail="remote/key not found")
        mark_space = db[remote][key]
        hid = hold_id or f"{remote}:{key}:press"

    start_hold_for_duration(hid, mark_space, interval_ms, duration_ms, carrier)
    return {
        "ok": True,
        "hold_id": hid,
        "duration_ms": duration_ms,
        "interval_ms": interval_ms,
        "carrier_khz": carrier
    }



# --- COGS: Learn start (URL-only) ---
@app.get("/cogs/ir/learn/start")
def cogs_ir_learn_start():
    recorder.start()
    return {"ok": True, "msg": "learning started; press a button once"}

# --- COGS: Learn stop + save by name (URL-only) ---
@app.get("/cogs/ir/learn/stop/{remote}/{key}")
def cogs_ir_learn_stop(remote: str, key: str):
    raw = recorder.stop()
    if not raw:
        raise HTTPException(status_code=400, detail="no signal captured")
    db = load_db()
    db.setdefault(remote, {})[key] = raw
    save_db(db)
    return {"ok": True, "remote": remote, "key": key, "len": len(raw)}



# -------------------- Optional JSON endpoints (for setup/learn) --------------------
@app.post("/ir/save_code")
def ir_save_code(remote: str = Body(...), key: str = Body(...), mark_space_us: List[int] = Body(...)):
    db = load_db()
    db.setdefault(remote, {})[key] = mark_space_us
    save_db(db)
    return {"ok": True, "remote": remote, "key": key, "len": len(mark_space_us)}

@app.post("/ir/learn/start")
def ir_learn_start():
    recorder.start(); return {"ok": True}

@app.post("/ir/learn/stop")
def ir_learn_stop():
    raw = recorder.stop()
    if not raw: raise HTTPException(status_code=400, detail="no signal captured")
    return {"ok": True, "mark_space_us": raw}

@app.get("/")
def root():
    return {"service":"ir-server","cogs_endpoints":[
        "/cogs/health",
        "/cogs/ir/send?remote=...&key=...[&carrier=38]",
        "/cogs/ir/send_raw?ms=CSV[&carrier=38]",
        "/cogs/ir/hold/start?remote=...&key=...&interval_ms=110&hold_id=...&carrier=38",
        "/cogs/ir/hold/start?ms=CSV&interval_ms=110&hold_id=...&carrier=38",
        "/cogs/ir/hold/stop?hold_id=...",
        "/cogs/ir/hold/stop_all"
    ]}
