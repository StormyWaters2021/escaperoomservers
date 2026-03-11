#!/usr/bin/env bash
set -euo pipefail

# =========================
# Config (can override via env)
# =========================
APP_NAME="hue-server"
APP_DIR="${APP_DIR:-/opt/hue-server}"
VENVDIR="${VENVDIR:-$APP_DIR/.venv}"
SERVICE_NAME="${SERVICE_NAME:-hue-server.service}"

# Use the repo that was cloned by the master installer
REPO_ROOT="${ESCAPEROOM_REPO_DIR:-/opt/escaperoomservers/repo}"
SRC_PY="${SRC_PY:-${REPO_ROOT}/Hue Server/data/hue_server.py}"
APP_PY="${APP_PY:-${APP_DIR}/hue_server.py}"
APP_ENTRY="${APP_ENTRY:-hue_server:app}"            # uvicorn import path

# Disable flags (service won't start if these exist)
DISABLE_FLAG_ETC="/etc/hue-server.disabled"
BOOT_FLAG_1="/boot/hue-server.disable"
BOOT_FLAG_2="/boot/firmware/hue-server.disable"

# Defaults
PORT="${PORT:-8002}"
HUE_CONFIG="${HUE_CONFIG:-$APP_DIR/hue_config.json}"

# =========================
# Helpers
# =========================
detect_user() {
  local u
  u="$(logname 2>/dev/null || true)"; [[ -z "${u:-}" ]] && u="${SUDO_USER:-}"
  [[ -z "${u:-}" ]] && u="$(whoami)"; echo "$u"
}
SERVICE_USER="${SERVICE_USER:-$(detect_user)}"

usage() {
cat <<EOF
Usage: sudo bash $0 <command> [args]

Commands:
  install           Install app, deps, and systemd service (autostarts unless disabled)
  update            Re-copy hue_server.py, reinstall deps, and restart service
  start|stop|restart|status|logs
  disable           Create /etc/hue-server.disabled and stop service
  enable            Remove disable flag and restart service
  boot-disable      Touch /boot*/hue-server.disable and stop service (Windows-visible)
  boot-enable       Remove boot flag(s) and restart service
  set-port <N>      Change service port to N and restart
  info              Print service/app/port + config locations (no changes)
  uninstall         Remove service and app directory

Env overrides: SERVICE_USER, APP_DIR, VENVDIR, PORT, HUE_CONFIG, APP_ENTRY, SRC_PY, REPO_ROOT
EOF
}

require_root() { [[ "${EUID:-$(id -u)}" -eq 0 ]] || { echo "run as root"; exit 1; }; }

apt_install() {
  export DEBIAN_FRONTEND=noninteractive
  apt-get update || true
  apt-get install -y python3 python3-venv python3-pip
}

write_service() {
  cat >"/etc/systemd/system/$SERVICE_NAME" <<EOF
[Unit]
Description=Hue Server (FastAPI, Philips Hue control)
Wants=network-online.target
After=network-online.target
ConditionPathExists=!${DISABLE_FLAG_ETC}
ConditionPathExists=!${BOOT_FLAG_1}
ConditionPathExists=!${BOOT_FLAG_2}

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${APP_DIR}
Environment=HUE_CONFIG=${HUE_CONFIG}
ExecStart=${VENVDIR}/bin/uvicorn ${APP_ENTRY} --host 0.0.0.0 --port ${PORT}
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
}

install_app() {
  mkdir -p "$APP_DIR"

  # Copy the app into install dir from the cloned repo
  [[ -f "$SRC_PY" ]] || { echo "Missing source: $SRC_PY"; exit 1; }
  sed -i 's/\r$//' "$SRC_PY" >/dev/null 2>&1 || true
  install -m 0644 "$SRC_PY" "$APP_PY"
  chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"

  # Create/upgrade venv + deps (as service user)
  sudo -u "$SERVICE_USER" -H python3 -m venv "$VENVDIR"
  sudo -u "$SERVICE_USER" -H bash -lc "
    set -e
    source '$VENVDIR/bin/activate'
    python -m pip install --upgrade pip wheel
    python -m pip install fastapi uvicorn requests
  "

  # Default config, if missing
  if [[ ! -f "$HUE_CONFIG" ]]; then
    install -d -m 0755 "$(dirname "$HUE_CONFIG")"
    printf '%s\n' '{"bridge_ip":"","username":"","map":{}}' > "$HUE_CONFIG"
    chown "$SERVICE_USER:$SERVICE_USER" "$HUE_CONFIG"
  fi
}

