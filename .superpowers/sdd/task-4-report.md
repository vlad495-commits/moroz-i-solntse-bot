# Task 4 Report: Verified YCLIENTS identity and bounded activity sync

## Status

Completed and committed on `codex/reactivation-v2`.

Feature commit: `67632d0a4720484be00134f527604da58c6bddc5`

Review fix commit: `91bccd48427fd8658b5e95479b518323bb4beb0f`

## RED evidence

All Python/tests ran only in Docker Compose project `codex-reactivation-v2`
with the repository-external `.env`; payloads and providers were synthetic.

- Adapter fields: `9 failed / 24 passed` because `ProjectionRecord.client_id`
  and `record_created_at` were absent and malformed non-null `create_date` was
  accepted.
- History module: collection failed with `ModuleNotFoundError` for
  `moroz.reactivation.activity`.
- Coordinator: collection failed on missing `ActivityCandidate`.
- Postgres projection: collection failed on missing `ActivityRepository`.
- Provider filter integrity: `2 failed / 37 deselected` because empty or
  mismatched record client IDs were aggregated.
- Persisted-ID/batch fairness: malformed local provider ID raised through the
  entire job and 25 unverified rows starved a due verified history
  (`1 failed / 1 passed`).
- Existing single-record provider envelope: one-item list form failed
  (`1 failed / 39 deselected`).
- Review fairness/reverification regressions: rebuilt Docker run produced
  `5 failed / 14 deselected`. The 26th due verified row remained starved after
  25 persistent partial/error attempts; recent booking sync moved the proposed
  attempt cursor; verified rows without current projection skipped exact
  latest-local proof, trusted the stored client ID, and reached history despite
  missing or changed proof.

## GREEN evidence

```powershell
docker compose -p codex-reactivation-v2 --env-file D:\AI_Projects\moroz_i_solntse\moroz-i-solntse-bot\.env build test
docker compose -p codex-reactivation-v2 --env-file D:\AI_Projects\moroz_i_solntse\moroz-i-solntse-bot\.env run --rm test pytest -q tests/contract/booking/test_yclients_records.py tests/unit/booking/test_projection_sync.py tests/unit/reactivation/test_activity_sync.py tests/integration/reactivation/test_activity_projection.py
docker compose -p codex-reactivation-v2 --env-file D:\AI_Projects\moroz_i_solntse\moroz-i-solntse-bot\.env run --rm test pytest -q tests/contract/booking tests/unit/booking tests/integration/booking
docker compose -p codex-reactivation-v2 --env-file D:\AI_Projects\moroz_i_solntse\moroz-i-solntse-bot\.env run --rm test python -m compileall -q /app/src /app/tests
docker compose -p codex-reactivation-v2 --env-file D:\AI_Projects\moroz_i_solntse\moroz-i-solntse-bot\.env config --quiet
```

Results:

- original focused Task 4 gate: `59 passed in 24.09s`;
- review RED subset after the production fix: `5 passed / 14 deselected in
  9.70s`;
- final rebuilt focused Task 4 gate: `65 passed in 27.77s`;
- affected booking regression: `279 passed in 119.66s`;
- Docker compileall, Compose config, `git diff --check`, staged diff check,
  whitespace and forbidden phone/name identity-inference scans: exit `0`.

## Files

- `project/src/moroz/booking/yclients_records.py`
- `project/src/moroz/booking/projection.py`
- `project/src/moroz/reactivation/activity.py`
- `project/tests/contract/booking/test_yclients_records.py`
- `project/tests/unit/booking/test_projection_sync.py`
- `project/tests/unit/reactivation/test_activity_sync.py`
- `project/tests/integration/reactivation/test_activity_projection.py`
- `changelog.md`

## Implemented contract

- Identity uses only stable positive YCLIENTS `client_id` proven by the same
  local booking owner, exact `external_id`, and exact valid
  `moroz_booking_key`; phone/name never participate.
- Current projection proof and latest-local single-record fallback share the
  same ownership rule. Conflicting client IDs and duplicate client claims mark
  every affected projection row `conflict` in one transaction.
- History requests are filtered by `client_id`, include deleted records for
  correct cancellation handling, validate every returned record still belongs
  to that client, deduplicate provider IDs, and stop at `20 x 100` records.
- Full page 20 returns `partial/history_page_limit`; provider failures are
  collapsed to allowlisted codes. Partial/error attempts do not advance the
  successful history watermark or replace completed-visit data.
- Activity claims at most 25 rows. Successful `history_synced_at` alone controls
  due eligibility, while activity-owned `updated_at` rotates every verified
  current/partial/error attempt so a persistent first batch cannot starve row
  26.
- History writer owns identity/history/status columns. The existing recent
  projection owns `next_active_booking_at` and
  `recent_bookings_synced_at` only, and cannot move the activity attempt cursor;
  neither writer changes Telegram inbound time.
- Every due verified candidate is re-proven before history: current projection
  remains the fast path, otherwise the coordinator reads only the exact latest
  locally owned external ID/key and resolves the result transactionally. A
  changed client ID creates a symmetric conflict; missing/error proof preserves
  the stored verified identity but records an allowlisted sync error and never
  reads history.
- Projection records persist only allowlisted fields, including safe client ID
  and nullable UTC record creation time; no raw phone is stored or logged.

## Concerns

- `ActivitySyncCoordinator` is delivered as the Task 4 interface but is not
  wired into worker dispatch here because worker/runtime wiring was outside the
  Task 4 file boundary and must be handled by the owning rollout task.
- Activity attempts reuse `customer_activity_projection.updated_at` as the
  minimal fairness cursor; no speculative migration, attempt table, or column
  was added.
- No real YCLIENTS, Telegram, LLM, staging, production, push, merge, or Task 5
  action was performed.

This report is updated after both code commits so it can contain their final
SHAs; it is not part of commits `67632d0` or `91bccd4`.
