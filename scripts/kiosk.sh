#!/usr/bin/env bash
#
# WallCal kiosk launcher.
#
# Started from the desktop session's autostart. Because Raspberry Pi OS has
# several possible autostart mechanisms (XDG, labwc, wayfire, LXDE) and the
# installer wires up every one it finds, this script takes a lock so only the
# first invocation actually launches a browser.
#
# It waits for the web app, prepares the session (no screen blanking, no mouse
# pointer, optional rotation), then keeps Chromium alive in a supervision loop.
#
set -Eeuo pipefail

APP_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
LOCK_FILE="${XDG_RUNTIME_DIR:-/tmp}/wallcal-kiosk.lock"
LOG_TAG="wallcal-kiosk"

PORT="${WALLCAL_PORT:-5005}"
URL="${WALLCAL_KIOSK_URL:-http://localhost:${PORT}/}"
PROFILE_DIR="${WALLCAL_KIOSK_PROFILE:-$HOME/.config/wallcal-kiosk}"
STARTUP_TIMEOUT="${WALLCAL_KIOSK_TIMEOUT:-90}"

log() { printf '%s [%s] %s\n' "$(date '+%F %T')" "$LOG_TAG" "$*"; }

# Chromium's GPU path on a Pi 3 and older frequently brings up a window that
# never paints — a white screen with only a cursor. Those boards have no GPU
# headroom worth fighting for, and software rendering repaints a calendar
# faster than anyone will notice, so default them to "off".
auto_gpu_mode() {
  local model=""
  # Guard the redirect: the shell reports a missing file itself, before any
  # 2>/dev/null on the command could suppress it.
  if [[ -r /proc/device-tree/model ]]; then
    model="$(tr -d '\0' </proc/device-tree/model || true)"
  fi
  case "$model" in
    *"Raspberry Pi 5"*|*"Raspberry Pi 4"*|*"Compute Module 5"*|*"Compute Module 4"*)
      printf 'default' ;;
    *"Raspberry Pi"*)
      printf 'off' ;;
    *)
      printf 'default' ;;
  esac
}

# Let other tools ask what "auto" resolves to here without launching anything.
if [[ "${1:-}" == "--print-gpu-mode" ]]; then
  auto_gpu_mode
  printf '\n'
  exit 0
fi

# --- single instance --------------------------------------------------------

exec 9>"$LOCK_FILE" 2>/dev/null || exec 9>"/tmp/wallcal-kiosk.lock"
if ! flock -n 9; then
  log "another kiosk instance already holds $LOCK_FILE — exiting"
  exit 0
fi

# --- helpers ----------------------------------------------------------------

have() { command -v "$1" >/dev/null 2>&1; }

find_browser() {
  local candidate
  for candidate in chromium-browser chromium google-chrome-stable google-chrome; do
    if have "$candidate"; then printf '%s' "$candidate"; return 0; fi
  done
  return 1
}

# Read one setting from the running app. Falls back to $2 if anything fails.
setting() {
  local key="$1" fallback="${2:-}"
  local json
  json="$(curl -fsS --max-time 4 "http://localhost:${PORT}/api/settings" 2>/dev/null)" || {
    printf '%s' "$fallback"; return 0
  }
  printf '%s' "$json" | python3 -c '
import json, sys
try:
    print(json.load(sys.stdin).get(sys.argv[1], sys.argv[2]))
except Exception:
    print(sys.argv[2])
' "$key" "$fallback" 2>/dev/null || printf '%s' "$fallback"
}

wait_for_app() {
  local deadline=$(( SECONDS + STARTUP_TIMEOUT ))
  log "waiting for $URL"
  while (( SECONDS < deadline )); do
    if curl -fsS --max-time 3 -o /dev/null "http://localhost:${PORT}/api/status"; then
      log "web app is up"
      return 0
    fi
    sleep 2
  done
  log "web app did not come up within ${STARTUP_TIMEOUT}s — starting anyway"
  return 0
}

prepare_session() {
  # Applies to the browser's own pointer rendering; the compositor picks the
  # same theme up from its session environment (set by the installer).
  if [[ -d "$HOME/.icons/wallcal-blank" ]]; then
    export XCURSOR_THEME=wallcal-blank
  fi

  if [[ -n "${WAYLAND_DISPLAY:-}" ]]; then
    log "wayland session (${WAYLAND_DISPLAY})"
  elif [[ -n "${DISPLAY:-}" ]]; then
    log "x11 session (${DISPLAY})"
    # WallCal owns the display power state; stop X from blanking on its own,
    # but keep DPMS itself enabled so the presence daemon can force it off.
    if have xset; then
      xset s off       || true
      xset s noblank   || true
      xset +dpms       || true
      xset dpms 0 0 0  || true
    fi
    # Hide the mouse pointer on an untouched wall display.
    if have unclutter && ! pgrep -u "$USER" -x unclutter >/dev/null 2>&1; then
      unclutter -idle 0 -root >/dev/null 2>&1 &
    fi
  else
    log "no graphical session detected — cannot start the kiosk"
    exit 1
  fi
}

