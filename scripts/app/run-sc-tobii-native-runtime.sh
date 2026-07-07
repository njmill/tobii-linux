#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
work_dir="${SC_TOBII_NATIVE_RUNTIME_DIR:-$root_dir/.tmp/sc-tobii-native-runtime}"
stock_dir="$root_dir/.tmp/sc-tobii-stock-recon"
mode="${1:-dashboard}"
middleware_port="${SC_TOBII_MIDDLEWARE_PORT:-4455}"
middleware_udp_port="${SC_TOBII_MIDDLEWARE_UDP_PORT:-4457}"
tobii_udp_port="${SC_TOBII_PORT:-4243}"
trackir_udp_port="${SC_TRACKIR_BRIDGE_PORT:-4242}"
prefix="${STAR_CITIZEN_PREFIX:-$HOME/Games/star-citizen}"
wine_bin="${STAR_CITIZEN_WINE:-}"
display_name="${SC_TOBII_DISPLAY_NAME:-\\\\.\\DISPLAY1}"
display_id="${SC_TOBII_DISPLAY_ID:-DISPLAY\\DEFAULT_MONITOR\\0000&0000}"
display_x="${SC_TOBII_DISPLAY_X:-0}"
display_y="${SC_TOBII_DISPLAY_Y:-0}"
display_width="${SC_TOBII_DISPLAY_WIDTH:-6000}"
display_height="${SC_TOBII_DISPLAY_HEIGHT:-1440}"
display_rect="${SC_TOBII_DISPLAY_RECT:-}"
display_area_mode="${SC_TOBII_DISPLAY_AREA_MODE:-dynamic}"

if [[ -z "$wine_bin" && -f "$prefix/sc-launch.sh" ]]; then
  wine_path="$(
    awk -F= '/^[[:space:]]*export[[:space:]]+wine_path=/{print $2}' "$prefix/sc-launch.sh" \
      | tail -1 \
      | sed -e 's/^"//' -e 's/"$//'
  )"
  if [[ -n "$wine_path" && -x "$wine_path/wine" ]]; then
    wine_bin="$wine_path/wine"
  fi
fi
wine_bin="${wine_bin:-wine}"

mkdir -p "$work_dir" "$stock_dir"

if [[ "${SC_TOBII_STOCK_DLL_PREFLIGHT:-1}" == "1" ]]; then
  "$root_dir/scripts/app/check-sc-stock-tobii-dll.sh"
fi

kill_matching_except_self() {
  local pattern="$1"
  local pid
  while read -r pid; do
    [[ -z "$pid" ]] && continue
    [[ "$pid" == "$$" || "$pid" == "$BASHPID" || "$pid" == "$PPID" ]] && continue
    kill "$pid" 2>/dev/null || true
  done < <(pgrep -f "$pattern" 2>/dev/null || true)
}

kill_tcp_port_listeners_except_self() {
  local port="$1"
  local pid
  while read -r pid; do
    [[ -z "$pid" ]] && continue
    [[ "$pid" == "$$" || "$pid" == "$BASHPID" || "$pid" == "$PPID" ]] && continue
    kill "$pid" 2>/dev/null || true
  done < <(
    ss -H -ltnp "sport = :$port" 2>/dev/null \
      | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' \
      | sort -u
  )
}

wait_tcp_port_free() {
  local port="$1"
  local tries="${2:-20}"
  local i
  for ((i = 0; i < tries; ++i)); do
    if ! ss -H -ltnp "sport = :$port" 2>/dev/null | grep -q .; then
      return 0
    fi
    sleep 0.2
  done
  return 1
}

restart_services_or_exit() {
  local name="$1"
  local log_path="$2"
  echo "error: $name exited while in service mode; log follows" >&2
  sed -n '1,160p' "$log_path" >&2
  if [[ "${SC_TOBII_SERVICE_AUTORESTART:-1}" == "1" ]]; then
    echo "restarting native Tobii runtime services after $name exit" >&2
    trap - EXIT
    cleanup 2>/dev/null || true
    sleep 1
    exec "$0" services
  fi
  exit 1
}

