#!/usr/bin/env python3
import argparse, json, os, socket, subprocess, threading, time
from typing import Optional, Tuple
try:
    from typing import Literal
except Exception:
    # for Python 3.8/3.9 if needed: pip install typing_extensions
    from typing_extensions import Literal
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from fastapi import Request
import socket, subprocess, os, time, json, threading

def _first(v):
    # flatten values that may come from parse_qs
    if isinstance(v, (list, tuple)):
        return v[0] if v else None
    return v

async def _parse_noheader_body(request: Request) -> dict:
    """
    Accepts POST bodies without Content-Type.
    Supports:
      - key=value&key2=value2
      - raw JSON (if COGS ever sends it)
      - empty body (falls back to query params)
    """
    try:
        raw = await request.body()
        if not raw:
            return dict(request.query_params)
        s = raw.decode("utf-8", errors="ignore").strip()
        # Try JSON
        if s and s[0] in "{[":
            import json
            try:
                d = json.loads(s)
                return d if isinstance(d, dict) else {}
            except Exception:
                pass
        # Try querystring form: a=b&c=d
        from urllib.parse import parse_qs
        d = {k: _first(v) for k, v in parse_qs(s, keep_blank_values=True).items()}
        return d or dict(request.query_params)
    except Exception:
        return dict(request.query_params)


# --- Added for better diagnostics ---
import traceback, logging, pathlib

TOAST_WRAP_CHARS = 15   # ← change this to whatever wrap length you want

# ======== mpv JSON IPC helper ========

class MPVIPC:
    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self._sock = None
        self._lock = threading.Lock()
        self._event_listeners = []
        self._running = False

    def connect(self, timeout=10.0):
        start = time.monotonic()
        while True:
            try:
                self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self._sock.connect(self.socket_path)
                self._sock.settimeout(0.2)
                break
            except (FileNotFoundError, ConnectionRefusedError, OSError):
                if time.monotonic() - start > timeout:
                    raise RuntimeError(f"Could not connect to mpv IPC at {self.socket_path}")
                time.sleep(0.05)
        self._running = True
        threading.Thread(target=self._reader_loop, daemon=True).start()

    def close(self):
        self._running = False
        try:
            if self._sock:
                self._sock.close()
        except Exception:
            pass

    def _reader_loop(self):
        buf = b""
        while self._running:
            try:
                chunk = self._sock.recv(4096)
                if not chunk:
                    time.sleep(0.05)
                    continue
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line.decode("utf-8", errors="ignore"))
                        self._dispatch_event(msg)
                    except json.JSONDecodeError:
                        pass
            except socket.timeout:
                continue
            except OSError:
                break

    def _dispatch_event(self, msg):
        if "event" in msg:
            for cb in list(self._event_listeners):
                try:
                    cb(msg)
                except Exception:
                    pass

    def on_event(self, callback):
        self._event_listeners.append(callback)

    def send(self, command: list, request_id: Optional[int] = None) -> dict:
        payload = {"command": command}
        if request_id is not None:
            payload["request_id"] = request_id
        data = (json.dumps(payload) + "\n").encode("utf-8")
        with self._lock:
            self._sock.sendall(data)
        return {}

    def request(self, command: list) -> dict:
        req_id = int(time.time()*1000000) & 0x7fffffff
        payload = {"command": command, "request_id": req_id}
        data = (json.dumps(payload) + "\n").encode("utf-8")
        with self._lock:
            self._sock.sendall(data)
        deadline = time.monotonic() + 2.0
        partial = b""
        while time.monotonic() < deadline:
            try:
                data = self._sock.recv(4096)
                if not data:
                    time.sleep(0.01)
                    continue
                partial += data
                for raw in partial.split(b"\n"):
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        msg = json.loads(raw.decode("utf-8", errors="ignore"))
                        if msg.get("request_id") == req_id:
                            return msg
                        if "event" in msg:
                            self._dispatch_event(msg)
                    except json.JSONDecodeError:
                        continue
                partial = b""
            except socket.timeout:
                continue
            except OSError:
                break
        return {"error": "timeout"}

    def set_property(self, name, value):
        return self.send(["set_property", name, value])

    def get_property(self, name):
        rsp = self.request(["get_property", name])
        if rsp.get("error") == "success":
            return rsp.get("data")
        return None

    def command(self, *args):
        return self.send(list(args))


# ======== Video Controller ========

