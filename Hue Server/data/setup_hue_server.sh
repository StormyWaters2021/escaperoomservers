#!/usr/bin/env bash
set -euo pipefail

APP_NAME="hue-server"
APP_DIR="/opt/hue-server"
APP_FILE_LOCAL="hue_server.py"
APP_ENTRY="hue_server:app"
VENVDIR="$APP_DIR/.venv"
SERVICE_NAME="hue-server.service"

# Disable flags
DISABLE_FLAG_ETC="/etc/hue-server.disabled"
BOOT_FLAG_1="/boot/hue.disable"
BOOT_FLAG_2="/boot/firmware/hue.disable"

# Defaults (can override via env or set-port)
PORT="${PORT:-8002}"
HUE_CONFIG="${HUE_CONFIG:-$APP_DIR/hue_config.json}"

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
Usage: sudo bash $0 <command>

Commands:
  install         Install app, deps, service (autostarts unless disabled)
  update          Re-copy hue_server.py, reinstall deps, restart service
  start|stop|restart|status|logs
  disable         Temp-disable (creates $DISABLE_FLAG_ETC), stops service
  enable          Remove disable flag and restart
  boot-disable    Create Windows-visible flag (touch boot:/hue.disable) and stop
  boot-enable     Remove boot flag(s) and restart
  set-port N      Change port and restart
  uninstall       Remove service and app

Env overrides: SERVICE_USER, PORT, HUE_CONFIG
EOF
}

require_root() { [[ "${EUID:-$(id -u)}" -eq 0 ]] || { echo "run as root"; exit 1; }; }

apt_install() {
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv python3-pip
}

write_service() {
  cat >/etc/systemd/system/$SERVICE_NAME <<EOF
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
  [[ -f "$APP_FILE_LOCAL" ]] || { echo "Missing $APP_FILE_LOCAL in current dir"; exit 1; }
  cp -f "$APP_FILE_LOCAL" "$APP_DIR/hue_server.py"
  chown -R "$SERVICE_USER":"$SERVICE_USER" "$APP_DIR"
  python3 -m venv "$VENVDIR"
  "$VENVDIR/bin/pip" install --upgrade pip
  "$VENVDIR/bin/pip" install fastapi uvicorn requests
  [[ -f "$HUE_CONFIG" ]] || { mkdir -p "$(dirname "$HUE_CONFIG")"; echo '{"bridge_ip":"","username":"","map":{}}' > "$HUE_CONFIG"; chown "$SERVICE_USER":"$SERVICE_USER" "$HUE_CONFIG"; }
}

cmd_install()   { require_root; echo "Installing as user: $SERVICE_USER"; apt_install; install_app; write_service; systemctl daemon-reload; systemctl enable "$SERVICE_NAME"; [[ -f "$DISABLE_FLAG_ETC" || -f "$BOOT_FLAG_1" || -f "$BOOT_FLAG_2" ]] || systemctl start "$SERVICE_NAME"; echo "Install complete (port: $PORT)."; }
cmd_update()    { require_root; install_app; systemctl restart "$SERVICE_NAME" || true; echo "Update complete."; }
cmd_start()     { require_root; systemctl start "$SERVICE_NAME"; }
cmd_stop()      { require_root; systemctl stop "$SERVICE_NAME" || true; }
cmd_restart()   { require_root; systemctl restart "$SERVICE_NAME" || true; }
cmd_status()    { systemctl status "$SERVICE_NAME" --no-pager || true; }
cmd_logs()      { journalctl -u "$SERVICE_NAME" -f --no-pager; }
cmd_disable()   { require_root; touch "$DISABLE_FLAG_ETC"; systemctl stop "$SERVICE_NAME" || true; echo "Disabled (flag $DISABLE_FLAG_ETC)."; }
cmd_enable()    { require_root; rm -f "$DISABLE_FLAG_ETC"; systemctl daemon-reload; systemctl enable "$SERVICE_NAME"; systemctl restart "$SERVICE_NAME" || true; echo "Enabled."; }
boot_dir()      { [[ -d /boot/firmware ]] && echo /boot/firmware || echo /boot; }
cmd_boot_disable(){ require_root; local d; d="$(boot_dir)"; touch "$d/hue.disable"; systemctl stop "$SERVICE_NAME" || true; echo "Created $d/hue.disable"; }
cmd_boot_enable(){ require_root; local changed=0; [[ -f "$BOOT_FLAG_1" ]] && { rm -f "$BOOT_FLAG_1"; changed=1; }; [[ -f "$BOOT_FLAG_2" ]] && { rm -f "$BOOT_FLAG_2"; changed=1; }; [[ "$changed" -eq 1 ]] && echo "Removed boot flag(s)."; systemctl daemon-reload; systemctl restart "$SERVICE_NAME" || true; }
cmd_set_port() {\
  require_root\
  local PORT="${1:-}"\
  if [[ -z "$PORT" || ! "$PORT" =~ ^[0-9]+$ || "$PORT" -lt 1 || "$PORT" -gt 65535 ]]; then\
    echo "Usage: $0 set-port <1-65535>"\
    exit 2\
  fi\
  echo "[+] Port validated: $PORT"\
}' "/opt/escaperoomservers/repo/Hue Server/data/setup_hue_server.sh"
