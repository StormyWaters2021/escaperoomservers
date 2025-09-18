#!/usr/bin/env bash
set -euo pipefail

# =========================
# Config (can be overridden via env or with set-* commands)
# =========================
APP_NAME="ir-server"
APP_DIR="/opt/ir-server"
APP_FILE_LOCAL="ir_server.py"          # must exist in the current directory for install/update
APP_ENTRY="ir_server:app"
VENVDIR="$APP_DIR/.venv"
SERVICE_NAME="ir-server.service"

# Disable flags
DISABLE_FLAG_ETC="/etc/ir-server.disabled"
# Boot partition flags (Windows-visible). Either path may exist depending on OS.
BOOT_FLAG_1="/boot/server.disable"
BOOT_FLAG_2="/boot/firmware/server.disable"

# Defaults (can be changed later via commands)
PORT="${PORT:-8001}"
IR_TX_GPIO="${IR_TX_GPIO:-18}"
IR_RX_GPIO="${IR_RX_GPIO:-23}"
IR_CARRIER_KHZ="${IR_CARRIER_KHZ:-38.0}"
IR_CODES_DB="${IR_CODES_DB:-$APP_DIR/ir_codes.json}"

# Detect the intended service user automatically at install time.
# Priority: invoking login user -> SUDO_USER -> current user
detect_user() {
  local u
  u="$(logname 2>/dev/null || true)"
  if [[ -z "${u:-}" ]]; then
    u="${SUDO_USER:-}"
  fi
  if [[ -z "${u:-}" ]]; then
    u="$(whoami)"
  fi
  echo "$u"
}
SERVICE_USER="${SERVICE_USER:-$(detect_user)}"

# =========================
usage() {
  cat <<EOF
Usage: sudo bash $0 <command> [args]

Commands:
  install                 Install everything, copy ir_server.py, create service, start it
  update                  Re-copy local ir_server.py, (re)install Python deps, restart service
  start|stop|restart      Manage systemd service
  status                  Show service status
  logs                    Follow service logs (Ctrl+C to exit)

  disable                 Temp-disable on Linux (creates ${DISABLE_FLAG_ETC}); stops & blocks on boot
  enable                  Re-enable after 'disable' (removes ${DISABLE_FLAG_ETC})

  boot-disable            Create Windows-visible boot flag (touch ${BOOT_FLAG_1} or ${BOOT_FLAG_2})
  boot-enable             Remove Windows-visible boot flag

  set-port <PORT>         Change port and restart
  set-tx <GPIO>           Change IR TX GPIO and restart
  set-rx <GPIO>           Change IR RX GPIO and restart

  uninstall               Stop and remove the service + app directory

Environment overrides (optional):
  SERVICE_USER, PORT, IR_TX_GPIO, IR_RX_GPIO, IR_CARRIER_KHZ, IR_CODES_DB

Notes:
- To disable from Windows: mount the SD card and create a file named 'server.disable'
  on the boot partition (no extension). The service won't start while it exists.
EOF
}

require_root() {
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo "Please run as root (use sudo)"; exit 1
  fi
}

apt_install() {
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv python3-pip pigpio
  systemctl enable pigpiod
  systemctl start pigpiod
}

write_service() {
  # The ConditionPathExists lines are ANDed. Using '!' means "only start if the file does NOT exist".
  cat >/etc/systemd/system/$SERVICE_NAME <<EOF
[Unit]
Description=IR Server (FastAPI + pigpio)
Wants=network-online.target pigpiod.service
After=network-online.target pigpiod.service
ConditionPathExists=!${DISABLE_FLAG_ETC}
ConditionPathExists=!${BOOT_FLAG_1}
ConditionPathExists=!${BOOT_FLAG_2}

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${APP_DIR}
Environment=IR_TX_GPIO=${IR_TX_GPIO}
Environment=IR_RX_GPIO=${IR_RX_GPIO}
Environment=IR_CARRIER_KHZ=${IR_CARRIER_KHZ}
Environment=IR_CODES_DB=${IR_CODES_DB}
ExecStart=${VENVDIR}/bin/uvicorn ${APP_ENTRY} --host 0.0.0.0 --port ${PORT}
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
}

install_app() {
  # Prepare directories
  mkdir -p "$APP_DIR"
  # Copy server file in
  if [[ ! -f "$APP_FILE_LOCAL" ]]; then
    echo "ERROR: $APP_FILE_LOCAL not found in current directory"; exit 1
  fi
  cp -f "$APP_FILE_LOCAL" "$APP_DIR/ir_server.py"
  chown -R "$SERVICE_USER":"$SERVICE_USER" "$APP_DIR"

  # Python venv + deps
  python3 -m venv "$VENVDIR"
  "$VENVDIR/bin/pip" install --upgrade pip
  "$VENVDIR/bin/pip" install fastapi uvicorn pigpio

  # Codes DB (if missing)
  if [[ ! -f "$IR_CODES_DB" ]]; then
    mkdir -p "$(dirname "$IR_CODES_DB")"
    echo "{}" > "$IR_CODES_DB"
    chown "$SERVICE_USER":"$SERVICE_USER" "$IR_CODES_DB"
  fi
}

cmd_install() {
  require_root
  echo "Installing as user: ${SERVICE_USER}"
  apt_install
  install_app
  write_service
  systemctl daemon-reload
  systemctl enable "$SERVICE_NAME"
  # Respect existing disable flags
  if [[ -f "$DISABLE_FLAG_ETC" || -f "$BOOT_FLAG_1" || -f "$BOOT_FLAG_2" ]]; then
    echo "Disable flag present. Service will not start until flags are removed."
  else
    systemctl start "$SERVICE_NAME"
  fi
  echo "Install complete."
}