class VideoController:
    def __init__(self, main_path: Optional[str], fullscreen: bool):
        self.socket_path = "/tmp/mpv-video.sock"
        self.main_path = os.path.abspath(main_path) if main_path else None
        self._proc = None
        self.mpv = None
        self.interrupt_active = False
        self.interrupt_mode = None  # "return" or "skip"
        self.saved_pos = 0.0
        self._osd_prev_level = None
        self.interrupt_start = 0.0
        self._loop_for_next_file = True  # applied on 'file-loaded'
        self._pending_interrupt_path = None  # set when /interrupt/* starts; armed on file-loaded
        self._osd_timer_thread = None
        self._osd_timer_stop = threading.Event()
        self._osd_timer_end = 0.0
        self._osd_timer_rotation = 180   # 0/90/180/270
        self._osd_timer_anchor = 9       # 7/8/9 top, 4/5/6 middle, 1/2/3 bottom
        self._osd_timer_font = 72
        self._start_mpv(fullscreen=fullscreen)
        self._msg_ovl_id = 701         # separate from the timer’s ID (700)
        self._msg_remove_timer = None   # threading.Timer handle
        #self.timer = CountdownTimer(self.mpv)
        # Overlay restore state
        self._overlay_restore_timer = None
        self._orig_audio_id = None

    def _get_selected_audio_track_id(self) -> int | None:
        """Return the mpv track id of the currently selected audio track, or None."""
        try:
            tracks = self.mpv.get_property("track-list") or []
            for t in tracks:
                if t.get("type") == "audio" and t.get("selected"):
                    return t.get("id")
        except Exception:
            pass
        return None

    def _schedule_restore_original_audio(self, delay_s: float):
        """After delay_s, switch audio back to the original track id (if known)."""
        import threading, time
        # cancel any existing timer
        try:
            if self._overlay_restore_timer and self._overlay_restore_timer.is_alive():
                # best effort: no cancel on Timer, so we just let it finish; we’ll replace the ref
                pass
        except Exception:
            pass
        def _restore():
            try:
                time.sleep(max(0.0, float(delay_s)))
                # If we remembered a specific id, restore it. Otherwise fall back to 'auto'
                if self._orig_audio_id is not None:
                    self.mpv.set_property("audio", self._orig_audio_id)
                else:
                    self.mpv.set_property("audio", "auto")
                # Optional: clear external audio list to keep things tidy
                try:
                    self.mpv.set_property("audio-files", [])
                except Exception:
                    pass
            except Exception:
                pass
        self._overlay_restore_timer = threading.Thread(target=_restore, daemon=True)
        self._overlay_restore_timer.start()


    def _osd_map_xy_for_rotation(self, x_px: int, y_px: int) -> Tuple[int, int]:
        """Map desired on-screen (viewer) coords to mpv's pre-rotation OSD coords."""
        W, H = self._osd_get_size()
        try:
            rot = int(self.mpv.get_property("video-rotate") or 0) % 360
        except Exception:
            rot = 0

        if rot == 0:
            return x_px, y_px
        elif rot == 90:
            # viewer (x→right, y→down) ⇢ mpv pre-rot: x' = W - y, y' = x
            return max(0, min(W, W - y_px)), max(0, min(H, x_px))
        elif rot == 180:
            # x' = W - x, y' = H - y
            return max(0, min(W, W - x_px)), max(0, min(H, H - y_px))
        elif rot == 270:
            # x' = y, y' = H - x
            return max(0, min(W, y_px)), max(0, min(H, H - x_px))
        else:
            return x_px, y_px
    
    
    def _ovl_update(self, ovl_id: int, ass_text: str, *, res_x: int = 1000, res_y: int = 1000, z: int = 0):
        """Draw/replace a persistent ASS overlay (independent of the current video)."""
        try:
            # osd-overlay <id> ass-events <data> [res-x] [res-y] [z]
            self.mpv.command("osd-overlay", str(ovl_id), "ass-events",
                             ass_text, str(res_x), str(res_y), str(z))
        except Exception:
            pass

    def _ovl_remove(self, ovl_id: int):
        try:
            # osd-overlay <id> none   => remove
            self.mpv.command("osd-overlay", str(ovl_id), "none")
        except Exception:
            pass


    def _start_mpv(self, fullscreen: bool):
        if os.path.exists(self.socket_path):
            try:
                os.remove(self.socket_path)
            except Exception:
                pass

        log_path = str(pathlib.Path.home() / "mpv.log")
        log_file = open(log_path, "w")
        base = [
            "mpv",
            f"--input-ipc-server={self.socket_path}",
            "--idle=yes",
            "--force-window=yes",
            "--keep-open=no",
            "--cache=no",
            "--msg-level=all=v",
            "--reset-on-next-file=all",
            "--osc=no",
            "--no-terminal",
            "--loop-file=no",
            "--loop-playlist=no",
            "--hwdec=drm",
            "--vo=gpu",
            "--video-rotate=0",
            "--gpu-context=drm",
            "--interpolation=no",
            "--video-sync=audio",
            "--scale=bilinear",
            "--dscale=bilinear",
            "--tscale=linear",
            "--msg-level=ipc=v", 
            "--osd-level=0",
            "--osd-status-msg=",
            "--really-quiet",
            "--log-file=" + log_path,
            "--ao=alsa",
            # "--audio-device=alsa/plughw:CARD=wm8960soundcard,DEV=0",
            "--audio-device=alsa/hdmi:CARD=vc4hdmi0,DEV=0", # One HDMI port
            # "--audio-device=alsa/hdmi:CARD=vc4hdmi1,DEV=0", # Other HDMI port
            
        ]
        if fullscreen:
            base.append("--fullscreen")

        candidates = [
            base,                # DRM + gpu (headless)
            base + ["--vo=drm"], # simple DRM fallback
        ]
        if os.environ.get("DISPLAY"):
            candidates.append(base + ["--vo=gpu"])
        if os.environ.get("WAYLAND_DISPLAY"):
            candidates.append(base + ["--vo=gpu"])

        last_err = None
        for args in candidates:
            try:
                self._proc = subprocess.Popen(args, stdout=log_file, stderr=log_file)
                self.mpv = MPVIPC(self.socket_path)
                self.mpv.connect(timeout=20.0)
                break
            except Exception as e:
                last_err = e
                try:
                    if self._proc and self._proc.poll() is None:
                        self._proc.terminate()
                        self._proc.wait(timeout=3)
                except Exception:
                    pass
                self._proc = None
        else:
            raise RuntimeError(f"Failed to start mpv with any video output. See log: {log_path}") from last_err

        self.mpv.on_event(self._on_mpv_event)
        if self.main_path:
            self.play_main(self.main_path)
    
    
    # ==== OSD TIMER ================================================

    def _ensure_osd_timer_state(self):
        import threading, time  # safe if already imported
        if not hasattr(self, "_osd_timer_thread"): self._osd_timer_thread = None
        if not hasattr(self, "_osd_timer_stop"):   self._osd_timer_stop = threading.Event()
        if not hasattr(self, "_osd_timer_end"):    self._osd_timer_end = 0.0
        if not hasattr(self, "_osd_timer_rotation"): self._osd_timer_rotation = 180
        if not hasattr(self, "_osd_timer_anchor"):   self._osd_timer_anchor = 9
        if not hasattr(self, "_osd_timer_font"):     self._osd_timer_font = 72
        if not hasattr(self, "_osd_prev_level"):     self._osd_prev_level = None
        # NEW fields must always exist:
        if not hasattr(self, "_osd_timer_font_name"): self._osd_timer_font_name = "DejaVu Sans"
        if not hasattr(self, "_osd_timer_pos_mode"):  self._osd_timer_pos_mode = "anchor"  # "anchor" | "percent" | "absolute"
        if not hasattr(self, "_osd_timer_x"):         self._osd_timer_x = None
        if not hasattr(self, "_osd_timer_y"):         self._osd_timer_y = None
        if not hasattr(self, "_osd_timer_margin_x"):  self._osd_timer_margin_x = 40
        if not hasattr(self, "_osd_timer_margin_y"):  self._osd_timer_margin_y = 40
        if not hasattr(self, "_osd_timer_paused"):      self._osd_timer_paused = threading.Event()
        if not hasattr(self, "_osd_timer_pause_left"):  self._osd_timer_pause_left = 0


    def _osd_apply_anchor(self, anchor: int, margin_x: int = 40, margin_y: int = 40):
        row = {7:"top",8:"top",9:"top",4:"center",5:"center",6:"center",1:"bottom",2:"bottom",3:"bottom"}.get(anchor, "top")
        col = {7:"left",8:"center",9:"right",4:"left",5:"center",6:"right",1:"left",2:"center",3:"right"}.get(anchor, "center")
        try:
            self.mpv.set_property("osd-align-x", col)
            self.mpv.set_property("osd-align-y", row)
            self.mpv.set_property("osd-margin-x", margin_x)
            self.mpv.set_property("osd-margin-y", margin_y)
        except Exception:
            pass

    def _osd_timer_format(self, secs_left: int) -> str:
        h = secs_left // 3600
        m = (secs_left % 3600) // 60
        s = secs_left % 60
        return (f"{h:01d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}")

    def _osd_get_size(self):
        try:
            w = int(self.mpv.get_property("osd-width") or 1920)
            h = int(self.mpv.get_property("osd-height") or 1080)
        except Exception:
            w, h = 1920, 1080
        return w, h

    def _osd_timer_ass(self, secs_left: int) -> str:
        # text
        timer = self._osd_timer_format(secs_left)

        # style
        font_name = getattr(self, "_osd_timer_font_name", "DejaVu Sans").replace("{","").replace("}","")
        font_tag = ("\\fn" + font_name) if font_name else ""
        size_tag = "\\fs" + str(int(getattr(self, "_osd_timer_font", 72)))
        style_common = "\\bord4\\1c&HFFFFFF&\\3c&H000000&\\fad(0,0)"

        # placement on a fixed virtual canvas
        CANVAS_W, CANVAS_H = 1000, 1000
        anchor = int(getattr(self, "_osd_timer_anchor", 9))
        mode   = str(getattr(self, "_osd_timer_pos_mode", "anchor")).strip().lower()

        # compute x,y in pixels on the fixed canvas
        if mode == "percent":
            xp = 50.0 if (getattr(self, "_osd_timer_x", None) is None) else float(self._osd_timer_x)
            yp = 50.0 if (getattr(self, "_osd_timer_y", None) is None) else float(self._osd_timer_y)
            x_px = int(round(CANVAS_W * max(0,min(100,xp)) / 100.0))
            y_px = int(round(CANVAS_H * max(0,min(100,yp)) / 100.0))
            pos_tag = f"\\pos({x_px},{y_px})"
        elif mode == "absolute":
            x_px = int(round(0.0 if (getattr(self, "_osd_timer_x", None) is None) else float(self._osd_timer_x)))
            y_px = int(round(0.0 if (getattr(self, "_osd_timer_y", None) is None) else float(self._osd_timer_y)))
            pos_tag = f"\\pos({x_px},{y_px})"
        else:
            # anchor mode: put it near edges via margins on the fixed canvas
            mx = int(getattr(self, "_osd_timer_margin_x", 40))
            my = int(getattr(self, "_osd_timer_margin_y", 40))
            # top row (7,8,9)
            if anchor == 7: x_px,y_px = mx, my
            elif anchor == 8: x_px,y_px = CANVAS_W//2 + mx, my
            elif anchor == 9: x_px,y_px = CANVAS_W - mx, my
            # middle row (4,5,6)
            elif anchor == 4: x_px,y_px = mx, CANVAS_H//2 + my
            elif anchor == 5: x_px,y_px = CANVAS_W//2 + mx, CANVAS_H//2 + my
            elif anchor == 6: x_px,y_px = CANVAS_W - mx, CANVAS_H//2 + my
            # bottom row (1,2,3)
            elif anchor == 1: x_px,y_px = mx, CANVAS_H - my
            elif anchor == 2: x_px,y_px = CANVAS_W//2 + mx, CANVAS_H - my
            else:              x_px,y_px = CANVAS_W - mx, CANVAS_H - my
            pos_tag = f"\\pos({x_px},{y_px})"

        rot = int(getattr(self, "_osd_timer_rotation", 0))  # spin text only
        return "{" + f"\\an{anchor}{font_tag}{size_tag}{style_common}{pos_tag}\\frz{rot}" + "}" + timer


    def _osd_timer_loop(self):
        import time
        OVL_ID = 700
        CANVAS_W = CANVAS_H = 1000
        last_ass = None
        TICK_S = 1.0

        while not self._osd_timer_stop.is_set():
            # if you added pause support:
            if getattr(self, "_osd_timer_paused", None) and self._osd_timer_paused.is_set():
                left = int(max(0, self._osd_timer_pause_left))
            else:
                left = int(max(0, round(self._osd_timer_end - time.monotonic())))

            ass = self._osd_timer_ass(left)
            if ass != last_ass:
                self._ovl_update(OVL_ID, ass, res_x=CANVAS_W, res_y=CANVAS_H, z=0)
                last_ass = ass

            if (not getattr(self, "_osd_timer_paused", None) or not self._osd_timer_paused.is_set()) and left <= 0:
                break
            self._osd_timer_stop.wait(TICK_S)

        # IMPORTANT: blank the overlay so the last value doesn’t stick
        self._ovl_update(OVL_ID, "", res_x=CANVAS_W, res_y=CANVAS_H, z=0)

       

    def start_osd_timer(self, seconds: int, rotation: int = 180, anchor: int = 9, font_size: int = 72,
                        font: Optional[str] = None, position_mode: str = "anchor",
                        x: Optional[float] = None, y: Optional[float] = None,
                        margin_x: int = 40, margin_y: int = 40):
        import threading, time

        self._ensure_osd_timer_state()

        # --- HARD RESET of any previous run ---
        try:
            self._osd_timer_stop.set()
            t = getattr(self, "_osd_timer_thread", None)
            if t and t.is_alive():
                t.join(timeout=0.5)
        except Exception:
            pass
        self._osd_timer_thread = None

        # clear pause state (in case a previous pause was left set)
        try:
            self._osd_timer_paused.clear()
            self._osd_timer_pause_left = 0
        except Exception:
            pass

        # blank the old overlay frame so no stale time remains on screen
        try:
            self._ovl_update(700, "", res_x=1000, res_y=1000, z=0)
        except Exception:
            pass

        # Make sure OSD is visible during the timer
        try:
            self._osd_prev_level = int(self.mpv.get_property("osd-level"))
        except Exception:
            self._osd_prev_level = None
        try:
            self.mpv.set_property("osd-level", 1)
        except Exception:
            pass

        # Store style/placement inputs
        self._osd_timer_rotation = int(rotation)
        self._osd_timer_anchor   = int(anchor)
        self._osd_timer_font     = int(font_size)
        if font is not None:
            self._osd_timer_font_name = str(font)

        mode = (position_mode or "anchor").strip().lower()
        self._osd_timer_pos_mode = mode
        self._osd_timer_x = x
        self._osd_timer_y = y
        self._osd_timer_margin_x = int(margin_x)
        self._osd_timer_margin_y = int(margin_y)

        # Normalize mpv OSD alignment if needed
        if mode == "anchor":
            self._osd_apply_anchor(self._osd_timer_anchor, self._osd_timer_margin_x, self._osd_timer_margin_y)
        else:
            try:
                self.mpv.set_property("osd-align-x", "left")
                self.mpv.set_property("osd-align-y", "top")
                self.mpv.set_property("osd-margin-x", 0)
                self.mpv.set_property("osd-margin-y", 0)
            except Exception:
                pass

        # Arm fresh deadline and launch the loop
        self._osd_timer_stop.clear()
        self._osd_timer_end = time.monotonic() + max(0, int(seconds))
        self._osd_timer_thread = threading.Thread(target=self._osd_timer_loop, name="osd-timer", daemon=True)
        self._osd_timer_thread.start()


    def pause_osd_timer(self):
        """Freeze the countdown in place; keeps the overlay on screen."""
        import time
        self._ensure_osd_timer_state()
        if self._osd_timer_thread and self._osd_timer_thread.is_alive():
            if not self._osd_timer_paused.is_set():
                left = int(max(0, round(self._osd_timer_end - time.monotonic())))
                self._osd_timer_pause_left = left
                self._osd_timer_paused.set()

    def resume_osd_timer(self):
        """Resume countdown from where it was paused."""
        import time
        self._ensure_osd_timer_state()
        if self._osd_timer_paused.is_set():
            self._osd_timer_end = time.monotonic() + int(max(0, self._osd_timer_pause_left))
            self._osd_timer_paused.clear()

    def stop_osd_timer(self):
        self._ensure_osd_timer_state()
        self._osd_timer_stop.set()
        t = getattr(self, "_osd_timer_thread", None)
        if t and t.is_alive():
            try:
                t.join(timeout=0.5)
            except Exception:
                pass
        self._osd_timer_thread = None

        # blank now so a follow-up Start is always fresh
        try:
            self._ovl_update(700, "", res_x=1000, res_y=1000, z=0)
        except Exception:
            pass

        # reset pause state (if present)
        try:
            self._osd_timer_paused.clear()
            self._osd_timer_pause_left = 0
        except Exception:
            pass

        # restore osd-level if we raised it
        if getattr(self, "_osd_prev_level", None) is not None:
            try:
                self.mpv.set_property("osd-level", int(self._osd_prev_level))
            except Exception:
                pass
            self._osd_prev_level = None


    # ==== /OSD TIMER =========================================================

    def _on_mpv_event(self, msg: dict):
        if msg.get("event") == "file-loaded":
            try:
                # Apply the intended loop mode for the file that just loaded
                self.mpv.set_property("loop-file", "inf" if self._loop_for_next_file else "no")

                # If we were starting an interrupt, arm it *now* (after it really loaded)
                try:
                    cur_path = self.mpv.get_property("path") or self.mpv.get_property("filename")
                except Exception:
                    cur_path = None
                if self._pending_interrupt_path:
                    want = self._pending_interrupt_path
                    if cur_path and (str(cur_path) == want or os.path.basename(str(cur_path)) == os.path.basename(want)):
                        self.interrupt_active = True
                        self.interrupt_start = time.monotonic()
                        self._pending_interrupt_path = None

                # Keep the timer visible and ensure playback continues
                # self._reattach_timer_sub()
                self.mpv.set_property("pause", False)
            except Exception:
                pass

        if msg.get("event") == "end-file" and self.interrupt_active:
            dt = 0.0
            if self.interrupt_mode == "skip":
                dt = time.monotonic() - self.interrupt_start
            resume_at = max(0.0, self.saved_pos + dt)
            if self.main_path:
                self._loop_for_next_file = True   # main should loop
                self._play_file(self.main_path, loop=True, start=resume_at)
            self.interrupt_active = False
            self.interrupt_mode = None


    def _play_file(self, path: str, loop: bool, start: Optional[float] = None):
        abs_path = os.path.abspath(path)

        # Allow http(s), otherwise require local file
        if not (abs_path.startswith("http://") or abs_path.startswith("https://")):
            if not os.path.exists(abs_path):
                raise RuntimeError(f"File not found: {abs_path}")

        target_base = os.path.basename(abs_path)

        # Issue loadfile with no per-file options (max compatibility)
        # NOTE: do not append "start=..." here (breaks on some mpv builds)
        self.mpv.command("loadfile", abs_path, "replace")

        # Apply per-file behavior AFTER load (so it can't be reset by the load)
        try:
            self.mpv.set_property("loop-file", "inf" if loop else "no")
        except Exception:
            pass

        # If a start offset is requested, do a precise seek
        if start is not None:
            try:
                # absolute+exact: go to timestamp immediately
                self.mpv.command("seek", f"{float(start):.3f}", "absolute+exact")
            except Exception:
                pass

        # Make sure we’re playing
        try:
            self.mpv.set_property("pause", False)
        except Exception:
            pass

        # Verify the switch quickly (no sleeping for ages)
        def switched_enough() -> Tuple[bool, dict]:
            state = {"path": None, "filename": None, "playlist_name": None, "time_pos": None}
            try:
                cur_path = self.mpv.get_property("path")
                cur_fname = self.mpv.get_property("filename")
                playlist  = self.mpv.get_property("playlist")
                tpos      = self.mpv.get_property("time-pos")
            except Exception:
                cur_path = cur_fname = playlist = tpos = None

            state["path"] = cur_path
            state["filename"] = cur_fname
            state["time_pos"] = tpos

            if cur_path and (str(cur_path) == abs_path or os.path.basename(str(cur_path)) == target_base):
                return True, state
            if cur_fname and os.path.basename(str(cur_fname)) == target_base:
                return True, state
            if isinstance(playlist, list):
                cur_entries = [e for e in playlist if isinstance(e, dict) and e.get("current")]
                if cur_entries:
                    name = cur_entries[0].get("filename") or cur_entries[0].get("title") or ""
                    state["playlist_name"] = name
                    if os.path.basename(str(name)) == target_base or str(name) == abs_path:
                        return True, state
            return False, state

        last_state = {}
        for _ in range(20):  # ~2s total
            ok, state = switched_enough()
            last_state = state
            if ok:
                return
            time.sleep(0.1)

        ctx = {
            "wanted": abs_path,
            "seen_path": last_state.get("path"),
            "seen_filename": last_state.get("filename"),
            "seen_playlist_name": last_state.get("playlist_name"),
            "seen_time_pos": last_state.get("time_pos"),
        }
        raise RuntimeError(f"mpv did not switch to: {abs_path} ; observed={ctx}")



    def play_main(self, path: str):
        self.main_path = os.path.abspath(path)
        self._loop_for_next_file = True
        self._play_file(self.main_path, loop=True, start=None)


    def get_time(self) -> float:
        t = self.mpv.get_property("time-pos")
        try:
            return float(t)
        except Exception:
            return 0.0

    def interrupt(self, path: str, mode: str):
        if not self.main_path:
            raise RuntimeError("Main video is not playing yet.")
        self.saved_pos = self.get_time()
        self.interrupt_mode = mode
        self._loop_for_next_file = False              # interrupt should not loop
        self._pending_interrupt_path = os.path.abspath(path)
        # Do NOT set interrupt_active yet; arm it on file-loaded for this path
        self._play_file(path, loop=False, start=None)


    def overlay_text(self, text: str, font: Optional[str], size: Optional[int],
                     align: str, margin_x: int, margin_y: int, duration_ms: int,
                     rotate_deg: int = 0):
        import threading

        # Single overlay id for these toasts; create attrs if they don't exist
        if not hasattr(self, "_toast_ovl_id"):
            self._toast_ovl_id = 702
        if not hasattr(self, "_toast_timer"):
            self._toast_timer = None

        OVL_ID = self._toast_ovl_id
        RES_W = RES_H = 1000   # simple, fixed canvas; margins are in these pixels

        # Remember current OSD level, bump to 1 so text is visible
        try:
            prev_level = int(self.mpv.get_property("osd-level"))
        except Exception:
            prev_level = None
        try:
            self.mpv.set_property("osd-level", 1)
        except Exception:
            pass

        # Map align → ASS \an and simple anchor->(x,y) on a 1000×1000 canvas
        an_map = {
            "top-left":7, "top-center":8, "top-right":9,
            "center-left":4, "center":5, "center-right":6,
            "bottom-left":1, "bottom-center":2, "bottom-right":3,
        }
        a = an_map.get(align, 8)

        mx, my = int(margin_x), int(margin_y)
        if   a == 7: x_px, y_px = mx, my
        elif a == 8: x_px, y_px = RES_W//2 + mx, my
        elif a == 9: x_px, y_px = RES_W - mx, my
        elif a == 4: x_px, y_px = mx, RES_H//2 + my
        elif a == 5: x_px, y_px = RES_W//2 + mx, RES_H//2 + my
        elif a == 6: x_px, y_px = RES_W - mx, RES_H//2 + my
        elif a == 1: x_px, y_px = mx, RES_H - my
        elif a == 2: x_px, y_px = RES_W//2 + mx, RES_H - my
        else:        x_px, y_px = RES_W - mx, RES_H - my

        # Fixed-length, word-boundary wrapping
        wrap_chars = globals().get("TOAST_WRAP_CHARS", 40)
        def _wrap_words(s: str, maxc: int) -> str:
            words = (s or "").split()
            if not words:
                return ""
            lines, line = [], words[0]
            for w in words[1:]:
                if len(line) + 1 + len(w) <= maxc:
                    line += " " + w
                else:
                    lines.append(line)
                    line = w
            lines.append(line)
            return "\\N".join(lines)

        wrapped = _wrap_words(str(text), int(wrap_chars))

        # Minimal ASS: font, size, outline, rotation, explicit position
        font_tag = f"\\fn{font}" if font else ""
        size_tag = f"\\fs{int(size)}" if size is not None else ""
        rot_tag  = f"\\frz{int(rotate_deg)}" if int(rotate_deg) else ""
        style    = "\\bord3\\1c&HFFFFFF&\\3c&H000000&"  # white with black outline
        ass = "{" + f"\\an{a}{font_tag}{size_tag}{style}\\pos({x_px},{y_px}){rot_tag}" + "}" + wrapped

        # Draw (no “remove”; we’ll blank it later)
        try:
            # your helper must be: mpv.command("osd-overlay", str(id), "ass-events", ass, str(res_x), str(res_y), str(z))
            self._ovl_update(OVL_ID, ass, res_x=RES_W, res_y=RES_H, z=0)
        except Exception:
            pass

        # Cancel any previous clear timer, then schedule a simple "blank" draw
        try:
            if self._toast_timer:
                self._toast_timer.cancel()
        except Exception:
            pass

        def _blank():
            try:
                # print an empty message to the SAME overlay id
                self._ovl_update(OVL_ID, "", res_x=RES_W, res_y=RES_H, z=0)
            except Exception:
                pass
            # restore osd-level if the main timer isn't running
            try:
                active = bool(self._osd_timer_thread and self._osd_timer_thread.is_alive())
            except Exception:
                active = False
            if not active and prev_level is not None:
                try:
                    self.mpv.set_property("osd-level", prev_level)
                except Exception:
                    pass

        import threading
        t = threading.Timer(max(0, int(duration_ms)) / 1000.0, _blank)
        t.daemon = True
        self._toast_timer = t
        t.start()


    # Rotatable OSD message (independent of the timer overlay)
    def show_osd_message(self, text: str,
                         rotate_deg: int = 0,
                         anchor: int = 8,            # ASS \an (1..9)
                         font_size: int = 72,
                         font: Optional[str] = None,
                         duration_ms: int = 1500,
                         margin_x: int = 40,
                         margin_y: int = 40):
        import threading

        OVL_ID = int(getattr(self, "_msg_ovl_id", 701))
        CANVAS_W = CANVAS_H = 1000

        # Build ASS: style + rotation + position from anchor/margins
        font_tag = f"\\fn{font}" if font else ""
        style_common = "\\bord4\\1c&HFFFFFF&\\3c&H000000&\\fad(0,0)"
        a = int(anchor); mx = int(margin_x); my = int(margin_y)
        if   a == 7: x_px, y_px = mx, my
        elif a == 8: x_px, y_px = CANVAS_W//2 + mx, my
        elif a == 9: x_px, y_px = CANVAS_W - mx, my
        elif a == 4: x_px, y_px = mx, CANVAS_H//2 + my
        elif a == 5: x_px, y_px = CANVAS_W//2 + mx, CANVAS_H//2 + my
        elif a == 6: x_px, y_px = CANVAS_W - mx, CANVAS_H//2 + my
        elif a == 1: x_px, y_px = mx, CANVAS_H - my
        elif a == 2: x_px, y_px = CANVAS_W//2 + mx, CANVAS_H - my
        else:        x_px, y_px = CANVAS_W - mx, CANVAS_H - my
        ass = "{" + f"\\an{a}{font_tag}\\fs{int(font_size)}{style_common}\\pos({x_px},{y_px})\\frz{int(rotate_deg)}" + "}" + str(text)

        # Ensure OSD is visible for the toast (remember previous level to restore later)
        try:
            prev_level = int(self.mpv.get_property("osd-level"))
        except Exception:
            prev_level = None
        try:
            self.mpv.set_property("osd-level", 1)
        except Exception:
            pass

        # Clear any stuck old message, then draw this one
        try:
            self._ovl_remove(OVL_ID)
        except Exception:
            pass
        self._ovl_update(OVL_ID, ass, res_x=CANVAS_W, res_y=CANVAS_H, z=0)

        # Cancel any previous auto-remove before scheduling a new one
        try:
            if getattr(self, "_msg_remove_timer", None):
                self._msg_remove_timer.cancel()
        except Exception:
            pass

        def _remove():
            # remove overlay
            try:
                self._ovl_remove(OVL_ID)
            except Exception:
                pass
            # restore osd-level if timer isn’t running
            active = False
            try:
                active = bool(self._osd_timer_thread and self._osd_timer_thread.is_alive())
            except Exception:
                pass
            if not active and prev_level is not None:
                try:
                    self.mpv.set_property("osd-level", prev_level)
                except Exception:
                    pass

        t = threading.Timer(max(0, int(duration_ms)) / 1000.0, _remove)
        t.daemon = True
        self._msg_remove_timer = t
        t.start()


    def audio_overlay_smart_start_single(self, path: str, volume: Optional[int] = None, grace: float = 0.35):
        """Single-file loop variant:
        - If audio fits into current remaining time, attach now.
        - Else restart the same video from t=0 and then attach.
        No persistent state; no reattach on subsequent loops.
        """
        import json, subprocess, os, time
        
        self._orig_audio_id = self._get_selected_audio_track_id()
        
        def _get_time_remaining() -> float:
            try:
                rem = self.mpv.get_property("time-remaining")
                if rem is not None:
                    return max(0.0, float(rem))
            except Exception:
                pass
            try:
                dur = float(self.mpv.get_property("duration") or 0.0)
                pos = float(self.mpv.get_property("time-pos") or 0.0)
                return max(0.0, dur - pos)
            except Exception:
                return 0.0

        def _audio_duration(p: str) -> float:
            try:
                out = subprocess.check_output(
                    ["ffprobe","-v","error","-show_entries","format=duration","-of","json", p],
                    stderr=subprocess.STDOUT, text=True
                )
                return max(0.0, float(json.loads(out)["format"]["duration"]))
            except Exception:
                return 0.0  # unknown -> treat as "fits"

        abs_path = os.path.abspath(path)
        remaining = _get_time_remaining()
        audio_dur = _audio_duration(abs_path)

        if audio_dur <= (remaining + float(grace)) or audio_dur == 0.0:
            if volume is not None:
                try:
                    self.mpv.set_property("volume", int(volume))
                except Exception:
                    pass
            self.mpv.command("audio-add", abs_path, "select")
            if audio_dur > 0.0:
                self._schedule_restore_original_audio(audio_dur)
            
            return {"action": "attach_now", "audio_duration": audio_dur, "time_remaining": remaining}

        # Doesn't fit: restart same file at t=0, then attach (avoids crossing the loop boundary).
        try:
            self.mpv.command("seek", 0, "absolute", "exact")
        except Exception:
            cur_path = self.mpv.get_property("path")
            if cur_path:
                try:
                    self.mpv.command("loadfile", cur_path, "replace")
                except Exception:
                    pass

        time.sleep(0.02)

        if volume is not None:
            try:
                self.mpv.set_property("volume", int(volume))
            except Exception:
                pass
        self.mpv.command("audio-add", abs_path, "select")

        
        new_remaining = _get_time_remaining()
        if audio_dur > 0.0:
            self._schedule_restore_original_audio(audio_dur)        
        return {"action": "restart_then_attach", "audio_duration": audio_dur,
                "time_remaining": remaining, "new_time_remaining": new_remaining}