if [[ "$mode" == "services" ]]; then
  kill_matching_except_self "run-sc-tobii-native-runtime.sh services"
  pkill -f "$root_dir/scripts/app/tobii-middleware-spy.py.*--port $middleware_port" 2>/dev/null || true
  pkill -f "tobii-middleware-pipe-spy.exe" 2>/dev/null || true
  kill_tcp_port_listeners_except_self "$middleware_port"
  if ! wait_tcp_port_free "$middleware_port" 20; then
    echo "warning: TCP middleware port $middleware_port still busy after graceful cleanup; forcing listeners down" >&2
    while read -r pid; do
      [[ -z "$pid" ]] && continue
      [[ "$pid" == "$$" || "$pid" == "$BASHPID" || "$pid" == "$PPID" ]] && continue
      kill -9 "$pid" 2>/dev/null || true
    done < <(
      ss -H -ltnp "sport = :$middleware_port" 2>/dev/null \
        | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' \
        | sort -u
    )
    wait_tcp_port_free "$middleware_port" 10 || true
  fi
  sleep 0.2
fi

: >"$work_dir/middleware-spy.log"
: >"$work_dir/middleware-spy.stdout"
: >"$work_dir/middleware-pipe-spy.log"
: >"$work_dir/middleware-pipe-spy.stdout"
: >"$work_dir/etdefaultpipe-spy.log"
: >"$work_dir/etdefaultpipe-spy.stdout"
: >"$work_dir/tobii-prefixed-pipe-spy.log"
: >"$work_dir/tobii-prefixed-pipe-spy.stdout"
: >"$work_dir/tobiiprp-prefixed-pipe-spy.log"
: >"$work_dir/tobiiprp-prefixed-pipe-spy.stdout"

if [[ "${SC_POC_KEEP_EXISTING_CAPTURE:-0}" != "1" ]]; then
  kill_matching_except_self "run-sc-tobii-native-runtime.sh services"
  pkill -f "$root_dir/scripts/recon/eye-pose-dashboard.py" 2>/dev/null || true
  pkill -f "$root_dir/build/tobii-ttp-mux" 2>/dev/null || true
  pkill -f "$root_dir/build/tobii-gaze-native" 2>/dev/null || true
  pkill -f "tobii-middleware-pipe-spy.exe" 2>/dev/null || true
  pkill -f "tobii-middleware-spy.py.*--port $middleware_port" 2>/dev/null || true
  sleep 0.3
fi

echo "starting Tobii TCP middleware emulator on 127.0.0.1:$middleware_port"
middleware_display_args=(
  --display-id "$display_id"
  --display-name "$display_name"
  --display-x "$display_x"
  --display-y "$display_y"
  --display-width "$display_width"
  --display-height "$display_height"
  --display-area-mode "$display_area_mode"
)
if [[ -n "$display_rect" ]]; then
  middleware_display_args+=(--display-rect "$display_rect")
fi
if [[ -n "${SC_TOBII_DISPLAY_AREA_WIDTH:-}" ]]; then
  middleware_display_args+=(--display-area-width "$SC_TOBII_DISPLAY_AREA_WIDTH")
fi
if [[ -n "${SC_TOBII_DISPLAY_AREA_HEIGHT:-}" ]]; then
  middleware_display_args+=(--display-area-height "$SC_TOBII_DISPLAY_AREA_HEIGHT")
