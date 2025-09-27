#!/usr/bin/env bash
# IR Server installer / service manager
# Usage:
#   sudo bash setup_ir_server.sh install
#   sudo bash setup_ir_server.sh enable|disable|start|stop|status|remove

set -euo pipefail
[ -z "${BASH_VERSION:-}" ] && exec /bin/bash "$0" "$@"
sed -i 's/\r$//' "$0" >/dev/null 2>&1 || true

# ---- Config ----
APP_DIR="/opt/ir-server"
VENV_DIR="${APP_DIR}/.venv"
APP_PY="${APP_DIR}/ir_server.py"
CODES_JSON="${APP_DIR}/ir_codes.json"
SERVICE="ir-server.service"

# Defaults (can be overridden in systemd Environment)
IR_TX_GPIO="${IR_TX_GPIO:-18}"
IR_RX_GPIO="${IR_RX_GPIO:-23}"
IR_CARRIER_KHZ="${IR_CARRIER_KHZ:-38.0}"

# Resolve source directory (repo data folder)
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
  # Normalize and copy python file
  if [ -f "${SCRIPT_DIR}/ir_server.py" ]; then
    sed -i 's/\r$//' "${SCRIPT_DIR}/ir_server.py" || true
    install -m 0644 "${SCRIPT_DIR}/ir_server.py" "${APP_PY}"
  else
    echo "ERROR: ${SCRIPT_DIR}/ir_server.py not found" >&2
    exit 1
  fi

  # Preserve existing codes DB if present
  if [ ! -f "${CODES_JSON}" ]; then
    if [ -f "${SCRIPT_DIR}/ir_codes.json" ]; then
      sed -i 's/\r$//' "${SCRIPT_DIR}/ir_codes.json" || true
      install -m 0644 "${SCRIPT_DIR}/ir_codes.json" "${CODES_JSON}"
    else
      echo "{}" > "${CODES_JSON}"
    fi
  fi

  # Python venv
  python3 -m venv "${VENV_DIR}"
  "${VENV_DIR}/bin/pip" install --upgrade pip wheel
  "${VENV_DIR}/bin/pip" install fastapi uvicorn pigpio

  # Service unit
  cat > "/etc/systemd/system/${SERVICE}" <<EOF
[Unit]
Description=IR Server (FastAPI + pigpio)
Wants=network-online.target pigpiod.service
After=network-online.target pigpiod.service
# Allow disabling at boot with a file (either path)
ConditionPathExists=!/etc/ir-server.disabled
ConditionPathExists=!/boot/firmware/server.disable

[Service]
Type=simple
User=root
WorkingDirectory=${APP_DIR}
# GPIO and settings (override with: systemctl edit ${SERVICE})
Environment=IR_TX_GPIO=${IR_TX_GPIO}
Environment=IR_RX_GPIO=${IR_RX_GPIO}
Environment=IR_CARRIER_KHZ=${IR_CARRIER_KHZ}
Environment=IR_CODES_DB=${CODES_JSON}
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
  echo "    Disable boot:  sudo touch /etc/ir-server.disabled"
  echo "                    OR place a file 'server.disable' on the boot partition (Windows-visible: /boot/firmware)"
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
  echo "[!] Removed service. App files left at ${APP_DIR} (not deleted)."
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
    exit 1
    ;;
esac