# ======== HTTP API ========

app = FastAPI()
_controller: Optional[VideoController] = None
_controller_lock = threading.Lock()

# =========================
# Audio-only MPV Controller
# =========================
class AudioController:
    def __init__(self, sock_path: str = "/tmp/mpv-audio.sock"):
        self.sock_path = sock_path
        self.proc: Optional[subprocess.Popen] = None

    # ---- process helpers ----
    def _is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _wait_for_socket(self, timeout_s: float = 3.0) -> bool:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout_s:
            if os.path.exists(self.sock_path):
                try:
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                        s.settimeout(0.2)
                        s.connect(self.sock_path)
                        return True
                except Exception:
                    pass
            time.sleep(0.03)
        return False

    def _ipc(self, cmd: list) -> Dict[str, Any]:
        """Send a JSON-IPC command and return mpv's response (or {})."""
        payload = (json.dumps({"command": cmd}) + "\n").encode("utf-8")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            s.connect(self.sock_path)
            s.sendall(payload)
            data = b""
            # read one line
            while not data.endswith(b"\n"):
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
        try:
            return json.loads(data.decode("utf-8"))
        except Exception:
            return {}

    # ---- public controls ----
    def start(self, path: str, volume: Optional[int] = None, loop: bool = False):
        abs_path = os.path.abspath(path)
        if not os.path.isfile(abs_path):
            raise RuntimeError(f"Audio file not found: {abs_path}")
        # spin up mpv --no-video if not running
        if not self._is_running():
            # remove stale socket if present
            try:
                if os.path.exists(self.sock_path):
                    os.remove(self.sock_path)
            except Exception:
                pass
            self.proc = subprocess.Popen([
                "mpv",
                "--no-video",
                "--input-ipc-server=" + self.sock_path,
                "--idle=yes",           # stay alive between tracks
                "--keep-open=yes",
                "--really-quiet"
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if not self._wait_for_socket(3.0):
                raise RuntimeError("audio mpv failed to start")
        # load the file
        self._ipc(["loadfile", abs_path, "replace"])
        # loop setting
        self._ipc(["set_property", "loop-file", "inf" if loop else "no"])
        # volume
        if volume is not None:
            v = max(0, min(100, int(volume)))
            self._ipc(["set_property", "volume", v])
        # ensure playing
        self._ipc(["set_property", "pause", False])

    def stop(self):
        if not self._is_running():
            return
        try:
            self._ipc(["quit"])
        except Exception:
            pass
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
        except Exception:
            pass
        self.proc = None
        try:
            if os.path.exists(self.sock_path):
                os.remove(self.sock_path)
        except Exception:
            pass

    def set_volume(self, volume: int):
        if not self._is_running():
            raise RuntimeError("audio not running")
        v = max(0, min(100, int(volume)))
        self._ipc(["set_property", "volume", v])

    def status(self) -> Dict[str, Any]:
        if not self._is_running():
            return {"running": False}
        # pull a few properties (best-effort)
        resp = {
            "running": True,
            "volume": None,
            "pause": None,
            "filename": None,
        }
        try:
            resp["volume"] = self._ipc(["get_property", "volume"]).get("data")
            resp["pause"] = self._ipc(["get_property", "pause"]).get("data")
            resp["filename"] = self._ipc(["get_property", "filename"]).get("data")
        except Exception:
            pass
        return resp

# single instance
_audio = AudioController()

# --- Error helpers & debug utilities ---
logging.basicConfig(level=logging.INFO)

def _http_500(context: str, e: Exception) -> HTTPException:
    tb = traceback.format_exc()
    logging.error("API error in %s: %s\n%s", context, repr(e), tb)
    return HTTPException(
        status_code=500,
        detail={
            "where": context,
            "type": e.__class__.__name__,
            "message": str(e),
        }
    )

def _tail_mpv_log(lines: int = 60) -> str:
    p = pathlib.Path.home() / "mpv.log"
    if not p.exists():
        return "(mpv.log not found)"
    try:
        with p.open("rb") as f:
            data = f.read()
        tail = b"\n".join(data.splitlines()[-lines:])
        return tail.decode("utf-8", errors="replace")
    except Exception as e:
        return f"(failed to read mpv.log: {e})"

class InterruptBody(BaseModel):
    path: str

class OverlayBody(BaseModel):
    text: str
    font: Optional[str] = None
    size: Optional[int] = None
    align: str = "top-center"
    margin_x: int = 40
    margin_y: int = 40
    rotate_deg: int = 0
    duration_ms: int = 2000  # overlay text display duration only

class PlayBody(BaseModel):
    path: str


class OSDTimerBody(BaseModel):
    seconds: int
    rotation: int = 180     # 0/90/180/270
    anchor: int = 9         # 7/8/9 top, 4/5/6 middle, 1/2/3 bottom
    font_size: int = 72
    # NEW:
    font: Optional[str] = None
    position_mode: Literal["anchor", "percent", "absolute"] = "anchor"
    x: Optional[float] = None
    y: Optional[float] = None
    margin_x: int = 40
    margin_y: int = 40


class MessageBody(BaseModel):
    text: str
    rotate_deg: int = 0
    anchor: int = 8
    font_size: int = 72
    font: Optional[str] = None
    duration_ms: int = 2000
    margin_x: int = 40
    margin_y: int = 40

@app.post("/message")
def osd_message(body: MessageBody):
    with _controller_lock:
        if not _controller:
            raise HTTPException(500, "Controller not initialized")
        _controller.show_osd_message(
            text=body.text,
            rotate_deg=body.rotate_deg,
            anchor=body.anchor,
            font_size=body.font_size,
            font=body.font,
            duration_ms=body.duration_ms,
            margin_x=body.margin_x,
            margin_y=body.margin_y,
        )
    return {"status": "ok"}


@app.post("/play")
def play_main(body: PlayBody):
    try:
        with _controller_lock:
            if not _controller:
                raise RuntimeError("Controller not initialized (did you start the script directly, not via `uvicorn video_server:app`?)")
            _controller.play_main(body.path)
        return {"status": "ok"}
    except Exception as e:
        raise _http_500("POST /play", e)

@app.get("/status")
def status():
    with _controller_lock:
        if not _controller:
            raise HTTPException(500, "Controller not initialized")
        t = _controller.get_time()
        return {
            "main": _controller.main_path,
            "time_pos": t,
            "interrupt_active": _controller.interrupt_active,
            "interrupt_mode": _controller.interrupt_mode
        }

@app.post("/interrupt/return")
def interrupt_return(body: InterruptBody):
    try:
        with _controller_lock:
            if not _controller:
                raise RuntimeError("Controller not initialized")
            _controller.interrupt(body.path, "return")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={
                "where": "POST /interrupt/return",
                "type": e.__class__.__name__,
                "message": str(e),
                "mpv_log_tail": _tail_mpv_log(80)
            }
        )

@app.post("/interrupt/skip")
def interrupt_skip(body: InterruptBody):
    try:
        with _controller_lock:
            if not _controller:
                raise RuntimeError("Controller not initialized")
            _controller.interrupt(body.path, "skip")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={
                "where": "POST /interrupt/skip",
                "type": e.__class__.__name__,
                "message": str(e),
                "mpv_log_tail": _tail_mpv_log(80)
            }
        )

