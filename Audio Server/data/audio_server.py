#!/usr/bin/env python3
import os
import wave
import time
import threading

import numpy as np
import alsaaudio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
import uvicorn

# ==========================================================
# CONFIGURATION
# ==========================================================

# CHANGE THIS to where your WAV files are stored
AUDIO_FOLDER = os.getenv("AUDIO_FOLDER", "./audio_files/")

TARGET_RATE = 44100
CHUNK = 1024

# ==========================================================
# SHARED STATE
# ==========================================================
bg_buffer: np.ndarray | None = None
bg_pos: int = 0

interrupt_L: np.ndarray | None = None
interrupt_R: np.ndarray | None = None
il_pos: int = 0
ir_pos: int = 0

bg_volume: float = 1.0
interrupt_volume_L: float = 1.0
interrupt_volume_R: float = 1.0
master_volume: float = 1.0
last_nonzero_volume: float = 1.0

running = True
state_lock = threading.Lock()

app = FastAPI()


SERVICE_NAME = "audio"

def api_ok(action: str, message: str = "ok", **extra):
    payload = {"ok": True, "service": SERVICE_NAME, "action": action, "message": message}
    payload.update(extra)
    return payload


@app.exception_handler(FileNotFoundError)
async def file_not_found_handler(request: Request, exc: FileNotFoundError):
    return JSONResponse(status_code=404, content={
        "ok": False,
        "service": SERVICE_NAME,
        "action": "error",
        "message": f"File not found: {exc}",
        "path": request.url.path,
        "error_code": "file_not_found",
    })


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError):
    return JSONResponse(status_code=400, content={
        "ok": False,
        "service": SERVICE_NAME,
        "action": "error",
        "message": str(exc),
        "path": request.url.path,
        "error_code": "value_error",
    })


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={
        "ok": False,
        "service": SERVICE_NAME,
        "action": "error",
        "message": "Invalid request",
        "path": request.url.path,
        "error_code": "validation_error",
    })


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    return JSONResponse(status_code=500, content={
        "ok": False,
        "service": SERVICE_NAME,
        "action": "error",
        "message": str(exc),
        "path": request.url.path,
        "error_code": "internal_error",
    })



# ==========================================================
# RESAMPLING (LINEAR)
# ==========================================================

def resample_linear(data: np.ndarray, orig_rate: int, target_rate: int) -> np.ndarray:
    if orig_rate == target_rate or len(data) == 0:
        return data.astype(np.float32)

    duration = len(data) / float(orig_rate)
    new_len = max(1, int(round(duration * target_rate)))

    old_idx = np.linspace(0, len(data) - 1, num=len(data), dtype=np.float32)
    new_idx = np.linspace(0, len(data) - 1, num=new_len, dtype=np.float32)

    return np.interp(new_idx, old_idx, data).astype(np.float32)


# ==========================================================
# WAV LOADING
# ==========================================================

def load_wav_any(path: str, require_stereo=False, require_mono=False):
    if not os.path.isfile(path):
        raise FileNotFoundError(path)

    with wave.open(path, "rb") as w:
        rate = w.getframerate()
        channels = w.getnchannels()
        width = w.getsampwidth()
        frames = w.getnframes()

        if width != 2:
            raise ValueError("Only 16-bit PCM WAV supported")

        raw = w.readframes(frames)
        data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

    if channels == 2:
        stereo = data.reshape(-1, 2)
    elif channels == 1:
        stereo = data.reshape(-1, 1)
    else:
        raise ValueError("Unsupported channel count")

    # Resample if needed
    if rate != TARGET_RATE:
        if stereo.shape[1] == 2:
            L = resample_linear(stereo[:, 0], rate, TARGET_RATE)
            R = resample_linear(stereo[:, 1], rate, TARGET_RATE)
            stereo = np.column_stack((L, R))
        else:
            mono = resample_linear(stereo[:, 0], rate, TARGET_RATE)
            stereo = mono.reshape(-1, 1)

    if require_stereo:
        if stereo.shape[1] == 1:
            stereo = np.repeat(stereo, 2, axis=1)
        return stereo.astype(np.float32)

    if require_mono:
        if stereo.shape[1] == 2:
            mono = (stereo[:, 0] + stereo[:, 1]) * 0.5
        else:
            mono = stereo[:, 0]
        return mono.astype(np.float32)

    return stereo.astype(np.float32)


# ==========================================================
# AUDIO THREAD (TRUE MIXER)
# ==========================================================

