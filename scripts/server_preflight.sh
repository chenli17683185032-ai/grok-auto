#!/usr/bin/env bash
set -euo pipefail
set +x
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${GROK2API_COMPOSE_FILE:-$ROOT_DIR/docker-compose.server.yml}"
ENV_FILE="${GROK2API_ENV_FILE:-$ROOT_DIR/.env}"
COMMAND_TIMEOUT_SEC="${GROK2API_PREFLIGHT_COMMAND_TIMEOUT_SEC:-120}"
BUILD_TIMEOUT_SEC="${GROK2API_PREFLIGHT_BUILD_TIMEOUT_SEC:-1200}"

fail() {
  printf 'preflight: ERROR: %s\n' "$1" >&2
  exit 1
}

command -v timeout >/dev/null 2>&1 || fail "GNU timeout is required"
command -v docker >/dev/null 2>&1 || fail "docker is required"
command -v python3 >/dev/null 2>&1 || fail "python3 is required"

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

require_env_name() {
  local name="$1"
  local value
  value="$(env_value "$name")"
  [[ -n "$value" ]] || fail "required variable $name is missing or empty"
  printf 'preflight: required variable %s is present\n' "$name"
}

[[ -f "$COMPOSE_FILE" ]] || fail "compose file is missing"
[[ -f "$ENV_FILE" ]] || fail "environment file is missing"
env_mode="$(stat -c '%a' "$ENV_FILE")"
(( 8#$env_mode <= 8#600 )) || fail "environment file must be mode 0600 or stricter"

for required in \
  GROK2API_ADMIN_PASSWORD \
  GROK2API_YESCAPTCHA_KEY; do
  require_env_name "$required"
done

mail_provider="$(env_value GROK2API_MAIL_PROVIDER)"
mail_provider="${mail_provider:-moemail}"
mail_provider="$(printf '%s' "$mail_provider" | sed "s/^[\"']//; s/[\"']$//" | tr '[:upper:]' '[:lower:]')"
case "$mail_provider" in
  moemail)
    require_env_name GROK2API_MOEMAIL_API_KEY
    require_env_name GROK2API_MOEMAIL_BASE_URL
    require_env_name GROK2API_MOEMAIL_DOMAIN
    ;;
  yyds|yydsmail)
    require_env_name GROK2API_YYDSMAIL_API_KEY
    require_env_name GROK2API_YYDSMAIL_BASE_URL
    # GROK2API_YYDSMAIL_DOMAIN is optional; empty lets YYDS choose a domain.
    ;;
  *)
    fail "GROK2API_MAIL_PROVIDER must be moemail or yyds"
    ;;
esac
printf 'preflight: mail provider %s selected\n' "$mail_provider"

if [[ "${GROK2API_PREFLIGHT_PIPELINE_V2:-1}" == "1" ]]; then
  [[ "$(env_value GROK2API_PIPELINE_V2)" == "1" ]] || fail "GROK2API_PIPELINE_V2 must be 1"
  [[ "$(env_value GROK2API_ROUTE_STICKY)" == "1" ]] || fail "GROK2API_ROUTE_STICKY must be 1"
fi

available_kb="$(awk '/MemAvailable:/ {print $2; exit}' /proc/meminfo)"
cpu_count="$(getconf _NPROCESSORS_ONLN)"
disk_kb="$(df -Pk "$ROOT_DIR" | awk 'NR==2 {print $4}')"
(( available_kb >= 5 * 1024 * 1024 )) || fail "less than 5 GiB memory is available"
(( cpu_count >= 4 )) || fail "fewer than 4 logical CPUs are available"
(( disk_kb >= 5 * 1024 * 1024 )) || fail "less than 5 GiB disk space is available"

DATA_DIR="${GROK2API_DATA_DIR:-$ROOT_DIR/data}"
mkdir -p "$DATA_DIR/backups"
chmod 700 "$DATA_DIR" "$DATA_DIR/backups"
backup_dir="$DATA_DIR/backups/preflight-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -m 700 "$backup_dir"
for name in registration_queue.db registration_metrics.db; do
  source_path="$DATA_DIR/$name"
  target_path="$backup_dir/$name"
  if [[ -f "$source_path" ]]; then
    run_bounded "$COMMAND_TIMEOUT_SEC" env \
      SOURCE_DB="$source_path" TARGET_DB="$target_path" python3 - <<'PY'