# =========================
# Commands
# =========================
cmd_install(){
  require_root
  echo "Installing ${APP_NAME} as user: ${SERVICE_USER}"
  apt_install
  install_app
  write_service
  systemctl daemon-reload
  systemctl enable "$SERVICE_NAME"
  if [[ ! -f "$DISABLE_FLAG_ETC" && ! -f "$BOOT_FLAG_1" && ! -f "$BOOT_FLAG_2" ]]; then
    systemctl start "$SERVICE_NAME"
  else
    echo "Start suppressed due to disable flag(s)."
  fi
  echo "Install complete (port: $PORT)."
}

cmd_update(){ require_root; install_app; systemctl restart "$SERVICE_NAME" || true; echo "Update complete."; }
cmd_start(){ require_root; systemctl start "$SERVICE_NAME"; }
cmd_stop(){ require_root; systemctl stop "$SERVICE_NAME" || true; }
cmd_restart(){ require_root; systemctl restart "$SERVICE_NAME" || true; }
cmd_status(){ systemctl status "$SERVICE_NAME" --no-pager || true; }
cmd_logs(){ journalctl -u "$SERVICE_NAME" -f --no-pager; }

cmd_disable(){ require_root; touch "$DISABLE_FLAG_ETC"; systemctl stop "$SERVICE_NAME" || true; echo "Disabled (flag $DISABLE_FLAG_ETC)."; }
cmd_enable(){ require_root; rm -f "$DISABLE_FLAG_ETC"; systemctl daemon-reload; systemctl enable "$SERVICE_NAME"; systemctl restart "$SERVICE_NAME" || true; echo "Enabled."; }

cmd_boot_disable(){
  require_root
  touch "/boot/hue-server.disable" 2>/dev/null || true
  touch "/boot/firmware/hue-server.disable" 2>/dev/null || true
  systemctl stop "$SERVICE_NAME" || true
  echo "Created boot disable flag(s): /boot/hue-server.disable and/or /boot/firmware/hue-server.disable"
}

cmd_boot_enable(){
  require_root
  rm -f "$BOOT_FLAG_1" "$BOOT_FLAG_2" || true
  systemctl daemon-reload
  systemctl restart "$SERVICE_NAME" || true
  echo "Removed boot disable flag(s)."
}

cmd_set_port(){
  require_root
  local new="${1:-}"
  if [[ -z "$new" || ! "$new" =~ ^[0-9]+$ || "$new" -lt 1 || "$new" -gt 65535 ]]; then
    echo "Usage: $0 set-port <1-65535>"
    exit 2
  fi
  PORT="$new"
  write_service
  systemctl daemon-reload
  systemctl restart "$SERVICE_NAME" || true
  echo "Port updated to: $PORT"
}

cmd_info(){
  echo "Server: Hue Server"
  echo "Service: ${SERVICE_NAME}"
  echo "App: ${APP_PY}"
  echo "Port: ${PORT}"
  echo "Resources:"
  echo "  Config file: ${HUE_CONFIG}"
  echo "  Server reads config via env: HUE_CONFIG=${HUE_CONFIG}"
}

cmd_uninstall(){
  require_root
  systemctl disable --now "$SERVICE_NAME" 2>/dev/null || true
  rm -f "/etc/systemd/system/$SERVICE_NAME"
  systemctl daemon-reload
  rm -rf "$APP_DIR"
  echo "Uninstalled $SERVICE_NAME and removed $APP_DIR"
}

main(){
  local cmd="${1:-}"
  case "$cmd" in
    install)      shift; cmd_install "$@";;
    update)       shift; cmd_update "$@";;
    start)        shift; cmd_start "$@";;
    stop)         shift; cmd_stop "$@";;
    restart)      shift; cmd_restart "$@";;
    status)       shift; cmd_status "$@";;
    logs)         shift; cmd_logs "$@";;
    disable)      shift; cmd_disable "$@";;
    enable)       shift; cmd_enable "$@";;
    boot-disable) shift; cmd_boot_disable "$@";;
    boot-enable)  shift; cmd_boot_enable "$@";;
    set-port)     shift; cmd_set_port "${1:-}";;
    info)         shift; cmd_info "$@";;
    uninstall)    shift; cmd_uninstall "$@";;
    -h|--help|help|"") usage;;
    *) echo "Unknown command: $cmd"; usage; exit 2;;
  esac
}
main "$@"
