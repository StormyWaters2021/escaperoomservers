#!/usr/bin/env python3
import os
import sys
import json
import time
import socket
import threading
import subprocess
from typing import Optional, Any, Dict

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
import uvicorn

# ----------------------------
# Constants / configuration
# ----------------------------
VIDEO_SOCK = "/tmp/mpv-video.sock"
AUDIO_SOCK = "/tmp/mpv-audio.sock"

APP_HOST = os.environ.get("VIDEO_SERVER_HOST", "0.0.0.0")
APP_PORT = int(os.environ.get("VIDEO_SERVER_PORT", "8000"))

# Optional: pulse sink name for the overlay (strongly recommended)
# e.g. export VIDEO_SERVER_PULSE_SINK=alsa_output.platform-fe007000.hdmi.hdmi-stereo
DEFAULT_OVERLAY_SINK = os.environ.get("VIDEO_SERVER_PULSE_SINK")  # None = use Pulse default

# ----------------------------
# Helpers
# ----------------------------
def _rm(path: str):
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass

# ----------------------------
# Video Controller (persistent)
# ----------------------------
class VideoController:
    def __init__(self, socket_path: str = VIDEO_SOCK, log_path: str = os.path.expanduser("~/mpv.log")):
        self.socket_path = socket_path
        self.log_path = log_path
        self.proc: Optional[subprocess.Popen] = None

    # ---- IPC ----
    def _ipc(self, cmd: list) -> Dict[str, Any]:
        payload = (json.dumps({"command": cmd}) + "\n").encode("utf-8")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(2.5)
            s.connect(self.socket_path)
            s.sendall(payload)
            data = b""
            try:
                while not data.endswith(b"\n"):
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    data += chunk
            except socket.timeout:
                pass
        try:
            return json.loads(data.decode("utf-8"))
        except Exception:
            return {}

    def _get(self, prop: str):
        try:
            return self._ipc(["get_property", prop]).get("data")
        except Exception:
            return None

    def _is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _wait_for_socket(self, timeout_s: float = 5.0) -> bool:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout_s:
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.settimeout(0.25)
                    s.connect(self.socket_path)
                    return True
            except Exception:
                time.sleep(0.05)
        return False

    # ---- start mpv ----
    def _start_mpv(self):
        if self._is_running():
            return
        _rm(self.socket_path)

        base = [
            "mpv",
            f"--input-ipc-server={self.socket_path}",
            "--idle=yes",                   # stay up between plays
            "--force-window=yes",           # show a blank window on boot
            "--keep-open=no",
            "--no-terminal",
            "--title=VIDEO_MPV",
            "--cache=no",
            "--ao=pulse",
            f"--log-file={self.log_path}",
            "--really-quiet",
        ]
        self.proc = subprocess.Popen(base, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not self._wait_for_socket(6.0):
            raise RuntimeError("video mpv failed to start")

    # ---- playback ----
    def _play_file(self, path: str, loop: bool, start: Optional[float] = None):
        abs_path = os.path.abspath(path)
        if not os.path.isfile(abs_path):
            raise RuntimeError(f"Video file not found: {abs_path}")

        # Guard: do not allow audio-only files to replace the video instance
        audio_exts = {".wav", ".mp3", ".ogg", ".flac", ".aac", ".m4a", ".opus"}
        ext = os.path.splitext(abs_path.lower())[1]
        if ext in audio_exts:
            raise RuntimeError(f"Refusing to load audio-only file on VIDEO player: {abs_path}")

        self._start_mpv()
        # reset loop/file state and load
        self._ipc(["set_property", "loop-file", "no"])
        self._ipc(["set_property", "loop-playlist", "no"])
        self._ipc(["loadfile", abs_path, "replace"])
        if loop:
            self._ipc(["set_property", "loop-file", "inf"])
        if start is not None:
            self._ipc(["set_property", "time-pos", float(start)])
        self._ipc(["set_property", "pause", False])

    # ---- public ops ----
    def play(self, path: str, loop: bool = False, start: Optional[float] = None):
        self._play_file(path, loop=loop, start=start)

    def pause(self):
        self._start_mpv()
        self._ipc(["set_property", "pause", True])

    def resume(self):
        self._start_mpv()
        self._ipc(["set_property", "pause", False])

    def stop(self):
        if not self._is_running():
            return
        try:
            self._ipc(["stop"])
        except Exception:
            pass

    def set_volume(self, volume: int):
        self._start_mpv()
        v = max(0, min(100, int(volume)))
        self._ipc(["set_property", "volume", v])

    def status(self) -> Dict[str, Any]:
        if not self._is_running():
            return {"running": False}
        return {
            "running": True,
            "pause": bool(self._get("pause")),
            "volume": self._get("volume"),
            "filename": self._get("filename"),
            "time_pos": self._get("time-pos"),
        }

# ----------------------------
# Audio Controller (one-shot)
# ----------------------------
class AudioController:
    def __init__(self, sock_path: str = AUDIO_SOCK):
        self.sock_path = sock_path
        self.proc: Optional[subprocess.Popen] = None
        self.log_path = "/tmp/mpv-audio.log"
        # allow runtime override via endpoint
        self.pulse_sink: Optional[str] = DEFAULT_OVERLAY_SINK

    def _is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        # Hard stop current overlay, if any
        if not self._is_running():
            _rm(self.sock_path)
            return
        try:
            self.proc.terminate()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=2)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        self.proc = None
        _rm(self.sock_path)

    def _reap(self):
        # Wait for one-shot mpv to exit and clean its socket
        if not self.proc:
            return
        try:
            self.proc.wait(timeout=300)
        except Exception:
            try:
                self.proc.terminate()
            except Exception:
                pass
        finally:
            _rm(self.sock_path)
            self.proc = None

    def start(self, path: str, volume: Optional[int] = None, loop: bool = False):
        abs_path = os.path.abspath(path)
        if not os.path.isfile(abs_path):
            raise RuntimeError(f"Audio file not found: {abs_path}")
        if not os.access(abs_path, os.R_OK):
            raise RuntimeError(f"Audio file not readable: {abs_path}")

        # stop any previous overlay cleanly
        if self._is_running():
            self.stop()

        # fresh socket each play
        _rm(self.sock_path)

        cmd = [
            "mpv", "--no-video",
            f"--input-ipc-server={self.sock_path}",
            "--idle=no",                 # exit when file finishes
            "--keep-open=no",            # fully close
            "--title=AUDIO_MPV",
            "--reset-on-next-file=all",
            "--loop-file=no",
            "--loop-playlist=no",
            "--ao=pulse",
            f"--log-file={self.log_path}",
            "--really-quiet",
        ]

        # Pin to a specific Pulse sink if configured (prevents any default-sink shuffle)
        if self.pulse_sink:
            cmd.append(f"--pulse-sink={self.pulse_sink}")

        # volume up front so it applies from first frame
        if volume is not None:
            v = max(0, min(100, int(volume)))
            cmd.append(f"--volume={v}")

        # apply loop if requested
        if loop:
            cmd.append("--loop-file=inf")

        # finally, the file to play
        cmd.append(abs_path)

        # launch and reap
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        threading.Thread(target=self._reap, daemon=True).start()

    def status(self) -> Dict[str, Any]:
        # We keep a lightweight status; for deeper info, read the log
        return {"running": self._is_running(), "sink": self.pulse_sink}

    def set_sink(self, sink: str):
        # set sink to use for subsequent plays
        self.pulse_sink = sink

