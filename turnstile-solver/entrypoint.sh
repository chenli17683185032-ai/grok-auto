#!/usr/bin/env bash
set -euo pipefail
cd /app

HOST="${TURNSTILE_HOST:-0.0.0.0}"
PORT="${TURNSTILE_PORT:-5072}"
THREAD="${TURNSTILE_THREAD:-1}"
NICE="${TURNSTILE_NICE:-10}"
BROWSER_TYPE="${TURNSTILE_BROWSER_TYPE:-camoufox}"
if [[ ! "${NICE}" =~ ^[0-9]+$ ]] || (( 10#${NICE} > 19 )); then
  echo "[turnstile-solver] WARN: TURNSTILE_NICE must be 0..19; using 10" >&2
  NICE=10
else
  NICE="$((10#${NICE}))"
fi
DEBUG_FLAG=()
if [[ "${TURNSTILE_DEBUG:-1}" == "1" || "${TURNSTILE_DEBUG:-true}" == "true" ]]; then
  DEBUG_FLAG=(--debug)
fi

mkdir -p /app/logs /app/keys

echo "[turnstile-solver] browser=${BROWSER_TYPE} thread=${THREAD} nice=${NICE} ${HOST}:${PORT}"
exec nice -n "${NICE}" python api_solver.py \
  --browser_type "${BROWSER_TYPE}" \
  --thread "${THREAD}" \
  --host "${HOST}" \
  --port "${PORT}" \
  "${DEBUG_FLAG[@]}"