@app.post("/overlay")
def overlay(body: OverlayBody):
    try:
        with _controller_lock:
            if not _controller:
                raise RuntimeError("Controller not initialized")
            _controller.overlay_text(
                text=body.text,
                font=body.font,
                size=body.size,
                align=body.align,
                margin_x=body.margin_x,
                margin_y=body.margin_y,
                rotate_deg=body.rotate_deg,
                duration_ms=body.duration_ms
            )
        return {"status": "ok"}
    except Exception as e:
        raise _http_500("POST /overlay", e)


# Optional: quick log tail for debugging
@app.get("/debug/mpvlog")
def debug_mpvlog(lines: int = 120):
    try:
        return {"tail": _tail_mpv_log(lines)}
    except Exception as e:
        raise _http_500("GET /debug/mpvlog", e)


@app.post("/timer/osd/start")
def osd_timer_start(body: OSDTimerBody):
    with _controller_lock:
        if not _controller:
            raise HTTPException(500, "Controller not initialized")
        _controller.start_osd_timer(
            seconds=body.seconds,
            rotation=body.rotation,
            anchor=body.anchor,
            font_size=body.font_size,
            font=body.font,
            position_mode=body.position_mode,
            x=body.x, y=body.y,
            margin_x=body.margin_x, margin_y=body.margin_y,
        )
    return {"status": "ok"}

