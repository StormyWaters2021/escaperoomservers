#!/usr/bin/env bash
set -euo pipefail
[ -n "${BASH_VERSION:-}" ] || exec /usr/bin/env bash "$0" "$@"

# Normalize CRLF/BOM if this file was edited on Windows
sed -i 's/\r$//' "$0" >/dev/null 2>&1 || true
sed -i '1s/^\xEF\xBB\xBF//' "$0" >/dev/null 2>&1 || true

APP_NAME="audio-server"
APP_DIR="${APP_DIR:-/opt/${APP_NAME}}"
VENV_DIR="${VENV_DIR:-${APP_DIR}/.venv}"
APP_PY="${APP_PY:-${APP_DIR}/audio_server.py}"
SERVICE="${SERVICE:-${APP_NAME}.service}"
PORT="${PORT:-8003}"

# Use the repo that was cloned by the master installer
REPO_ROOT="${ESCAPEROOM_REPO_DIR:-/opt/escaperoomservers/repo}"
SRC_PY="${SRC_PY:-${REPO_ROOT}/Audio Server/data/audio_server.py}"

AUDIO_FILES_DIR="${AUDIO_FILES_DIR:-${APP_DIR}/audio_files}"

DISABLE_FLAG_ETC="/etc/${APP_NAME}.disabled"
BOOT_FLAG_1="/boot/${APP_NAME}.disable"
BOOT_FLAG_2="/boot/firmware/${APP_NAME}.disable"

# Choose the service user (default: SUDO_USER, else pi, else current)
if [[ -n "${SUDO_USER-}" ]] && id -u "$SUDO_USER" &>/dev/null; then
  RUN_USER="$SUDO_USER"
elif id -u pi &>/dev/null; then
  RUN_USER="pi"
else
  RUN_USER="$(id -un)"
fi

require_root(){
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo "Please run as root (use sudo)." >&2
    exit 1
  fi
}

log(){ echo "[$APP_NAME] $*"; }

ensure_dirs(){
  mkdir -p "$APP_DIR" "$AUDIO_FILES_DIR"
  chown -R "$RUN_USER:$RUN_USER" "$APP_DIR" || true
}

apt_install(){
  export DEBIAN_FRONTEND=noninteractive
  apt-get update || { sleep 3; apt-get update; }

  # Core runtime deps
  local pkgs=(python3 python3-venv python3-pip alsa-utils libasound2)
  apt-get install -y "${pkgs[@]}"

  # Ensure the service user can access ALSA devices
  usermod -aG audio "$RUN_USER" || true

  # Prefer system packages for numpy/alsaaudio to avoid building on Pi
  if apt-get install -y python3-numpy python3-alsaaudio; then
    log "Installed python3-numpy and python3-alsaaudio from apt."
  else
    log "Could not install python3-numpy/python3-alsaaudio from apt; falling back to pip build deps."
    apt-get install -y build-essential libasound2-dev || true
  fi
}

ensure_venv(){
  if [[ ! -d "$VENV_DIR" ]]; then
    # Use system-site-packages so apt-provided numpy/alsaaudio are visible in the venv
    sudo -u "$RUN_USER" python3 -m venv --system-site-packages "$VENV_DIR"
  fi
  sudo -u "$RUN_USER" bash -lc "
    set -e
    source '$VENV_DIR/bin/activate'
    python -m pip install --upgrade pip wheel
    python -m pip install fastapi 'uvicorn[standard]'
    # If numpy/alsaaudio weren't installed via apt, these will satisfy imports (may compile)
    python -m pip install --upgrade numpy pyalsaaudio || true
  "
}

deploy_app(){
  ensure_dirs
  if [[ ! -f "$SRC_PY" ]]; then
    echo "Source not found: $SRC_PY" >&2
    exit 1
  fi
  sed -i 's/\r$//' "$SRC_PY" >/dev/null 2>&1 || true
  install -m 0644 "$SRC_PY" "$APP_PY"
  chown "$RUN_USER:$RUN_USER" "$APP_PY" || true
}

