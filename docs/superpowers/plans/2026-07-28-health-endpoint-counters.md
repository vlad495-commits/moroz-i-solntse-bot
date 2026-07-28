# Safe Health Endpoint and Real Counters Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:test-driven-development` task-by-task and
> `superpowers:verification-before-completion` before every completion claim.

**Goal:** Close the local launch blocker by adding a minimal public `/healthz`
and owner-only metrics derived from real PostgreSQL, Redis, and RabbitMQ state.

**Architecture:** The bot owns the public readiness endpoint and probes only its
critical PostgreSQL dependency. The admin builds a fresh metrics registry for
each authenticated scrape: durable counters come from PostgreSQL, Redis is
pinged, and live queue/DLQ depths come from RabbitMQ's existing internal
Prometheus endpoint. Caddy exposes only the exact health path; detailed metrics stay
under the existing `/admin` security boundary.

**Tech stack:** Python 3.12, FastAPI 0.136, asyncpg, redis-py, httpx, RabbitMQ
Prometheus endpoint, Caddy, Docker Compose, pytest.

**Constraints:** Docker only; no new service or project dependency; no
staging/production/provider/YCLIENTS/Telegram mutation; no merge or push; no PII
or secrets in public responses, metrics, labels, or logs.

---

## Task 1: Public health contract

**Files:**

- Create: `project/tests/unit/test_health_endpoint.py`
- Modify: `project/llm/webhook.py`

### Step 1: Write failing route tests

Add async ASGI tests proving:

- ready database returns exactly `200 {"status":"ok"}`;
- a failed or timed-out database probe returns exactly
  `503 {"status":"unavailable"}`;
- failure output contains no exception text, DSN, component name, or stack
  details;
- no Telegram, LLM, YCLIENTS, Redis, or RabbitMQ call is needed.

Use a small fake database attached to `app.state`; do not start real providers.

### Step 2: Run Docker RED

Run:

```powershell
docker compose --env-file ../.env -p moroz_health_counters --profile test run --rm --build test pytest -q tests/unit/test_health_endpoint.py
```

Expected: FAIL because `/healthz` does not exist.

### Step 3: Implement the minimum route

- retain the connected `Database` instance in `webhook_app.state`;
- add a bounded PostgreSQL `SELECT 1` readiness helper;
- add `GET /healthz`;
- return only the two fixed response bodies and status codes;
- log only the safe exception type on probe failure.

### Step 4: Run Docker GREEN

Repeat the focused command. Expected: all focused tests pass.

### Step 5: Commit

```bash
git add project/llm/webhook.py project/tests/unit/test_health_endpoint.py changelog.md
git commit -m "feat: добавлен безопасный health endpoint"
```

## Task 2: Caddy and Compose health wiring

**Files:**

- Modify: `project/ops/Caddyfile`
- Modify: `project/ops/staging/Caddyfile`
- Modify: `project/docker-compose.yml`
- Modify: `project/docker-compose.staging.yml`
- Modify: `project/tests/unit/ops/test_validate_env.py`
- Modify: `project/tests/unit/test_staging.py`

### Step 1: Write failing configuration tests

Require:

- exact `/healthz` Caddy matcher proxied to `bot:8081`;
- no wildcard public health route;
- base and staging bot healthchecks probe
  `http://127.0.0.1:8081/healthz`;
- `/openapi.json` and `/proc/1/cmdline` are not used as bot readiness.

### Step 2: Run Docker RED

Run only the new/changed config tests through the test service. Expected: fail
on missing Caddy route and old healthcheck commands.

### Step 3: Implement minimal wiring

Add the exact Caddy route in production and staging files, and replace the two
bot healthcheck URLs. Do not expose any additional paths.

### Step 4: Run Docker GREEN and Compose renders

Run focused tests, then:

```powershell
docker compose --env-file ../.env -p moroz_health_counters config --quiet
docker compose --env-file ../.env -p moroz_health_counters -f docker-compose.yml -f docker-compose.prod.yml config --quiet
docker compose --env-file ../.env -p moroz_health_counters -f docker-compose.yml -f docker-compose.staging.yml config --quiet
```

### Step 5: Commit

```bash
git add project/ops/Caddyfile project/ops/staging/Caddyfile project/docker-compose.yml project/docker-compose.staging.yml project/tests/unit/ops/test_validate_env.py project/tests/unit/test_staging.py changelog.md
git commit -m "ops: health endpoint подключен к Caddy и Compose"
```

## Task 3: Real PostgreSQL system counters

**Files:**

- Create: `project/admin/system_metrics.py`
- Create: `project/tests/unit/admin/test_system_metrics.py`
- Modify: `project/admin/database.py`
- Modify: `project/admin/metrics_routes.py`
- Modify: `project/src/moroz/common/metrics.py`

### Step 1: Write failing aggregation tests

Seed or fake rows for:

- `message_inbox` accepted and processed totals;
- accepted inbox backlog and oldest age;
- `task_outbox` pending and published totals;
- `outbound_messages` totals by bounded status;
- `scheduler_jobs` totals by bounded status;
- LLM call and token totals;
- open escalation count.

Prove that:

- output uses fixed metric names;
- status labels are allowlisted and bounded;
- unavailable PostgreSQL produces `postgres_available 0` without invented
  durable counters;
- no row content or PII appears in output.

### Step 2: Run Docker RED

Run the focused admin metrics tests. Expected: fail because the collector does
not exist and `/metrics` still exports the process-local global registry.

### Step 3: Implement the PostgreSQL collector

