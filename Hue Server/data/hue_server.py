#!/usr/bin/env python3
"""
Hue Server (path-only, COGS-friendly)
- COGS endpoints (GET, path-only): turn ON/OFF mapped devices by name.
- Local admin endpoints (path-only): register bridge, list devices, map/unmap names, view mappings & status.

Notes
- Names in URLs can use underscores "_" instead of spaces. The server converts "_" -> " ".
- Uses Hue v1 local API. Press the bridge's LINK button before calling /hue/register/ip/{ip}.
"""
import os, json, time
from typing import Dict, Any, Optional
import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse

# ---------- Config ----------
APP_TITLE = "Hue Server (path-only)"
CFG_PATH = os.environ.get("HUE_CONFIG", "/opt/hue-server/hue_config.json")
REQUEST_TIMEOUT = float(os.environ.get("HUE_TIMEOUT", "3.0"))
DEVICETYPE = "hue-server#pi"  # registration identifier

app = FastAPI(title=APP_TITLE, version="1.1")


# ---------- Color presets (name -> Hue state) ----------

# Keys are normalized by: lowercased, "_" and "-" removed
COLOR_PRESETS = {
    # Pure colors (hue/sat)
    "red":        {"hue": 0,     "sat": 254},
    "orange":     {"hue": 6500,  "sat": 254},
    "yellow":     {"hue": 12750, "sat": 254},
    "green":      {"hue": 25500, "sat": 254},
    "cyan":       {"hue": 35000, "sat": 254},
    "blue":       {"hue": 46920, "sat": 254},
    "purple":     {"hue": 56100, "sat": 254},
    "pink":       {"hue": 56100, "sat": 180},
    "magenta":    {"hue": 56100, "sat": 254},
    "mint":       {"hue": 20000, "sat": 120},
    "peach":      {"hue": 8000,  "sat": 180},

    # Whites (color temperature)
    "coolwhite":  {"ct": 153},  # ≈ 6500K
    "daylight":   {"ct": 200},  # ≈ 5000K
    "neutral":    {"ct": 300},  # ≈ 3500K
    "warmwhite":  {"ct": 370},  # ≈ 2700K
    "candle":     {"ct": 450},  # ≈ 2200K
    "softwhite":  {"ct": 400},  # slightly warmer than warmwhite
}

# ---------- Config helpers ----------
def _ensure_cfg_dir():
    d = os.path.dirname(CFG_PATH)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)

def load_cfg() -> Dict[str, Any]:
    if not os.path.exists(CFG_PATH):
        return {"bridge_ip": "", "username": "", "map": {}}
    with open(CFG_PATH, "r") as f:
        try:
            return json.load(f)
        except Exception:
            return {"bridge_ip": "", "username": "", "map": {}}

def save_cfg(cfg: Dict[str, Any]):
    _ensure_cfg_dir()
    with open(CFG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)

def require_bridge(cfg: Dict[str, Any]):
    if not cfg.get("bridge_ip") or not cfg.get("username"):
        raise HTTPException(status_code=400, detail="Bridge not configured. Press bridge LINK button, then call /hue/register/ip/{ip}")

# ---------- Hue local API helpers ----------
def hue_base(cfg) -> str:
    return f"http://{cfg['bridge_ip']}/api/{cfg['username']}"