cmd_update() {
  require_root
  if [[ ! -f "$APP_FILE_LOCAL" ]]; then
    echo "ERROR: $APP_FILE_LOCAL not found in current directory"; exit 1
  fi
  cp -f "$APP_FILE_LOCAL" "$APP_DIR/ir_server.py"
  chown -R "$SERVICE_USER":"$SERVICE_USER" "$APP_DIR"
  # ensure venv exists
  if [[ ! -d "$VENVDIR" ]]; then
    python3 -m venv "$VENVDIR"
  fi
  "$VENVDIR/bin/pip" install --upgrade pip
  "$VENVDIR/bin/pip" install fastapi uvicorn pigpio
  systemctl restart "$SERVICE_NAME" || true
  echo "Update complete."
}

cmd_disable() {
  require_root
  touch "$DISABLE_FLAG_ETC"
  systemctl stop "$SERVICE_NAME" || true
  echo "Temporarily disabled (Linux flag: $DISABLE_FLAG_ETC). To re-enable: sudo bash $0 enable"
}

cmd_enable() {
  require_root
  rm -f "$DISABLE_FLAG_ETC"
  systemctl daemon-reload
  systemctl enable "$SERVICE_NAME"
  systemctl restart "$SERVICE_NAME"
  echo "Enabled and restarted."
}

ensure_boot_mount() {
  # Ensure at least one boot mount point exists
  if [[ -d "/boot/firmware" ]]; then
    BOOT_DIR="/boot/firmware"
  elif [[ -d "/boot" ]]; then
    BOOT_DIR="/boot"
  else
    echo "Could not find /boot or /boot/firmware mount point."; exit 1
  fi
  echo "$BOOT_DIR"
}

cmd_boot_disable() {
  require_root
  local boot_dir
  boot_dir="$(ensure_boot_mount)"
  local flag_path="${boot_dir}/server.disable"
  touch "$flag_path"
  echo "Created boot disable flag: $flag_path"
  echo "The service will not start while this file exists (also visible/editable from Windows)."
  systemctl stop "$SERVICE_NAME" || true
}

cmd_boot_enable() {
  require_root
  local changed=0
  if [[ -f "$BOOT_FLAG_1" ]]; then rm -f "$BOOT_FLAG_1"; changed=1; fi
  if [[ -f "$BOOT_FLAG_2" ]]; then rm -f "$BOOT_FLAG_2"; changed=1; fi
  if [[ "$changed" -eq 1 ]]; then
    echo "Removed boot disable flag(s)."
  else
    echo "No boot disable flag found."
  fi
  systemctl daemon-reload
  systemctl restart "$SERVICE_NAME" || true
}

cmd_set_port() {
  require_root
  local new_port="${1:-}"
  if [[ -z "$new_port" ]]; then echo "Usage: $0 set-port <PORT>"; exit 1; fi
  PORT="$new_port"
  write_service
  systemctl daemon-reload
  systemctl restart "$SERVICE_NAME" || true
  echo "Port set to $PORT"
}

cmd_set_tx() {
  require_root
  local new_gpio="${1:-}"
  if [[ -z "$new_gpio" ]]; then echo "Usage: $0 set-tx <GPIO>"; exit 1; fi
  IR_TX_GPIO="$new_gpio"
  write_service
  systemctl daemon-reload
  systemctl restart "$SERVICE_NAME" || true
  echo "IR_TX_GPIO set to $IR_TX_GPIO"
}

cmd_set_rx() {
  require_root
  local new_gpio="${1:-}"
  if [[ -z "$new_gpio" ]]; then echo "Usage: $0 set-rx <GPIO>"; exit 1; fi
  IR_RX_GPIO="$new_gpio"
  write_service
  systemctl daemon-reload
  systemctl restart "$SERVICE_NAME" || true
  echo "IR_RX_GPIO set to $IR_RX_GPIO"
}

cmd_uninstall() {
  require_root
  systemctl stop "$SERVICE_NAME" || true
  systemctl disable "$SERVICE_NAME" || true
  rm -f "/etc/systemd/system/$SERVICE_NAME"
  systemctl daemon-reload
  rm -rf "$APP_DIR"
  rm -f "$DISABLE_FLAG_ETC" "$BOOT_FLAG_1" "$BOOT_FLAG_2"
  echo "Uninstalled $APP_NAME."
}

cmd_start()   { require_root; systemctl start "$SERVICE_NAME"; }
cmd_stop()    { require_root; systemctl stop "$SERVICE_NAME" || true; }
cmd_restart() { require_root; systemctl restart "$SERVICE_NAME" || true; }
cmd_status()  { systemctl status "$SERVICE_NAME" --no-pager || true; }
cmd_logs()    { journalctl -u "$SERVICE_NAME" -f --no-pager; }

# =========================
main() {
  local cmd="${1:-}"
  case "$cmd" in
    install)       shift; cmd_install "$@";;
    update)        shift; cmd_update "$@";;
    start)         shift; cmd_start "$@";;
    stop)          shift; cmd_stop "$@";;
    restart)       shift; cmd_restart "$@";;
    status)        shift; cmd_status "$@";;
    logs)          shift; cmd_logs "$@";;
    disable)       shift; cmd_disable "$@";;
    enable)        shift; cmd_enable "$@";;
    boot-disable)  shift; cmd_boot_disable "$@";;
    boot-enable)   shift; cmd_boot_enable "$@";;
    set-port)      shift; cmd_set_port "${1:-}";;
    set-tx)        shift; cmd_set_tx "${1:-}";;
    set-rx)        shift; cmd_set_rx "${1:-}";;
    uninstall)     shift; cmd_uninstall "$@";;
    ""|help|-h|--help) usage;;
    *) echo "Unknown command: $cmd"; usage; exit 1;;
  esac
}
main "$@"
