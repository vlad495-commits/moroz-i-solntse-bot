# Production Admin Phase 7 Design

## Context

Phase 6 Scheduler & Notifications is complete, and the current migration head is `0008_yclients_lifecycle`. The next project phase in `План реализации.md` is **Phase 7: Production Admin**.

The repository already contains a FastAPI/Jinja2 admin container with:

- env-based login and signed session cookie;
- dialogs, chat detail, stats, prompt editor, bot pause control, logs;
- eval and eval-review screens;
- Docker Compose `admin` service.

## Goal

Turn the existing admin panel into the first production-safe operational admin surface without adding a new service, frontend stack, ORM, or external provider mutation.

## Scope

Build the minimal Phase 7 foundation:

- DB-backed admin users with roles `owner` and `admin`;
- password verification with standard-library PBKDF2 hashes;
- TOTP verification implemented with the standard library;
- signed session cookies that carry a DB session id;
- CSRF token validation for admin POST forms;
- append-only audit events for sensitive reads and meaningful admin writes;
- permission helpers for owner-only and admin-allowed routes;
- small UI adjustments for login/TOTP/CSRF and role-aware navigation;
- migration `0009_production_admin.py`, down-revising `0008_yclients_lifecycle`.

## Out Of Scope

- No new SPA, admin service, worker, scheduler, queue, or dependency.
- No staging, production, Telegram, YCLIENTS, or provider mutations.
- No payments, marketing broadcasts, medical forms, or new external integrations.
- No broad redesign of existing templates.

## Architecture

Keep the current `project/admin` FastAPI app. Add focused modules:

- `admin/security.py`: PBKDF2 password hashing, TOTP verification, CSRF token generation/verification.
- `admin/user_repository.py`: admin user/session queries.
- `admin/audit_repository.py`: append-only audit writes.
- `admin/rbac.py`: role/permission checks.

Existing routes continue to use `get_current_user(request)`, but the return value becomes a small authenticated user object instead of a raw username string. Templates keep rendering `{{ user }}` via a display property.

## Data Model

Migration `0009_production_admin.py` creates:

- `admin_users`: username, role, password hash, TOTP secret, enabled flag, timestamps.
- `admin_sessions`: opaque session id, user id, CSRF token, expiry and last-seen timestamps.
- `admin_audit_events`: actor id, action, object type/id, before/after JSON, request metadata, created timestamp.

The migration only expands schema. Runtime does not create tables.

## Security Rules

- Roles are exactly `owner` and `admin`.
- TOTP is required for DB-backed admin login.
- Shared env login remains only as a local bootstrap fallback while no `admin_users` rows exist.
- Every admin POST must include the per-session CSRF token.
- Sensitive dialog detail reads and meaningful admin writes are audited.
- Production cookies are `HttpOnly`, `SameSite=Lax`, and `Secure` unless explicitly running in local test mode.

## Testing

Use TDD through Docker Compose:

- focused auth/security unit tests for PBKDF2, TOTP and CSRF;
- focused admin route tests for login, failed TOTP, disabled user, CSRF rejection and audit insertion;
- migration tests that verify `0009` depends on `0008_yclients_lifecycle`;
- existing full Docker pytest gate before completion.
