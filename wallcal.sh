#!/usr/bin/env bash
#
#  ██╗    ██╗ █████╗ ██╗     ██╗      ██████╗ █████╗ ██╗
#  ██║    ██║██╔══██╗██║     ██║     ██╔════╝██╔══██╗██║
#  ██║ █╗ ██║███████║██║     ██║     ██║     ███████║██║
#  ██║███╗██║██╔══██║██║     ██║     ██║     ██╔══██║██║
#  ╚███╔███╔╝██║  ██║███████╗███████╗╚██████╗██║  ██║███████╗
#   ╚══╝╚══╝ ╚═╝  ╚═╝╚══════╝╚══════╝ ╚═════╝╚═╝  ╚═╝╚══════╝
#
#  Presence-aware wall calendar for the Raspberry Pi.
#  Single point of entry: install, service control, kiosk, sensor, display.
#
#  Usage:  ./wallcal.sh <command> [options]
#          ./wallcal.sh help
#
set -Eeuo pipefail

WALLCAL_VERSION="1.0.0"
APP_NAME="WallCal"

# ===========================================================================
#  Paths and identity
# ===========================================================================

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
APP_DIR="$(dirname "$SCRIPT_PATH")"
VENV_DIR="${WALLCAL_VENV:-$APP_DIR/.venv}"
VENV_PY="$VENV_DIR/bin/python"
DATA_DIR="${WALLCAL_DATA_DIR:-$APP_DIR/data}"
BACKUP_DIR="$DATA_DIR/backups"
INSTALL_LOG="$DATA_DIR/install.log"

SERVICE_WEB="wallcal.service"
SERVICE_PRESENCE="wallcal-presence.service"
SERVICE_WATCHDOG="wallcal-watchdog"
SERVICE_MAINT="wallcal-maintenance"
SYSTEMD_DIR="${WALLCAL_SYSTEMD_DIR:-/etc/systemd/system}"

BLOCK_BEGIN="# >>> WallCal begin >>>"
BLOCK_END="# <<< WallCal end <<<"

# The desktop user WallCal runs as: whoever owns the checkout, unless told
# otherwise. Running the installer under sudo must not turn this into root.
if [[ -n "${WALLCAL_USER:-}" ]]; then
  RUN_USER="$WALLCAL_USER"
elif [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]]; then
  RUN_USER="$SUDO_USER"
else
  RUN_USER="$(stat -c '%U' "$APP_DIR" 2>/dev/null || id -un)"
  [[ "$RUN_USER" == "root" || -z "$RUN_USER" ]] && RUN_USER="$(id -un)"
fi
RUN_HOME="$(getent passwd "$RUN_USER" 2>/dev/null | cut -d: -f6 || true)"
RUN_HOME="${RUN_HOME:-$HOME}"
# Keep these single-token: they are interpolated into systemd unit files,
# where a stray newline would silently corrupt the directive.
first_token() { head -n1 | awk '{print $1}'; }
RUN_UID="$(id -u "$RUN_USER" 2>/dev/null | first_token || true)"
RUN_UID="${RUN_UID:-$(id -u 2>/dev/null | first_token || echo 1000)}"
RUN_GROUP="$(id -gn "$RUN_USER" 2>/dev/null | first_token || true)"
RUN_GROUP="${RUN_GROUP:-$RUN_USER}"

KIOSK_LOG="$RUN_HOME/.local/state/wallcal/kiosk.log"
KIOSK_DESKTOP="$RUN_HOME/.config/autostart/wallcal-kiosk.desktop"

# Global option defaults (may be changed by the flag parser).
ASSUME_YES=0
DRY_RUN=0
QUIET=0
VERBOSE=0
JSON_OUT=0
USE_COLOR=1

# ===========================================================================
#  Output helpers
# ===========================================================================

setup_colors() {
  if (( USE_COLOR )) && [[ -t 1 ]] && [[ -z "${NO_COLOR:-}" ]]; then
    C_RESET=$'\033[0m'; C_BOLD=$'\033[1m';  C_DIM=$'\033[2m'
    C_RED=$'\033[31m';  C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'
    C_BLUE=$'\033[34m'; C_CYAN=$'\033[36m';  C_GREY=$'\033[90m'
  else
    C_RESET=''; C_BOLD=''; C_DIM=''; C_RED=''; C_GREEN=''
    C_YELLOW=''; C_BLUE=''; C_CYAN=''; C_GREY=''
  fi
}
setup_colors

say()   { (( QUIET )) || printf '%s\n' "$*"; }
info()  { (( QUIET )) || printf '%s\n' "${C_CYAN}::${C_RESET} $*"; }
ok()    { (( QUIET )) || printf '%s\n' "${C_GREEN}  ✔${C_RESET} $*"; }
warn()  { printf '%s\n' "${C_YELLOW}  ▲${C_RESET} $*" >&2; }
err()   { printf '%s\n' "${C_RED}  ✖${C_RESET} $*" >&2; }
dbg()   { (( VERBOSE )) && printf '%s\n' "${C_GREY}  · $*${C_RESET}" >&2 || true; }
die()   { err "$*"; exit 1; }

hr() { (( QUIET )) || printf '%s\n' "${C_GREY}$(printf '─%.0s' $(seq 1 "${1:-64}"))${C_RESET}"; }

title() {
  (( QUIET )) && return 0
  printf '\n%s\n' "${C_BOLD}$*${C_RESET}"
  hr "${#1}"
}

banner() {
  (( QUIET )) && return 0
  printf '%s\n' "${C_CYAN}${C_BOLD}"
  cat <<'ART'
  ╦ ╦┌─┐┬  ┬  ╔═╗┌─┐┬
  ║║║├─┤│  │  ║  ├─┤│
  ╚╩╝┴ ┴┴─┘┴─┘╚═╝┴ ┴┴─┘
ART
  printf '%s  presence-aware wall calendar  ·  v%s\n\n' "${C_RESET}${C_GREY}" "$WALLCAL_VERSION"
  printf '%s' "${C_RESET}"
}

kv() { (( QUIET )) || printf '  %-22s %s\n' "$1" "$2"; }

confirm() {
  local prompt="$1"
  (( ASSUME_YES )) && return 0
  if [[ ! -t 0 ]]; then
    warn "not a terminal and --yes was not given — assuming no for: $prompt"
    return 1
  fi
  local reply
  read -r -p "${C_YELLOW}?${C_RESET} $prompt [y/N] " reply
  [[ "$reply" =~ ^[Yy]([Ee][Ss])?$ ]]
}

_ERROR_REPORTED=0
on_error() {
  local exit_code=$? line=${1:-?}
  # set -E propagates the trap into functions, so an error deep in a call
  # chain fires it at every level. Only the innermost report is useful.
  if (( _ERROR_REPORTED )); then
    exit "$exit_code"
  fi
  _ERROR_REPORTED=1
  err "failed at line $line (exit $exit_code)"
  # Anything routed through run()/as_user()/as_root() reports the helper's own
  # line, which is identical for every command in the script and says nothing
  # about what actually failed. Walk the call stack out to the real caller.
  local i
  for (( i = 1; i < ${#FUNCNAME[@]} - 1; i++ )); do
    err "    in ${FUNCNAME[i]}() called at line ${BASH_LINENO[i-1]}"
  done
  [[ -f "$INSTALL_LOG" ]] && err "recent log: tail -n 40 $INSTALL_LOG"
  exit "$exit_code"
}
trap 'on_error $LINENO' ERR

# ===========================================================================
#  Execution helpers
# ===========================================================================

have() { command -v "$1" >/dev/null 2>&1; }

run() {
  if (( DRY_RUN )); then
    printf '%s\n' "${C_GREY}  [dry-run] $*${C_RESET}"
    return 0
  fi
  dbg "$*"
  "$@"
}

as_root() {
  if (( DRY_RUN )); then
    printf '%s\n' "${C_GREY}  [dry-run] sudo $*${C_RESET}"
    return 0
  fi
  if [[ $EUID -eq 0 ]]; then
    run "$@"
  elif have sudo; then
    run sudo "$@"
  else
    die "this needs root and sudo is not available: $*"
  fi
}

as_user() {
  if [[ "$(id -un)" == "$RUN_USER" ]]; then
    run "$@"
  else
    run sudo -u "$RUN_USER" "$@"
  fi
}

# write_root_file <path> [mode] — content on stdin
write_root_file() {
  local path="$1" mode="${2:-0644}"
  if (( DRY_RUN )); then
    printf '%s\n' "${C_GREY}  [dry-run] write $path${C_RESET}"
    cat >/dev/null
    return 0
  fi
  if [[ $EUID -eq 0 ]]; then
    cat >"$path"
  else
    sudo tee "$path" >/dev/null
  fi
  # /boot/firmware is vfat, where chmod is a no-op that reports failure.
  as_root chmod "$mode" "$path" 2>/dev/null || true
}

# write_user_file <path> [mode] — content on stdin, owned by RUN_USER
write_user_file() {
  local path="$1" mode="${2:-0644}"
  if (( DRY_RUN )); then
    printf '%s\n' "${C_GREY}  [dry-run] write $path${C_RESET}"
    cat >/dev/null
    return 0
  fi
  local dir; dir="$(dirname "$path")"
  as_user mkdir -p "$dir"
  if [[ "$(id -un)" == "$RUN_USER" ]]; then
    cat >"$path"
  else
    sudo -u "$RUN_USER" tee "$path" >/dev/null
  fi
  as_user chmod "$mode" "$path"
}

ensure_user_line() {
  local file="$1" line="$2"
  as_user mkdir -p "$(dirname "$file")"
  if [[ -f "$file" ]] && grep -qxF "$line" "$file" 2>/dev/null; then
    return 0
  fi
  if (( DRY_RUN )); then
    printf '%s\n' "${C_GREY}  [dry-run] append to $file: $line${C_RESET}"
    return 0
  fi
  if [[ "$(id -un)" == "$RUN_USER" ]]; then
    printf '%s\n' "$line" >>"$file"
  else
    printf '%s\n' "$line" | sudo -u "$RUN_USER" tee -a "$file" >/dev/null
  fi
}

remove_user_lines() {
  local file="$1" pattern="$2"
  [[ -f "$file" ]] || return 0
  if (( DRY_RUN )); then
    printf '%s\n' "${C_GREY}  [dry-run] strip '$pattern' from $file${C_RESET}"
    return 0
  fi
  local tmp; tmp="$(mktemp)"
  grep -v "$pattern" "$file" >"$tmp" 2>/dev/null || true
  write_user_file "$file" <"$tmp"
  rm -f "$tmp"
}

# PYTHONPATH rather than a "cd" subshell: it works from any directory, and a
# subshell would swallow the error-trap bookkeeping below.
py() {
  local interpreter
  if [[ -x "$VENV_PY" ]]; then
    interpreter="$VENV_PY"
  elif have python3; then
    interpreter="python3"
  else
    die "no Python interpreter found — run: ./wallcal.sh install"
  fi
  PYTHONPATH="$APP_DIR${PYTHONPATH:+:$PYTHONPATH}" "$interpreter" "$@"
}

# Run the presence CLI, forwarding all arguments. It prints its own
# diagnostics, which are more useful than the generic trap message, so mark
# any failure as already reported.
pcli() {
  local rc=0
  py -m presence.cli "$@" || rc=$?
  (( rc != 0 )) && _ERROR_REPORTED=1
  return "$rc"
}

# Run Python as the desktop user. Display probing reads that user's session
# (its Wayland socket, its X authority, its compositor process). As root it
# finds none of that and confidently reports the wrong backend — so anything
# that touches the display must go through here, not py().
py_user() {
  local interpreter="python3"
  [[ -x "$VENV_PY" ]] && interpreter="$VENV_PY"
  if [[ $EUID -eq 0 && "$RUN_USER" != "root" ]] && have sudo; then
    sudo -u "$RUN_USER" env "PYTHONPATH=$APP_DIR" "$interpreter" "$@"
  else
    py "$@"
  fi
}

# Ask config.py where the database lives, so WALLCAL_DB_PATH is honoured.
# Memoised — resolving it costs a Python startup.
_DB_PATH_CACHE=""
db_path() {
  if [[ -z "$_DB_PATH_CACHE" ]]; then
    _DB_PATH_CACHE="$(py -c 'import config; print(config.DATABASE_PATH)' 2>/dev/null || true)"
    [[ -n "$_DB_PATH_CACHE" ]] || _DB_PATH_CACHE="$DATA_DIR/wallcal.db"
  fi
  printf '%s' "$_DB_PATH_CACHE"
}

# ===========================================================================
#  Platform detection
# ===========================================================================

pi_model() {
  if [[ -r /proc/device-tree/model ]]; then
    tr -d '\0' </proc/device-tree/model
  else
    echo "unknown"
  fi
}

is_raspberry_pi() { [[ "$(pi_model)" == *"Raspberry Pi"* ]]; }

os_pretty() {
  if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    ( . /etc/os-release; echo "${PRETTY_NAME:-unknown}" )
  else
    uname -sr
  fi
}

boot_config_path() {
  local candidate
  for candidate in /boot/firmware/config.txt /boot/config.txt; do
    [[ -f "$candidate" ]] && { printf '%s' "$candidate"; return 0; }
  done
  return 1
}

boot_cmdline_path() {
  local candidate
  for candidate in /boot/firmware/cmdline.txt /boot/cmdline.txt; do
    [[ -f "$candidate" ]] && { printf '%s' "$candidate"; return 0; }
  done
  return 1
}

lan_ip() {
  local address=""
  if have ip; then
    address="$(ip -4 route get 1.1.1.1 2>/dev/null \
      | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}' || true)"
  fi
  if [[ -z "$address" ]] && have hostname; then
    address="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
  fi
  printf '%s' "$address"
}

app_port() {
  local port="${WALLCAL_PORT:-}"
  if [[ -z "$port" && -f "$APP_DIR/.env" ]]; then
    port="$(sed -n 's/^WALLCAL_PORT=//p' "$APP_DIR/.env" | tail -n1)"
  fi
  printf '%s' "${port:-5005}"
}

app_url() {
  local host; host="$(lan_ip)"
  [[ -n "$host" ]] || host="$(hostname).local"
  printf 'http://%s:%s/' "$host" "$(app_port)"
}

svc_state() {
  local unit="$1"
  if [[ ! -f "$SYSTEMD_DIR/$unit" && ! -f "$SYSTEMD_DIR/$unit.service" ]]; then
    printf 'not-installed'; return 0
  fi
  systemctl is-active "$unit" 2>/dev/null || true
}

svc_enabled() { systemctl is-enabled "$1" 2>/dev/null || echo "disabled"; }

state_colour() {
  case "$1" in
    active)         printf '%s' "${C_GREEN}$1${C_RESET}" ;;
    inactive|dead)  printf '%s' "${C_GREY}$1${C_RESET}" ;;
    not-installed)  printf '%s' "${C_GREY}$1${C_RESET}" ;;
    *)              printf '%s' "${C_RED}$1${C_RESET}" ;;
  esac
}