import os
import sqlite3

source = sqlite3.connect(os.environ["SOURCE_DB"], timeout=30)
target = sqlite3.connect(os.environ["TARGET_DB"], timeout=30)
try:
    source.backup(target)
finally:
    target.close()
    source.close()
PY
    chmod 600 "$target_path"
  fi
done
cp -p "$ENV_FILE" "$backup_dir/deployment.env"
chmod 600 "$backup_dir/deployment.env"
printf 'preflight: database/config backup created: %s\n' "$backup_dir"

network_name="${GROK2API_NEW_API_NETWORK:-$(env_value GROK2API_NEW_API_NETWORK)}"
network_name="${network_name:-app_yunbay-network}"
if ! run_bounded "$COMMAND_TIMEOUT_SEC" docker network inspect "$network_name" >/dev/null 2>&1; then
  run_bounded "$COMMAND_TIMEOUT_SEC" docker network create "$network_name" >/dev/null
  printf 'preflight: external network created: %s\n' "$network_name"
fi

mihomo_one="${GROK2API_MIHOMO_CONFIG_DIR:-$(env_value GROK2API_MIHOMO_CONFIG_DIR)}"
mihomo_two="${GROK2API_MIHOMO2_CONFIG_DIR:-$(env_value GROK2API_MIHOMO2_CONFIG_DIR)}"
mihomo_one="${mihomo_one:-/opt/new-api/mihomo}"
mihomo_two="${mihomo_two:-$ROOT_DIR/mihomo-2}"
for config_dir in "$mihomo_one" "$mihomo_two"; do
  [[ -d "$config_dir" ]] || fail "mihomo config directory is missing"
  [[ -f "$config_dir/config.yaml" ]] || fail "mihomo config.yaml is missing"
done

mihomo_image="${GROK2API_MIHOMO_IMAGE:-$(env_value GROK2API_MIHOMO_IMAGE)}"
mihomo_image="${mihomo_image:-metacubex/mihomo:v1.19.28}"
[[ "$mihomo_image" != *:latest ]] || fail "mihomo image must use a versioned tag"
run_bounded "$BUILD_TIMEOUT_SEC" docker pull "$mihomo_image" >/dev/null
for config_dir in "$mihomo_one" "$mihomo_two"; do
  run_bounded "$COMMAND_TIMEOUT_SEC" docker run --rm \
    -v "$config_dir:/root/.config/mihomo:ro" \
    "$mihomo_image" -d /root/.config/mihomo -t >/dev/null
done

run_bounded "$COMMAND_TIMEOUT_SEC" docker compose version >/dev/null
run_bounded "$COMMAND_TIMEOUT_SEC" docker compose \
  --env-file "$ENV_FILE" -f "$COMPOSE_FILE" config -q
run_bounded "$COMMAND_TIMEOUT_SEC" docker compose \
  --env-file "$ENV_FILE" -f "$COMPOSE_FILE" --profile pipeline-v2 config -q
run_bounded "$BUILD_TIMEOUT_SEC" docker compose \
  --env-file "$ENV_FILE" -f "$COMPOSE_FILE" --profile pipeline-v2 \
  build grokcli-2api ruyipage-approver

app_image="${GROK2API_APP_IMAGE:-$(env_value GROK2API_APP_IMAGE)}"
ruyi_image="${GROK2API_RUYIPAGE_IMAGE:-$(env_value GROK2API_RUYIPAGE_IMAGE)}"
app_image="${app_image:-grokcli-2api:2026.07.13-round8}"
ruyi_image="${ruyi_image:-ruyipage-headless:2026.07.13-round8}"
for image in "$app_image" "$ruyi_image" "$mihomo_image"; do
  [[ "$image" != *:latest ]] || fail "all images must use versioned tags"
  architecture="$(run_bounded "$COMMAND_TIMEOUT_SEC" docker image inspect \
    --format '{{.Architecture}}' "$image")"
  [[ "$architecture" == "amd64" ]] || fail "image architecture must be amd64"
done

printf 'preflight: PASS cpu=%s mem_available_mib=%s disk_available_mib=%s\n' \
  "$cpu_count" "$((available_kb / 1024))" "$((disk_kb / 1024))"