- add one read-only aggregation function to `admin/database.py`;
- keep SQL bounded and return only numeric values/fixed statuses;
- build a new `MetricsRegistry` per scrape;
- add the availability gauge;
- keep existing owner authentication before any dependency query;
- remove runtime dependence on the global `registry` from the route.

The generic registry remains available for alert unit tests, but production
metrics no longer pretend it is shared between processes.

### Step 4: Run Docker GREEN

Run focused tests and the existing admin/auth/alerts gate.

### Step 5: Commit

```bash
git add project/admin/database.py project/admin/metrics_routes.py project/admin/system_metrics.py project/src/moroz/common/metrics.py project/tests/unit/admin/test_system_metrics.py changelog.md
git commit -m "feat: metrics читают реальные системные counters"
```

## Task 4: Redis and RabbitMQ live gauges

**Files:**

- Modify: `project/admin/system_metrics.py`
- Modify: `project/admin/metrics_routes.py`
- Modify: `project/docker-compose.yml`
- Modify: `project/docker-compose.prod.yml`
- Modify: `project/.env.example`
- Modify: `project/tests/unit/admin/test_system_metrics.py`
- Modify: `project/tests/unit/test_migration_profile.py`
- Modify: `project/tests/unit/ops/test_validate_env.py`

### Step 1: Write failing source tests

Use fake async Redis and HTTP clients to prove:

- successful Redis `PING` yields `redis_available 1`;
- failed/timeout Redis yields `redis_available 0` and does not fail the scrape;
- RabbitMQ fixed queue responses yield actual ready counts for `tasks` and
  `tasks.dlq`;
- RabbitMQ failure yields `rabbitmq_available 0` and no fabricated queue
  samples;
- credentials, URLs, response bodies, and exception messages never enter
  metrics or logs;
- owner authentication runs before any live probe.

### Step 2: Run Docker RED

Expected: fail because live probes and required admin environment are absent.

### Step 3: Implement bounded probes

- use existing `redis-py` and `httpx`;
- target fixed `RABBITMQ_METRICS_URL`, defaulting internally to
  `http://rabbitmq:15692`;
- do not pass RabbitMQ credentials into `admin`;
- query only `/metrics/detailed?family=queue_coarse_metrics&vhost=%2F` and
  parse only `tasks` and `tasks.dlq`;
- use short connect/read timeouts and close clients reliably;
- add availability gauges and queue ready-message gauges.

Do not publish RabbitMQ metrics/management ports or add a collector service.

### Step 4: Run Docker GREEN and config gates

Run focused tests, admin/ops tests, and base/production Compose
`config --quiet`.

### Step 5: Commit

```bash
git add project/admin/system_metrics.py project/admin/metrics_routes.py project/docker-compose.yml project/docker-compose.prod.yml project/.env.example project/tests/unit/admin/test_system_metrics.py project/tests/unit/test_migration_profile.py project/tests/unit/ops/test_validate_env.py changelog.md
git commit -m "ops: добавлены live gauges Redis и RabbitMQ"
```

## Task 5: Documentation and runbook truth

**Files:**

- Modify: `Дорожная карта.md`
- Modify: `План реализации.md`
- Modify: `project/ops/incident-runbook.md`
- Modify: `project/ops/launch-checklist.md`
- Modify: `docs/superpowers/plans/2026-07-14-production-v1-operations.md`
- Modify: `changelog.md`
- Test: relevant documentation contract tests

### Step 1: Write or update documentation contract tests

Require the runbooks to use `/healthz`, keep `/metrics` owner-only, and identify
real external uptime-monitor setup/evidence as a remaining launch gate.

### Step 2: Run RED, update docs, run GREEN

Mark only the local health/counter implementation complete. Do not mark
production launched and do not close credentials/access, external monitor,
staging, restore, load, alert, eval, TLS/domain, or checklist-signoff gates.

### Step 3: Commit

```bash
git add "Дорожная карта.md" "План реализации.md" project/ops/incident-runbook.md project/ops/launch-checklist.md docs/superpowers/plans/2026-07-14-production-v1-operations.md changelog.md
git commit -m "docs: health и counters отражены в launch gates"
```

## Task 6: Verification and independent review

### Step 1: Targeted Docker gates

Run fresh targeted health, metrics, admin auth/RBAC, Compose/Caddy, and ops
tests. Record exact counts in `changelog.md`.

### Step 2: Full Docker pytest

Run the complete test service suite if feasible and record the exact result.

### Step 3: Static/config checks

- base, production, and staging Compose `config --quiet`;
- production env validator with local non-secret placeholders permitted only
  for the test command;
- `git diff --check`;
- `git status --short --branch`.

### Step 4: Runtime smoke

Build/start an isolated local namespace without Telegram polling or external
provider calls. Probe the internal/public-local health path and authenticated
metrics using only local synthetic state.

### Step 5: Cleanup

Remove only namespace `moroz_health_counters`. Confirm zero leftover containers,
networks, volumes, and task-built images owned by the namespace.

### Step 6: Request independent review

Invoke `superpowers:requesting-code-review` against the implementation range.
Fix all validated Critical/Important findings through Docker RED/GREEN and
repeat the relevant gates.

### Step 7: Final documentation commit if verification changes evidence

Update `Дорожная карта.md` and `changelog.md`, then commit the final verified
evidence as one logical documentation step.

### Step 8: Final report

Report in Russian:

- all local commits;
- targeted/full test and Compose results;
- independent review verdict;
- namespace cleanup evidence;
- remaining blockers requiring real keys/accesses/evidence.

Do not merge or push.
