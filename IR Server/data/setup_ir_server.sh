#!/usr/bin/env bash
# IR Server installer / service manager (COGS endpoints version)
set -euo pipefail
[ -z "${BASH_VERSION:-}" ] && exec /bin/bash "$0" "$@"

# Normalize CRLF/BOM
sed -i 's/\r$//' "$0" >/dev/null 2>&1 || true
sed -i '1s/^\xEF\xBB\xBF//' "$0" >/dev/null 2>&1 || true

APP_NAME="ir-server"
APP_DIR="${APP_DIR:-/opt/${APP_NAME}}"
VENV_DIR="${VENV_DIR:-${APP_DIR}/.venv}"
APP_PY="${APP_PY:-${APP_DIR}/ir_server.py}"
SIGNALS_DIR="${SIGNALS_DIR:-${APP_DIR}/signals}"
SERVICE="${SERVICE:-${APP_NAME}.service}"
PORT="${PORT:-8001}"

# Use the repo that was cloned by the master installer
REPO_ROOT="${ESCAPEROOM_REPO_DIR:-/opt/escaperoomservers/repo}"
SRC_PY="${SRC_PY:-${REPO_ROOT}/IR Server/data/ir_server.py}"

# Defaults (consumed by ir_server.py via env)
IR_TX_GPIO="${IR_TX_GPIO:-18}"
IR_RX_GPIO="${IR_RX_GPIO:-23}"
IR_CARRIER_KHZ="${IR_CARRIER_KHZ:-38.0}"
IR_SIGNALS_DIR="${IR_SIGNALS_DIR:-${SIGNALS_DIR}}"

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

require_root(){ [[ "${EUID:-$(id -u)}" -eq 0 ]] || { echo "Please run as root (sudo)."; exit 1; }; }
log(){ echo "[$APP_NAME] $*"; }

ensure_deps() {
  export DEBIAN_FRONTEND=noninteractive
  apt-get update || true
  apt-get install -y python3 python3-venv python3-pip

  # If pigpiod is available from apt, use it.
  if apt-cache show pigpiod >/dev/null 2>&1; then
    apt-get install -y pigpiod || true
    systemctl enable --now pigpiod 2>/dev/null || true
    log "Installed pigpiod from apt."
    return 0
  fi

  log "pigpiod not available via apt. Building pigpio (pigpiod) from source..."

  # Build dependencies for pigpio
  apt-get install -y git make gcc libc6-dev

  local TMP="/tmp/pigpio-src"
  rm -rf "$TMP"
  git clone --depth 1 https://github.com/joan2937/pigpio.git "$TMP"
  make -C "$TMP"
  make -C "$TMP" install

  # Install a systemd unit for pigpiod if the OS doesn't provide one
  if [[ ! -f /etc/systemd/system/pigpiod.service && ! -f /lib/systemd/system/pigpiod.service ]]; then
    cat > /etc/systemd/system/pigpiod.service <<'EOF'
[Unit]
Description=pigpio daemon
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/pigpiod -g
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF
  fi

  systemctl daemon-reload
  systemctl enable --now pigpiod 2>/dev/null || true
  log "Installed pigpiod from source + enabled pigpiod.service."
}