# ===========================================================================
#  Install steps
# ===========================================================================

INSTALL_STEPS=(preflight packages venv database groups uart services kiosk display autologin tuning)
INSTALL_ONLY=""
INSTALL_SKIP=""
KEEP_BLUETOOTH=0
NEEDS_REBOOT=0

step_enabled() {
  local step="$1"
  if [[ -n "$INSTALL_ONLY" ]]; then
    [[ ",$INSTALL_ONLY," == *",$step,"* ]] && return 0 || return 1
  fi
  [[ ",$INSTALL_SKIP," == *",$step,"* ]] && return 1
  return 0
}

step_header() { info "${C_BOLD}$1${C_RESET}"; }

#: Optional packages apt could not provide. Surfaced in the install summary —
#: a warning that scrolls past mid-install is a warning nobody reads.
UNAVAILABLE_PACKAGES=()

apt_install() {
  local kind="$1"; shift
  local pkg missing=()
  for pkg in "$@"; do
    dpkg -s "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
  done
  if (( ${#missing[@]} == 0 )); then
    ok "packages already present (${#@})"
    return 0
  fi
  for pkg in "${missing[@]}"; do
    if as_root apt-get install -y --no-install-recommends "$pkg" >>"$INSTALL_LOG" 2>&1; then
      ok "installed $pkg"
    elif [[ "$kind" == required ]]; then
      die "could not install required package '$pkg' (see $INSTALL_LOG)"
    else
      warn "optional package '$pkg' unavailable on this release — skipping"
      UNAVAILABLE_PACKAGES+=("$pkg")
    fi
  done
}

step_preflight() {
  step_header "Preflight"
  mkdir -p "$DATA_DIR" "$BACKUP_DIR"
  : >>"$INSTALL_LOG"

  kv "app directory" "$APP_DIR"
  kv "run as user" "$RUN_USER (uid $RUN_UID)"
  kv "model" "$(pi_model)"
  kv "os" "$(os_pretty)"

  if ! is_raspberry_pi; then
    warn "this does not look like a Raspberry Pi — hardware steps may not apply"
    confirm "Continue anyway?" || die "aborted"
  fi

  have python3 || die "python3 is missing — install it first"
  local pyver; pyver="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
  kv "python" "$pyver"
  case "$pyver" in
    3.[0-8]) die "Python 3.9 or newer is required (found $pyver)" ;;
  esac

  local free_mb; free_mb="$(df -Pm "$APP_DIR" | awk 'NR==2{print $4}')"
  if (( free_mb < 500 )); then
    warn "only ${free_mb} MB free on this filesystem"
  else
    ok "${free_mb} MB free"
  fi

  # Windows-edited checkouts are the classic cause of "bad interpreter: ^M".
  normalize_line_endings
}

normalize_line_endings() {
  local file changed=0
  while IFS= read -r file; do
    if grep -qU $'\r' "$file" 2>/dev/null; then
      (( DRY_RUN )) || sed -i 's/\r$//' "$file"
      changed=1
      dbg "normalised CRLF in $file"
    fi
  done < <(find "$APP_DIR" -maxdepth 2 \( -name '*.sh' -o -name '*.py' \) \
             -not -path "$VENV_DIR/*" 2>/dev/null)
  (( changed )) && ok "normalised Windows line endings" || true
}

step_packages() {
  step_header "System packages"
  info "apt-get update (this takes a minute on a Pi 3)"
  as_root apt-get update >>"$INSTALL_LOG" 2>&1 || warn "apt-get update reported problems"

  apt_install required \
    python3-venv python3-pip python3-dev curl ca-certificates

  # Sensor + GPIO. Pi OS ships these system-wide; the venv is created with
  # --system-site-packages so they are importable from it.
  apt_install optional \
    python3-serial python3-lgpio python3-gpiozero python3-rpi.gpio

  # Kiosk browser and session tools.
  apt_install optional \
    chromium-browser unclutter x11-xserver-utils fonts-dejavu-core

  # Display power control. These matter more than the label "optional"
  # suggests: without one of them the daemon falls back to vcgencmd, which
  # powers the HDMI link down behind the compositor's back and leaves the
  # panel showing a blank frame when it wakes. Availability varies by release,
  # so a miss is not fatal — but it is reported at the end of the install.
  apt_install optional wlr-randr wlopm
  if have wlr-randr || have wlopm; then
    ok "Wayland display power control available"
  elif have xset; then
    ok "X11 DPMS available via xset"
  else
    warn "no DPMS-capable display tool — the panel may wake to a blank frame"
  fi

  apt_install optional v4l-utils libraspberrypi-bin

  # Housekeeping and diagnostics.
  apt_install optional \
    sqlite3 jq git rsync avahi-daemon

  if ! have chromium-browser && ! have chromium; then
    warn "no Chromium found — kiosk mode will not start until one is installed"
  fi
}

step_venv() {
  step_header "Python environment"
  if [[ ! -x "$VENV_PY" ]]; then
    # --system-site-packages so RPi.GPIO / gpiozero / lgpio from apt are usable.
    as_user python3 -m venv --system-site-packages "$VENV_DIR"
    ok "created virtualenv at $VENV_DIR"
  else
    ok "virtualenv already present"
  fi
  as_user "$VENV_DIR/bin/pip" install --upgrade pip wheel >>"$INSTALL_LOG" 2>&1 \
    || warn "pip self-upgrade failed — continuing"
  info "installing Python dependencies"
  as_user "$VENV_DIR/bin/pip" install -r "$APP_DIR/requirements.txt" >>"$INSTALL_LOG" 2>&1 \
    || die "dependency install failed (see $INSTALL_LOG)"
  ok "dependencies installed"
}

step_database() {
  step_header "Database"
  as_user mkdir -p "$DATA_DIR" "$BACKUP_DIR"
  py -c "import database; database.init_db(); print('ok')" >>"$INSTALL_LOG" 2>&1 \
    || die "database initialisation failed (see $INSTALL_LOG)"
  ok "schema ready at $DATA_DIR"

  # A random secret means CalDAV passwords are not encrypted with the
  # published default key.
  if [[ ! -f "$APP_DIR/.env" ]]; then
    local secret; secret="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    write_user_file "$APP_DIR/.env" 0600 <<EOF
# WallCal environment overrides — read by the systemd units.
WALLCAL_SECRET_KEY=$secret
WALLCAL_PORT=5005
WALLCAL_LOG_LEVEL=INFO
EOF
    ok "generated .env with a unique secret key"
  else
    ok ".env already exists — leaving it alone"
  fi
}

step_groups() {
  step_header "Permissions"
  local group existing=()
  for group in dialout gpio video render input tty plugdev; do
    getent group "$group" >/dev/null 2>&1 && existing+=("$group")
  done
  local needed=() current
  current="$(id -nG "$RUN_USER" 2>/dev/null || echo "")"
  for group in "${existing[@]}"; do
    [[ " $current " == *" $group "* ]] || needed+=("$group")
  done
  if (( ${#needed[@]} )); then
    as_root usermod -aG "$(IFS=,; echo "${needed[*]}")" "$RUN_USER"
    ok "added $RUN_USER to: ${needed[*]}"
    NEEDS_REBOOT=1
  else
    ok "$RUN_USER already in all required groups"
  fi
}

# The backlight overlay belongs inside the same managed block as the UART one,
# because step_uart regenerates that block wholesale. Written anywhere else it
# would survive, but be orphaned from the block that owns it and lost the next
# time somebody runs 'install --only uart'.
boot_block_pwm() {
  local line
  line="$(py_user -c "
import database
from presence import pwm
s = database.get_all_settings()
strategy = [p.strip().lower() for p in str(s.get('display_off_strategy','hdmi')).split(',')]
if 'pwm' in strategy:
    try:
        print(pwm.overlay_line(int(s.get('pwm_gpio', 18))))
    except Exception:
        pass
" 2>/dev/null || true)"
  [[ -n "$line" ]] || return 0
  printf '\n# Hardware PWM for the panel backlight (BL_PWM)\n'
  printf '%s\n' "$line"
}

step_uart() {
  step_header "Serial UART for the presence sensor"
  local config_txt cmdline_txt
  if ! config_txt="$(boot_config_path)"; then
    warn "no config.txt found — skipping (not a Raspberry Pi boot layout?)"
    return 0
  fi
  cmdline_txt="$(boot_cmdline_path || true)"

  say
  say "  The HLK-LD2410C talks at 256000 baud. On a Pi 3B+ the pins GPIO14/15"
  say "  are wired to the *mini* UART by default, which cannot hold that rate"
  say "  reliably — the good PL011 is reserved for Bluetooth."
  say
  if (( KEEP_BLUETOOTH )); then
    say "  Chosen: keep Bluetooth (moves it to the mini UART, PL011 to the pins)."
  else
    say "  Chosen: disable the on-board Bluetooth so the PL011 serves the pins."
    say "  Pass --keep-bluetooth to swap the UARTs around instead."
  fi
  say

  if ! confirm "Apply this change to $config_txt?"; then
    warn "skipped UART setup — GPIO (OUT pin) mode will still work"
    return 0
  fi

  as_root cp -a "$config_txt" "${config_txt}.wallcal.bak"
  local overlay="disable-bt" extra=""
  if (( KEEP_BLUETOOTH )); then
    overlay="miniuart-bt"
    extra=$'core_freq=250\n'
  fi

  boot_block_remove "$config_txt"
  local staged; staged="$(mktemp)"
  {
    cat "$config_txt"
    printf '\n%s\n' "$BLOCK_BEGIN"
    printf '# Serial UART for the HLK-LD2410C presence sensor\n'
    printf 'enable_uart=1\n'
    printf 'dtoverlay=%s\n' "$overlay"
    [[ -n "$extra" ]] && printf '%s' "$extra"
    boot_block_pwm
    printf '%s\n' "$BLOCK_END"
  } >"$staged"
  write_root_file "$config_txt" <"$staged"
  rm -f "$staged"
  ok "updated $config_txt (backup: ${config_txt}.wallcal.bak)"

  if [[ -n "$cmdline_txt" ]] && grep -qE 'console=(serial0|ttyAMA0|ttyS0)' "$cmdline_txt"; then
    as_root cp -a "$cmdline_txt" "${cmdline_txt}.wallcal.bak"
    rewrite_root_file "$cmdline_txt" \
      sed -E 's/console=(serial0|ttyAMA0|ttyS0),[0-9]+ ?//g'
    ok "removed the serial console from $cmdline_txt"
  else
    ok "serial console already off"
  fi

  local unit
  for unit in serial-getty@ttyS0.service serial-getty@ttyAMA0.service; do
    systemctl list-unit-files "$unit" >/dev/null 2>&1 && \
      as_root systemctl disable --now "$unit" >>"$INSTALL_LOG" 2>&1 || true
  done
  if (( ! KEEP_BLUETOOTH )); then
    as_root systemctl disable --now hciuart.service >>"$INSTALL_LOG" 2>&1 || true
    ok "disabled hciuart (Bluetooth serial attach)"
  fi

  NEEDS_REBOOT=1
  ok "UART configured — a reboot is required before the sensor is reachable"
}

# Rewrite a root-owned file from a filter. The filtered output is staged in a
# temp file first — piping straight back into the same path would let tee
# truncate it before the reader finished.
rewrite_root_file() {
  local file="$1"; shift
  local tmp; tmp="$(mktemp)"
  "$@" <"$file" >"$tmp"
  write_root_file "$file" <"$tmp"
  rm -f "$tmp"
}

boot_block_remove() {
  local file="$1"
  [[ -f "$file" ]] || return 0
  grep -qF "$BLOCK_BEGIN" "$file" 2>/dev/null || return 0
  rewrite_root_file "$file" awk -v b="$BLOCK_BEGIN" -v e="$BLOCK_END" '
    $0 == b { skip = 1 }
    !skip   { print }
    $0 == e { skip = 0 }
  '
}

# Naming a group that does not exist makes the unit fail to start, so only
# emit the ones this system actually has.
supplementary_groups() {
  local group out=()
  for group in dialout gpio video render input tty; do
    getent group "$group" >/dev/null 2>&1 && out+=("$group")
  done
  (( ${#out[@]} )) && printf 'SupplementaryGroups=%s' "${out[*]}"
  return 0
}

step_services() {
  step_header "systemd services"
  local groups; groups="$(supplementary_groups)"
  # ReadWritePaths= makes the unit fail to start if the path is missing.
  as_user mkdir -p "$DATA_DIR"

  write_root_file "$SYSTEMD_DIR/$SERVICE_WEB" <<EOF
[Unit]
Description=WallCal calendar web application
Documentation=file://$APP_DIR/README.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_GROUP
WorkingDirectory=$APP_DIR
EnvironmentFile=-$APP_DIR/.env
Environment=PYTHONUNBUFFERED=1
ExecStart=$VENV_PY $APP_DIR/app.py
Restart=always
RestartSec=5
TimeoutStopSec=20
RuntimeDirectory=wallcal
RuntimeDirectoryMode=0775
RuntimeDirectoryPreserve=yes
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=full
ReadWritePaths=$DATA_DIR
StandardOutput=journal
StandardError=journal
SyslogIdentifier=wallcal

[Install]
WantedBy=multi-user.target
EOF
  ok "wrote $SERVICE_WEB"

  write_root_file "$SYSTEMD_DIR/$SERVICE_PRESENCE" <<EOF
[Unit]
Description=WallCal presence sensor and display power daemon
After=$SERVICE_WEB
Wants=$SERVICE_WEB

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_GROUP
$groups
WorkingDirectory=$APP_DIR
EnvironmentFile=-$APP_DIR/.env
Environment=PYTHONUNBUFFERED=1
Environment=XDG_RUNTIME_DIR=/run/user/$RUN_UID
ExecStart=$VENV_PY -m presence.daemon
Restart=always
RestartSec=5
TimeoutStopSec=20
RuntimeDirectory=wallcal
RuntimeDirectoryMode=0775
RuntimeDirectoryPreserve=yes
StandardOutput=journal
StandardError=journal
SyslogIdentifier=wallcal-presence

[Install]
WantedBy=multi-user.target
EOF
  ok "wrote $SERVICE_PRESENCE"

  write_root_file "$SYSTEMD_DIR/$SERVICE_WATCHDOG.service" <<EOF
[Unit]
Description=WallCal health watchdog

[Service]
Type=oneshot
User=root
ExecStart=$SCRIPT_PATH --quiet --yes watchdog
SyslogIdentifier=wallcal-watchdog
EOF

  write_root_file "$SYSTEMD_DIR/$SERVICE_WATCHDOG.timer" <<EOF
[Unit]
Description=Run the WallCal health watchdog periodically

[Timer]
OnBootSec=3min
OnUnitActiveSec=2min
AccuracySec=30s

[Install]
WantedBy=timers.target
EOF
  ok "wrote $SERVICE_WATCHDOG.timer"

  write_root_file "$SYSTEMD_DIR/$SERVICE_MAINT.service" <<EOF
[Unit]
Description=WallCal nightly maintenance (backup, vacuum, log trim)

[Service]
Type=oneshot
User=$RUN_USER
WorkingDirectory=$APP_DIR
ExecStart=$SCRIPT_PATH --quiet --yes maintain
SyslogIdentifier=wallcal-maintenance
EOF

  write_root_file "$SYSTEMD_DIR/$SERVICE_MAINT.timer" <<EOF
[Unit]
Description=Run WallCal maintenance daily

[Timer]
OnCalendar=*-*-* 04:20:00
RandomizedDelaySec=20min
Persistent=true

[Install]
WantedBy=timers.target
EOF
  ok "wrote $SERVICE_MAINT.timer"

  as_root systemctl daemon-reload
  as_root systemctl enable --now "$SERVICE_WEB" >>"$INSTALL_LOG" 2>&1
  as_root systemctl enable --now "$SERVICE_PRESENCE" >>"$INSTALL_LOG" 2>&1
  as_root systemctl enable --now "$SERVICE_WATCHDOG.timer" >>"$INSTALL_LOG" 2>&1
  as_root systemctl enable --now "$SERVICE_MAINT.timer" >>"$INSTALL_LOG" 2>&1
  ok "services enabled and started"
}

step_kiosk() {
  step_header "Kiosk mode"
  as_user chmod +x "$APP_DIR/scripts/kiosk.sh" "$SCRIPT_PATH" 2>/dev/null || true
  as_user mkdir -p "$(dirname "$KIOSK_LOG")"

  local exec_cmd="/bin/sh -c 'exec \"$APP_DIR/scripts/kiosk.sh\" >>\"$KIOSK_LOG\" 2>&1'"

  write_user_file "$KIOSK_DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=WallCal Kiosk
Comment=Full-screen calendar display
Exec=$exec_cmd
X-GNOME-Autostart-enabled=true
NoDisplay=true
EOF
  ok "XDG autostart entry: $KIOSK_DESKTOP"

  # Raspberry Pi OS has used LXDE/Openbox, Wayfire and labwc as the session
  # over the last few releases. Wire up every mechanism that exists here —
  # kiosk.sh holds a lock, so only the first one to fire actually launches.
  if [[ -d "$RUN_HOME/.config/labwc" ]] || have labwc; then
    local lab="$RUN_HOME/.config/labwc/autostart"
    # A user autostart file REPLACES /etc/xdg/labwc/autostart rather than
    # adding to it, and the system one is what starts the panel, the desktop
    # and the wallpaper. Seed from it, or the session comes up bare.
    if [[ ! -f "$lab" && -f /etc/xdg/labwc/autostart ]]; then
      as_user mkdir -p "$(dirname "$lab")"
      cat /etc/xdg/labwc/autostart | write_user_file "$lab" 0755
      ok "seeded labwc autostart from the system defaults"
    fi
    ensure_user_line "$lab" "$exec_cmd &  # wallcal"
    as_user chmod +x "$lab" 2>/dev/null || true
    ok "labwc autostart hooked"
  fi
  if [[ -f "$RUN_HOME/.config/wayfire.ini" ]]; then
    if ! grep -q '^\[autostart\]' "$RUN_HOME/.config/wayfire.ini"; then
      ensure_user_line "$RUN_HOME/.config/wayfire.ini" "[autostart]"
    fi
    ensure_user_line "$RUN_HOME/.config/wayfire.ini" "wallcal = $APP_DIR/scripts/kiosk.sh"
    ok "wayfire autostart hooked"
  fi
  # Hide the pointer. unclutter is X11-only and does nothing under labwc, but
  # every compositor honours XCURSOR_THEME — so point them at a theme whose
  # cursors are a single transparent pixel.
  if as_user python3 "$APP_DIR/scripts/blank_cursor.py" \
       --dir "$RUN_HOME/.icons" >/dev/null 2>&1; then
    # No trailing comments here: labwc parses this file as bare KEY=VALUE.
    ensure_user_line "$RUN_HOME/.config/labwc/environment" "XCURSOR_THEME=wallcal-blank"
    if [[ -f "$RUN_HOME/.config/wayfire.ini" ]]; then
      ensure_user_line "$RUN_HOME/.config/wayfire.ini" "cursor_theme = wallcal-blank"
    fi
    ok "pointer hidden via a transparent cursor theme"
  else
    warn "could not build the blank cursor theme — the pointer may stay visible"
  fi

  if [[ -d "$RUN_HOME/.config/lxsession" ]]; then
    local lx="$RUN_HOME/.config/lxsession/LXDE-pi/autostart"
    if [[ ! -f "$lx" && -f /etc/xdg/lxsession/LXDE-pi/autostart ]]; then
      as_user mkdir -p "$(dirname "$lx")"
      as_user cp /etc/xdg/lxsession/LXDE-pi/autostart "$lx"
    fi
    ensure_user_line "$lx" "@$APP_DIR/scripts/kiosk.sh  # wallcal"
    # The stock LXDE autostart blanks the screen; WallCal owns that now.
    remove_user_lines "$lx" '@xscreensaver'
    ok "LXDE autostart hooked"
  fi

  # Belt and braces: stop X from blanking on its own. Not fatal — on a
  # Wayland-only session there is nothing here to configure.
  if as_root mkdir -p /etc/X11/xorg.conf.d 2>/dev/null; then
    write_root_file "/etc/X11/xorg.conf.d/10-wallcal-blanking.conf" <<'EOF'
# WallCal controls display power itself; disable X's own timers.
Section "ServerFlags"
    Option "BlankTime"   "0"
    Option "StandbyTime" "0"
    Option "SuspendTime" "0"
    Option "OffTime"     "0"
EndSection
EOF
    ok "disabled X's automatic blanking"
  else
    warn "could not write to /etc/X11/xorg.conf.d — X may blank on its own"
  fi
}

step_display() {
  step_header "Display power backend"
  # The daemon starts at multi-user.target, before any compositor exists, so
  # its own first probe can only find the session-less backends and would
  # settle on vcgencmd. Probe now, as the desktop user, and write down the
  # right answer — the runtime re-probe is then only a safety net.
  if py_user -m presence.cli display autoselect --save; then
    return 0
  fi
  warn "leaving display_backend on 'auto'"
  say "  ${C_GREY}Re-run after the desktop is up:${C_RESET} ./wallcal.sh display autoselect"
  return 0
}

step_autologin() {
  step_header "Boot behaviour"
  if ! have raspi-config; then
    warn "raspi-config not found — set desktop autologin yourself"
    return 0
  fi
  if confirm "Boot straight into the desktop, logged in as $RUN_USER?"; then
    as_root raspi-config nonint do_boot_behaviour B4 >>"$INSTALL_LOG" 2>&1 \
      && ok "desktop autologin enabled" \
      || warn "could not set autologin via raspi-config"
    NEEDS_REBOOT=1
  else
    warn "skipped — kiosk mode needs an auto-logged-in desktop session"
  fi
}

step_tuning() {
  step_header "Quality-of-life tuning"

  # Wi-Fi power saving drops the calendar off the network for seconds at a
  # time, which shows up as stale events on a wall display.
  if have iw && iw dev wlan0 info >/dev/null 2>&1; then
    write_root_file "/etc/systemd/system/wallcal-wifi-powersave.service" <<'EOF'
[Unit]
Description=Disable Wi-Fi power saving for WallCal
After=network.target

[Service]
Type=oneshot
ExecStart=/usr/sbin/iw dev wlan0 set power_save off
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
    as_root systemctl enable --now wallcal-wifi-powersave.service >>"$INSTALL_LOG" 2>&1 || true
    ok "Wi-Fi power saving disabled"
  fi

  # Cap journald so months of uptime cannot fill the SD card.
  if as_root mkdir -p /etc/systemd/journald.conf.d 2>/dev/null; then
    write_root_file "/etc/systemd/journald.conf.d/wallcal.conf" <<'EOF'
[Journal]
SystemMaxUse=64M
RuntimeMaxUse=16M
EOF
    as_root systemctl restart systemd-journald >>"$INSTALL_LOG" 2>&1 || true
    ok "journal size capped at 64 MB"
  else
    warn "could not cap the journal size"
  fi

  # mDNS means the calendar is reachable as <hostname>.local without hunting
  # for a DHCP lease.
  if have systemctl && systemctl list-unit-files avahi-daemon.service >/dev/null 2>&1; then
    as_root systemctl enable --now avahi-daemon >>"$INSTALL_LOG" 2>&1 || true
    ok "mDNS enabled — http://$(hostname).local:$(app_port)/"
  fi
}

# The installer is meant to be run with sudo, but everything it creates inside
# the checkout has to end up owned by the desktop user: the app, the database
# and the venv all run as them, and a root-owned database is the one state
# this script works hardest to avoid. A checkout that is itself root-owned —
# cloned or copied with sudo — makes that impossible, and without this check
# the first symptom is a bare "mkdir: Permission denied" from a command that
# looks like it should obviously have the rights.
ensure_app_dir_writable() {
  if as_user test -w "$APP_DIR" 2>/dev/null; then
    return 0
  fi
  local owner group
  owner="$(stat -c '%U' "$APP_DIR" 2>/dev/null || echo 'someone else')"
  group="$(id -gn "$RUN_USER" 2>/dev/null || echo "$RUN_USER")"

  warn "$APP_DIR belongs to $owner, but WallCal runs as $RUN_USER"
  say "  The database, the virtualenv and the event cache all live in here and"
  say "  are written by $RUN_USER, so the checkout has to belong to them."
  say "  This normally means the repo was cloned or copied with sudo."
  say

  if [[ $EUID -eq 0 ]] && confirm "Change the owner of $APP_DIR to $RUN_USER?"; then
    run chown -R "$RUN_USER:$group" "$APP_DIR"
    ok "$APP_DIR now belongs to $RUN_USER"
    return 0
  fi
  die "fix it with: sudo chown -R $RUN_USER:$group $APP_DIR"
}

cmd_install() {
  banner
  title "Installing $APP_NAME"
  ensure_app_dir_writable
  as_user mkdir -p "$DATA_DIR" "$BACKUP_DIR"

  local step
  for step in "${INSTALL_STEPS[@]}"; do
    step_enabled "$step" || { dbg "skipping step: $step"; continue; }
    "step_$step"
  done

  title "Done"
  kv "web interface" "$(app_url)"
  kv "local" "http://localhost:$(app_port)/"
  kv "services" "$SERVICE_WEB, $SERVICE_PRESENCE"
  kv "logs" "./wallcal.sh logs -f"
  kv "diagnostics" "./wallcal.sh doctor"
  say

  if (( ${#UNAVAILABLE_PACKAGES[@]} )); then
    warn "apt could not provide: ${UNAVAILABLE_PACKAGES[*]}"
    local pkg
    for pkg in "${UNAVAILABLE_PACKAGES[@]}"; do
      case "$pkg" in
        wlr-randr|wlopm)
          say "  ${C_GREY}Without ${pkg}, display power falls back to vcgencmd, which can"
          say "  leave the panel blank after waking. Check for an alternative with:"
          say "  ${C_RESET}apt search wlr-randr wlopm${C_GREY}, then ./wallcal.sh presence rescan.${C_RESET}"
          ;;
      esac
    done
    say
  fi
  if (( NEEDS_REBOOT )); then
    warn "a reboot is required (boot config / group membership changed)"
    if confirm "Reboot now?"; then
      as_root reboot
    else
      say "  Run ${C_BOLD}sudo reboot${C_RESET} when convenient."
    fi
  else
    say "  ${C_GREEN}WallCal is running.${C_RESET}"
  fi
}

# ===========================================================================
#  Uninstall
# ===========================================================================

cmd_uninstall() {
  local purge=0 revert_boot=0
  while (( $# )); do
    case "$1" in
      --purge)       purge=1 ;;
      --revert-boot) revert_boot=1 ;;
      *) die "unknown option for uninstall: $1" ;;
    esac
    shift
  done

  title "Uninstalling $APP_NAME"
  confirm "Remove WallCal services, kiosk autostart and tuning?" || die "aborted"

  local unit
  for unit in "$SERVICE_WEB" "$SERVICE_PRESENCE" "$SERVICE_WATCHDOG.timer" \
              "$SERVICE_WATCHDOG.service" "$SERVICE_MAINT.timer" \
              "$SERVICE_MAINT.service" "wallcal-wifi-powersave.service"; do
    if [[ -f "$SYSTEMD_DIR/$unit" ]]; then
      as_root systemctl disable --now "$unit" >/dev/null 2>&1 || true
      as_root rm -f "$SYSTEMD_DIR/$unit"
      ok "removed $unit"
    fi
  done
  as_root systemctl daemon-reload

  as_root rm -f /etc/X11/xorg.conf.d/10-wallcal-blanking.conf \
                /etc/systemd/journald.conf.d/wallcal.conf
  as_root rm -f "$KIOSK_DESKTOP"
  remove_user_lines "$RUN_HOME/.config/labwc/autostart" 'wallcal'
  remove_user_lines "$RUN_HOME/.config/wayfire.ini" 'wallcal'
  remove_user_lines "$RUN_HOME/.config/lxsession/LXDE-pi/autostart" 'wallcal'
  ok "kiosk autostart entries removed"

  pkill -u "$RUN_USER" -f 'scripts/kiosk.sh' 2>/dev/null || true
  pkill -u "$RUN_USER" -f 'wallcal-kiosk' 2>/dev/null || true

  if (( revert_boot )); then
    local config_txt cmdline_txt
    if config_txt="$(boot_config_path)"; then
      boot_block_remove "$config_txt"
      ok "reverted $config_txt"
    fi
    if cmdline_txt="$(boot_cmdline_path)" && [[ -f "${cmdline_txt}.wallcal.bak" ]]; then
      as_root cp -a "${cmdline_txt}.wallcal.bak" "$cmdline_txt"
      ok "restored $cmdline_txt"
    fi
    as_root systemctl enable hciuart.service >/dev/null 2>&1 || true
    NEEDS_REBOOT=1
  else
    say "  ${C_GREY}Boot config left as-is; pass --revert-boot to undo the UART changes.${C_RESET}"
  fi

  # Always leave the display switched on — never strand somebody with a
  # black panel and no daemon left to wake it.
  pcli display on >/dev/null 2>&1 || true

  if (( purge )); then
    if confirm "Delete the database, backups and virtualenv? This cannot be undone."; then
      run rm -rf "$DATA_DIR" "$VENV_DIR" "$APP_DIR/.env"
      ok "application data removed"
    fi
  else
    say "  ${C_GREY}Data kept in $DATA_DIR (use --purge to delete).${C_RESET}"
  fi

  title "Uninstalled"
  (( NEEDS_REBOOT )) && warn "reboot to finish reverting the boot configuration"
  return 0
}

# ===========================================================================
#  Service control
# ===========================================================================

cmd_start() {
  as_root systemctl start "$SERVICE_WEB" "$SERVICE_PRESENCE"
  ok "started"
  cmd_status
}

cmd_stop() {
  as_root systemctl stop "$SERVICE_PRESENCE" "$SERVICE_WEB"
  # Do not leave the wall panel dark with nothing left to wake it.
  pcli display on >/dev/null 2>&1 || true
  ok "stopped (display left on)"
}

cmd_restart() {
  as_root systemctl restart "$SERVICE_WEB" "$SERVICE_PRESENCE"
  ok "restarted"
  cmd_status
}

cmd_enable()  { as_root systemctl enable "$SERVICE_WEB" "$SERVICE_PRESENCE"; ok "enabled at boot"; }
cmd_disable() { as_root systemctl disable "$SERVICE_WEB" "$SERVICE_PRESENCE"; ok "disabled at boot"; }

cmd_status() {
  if (( JSON_OUT )); then
    export WALLCAL_VERSION
    py - "$SERVICE_WEB" "$SERVICE_PRESENCE" \
      "$SERVICE_WATCHDOG.timer" "$SERVICE_MAINT.timer" <<'PY'
import json, os, subprocess, sys
from presence import runtime

def state(unit):
    try:
        out = subprocess.run(["systemctl", "is-active", unit], timeout=10,
                             capture_output=True, text=True).stdout.strip()
    except Exception:
        return "unknown"
    return out or "unknown"

print(json.dumps({
    "version": os.environ.get("WALLCAL_VERSION", "unknown"),
    "services": {unit: state(unit) for unit in sys.argv[1:]},
    "presence": runtime.read_state(),
}, indent=2, default=str))
PY
    return 0
  fi

  title "$APP_NAME status"
  kv "version" "$WALLCAL_VERSION"
  kv "directory" "$APP_DIR"
  kv "user" "$RUN_USER"
  say
  kv "web service" "$(state_colour "$(svc_state "$SERVICE_WEB")")  ($(svc_enabled "$SERVICE_WEB"))"
  kv "presence daemon" "$(state_colour "$(svc_state "$SERVICE_PRESENCE")")  ($(svc_enabled "$SERVICE_PRESENCE"))"
  kv "watchdog timer" "$(state_colour "$(svc_state "$SERVICE_WATCHDOG.timer")")"
  kv "maintenance" "$(state_colour "$(svc_state "$SERVICE_MAINT.timer")")"

  local http="unreachable"
  curl -fs --max-time 3 -o /dev/null "http://localhost:$(app_port)/api/status" \
    && http="${C_GREEN}reachable${C_RESET}" || http="${C_RED}unreachable${C_RESET}"
  kv "http" "$http  $(app_url)"

  if pgrep -u "$RUN_USER" -f 'scripts/kiosk.sh' >/dev/null 2>&1; then
    kv "kiosk" "${C_GREEN}running${C_RESET}"
  else
    kv "kiosk" "${C_GREY}not running${C_RESET}"
  fi

  say
  pcli presence state 2>/dev/null || warn "presence daemon has not published any state yet"
}

cmd_logs() {
  local follow=0 lines=100 target="all"
  while (( $# )); do
    case "$1" in
      -f|--follow) follow=1 ;;
      -n|--lines)  lines="$2"; shift ;;
      web|app)     target="web" ;;
      presence|sensor) target="presence" ;;
      kiosk)       target="kiosk" ;;
      all)         target="all" ;;
      *) die "unknown argument for logs: $1" ;;
    esac
    shift
  done

  if [[ "$target" == "kiosk" ]]; then
    [[ -f "$KIOSK_LOG" ]] || die "no kiosk log at $KIOSK_LOG"
    if (( follow )); then tail -n "$lines" -f "$KIOSK_LOG"; else tail -n "$lines" "$KIOSK_LOG"; fi
    return 0
  fi

  local args=(-n "$lines")
  (( follow )) && args+=(-f)
  case "$target" in
    web)      args+=(-u "$SERVICE_WEB") ;;
    presence) args+=(-u "$SERVICE_PRESENCE") ;;
    all)      args+=(-u "$SERVICE_WEB" -u "$SERVICE_PRESENCE"
                     -u "$SERVICE_WATCHDOG.service") ;;
  esac
  journalctl "${args[@]}"
}

cmd_update() {
  title "Updating $APP_NAME"
  # 'sudo ./wallcal.sh update' hits the same trap as install: the pull and the
  # venv rebuild both run as RUN_USER.
  ensure_app_dir_writable
  if [[ -d "$APP_DIR/.git" ]]; then
    info "pulling latest code"
    as_user git -C "$APP_DIR" pull --ff-only || warn "git pull failed — continuing with local code"
  else
    warn "not a git checkout — skipping code update"
  fi
  cmd_backup >/dev/null
  step_venv
  py -c "import database; database.init_db()" >/dev/null
  as_root systemctl daemon-reload
  as_root systemctl restart "$SERVICE_WEB" "$SERVICE_PRESENCE"
  ok "updated and restarted"
}

# ===========================================================================
#  Kiosk
# ===========================================================================

kiosk_session_env() {
  export XDG_RUNTIME_DIR="/run/user/$RUN_UID"
  local sock
  sock="$(find "$XDG_RUNTIME_DIR" -maxdepth 1 -name 'wayland-*' ! -name '*.lock' \
          2>/dev/null | head -n1 || true)"
  if [[ -n "$sock" ]]; then
    export WAYLAND_DISPLAY="$(basename "$sock")"
    unset DISPLAY
  else
    export DISPLAY="${DISPLAY:-:0}"
    if [[ -f "$RUN_HOME/.Xauthority" ]]; then
      export XAUTHORITY="$RUN_HOME/.Xauthority"
    fi
  fi
  # A missing .Xauthority is normal, not an error — do not let the last test
  # in this function decide its exit status.
  return 0
}

cmd_kiosk() {
  local action="${1:-status}"; shift || true
  case "$action" in
    start)
      pgrep -u "$RUN_USER" -f 'scripts/kiosk.sh' >/dev/null 2>&1 \
        && { ok "kiosk already running"; return 0; }
      kiosk_session_env
      mkdir -p "$(dirname "$KIOSK_LOG")"
      info "launching kiosk (log: $KIOSK_LOG)"
      setsid "$APP_DIR/scripts/kiosk.sh" >>"$KIOSK_LOG" 2>&1 &
      sleep 2
      pgrep -u "$RUN_USER" -f 'scripts/kiosk.sh' >/dev/null 2>&1 \
        && ok "kiosk started" || die "kiosk failed to start — see $KIOSK_LOG"
      ;;
    stop)
      pkill -u "$RUN_USER" -f 'scripts/kiosk.sh' 2>/dev/null || true
      pkill -u "$RUN_USER" -f 'wallcal-kiosk' 2>/dev/null || true
      ok "kiosk stopped"
      ;;
    restart)
      # kiosk.sh supervises the browser, so killing just Chromium is enough
      # and avoids a full session teardown.
      if pgrep -u "$RUN_USER" -f 'scripts/kiosk.sh' >/dev/null 2>&1; then
        pkill -u "$RUN_USER" -f 'wallcal-kiosk' 2>/dev/null || true
        ok "browser restarting"
      else
        cmd_kiosk start
      fi
      ;;
    status)
      if pgrep -u "$RUN_USER" -f 'scripts/kiosk.sh' >/dev/null 2>&1; then
        kv "supervisor" "${C_GREEN}running${C_RESET}"
      else
        kv "supervisor" "${C_GREY}stopped${C_RESET}"
      fi
      if pgrep -u "$RUN_USER" -f 'wallcal-kiosk' >/dev/null 2>&1; then
        kv "browser" "${C_GREEN}running${C_RESET}"
      else
        kv "browser" "${C_GREY}stopped${C_RESET}"
      fi
      kv "autostart" "$([[ -f "$KIOSK_DESKTOP" ]] && echo installed || echo missing)"
      kv "url" "$(app_url)"
      kv "log" "$KIOSK_LOG"
      ;;
    log|logs)
      cmd_logs kiosk "$@"
      ;;
    gpu)
      local mode="${1:?usage: kiosk gpu auto|on|off}"
      case "$mode" in
        auto|on|off) ;;
        *) die "gpu mode must be auto, on or off" ;;
      esac
      pcli settings set "kiosk_gpu=$mode"
      cmd_kiosk restart
      ;;
    diagnose|doctor)
      cmd_kiosk_diagnose
      ;;
    *)
      die "usage: wallcal.sh kiosk {start|stop|restart|status|logs|gpu|diagnose}"
      ;;
  esac
}