@app.post("/timer/osd/stop")
def osd_timer_stop():
    with _controller_lock:
        if not _controller:
            raise HTTPException(500, "Controller not initialized")
        _controller.stop_osd_timer()
    return {"status": "ok"}

@app.post("/timer/osd/pause")
def osd_timer_pause():
    with _controller_lock:
        if not _controller:
            raise HTTPException(500, "Controller not initialized")
        _controller.pause_osd_timer()
    return {"status": "ok"}

@app.post("/timer/osd/resume")
def osd_timer_resume():
    with _controller_lock:
        if not _controller:
            raise HTTPException(500, "Controller not initialized")
        _controller.resume_osd_timer()
    return {"status": "ok"}


# ---------- OVERLAY (headerless) ----------
@app.get("/cogs/overlay")
def cogs_overlay_get(
    text: str,
    align: str = "top-center",
    margin_x: int = 40,
    margin_y: int = 40,
    size: int | None = None,
    font: str | None = None,
    duration_ms: int = 2000,
    rotate_deg: int = 0,
):
    with _controller_lock:
        _controller.overlay_text(
            text=text, font=font, size=size, align=align,
            margin_x=margin_x, margin_y=margin_y,
            duration_ms=duration_ms, rotate_deg=rotate_deg
        )
    return {"ok": True}

