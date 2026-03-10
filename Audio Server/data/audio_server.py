#!/usr/bin/env python3
import os
import wave
import time
import threading

import numpy as np
import alsaaudio
from fastapi import FastAPI
import uvicorn

# ==========================================================
# CONFIGURATION
# ==========================================================

# CHANGE THIS to where your WAV files are stored
AUDIO_FOLDER = "./audio_files/"

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
interrupt_volume: float = 1.0
master_volume: float = 1.0
last_nonzero_volume: float = 1.0

running = True

app = FastAPI()


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
    global bg_pos, il_pos, ir_pos
    global interrupt_L, interrupt_R

    out = alsaaudio.PCM(alsaaudio.PCM_PLAYBACK)
    out.setchannels(2)
    out.setrate(TARGET_RATE)
    out.setformat(alsaaudio.PCM_FORMAT_S16_LE)
    out.setperiodsize(CHUNK)

    while running:
        if bg_buffer is None:
            time.sleep(0.01)
            continue

        # -------- Background Loop -----------
        end = bg_pos + CHUNK
        if end <= len(bg_buffer):
            bg_chunk = bg_buffer[bg_pos:end]
        else:
            first = bg_buffer[bg_pos:]
            wrap = end % len(bg_buffer)
            second = bg_buffer[:wrap]
            bg_chunk = np.vstack((first, second))

        bg_pos = (bg_pos + CHUNK) % len(bg_buffer)

        bgL = bg_chunk[:, 0] * bg_volume
        bgR = bg_chunk[:, 1] * bg_volume

        intL = np.zeros(CHUNK, dtype=np.float32)
        intR = np.zeros(CHUNK, dtype=np.float32)

        # -------- Interrupt Left -----------
        if interrupt_L is not None:
            seg = interrupt_L[il_pos : il_pos + CHUNK]
            finished = len(seg) < CHUNK

            if finished:
                padded = np.zeros(CHUNK, dtype=np.float32)
                padded[:len(seg)] = seg
                seg = padded

            intL = seg * interrupt_volume
            il_pos += CHUNK

            if finished or il_pos >= interrupt_L.shape[0]:
                interrupt_L = None
                il_pos = 0

        # -------- Interrupt Right ----------
        if interrupt_R is not None:
            seg = interrupt_R[ir_pos : ir_pos + CHUNK]
            finished = len(seg) < CHUNK

            if finished:
                padded = np.zeros(CHUNK, dtype=np.float32)
                padded[:len(seg)] = seg
                seg = padded

            intR = seg * interrupt_volume
            ir_pos += CHUNK

            if finished or ir_pos >= interrupt_R.shape[0]:
                interrupt_R = None
                ir_pos = 0

        # -------- TRUE MIXER ---------------
        outL = (bgL + intL) * master_volume
        outR = (bgR + intR) * master_volume

        stereo = np.column_stack((outL, outR))
        stereo = np.clip(stereo, -1.0, 1.0)

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
    bg_buffer = load_wav_any(path, require_stereo=True)
    bg_pos = 0
    bg_volume = _clamp_vol(vol)

    return "OK"


@app.get("/audio/set_background/{file}")
def set_background_default(file: str):
    return set_background(file, 100)


@app.get("/audio/interrupt_left/{file}/{vol}")
def interrupt_left(file: str, vol: int):
    global interrupt_L, il_pos, interrupt_volume

    path = file if file.startswith("/") else os.path.join(AUDIO_FOLDER, file)
    interrupt_L = load_wav_any(path, require_mono=True)
    il_pos = 0
    interrupt_volume = _clamp_vol(vol)

    return "OK"

@app.get("/audio/interrupt_left/{file}")
def interrupt_left_default(file: str):
    return interrupt_left(file, 100)
    
    
@app.get("/audio/interrupt_right/{file}/{vol}")
def interrupt_right(file: str, vol: int):
    global interrupt_R, ir_pos, interrupt_volume

    path = file if file.startswith("/") else os.path.join(AUDIO_FOLDER, file)
    interrupt_R = load_wav_any(path, require_mono=True)
    ir_pos = 0
    interrupt_volume = _clamp_vol(vol)

    return "OK"


@app.get("/audio/interrupt_right/{file}")
def interrupt_right_default(file: str):
    return interrupt_right(file, 100)


@app.get("/audio/stop")
def stop_audio():
    global bg_buffer, bg_pos
    global interrupt_L, interrupt_R, il_pos, ir_pos

    bg_buffer = None
    bg_pos = 0

    interrupt_L = None
    interrupt_R = None
    il_pos = 0
    ir_pos = 0

    return {"status": "ok", "message": "audio stopped"}
    
    
# -------- GLOBAL MASTER VOLUME --------

@app.get("/audio/set_volume/{vol}")
def set_volume(vol: int):
    global master_volume, last_nonzero_volume
    v = _clamp_vol(vol)

    if v > 0:
        last_nonzero_volume = v

    master_volume = v
    return {"status": "ok", "volume": int(master_volume * 100)}


@app.get("/audio/volume_up/{amount}")
def volume_up(amount: int):
    global master_volume, last_nonzero_volume
    step = _clamp_vol(amount)  # convert 0–100 step to 0.0–1.0

    master_volume = min(1.0, master_volume + step)
    if master_volume > 0:
        last_nonzero_volume = master_volume

    return {"status": "ok", "volume": int(master_volume * 100)}


@app.get("/audio/volume_down/{amount}")
def volume_down(amount: int):
    global master_volume, last_nonzero_volume
    step = _clamp_vol(amount)

    master_volume = max(0.0, master_volume - step)
    if master_volume > 0:
        last_nonzero_volume = master_volume

    return {"status": "ok", "volume": int(master_volume * 100)}


@app.get("/audio/mute_toggle")
def mute_toggle():
    global master_volume, last_nonzero_volume

    if master_volume > 0:
        last_nonzero_volume = master_volume
        master_volume = 0.0
    else:
        if last_nonzero_volume <= 0:
            last_nonzero_volume = 1.0
        master_volume = last_nonzero_volume

    return {"status": "ok", "volume": int(master_volume * 100)}


@app.get("/audio/set_background_volume/{vol}")
def set_background_volume(vol: int):
    """
    Set background loop volume only (0–100).
    Does NOT affect interrupt volume or master volume.
    """
    global bg_volume
    bg_volume = _clamp_vol(vol)
    return {
        "status": "ok",
        "background_volume": int(bg_volume * 100),
    }


@app.get("/audio/background_volume_up/{amount}")
def background_volume_up(amount: int):
    """
    Increase background loop volume by <amount> (0–100 step).
    Example: /audio/background_volume_up/10 -> +10%
    """
    global bg_volume
    step = _clamp_vol(amount)
    bg_volume = min(1.0, bg_volume + step)
    return {
        "status": "ok",
        "background_volume": int(bg_volume * 100),
    }


@app.get("/audio/background_volume_down/{amount}")
def background_volume_down(amount: int):
    """
    Decrease background loop volume by <amount> (0–100 step).
    Example: /audio/background_volume_down/15 -> -15%
    """
    global bg_volume
    step = _clamp_vol(amount)
    bg_volume = max(0.0, bg_volume - step)
    return {
        "status": "ok",
        "background_volume": int(bg_volume * 100),
    }
    

@app.get("/")
def root():
    return "Audio server running"


# ==========================================================
# START THREAD + SERVER
# ==========================================================

threading.Thread(target=audio_thread, daemon=True).start()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
