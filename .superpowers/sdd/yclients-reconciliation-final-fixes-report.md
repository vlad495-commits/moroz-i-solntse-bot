# YCLIENTS reconciliation — final review fixes

## Scope and baseline

- Branch: `codex/admin-bookings-reconciliation`.
- Baseline HEAD: `3486742`.
- Scope: only the four confirmed final-review findings; no new table, service, queue, dependency, cache or generic abstraction.
- External Compose env was passed from `D:\AI_Projects\moroz_i_solntse\moroz-i-solntse-bot\.env` without reading, printing or copying it.
- No provider/YCLIENTS, staging, production, push or deploy action was performed.

## RED evidence

- The first Docker attempt was stopped by the shell's five-second limit during build/start and produced no pytest result.
- The first completed run gave `14 failed, 62 passed in 53.20s`; three PostgreSQL cases stopped in a test-only ambiguous-parameter INSERT before exercising production behaviour. Explicit test fixture casts corrected that setup error.
- Clean RED in exact namespace `yclients-final-fixes-red-2830`: `14 failed, 62 passed in 49.74s`.
- Exact missing-behaviour signals:
  - missing/null `services` raised `yclients_response_shape`;
  - the 65-digit provider ID reached the sentinel `ProjectionRecord` constructor;
  - the five-code local mapping helper did not exist;
  - retry/failed scheduler jobs did not add safe error label/time to freshness;
  - the template still rendered `Обновлено локально` and had no safe failure banner.

## GREEN evidence

- First combined reader/unit/PostgreSQL/E2E gate: `76 passed in 45.25s`.
- Reader contract: `25 passed in 0.49s`.
- Booking-view unit plus PostgreSQL freshness: `16 passed in 10.11s`; after self-review strengthening: `16 passed in 9.84s`.
- Admin bookings E2E: `17 passed in 5.05s`.
- Exact affected Task 7 file selection: `178 passed in 98.42s`; fresh final run after self-review: `178 passed in 99.94s`.
- Docker `python -m compileall -q admin src worker`: exit 0.
- `git diff --check`: exit 0.

## Changed files

- `project/src/moroz/booking/yclients_records.py`: accepts only missing/null service lists as service-less records and rejects canonical provider IDs longer than 64 digits before `ProjectionRecord`.
- `project/admin/booking_views.py`: exact five-code local failure-label allowlist with one generic fallback.
- `project/admin/bookings_database.py`: fixed parameterized lookup of the latest unsuccessful projection job by `updated_at`; page freshness receives only a safe label and timestamp.
- `project/admin/templates/bookings.html`: safe last-error banner and neutral `Обновлено` label.
- `project/tests/contract/booking/test_yclients_records.py`: service-less, non-list, >64 ID and pre-constructor regressions.
- `project/tests/unit/admin/test_booking_views.py`: exact allowlist and unknown-code mapping.
- `project/tests/integration/admin/test_admin_bookings_postgres.py`: pending retry, terminal failure, unknown code, latest-job ordering and raw-code exclusion.
- `project/tests/e2e/admin/test_admin_bookings.py`: safe banner, raw/private exclusion and neutral update label.
- `Дорожная карта.md`, `changelog.md`: task status and exact evidence.

## Self-review

- Provider ID validation happens before any projection object is constructed; the existing cursor contract remains unchanged.
- `services=None` and a missing field become `()`; all other non-list values and lists longer than 50 still fail closed.
- Freshness SQL is fixed and binds the job kind and allowed scheduler statuses; it reads the schema's durable `updated_at` and never imports or calls admin transport/provider code.
- The local mapping contains exactly the five approved codes. Unknown/private codes map to one generic Russian label.
- Raw `last_error_code` is not returned in `page["freshness"]` and the template ignores a synthetic raw field in E2E.
- Existing `last_success_at` and twenty-minute stale semantics are unchanged and remain covered by exact-dict PostgreSQL assertions.
- No new schema, service, queue, dependency, cache, provider mutation or UI write path was added.

## Cleanup and concerns

- Before cleanup, exact namespace ownership was verified for 3 containers, 3 volumes and 1 network.
- `docker compose ... down --volumes` removed only `yclients-final-fixes-red-2830`; post-cleanup counts are `0` containers, `0` volumes and `0` networks.
- Open correctness concerns: none found in the requested scope.
