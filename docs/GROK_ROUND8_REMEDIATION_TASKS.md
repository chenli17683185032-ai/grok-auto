# Grok Round 8 Production Closure Tasks

Date: 2026-07-13

Status: Development Agent must implement; final acceptance belongs to the primary acceptance Agent.

Workspace:

```text
/Users/ethan/Documents/grok
```

## 1. Non-negotiable outcome

This round is not another report-only or helper-only pass. Close the real production paths so the server can run continuously and reproduce all important behavior from a clean Git checkout.

The account lifecycle rules are:

```text
Each Grok account has 1,000,000 tokens in a rolling 24-hour window.
Quota exhaustion is not account death.
Quota exhaustion removes the account from request and model-health rotation, while token refresh continues.
At reset time, the service runs a real Grok Build free-usage probe.
Confirmed recovery returns the account to rotation.
Inconclusive network/5xx results never recover or destroy an account.
Repeated explicit exhaustion after reset enters grace, then quota_reset_failed.
Only a separately confirmed terminal state may become a cleanup candidate.
Manual disable, quota wait, model-only block, credential suspension, and refresh failure are independent states.
```

Do not claim completion until the production default path, not only an injected test callback, passes end to end.

## 2. Authoritative task and output locations

This file is the only Round 8 task specification:

```text
/Users/ethan/Documents/grok/docs/GROK_ROUND8_REMEDIATION_TASKS.md
```

Modify the existing implementation in place. Do not create a second service or parallel replacement tree.

New regression tests go only under:

```text
/Users/ethan/Documents/grok/tests/
```

Deployment helper scripts go under:

```text
/Users/ethan/Documents/grok/scripts/
```

Maintain one Round 8 implementation report:

```text
/Users/ethan/Documents/grok/test-output/acceptance-fix-round8.md
```

Update the existing deployment manual in place:

```text
/Users/ethan/Documents/grok/SERVER_DEPLOYMENT.md
```

Do not create additional `round8-final-v2`, `notes`, `results-copy`, or similarly duplicated reports.

The primary acceptance Agent will perform final acceptance. The development Agent must not self-approve, deploy to the Yunbei server, modify the live account pool, or update the Yunbei operations record in this round.

## 3. Working-tree and secret constraints

1. Inspect the current dirty tree before editing. Never reset, revert, or overwrite changes that already exist.
2. Never read or print real token, SSO, API key, password, proxy credential, cookie, device code, or refresh token values.
3. Tests must use synthetic credentials and temporary directories/databases.
4. Do not use real accounts or real upstream calls in unit tests.
5. Do not weaken assertions to match broken behavior.
6. Do not add sleeps to hide races. Use controllable clocks, events, barriers, and bounded polling.
7. No server deployment or NewAPI mutation before primary acceptance.
8. Local Docker build/smoke is allowed and required when a local daemon is available.

## 4. P0: Implement the real 1M free-usage probe

The current production re-probe calls `/billing`, which is a monthly USD endpoint and cannot prove recovery of the rolling 1M Token allowance. Replace that default boundary.

Implement a structured production function in `quota.py`, for example:

```python
probe_free_usage_for_creds(creds, *, proxy=None, timeout=...)
```

Requirements:

1. Use the Grok Build endpoint `POST https://cli-chat-proxy.grok.com/v1/responses` or the existing canonical `UPSTREAM_BASE` only when it resolves to that Build endpoint.
2. Reuse `upstream_headers()` and current CLI identity headers. Never log Authorization or cookies.
3. Send the smallest deterministic request that proves serving ability, with bounded output tokens.
4. Parse at least:
   - `x-ratelimit-limit-tokens`
   - `x-ratelimit-remaining-tokens`
   - token reset headers
   - explicit `subscription:free-usage-exhausted`
   - `tokens (actual/limit): actual/limit`
5. Return one stable structure containing `ok`, `free_usage_ok`, `exhausted`, `inconclusive`, `status_code`, `limit_tokens`, `remaining_tokens`, `actual_tokens`, `reset_at`, and a bounded redacted error category.
6. A successful response proves `free_usage_ok=True`, even if optional quota headers are absent.
7. Explicit exhaustion proves `exhausted=True` and `free_usage_ok=False`.
8. Generic 429 without the explicit free-usage signal is inconclusive/rate-limited, not 24-hour exhaustion.
9. 5xx, timeout, DNS, proxy, malformed response, or missing headers are inconclusive. They must not recover, suspend, or delete the account.
10. 401/403 credential errors go to the credential classifier, not quota waiting.
11. Use an explicit per-request proxy without mutating process environment variables.
12. Do not import the CLI script as a runtime module. Reuse its verified parsing behavior in production code with structured APIs and tests.

