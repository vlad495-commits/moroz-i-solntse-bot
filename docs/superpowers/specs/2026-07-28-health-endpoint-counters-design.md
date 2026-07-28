# Safe Health Endpoint and Real Counters Design

## Context

Phase 7–8 is local-ready, but the first production launch remains blocked by a
safe public health endpoint and real bot/worker/system counters. The current
`moroz.common.metrics` registry is process-local: the `admin` process cannot see
values incremented inside `bot`, `worker`, or `scheduler`.

This change stays local on `codex/phase7-8-readiness`. It does not perform
staging, production, provider, YCLIENTS, or Telegram mutations, and it does not
merge or push.

## Goals

- expose one minimal public health endpoint suitable for Caddy and container
  probes;
- replace placeholder/process-local metric output with counters and gauges read
  from the real PostgreSQL, Redis, and RabbitMQ state;
- keep `/metrics` owner-only;
- expose no PII, secrets, exception text, internal hostnames, or component
  details through the public endpoint;
- add no new service or project dependency.

## Non-goals

- a full monitoring platform, Prometheus server, Grafana, or a new collector
  service;
- public detailed health diagnostics;
- outbound probes to Telegram, LLM providers, or YCLIENTS;
- the post-launch booking, knowledge/version, and escalation workflow UI;
- historical latency instrumentation or a new metrics event store.

## Considered approaches

### 1. Derive metrics from authoritative state — selected

At scrape time, the admin reads durable counters and backlog gauges from
PostgreSQL and live queue gauges from RabbitMQ. Redis receives a bounded
availability probe.

This is the smallest design that survives process restarts and does not pretend
that one process can see another process's in-memory registry.

### 2. Add an `operational_metrics` table

Every process would increment shared counters in PostgreSQL. This could capture
events not represented by current domain tables, but it adds a migration, write
amplification, duplicate state, and more failure paths. It is deferred until a
metric is required that cannot be derived from existing authoritative state.

### 3. Store shared counters in Redis

This is simple and cross-process, but counters disappear with Redis loss or
cleanup. It does not satisfy the production contract for real restart-safe
counters.

## Public health contract

`bot` exposes `GET /healthz`.

- `200 {"status":"ok"}` means the FastAPI process completed startup and
  PostgreSQL answers a bounded `SELECT 1`.
- `503 {"status":"unavailable"}` means the application is not ready or the
  PostgreSQL probe failed or timed out.
- The response never includes component names, timings, URLs, credentials,
  exception messages, or stack traces.
- Redis is intentionally not a critical readiness dependency because the
  approved degradation contract allows the message pipeline to continue from
  PostgreSQL when Redis is unavailable.
- Telegram, LLM, YCLIENTS, and RabbitMQ are not contacted by the public probe.

Caddy routes only the exact public path `/healthz` to `bot`. The base and
staging Compose bot healthchecks probe this endpoint instead of inspecting
`/proc` or using `/openapi.json`.

## Owner-only metrics contract

`GET /metrics` remains protected by the existing owner role. It renders
Prometheus-compatible text built for each request from authoritative sources.

PostgreSQL supplies:

- accepted and processed `message_inbox` totals;
- accepted inbox backlog and oldest-item age;
- pending and published `task_outbox` totals;
- outbound totals grouped by allowlisted status;
- scheduler job totals grouped by allowlisted status;
- retained LLM calls and token usage (gauges, not monotonic totals);
- open escalation count.

RabbitMQ supplies live ready-message counts for `tasks` and `tasks.dlq` through
its existing internal Prometheus endpoint. Redis supplies a bounded `PING`.

The output also contains one `*_available` gauge for PostgreSQL, Redis, and
RabbitMQ. If an optional source cannot be queried, the endpoint still returns
the safe metrics obtained from other sources and publishes availability `0`.
PostgreSQL failure produces only availability gauges because the durable
counters cannot be trusted without their source.

Metric names and labels are fixed in code. Labels may contain only bounded
technical values such as queue or status. Chat IDs, user IDs, message text,
phone numbers, usernames, error text, URLs, and credentials are forbidden.

## RabbitMQ access

The existing `rabbitmq:*management-alpine` service already exposes the built-in
Prometheus detailed metrics endpoint on the internal Compose network. The admin
uses its existing `httpx` dependency and needs no RabbitMQ credentials. No
RabbitMQ metrics or management port is published publicly.

Requests use a short timeout, request only the `queue_coarse_metrics` family for
the fixed `/` virtual host, and parse only the fixed queue names `tasks` and
`tasks.dlq`.

## Error handling

- Every dependency probe is bounded by a short timeout.
- Public health catches dependency errors and returns the fixed `503` body.
- Detailed source failures are logged only as safe error types, without
  connection strings or exception messages.
- `/metrics` degrades per source and never substitutes invented zero counters
  for an unavailable source; only the explicit availability gauge becomes
  zero.

## TDD and verification

Implementation starts with Docker RED tests for:

- exact `/healthz` bodies and status codes;
- absence of diagnostic details in public responses;
- exact Caddy routing and Compose healthcheck commands;
- owner-only `/metrics`;
- SQL aggregation from seeded durable state;
- RabbitMQ queue/DLQ gauges and partial-source failure behavior;
- forbidden PII labels and values.

GREEN uses only the current FastAPI, asyncpg, Redis, httpx, RabbitMQ management,
Docker Compose, and Caddy stack.

Completion requires targeted Docker tests, relevant admin/ops gates, production
Compose `config --quiet`, the full Docker pytest suite if feasible,
`git diff --check`, cleanup of the task's Docker namespace, and independent
review before the final report.
