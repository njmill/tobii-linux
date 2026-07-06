#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
work_dir="${SC_TOBII_NATIVE_RUNTIME_DIR:-$root_dir/.tmp/sc-tobii-native-runtime}"
timeout_s="${SC_TOBII_READY_TIMEOUT:-15}"
deadline=$((SECONDS + timeout_s))

middleware_stdout="$work_dir/middleware-spy.stdout"
sesp_stdout="$work_dir/middleware-pipe-spy.stdout"
etdefault_stdout="$work_dir/etdefaultpipe-spy.stdout"
tobii_prefixed_stdout="$work_dir/tobii-prefixed-pipe-spy.stdout"
tobiiprp_prefixed_stdout="$work_dir/tobiiprp-prefixed-pipe-spy.stdout"

has_line() {
  local path="$1"
  local pattern="$2"
  [[ -f "$path" ]] && grep -Eq "$pattern" "$path"
}

want_etdefault="${SC_TOBII_ETDEFAULTPIPE:-1}"
want_tobii_prefixed="${SC_TOBII_PREFIXED_PIPE:-1}"
want_tobiiprp_prefixed="${SC_TOBII_PRP_PREFIXED_PIPE:-1}"

while (( SECONDS <= deadline )); do
  middleware_ready=0
  sesp_ready=0
  etdefault_ready=1
  tobii_prefixed_ready=1
  tobiiprp_prefixed_ready=1

  has_line "$middleware_stdout" 'listening host=127\.0\.0\.1 port=4455' && middleware_ready=1
  has_line "$sesp_stdout" 'pipe_listening name=.*streamengineservices' && sesp_ready=1

  if [[ "$want_etdefault" == "1" ]]; then
    etdefault_ready=0
    has_line "$etdefault_stdout" 'pipe_listening name=.*ETDefaultPIPE' && etdefault_ready=1
  fi

  if [[ "$want_tobii_prefixed" == "1" ]]; then
    tobii_prefixed_ready=0
    has_line "$tobii_prefixed_stdout" 'pipe_listening name=.*TOBII-' && tobii_prefixed_ready=1
  fi

  if [[ "$want_tobiiprp_prefixed" == "1" ]]; then
    tobiiprp_prefixed_ready=0
    has_line "$tobiiprp_prefixed_stdout" 'pipe_listening name=.*TOBIIPRP-' && tobiiprp_prefixed_ready=1
  fi

  if (( middleware_ready && sesp_ready && etdefault_ready && tobii_prefixed_ready && tobiiprp_prefixed_ready )); then
    echo "native Tobii runtime ready"
    exit 0
  fi

  sleep 0.25
done

echo "warning: native Tobii runtime not fully ready after ${timeout_s}s" >&2
echo "  middleware=$middleware_ready sesp=$sesp_ready etdefault=$etdefault_ready tobii_prefixed=$tobii_prefixed_ready tobiiprp_prefixed=$tobiiprp_prefixed_ready" >&2
exit 1
