#!/usr/bin/env bash
set -euo pipefail

### ------------ Config (no git pulls here) ------------
APP_NAME="video-server"
APP_DIR="/opt/${APP_NAME}"
SERVICE="${APP_NAME}.service"
PORT="${PORT:-8000}"

# Use the repo that was cloned by the master installer
REPO_ROOT="${ESCAPEROOM_REPO_DIR:-/opt/escaperoomservers/repo}"

+APT_PKGS=(mpv python3 python3-venv python3-pip fontconfig fonts-dejavu-core curl ffmpeg libmpv2)
PIP_PKGS=(fastapi "uvicorn[standard]" python-mpv pydantic requests)

# Choose the service user
if [[ -n "${SUDO_USER-}" ]] && id -u "$SUDO_USER" &>/dev/null; then
  RUN_USER="$SUDO_USER"
elif id -u pi &>/dev/null; then
  RUN_USER="pi"
else
  RUN_USER="$(id -un)"
fi
### ----------------------------------------------------

msg(){ echo "==> $*"; }

require_root(){ [[ "${EUID:-$(id -u)}" -eq 0 ]] || { echo "Please run as root (sudo)."; exit 1; }; }

install_apt() {
  msg "Installing apt packages..."
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y "${APT_PKGS[@]}"
}

prepare_user_env() {
  msg "Ensuring ${RUN_USER} has video access..."
  usermod -aG video,render "$RUN_USER" || true
  mkdir -p "/home/${RUN_USER}/Videos"
  chown -R "${RUN_USER}:${RUN_USER}" "/home/${RUN_USER}/Videos"
}

find_src_dir() {
  # Find the server folder that contains data/video_server.py inside the already-cloned repo
  local m
  m="$(grep -ril --exclude-dir=.git --include=video_server.py '^' "$REPO_ROOT" 2>/dev/null | grep '/data/video_server.py' || true)"
  if [[ -z "$m" ]]; then
    echo "ERROR: Could not locate data/video_server.py under $REPO_ROOT" >&2
    exit 1
  fi
  dirname "$m"   # returns .../<ServerFolder>/data
}

deploy_files() {
  local SRC_DIR="$1"   # .../data
  msg "Deploying from ${SRC_DIR} to ${APP_DIR} ..."
  mkdir -p "${APP_DIR}"
  install -m 0644 "${SRC_DIR}/video_server.py" "${APP_DIR}/video_server.py"
  chown -R "${RUN_USER}:${RUN_USER}" "${APP_DIR}"
  # normalize endings
  sed -i 's/\r$//' "${APP_DIR}/video_server.py" || true

  # ensure data dir exists for assets like blank.mp4
  mkdir -p "${APP_DIR}/data"
  chown -R "${RUN_USER}:${RUN_USER}" "${APP_DIR}/data"

  # Copy optional fonts folder that sits alongside data/
  local SERVER_DIR; SERVER_DIR="$(dirname "$SRC_DIR")"
  if [[ -d "${SERVER_DIR}/fonts" ]]; then
    local FONTS_SRC="${SERVER_DIR}/fonts"
    local FONTS_DST="/usr/local/share/fonts/escaperoom-video"
    msg "Installing custom fonts from ${FONTS_SRC} ..."
    mkdir -p "$FONTS_DST"
    cp -a "$FONTS_SRC/." "$FONTS_DST/"
    fc-cache -f || true
  fi
}

make_blank_video() {
  # Create a tiny 10s black 1920x1080 H.264 MP4 used as the idle/blank screen.
  # Safe to run repeatedly; only writes if missing.
  local BLANK="${APP_DIR}/data/blank.mp4"
  if [[ ! -f "$BLANK" ]]; then
    msg "Creating blank video at ${BLANK} ..."
    # 10 seconds, silent, constant black. Hardware-friendly (h264 baseline).
    sudo -u "${RUN_USER}" ffmpeg -y -v error -f lavfi -i color=c=black:s=1920x1080:r=30:d=10 \
      -f lavfi -i anullsrc=r=48000:cl=stereo -shortest \
      -c:v libx264 -pix_fmt yuv420p -profile:v baseline -level 3.0 -crf 28 -preset veryfast \
      -c:a aac -b:a 96k "$BLANK"
  fi
}