fi
"$root_dir/scripts/app/tobii-middleware-spy.py" \
  --host 127.0.0.1 \
  --port "$middleware_port" \
  --udp-port "$middleware_udp_port" \
  --seconds "${SC_TOBII_RUNTIME_SECONDS:-86400}" \
  --log "$work_dir/middleware-spy.log" \
  --device-serial "${SC_TOBII_DEVICE_SERIAL:-IS5FF-100203612152}" \
  --model-name "${SC_TOBII_MODEL_NAME:-IS5_Large_Eyetracker_5}" \
  --short-model "${SC_TOBII_SHORT_MODEL:-IS5}" \
  --firmware "${SC_TOBII_FIRMWARE:-02a1a6a977}" \
  "${middleware_display_args[@]}" \
  --stream-catalog-variant host-headpose \
  --capability-mode "${SC_TOBII_TTP_CAPABILITY_MODE:-headpose}" \
  --reply-bootstrap \
  --force-headpose-capabilities \
  --synthetic-presence \
  --live-gaze \
  --live-gaze-hz "${SC_TOBII_LIVE_GAZE_HZ:-60}" \
  --live-gaze-max-age-s "${SC_TOBII_LIVE_GAZE_MAX_AGE_S:-0.5}" \
  --runtime-metadata-mode "${SC_TOBII_RUNTIME_METADATA_MODE:-empty}" \
  ${SC_TOBII_TTP_UNSOLICITED_PRESENCE:+--unsolicited-presence-after-discovery} \
  >"$work_dir/middleware-spy.stdout" 2>&1 &
middleware_pid=$!
dashboard_pid=""
etdefault_pid=""
tobii_prefixed_pid=""
tobiiprp_prefixed_pid=""
pipe_args=(
  --seconds "${SC_TOBII_RUNTIME_SECONDS:-86400}"
  --log "$work_dir/middleware-pipe-spy.log"
  --reply-bootstrap
  --sesp-connect-reply 1
  --sesp-auto-reply "${SC_TOBII_SESP_AUTO_REPLY:-3}"
  --sesp-synthetic-headpose "${SC_TOBII_HEADPOSE_HZ:-30}"
  --sesp-headpose-udp "$tobii_udp_port"
  --display-name "$display_name"
  --display-device-id "$display_id"
  --display-width "$display_width"
  --display-height "$display_height"
)
if [[ "${SC_TOBII_SESP_PROVIDER_NUDGE:-0}" == "1" ]]; then
  pipe_args+=(--sesp-provider-nudge)
fi

echo "starting Tobii SESP pipe emulator; live headpose UDP 127.0.0.1:$tobii_udp_port"
(
  cd "$work_dir"
  env \
    WINEPREFIX="$prefix" \
    WINEDEBUG="${SC_TOBII_PIPE_WINEDEBUG:--all}" \
    "$wine_bin" "$stock_dir/tobii-middleware-pipe-spy.exe" "${pipe_args[@]}"
) >"$work_dir/middleware-pipe-spy.stdout" 2>&1 &
pipe_pid=$!

if [[ "${SC_TOBII_ETDEFAULTPIPE:-1}" == "1" ]]; then
  echo "starting Tobii ETDefaultPIPE discovery emulator; entry=${SC_TOBII_ETDEFAULT_ENTRY:-127.0.0.1}"
  (
    cd "$work_dir"
    env \
      WINEPREFIX="$prefix" \
      WINEDEBUG="${SC_TOBII_PIPE_WINEDEBUG:--all}" \
      "$wine_bin" "$stock_dir/tobii-middleware-pipe-spy.exe" \
        --seconds "${SC_TOBII_RUNTIME_SECONDS:-86400}" \
        --log "$work_dir/etdefaultpipe-spy.log" \
        --pipe "\\\\.\\pipe\\ETDefaultPIPE" \
        --etdefaultpipe \
        --etdefault-entry "${SC_TOBII_ETDEFAULT_ENTRY:-127.0.0.1}"
  ) >"$work_dir/etdefaultpipe-spy.stdout" 2>&1 &
  etdefault_pid=$!
fi

