# Task 1 Report: Domain, Migration, and Provider Mapping

## Scope

- Worktree: `D:\AI_Projects\moroz_i_solntse\moroz-i-solntse-bot\.worktrees\yclients-lifecycle-0008`
- Branch: `codex/yclients-lifecycle-0008`
- No YCLIENTS, Telegram, staging, production, or main worktree requests were made.

## Delivered

- Added `BookingStatus` with `confirmed`, `cancelled`, `completed`, `no_show`, and `unknown`.
- Added validated optional `ExternalBooking.scheduled_end_at`.
- Added YCLIENTS visit lifecycle mapping, including strict validation of `deleted` and `attendance`.
- Calculated scheduled end from provider `seance_length` and preserved it in the mock adapter.
- Added Alembic revision `0008_yclients_lifecycle`, durable `bookings.scheduled_end_at`, extended status constraint, and downgrade normalization.
- Persisted scheduled end in booking INSERT/UPDATE, snapshots, and database-to-domain mapping.

## RED Evidence

1. Adapter RED:

```powershell
Set-Location <worktree>\project
$env:COMPOSE_PROJECT_NAME='moroz_lifecycle_0008'
docker compose --project-name moroz_lifecycle_0008 --env-file ../../../.env run --rm --build test pytest -q tests/contract/booking/test_yclients_adapter.py -k lifecycle
```

Result: `7 failed, 90 deselected`. Failures showed lifecycle values collapsed to `confirmed`, missing `scheduled_end_at`, and no rejection for string attendance.

2. Migration RED:

```powershell
docker compose --project-name moroz_lifecycle_0008 --env-file ../../../.env run --rm test pytest -q tests/integration/test_migrations.py -k lifecycle
```

Result: `1 failed, 22 deselected`; `bookings.scheduled_end_at` did not exist.

3. Repository RED:

```powershell
docker compose --project-name moroz_lifecycle_0008 --env-file ../../../.env run --rm test pytest -q tests/integration/booking/test_booking_repository.py -k scheduled_end
```

Result: `1 failed, 11 deselected`; readback returned `scheduled_end_at=None`.

## GREEN Evidence

1. Adapter lifecycle slice: `7 passed, 90 deselected`.
2. Booking regression slice:

```powershell
docker compose --project-name moroz_lifecycle_0008 --env-file ../../../.env run --rm --build test pytest -q tests/contract/booking/test_yclients_adapter.py tests/unit/booking tests/integration/booking
```

Result: `159 passed in 76.03s`.

3. Lifecycle migration slice: `1 passed, 22 deselected`.
4. Final focused Task 1 gate:

```powershell
docker compose --project-name moroz_lifecycle_0008 --env-file ../../../.env run --rm --build test pytest -q tests/contract/booking/test_yclients_adapter.py tests/integration/test_migrations.py tests/integration/booking
```

Result: `133 passed in 137.74s`.

All Compose invocations set the requested synthetic process-local credentials for Telegram webhook, RabbitMQ, PostgreSQL, and Redis before execution.

## Files

- `project/migrations/versions/0008_yclients_lifecycle.py`
- `project/src/moroz/booking/models.py`
- `project/src/moroz/booking/yclients.py`
- `project/src/moroz/booking/mock_yclients.py`
- `project/src/moroz/booking/repository.py`
- `project/tests/contract/booking/test_yclients_adapter.py`
- `project/tests/unit/booking/test_mock_adapter.py`
- `project/tests/integration/test_migrations.py`
- `project/tests/integration/booking/test_booking_repository.py`
- `changelog.md`

## Self-review and Concerns

- Self-review found the read/write/snapshot paths consistent and all Task 1 focused tests green.
- Brief conflict: its downgrade code preserves `cancelled`, while its required assertion expects no status other than `confirmed` after downgrade. The implementation follows the explicit assertion and normalizes `cancelled` together with all new statuses to `confirmed`.
- A legacy fake YCLIENTS record omitted `attendance`; the shared fixture now explicitly represents confirmed records with `attendance=0`, while `attendance=None` is covered as `unknown`.
