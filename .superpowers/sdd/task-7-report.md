# Task 7 implementation report

Status: DONE

## Scope delivered

- Added `ReactivationCoordinator` with the exact public methods `ensure_current`, `run_activity_sync`, and `run_tick`.
- Added fixed scheduler kinds `reactivation_activity_sync` and `reactivation_tick`, 10m/5m successors, planner limit 100 and step claim limit 100.
- Reused Task 4 through a small `ActivitySyncCoordinator.sync_once()` seam, avoiding a duplicate legacy scheduler job.
- Added transactional planner lifecycle: outcome refresh, global fail-closed gates, SQL-prefiltered eligible population, unique journey/main step creation, `FOR UPDATE SKIP LOCKED` claim, in-transaction eligibility replay, durable outbound enqueue, and reserved step state.
- Added idempotent delivery acceptance materialization: actual `sent_at`, active/exhausted transitions, at-most-one reminder, and quiet-window scheduling.
- Added worker routing/seeding only when a valid YCLIENTS reader exists. Missing configuration atomically returns the program to `dry_run`; admin readiness explicitly renders `YCLIENTS unavailable`.
- Did not add a process, queue, dependency, timer library, external API call, Telegram send, or Task 8 delivery fence/error classifier.

## TDD evidence

- RED: Docker collection failed with `ModuleNotFoundError: No module named 'moroz.reactivation.service'` before production implementation.
- RED: fail-closed YCLIENTS tests failed with missing repository method and missing explicit admin gate.
- RED: eligible-limit fairness test returned no journey because an earlier excluded consent consumed the limit.
- GREEN: focused planner/worker/scheduler/activity/admin suite: `92 passed in 41.20s`.
- GREEN: expanded policy/consent/activity/preview/admin/worker safety regression: `200 passed in 178.15s`.
- GREEN: dedicated PostgreSQL planner suite before final fairness case: `8 passed`; final focused suite includes all 13 planner cases.

## Static/runtime checks

- `python -m compileall -q src worker admin tests`: passed in Docker.
- `docker compose ... config --quiet`: passed.
- `git diff --check`: passed (only Windows LF/CRLF notices).
- `ruff` is not installed in the approved test image; no dependency was added solely for linting.

## Self-review

- Idempotency is backed by existing unique open-journey, journey-step, outbound, task-outbox, and scheduler idempotency constraints.
- Concurrent ticks may both inspect a recipient, but only one open journey can commit; due-step claims use row locks with `SKIP LOCKED`.
- Population LIMIT applies after SQL eligibility gates, preventing excluded rows from starving eligible recipients; pure policy is still rerun before insert and again before reserve.
- Task 7 intentionally does not cancel a pending outbound after a later state change and does not classify Telegram failures; Task 8's pre-send fence/result hook owns that atomic delivery behavior.
- No raw Telegram/YCLIENTS identifiers or message bodies were added to logs/audit records.

## Changed files

- `project/src/moroz/reactivation/service.py`
- `project/src/moroz/reactivation/repository.py`
- `project/src/moroz/reactivation/activity.py`
- `project/worker/main.py`
- `project/admin/reactivation_database.py`
- `project/admin/templates/reactivation.html`
- `project/tests/unit/reactivation/test_service.py`
- `project/tests/integration/reactivation/test_journey_planner.py`
- `project/tests/unit/test_worker.py`
- `project/tests/unit/admin/test_reactivation_database_module.py`
- `changelog.md`