# Why is the panel showing the wrong thing? Everything between the web app and
# the pixels, in one place.
cmd_kiosk_diagnose() {
  title "Kiosk diagnosis"

  kiosk_session_env
  local session="none"
  [[ -n "${WAYLAND_DISPLAY:-}" ]] && session="wayland (${WAYLAND_DISPLAY})"
  [[ -z "${WAYLAND_DISPLAY:-}" && -n "${DISPLAY:-}" ]] && session="x11 (${DISPLAY})"
  kv "session" "$session"
  kv "compositor" "$(pgrep -u "$RUN_USER" -a -f '(labwc|wayfire|Xorg|openbox|mutter)' \
                     2>/dev/null | head -n1 | cut -c1-60 || echo 'none found')"

  local browser="none"
  for candidate in chromium-browser chromium google-chrome-stable; do
    have "$candidate" && { browser="$candidate"; break; }
  done
  kv "browser" "$browser"

  local gpu_setting gpu_effective
  gpu_setting="$(pcli settings get kiosk_gpu 2>/dev/null || echo '?')"
  if [[ "$gpu_setting" == "auto" ]]; then
    # Ask kiosk.sh itself rather than duplicating the board check here.
    gpu_effective="$(bash "$APP_DIR/scripts/kiosk.sh" --print-gpu-mode 2>/dev/null \
                     || echo 'default')"
    kv "gpu mode" "auto → ${gpu_effective:-default}"
  else
    kv "gpu mode" "$gpu_setting (pinned)"
  fi
  kv "profile" "$RUN_HOME/.config/wallcal-kiosk"

  # A root-owned profile silently breaks Chromium; it happens if the kiosk was
  # ever started under sudo.
  local profile="$RUN_HOME/.config/wallcal-kiosk"
  if [[ -d "$profile" ]]; then
    local owner; owner="$(stat -c '%U' "$profile" 2>/dev/null || echo '?')"
    if [[ "$owner" != "$RUN_USER" ]]; then
      err "profile directory is owned by $owner, not $RUN_USER"
      say "      ${C_CYAN}→ sudo chown -R $RUN_USER: $profile${C_RESET}"
    else
      kv "profile owner" "$owner"
    fi
  fi

  say
  title "Page"
  local url="http://localhost:$(app_port)/"
  local body_size status
  status="$(curl -s -o /dev/null -w '%{http_code}' --max-time 6 "$url" 2>/dev/null || echo 000)"
  body_size="$(curl -s --max-time 6 "$url" 2>/dev/null | wc -c || echo 0)"
  kv "url" "$url"
  kv "http status" "$status"
  kv "html bytes" "$body_size"
  if [[ "$status" != "200" ]]; then
    err "the page is not being served — check: ./wallcal.sh logs web -n 50"
  elif (( body_size < 1000 )); then
    err "the page is suspiciously small — the template may not be rendering"
  else
    ok "the web app is serving a full page"
  fi

  # A wall display often has no internet; anything render-blocking from an
  # external host would leave the panel white until the request times out.
  if curl -s --max-time 6 "$url" 2>/dev/null | grep -q 'rel="stylesheet"[^>]*href="https\?://'; then
    if curl -fs --max-time 5 -o /dev/null https://fonts.googleapis.com 2>/dev/null; then
      warn "the page links an external stylesheet, but the internet is reachable"
    else
      err "the page links a render-blocking external stylesheet and there is no internet"
      say "      ${C_CYAN}→ update to a build where the font load is async${C_RESET}"
    fi
  else
    ok "no render-blocking external stylesheets"
  fi

  say
  title "Processes"
  if pgrep -u "$RUN_USER" -f 'scripts/kiosk.sh' >/dev/null 2>&1; then
    ok "kiosk supervisor running"
  else
    err "kiosk supervisor not running — ./wallcal.sh kiosk start"
  fi
  local browser_count
  browser_count="$(pgrep -u "$RUN_USER" -c -f 'wallcal-kiosk' 2>/dev/null || echo 0)"
  if (( browser_count > 0 )); then
    ok "$browser_count browser process(es)"
  else
    err "no browser process — see the log below"
  fi

  say
  title "Recent kiosk log"
  if [[ -f "$KIOSK_LOG" ]]; then
    tail -n 20 "$KIOSK_LOG" | sed 's/^/  /'
  else
    say "  ${C_GREY}no log yet at $KIOSK_LOG${C_RESET}"
  fi

  say
  say "  If the panel is white: ${C_BOLD}./wallcal.sh kiosk gpu off${C_RESET}"
  say "  ${C_GREY}forces software rendering, which fixes a Chromium window that never paints.${C_RESET}"
  return 0
}