Wire `account_pool.process_quota_probe_due()` to this function in its default production path. Remove the second redundant call to `maybe_disable_from_quota_result()`.

Required tests:

```text
test_real_default_quota_reprobe_uses_responses_not_billing
test_successful_free_usage_probe_recovers_waiting_account
test_explicit_free_usage_exhaustion_remains_waiting
test_generic_429_is_inconclusive_not_daily_exhaustion
test_probe_5xx_does_not_recover_or_increment_terminal_confirmation
test_probe_timeout_does_not_recover_or_suspend
test_free_usage_probe_never_logs_authorization
test_free_usage_probe_uses_explicit_route_proxy
```

The first test must execute the default `process_quota_probe_due()` path with network mocked at the HTTP boundary. It may not pass a custom `probe_fn`.

## 5. P0: Make quota waiting idempotent and stop all premature polling

`mark_quota_waiting()` must create a quota cycle once. Repeated observations in the same cycle must not move the reset time forward.

Requirements:

1. On the first exhaustion, persist:
   - `quota_cycle_id`
   - `quota_waiting_since`
   - `quota_reset_at`
   - `quota_next_probe_at`
   - `quota_limit_tokens=1000000`
   - `quota_remaining_tokens=0`
   - `quota_grace_count=0`
   - `quota_confirmation_count=0`
2. If the account is already waiting, preserve the original cycle start and reset time. An authoritative reset header may update the time only through a documented, bounded rule; a fallback `now+24h` must never overwrite an existing reset.
3. `acquire()` and `try_acquire_sequence()` must never return waiting, reset-failed, manually disabled, suspended, or refresh-terminal accounts.
4. `model_health._unique_live_creds()` and all automatic model-health batches must exclude quota waiting, quota reset failed, manual disabled, and credential suspended accounts.
5. A quota error must never create a model-specific block. Classify quota before model availability.
6. On confirmed quota recovery, remove only quota-owned state and quota-owned model blocks. Preserve manual and unrelated model blocks.
7. Token refresh must continue for waiting accounts.
8. Pool status must separately report `active`, `quota_waiting`, `quota_grace`, `quota_reset_failed`, `manual_disabled`, `credential_suspended`, `refresh_pending`, and `refresh_terminal`. Waiting accounts must not inflate `enabled/available` counts.

Required tests:

```text
test_repeated_exhaustion_does_not_extend_existing_reset
test_model_health_skips_quota_waiting_accounts
test_quota_exhaustion_never_creates_model_block
test_quota_recovery_clears_only_quota_owned_state
test_pool_summary_does_not_count_waiting_as_available
test_all_waiting_request_path_returns_no_account
```

## 6. P0: Complete reset, grace, and terminal cleanup semantics

Implement the production lifecycle:

```text
active
  -> quota_waiting
  -> quota_probe_due
  -> active                    # real probe succeeded
  -> quota_grace               # explicit exhaustion after reset
  -> quota_reset_failed        # repeated explicit confirmations
  -> terminal_cleanup_candidate
```

Requirements:

1. Only an explicit free-usage exhaustion result after `quota_reset_at` increments `quota_confirmation_count` and `quota_grace_count`.
2. Network errors, generic 429, 5xx, or parse errors only schedule a bounded retry and do not increment terminal evidence.
3. Require at least 3 explicit post-reset exhaustion confirmations across at least 2 separate maintenance cycles and a configurable grace window.
4. Persist first/last confirmation timestamps and last explicit evidence category.
5. `quota_reset_failed` must stop normal/model-health requests but remain refreshable.
6. Define a guarded cleanup path. `_cleanup_reason()` may return `quota_reset_failed` only when all confirmation and age fields satisfy the gate.
7. Cleanup remains dry-run by default. A non-dry-run deletion must still use the existing observation window and confirmation count.
8. Successful recovery clears reset/grace/confirmation fields so the next 24-hour cycle starts at zero.
9. A failed cleanup request must retain all credentials and evidence for retry.

Required tests:

```text
test_inconclusive_probe_does_not_increment_quota_grace
test_three_explicit_post_reset_exhaustions_reach_reset_failed
test_quota_reset_failed_not_cleanup_ready_before_grace
test_quota_reset_failed_becomes_cleanup_candidate_after_all_gates
test_quota_recovery_resets_grace_for_next_cycle
test_second_quota_cycle_starts_with_zero_confirmations
test_cleanup_failure_preserves_account_and_evidence
```

## 7. P0: Separate manual, quota, credential, model, and refresh states

Create one explicit error-classification order used by normal OpenAI requests, Anthropic requests, streaming paths, and model probes:

```text
free-usage quota
credential/account suspended or revoked
authentication refresh issue
model-specific unavailable
temporary rate/network error
```

Requirements:

1. `set_account_enabled(False)` sets `manual_disabled=True`; enabling clears that flag only through the explicit admin action.
2. Quota mark/clear never changes a manually disabled decision.
3. `account_suspended`, `user_blocked`, `account_disabled`, and equivalent hard identity errors enter `credential_suspended` from normal API paths as well as model probes.
4. `run out of credits`, `out of credits`, `usage_limit_reached`, and `usage_pool_exhausted` must not automatically become credential suspension. Classify them using quota evidence.
5. `set_account_enabled(True)` must define and test whether it explicitly clears `credential_suspended`; never leave `enabled=True` with contradictory terminal metadata.
6. Stream and non-stream OpenAI/Anthropic paths pass response headers to the shared classifier.
7. Parse numeric epoch seconds, epoch milliseconds, relative reset seconds, and RFC HTTP-date `Retry-After`.

Required tests:

```text
test_manual_disable_survives_quota_wait_and_recovery
test_manual_enable_clears_contradictory_suspend_state_atomically
test_normal_request_account_suspended_marks_credential_suspended
test_quota_phrases_do_not_mark_credential_suspended
test_openai_stream_passes_quota_reset_headers
test_anthropic_stream_passes_quota_reset_headers
test_retry_after_http_date_is_parsed
```

## 8. P0: Make refresh failure a confirmed lifecycle, not one-shot death

The first `invalid_grant` must not create an immediately permanent cleanup marker while the Access Token is still usable.

Implement fields such as:

```text
refresh_status
refresh_failure_count
refresh_first_failed_at
refresh_last_failed_at
refresh_next_retry_at
refresh_terminal_at
```

Requirements:

1. First failure enters `refresh_pending_confirmation`; keep an unexpired Access Token usable.
2. Retry with bounded backoff across separate maintenance cycles.
3. Require at least 3 independent definitive failures, including at least one attempt after Access Token expiry, before `refresh_terminal`.
4. Transient network/5xx errors never count as definitive invalid-grant confirmations.
5. A later success clears all failure evidence.
6. Producer effective counts exclude only accounts that cannot currently serve; do not prematurely trigger replacement/deletion from one proactive failure.
7. Cleanup uses `refresh_terminal`, not legacy one-shot `refresh_invalid`, and retains the existing age/observation gates.
8. Migrate legacy `refresh_invalid` safely: do not automatically delete it without a new post-migration confirmation.

Required tests:

```text
test_first_invalid_grant_keeps_unexpired_access_usable
test_refresh_retried_on_later_maintenance_cycle
test_refresh_not_terminal_before_access_expiry_confirmation
test_three_definitive_refresh_failures_reach_terminal
test_transient_refresh_error_not_counted_as_definitive
test_refresh_success_clears_failure_evidence
test_legacy_refresh_invalid_requires_new_confirmation
```

## 9. P0: Enforce one active Queue Job per registration session

Requirements:

1. Add a SQLite uniqueness guarantee for one active Job per `session_id`, preferably a partial unique index covering non-terminal states.
2. Add a safe migration for existing databases. Resolve existing active duplicates deterministically while preserving the newest valid owner and all recovery material.
3. `enqueue()` must be idempotent: when an active Job already exists, return that Job or a typed duplicate result instead of creating another Job.
4. The active-session check and insert must occur in one `BEGIN IMMEDIATE` transaction.
5. Terminal history may remain, but a new active Job must not race with another insert.

Required tests:

```text
test_session_active_partial_unique_constraint
test_concurrent_same_session_enqueue_returns_one_job
test_two_workers_cannot_claim_same_session_via_duplicate_jobs
test_existing_duplicate_migration_preserves_recovery_material
test_terminal_session_history_allows_intentional_new_job
```

Use true multiprocessing for the concurrent test.

## 10. P1: Continuous lease heartbeat and two real Mint workers

Requirements:

1. Add a scoped background lease heartbeat around browser approval, Token Poll, and Model Probe.
2. Heartbeat cadence must be safely below the lease duration and stop on completion.
3. Losing owner/generation stops the old worker immediately before further upstream or import operations.
4. No heartbeat thread may survive the Job.
5. Run two actual Mint consumers so both ruyiPage Sidecars can process work concurrently. Use either a bounded two-worker pool with distinct worker IDs or two Compose services; do not rely on comments or unsupported Swarm-only fields.
6. Keep concurrency configurable and default it to the verified server value of 2.

Required tests:

```text
test_heartbeat_runs_during_long_token_flow
test_heartbeat_runs_during_long_probe
test_heartbeat_loss_aborts_before_import
test_no_heartbeat_thread_leak_after_job
test_two_mint_workers_process_two_jobs_concurrently
test_multiprocess_claim_exactly_once_8_processes_1000_jobs
test_concurrent_enqueue_hard_limit_8_processes
```

The last two tests must actually use 8 OS processes and the stated workload. Do not name a thread test `multiprocess`.

## 11. P0: Close credential and API disclosure paths

Requirements:

1. Run credential permission migration during application startup before serving requests.
2. Create and atomically replace auth, settings, API key, Pending SSO, Cookie Bundle, Queue, and Metrics files with `0600`; directories must be `0700`.
3. Migrate existing files/directories without reading or printing their contents.
4. `/health` returns counts and component state only, never email, auth key, expiry tied to identity, or token hints.
5. Public status routes return no account identity. Sensitive admin status must require admin authentication.
6. OpenAI/Anthropic responses must not contain `x_grok2api_account` or internal account IDs.
7. OIDC session output must never return `device_code`, `user_code`, Access Token, Refresh Token, SSO, cookies, or authorization URLs containing secrets.
8. Batch/Session sanitization must include `device_code`, `user_code`, passwords, proxy credentials, and secret-bearing free text.
9. Debug logging may contain status, hop count, and error category only. Never print Set-Cookie URLs, JWT/SSO prefixes, headers, or query parameters.
10. Corrupt `settings.json` must fail closed. `/setup` is allowed only for a genuinely absent first-run file and must not reopen because persisted JSON is malformed.
11. Default API authentication must be fail-closed. Local open development mode requires an explicit opt-out environment variable.

Required tests:

```text
test_startup_migrates_existing_secret_permissions
test_new_settings_and_key_files_are_0600
test_secret_directories_are_0700
test_health_contains_no_account_identity
test_public_status_contains_no_credentials_email
test_client_response_contains_no_internal_account
test_oidc_output_tail_redacts_all_device_and_token_secrets
test_batch_redacts_device_code_and_secret_free_text
test_debug_mode_never_prints_set_cookie_or_jwt
test_corrupt_settings_fails_closed_without_reopening_setup
test_api_auth_defaults_fail_closed
```

## 12. P1: Bound 24-hour storage growth

Requirements:

1. Add configurable retention for terminal Queue Jobs and Metrics Events.
2. Run cleanup periodically from one designated maintenance owner. Do not make every API request prune databases.
3. Add Cookie Bundle TTL sweeping to production, not only tests.
4. Add Docker JSON log rotation to every service, with explicit `max-size` and `max-file`.
5. Keep cleanup bounded per cycle and avoid long SQLite locks.
6. Report last cleanup time and removed counts without secrets.
7. Provide safe defaults appropriate for continuous operation, such as 7 days for terminal/metrics history and 48 hours for abandoned bundles, unless existing requirements justify another value.

Required tests:

```text
test_terminal_queue_retention_is_bounded
test_metrics_retention_is_bounded
test_cookie_sweeper_runs_from_production_maintenance
test_retention_does_not_delete_open_jobs
test_compose_all_services_have_log_rotation
```

## 13. P0: Make the repository deliverable from a clean commit

The current system exists largely as untracked files. This is a release blocker.

Requirements:

1. Add all intended source, Compose, runtime, documentation, scripts, and tests to Git.
2. Never add `.env`, `data/`, `test-output/`, real credentials, virtual environments, browser caches, generated databases, or decoded secrets.
3. Extend `.gitignore` and `.dockerignore` for `.venv*`, `.venv_sys`, `test-output`, `artifacts`, `_compare_grok_register`, caches, and other local-only output.
4. Create a local `codex/round8-remediation` branch if a branch is needed. Do not push.
5. Create one local commit containing only reviewed delivery files. Do not include unrelated user files.
6. Validate with a clean checkout or `git archive` of that commit, not the dirty source tree.
7. The clean tree must contain at minimum:
   - `docker-compose.server.yml`
   - `SERVER_DEPLOYMENT.md`
   - Queue/Controller/Producer/Metrics modules
   - Sidecar runtime and Dockerfile
   - Round 8 tests
   - preflight/smoke/rollback scripts