create_venv() {
  msg "Creating Python venv + pip deps..."
  if [[ ! -x "${APP_DIR}/.venv/bin/python" ]]; then
    sudo -u "${RUN_USER}" python3 -m venv "${APP_DIR}/.venv"
  fi
  sudo -u "${RUN_USER}" bash -lc "
    set -e
    source '${APP_DIR}/.venv/bin/activate'
    python -m pip install -U pip wheel
    python -m pip install ${PIP_PKGS[*]}
  "
}

write_runner() {
  msg "Writing run wrapper ..."
  cat >/usr/local/bin/${APP_NAME}-run.sh <<RUN
#!/usr/bin/env bash
set -euo pipefail
cd "${APP_DIR}"
# Allow a simple skip flag at runtime if you ever need it:
[[ -f /run/video-server-skip ]] && { echo "[video-server] Skip flag present. Not starting."; exit 77; }
# Boot with a blank video so mpv opens the display immediately and waits for commands
BLANK="\${BLANK:-${APP_DIR}/data/blank.mp4}"
exec "${APP_DIR}/.venv/bin/python" "${APP_DIR}/video_server.py" \\
  --host 0.0.0.0 --port ${PORT} \\
  --main "\${BLANK}" --fullscreen
RUN
  chmod +x /usr/local/bin/${APP_NAME}-run.sh
  sed -i 's/\r$//' /usr/local/bin/${APP_NAME}-run.sh || true
}

write_unit() {
  local BOOT_DIR="/boot"; [[ -d /boot/firmware ]] && BOOT_DIR="/boot/firmware"
  msg "Writing systemd unit ..."
  cat >/etc/systemd/system/${SERVICE} <<EOF
[Unit]
Description=Video Server (mpv + FastAPI)
Wants=network-online.target
After=network-online.target
ConditionPathExists=!${BOOT_DIR}/video-server.disable
ConditionPathExists=!/boot/video-server.disable

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${APP_DIR}
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/local/bin/${APP_NAME}-run.sh
StandardOutput=journal
StandardError=journal
Restart=always
RestartSec=1
# If the wrapper exits 77 (skip), don't restart
RestartPreventExitStatus=77
SuccessExitStatus=77
TimeoutStartSec=45s
TimeoutStopSec=10s
KillMode=control-group
NoNewPrivileges=true
ProtectSystem=full
ProtectHome=false
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
  sed -i 's/\r$//' "/etc/systemd/system/${SERVICE}" || true
}

reload_enable_start() {
  msg "Enable + start service ..."
  systemctl daemon-reload
  systemctl enable "${SERVICE}"
  rm -f /run/video-server-skip || true
  systemctl restart "${SERVICE}" || true
}

post_checks() {
  echo
  echo "==> Post-install checks:"
  if systemctl is-active --quiet "${SERVICE}"; then
    for i in {1..12}; do
      if curl -fsS -m 2 "http://127.0.0.1:${PORT}/status" >/dev/null; then
        echo "OK: http://127.0.0.1:${PORT}/status"
        break
      fi
      sleep 1
    done
  else
    echo "Service not active; run: sudo journalctl -u ${SERVICE} -e -f"
  fi
  local IP_NOW; IP_NOW="$(hostname -I 2>/dev/null | awk '{print $1}')"
  echo
  echo "Service: ${SERVICE}"
  echo "IP: ${IP_NOW:-<unknown>}"
  echo "Disable next boot from Windows: create empty file on boot partition:"
  local BD="/boot"; [[ -d /boot/firmware ]] && BD="/boot/firmware"
  echo "  ${BD}/video-server.disable"
}

cmd_install() {
  require_root
  msg "Installing from local repo: ${REPO_ROOT}"
  install_apt
  prepare_user_env
  local SRC_DIR; SRC_DIR="$(find_src_dir)"
  deploy_files "$SRC_DIR"
  create_venv
  write_runner
  write_unit
  reload_enable_start
  post_checks
}

case "${1:-install}" in
  install) cmd_install;;
  *) echo "Usage: $0 [install]"; exit 1;;
esac