# ===========================================================================
#  Thin wrappers around the Python CLI
# ===========================================================================

json_flag() { (( JSON_OUT )) && printf '%s' "--json" || printf '%s' ""; }

cmd_display() {
  local action="${1:-info}"; shift || true
  case "$action" in
    detect|survey) pcli $(json_flag) display survey ;;
    info|status)   pcli $(json_flag) display info ;;
    autoselect)    pcli $(json_flag) display autoselect --save ;;
    on|off|toggle) pcli display "$action" ;;
    rotate)        pcli display rotate "${1:?usage: display rotate normal|left|right|inverted}" ;;
    backend)
      pcli settings set "display_backend=${1:?usage: display backend <name|auto>}"
      pcli presence rescan
      ;;
    strategy)  pcli display strategy "$@" ;;
    pwm)
      local sub="${1:-status}"; shift || true
      case "$sub" in
        status) pcli $(json_flag) display pwm status ;;
        test)   pcli display pwm test "$@" ;;
        *) die "usage: wallcal.sh display pwm {status|test}" ;;
      esac
      ;;
    output)
      pcli settings set "display_output=${1:?usage: display output <name|auto>}"
      pcli presence rescan
      ;;
    test)
      info "switching the display off for 3 seconds…"
      pcli display off
      sleep 3
      pcli display on
      ok "if the panel blinked, display control works"
      ;;
    *) die "usage: wallcal.sh display {detect|status|on|off|toggle|test|rotate|backend|output|strategy|pwm}" ;;
  esac
}