@app.post("/cogs/overlay")
async def cogs_overlay_post(request: Request):
    d = await _parse_noheader_body(request)
    if not d.get("text"):
        raise HTTPException(400, "missing 'text'")
    with _controller_lock:
        _controller.overlay_text(
            text=str(d.get("text","")),
            font=d.get("font"),
            size=int(d["size"]) if d.get("size") not in (None, "") else None,
            align=str(d.get("align","top-center")),
            margin_x=int(d.get("margin_x", 40)),
            margin_y=int(d.get("margin_y", 40)),
            duration_ms=int(d.get("duration_ms", 2000)),
            rotate_deg=int(d.get("rotate_deg", 0)),
        )
    return {"ok": True}


# ---------- TIMER OSD (headerless) ----------
@app.get("/cogs/timer/osd/start")
def cogs_timer_osd_start_get(
    seconds: int,
    rotation: int = 180,
    anchor: int = 9,
    font_size: int = 72,
    font: str | None = None,
    position_mode: str = "anchor",
    x: float | None = None,
    y: float | None = None,
    margin_x: int = 40,
    margin_y: int = 40,
):
    with _controller_lock:
        _controller.start_osd_timer(
            seconds, rotation, anchor, font_size, font,
            position_mode, x, y, margin_x, margin_y
        )
    return {"ok": True}