# ----------------------------
# FastAPI app & endpoints
# ----------------------------
app = FastAPI()
_controller = VideoController()
_audio = AudioController()

@app.get("/status")
def root_status():
    return {"ok": True, "video": _controller.status(), "audio": _audio.status()}

# --- Video COGs ---
@app.get("/cogs/play")
def cogs_play(path: str, loop: int = 0, start: float = None):
    try:
        _controller.play(path=path, loop=bool(int(loop)), start=start)
        return {"ok": True}
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"failed to play: {e}")

@app.get("/cogs/pause")
def cogs_pause():
    try:
        _controller.pause()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, f"failed to pause: {e}")

@app.get("/cogs/resume")
def cogs_resume():
    try:
        _controller.resume()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, f"failed to resume: {e}")

@app.get("/cogs/stop")
def cogs_stop():
    try:
        _controller.stop()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, f"failed to stop: {e}")

@app.get("/cogs/volume")
def cogs_volume(volume: int):
    try:
        _controller.set_volume(volume)
        return {"ok": True, "volume": volume}
    except Exception as e:
        raise HTTPException(500, f"failed to set volume: {e}")

@app.get("/cogs/status")
def cogs_status():
    return {"ok": True, **_controller.status()}

# --- Audio COGs ---
@app.get("/cogs/audio/play")
def cogs_audio_play(path: str, volume: Optional[int] = None, loop: int = 0):
    try:
        _audio.start(path=path, volume=volume, loop=bool(int(loop)))
        return {"ok": True}
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"failed to start audio: {e}")

@app.get("/cogs/audio/stop")
def cogs_audio_stop():
    try:
        _audio.stop()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, f"failed to stop audio: {e}")

@app.get("/cogs/audio/status")
def cogs_audio_status():
    return {"ok": True, **_audio.status()}

@app.get("/cogs/audio/sink")
def cogs_audio_sink(name: Optional[str] = None):
    """
    Get or set the overlay Pulse sink.
    - GET current: /cogs/audio/sink
    - SET new:     /cogs/audio/sink?name=<pulse_sink_name>
    """
    try:
        if name is not None:
            _audio.set_sink(name)
            return {"ok": True, "sink": name}
        else:
            return {"ok": True, "sink": _audio.pulse_sink}
    except Exception as e:
        raise HTTPException(500, f"failed to update sink: {e}")

# --- Debug (optional) ---
@app.get("/debug/audio/log")
def debug_audio_log(lines: int = 200):
    try:
        with open("/tmp/mpv-audio.log", "rb") as f:
            tail = f.read().splitlines()[-lines:]
        return {"tail": "\n".join(x.decode("utf-8", "replace") for x in tail)}
    except FileNotFoundError:
        raise HTTPException(404, "audio log not found (play once to generate it)")
    except Exception as e:
        raise HTTPException(500, f"failed to read audio log: {e}")

# ----------------------------
# Entrypoint
# ----------------------------
if __name__ == "__main__":
    # optional CLI: --host, --port, --fullscreen (ignored by server, kept for symmetry)
    host = APP_HOST
    port = APP_PORT
    for i, arg in enumerate(sys.argv):
        if arg == "--host" and i + 1 < len(sys.argv):
            host = sys.argv[i + 1]
        if arg == "--port" and i + 1 < len(sys.argv):
            port = int(sys.argv[i + 1])
    uvicorn.run("video_server:app", host=host, port=port, reload=False)