cmd_sensor() {
  local action="${1:-status}"; shift || true
  case "$action" in
    status|test) pcli $(json_flag) sensor test "$@" ;;
    scan)        pcli $(json_flag) sensor scan "$@" ;;
    monitor|live) pcli sensor monitor "$@" ;;
    params|config) pcli $(json_flag) sensor params "$@" ;;
    gates|range) pcli sensor gates "$@" ;;
    sensitivity) pcli sensor sensitivity "$@" ;;
    calibrate)   pcli $(json_flag) sensor calibrate "$@" ;;
    reset)       if confirm "Factory-reset the sensor?"; then pcli sensor reset "$@"; fi ;;
    *) die "usage: wallcal.sh sensor {status|scan|monitor|params|gates|sensitivity|calibrate|reset}" ;;
  esac
}

cmd_presence() {
  local action="${1:-status}"; shift || true
  case "$action" in
    status|state) pcli $(json_flag) presence state ;;
    on|off|auto)  pcli presence override "$action" ;;
    wake)         pcli presence wake "${1:-300}" ;;
    rescan)       pcli presence rescan ;;
    distance)     pcli settings set "sensor_distance_max_cm=${1:?usage: presence distance <cm>}" ;;
    timeout)      pcli settings set "display_off_timeout=${1:?usage: presence timeout <seconds>}" ;;
    *) die "usage: wallcal.sh presence {status|on|off|auto|wake|rescan|distance|timeout}" ;;
  esac
}