write_service(){
  cat > "/etc/systemd/system/${SERVICE}" <<EOF
[Unit]
Description=Audio Server (FastAPI + ALSA)
Wants=network-online.target
After=network-online.target
ConditionPathExists=!${DISABLE_FLAG_ETC}
ConditionPathExists=!${BOOT_FLAG_1}
ConditionPathExists=!${BOOT_FLAG_2}

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${APP_DIR}
Environment=AUDIO_FOLDER=${AUDIO_FILES_DIR}
ExecStart=${VENV_DIR}/bin/uvicorn audio_server:app --host 0.0.0.0 --port ${PORT}
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
}

do_install(){
  require_root
  apt_install
  ensure_dirs
  ensure_venv
  deploy_app
  write_service
  systemctl enable "$SERVICE"
  systemctl restart "$SERVICE"
  log "Install complete (service: $SERVICE, port: $PORT, app: $APP_PY)"
}

do_update(){
  require_root
  deploy_app
  systemctl restart "$SERVICE" || true
  log "Updated and restarted $SERVICE"
}

do_enable(){ require_root; rm -f "$DISABLE_FLAG_ETC"; systemctl enable "$SERVICE"; systemctl start "$SERVICE"; }
do_disable(){ require_root; touch "$DISABLE_FLAG_ETC"; systemctl stop "$SERVICE" || true; systemctl disable "$SERVICE" || true; }
do_start(){ require_root; systemctl start "$SERVICE"; }
do_stop(){ require_root; systemctl stop "$SERVICE" || true; }
do_restart(){ require_root; systemctl restart "$SERVICE" || true; }
do_status(){ systemctl status "$SERVICE" --no-pager || true; }
do_logs(){ journalctl -u "$SERVICE" -n 200 --no-pager || true; }

do_boot_disable(){
  require_root
  touch "/boot/${APP_NAME}.disable" 2>/dev/null || true
  touch "/boot/firmware/${APP_NAME}.disable" 2>/dev/null || true
  systemctl stop "$SERVICE" || true
  log "Created boot disable flag(s): /boot/${APP_NAME}.disable and/or /boot/firmware/${APP_NAME}.disable"
}

do_boot_enable(){
  require_root
  rm -f "$BOOT_FLAG_1" "$BOOT_FLAG_2" || true
  systemctl daemon-reload
  systemctl restart "$SERVICE" || true
  log "Removed boot disable flag(s)."
}

do_set_port(){
  require_root
  local new="${1:-}"
  [[ -z "$new" ]] && { echo "Usage: $0 set-port <port>"; exit 1; }
  if ! [[ "$new" =~ ^[0-9]+$ ]]; then
    echo "Port must be numeric." >&2
    exit 1
  fi
  PORT="$new"
  write_service
  systemctl restart "$SERVICE"
  log "Port updated to: $PORT"
}

do_info(){
  echo "Server: Audio Server"
  echo "Service: $SERVICE"
  echo "App: $APP_PY"
  echo "Port: $PORT"
  echo "Resources:"
  echo "  Audio files folder: $AUDIO_FILES_DIR"
  echo "  Server reads audio folder via env: AUDIO_FOLDER=$AUDIO_FILES_DIR"
}

usage(){
  cat <<EOF
Usage: sudo bash $(basename "$0") <command>

Commands:
  install        Install dependencies, deploy to $APP_DIR, create/enable systemd service
  update         Re-copy python file from repo and restart service
  enable         Enable service and remove disable flag
  disable        Stop+disable service and create disable flag ($DISABLE_FLAG_ETC)
  boot-disable   Create boot-partition disable file(s) and stop service
  boot-enable    Remove boot disable file(s) and restart service
  start|stop|restart|status|logs
  set-port <p>   Rewrite unit to use a new port
  info           Print service/app/port + resource locations (no changes)
EOF
}

cmd="${1:-install}"
case "$cmd" in
  install) do_install ;;
  update) do_update ;;
  enable) do_enable ;;
  disable) do_disable ;;
  boot-disable) do_boot_disable ;;
  boot-enable) do_boot_enable ;;
  start) do_start ;;
  stop) do_stop ;;
  restart) do_restart ;;
  status) do_status ;;
  logs) do_logs ;;
  set-port) shift; do_set_port "${1:-}" ;;
  info) do_info ;;
  *) usage; exit 1 ;;
esac
