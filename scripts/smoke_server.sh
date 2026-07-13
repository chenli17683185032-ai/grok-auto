#!/usr/bin/env bash
set -euo pipefail
set +x

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${GROK2API_COMPOSE_FILE:-$ROOT_DIR/docker-compose.server.yml}"
ENV_FILE="${GROK2API_ENV_FILE:-$ROOT_DIR/.env}"
BASE_URL="${GROK2API_SMOKE_BASE_URL:-http://127.0.0.1:${GROK2API_BIND_PORT:-3000}}"
SMOKE_TIMEOUT_SEC="${GROK2API_SMOKE_TIMEOUT_SEC:-300}"
COMMAND_TIMEOUT_SEC="${GROK2API_SMOKE_COMMAND_TIMEOUT_SEC:-30}"

fail() {
  printf 'smoke: ERROR: %s\n' "$1" >&2
  exit 1
}

command -v timeout >/dev/null 2>&1 || fail "GNU timeout is required"
command -v curl >/dev/null 2>&1 || fail "curl is required"
[[ -f "$ENV_FILE" ]] || fail "environment file is missing"

run_bounded() {
  local seconds="$1"
  shift
  timeout --foreground "${seconds}s" "$@"
}

COMPOSE=(docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" --profile pipeline-v2)

deadline=$((SECONDS + SMOKE_TIMEOUT_SEC))
until run_bounded "$COMMAND_TIMEOUT_SEC" curl -fsS "$BASE_URL/" >/dev/null 2>&1; do
  (( SECONDS < deadline )) || fail "API readiness timed out"
  sleep 2
done

unauth_status="$(run_bounded "$COMMAND_TIMEOUT_SEC" curl -sS -o /dev/null -w '%{http_code}' \
  -H 'content-type: application/json' \
  --data-binary '{"model":"grok-4.5","messages":[{"role":"user","content":"smoke"}]}' \
  "$BASE_URL/v1/chat/completions")"
[[ "$unauth_status" == "401" ]] || fail "unauthenticated API request was not rejected"

admin_password="$(awk -F= '$1 == "GROK2API_ADMIN_PASSWORD" {sub(/^[^=]*=/, ""); print; exit}' "$ENV_FILE")"
[[ -n "$admin_password" ]] || fail "admin password is missing"
login_payload="$(ADMIN_PASSWORD="$admin_password" run_bounded "$COMMAND_TIMEOUT_SEC" python3 -c \
  'import json,os; print(json.dumps({"password":os.environ["ADMIN_PASSWORD"]}))')"
login_response="$(printf '%s' "$login_payload" | run_bounded "$COMMAND_TIMEOUT_SEC" \
  curl -fsS -H 'content-type: application/json' --data-binary @- "$BASE_URL/admin/api/login")"
admin_token="$(printf '%s' "$login_response" | run_bounded "$COMMAND_TIMEOUT_SEC" \
  python3 -c 'import json,sys; print(json.load(sys.stdin).get("token", ""))')"
unset admin_password login_payload login_response
[[ -n "$admin_token" ]] || fail "admin login did not return a session"
run_bounded "$COMMAND_TIMEOUT_SEC" curl -fsS \
  -H "Authorization: Bearer $admin_token" "$BASE_URL/admin/api/dashboard" >/dev/null
unset admin_token

for service in ruyipage-approver ruyipage-approver-2; do
  deadline=$((SECONDS + SMOKE_TIMEOUT_SEC))
  until run_bounded "$COMMAND_TIMEOUT_SEC" "${COMPOSE[@]}" exec -T "$service" \
    curl -fsS http://127.0.0.1:8765/health >/dev/null 2>&1; do
    (( SECONDS < deadline )) || fail "$service health timed out"
    sleep 2
  done
done

for proxy in grok-mihomo:7890 grok-mihomo-2:7890; do
  run_bounded "$COMMAND_TIMEOUT_SEC" "${COMPOSE[@]}" exec -T registration-producer \
    curl -fsS --proxy "http://$proxy" --max-time 20 \
    https://auth.x.ai/.well-known/openid-configuration >/dev/null
done

for service in registration-producer pending-recovery registration-mint-worker registration-mint-worker-2; do
  running="$(run_bounded "$COMMAND_TIMEOUT_SEC" "${COMPOSE[@]}" ps --status running --services "$service")"
  [[ "$running" == "$service" ]] || fail "$service is not running"
done

# Isolated synthetic queue: handoff, dual claims, and expired-lease recovery.
run_bounded "$COMMAND_TIMEOUT_SEC" "${COMPOSE[@]}" exec -T registration-producer python - <<'PY'
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from registration_jobs import JobState, RegistrationJob, new_job_id
from registration_queue import RegistrationQueue

with tempfile.TemporaryDirectory(prefix="grok-smoke-") as tmp:
    queue = RegistrationQueue(Path(tmp) / "queue.db")
    for index in range(2):
        queue.enqueue(RegistrationJob(
            job_id=new_job_id(), session_id=f"synthetic-{index}",
            route_id=f"route-{index + 1}", state=JobState.MINT_QUEUED.value,
        ))
    db_path = Path(tmp) / "queue.db"
    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = executor.map(
            lambda worker: RegistrationQueue(db_path).claim(worker, lease_sec=30),
            ("mint-1", "mint-2"),
        )
    assert first and second and first.job_id != second.job_id
    first.lease_until = time.time() - 1
    queue.save(first)
    recovered = queue.claim("mint-recovery", lease_sec=30)
    assert recovered and recovered.job_id == first.job_id
    assert recovered.lease_generation > first.lease_generation
PY

printf 'smoke: PASS api/auth/admin/sidecars/routes/queue/dual-mint/recovery\n'