cmd_config() {
  local action="${1:-list}"; shift || true
  case "$action" in
    list|show) pcli $(json_flag) settings list ;;
    get)       pcli settings get "${1:?usage: config get <key>}" ;;
    set)       pcli settings set "$@" ;;
    edit)      "${EDITOR:-nano}" "$APP_DIR/.env" ;;
    *) die "usage: wallcal.sh config {list|get|set|edit}" ;;
  esac
}

# ===========================================================================
#  Backup / restore
# ===========================================================================

cmd_backup() {
  local target="${1:-}"
  mkdir -p "$BACKUP_DIR"
  [[ -n "$target" ]] || target="$BACKUP_DIR/wallcal-$(date +%Y%m%d-%H%M%S).db"
  local db="$(db_path)"
  [[ -f "$db" ]] || die "no database at $db"

  # .backup takes a consistent snapshot even while the app is writing.
  if have sqlite3; then
    run sqlite3 "$db" ".backup '$target'"
  else
    py -c "
import sqlite3, sys
src = sqlite3.connect(sys.argv[1]); dst = sqlite3.connect(sys.argv[2])
src.backup(dst); dst.close(); src.close()
" "$db" "$target"
  fi
  ok "backup written to $target"
  printf '%s\n' "$target"
}

cmd_restore() {
  local source="${1:?usage: wallcal.sh restore <backup.db>}"
  [[ -f "$source" ]] || die "no such backup: $source"
  confirm "Replace the current database with $source?" || die "aborted"
  cmd_backup >/dev/null
  as_root systemctl stop "$SERVICE_WEB" "$SERVICE_PRESENCE" 2>/dev/null || true
  run cp "$source" "$(db_path)"
  run rm -f "$(db_path)-wal" "$(db_path)-shm"
  as_user chown "$RUN_USER:$RUN_GROUP" "$(db_path)" 2>/dev/null || true
  as_root systemctl start "$SERVICE_WEB" "$SERVICE_PRESENCE" 2>/dev/null || true
  ok "restored from $source"
}

# ===========================================================================
#  Doctor
# ===========================================================================

CHK_PASS=0; CHK_WARN=0; CHK_FAIL=0
FIX_MODE=0

chk() {
  # chk <status: ok|warn|fail> <label> [detail] [fix-hint]
  local status="$1" label="$2" detail="${3:-}" hint="${4:-}"
  case "$status" in
    ok)   printf '  %s %s\n' "${C_GREEN}✔${C_RESET}" "$label"; CHK_PASS=$((CHK_PASS+1)) ;;
    warn) printf '  %s %s\n' "${C_YELLOW}▲${C_RESET}" "$label"; CHK_WARN=$((CHK_WARN+1)) ;;
    fail) printf '  %s %s\n' "${C_RED}✖${C_RESET}" "$label";   CHK_FAIL=$((CHK_FAIL+1)) ;;
  esac
  [[ -n "$detail" ]] && printf '      %s\n' "${C_GREY}${detail}${C_RESET}"
  [[ -n "$hint" && "$status" != ok ]] && printf '      %s\n' "${C_CYAN}→ ${hint}${C_RESET}"
  return 0
}

# Only checked when the off-strategy actually includes pwm — a user on a
# monitor that honours DPMS should never be told about a backlight they have
# no reason to have wired.
doctor_backlight() {
  local report enabled pin overlay err sysfs config_txt
  # Tab-separated rather than JSON: there is no JSON helper in this script and
  # adding one for four fields is not worth it.
  report="$(py_user -c "
import database
from presence import pwm

s = database.get_all_settings()
strategy = [p.strip().lower() for p in str(s.get('display_off_strategy','hdmi')).split(',')]
enabled = 'pwm' in strategy
overlay = err = ''
sysfs = False
if enabled:
    try:
        overlay = pwm.overlay_line(int(s.get('pwm_gpio', 18)))
    except Exception as exc:
        err = str(exc)
    sysfs = pwm.survey()['available']
print('\t'.join(['1' if enabled else '0', str(s.get('pwm_gpio','')),
                 overlay, err, '1' if sysfs else '0']))
" 2>/dev/null || printf '0\t\t\t\t0')"

  IFS=$'\t' read -r enabled pin overlay err sysfs <<<"$report"
  [[ "$enabled" == "1" ]] || return 0

  if [[ -n "$err" ]]; then
    chk fail "backlight PWM: $err" "" \
      "./wallcal.sh config set pwm_gpio=18   # or 12, 13, 19"
    return 0
  fi

  config_txt="$(boot_config_path 2>/dev/null || true)"
  if [[ -n "$config_txt" ]] && grep -qF "$overlay" "$config_txt" 2>/dev/null; then
    chk ok "backlight PWM overlay present (GPIO$pin)"
  elif [[ -n "$config_txt" ]] && grep -qE '^dtoverlay=pwm' "$config_txt" 2>/dev/null; then
    # An overlay for a different pin, or the wrong func, produces no output
    # and no error at all — the single most confusing PWM failure there is.
    chk fail "backlight PWM overlay does not match GPIO$pin" \
      "config.txt has a pwm overlay, but not '$overlay'" \
      "the func value differs per pin; fix the line to: $overlay"
  else
    chk fail "backlight PWM overlay missing" \
      "the pwm strategy is selected but no dtoverlay=pwm line is present" \
      "add '$overlay' to $config_txt and reboot"
  fi

  if [[ "$sysfs" == "1" ]]; then
    chk ok "backlight PWM sysfs available"
  else
    chk fail "no /sys/class/pwm — the overlay is not loaded" \
      "a reboot is needed after adding the overlay" \
      "sudo reboot, then ./wallcal.sh display pwm test"
  fi
}

# Generic: every configured pin is checked against every other, so a pin
# setting added later is covered without touching this.
doctor_pins() {
  local conflicts
  conflicts="$(py_user -c "
import database
for severity, a, b in database.pin_conflicts():
    print('%s\t%s\t%s\tGPIO%d' % (severity, a.label, b.label, a.pin))
" 2>/dev/null || true)"

  if [[ -z "$conflicts" ]]; then
    chk ok "no GPIO pin conflicts"
    return 0
  fi
  while IFS=$'\t' read -r severity a b pin; do
    [[ -n "$severity" ]] || continue
    if [[ "$severity" == "error" ]]; then
      chk fail "$a and $b are both on $pin" \
        "two things are driving the same pin" \
        "move one: ./wallcal.sh config set sensor_gpio_pin=23"
    else
      chk warn "$a and $b share $pin" \
        "only a problem if both are wired up — one of them is not active in this mode" \
        "if you use both, move one: ./wallcal.sh config set sensor_gpio_pin=23"
    fi
  done <<<"$conflicts"
}