install_app() {
  log "Installing IR server to ${APP_DIR} (user: ${RUN_USER})"
  mkdir -p "${APP_DIR}" "${SIGNALS_DIR}"
  chown -R "$RUN_USER:$RUN_USER" "$APP_DIR" || true

  # Copy Python from repo
  [[ -f "$SRC_PY" ]] || { echo "ERROR: Source not found: $SRC_PY" >&2; exit 1; }
  sed -i 's/\r$//' "$SRC_PY" >/dev/null 2>&1 || true
  install -m 0644 "$SRC_PY" "$APP_PY"
  chown "$RUN_USER:$RUN_USER" "$APP_PY" || true

  # Python venv + deps
  if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    sudo -u "$RUN_USER" -H python3 -m venv "${VENV_DIR}"
  fi
  sudo -u "$RUN_USER" -H bash -lc "
    set -e
    source '${VENV_DIR}/bin/activate'
    python -m pip install --upgrade pip wheel
    python -m pip install fastapi uvicorn pigpio pydantic
  "

  # Ensure pigpiod is running.
  # Ensure pigpiod is running (installed from apt if available, otherwise built from source above).
  systemctl enable --now pigpiod 2>/dev/null || true

  # systemd unit
  cat > "/etc/systemd/system/${SERVICE}" <<EOF
[Unit]
Description=IR Server (FastAPI + pigpio, COGS endpoints)
Wants=network-online.target pigpiod.service
After=network-online.target pigpiod.service
ConditionPathExists=!${DISABLE_FLAG_ETC}
ConditionPathExists=!${BOOT_FLAG_1}
ConditionPathExists=!${BOOT_FLAG_2}

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${APP_DIR}
Environment=IR_TX_GPIO=${IR_TX_GPIO}
Environment=IR_RX_GPIO=${IR_RX_GPIO}
Environment=IR_CARRIER_KHZ=${IR_CARRIER_KHZ}
Environment=IR_SIGNALS_DIR=${IR_SIGNALS_DIR}
ExecStart=${VENV_DIR}/bin/uvicorn ir_server:app --host 0.0.0.0 --port ${PORT}
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable "${SERVICE}"
  systemctl restart "${SERVICE}"

  log "Installed. Service: ${SERVICE} (port ${PORT})"
}

disable_srv(){ require_root; touch "$DISABLE_FLAG_ETC"; systemctl stop "${SERVICE}" || true; systemctl disable "${SERVICE}" || true; }
enable_srv(){ require_root; rm -f "$DISABLE_FLAG_ETC"; systemctl enable --now "${SERVICE}"; }
start_srv(){ require_root; systemctl start "${SERVICE}"; }
stop_srv(){ require_root; systemctl stop "${SERVICE}" || true; }
status_srv(){ systemctl status "${SERVICE}" --no-pager || true; }
logs_srv(){ journalctl -u "${SERVICE}" -n 200 --no-pager || true; }

boot_disable(){
  require_root
  touch "/boot/${APP_NAME}.disable" 2>/dev/null || true
  touch "/boot/firmware/${APP_NAME}.disable" 2>/dev/null || true
  systemctl stop "${SERVICE}" || true
  log "Created boot disable flag(s): /boot/${APP_NAME}.disable and/or /boot/firmware/${APP_NAME}.disable"
}

boot_enable(){
  require_root
  rm -f "$BOOT_FLAG_1" "$BOOT_FLAG_2" || true
  systemctl daemon-reload
  systemctl restart "${SERVICE}" || true
  log "Removed boot disable flag(s)."
}

remove_all(){
  require_root
  systemctl disable --now "${SERVICE}" 2>/dev/null || true
  rm -f "/etc/systemd/system/${SERVICE}"
  systemctl daemon-reload
  log "Removed service. App files left at ${APP_DIR}."
  log "To delete app files: sudo rm -rf ${APP_DIR}"
}

info_srv(){
  echo "Server: IR Server"
  echo "Service: ${SERVICE}"
  echo "App: ${APP_PY}"
  echo "Port: ${PORT}"
  echo "Resources:"
  echo "  Signals folder: ${SIGNALS_DIR}"
  echo "  Server reads signals folder via env: IR_SIGNALS_DIR=${IR_SIGNALS_DIR}"
  echo "  pigpiod daemon: must be running (systemd pigpiod.service if available, otherwise pigpiod process)"
}

case "${1:-install}" in
  install) require_root; ensure_deps; install_app ;;
  disable) disable_srv ;;
  enable)  enable_srv ;;
  start)   start_srv ;;
  stop)    stop_srv ;;
  status)  status_srv ;;
  logs)    logs_srv ;;
  boot-disable) boot_disable ;;
  boot-enable)  boot_enable ;;
  info)    info_srv ;;
  remove|uninstall) remove_all ;;
  *)
    echo "Usage: sudo bash $0 {install|enable|disable|start|stop|status|logs|boot-disable|boot-enable|info|remove}"
    exit 1 ;;
esac