def audio_thread():
    global bg_buffer, bg_pos, il_pos, ir_pos
    global interrupt_L, interrupt_R

    out = alsaaudio.PCM(alsaaudio.PCM_PLAYBACK)
    out.setchannels(2)
    out.setrate(TARGET_RATE)
    out.setformat(alsaaudio.PCM_FORMAT_S16_LE)
    out.setperiodsize(CHUNK)

    while running:
        with state_lock:
            local_bg_buffer = bg_buffer
            local_bg_pos = bg_pos
            local_interrupt_L = interrupt_L
            local_interrupt_R = interrupt_R
            local_il_pos = il_pos
            local_ir_pos = ir_pos
            local_bg_volume = bg_volume
            local_interrupt_volume_L = interrupt_volume_L
            local_interrupt_volume_R = interrupt_volume_R
            local_master_volume = master_volume

        if local_bg_buffer is None:
            time.sleep(0.01)
            continue

        # -------- Background Loop -----------
        end = local_bg_pos + CHUNK
        if end <= len(local_bg_buffer):
            bg_chunk = local_bg_buffer[local_bg_pos:end]
        else:
            first = local_bg_buffer[local_bg_pos:]
            wrap = end % len(local_bg_buffer)
            second = local_bg_buffer[:wrap]
            bg_chunk = np.vstack((first, second))

        new_bg_pos = (local_bg_pos + CHUNK) % len(local_bg_buffer)

        bgL = bg_chunk[:, 0] * local_bg_volume
        bgR = bg_chunk[:, 1] * local_bg_volume

        intL = np.zeros(CHUNK, dtype=np.float32)
        intR = np.zeros(CHUNK, dtype=np.float32)

        new_interrupt_L = local_interrupt_L
        new_interrupt_R = local_interrupt_R
        new_il_pos = local_il_pos
        new_ir_pos = local_ir_pos

        # -------- Interrupt Left -----------
        if local_interrupt_L is not None:
            seg = local_interrupt_L[local_il_pos: local_il_pos + CHUNK]
            finished = len(seg) < CHUNK

            if finished:
                padded = np.zeros(CHUNK, dtype=np.float32)
                padded[:len(seg)] = seg
                seg = padded

            intL = seg * local_interrupt_volume_L
            new_il_pos = local_il_pos + CHUNK

            if finished or new_il_pos >= local_interrupt_L.shape[0]:
                new_interrupt_L = None
                new_il_pos = 0

        # -------- Interrupt Right ----------
        if local_interrupt_R is not None:
            seg = local_interrupt_R[local_ir_pos: local_ir_pos + CHUNK]
            finished = len(seg) < CHUNK

            if finished:
                padded = np.zeros(CHUNK, dtype=np.float32)
                padded[:len(seg)] = seg
                seg = padded

            intR = seg * local_interrupt_volume_R
            new_ir_pos = local_ir_pos + CHUNK

            if finished or new_ir_pos >= local_interrupt_R.shape[0]:
                new_interrupt_R = None
                new_ir_pos = 0

        # -------- TRUE MIXER ---------------
        outL = (bgL + intL) * local_master_volume
        outR = (bgR + intR) * local_master_volume

        stereo = np.column_stack((outL, outR))
        stereo = np.clip(stereo, -1.0, 1.0)

        with state_lock:
            if bg_buffer is local_bg_buffer:
                bg_pos = new_bg_pos

            if interrupt_L is local_interrupt_L:
                interrupt_L = new_interrupt_L
                il_pos = new_il_pos

            if interrupt_R is local_interrupt_R:
                interrupt_R = new_interrupt_R
                ir_pos = new_ir_pos

        out.write((stereo * 32767).astype(np.int16).tobytes())


# ==========================================================
# UTILS
# ==========================================================

def _clamp_vol(v: int) -> float:
    return max(0, min(int(v), 100)) / 100.0


# ==========================================================
# ENDPOINTS
# ==========================================================

@app.get("/audio/set_background/{file}/{vol}")
def set_background(file: str, vol: int):
    global bg_buffer, bg_pos, bg_volume

    path = file if file.startswith("/") else os.path.join(AUDIO_FOLDER, file)
    new_buffer = load_wav_any(path, require_stereo=True)
    new_volume = _clamp_vol(vol)

    with state_lock:
        bg_buffer = new_buffer
        bg_pos = 0
        bg_volume = new_volume

    return api_ok("set_background", "background updated", file=file, volume=int(new_volume * 100))


@app.get("/audio/set_background/{file}")
def set_background_default(file: str):
    return set_background(file, 100)


