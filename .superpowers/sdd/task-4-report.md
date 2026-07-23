# Task 4 report — exact-key sandbox smoke

## Commands and counts

- RED: `docker compose --env-file ../tmp/compose-empty.env -p moroz-ownership-task4-red --profile test run --rm test pytest tests/unit/booking/test_yclients_sandbox_smoke.py -q` → `5 failed, 22 passed`.
- GREEN: `docker compose --env-file ../tmp/compose-empty.env -p moroz-ownership-task4-green --profile test run --rm test pytest tests/unit/booking/test_yclients_sandbox_smoke.py tests/unit/test_runtime_logging_policy.py -q` → `32 passed`.
- Static privacy check → `privacy legacy=0 schema_disclosure=0` for the smoke source and `_empty_summary` output schema.

## Safety

- Only Docker test profile, local fakes and synthetic values were used. No live YCLIENTS, `.env` or credentials were read or emitted.
- One UUID is passed through create/get/reschedule/get/cancel. Reconciliation uses only exact canonical `custom_fields.moroz_booking_key`; `api_id` and structural similarity are ignored.
- Output has no provider record ID and accepts only stage/count/boolean gates plus allowlisted unknown kind/status.

## Cleanup

- `moroz-ownership-task4-red`: `containers=0 volumes=0 networks=0 images=0`.
- `moroz-ownership-task4-green`: `containers=0 volumes=0 networks=0 images=0`.
- Ignored `tmp/compose-empty.env` was removed.

## Commit

`test: sandbox smoke использует moroz booking key`

## Concerns

Local implementation is complete. Live completion remains blocked until the branch field `moroz_booking_key` exists, separate cleanup consent is granted for the one pre-design active synthetic record, and a new lifecycle smoke is explicitly consented. No live smoke was run.