if [[ "${SC_TOBII_PREFIXED_PIPE:-1}" == "1" ]]; then
  echo "starting Tobii TOBII-* discovery marker pipe; name=TOBII-${SC_TOBII_PREFIXED_ENTRY:-127.0.0.1}"
  (
    cd "$work_dir"
    env \
      WINEPREFIX="$prefix" \
      WINEDEBUG="${SC_TOBII_PIPE_WINEDEBUG:--all}" \
      "$wine_bin" "$stock_dir/tobii-middleware-pipe-spy.exe" \
        --seconds "${SC_TOBII_RUNTIME_SECONDS:-86400}" \
        --log "$work_dir/tobii-prefixed-pipe-spy.log" \
        --pipe "\\\\.\\pipe\\TOBII-${SC_TOBII_PREFIXED_ENTRY:-127.0.0.1}"
  ) >"$work_dir/tobii-prefixed-pipe-spy.stdout" 2>&1 &
  tobii_prefixed_pid=$!
fi

if [[ "${SC_TOBII_PRP_PREFIXED_PIPE:-1}" == "1" ]]; then
  echo "starting Tobii TOBIIPRP-* discovery marker pipe; name=TOBIIPRP-${SC_TOBII_PRP_PREFIXED_ENTRY:-IS5FF-100203612152}"
  (
    cd "$work_dir"
    env \
      WINEPREFIX="$prefix" \
      WINEDEBUG="${SC_TOBII_PIPE_WINEDEBUG:--all}" \
      "$wine_bin" "$stock_dir/tobii-middleware-pipe-spy.exe" \
        --seconds "${SC_TOBII_RUNTIME_SECONDS:-86400}" \
        --log "$work_dir/tobiiprp-prefixed-pipe-spy.log" \
        --pipe "\\\\.\\pipe\\TOBIIPRP-${SC_TOBII_PRP_PREFIXED_ENTRY:-IS5FF-100203612152}"
  ) >"$work_dir/tobiiprp-prefixed-pipe-spy.stdout" 2>&1 &
  tobiiprp_prefixed_pid=$!
fi