@app.get("/audio/interrupt_left/{file}/{vol}")
def interrupt_left(file: str, vol: int):
    global interrupt_L, il_pos, interrupt_volume_L

    path = file if file.startswith("/") else os.path.join(AUDIO_FOLDER, file)
    new_interrupt = load_wav_any(path, require_mono=True)
    new_volume = _clamp_vol(vol)

    with state_lock:
        interrupt_L = new_interrupt
        il_pos = 0
        interrupt_volume_L = new_volume

    return api_ok("interrupt_left", "left interrupt queued", file=file, volume=int(new_volume * 100))

@app.get("/audio/interrupt_left/{file}")
def interrupt_left_default(file: str):
    return interrupt_left(file, 100)
    
    
@app.get("/audio/interrupt_right/{file}/{vol}")
def interrupt_right(file: str, vol: int):
    global interrupt_R, ir_pos, interrupt_volume_R

    path = file if file.startswith("/") else os.path.join(AUDIO_FOLDER, file)
    new_interrupt = load_wav_any(path, require_mono=True)
    new_volume = _clamp_vol(vol)

    with state_lock:
        interrupt_R = new_interrupt
        ir_pos = 0
        interrupt_volume_R = new_volume

    return api_ok("interrupt_right", "right interrupt queued", file=file, volume=int(new_volume * 100))


@app.get("/audio/interrupt_right/{file}")
def interrupt_right_default(file: str):
    return interrupt_right(file, 100)


@app.get("/audio/stop")
def stop_audio():
    global bg_buffer, bg_pos
    global interrupt_L, interrupt_R, il_pos, ir_pos

    with state_lock:
        bg_buffer = None
        bg_pos = 0

        interrupt_L = None
        interrupt_R = None
        il_pos = 0
        ir_pos = 0

    return api_ok("stop_audio", "audio stopped")

# -------- GLOBAL MASTER VOLUME --------

@app.get("/audio/set_volume/{vol}")
def set_volume(vol: int):
    global master_volume, last_nonzero_volume
    v = _clamp_vol(vol)

    with state_lock:
        if v > 0:
            last_nonzero_volume = v
        master_volume = v
        current = master_volume

    return api_ok("set_volume", "master volume updated", volume=int(current * 100))


@app.get("/audio/volume_up/{amount}")
def volume_up(amount: int):
    global master_volume, last_nonzero_volume
    step = _clamp_vol(amount)

    with state_lock:
        master_volume = min(1.0, master_volume + step)
        if master_volume > 0:
            last_nonzero_volume = master_volume
        current = master_volume

    return api_ok("volume_up", "master volume updated", volume=int(current * 100))


@app.get("/audio/volume_down/{amount}")
def volume_down(amount: int):
    global master_volume, last_nonzero_volume
    step = _clamp_vol(amount)

    with state_lock:
        master_volume = max(0.0, master_volume - step)
        if master_volume > 0:
            last_nonzero_volume = master_volume
        current = master_volume

    return api_ok("volume_down", "master volume updated", volume=int(current * 100))


@app.get("/audio/mute_toggle")
def mute_toggle():
    global master_volume, last_nonzero_volume

    with state_lock:
        if master_volume > 0:
            last_nonzero_volume = master_volume
            master_volume = 0.0
        else:
            if last_nonzero_volume <= 0:
                last_nonzero_volume = 1.0
            master_volume = last_nonzero_volume
        current = master_volume

    return api_ok("mute_toggle", "master volume updated", volume=int(current * 100))


@app.get("/audio/set_background_volume/{vol}")
def set_background_volume(vol: int):
    global bg_volume
    with state_lock:
        bg_volume = _clamp_vol(vol)
        current = bg_volume
    return api_ok("set_background_volume", "background volume updated", background_volume=int(current * 100))


@app.get("/audio/background_volume_up/{amount}")
def background_volume_up(amount: int):
    global bg_volume
    step = _clamp_vol(amount)
    with state_lock:
        bg_volume = min(1.0, bg_volume + step)
        current = bg_volume
    return api_ok("background_volume_up", "background volume updated", background_volume=int(current * 100))


@app.get("/audio/background_volume_down/{amount}")
def background_volume_down(amount: int):
    global bg_volume
    step = _clamp_vol(amount)
    with state_lock:
        bg_volume = max(0.0, bg_volume - step)
        current = bg_volume
    return api_ok("background_volume_down", "background volume updated", background_volume=int(current * 100))
    

@app.get("/")
def root():
    return api_ok("root", "audio server running")


# ==========================================================
# START THREAD + SERVER
# ==========================================================

threading.Thread(target=audio_thread, daemon=True).start()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
