# Task 1 Report: Additive Reactivation V2 schema

## Status

Completed and committed on `codex/reactivation-v2`.

Commit: `4f2ba1d41896f676f30f7cb58522d34c80e9a545`

## RED evidence

```powershell
docker compose -p codex-reactivation-v2 --env-file D:\AI_Projects\moroz_i_solntse\moroz-i-solntse-bot\.env run --rm test pytest -q tests/unit/admin/test_migration_0023_reactivation_v2.py tests/integration/reactivation/test_schema.py
```

Result: `3 errors, 1 failed`. The source fixture could not find
`/workspace/migrations/versions/0023_reactivation_v2.py`, and Alembic head
remained `0022_admin_statistics`; this proved the migration was absent.

## GREEN evidence

```powershell
docker compose -p codex-reactivation-v2 --env-file D:\AI_Projects\moroz_i_solntse\moroz-i-solntse-bot\.env build test migrate
docker compose -p codex-reactivation-v2 --env-file D:\AI_Projects\moroz_i_solntse\moroz-i-solntse-bot\.env run --rm test pytest -q tests/unit/admin/test_migration_0021_reactivation.py tests/unit/admin/test_migration_0023_reactivation_v2.py tests/integration/reactivation/test_schema.py
docker compose -p codex-reactivation-v2 --env-file D:\AI_Projects\moroz_i_solntse\moroz-i-solntse-bot\.env run --rm migrate alembic upgrade head
docker compose -p codex-reactivation-v2 --env-file D:\AI_Projects\moroz_i_solntse\moroz-i-solntse-bot\.env run --rm migrate alembic current
docker compose -p codex-reactivation-v2 --env-file D:\AI_Projects\moroz_i_solntse\moroz-i-solntse-bot\.env run --rm migrate alembic heads
```

Result: `6 passed in 6.98s`; both `current` and `heads` reported the sole head
`0023_reactivation_v2`. `git diff --check` and staged `git diff --cached --check`
completed without errors.

## Changed files

- `project/migrations/versions/0023_reactivation_v2.py`
- `project/tests/unit/admin/test_migration_0023_reactivation_v2.py`
- `project/tests/integration/reactivation/test_schema.py`
- `project/tests/integration/conftest.py`
- `changelog.md`

## Concerns

None. The scope is schema-only: no runtime flow, staging/production, provider
call, or real message was touched. `Дорожная карта.md` remains unchanged because
completion of Task 1 does not complete the overarching Reactivation V2 item.

## Review follow-up

The accepted review found incomplete acceptance coverage, not a migration or
runtime defect. The PostgreSQL test now verifies all nine new foreign keys with
their exact target table and `ON DELETE` policy. It also seeds representative
pre-0023 rows for `marketing_consents`, `reactivation_settings`,
`reactivation_campaigns`, `reactivation_deliveries`, and
`yclients_booking_projection`; after downgrade it verifies their original
columns and values, including the mandated legacy-consent deactivation.

RED used an intentionally incorrect expected `ON DELETE RESTRICT` for the
journey-step FK and failed against PostgreSQL's actual `ON DELETE CASCADE`.
After restoring the contract, the full focused Docker gate passed: `6 passed
in 7.65s`; `alembic current` and `alembic heads` both returned only
`0023_reactivation_v2 (head)`.