cmd_doctor() {
  while (( $# )); do
    case "$1" in
      --fix) FIX_MODE=1 ;;
      *) die "unknown option for doctor: $1" ;;
    esac
    shift
  done

  banner
  title "Environment"
  chk ok "model: $(pi_model)"
  chk ok "os: $(os_pretty)"
  chk ok "kernel: $(uname -r)"

  # A root-owned checkout breaks far more than the installer: the services run
  # as RUN_USER and cannot write the database, the cache or the log.
  if as_user test -w "$APP_DIR" 2>/dev/null; then
    chk ok "checkout writable by $RUN_USER"
  else
    local dir_owner; dir_owner="$(stat -c '%U' "$APP_DIR" 2>/dev/null || echo '?')"
    if (( FIX_MODE )); then
      as_root chown -R "$RUN_USER:$(id -gn "$RUN_USER")" "$APP_DIR"
      chk ok "checkout ownership repaired ($dir_owner -> $RUN_USER)"
    else
      chk fail "$APP_DIR belongs to $dir_owner, not $RUN_USER" \
        "the services run as $RUN_USER and cannot write the database here" \
        "sudo chown -R $RUN_USER:$(id -gn "$RUN_USER" 2>/dev/null || echo "$RUN_USER") $APP_DIR"
    fi
  fi
  if have vcgencmd; then
    local throttled; throttled="$(vcgencmd get_throttled 2>/dev/null | cut -d= -f2 || true)"
    if [[ -z "$throttled" ]]; then
      chk warn "could not read the power/throttling flags"
    elif [[ "$throttled" == "0x0" ]]; then
      chk ok "power supply: no under-voltage recorded"
    else
      local bits
      bits=$(( throttled ))
      local now=() past=()
      (( bits & 0x1 ))     && now+=("under-voltage")
      (( bits & 0x2 ))     && now+=("ARM frequency capped")
      (( bits & 0x4 ))     && now+=("throttled")
      (( bits & 0x8 ))     && now+=("soft temperature limit")
      (( bits & 0x10000 )) && past+=("under-voltage")
      (( bits & 0x20000 )) && past+=("ARM frequency capped")
      (( bits & 0x40000 )) && past+=("throttled")
      (( bits & 0x80000 )) && past+=("soft temperature limit")

      if (( ${#now[@]} )); then
        chk fail "power supply: ${now[*]} — right now" \
          "flags $throttled; also seen since boot: ${past[*]:-none}" \
          "fit a 5V/3A supply and a short, thick cable. Under-voltage corrupts SD cards and makes the 256000-baud sensor link unreliable"
      else
        chk warn "power supply: ${past[*]} since boot (recovered)" \
          "flags $throttled" \
          "if this keeps happening, use a beefier 5V supply"
      fi
    fi
    local temp; temp="$(vcgencmd measure_temp 2>/dev/null | cut -d= -f2)"
    chk ok "temperature: ${temp:-unknown}"
  fi
  local free_mb; free_mb="$(df -Pm "$APP_DIR" | awk 'NR==2{print $4}')"
  if (( free_mb < 200 )); then
    chk fail "disk space: ${free_mb} MB free" "" "free space or the database will fail to write"
  else
    chk ok "disk space: ${free_mb} MB free"
  fi

  title "Application"
  if [[ -x "$VENV_PY" ]]; then
    chk ok "virtualenv present" "$VENV_DIR"
  else
    chk fail "virtualenv missing" "$VENV_DIR" "./wallcal.sh install --only venv"
  fi
  local missing_mods
  missing_mods="$(py -c "
import importlib.util
mods = ['flask','caldav','icalendar','dateutil','cryptography','serial']
print(' '.join(m for m in mods if importlib.util.find_spec(m) is None))
" 2>/dev/null || echo "?")"
  if [[ -z "$missing_mods" ]]; then
    chk ok "python dependencies importable"
  else
    chk fail "missing modules: $missing_mods" "" "./wallcal.sh install --only venv"
  fi
  if [[ -f "$(db_path)" ]]; then
    local integrity; integrity="$(py -c "
import sqlite3, sys
print(sqlite3.connect(sys.argv[1]).execute('PRAGMA quick_check').fetchone()[0])
" "$(db_path)" 2>/dev/null || echo "unreadable")"
    [[ "$integrity" == "ok" ]] && chk ok "database healthy" \
      || chk fail "database check: $integrity" "" "./wallcal.sh restore <backup>"
  else
    chk warn "no database yet" "$(db_path)" "./wallcal.sh install --only database"
  fi
  if [[ -f "$APP_DIR/.env" ]] && grep -q 'WALLCAL_SECRET_KEY=' "$APP_DIR/.env"; then
    chk ok "secret key configured"
  else
    chk warn "using the default secret key" \
      "stored CalDAV passwords are encrypted with a published key" \
      "./wallcal.sh install --only database"
  fi

  title "Services"
  local unit
  for unit in "$SERVICE_WEB" "$SERVICE_PRESENCE"; do
    local state; state="$(svc_state "$unit")"
    case "$state" in
      active) chk ok "$unit active" ;;
      not-installed) chk fail "$unit not installed" "" "./wallcal.sh install --only services" ;;
      *)
        chk fail "$unit is $state" "" "./wallcal.sh logs -n 50"
        if (( FIX_MODE )); then
          as_root systemctl restart "$unit" && chk ok "restarted $unit"
        fi
        ;;
    esac
  done
  if curl -fs --max-time 4 -o /dev/null "http://localhost:$(app_port)/api/status"; then
    chk ok "web app answering on port $(app_port)"
  else
    chk fail "web app not answering on port $(app_port)" "" "./wallcal.sh logs web -n 50"
  fi

  title "Presence sensor"
  local groups_now; groups_now="$(id -nG "$RUN_USER" 2>/dev/null || echo)"
  if [[ " $groups_now " == *" dialout "* ]]; then
    chk ok "$RUN_USER is in the dialout group"
  else
    chk fail "$RUN_USER not in the dialout group" "serial access will be denied" \
      "./wallcal.sh install --only groups, then log out and back in"
  fi
  local config_txt
  if config_txt="$(boot_config_path)"; then
    if grep -q '^enable_uart=1' "$config_txt"; then
      chk ok "enable_uart=1 in $(basename "$config_txt")"
    else
      chk warn "enable_uart is not set" "$config_txt" "./wallcal.sh install --only uart"
    fi
    if grep -qE '^dtoverlay=(disable-bt|miniuart-bt)' "$config_txt"; then
      chk ok "PL011 UART routed to GPIO14/15"
    else
      chk warn "the mini UART is on the header pins" \
        "256000 baud is unreliable there on a Pi 3B+" \
        "./wallcal.sh install --only uart"
    fi
  fi
  local ports; ports="$(py -c "from presence import ld2410; print(' '.join(ld2410.available_ports()))" 2>/dev/null || echo)"
  if [[ -n "$ports" ]]; then
    chk ok "serial devices: $ports"
  else
    chk warn "no serial devices found" "" "./wallcal.sh install --only uart && sudo reboot"
  fi
  if pcli sensor test >/dev/null 2>&1; then
    chk ok "sensor responding"
  else
    chk warn "sensor not responding" "" "./wallcal.sh sensor scan --save"
  fi

  title "Display"
  if [[ $EUID -eq 0 && "$RUN_USER" != "root" ]]; then
    say "  ${C_GREY}probing as $RUN_USER — the display belongs to that session${C_RESET}"
  fi
  local backend
  backend="$(py_user -c "
from presence import runtime
from presence.display import DisplayController
# What the daemon actually settled on beats a fresh probe — that is the code
# driving the panel right now.
state = runtime.read_state()
chosen = state.get('display_backends') or []
if state.get('daemon_running') and chosen:
    print(chosen[0])
else:
    rows = [r['name'] for r in DisplayController.survey()
            if r['available'] and r['name'] != 'none']
    print(rows[0] if rows else '')
" 2>/dev/null || echo)"
  if [[ -n "$backend" ]]; then
    case "$backend" in
      wlopm|xset)
        chk ok "display backend: $backend (true DPMS)"
        ;;
      *)
        # These power the output down in a way the compositor does not see,
        # so the window is never marked dirty and can come back blank.
        chk warn "display backend: $backend — does not preserve the output" \
          "the panel may show a white or stale frame after waking" \
          "install a DPMS-capable tool: sudo apt install wlr-randr wlopm"
        ;;
    esac
  else
    chk warn "no display power backend found" \
      "is a desktop session running as $RUN_USER?" \
      "./wallcal.sh display detect"
  fi
  doctor_backlight
  doctor_pins

  if [[ -f "$KIOSK_DESKTOP" ]]; then
    chk ok "kiosk autostart installed"
  else
    chk warn "kiosk autostart missing" "" "./wallcal.sh install --only kiosk"
  fi
  if pgrep -u "$RUN_USER" -f 'scripts/kiosk.sh' >/dev/null 2>&1; then
    chk ok "kiosk supervisor running"
  else
    chk warn "kiosk not running" "" "./wallcal.sh kiosk start"
  fi
  if have chromium-browser || have chromium; then
    chk ok "chromium installed"
  else
    chk fail "chromium missing" "" "sudo apt-get install chromium-browser"
  fi

  title "Summary"
  printf '  %s%d passed%s   %s%d warnings%s   %s%d failures%s\n\n' \
    "$C_GREEN" "$CHK_PASS" "$C_RESET" \
    "$C_YELLOW" "$CHK_WARN" "$C_RESET" \
    "$C_RED" "$CHK_FAIL" "$C_RESET"

  if (( CHK_FAIL > 0 )); then
    say "  Run ${C_BOLD}./wallcal.sh doctor --fix${C_RESET} to attempt repairs."
    trap - ERR
    exit 1
  fi
  return 0
}

# ===========================================================================
#  Automation entry points (called by the systemd timers)
# ===========================================================================

cmd_watchdog() {
  local restarted=0

  if [[ "$(svc_state "$SERVICE_WEB")" == "active" ]]; then
    local tries=0
    until curl -fs --max-time 5 -o /dev/null "http://localhost:$(app_port)/api/status"; do
      tries=$((tries + 1))
      (( tries >= 3 )) && break
      sleep 3
    done
    if (( tries >= 3 )); then
      logger -t wallcal-watchdog "web app unresponsive — restarting $SERVICE_WEB"
      as_root systemctl restart "$SERVICE_WEB"
      restarted=1
    fi
  elif [[ "$(svc_state "$SERVICE_WEB")" != "not-installed" ]]; then
    logger -t wallcal-watchdog "$SERVICE_WEB inactive — starting"
    as_root systemctl start "$SERVICE_WEB" || true
    restarted=1
  fi

  if [[ "$(svc_state "$SERVICE_PRESENCE")" != "active" ]] \
     && [[ "$(svc_state "$SERVICE_PRESENCE")" != "not-installed" ]]; then
    logger -t wallcal-watchdog "$SERVICE_PRESENCE inactive — starting"
    as_root systemctl start "$SERVICE_PRESENCE" || true
    restarted=1
  fi

  # If a desktop session exists but the kiosk supervisor died, bring it back.
  if [[ -d "/run/user/$RUN_UID" ]] \
     && pgrep -u "$RUN_USER" -f '(labwc|wayfire|Xorg|openbox)' >/dev/null 2>&1 \
     && ! pgrep -u "$RUN_USER" -f 'scripts/kiosk.sh' >/dev/null 2>&1; then
    logger -t wallcal-watchdog "kiosk supervisor missing — relaunching"
    su - "$RUN_USER" -c "$SCRIPT_PATH --quiet kiosk start" >/dev/null 2>&1 || true
    restarted=1
  fi

  (( restarted )) && say "watchdog took corrective action" || dbg "watchdog: all healthy"
  return 0
}

cmd_maintain() {
  mkdir -p "$BACKUP_DIR"
  local db="$(db_path)"

  if [[ -f "$db" ]]; then
    cmd_backup >/dev/null 2>&1 || warn "nightly backup failed"
    py -c "
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
conn.execute('VACUUM')
conn.close()
" "$db" 2>/dev/null || warn "vacuum failed"
  fi

  # Keep the 14 most recent backups.
  if [[ -d "$BACKUP_DIR" ]]; then
    local old
    old="$(ls -1t "$BACKUP_DIR"/wallcal-*.db 2>/dev/null | tail -n +15 || true)"
    [[ -n "$old" ]] && printf '%s\n' "$old" | xargs -r rm -f
  fi

  # The kiosk log is append-only; trim it before it eats the card.
  if [[ -f "$KIOSK_LOG" ]]; then
    local size; size="$(stat -c '%s' "$KIOSK_LOG" 2>/dev/null || echo 0)"
    if (( size > 5242880 )); then
      tail -c 1048576 "$KIOSK_LOG" >"${KIOSK_LOG}.tmp" && mv "${KIOSK_LOG}.tmp" "$KIOSK_LOG"
    fi
  fi

  say "maintenance complete"
  return 0
}