cleanup() {
  if [[ -n "$dashboard_pid" ]]; then
    kill "$dashboard_pid" 2>/dev/null || true
  fi
  pkill -P "$$" 2>/dev/null || true
  if [[ -n "$etdefault_pid" ]]; then
    kill "$etdefault_pid" 2>/dev/null || true
  fi
  if [[ -n "$tobii_prefixed_pid" ]]; then
    kill "$tobii_prefixed_pid" 2>/dev/null || true
  fi
  if [[ -n "$tobiiprp_prefixed_pid" ]]; then
    kill "$tobiiprp_prefixed_pid" 2>/dev/null || true
  fi
  kill "$middleware_pid" "$pipe_pid" 2>/dev/null || true
  if [[ -n "$dashboard_pid" ]]; then
    wait "$dashboard_pid" 2>/dev/null || true
  fi
  wait "$middleware_pid" 2>/dev/null || true
  wait "$pipe_pid" 2>/dev/null || true
  if [[ -n "$etdefault_pid" ]]; then
    wait "$etdefault_pid" 2>/dev/null || true
  fi
  if [[ -n "$tobii_prefixed_pid" ]]; then
    wait "$tobii_prefixed_pid" 2>/dev/null || true
  fi
  if [[ -n "$tobiiprp_prefixed_pid" ]]; then
    wait "$tobiiprp_prefixed_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT

sleep 1
if ! kill -0 "$middleware_pid" 2>/dev/null; then
  echo "error: TCP middleware emulator exited; log follows" >&2
  sed -n '1,160p' "$work_dir/middleware-spy.stdout" >&2
  exit 1
fi
if ! kill -0 "$pipe_pid" 2>/dev/null; then
  echo "error: SESP pipe emulator exited; log follows" >&2
  sed -n '1,160p' "$work_dir/middleware-pipe-spy.stdout" >&2
  exit 1
fi
if [[ -n "$etdefault_pid" ]] && ! kill -0 "$etdefault_pid" 2>/dev/null; then
  echo "error: ETDefaultPIPE discovery emulator exited; log follows" >&2
  sed -n '1,160p' "$work_dir/etdefaultpipe-spy.stdout" >&2
  exit 1
fi
if [[ -n "$tobii_prefixed_pid" ]] && ! kill -0 "$tobii_prefixed_pid" 2>/dev/null; then
  echo "error: TOBII-* discovery marker exited; log follows" >&2
  sed -n '1,160p' "$work_dir/tobii-prefixed-pipe-spy.stdout" >&2
  exit 1
fi
if [[ -n "$tobiiprp_prefixed_pid" ]] && ! kill -0 "$tobiiprp_prefixed_pid" 2>/dev/null; then
  echo "error: TOBIIPRP-* discovery marker exited; log follows" >&2
  sed -n '1,160p' "$work_dir/tobiiprp-prefixed-pipe-spy.stdout" >&2
  exit 1
fi

tuning_file="${SC_TUNING_FILE:-$work_dir/sc-tuning.json}"
window_state_file="${SC_WINDOW_STATE_FILE:-$work_dir/sc-window.json}"
screen_calibration_file="${SC_SCREEN_CALIBRATION_FILE:-$work_dir/screen-calibration.json}"
gaze_calibration_file="${SC_GAZE_CALIBRATION_FILE:-$work_dir/gaze-calibration.json}"
echo "native Tobii runtime active"
echo "middleware_log=$work_dir/middleware-spy.log"
echo "pipe_log=$work_dir/middleware-pipe-spy.log"
echo "etdefaultpipe_log=$work_dir/etdefaultpipe-spy.log"
echo "tobii_prefixed_pipe_log=$work_dir/tobii-prefixed-pipe-spy.log"
echo "live_gaze_udp=127.0.0.1:$middleware_udp_port"
echo "display_binding name=$display_name id=$display_id rect=${display_rect:-$display_x,$display_y,$display_width,$display_height} area_mode=$display_area_mode"
echo "dashboard output defaults to Tobii; TrackIR fallback target is 127.0.0.1:$trackir_udp_port"
echo

common_args=(
  --no-hq-frames --face-frame-hz "${SC_TOBII_FACE_FPS:-60}"
  --harness "${SC_TOBII_HARNESS:-tobii}"
  --tobii-udp "127.0.0.1:$tobii_udp_port"
  --trackir-udp "127.0.0.1:$trackir_udp_port"
  --opentrack-udp "127.0.0.1:$tobii_udp_port"
  --stock-gaze-udp "127.0.0.1:$middleware_udp_port"
  --opentrack-yaw-scale "${SC_TOBII_YAW_SCALE:-8.0}"
  --opentrack-pitch-scale "${SC_TOBII_PITCH_SCALE:--14.0}"
  --opentrack-roll-scale "${SC_TOBII_ROLL_SCALE:-1.0}"
  --tobii-roll-source "${SC_TOBII_ROLL_SOURCE:-pose}"
  --opentrack-x-scale "${SC_TOBII_X_SCALE:--3.0}"
  --opentrack-y-scale "${SC_TOBII_Y_SCALE:-3.0}"
  --opentrack-z-scale "${SC_TOBII_Z_SCALE:-4.0}"
  --opentrack-output-smoothing "${SC_TOBII_OUTPUT_SMOOTHING:-0.03}"
  --opentrack-motion-smoothing "${SC_TOBII_MOTION_SMOOTHING:-0.05}"
  --opentrack-prediction-ms "${SC_TOBII_PREDICTION_MS:-45.0}"
  --opentrack-stillness-deadband-deg "${SC_TOBII_STILLNESS_DEADBAND_DEG:-0.18}"
  --opentrack-stillness-velocity-dps "${SC_TOBII_STILLNESS_VELOCITY_DPS:-10.0}"
  --opentrack-send-hz "${SC_TOBII_SEND_HZ:-120.0}"
  --opentrack-max-angle-step "${SC_TOBII_MAX_ANGLE_STEP:-180.0}"
  --opentrack-max-translation-step "${SC_TOBII_MAX_TRANSLATION_STEP:-0.20}"
  --opentrack-yaw-curve "${SC_TOBII_YAW_CURVE:-2.0}"
  --opentrack-pitch-curve "${SC_TOBII_PITCH_CURVE:-2.0}"
  --opentrack-curve-knee-deg "${SC_TOBII_CURVE_KNEE_DEG:-18.0}"
  --opentrack-max-output-angle "${SC_TOBII_MAX_OUTPUT_ANGLE:-160.0}"
  --mediapipe-yaw-output-scale "${SC_TOBII_MEDIAPIPE_YAW_SCALE:-0.15}"
  --mediapipe-pitch-output-scale "${SC_TOBII_MEDIAPIPE_PITCH_SCALE:-0.10}"
  --mediapipe-roll-output-scale "${SC_TOBII_MEDIAPIPE_ROLL_SCALE:-0.15}"
  --mediapipe-translation-output-scale "${SC_TOBII_MEDIAPIPE_TRANSLATION_SCALE:-10.0}"
  --mediapipe-depth-output-scale "${SC_TOBII_MEDIAPIPE_DEPTH_SCALE:-250.0}"
  --eye-origin-z-deadband-mm "${SC_TOBII_EYE_Z_DEADBAND_MM:-1.0}"
  --mediapipe-pitch-yaw-comp "${SC_TOBII_MEDIAPIPE_PITCH_YAW_COMP:-1.0}"
  --mediapipe-rotation-mode "${SC_TOBII_MEDIAPIPE_ROTATION_MODE:-forward}"
  --mediapipe-pose-source "${SC_TOBII_MEDIAPIPE_POSE_SOURCE:-hybrid}"
  --blink-hold-s "${SC_TOBII_BLINK_HOLD_S:-0.20}"
  --tuning-file "$tuning_file"
  --window-state-file "$window_state_file"
  --screen-calibration-file "$screen_calibration_file"
  --gaze-calibration-file "$gaze_calibration_file"
)

case "$mode" in
  services)
    echo "service-only mode: waiting for Star Citizen; stop this process to shut down Tobii emulation"
    while true; do
      if ! kill -0 "$middleware_pid" 2>/dev/null; then
        restart_services_or_exit "TCP middleware emulator" "$work_dir/middleware-spy.stdout"
      fi
      if ! kill -0 "$pipe_pid" 2>/dev/null; then
        restart_services_or_exit "SESP pipe emulator" "$work_dir/middleware-pipe-spy.stdout"
      fi
      if [[ -n "$etdefault_pid" ]] && ! kill -0 "$etdefault_pid" 2>/dev/null; then
        restart_services_or_exit "ETDefaultPIPE discovery emulator" "$work_dir/etdefaultpipe-spy.stdout"
      fi
      if [[ -n "$tobii_prefixed_pid" ]] && ! kill -0 "$tobii_prefixed_pid" 2>/dev/null; then
        restart_services_or_exit "TOBII-* discovery marker" "$work_dir/tobii-prefixed-pipe-spy.stdout"
      fi
      if [[ -n "$tobiiprp_prefixed_pid" ]] && ! kill -0 "$tobiiprp_prefixed_pid" 2>/dev/null; then
        restart_services_or_exit "TOBIIPRP-* discovery marker" "$work_dir/tobiiprp-prefixed-pipe-spy.stdout"
      fi
      sleep 2 &
      wait $! || true
    done
    ;;
  dashboard)
    "$root_dir/scripts/recon/eye-pose-dashboard.py" "${common_args[@]}" &
    dashboard_pid=$!
    wait "$dashboard_pid"
    ;;
  headless)
    "$root_dir/scripts/recon/eye-pose-dashboard.py" --headless "${common_args[@]}" &
    dashboard_pid=$!
    wait "$dashboard_pid"
    ;;
  *)
    echo "usage: $0 [dashboard|headless]" >&2
    exit 2
    ;;
esac