def hue_get(cfg, path: str):
    r = requests.get(hue_base(cfg) + path, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()

def hue_put(cfg, path: str, body: Dict[str, Any]):
    r = requests.put(hue_base(cfg) + path, json=body, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()

def hue_post_raw(ip: str, path: str, body: Dict[str, Any]):
    r = requests.post(f"http://{ip}{path}", json=body, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()

# ---------- Name/mapping helpers ----------
def _norm_name(name: str) -> str:
    return name.replace("_", " ").strip()

def _resolve_light_id_by_name(cfg, name: str) -> str:
    name_norm = _norm_name(name)
    lid = cfg.get("map", {}).get(name_norm)
    if not lid:
        raise HTTPException(status_code=404, detail=f"Mapping not found for '{name_norm}'. Use /hue/map/{{name}}/{{light_id}}")
    return str(lid)

def _set_on_state(cfg, light_id: str, on: bool):
    # Hue v1: PUT /api/<user>/lights/<id>/state  {"on": true/false}
    res = hue_put(cfg, f"/lights/{light_id}/state", {"on": on})
    if not isinstance(res, list):
        raise HTTPException(status_code=502, detail=f"Unexpected bridge response: {res}")
    return {"ok": True, "light_id": light_id, "on": on}

def _get_light_state(cfg, light_id: str) -> Optional[bool]:
    data = hue_get(cfg, f"/lights/{light_id}")
    return bool(data.get("state", {}).get("on"))


def _get_light_bri(cfg, light_id: str) -> int:
    """
    Return the light's current brightness (0–254).
    If unavailable, returns 0.
    """
    data = hue_get(cfg, f"/lights/{light_id}")
    return int(data.get("state", {}).get("bri", 0) or 0)
    

def _confirm_state(cfg, light_id: str, want_on: bool, retries: int = 5, delay_s: float = 0.1) -> bool:
    """
    Poll the bridge briefly to confirm the on/off state after a write.
    Hue can be eventually consistent; this gives a fast, lightweight confirmation.
    """
    for _ in range(max(1, retries)):
        try:
            is_on = _get_light_state(cfg, light_id)
            if is_on == want_on:
                return True
        except Exception:
            pass
        time.sleep(delay_s)
    return False

# ---------- COGS PATH ENDPOINTS (GET-only, no query strings) ----------
@app.get("/cogs/health")
def cogs_health():
    cfg = load_cfg()
    return {"status": "ok", "bridge_ip": cfg.get("bridge_ip", ""), "mapped": len(cfg.get("map", {}))}

@app.get("/cogs/hue/state/{name}")
def cogs_hue_state(name: str):
    """
    Read the actual on/off state by friendly name (underscores allowed in path).
    Returns: {"ok": true, "name": "...", "light_id": "X", "on": true/false}
    """
    cfg = load_cfg(); require_bridge(cfg)
    lid = _resolve_light_id_by_name(cfg, name)
    on = _get_light_state(cfg, lid)
    return {"ok": True, "name": _norm_name(name), "light_id": lid, "on": bool(on)}

@app.get("/cogs/hue/on/{name}")
def cogs_hue_on(name: str):
    cfg = load_cfg(); require_bridge(cfg)
    lid = _resolve_light_id_by_name(cfg, name)
    _set_on_state(cfg, lid, True)
    confirmed = _confirm_state(cfg, lid, True)
    return {"ok": True, "light_id": lid, "on": True, "confirmed": bool(confirmed)}

@app.get("/cogs/hue/off/{name}")
def cogs_hue_off(name: str):
    cfg = load_cfg(); require_bridge(cfg)
    lid = _resolve_light_id_by_name(cfg, name)
    _set_on_state(cfg, lid, False)
    confirmed = _confirm_state(cfg, lid, False)
    return {"ok": True, "light_id": lid, "on": False, "confirmed": bool(confirmed)}

# ---------- Local admin PATH endpoints ----------
@app.get("/hue/register/ip/{ip}")
def hue_register_path(ip: str):
    """
    Press the physical LINK button on the Hue Bridge, then call:
    /hue/register/ip/{bridge_lan_ip}
    """
    try:
        res = hue_post_raw(ip, "/api", {"devicetype": DEVICETYPE})
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Bridge register error: {e}")

    if not isinstance(res, list) or not res:
        raise HTTPException(status_code=502, detail=f"Unexpected response: {res}")
    first = res[0]
    if "error" in first:
        raise HTTPException(status_code=400, detail=f"Bridge error: {first['error']}")
    username = first.get("success", {}).get("username")
    if not username:
        raise HTTPException(status_code=502, detail=f"No username in response: {res}")

    cfg = load_cfg()
    cfg["bridge_ip"] = ip
    cfg["username"] = username
    cfg.setdefault("map", {})
    save_cfg(cfg)
    return {"ok": True, "bridge_ip": ip, "username_saved": True}

@app.get("/hue/list")
def hue_list():
    cfg = load_cfg(); require_bridge(cfg)
    lights = hue_get(cfg, "/lights")
    trimmed = []
    for lid, info in lights.items():
        trimmed.append({
            "id": lid,
            "name": info.get("name"),
            "type": info.get("type"),
            "on": info.get("state", {}).get("on", None)
        })
    return {"ok": True, "lights": trimmed}

@app.get("/hue/status/txt", response_class=PlainTextResponse)
def hue_status_txt():
    cfg = load_cfg(); require_bridge(cfg)
    lights = hue_get(cfg, "/lights")
    lines = []
    for lid, info in sorted(lights.items(), key=lambda kv: int(kv[0])):
        nm = info.get("name", "")
        tp = info.get("type", "")
        on = info.get("state", {}).get("on", None)
        lines.append(f"{lid}\t{nm}\t{tp}\t(on={on})")
    return "\n".join(lines) + ("\n" if lines else "")

@app.get("/hue/map/{name}/{light_id}")
def hue_map_path(name: str, light_id: str):
    cfg = load_cfg()
    cfg.setdefault("map", {})[_norm_name(name)] = str(light_id)
    save_cfg(cfg)
    return {"ok": True, "mapped": {_norm_name(name): str(light_id)}}

@app.get("/hue/unmap/{name}")
def hue_unmap_path(name: str):
    cfg = load_cfg()
    existed = cfg.get("map", {}).pop(_norm_name(name), None)
    save_cfg(cfg)
    return {"ok": True, "removed": bool(existed)}

@app.get("/hue/mappings")
def hue_mappings():
    cfg = load_cfg()
    return {"ok": True, "map": cfg.get("map", {})}

@app.get("/hue/mappings/txt", response_class=PlainTextResponse)
def hue_mappings_txt():
    cfg = load_cfg()
    m = cfg.get("map", {})
    if not m:
        return "(no mappings)\n"
    lines = [f"{k} -> {v}" for k, v in sorted(m.items())]
    return "\n".join(lines) + "\n"

@app.get("/")
def root():
    return {
        "service": "hue-server-pathonly",
        "cogs": [
            "/cogs/health",
            "/cogs/hue/state/{Name_or_Name_With_Underscores}",
            "/cogs/hue/on/{Name_or_Name_With_Underscores}",
            "/cogs/hue/off/{Name_or_Name_With_Underscores}",
        ],
        "admin": [
            "/hue/register/ip/{bridge_ip}",
            "/hue/list",
            "/hue/status/txt",
            "/hue/map/{name}/{light_id}",
            "/hue/unmap/{name}",
            "/hue/mappings",
            "/hue/mappings/txt",
            "/hue/map/all",
        ],
    }

@app.get("/hue/map/all")
def hue_map_all():
    cfg = load_cfg(); require_bridge(cfg)
    lights = hue_get(cfg, "/lights")
    cfg.setdefault("map", {})
    added = 0
    for lid, info in lights.items():
        nm = info.get("name", "").strip()
        if nm:
            cfg["map"][nm] = str(lid)
            added += 1
    save_cfg(cfg)
    return {"ok": True, "mapped_count": added}
    

# ---------- NEW COGS endpoint: color by name ----------

def _norm_color_name(color: str) -> str:
    """
    Normalize a color name:
    - lowercase
    - remove underscores and dashes
    - strip spaces
    Examples:
      "Warm_White" -> "warmwhite"
      "cool-white" -> "coolwhite"
    """
    return color.lower().replace("_", "").replace("-", "").strip()


@app.get("/cogs/hue/color/name/{name}/{color}")
def cogs_hue_color_name(name: str, color: str):
    """
    Set a light's color using a simple color name, via COGS trigger.
    Examples:
      /cogs/hue/color/name/Desk_Light/red
      /cogs/hue/color/name/Lamp/warm_white
      /cogs/hue/color/name/Office/cool-white
    """
    cfg = load_cfg(); require_bridge(cfg)
    lid = _resolve_light_id_by_name(cfg, name)

    color_key = _norm_color_name(color)
    preset = COLOR_PRESETS.get(color_key)
    if not preset:
        # Provide a helpful error listing supported colors
        supported = sorted(COLOR_PRESETS.keys())
        raise HTTPException(
            status_code=400,
            detail=f"Unknown color '{color}'. Supported colors: {', '.join(supported)}"
        )

    # Build the state body; always turn the light on when setting a color
    body = {"on": True}
    body.update(preset)

    res = hue_put(cfg, f"/lights/{lid}/state", body)

    return {
        "ok": True,
        "name": _norm_name(name),
        "light_id": lid,
        "color": color_key,
        "state_sent": body,
        "bridge_response": res,
    }
    

@app.get("/cogs/hue/bri/{name}/{percent}")
def cogs_hue_brightness(name: str, percent: int):
    """
    Set brightness as a percentage (0–100) via COGS trigger.
    Internally mapped to Hue 'bri' 0–254.
    0% = as dark as possible without turning the light fully off.
    Use /cogs/hue/off/{name} to actually turn it off.
    """
    cfg = load_cfg(); require_bridge(cfg)
    lid = _resolve_light_id_by_name(cfg, name)

    # Clamp percentage 0–100
    percent = max(0, min(100, int(percent)))

    # Map 0–100% -> 0–254 bri
    bri = round(percent * 254 / 100)

    # If we asked for >0% but rounding gave 0, force minimum non-zero
    if percent > 0 and bri == 0:
        bri = 1

    hue_put(cfg, f"/lights/{lid}/state", {"bri": bri})

    return {
        "ok": True,
        "name": _norm_name(name),
        "light_id": lid,
        "percent": percent,
        "bri": bri,
    }


@app.get("/cogs/hue/bri/up/{name}/{delta}")
def cogs_hue_brightness_up(name: str, delta: int):
    """
    Increase brightness by a percentage delta (0–100).
    Example: /cogs/hue/bri/up/Desk_Lamp/10  -> +10%
    """
    cfg = load_cfg(); require_bridge(cfg)
    lid = _resolve_light_id_by_name(cfg, name)

    # Current brightness in 0–254
    bri_now = _get_light_bri(cfg, lid)
    percent_now = round(bri_now * 100 / 254)

    # Clamp delta and compute new percentage
    delta = max(0, min(100, int(delta)))
    target_percent = max(0, min(100, percent_now + delta))

    # Reuse the main percentage setter
    return cogs_hue_brightness(name, target_percent)


@app.get("/cogs/hue/bri/down/{name}/{delta}")
def cogs_hue_brightness_down(name: str, delta: int):
    """
    Decrease brightness by a percentage delta (0–100).
    Example: /cogs/hue/bri/down/Desk_Lamp/10  -> -10%
    """
    cfg = load_cfg(); require_bridge(cfg)
    lid = _resolve_light_id_by_name(cfg, name)

    bri_now = _get_light_bri(cfg, lid)
    percent_now = round(bri_now * 100 / 254)

    delta = max(0, min(100, int(delta)))
    target_percent = max(0, min(100, percent_now - delta))

    return cogs_hue_brightness(name, target_percent)
    

@app.get("/cogs/hue/color/hs/{name}/{hue}/{sat}")
def cogs_hue_color_hs(name: str, hue: int, sat: int):
    """
    Set color using Hue/Sat (full RGB color).
    """
    cfg = load_cfg(); require_bridge(cfg)
    lid = _resolve_light_id_by_name(cfg, name)
    hue = max(0, min(65535, int(hue)))
    sat = max(0, min(254, int(sat)))
    hue_put(cfg, f"/lights/{lid}/state", {"hue": hue, "sat": sat})
    return {"ok": True, "light_id": lid, "hue": hue, "sat": sat}


@app.get("/cogs/hue/color/ct/{name}/{mireds}")
def cogs_hue_color_ct(name: str, mireds: int):
    """
    Set color temperature (153–500 mireds).
    """
    cfg = load_cfg(); require_bridge(cfg)
    lid = _resolve_light_id_by_name(cfg, name)
    mireds = max(153, min(500, int(mireds)))
    hue_put(cfg, f"/lights/{lid}/state", {"ct": mireds})
    return {"ok": True, "light_id": lid, "ct": mireds}


@app.get("/cogs/hue/color/xy/{name}/{x}/{y}")
def cogs_hue_color_xy(name: str, x: float, y: float):
    """
    Set XY color coordinates.
    """
    cfg = load_cfg(); require_bridge(cfg)
    lid = _resolve_light_id_by_name(cfg, name)
    hue_put(cfg, f"/lights/{lid}/state", {"xy": [float(x), float(y)]})
    return {"ok": True, "light_id": lid, "xy": [x, y]}