Required clean-tree commands:

```bash
python -m venv /tmp/grok-round8-clean-venv
/tmp/grok-round8-clean-venv/bin/pip install -r requirements.txt
/tmp/grok-round8-clean-venv/bin/python -m unittest discover -v
docker compose -f docker-compose.server.yml config -q
docker compose -f docker-compose.server.yml --profile pipeline-v2 config -q
```

## 14. P1: Deployment preflight, smoke, rollback, and documentation

Create and document:

```text
scripts/server_preflight.sh
scripts/smoke_server.sh
scripts/rollback_server.sh
```

Requirements:

1. Preflight validates required environment values without printing them.
2. Validate both mihomo config directories and configuration syntax.
3. Validate/create the external Docker network explicitly.
4. Validate free disk, required bind mounts, permissions, Compose version, and image architecture.
5. Every wait has a timeout and exits nonzero on failure. Nothing may hang waiting for manual input.
6. Smoke validates API readiness, forced API-key rejection, admin authentication, both sidecars, proxy routes, Queue handoff, two-worker Mint concurrency, and restart recovery using synthetic/mock jobs only.
7. Rollback recreates every container affected by changed environment variables and verifies the old pipeline behavior after rollback.
8. Use versioned image tags or digests and document data backup/restore before schema migration.
9. Update `SERVER_DEPLOYMENT.md` to one consistent topology and resource table:
   - default: 7 services, about 2.45 CPU / 4144 MiB
   - pipeline-v2: 8 services, about 2.50 CPU / 4240 MiB
10. User already permits the 2.50 CPU profile. Remove stale 2.35/2.40 claims.
11. Document the quota state machine, refresh state machine, retention, observability, and exact rollback commands.

If Docker/OrbStack is available, run local build and smoke. If the daemon is unavailable, do not claim Docker PASS; record the exact blocker and leave deployment status blocked for primary acceptance.

## 15. Mandatory verification matrix

All of the following must pass from the clean committed tree:

```text
1. Full unittest discovery
2. tests/ discovery
3. py_compile for all modified production modules
4. git diff --check / clean commit check
5. Compose default config
6. Compose pipeline-v2 config
7. Real 8-process/1000-Job exactly-once test
8. Real concurrent hard-limit test
9. Default quota re-probe HTTP-boundary tests
10. State-cycle tests covering two full quota cycles
11. Refresh confirmation tests covering multiple maintenance cycles
12. Security redaction and filesystem-mode tests
13. Retention tests
14. Clean checkout presence/build checks
15. Docker build/up/smoke when daemon is available
```

Do not count a test as covering production behavior when it passes only by injecting a custom callback that the production service never uses.

## 16. Round 8 report format

Update only:

```text
/Users/ethan/Documents/grok/test-output/acceptance-fix-round8.md
```

The report must contain:

1. Exact modified files.
2. State transition table.
3. Exact production function used for the 1M probe.
4. Exact HTTP boundary mocked by tests.
5. Test commands, counts, exit codes, and environment path.
6. Clean commit hash and clean-checkout path.
7. Compose expanded service/resource totals.
8. Docker build/smoke results or exact blocker.
9. Security permission modes checked without secret contents.
10. Remaining risks. Do not claim live KPI or server deployment unless actually performed later under separate approval.

## 17. Definition of done

The development Agent may say `ready for acceptance` only when:

1. The default background path probes the real 1M allowance and can recover an account.
2. Waiting accounts receive no normal or Model Health traffic before reset.
3. Repeated observations cannot extend the same reset indefinitely.
4. Quota, manual, credential, model, and refresh states remain independent.
5. Refresh terminal requires repeated confirmation after expiry.
6. One Session cannot have two active Queue Jobs.
7. Long operations continuously renew their lease.
8. Two Mint jobs can execute concurrently.
9. Secret files and outputs are closed.
10. Retention prevents unbounded disk growth.
11. A clean Git commit contains the full deployable system.
12. Required local tests pass from that clean commit.
13. The primary acceptance Agent, not the development Agent, gives the final approval.
