#!/usr/bin/env python3
import os
import json
import time
from typing import List, Optional, Dict, Any, Tuple

from fastapi import FastAPI, HTTPException, Query
import uvicorn

import pigpio
import threading
import signal
import subprocess
import atexit

# =========================
# Config (edit as needed)
# =========================
TX_GPIO = 22                 # IR LED output pin (via transistor)
RX_GPIO = 23                 # IR receiver (demodulated) input pin (TSOP style)
CARRIER_KHZ_DEFAULT = 38.0   # default carrier
SIGNALS_DIR = "./signals"    # where learned signals are stored
LONG_GAP_US = 7000           # gap threshold to split frames (us)
TOLERANCE_PCT = 0.2          # timing compare tolerance for repeat detection (20%)
ROUND_TO_US = 50             # normalize durations to nearest N us for storage
MAX_CAPTURE_SECONDS = 3.0    # safety cap for learning
MIN_PULSES_TO_ACCEPT = 6     # ignore tiny/noisy captures

os.makedirs(SIGNALS_DIR, exist_ok=True)

app = FastAPI(title="IR Learn/Send Server", version="1.1 (COGS-style URLs)")

# =========================
# pigpio init
# =========================
def ensure_pigpiod():
    try:
        pi = pigpio.pi()
        if not pi.connected:
            raise RuntimeError("pigpio not connected")
        pi.stop()
        return
    except Exception:
        pass
    try:
        subprocess.run(["sudo", "pigpiod"], check=False)
        time.sleep(0.2)
    except Exception:
        pass

ensure_pigpiod()
pi = pigpio.pi()
if not pi.connected:
    raise SystemExit("Unable to connect to pigpio. Ensure pigpiod is running: sudo pigpiod")

pi.set_mode(TX_GPIO, pigpio.OUTPUT)
pi.set_mode(RX_GPIO, pigpio.INPUT)
pi.set_pull_up_down(RX_GPIO, pigpio.PUD_OFF)

# =========================
# Utilities
# =========================
def round_us(v: int, step: int = ROUND_TO_US) -> int:
    return int(round(v / step) * step)

def approx_equal(a: int, b: int, tol_pct: float = TOLERANCE_PCT) -> bool:
    m = max(1, max(a, b))
    return abs(a - b) <= m * tol_pct

def frames_equal(f1: List[int], f2: List[int], tol_pct: float = TOLERANCE_PCT) -> bool:
    if len(f1) != len(f2):
        return False
    for a, b in zip(f1, f2):
        if (a > 0) != (b > 0):
            return False
        if not approx_equal(abs(a), abs(b), tol_pct):
            return False
    return True

def compress_repeats(frames: List[List[int]]) -> Tuple[List[int], int, int]:
    if not frames:
        return [], 0, LONG_GAP_US

    norm = [[(round_us(d) if d > 0 else -round_us(-d)) for d in fr] for fr in frames]
    canonical = norm[0]
    repeats = 1
    for fr in norm[1:]:
        if frames_equal(canonical, fr):
            repeats += 1
        else:
            from collections import Counter
            key = lambda arr: tuple(arr)
            counts = Counter([key(f) for f in norm])
            canonical = list(counts.most_common(1)[0][0])
            repeats = counts.most_common(1)[0][1]
            break

    gap_us = LONG_GAP_US
    if canonical:
        if canonical[-1] < 0:
            gap_us = abs(canonical[-1])
        else:
            gap_us = LONG_GAP_US

    return canonical, repeats, gap_us

