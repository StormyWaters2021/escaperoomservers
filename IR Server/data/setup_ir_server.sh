#!/usr/bin/env bash
# IR Server installer / service manager (COGS endpoints version)
# Usage:
#   sudo bash setup_ir_server.sh install
#   sudo bash setup_ir_server.sh enable|disable|start|stop|status|remove

set -euo pipefail
[ -z "${BASH_VERSION:-}" ] && exec /bin/bash "$0" "$@"
sed -i 's/\r$//' "$0" >/dev/null 2>&1 || true

APP_DIR="/opt/ir-server"
VENV_DIR="${APP_DIR}/.venv"
APP_PY="${APP_DIR}/ir_server.py"
SIGNALS_DIR="${APP_DIR}/signals"
SERVICE="ir-server.service"

# Defaults (kept for parity with your prior installer; can be used by a future server update)
IR_TX_GPIO="${IR_TX_GPIO:-22}"
IR_RX_GPIO="${IR_RX_GPIO:-23}"
IR_CARRIER_KHZ="${IR_CARRIER_KHZ:-38.0}"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

ensure_deps() {
  export DEBIAN_FRONTEND=noninteractive
  apt-get update || true
  apt-get install -y python3 python3-venv python3-pip pigpio || true
  systemctl enable --now pigpiod
}

install_app() {
  echo "[*] Installing IR server to ${APP_DIR}"
  mkdir -p "${APP_DIR}"
  mkdir -p "${SIGNALS_DIR}"

  # Copy Python (normalize line endings)
  if [ -f "${SCRIPT_DIR}/ir_server.py" ]; then
    sed -i 's/\r$//' "${SCRIPT_DIR}/ir_server.py" || true
    install -m 0644 "${SCRIPT_DIR}/ir_server.py" "${APP_PY}"
  else
    echo "ERROR: ${SCRIPT_DIR}/ir_server.py not found" >&2
    exit 1
  fi

  # Python venv + deps
  python3 -m venv "${VENV_DIR}"
  "${VENV_DIR}/bin/pip" install --upgrade pip wheel
  "${VENV_DIR}/bin/pip" install fastapi uvicorn pigpio pydantic

  # systemd unit (same pattern as your existing manager, including sentinel disable files)
  cat > "/etc/systemd/system/${SERVICE}" <<EOF
[Unit]
Description=IR Server (FastAPI + pigpio, COGS endpoints)
Wants=network-online.target pigpiod.service
After=network-online.target pigpiod.service
# Allow disabling at boot with either file:
ConditionPathExists=!/etc/ir-server.disabled
ConditionPathExists=!/boot/firmware/server.disable

[Service]
Type=simple
User=root
WorkingDirectory=${APP_DIR}
Environment=IR_TX_GPIO=${IR_TX_GPIO}
Environment=IR_RX_GPIO=${IR_RX_GPIO}
Environment=IR_CARRIER_KHZ=${IR_CARRIER_KHZ}
# If you later update ir_server.py to read these env vars, they'll be ready.
ExecStart=${VENV_DIR}/bin/uvicorn ir_server:app --host 0.0.0.0 --port 8001
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable "${SERVICE}"
  systemctl restart pigpiod
  systemctl restart "${SERVICE}"

  echo "[+] Installed. Service: ${SERVICE} (port 8001)"
  echo "    Disable on boot:"
  echo "      sudo touch /etc/ir-server.disabled"
  echo "      # or create 'server.disable' on the boot partition (Windows-visible: /boot/firmware)"
}

disable_srv() { systemctl disable --now "${SERVICE}" || true; }
enable_srv()  { systemctl enable --now "${SERVICE}"; }
start_srv()   { systemctl start "${SERVICE}"; }
stop_srv()    { systemctl stop "${SERVICE}" || true; }
status_srv()  { systemctl status "${SERVICE}" --no-pager || true; }

remove_all() {
  stop_srv
  systemctl disable "${SERVICE}" || true
  rm -f "/etc/systemd/system/${SERVICE}"
  systemctl daemon-reload
  echo "[!] Removed service. App files left at ${APP_DIR}."
  echo "    To delete app files: sudo rm -rf ${APP_DIR}"
}

case "${1:-}" in
  install) ensure_deps; install_app ;;
  disable) disable_srv ;;
  enable)  enable_srv ;;
  start)   start_srv ;;
  stop)    stop_srv ;;
  status)  status_srv ;;
  remove)  remove_all ;;
  *)
    echo "Usage: sudo bash $0 {install|enable|disable|start|stop|status|remove}"
    exit 1 ;;
esac
