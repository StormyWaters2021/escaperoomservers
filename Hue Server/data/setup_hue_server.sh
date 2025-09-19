#!/usr/bin/env bash
set -euo pipefail

# =========================
# Config (can override via env)
# =========================
APP_NAME="hue-server"
APP_DIR="${APP_DIR:-/opt/hue-server}"
APP_FILE_LOCAL="${APP_FILE_LOCAL:-hue_server.py}"   # source file (in CWD)
APP_ENTRY="${APP_ENTRY:-hue_server:app}"            # uvicorn import path
VENVDIR="${VENVDIR:-$APP_DIR/.venv}"
SERVICE_NAME="${SERVICE_NAME:-hue-server.service}"

# Disable flags (service won't start if these exist)
DISABLE_FLAG_ETC="/etc/hue-server.disabled"
BOOT_FLAG_1="/boot/hue.disable"
BOOT_FLAG_2="/boot/firmware/hue.disable"

# Defaults (can override via env or 'set-port')
PORT="${PORT:-8002}"
HUE_CONFIG="${HUE_CONFIG:-$APP_DIR/hue_config.json}"

# =========================
# Helpers
# =========================
detect_user() {
  local u
  u="$(logname 2>/dev/null || true)"
  [[ -z "${u:-}" ]] && u="${SUDO_USER:-}"
  [[ -z "${u:-}" ]] && u="$(whoami)"
  echo "$u"
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
  boot-disable      Touch /boot*/hue.disable and stop service (Windows-visible)
  boot-enable       Remove boot flag(s) and restart service
  set-port N        Change service port to N and restart
  uninstall         Remove service and app directory

Env overrides: SERVICE_USER, APP_DIR, VENVDIR, PORT, HUE_CONFIG, APP_ENTRY, APP_FILE_LOCAL
EOF
}

require_root() { [[ "${EUID:-$(id -u)}" -eq 0 ]] || { echo "run as root"; exit 1; }; }

apt_install() {
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
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
  # Ensure source file exists in current working directory
  [[ -f "$APP_FILE_LOCAL" ]] || { echo "Missing $APP_FILE_LOCAL in current dir"; exit 1; }

  # Copy the app into install dir
  install -m 0644 "$APP_FILE_LOCAL" "$APP_DIR/hue_server.py"
  chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"

  # Create/upgrade venv + deps
  python3 -m venv "$VENVDIR"
  "$VENVDIR/bin/pip" install --upgrade pip wheel
  "$VENVDIR/bin/pip" install fastapi uvicorn requests

  # Default config, if missing
  if [[ ! -f "$HUE_CONFIG" ]]; then
    install -d -m 0755 "$(dirname "$HUE_CONFIG")"
    printf '%s\n' '{"bridge_ip":"","username":"","map":{}}' > "$HUE_CONFIG"
    chown "$SERVICE_USER:$SERVICE_USER" "$HUE_CONFIG"
  fi
}

boot_dir() { [[ -d /boot/firmware ]] && echo /boot/firmware || echo /boot; }

# =========================
# Commands
# =========================
cmd_install()   {
  require_root
  echo "Installing ${APP_NAME} as user: ${SERVICE_USER}"
  apt_install
  install_app
  write_service
  systemctl daemon-reload
  systemctl enable "$SERVICE_NAME"
  # Autostart unless any disable flag exists
  if [[ ! -f "$DISABLE_FLAG_ETC" && ! -f "$BOOT_FLAG_1" && ! -f "$BOOT_FLAG_2" ]]; then
    systemctl start "$SERVICE_NAME"
  else
    echo "Start suppressed due to disable flag(s)."
  fi
  echo "Install complete (port: $PORT)."
}

cmd_update()    { require_root; install_app; systemctl restart "$SERVICE_NAME" || true; echo "Update complete."; }
cmd_start()     { require_root; systemctl start "$SERVICE_NAME"; }
cmd_stop()      { require_root; systemctl stop "$SERVICE_NAME" || true; }
cmd_restart()   { require_root; systemctl restart "$SERVICE_NAME" || true; }
cmd_status()    { systemctl status "$SERVICE_NAME" --no-pager || true; }
cmd_logs()      { journalctl -u "$SERVICE_NAME" -f --no-pager; }

cmd_disable()   { require_root; touch "$DISABLE_FLAG_ETC"; systemctl stop "$SERVICE_NAME" || true; echo "Disabled (flag $DISABLE_FLAG_ETC)."; }
cmd_enable()    { require_root; rm -f "$DISABLE_FLAG_ETC"; systemctl daemon-reload; systemctl enable "$SERVICE_NAME"; systemctl restart "$SERVICE_NAME" || true; echo "Enabled."; }

cmd_boot_disable(){
  require_root
  local d; d="$(boot_dir)"
  touch "$d/hue.disable"
  systemctl stop "$SERVICE_NAME" || true
  echo "Created $d/hue.disable"
}

cmd_boot_enable(){
  require_root
  local changed=0
  if [[ -f "$BOOT_FLAG_1" ]]; then rm -f "$BOOT_FLAG_1"; changed=1; fi
  if [[ -f "$BOOT_FLAG_2" ]]; then rm -f "$BOOT_FLAG_2"; changed=1; fi
  [[ "$changed" -eq 1 ]] && echo "Removed boot flag(s)."
  systemctl daemon-reload
  systemctl restart "$SERVICE_NAME" || true
}

cmd_set_port(){
  require_root
  local new="${1:-}"
  # Validate integer 1..65535
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

cmd_uninstall(){
  require_root
  systemctl disable --now "$SERVICE_NAME" 2>/dev/null || true
  rm -f "/etc/systemd/system/$SERVICE_NAME"
  systemctl daemon-reload
  rm -rf "$APP_DIR"
  echo "Uninstalled $SERVICE_NAME and removed $APP_DIR"
}

# =========================
# Dispatcher
# =========================
main() {
  local cmd="${1:-}"
  case "$cmd" in
    install)        shift; cmd_install "$@";;
    update)         shift; cmd_update "$@";;
    start)          shift; cmd_start "$@";;
    stop)           shift; cmd_stop "$@";;
    restart)        shift; cmd_restart "$@";;
    status)         shift; cmd_status "$@";;
    logs)           shift; cmd_logs "$@";;
    disable)        shift; cmd_disable "$@";;
    enable)         shift; cmd_enable "$@";;
    boot-disable)   shift; cmd_boot_disable "$@";;
    boot-enable)    shift; cmd_boot_enable "$@";;
    set-port)       shift; cmd_set_port "${1:-}";;
    uninstall)      shift; cmd_uninstall "$@";;
    -h|--help|help|"") usage;;
    *) echo "Unknown command: $cmd"; usage; exit 2;;
  esac
}
main "$@"
