#!/usr/bin/env bash
set -euo pipefail
set +x

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${GROK2API_COMPOSE_FILE:-$ROOT_DIR/docker-compose.server.yml}"
ENV_FILE="${GROK2API_ENV_FILE:-$ROOT_DIR/.env}"
BACKUP_DIR="${GROK2API_ROLLBACK_BACKUP_DIR:-${1:-}}"
COMMAND_TIMEOUT_SEC="${GROK2API_ROLLBACK_COMMAND_TIMEOUT_SEC:-180}"

fail() {
  printf 'rollback: ERROR: %s\n' "$1" >&2
  exit 1
}

command -v timeout >/dev/null 2>&1 || fail "GNU timeout is required"
[[ -f "$ENV_FILE" ]] || fail "environment file is missing"

run_bounded() {
  local seconds="$1"
  shift
  timeout --foreground "${seconds}s" "$@"
}

env_value() {
  local name="$1"
  awk -F= -v key="$name" '
    $0 !~ /^[[:space:]]*#/ && $1 == key {
      sub(/^[^=]*=/, ""); print; exit
    }
  ' "$ENV_FILE"
}

COMPOSE=(docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" --profile pipeline-v2)

run_bounded "$COMMAND_TIMEOUT_SEC" "${COMPOSE[@]}" stop \
  registration-mint-worker registration-mint-worker-2

if [[ -n "$BACKUP_DIR" ]]; then
  [[ -d "$BACKUP_DIR" ]] || fail "rollback backup directory does not exist"
  DATA_DIR="${GROK2API_DATA_DIR:-$ROOT_DIR/data}"
  mkdir -p "$DATA_DIR"
  chmod 700 "$DATA_DIR"
  run_bounded "$COMMAND_TIMEOUT_SEC" "${COMPOSE[@]}" stop \
    registration-producer pending-recovery grokcli-2api
  for name in registration_queue.db registration_metrics.db; do
    backup_file="$BACKUP_DIR/$name"
    if [[ -f "$backup_file" ]]; then
      rm -f "$DATA_DIR/$name" "$DATA_DIR/${name}-wal" \
        "$DATA_DIR/${name}-shm" "$DATA_DIR/${name}-journal"
      cp -p "$backup_file" "$DATA_DIR/$name"
      chmod 600 "$DATA_DIR/$name"
      # Backward compatibility for older physical DB backups that included WAL.
      for suffix in '-wal' '-shm'; do
        if [[ -f "$BACKUP_DIR/${name}${suffix}" ]]; then
          cp -p "$BACKUP_DIR/${name}${suffix}" "$DATA_DIR/${name}${suffix}"
          chmod 600 "$DATA_DIR/${name}${suffix}"
        fi
      done
    fi
  done
  if [[ -f "$BACKUP_DIR/deployment.env" ]]; then
    cp -p "$BACKUP_DIR/deployment.env" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
  fi
fi

file_app_image="$(env_value GROK2API_APP_IMAGE)"
file_ruyi_image="$(env_value GROK2API_RUYIPAGE_IMAGE)"
file_mihomo_image="$(env_value GROK2API_MIHOMO_IMAGE)"
export GROK2API_APP_IMAGE="${GROK2API_ROLLBACK_APP_IMAGE:-${file_app_image:-${GROK2API_APP_IMAGE:-grokcli-2api:2026.07.13-round8}}}"
export GROK2API_RUYIPAGE_IMAGE="${GROK2API_ROLLBACK_RUYIPAGE_IMAGE:-${file_ruyi_image:-${GROK2API_RUYIPAGE_IMAGE:-ruyipage-headless:2026.07.13-round8}}}"
export GROK2API_MIHOMO_IMAGE="${GROK2API_ROLLBACK_MIHOMO_IMAGE:-${file_mihomo_image:-${GROK2API_MIHOMO_IMAGE:-metacubex/mihomo:v1.19.28}}}"
for image in "$GROK2API_APP_IMAGE" "$GROK2API_RUYIPAGE_IMAGE" "$GROK2API_MIHOMO_IMAGE"; do
  [[ "$image" != *:latest ]] || fail "rollback image must use a versioned tag"
done

run_bounded "$COMMAND_TIMEOUT_SEC" "${COMPOSE[@]}" up -d --force-recreate \
  grok-mihomo grok-mihomo-2 grokcli-2api \
  ruyipage-approver ruyipage-approver-2 \
  registration-producer pending-recovery \
  registration-mint-worker registration-mint-worker-2

run_bounded "${GROK2API_ROLLBACK_SMOKE_TIMEOUT_SEC:-360}" \
  "$ROOT_DIR/scripts/smoke_server.sh"
printf 'rollback: PASS restored versioned services and bounded smoke\n'
