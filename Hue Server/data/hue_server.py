#!/usr/bin/env python3
"""
Hue Server (path-only, COGS-friendly)
- COGS endpoints (GET, path-only): turn ON/OFF mapped devices by name.
- Local admin endpoints (path-only): register bridge, list devices, map/unmap names, view mappings & status.

Notes
- Names in URLs can use underscores "_" instead of spaces. The server converts "_" -> " ".
- Uses Hue v1 local API. Press the bridge's LINK button before calling /hue/register/ip/{ip}.
"""
import os, json
from typing import Dict, Any, Optional
import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse

# ---------- Config ----------
APP_TITLE = "Hue Server (path-only)"
CFG_PATH = os.environ.get("HUE_CONFIG", "/opt/hue-server/hue_config.json")
REQUEST_TIMEOUT = float(os.environ.get("HUE_TIMEOUT", "3.0"))
DEVICETYPE = "hue-server#pi"  # registration identifier

app = FastAPI(title=APP_TITLE, version="1.0")

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

# ---------- COGS PATH ENDPOINTS (GET-only, no query strings) ----------
@app.get("/cogs/health")
def cogs_health():
    cfg = load_cfg()
    return {"status": "ok", "bridge_ip": cfg.get("bridge_ip", ""), "mapped": len(cfg.get("map", {}))}

@app.get("/cogs/hue/on/{name}")
def cogs_hue_on(name: str):
    cfg = load_cfg(); require_bridge(cfg)
    lid = _resolve_light_id_by_name(cfg, name)
    return _set_on_state(cfg, lid, True)

@app.get("/cogs/hue/off/{name}")
def cogs_hue_off(name: str):
    cfg = load_cfg(); require_bridge(cfg)
    lid = _resolve_light_id_by_name(cfg, name)
    return _set_on_state(cfg, lid, False)

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