@app.post("/cogs/timer/osd/start")
async def cogs_timer_osd_start_post(request: Request):
    d = await _parse_noheader_body(request)
    with _controller_lock:
        _controller.start_osd_timer(
            int(d.get("seconds", 0)),
            int(d.get("rotation", 180)),
            int(d.get("anchor", 9)),
            int(d.get("font_size", 72)),
            d.get("font"),
            str(d.get("position_mode", "anchor")),
            float(d["x"]) if d.get("x") not in (None, "") else None,
            float(d["y"]) if d.get("y") not in (None, "") else None,
            int(d.get("margin_x", 40)),
            int(d.get("margin_y", 40)),
        )
    return {"ok": True}

@app.get("/cogs/timer/osd/pause")
def cogs_timer_osd_pause_get():
    with _controller_lock:
        _controller.pause_osd_timer()
    return {"ok": True}

@app.get("/cogs/timer/osd/resume")
def cogs_timer_osd_resume_get():
    with _controller_lock:
        _controller.resume_osd_timer()
    return {"ok": True}

@app.get("/cogs/timer/osd/stop")
def cogs_timer_osd_stop_get():
    with _controller_lock:
        _controller.stop_osd_timer()
    return {"ok": True}

# ---------- INTERRUPT (headerless, for COGS) ----------
@app.get("/cogs/interrupt/return")
def cogs_interrupt_return_get(path: str):
    with _controller_lock:
        if not _controller:
            raise HTTPException(500, "Controller not initialized")
        _controller.interrupt(path, "return")
    return {"ok": True}