apply_rotation() {
  local rotate
  rotate="$(setting display_rotate normal)"
  [[ -z "$rotate" || "$rotate" == "normal" ]] && return 0
  log "applying rotation: $rotate"
  "$APP_DIR/wallcal.sh" display rotate "$rotate" >/dev/null 2>&1 || true
}

# Chromium remembers that it was killed and offers to restore tabs, which on a
# wall display means a dialog nobody will ever click away.
sanitize_profile() {
  local prefs="$PROFILE_DIR/Default/Preferences"
  [[ -f "$prefs" ]] || return 0
  sed -i \
    -e 's/"exit_type":"[^"]*"/"exit_type":"Normal"/' \
    -e 's/"exited_cleanly":false/"exited_cleanly":true/' \
    "$prefs" 2>/dev/null || true
}

# --- launch -----------------------------------------------------------------

BROWSER="$(find_browser)" || {
  log "no Chromium/Chrome binary found — install chromium-browser"
  exit 1
}

prepare_session
wait_for_app
apply_rotation
mkdir -p "$PROFILE_DIR"

FLAGS=(
  --kiosk
  --user-data-dir="$PROFILE_DIR"
  --start-fullscreen
  --noerrdialogs
  --disable-infobars
  --disable-session-crashed-bubble
  --hide-crash-restore-bubble
  --no-first-run
  --no-default-browser-check
  --disable-component-update
  --check-for-update-interval=31536000
  --disable-pinch
  --overscroll-history-navigation=0
  --password-store=basic
  --autoplay-policy=no-user-gesture-required
  --disable-background-timer-throttling
  --disable-renderer-backgrounding
  --disable-backgrounding-occluded-windows
  --disable-smooth-scrolling
  --force-device-scale-factor="${WALLCAL_KIOSK_SCALE:-1}"
  # Chromium honours only the LAST --disable-features it is given, so every
  # feature has to be in this one list.
  --disable-features=Translate,TranslateUI,MediaRouter,OptimizationHints,CalculateNativeWinOcclusion
)

GPU_MODE="${WALLCAL_KIOSK_GPU:-$(setting kiosk_gpu auto)}"
if [[ "$GPU_MODE" == "auto" ]]; then
  GPU_MODE="$(auto_gpu_mode)"
  log "GPU mode: auto -> $GPU_MODE"
fi
case "$GPU_MODE" in
  off)
    log "GPU disabled (software rendering)"
    FLAGS+=( --disable-gpu --disable-gpu-compositing --disable-accelerated-2d-canvas )
    ;;
  on)
    log "GPU forced on"
    FLAGS+=( --ignore-gpu-blocklist --enable-gpu-rasterization )
    ;;
  *)
    log "GPU: leaving Chromium's own decision alone"
    ;;
esac

if [[ -n "${WAYLAND_DISPLAY:-}" && "$BROWSER" == "chromium" ]]; then
  FLAGS+=( --ozone-platform=wayland )
fi

if [[ -n "${WALLCAL_KIOSK_EXTRA_FLAGS:-}" ]]; then
  # shellcheck disable=SC2206
  FLAGS+=( ${WALLCAL_KIOSK_EXTRA_FLAGS} )
fi

# The URL goes last as a positional argument. Combining --kiosk with --app=
# is a known way to get an app window that never enters fullscreen properly.
FLAGS+=( "$URL" )

log "launching $BROWSER -> $URL"

# Supervision loop: if Chromium is killed or crashes, bring it straight back.
# Back off a little if it dies repeatedly so we do not spin the CPU.
failures=0
while true; do
  sanitize_profile
  set +e
  "$BROWSER" "${FLAGS[@]}" >/dev/null 2>&1
  rc=$?
  set -e

  if [[ "${WALLCAL_KIOSK_ONCE:-0}" == "1" ]]; then
    log "browser exited (rc=$rc); WALLCAL_KIOSK_ONCE set — not restarting"
    exit "$rc"
  fi

  failures=$(( failures + 1 ))
  delay=$(( failures > 5 ? 30 : 5 ))
  log "browser exited (rc=$rc) — restarting in ${delay}s"
  sleep "$delay"
  (( failures > 20 )) && failures=10
done