def save_signal(name: str, data: Dict[str, Any]) -> None:
    path = os.path.join(SIGNALS_DIR, f"{name}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def load_signal(name: str) -> Dict[str, Any]:
    path = os.path.join(SIGNALS_DIR, f"{name}.json")
    if not os.path.exists(path):
        raise FileNotFoundError(name)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def list_signals() -> List[str]:
    return sorted([fn[:-5] for fn in os.listdir(SIGNALS_DIR) if fn.endswith(".json")])

# =========================
# Capturing (Learn)
# =========================
class CaptureSession:
    def __init__(self, rx_gpio: int):
        self.rx = rx_gpio
        self.cb = None
        self.running = False
        self.lock = threading.Lock()
        self.last_tick = None
        self.level = pi.read(self.rx)
        self.raw_us: List[int] = []

    def _edge(self, gpio, level, tick):
        with self.lock:
            if self.last_tick is None:
                self.last_tick = tick
                self.level = level
                return
            dt = pigpio.tickDiff(self.last_tick, tick)
            self.last_tick = tick
            if self.level == 0:
                self.raw_us.append(+dt)  # mark
            else:
                self.raw_us.append(-dt)  # space
            self.level = level

    def start(self):
        with self.lock:
            self.running = True
            self.last_tick = None
            self.raw_us = []
            self.level = pi.read(self.rx)
            self.cb = pi.callback(self.rx, pigpio.EITHER_EDGE, self._edge)

    def stop(self):
        with self.lock:
            if self.cb is not None:
                self.cb.cancel()
                self.cb = None
            self.running = False

    def get_result(self) -> List[int]:
        with self.lock:
            trimmed = [d for d in self.raw_us if abs(d) >= 80]
            if trimmed and trimmed[0] < 0:
                trimmed = trimmed[1:]
            return trimmed

def split_frames(raw: List[int], gap_us: int = LONG_GAP_US) -> List[List[int]]:
    frames = []
    cur = []
    for d in raw:
        cur.append(d)
        if d < 0 and abs(d) >= gap_us:
            frames.append(cur)
            cur = []
    if cur:
        frames.append(cur)
    return frames

# =========================
# Sending (pigpio wave)
# =========================
def build_wave_from_durations(durations: List[int], tx_gpio: int, carrier_khz: float) -> int:
    pulses = []
    carrier_period_us = int(round(1000.0 / carrier_khz))
    half = max(1, carrier_period_us // 2)

    for dur in durations:
        if dur > 0:
            remaining = dur
            while remaining > 0:
                on_us = min(half, remaining)
                pulses.append(pigpio.pulse(1 << tx_gpio, 0, on_us))
                remaining -= on_us
                if remaining <= 0:
                    break
                off_us = min(half, remaining)
                pulses.append(pigpio.pulse(0, 1 << tx_gpio, off_us))
                remaining -= off_us
        else:
            space = -dur
            if space > 0:
                pulses.append(pigpio.pulse(0, 1 << tx_gpio, space))

    pi.wave_add_generic(pulses)
    wave_id = pi.wave_create()
    if wave_id < 0:
        raise RuntimeError("wave_create failed")
    return wave_id

def send_durations(durations: List[int], repeat: int, gap_us: int, carrier_khz: float):
    if not durations:
        return
    wave_id = build_wave_from_durations(durations, TX_GPIO, carrier_khz)
    try:
        chain = []
        for _ in range(max(1, repeat)):
            chain += [255, 0, wave_id]
            if gap_us > 0:
                pi.wave_add_generic([pigpio.pulse(0, 1 << TX_GPIO, gap_us)])
                gap_id = pi.wave_create()
                chain += [255, 0, gap_id]
                pi.wave_delete(gap_id)

        pi.wave_chain(chain)
        while pi.wave_tx_busy():
            time.sleep(0.002)
    finally:
        pi.wave_delete(wave_id)

# =========================
# Simple helper: parse "ms" CSV into ints
# =========================
def parse_ms_csv(ms_csv: str) -> List[int]:
    out = []
    for token in ms_csv.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            out.append(int(token))
        except Exception:
            raise HTTPException(400, f"Bad ms value: '{token}'")
    if not out:
        raise HTTPException(400, "No ms values provided")
    if out[0] < 0:
        raise HTTPException(400, "ms must start with a positive mark")
    return out

# =========================
# COGS-style GET endpoints (no headers required)
# =========================
@app.get("/cogs/ir/learn")
def learn_get(
    name: Optional[str] = Query(default=None),
    timeout_s: float = Query(default=1.5, ge=0.2, le=MAX_CAPTURE_SECONDS),
    long_gap_us: int = Query(default=LONG_GAP_US, ge=2000),
):
    cap = CaptureSession(RX_GPIO)
    cap.start()
    time.sleep(timeout_s)
    cap.stop()

    raw = cap.get_result()
    if len(raw) < MIN_PULSES_TO_ACCEPT:
        raise HTTPException(400, "No valid IR activity captured. Try again while pressing a remote key.")

    frames = split_frames(raw, gap_us=long_gap_us)
    canonical, repeats, gap_us = compress_repeats(frames)
    canonical = [(round_us(d) if d > 0 else -round_us(-d)) for d in canonical]

    result = {
        "ok": True,
        "frames_detected": len(frames),
        "canonical_durations_us": canonical,
        "repeats": repeats,
        "gap_us": gap_us,
        "total_pulses": len(raw),
    }

    if name:
        payload = {
            "name": name,
            "carrier_khz": CARRIER_KHZ_DEFAULT,
            "canonical_durations_us": canonical,
            "repeats": repeats,
            "gap_us": gap_us,
            "meta": {
                "captured_at": int(time.time()),
                "rx_gpio": RX_GPIO,
                "tx_gpio": TX_GPIO,
                "long_gap_us": long_gap_us,
                "tolerance_pct": TOLERANCE_PCT,
                "round_to_us": ROUND_TO_US,
            },
        }
        save_signal(name, payload)
        result["name"] = name

    return result

@app.get("/cogs/ir/send")
def send_saved_get(
    name: str = Query(...),
    repeat: Optional[int] = Query(default=None, ge=1),
    carrier: Optional[float] = Query(default=None),  # kHz
    scale: Optional[float] = Query(default=1.0),
):
    try:
        data = load_signal(name)
    except FileNotFoundError:
        raise HTTPException(404, f"Signal '{name}' not found")

    durations = data["canonical_durations_us"]
    repeats = data.get("repeats", 1)
    gap_us = data.get("gap_us", LONG_GAP_US)
    carrier_khz = data.get("carrier_khz", CARRIER_KHZ_DEFAULT)

    if repeat is not None:
        repeats = max(1, int(repeat))
    if carrier is not None:
        carrier_khz = float(carrier)
    scale = float(scale or 1.0)

    scaled = [int(round(d * scale)) if d > 0 else -int(round((-d) * scale)) for d in durations]

    try:
        send_durations(scaled, repeat=repeats, gap_us=gap_us, carrier_khz=carrier_khz)
    except Exception as e:
        raise HTTPException(400, f"Failed to send: {e}")

    return {"ok": True, "name": name, "repeat": repeats, "gap_us": gap_us, "carrier_khz": carrier_khz, "scale": scale}

@app.get("/cogs/ir/send_raw")
def send_raw_get(
    ms: str = Query(..., description="Comma-separated signed microsecond durations (e.g. 900,-450,...)"),
    repeat: int = Query(default=1, ge=1),
    gap_us: int = Query(default=LONG_GAP_US, ge=0),
    carrier: float = Query(default=CARRIER_KHZ_DEFAULT, description="Carrier in kHz (e.g. 38)"),
):
    durations = parse_ms_csv(ms)
    try:
        send_durations(durations, repeat=repeat, gap_us=gap_us, carrier_khz=carrier)
    except Exception as e:
        raise HTTPException(400, f"Failed to send: {e}")

    return {"ok": True, "count": len(durations), "repeat": repeat, "gap_us": gap_us, "carrier_khz": carrier}

# Convenience/inspection (also GET)
@app.get("/cogs/ir/status")
def status_get():
    return {"ok": True, "tx_gpio": TX_GPIO, "rx_gpio": RX_GPIO, "signals": list_signals()}

@app.get("/cogs/ir/signals")
def signals_get():
    return {"ok": True, "signals": list_signals()}

@app.get("/cogs/ir/signal")
def signal_get(name: str = Query(...)):
    try:
        return load_signal(name)
    except FileNotFoundError:
        raise HTTPException(404, f"Signal '{name}' not found")

@app.get("/cogs/ir/delete")
def delete_get(name: str = Query(...)):
    path = os.path.join(SIGNALS_DIR, f"{name}.json")
    if not os.path.exists(path):
        raise HTTPException(404, f"Signal '{name}' not found")
    os.remove(path)
    return {"ok": True, "deleted": name}

# =========================
# Graceful shutdown
# =========================
def _cleanup():
    try:
        if pi is not None:
            pi.write(TX_GPIO, 0)
            pi.stop()
    except Exception:
        pass

atexit.register(_cleanup)

def handle_sigterm(signum, frame):
    _cleanup()
    raise SystemExit(0)

signal.signal(signal.SIGTERM, handle_sigterm)
signal.signal(signal.SIGINT, handle_sigterm)

# =========================
# Main
# =========================
if __name__ == "__main__":
    uvicorn.run("ir_server:app", host="0.0.0.0", port=8001, reload=False)