# ===========================================================================
#  Info / misc
# ===========================================================================

cmd_info() {
  banner
  kv "version" "$WALLCAL_VERSION"
  kv "directory" "$APP_DIR"
  kv "data" "$DATA_DIR"
  kv "virtualenv" "$VENV_DIR"
  kv "run user" "$RUN_USER (uid $RUN_UID)"
  kv "model" "$(pi_model)"
  kv "os" "$(os_pretty)"
  kv "uptime" "$(uptime -p 2>/dev/null || true)"
  kv "hostname" "$(hostname)"
  kv "url" "$(app_url)"
  kv "mdns" "http://$(hostname).local:$(app_port)/"
  say
  cmd_display info 2>/dev/null || true
}

cmd_url() { printf '%s\n' "$(app_url)"; }

cmd_open() {
  local url; url="http://localhost:$(app_port)/"
  if have xdg-open; then
    kiosk_session_env
    as_user xdg-open "$url" >/dev/null 2>&1 &
    ok "opened $url"
  else
    printf '%s\n' "$url"
  fi
}

cmd_completion() {
  cat <<'EOF'
# WallCal bash completion — install with:
#   ./wallcal.sh completion | sudo tee /etc/bash_completion.d/wallcal >/dev/null
_wallcal() {
  local cur prev commands
  cur="${COMP_WORDS[COMP_CWORD]}"
  prev="${COMP_WORDS[COMP_CWORD-1]}"
  commands="install uninstall update start stop restart status enable disable
            logs kiosk display sensor presence config backup restore doctor
            watchdog maintain info url open version help completion"
  case "$prev" in
    kiosk)    COMPREPLY=($(compgen -W "start stop restart status logs gpu diagnose" -- "$cur")); return ;;
    display)  COMPREPLY=($(compgen -W "detect autoselect status on off toggle test rotate backend output strategy pwm" -- "$cur")); return ;;
    sensor)   COMPREPLY=($(compgen -W "status scan monitor params gates sensitivity calibrate reset" -- "$cur")); return ;;
    presence) COMPREPLY=($(compgen -W "status on off auto wake rescan distance timeout" -- "$cur")); return ;;
    config)   COMPREPLY=($(compgen -W "list get set edit" -- "$cur")); return ;;
    logs)     COMPREPLY=($(compgen -W "web presence kiosk all -f -n" -- "$cur")); return ;;
  esac
  COMPREPLY=($(compgen -W "$commands" -- "$cur"))
}
complete -F _wallcal wallcal.sh ./wallcal.sh wallcal
EOF
}

cmd_version() { printf '%s %s\n' "$APP_NAME" "$WALLCAL_VERSION"; }

cmd_help() {
  banner
  cat <<EOF
${C_BOLD}USAGE${C_RESET}
  ./wallcal.sh [global options] <command> [arguments]

${C_BOLD}SETUP${C_RESET}
  install [options]        Full first-time setup on a fresh Pi OS install
      --only <steps>         Run only these comma-separated steps
      --skip <steps>         Run everything except these steps
      --keep-bluetooth       Move Bluetooth to the mini UART instead of
                             disabling it (default: disable, more reliable)
      steps: ${INSTALL_STEPS[*]}
  uninstall [--purge] [--revert-boot]
                           Remove services, kiosk hooks and tuning
  update                   Pull code, refresh dependencies, restart

${C_BOLD}SERVICES${C_RESET}
  start | stop | restart   Control the web app and presence daemon
  enable | disable         Toggle start-at-boot
  status                   Health overview (add --json for machine output)
  logs [web|presence|kiosk|all] [-f] [-n N]

${C_BOLD}KIOSK${C_RESET}
  kiosk start|stop|restart|status|logs
                           The full-screen browser on the wall panel
  kiosk diagnose           Why is the panel blank/wrong? Checks the whole chain
  kiosk gpu auto|on|off    Force software rendering if the panel stays white

${C_BOLD}DISPLAY${C_RESET}
  display detect           Probe every power-control backend
  display autoselect       Pick the best backend available and pin it
  display status           Show the selected backend and current power state
  display on|off|toggle    Switch the panel directly
  display test             Blink the panel to prove control works
  display rotate <mode>    normal | left | right | inverted
  display backend <name>   Pin a backend, or 'auto'
  display output <name>    Pin a connector (e.g. HDMI-A-1), or 'auto'

${C_BOLD}SENSOR  (HLK-LD2410C)${C_RESET}
  sensor status            Is the radar alive?
  sensor scan [--save]     Find which port and baud rate it is on
  sensor monitor           Live distance/energy readout with a meter
  sensor calibrate [--apply]
                           Walk-test and get suggested thresholds
  sensor params            Read the sensor's own configuration
  sensor gates <cm>        Program the sensor's detection range
  sensor sensitivity <moving> <stationary> [--gate N]
  sensor reset             Factory-reset the sensor

${C_BOLD}PRESENCE${C_RESET}
  presence status          Live presence, display and threshold state
  presence on|off|auto     Override the automation (auto returns control)
  presence wake [seconds]  Keep the panel awake for a while
  presence distance <cm>   Set the wake distance
  presence timeout <sec>   Set how long the panel stays on after you leave
  presence rescan          Re-detect sensor and display

${C_BOLD}DATA${C_RESET}
  config list|get|set|edit Stored settings (same ones the web UI writes)
  backup [file]            Snapshot the database
  restore <file>           Restore a snapshot

${C_BOLD}DIAGNOSTICS${C_RESET}
  doctor [--fix]           Full system check, optionally self-healing
  info                     Everything about this installation
  url | open               Where the calendar lives
  completion               Emit a bash completion script
  version | help

${C_BOLD}GLOBAL OPTIONS${C_RESET}
  -y, --yes                Assume yes for every prompt
  -q, --quiet              Only warnings and errors
  -v, --verbose            Echo commands as they run
  -n, --dry-run            Show what would happen, change nothing
      --json               Machine-readable output where supported
      --no-color           Disable ANSI colour

${C_BOLD}EXAMPLES${C_RESET}
  sudo ./wallcal.sh install                 First-time setup
  ./wallcal.sh sensor calibrate --apply     Tune detection from where you stand
  ./wallcal.sh presence distance 250        Wake when someone is within 2.5 m
  ./wallcal.sh presence timeout 90          Stay on 90 s after they leave
  ./wallcal.sh doctor --fix                 Diagnose and self-heal
  ./wallcal.sh logs presence -f             Watch the daemon think
EOF
}

# ===========================================================================
#  Dispatch
# ===========================================================================

# Commands that write the database or open the sensor. Running these under
# sudo would leave root-owned files (and WAL/SHM sidecars) that the services —
# which run as $RUN_USER — can no longer write. Re-exec as the owning user.
USER_LEVEL_COMMANDS=" sensor radar presence display screen config settings backup kiosk "

maybe_drop_privileges() {
  local command="$1"
  [[ $EUID -eq 0 ]] || return 0
  [[ "$RUN_USER" != "root" ]] || return 0
  [[ "$USER_LEVEL_COMMANDS" == *" $command "* ]] || return 0
  have sudo || return 0
  dbg "re-executing as $RUN_USER to avoid creating root-owned files"
  exec sudo -u "$RUN_USER" -- "$SCRIPT_PATH" "${ORIGINAL_ARGS[@]}"
}

# Suggest the nearest command when someone mistypes or drops the group name.
suggest_command() {
  local typed="$1" candidate
  for candidate in install uninstall update start stop restart status enable \
                   disable logs kiosk display sensor presence config backup \
                   restore doctor info url open version help completion; do
    if [[ "$candidate" == "$typed"* || "$typed" == "$candidate"* ]]; then
      printf '%s' "$candidate"; return 0
    fi
  done
  # Bare subcommands people reach for without the group, e.g. "scan".
  case "$typed" in
    scan|monitor|calibrate|gates|params|sensitivity) printf 'sensor %s' "$typed" ;;
    on|off|auto|wake|rescan)                         printf 'presence %s' "$typed" ;;
    detect|rotate|backend|output)                    printf 'display %s' "$typed" ;;
    *) return 1 ;;
  esac
}

main() {
  ORIGINAL_ARGS=("$@")
  local args=()
  while (( $# )); do
    case "$1" in
      -y|--yes)      ASSUME_YES=1 ;;
      -q|--quiet)    QUIET=1 ;;
      -v|--verbose)  VERBOSE=1 ;;
      -n|--dry-run)  DRY_RUN=1 ;;
      --json)        JSON_OUT=1 ;;
      --no-color)    USE_COLOR=0; setup_colors ;;
      -h|--help)     cmd_help; return 0 ;;
      -V|--version)  cmd_version; return 0 ;;
      --only)        INSTALL_ONLY="$2"; shift ;;
      --skip)        INSTALL_SKIP="$2"; shift ;;
      --only=*)      INSTALL_ONLY="${1#*=}" ;;
      --skip=*)      INSTALL_SKIP="${1#*=}" ;;
      --keep-bluetooth) KEEP_BLUETOOTH=1 ;;
      --)            shift; args+=("$@"); break ;;
      *)             args+=("$1") ;;
    esac
    shift
  done
  set -- "${args[@]:-}"

  local command="${1:-help}"
  [[ $# -gt 0 ]] && shift || true

  maybe_drop_privileges "$command"

  case "$command" in
    install|setup)        cmd_install "$@" ;;
    uninstall|remove)     cmd_uninstall "$@" ;;
    update|upgrade)       cmd_update "$@" ;;
    start)                cmd_start "$@" ;;
    stop)                 cmd_stop "$@" ;;
    restart)              cmd_restart "$@" ;;
    enable)               cmd_enable "$@" ;;
    disable)              cmd_disable "$@" ;;
    status)               cmd_status "$@" ;;
    logs|log)             cmd_logs "$@" ;;
    kiosk)                cmd_kiosk "$@" ;;
    display|screen)       cmd_display "$@" ;;
    sensor|radar)         cmd_sensor "$@" ;;
    presence)             cmd_presence "$@" ;;
    config|settings)      cmd_config "$@" ;;
    backup)               cmd_backup "$@" ;;
    restore)              cmd_restore "$@" ;;
    doctor|check)         cmd_doctor "$@" ;;
    watchdog)             cmd_watchdog "$@" ;;
    maintain|maintenance) cmd_maintain "$@" ;;
    info)                 cmd_info "$@" ;;
    url)                  cmd_url "$@" ;;
    open)                 cmd_open "$@" ;;
    completion)           cmd_completion "$@" ;;
    version)              cmd_version ;;
    help|"")              cmd_help ;;
    *)
      err "unknown command: $command"
      local guess
      if guess="$(suggest_command "$command")"; then
        say "Did you mean ${C_BOLD}./wallcal.sh $guess${C_RESET}?"
      fi
      say "Run ${C_BOLD}./wallcal.sh help${C_RESET} for the command list."
      trap - ERR
      exit 2
      ;;
  esac
}

main "$@"
