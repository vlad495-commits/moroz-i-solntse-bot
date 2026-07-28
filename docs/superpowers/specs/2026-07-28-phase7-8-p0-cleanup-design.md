# Phase 7–8 P0 Cleanup Design

## Status and scope

The accepted local Phase 7–8 baseline is commit `c7a34e8`. This cleanup stays local on `codex/phase7-8-readiness`: no merge, push, staging, production, provider, YCLIENTS, or Telegram mutations.

Phase 7–8 may be called `local-ready` only. Production launch remains blocked until the real access-, secret-, restore-, alert-, eval-, load-, and operational-evidence gates are complete.

## Admin prefix contract

Caddy keeps `handle_path /admin/*`, so the internal FastAPI routes continue to receive paths such as `/login` and `/stats`. The production admin service receives `ADMIN_ROOT_PATH=/admin`; FastAPI exposes it as `request.scope["root_path"]`.

All redirects, template links/forms/assets, and browser-side fetch/EventSource URLs use that request root path. Public navigation therefore stays under `/admin`, while direct local operation remains compatible with the empty default root path.

## Secure cookie contract

Production Compose passes `ADMIN_COOKIE_SECURE` into the admin container. The fail-closed production validator requires the value and accepts only `true`, making the existing secure-cookie behavior an enforced production contract rather than an unused setting.

## Executable image rollback

Existing application services receive explicit Compose image variables for bot, worker, scheduler, and admin. The rollback runbook sets those variables to immutable previous image references and runs `up -d --no-build` for application services only. PostgreSQL schema remains forward-only; no destructive downgrade is added.

## Documentation boundary

`Production Admin` means the production security/operations layer added to the existing FastAPI admin: DB-backed sessions, TOTP-ready login, CSRF, RBAC, audit, metrics, and secure ingress behavior. It does not claim the complete ideal admin from the target specification. The approved first-launch boundary requires a safe health endpoint and real system counters; booking operations UI, knowledge management/versioning UI, and escalation workflow UI are post-launch backlog.

## Tests

Docker-only TDD covers:

- prefix-aware redirect and rendered template/browser URLs;
- Caddy/FastAPI root-path Compose contract;
- required `ADMIN_COOKIE_SECURE=true`;
- rollback image variables and exact no-build command;
- production Compose rendering and focused admin/ops suites.

Completion also requires full Docker pytest when feasible, `git diff --check`, exact task namespace cleanup, and independent review.
