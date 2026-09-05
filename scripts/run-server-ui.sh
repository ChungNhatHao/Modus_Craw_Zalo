#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${ZALO_PROJECT_DIR:-$(cd -- "$SCRIPT_DIR/.." && pwd)}"
VENV_BIN="${ZALO_VENV_BIN:-$PROJECT_DIR/.venv/bin}"
DISPLAY_VALUE="${ZALO_DISPLAY:-:99}"
SCREEN_VALUE="${ZALO_SCREEN:-1920x1080x24}"
UI_PORT="${ZALO_UI_PORT:-8765}"
VNC_PORT="${ZALO_VNC_PORT:-5900}"
NOVNC_PORT="${ZALO_NOVNC_PORT:-6080}"
RUNTIME_DIR="${ZALO_SERVER_RUNTIME_DIR:-$PROJECT_DIR/.runtime/server-display}"
ROOTLESS_TOOLS_DIR="${ZALO_ROOTLESS_TOOLS_DIR:-$PROJECT_DIR/.runtime/tools/rootfs}"
VNC_PASSWORD_FILE="${ZALO_VNC_PASSWORD_FILE:-}"

export PATH="$ROOTLESS_TOOLS_DIR/usr/bin:$VENV_BIN:$PATH"
if [[ -n "${ZALO_NOVNC_WEB_DIR:-}" ]]; then
  NOVNC_WEB_DIR="$ZALO_NOVNC_WEB_DIR"
elif [[ -f /usr/share/novnc/vnc.html ]]; then
  NOVNC_WEB_DIR=/usr/share/novnc
else
  NOVNC_WEB_DIR="$ROOTLESS_TOOLS_DIR/usr/share/novnc"
fi

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Thiếu lệnh '$1'. Hãy cài gói server theo README." >&2
    exit 1
  fi
}

for command_name in Xvfb xdpyinfo x11vnc websockify; do
  require_command "$command_name"
done

if [[ ! -x "$VENV_BIN/zalo-order-crawler" ]]; then
  echo "Không tìm thấy $VENV_BIN/zalo-order-crawler. Hãy tạo .venv và cài dự án." >&2
  exit 1
fi
if [[ ! "$DISPLAY_VALUE" =~ ^:[0-9]+$ ]]; then
  echo "ZALO_DISPLAY phải có dạng :99." >&2
  exit 1
fi
if [[ ! "$SCREEN_VALUE" =~ ^[0-9]+x[0-9]+x[0-9]+$ ]]; then
  echo "ZALO_SCREEN phải có dạng 1920x1080x24." >&2
  exit 1
fi
for port_value in "$UI_PORT" "$VNC_PORT" "$NOVNC_PORT"; do
  if [[ ! "$port_value" =~ ^[0-9]+$ ]] || ((port_value < 1 || port_value > 65535)); then
    echo "Các cổng server phải là số trong khoảng 1..65535." >&2
    exit 1
  fi
done
if [[ ! -f "$NOVNC_WEB_DIR/vnc.html" ]]; then
  echo "Không tìm thấy noVNC tại $NOVNC_WEB_DIR/vnc.html." >&2
  exit 1
fi
if [[ -n "$VNC_PASSWORD_FILE" && ! -r "$VNC_PASSWORD_FILE" ]]; then
  echo "Không đọc được file mật khẩu VNC: $VNC_PASSWORD_FILE" >&2
  exit 1
fi

mkdir -p "$RUNTIME_DIR"
declare -a CHILD_PIDS=()

cleanup() {
  local exit_code=$?
  trap - EXIT INT TERM
  for ((index=${#CHILD_PIDS[@]} - 1; index >= 0; index--)); do
    kill "${CHILD_PIDS[$index]}" 2>/dev/null || true
  done
  for child_pid in "${CHILD_PIDS[@]}"; do
    wait "$child_pid" 2>/dev/null || true
  done
  exit "$exit_code"
}
trap cleanup EXIT INT TERM

Xvfb "$DISPLAY_VALUE" -screen 0 "$SCREEN_VALUE" -nolisten tcp \
  >"$RUNTIME_DIR/xvfb.log" 2>&1 &
CHILD_PIDS+=("$!")

display_ready=false
for _ in {1..50}; do
  if xdpyinfo -display "$DISPLAY_VALUE" >/dev/null 2>&1; then
    display_ready=true
    break
  fi
  if ! kill -0 "${CHILD_PIDS[0]}" 2>/dev/null; then
    break
  fi
  sleep 0.1
done
if [[ "$display_ready" != true ]]; then
  echo "Xvfb không khởi động được. Xem $RUNTIME_DIR/xvfb.log." >&2
  exit 1
fi

export DISPLAY="$DISPLAY_VALUE"
export ZALO_NOVNC_URL="${ZALO_NOVNC_URL:-http://127.0.0.1:$NOVNC_PORT/vnc.html?autoconnect=true&resize=scale}"
if command -v fluxbox >/dev/null 2>&1; then
  fluxbox >"$RUNTIME_DIR/fluxbox.log" 2>&1 &
  CHILD_PIDS+=("$!")
else
  echo "Không có Fluxbox; tiếp tục với Xvfb mà không có window manager."
fi

declare -a VNC_AUTH_ARGS=(-nopw)
if [[ -n "$VNC_PASSWORD_FILE" ]]; then
  VNC_AUTH_ARGS=(-rfbauth "$VNC_PASSWORD_FILE")
fi
x11vnc -display "$DISPLAY_VALUE" -listen 127.0.0.1 -rfbport "$VNC_PORT" \
  -forever -shared -no6 "${VNC_AUTH_ARGS[@]}" -noxdamage -o "$RUNTIME_DIR/x11vnc.log" &
CHILD_PIDS+=("$!")
websockify --web "$NOVNC_WEB_DIR" "127.0.0.1:$NOVNC_PORT" \
  "127.0.0.1:$VNC_PORT" >"$RUNTIME_DIR/novnc.log" 2>&1 &
CHILD_PIDS+=("$!")

echo "Web tool (trên server): http://127.0.0.1:$UI_PORT/"
echo "noVNC (trên server):    http://127.0.0.1:$NOVNC_PORT/vnc.html"
echo "Từ client, dùng SSH tunnel theo README; không mở trực tiếp cổng VNC/CDP."

cd "$PROJECT_DIR"
"$VENV_BIN/zalo-order-crawler" ui --no-open --port "$UI_PORT"