@app.get("/cogs/interrupt/skip")
def cogs_interrupt_skip_get(path: str):
    with _controller_lock:
        if not _controller:
            raise HTTPException(500, "Controller not initialized")
        _controller.interrupt(path, "skip")
    return {"ok": True}

@app.post("/cogs/interrupt/return")
async def cogs_interrupt_return_post(request: Request):
    d = await _parse_noheader_body(request)  # supports raw "path=/foo/bar.mp4" or JSON
    if not d.get("path"):
        raise HTTPException(400, "missing 'path'")
    with _controller_lock:
        if not _controller:
            raise HTTPException(500, "Controller not initialized")
        _controller.interrupt(str(d["path"]), "return")
    return {"ok": True}

@app.post("/cogs/interrupt/skip")
async def cogs_interrupt_skip_post(request: Request):
    d = await _parse_noheader_body(request)
    if not d.get("path"):
        raise HTTPException(400, "missing 'path'")
    with _controller_lock:
        if not _controller:
            raise HTTPException(500, "Controller not initialized")
        _controller.interrupt(str(d["path"]), "skip")
    return {"ok": True}

@app.get("/cogs/play")
def cogs_play_get(path: str):
    with _controller_lock:
        if not _controller:
            raise HTTPException(500, "Controller not initialized")
        _controller.play_main(path)
    return {"ok": True}

@app.post("/cogs/play")
async def cogs_play_post(request: Request):
    d = await _parse_noheader_body(request)  # accepts path in body or query
    if not d.get("path"):
        raise HTTPException(400, "missing 'path'")
    with _controller_lock:
        if not _controller:
            raise HTTPException(500, "Controller not initialized")
        _controller.play_main(str(d["path"]))
    return {"ok": True}


from urllib.parse import unquote_plus

# /cogs/overlayp/<rotate>/<align>/<mx>/<my>/<size>/<font>/<duration_ms>/<message...>
@app.get("/cogs/overlayp/{rotate_deg}/{align}/{margin_x}/{margin_y}/{size}/{font}/{duration_ms}/{raw:path}")
def cogs_overlay_path(rotate_deg: int, align: str, margin_x: int, margin_y: int,
                      size: int, font: str, duration_ms: int, raw: str):
    # Decode message + font safely (accept %20, +, _, and our tiny tokens)
    msg = unquote_plus(raw or "")
    msg = (msg.replace("%20", " ").replace("_", " ")
              .replace("~a","&").replace("~q","?").replace("~h","#")
              .replace("~p","%").replace("~pl","+").replace("~dq","\""))

    fnt = unquote_plus(font or "")
    fnt = fnt.replace("%20", " ").replace("_", " ")
    if fnt in ("-", "none", ""):  # allow "no font" via "-", "none", or empty
        fnt = None

    with _controller_lock:
        _controller.overlay_text(
            text=msg,
            font=fnt,
            size=int(size),
            align=align,
            margin_x=int(margin_x),
            margin_y=int(margin_y),
            duration_ms=int(duration_ms),
            rotate_deg=int(rotate_deg),
        )
    return {"ok": True}

@app.get("/cogs/audio/play")
def cogs_audio_play(path: str, volume: Optional[int] = None, loop: int = 0):
    try:
        _audio.start(path, volume=None if volume is None else int(volume), loop=bool(int(loop)))
        return {"ok": True}
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except Exception:
        raise HTTPException(500, "failed to start audio")

@app.get("/cogs/audio/stop")
def cogs_audio_stop():
    _audio.stop()
    return {"ok": True}

@app.get("/cogs/audio/status")
def cogs_audio_status():
    return {"ok": True, **_audio.status()}

@app.get("/cogs/audio/volume")
def cogs_audio_volume(volume: int):
    try:
        _audio.set_volume(int(volume))
        return {"ok": True, "volume": int(volume)}
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except Exception:
        raise HTTPException(500, "failed to set volume")

# ======== Entrypoint ========

@app.get("/cogs/audio/overlay/smart_start_single")
def cogs_audio_overlay_smart_start_single_get(path: str, volume: Optional[int] = None, grace: float = 0.35):
    if not path:
        raise HTTPException(400, "missing 'path'")
    with _controller_lock:
        if not _controller:
            raise HTTPException(500, "Controller not initialized")
        result = _controller.audio_overlay_smart_start_single(
            path, volume=None if volume is None else int(volume), grace=float(grace)
        )
    return {"ok": True, **result}

@app.post("/cogs/audio/overlay/smart_start_single")
async def cogs_audio_overlay_smart_start_single_post(request: Request):
    d = await _parse_noheader_body(request)
    path = (d.get("path") or "").strip()
    if not path:
        raise HTTPException(400, "missing 'path'")
    volume = d.get("volume")
    grace = float(d.get("grace", 0.35))
    with _controller_lock:
        if not _controller:
            raise HTTPException(500, "Controller not initialized")
        result = _controller.audio_overlay_smart_start_single(
            path, volume=None if volume in (None, "") else int(volume), grace=grace
        )
    return {"ok": True, **result}


def parse_args():
    ap = argparse.ArgumentParser(description="Looping video server with interrupt, overlays, and countdown timer (mpv + HTTP API)")
    ap.add_argument("--main", help="Path to main looping video (optional at startup)")
    ap.add_argument("--fullscreen", action="store_true", help="Start in fullscreen")
    ap.add_argument("--host", default="0.0.0.0", help="API bind host")
    ap.add_argument("--port", type=int, default=8000, help="API port")
    return ap.parse_args()

if __name__ == "__main__":
    args = parse_args()
    _controller = VideoController(main_path=args.main, fullscreen=args.fullscreen)
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port)




