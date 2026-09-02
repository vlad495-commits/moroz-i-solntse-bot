# Task 7 implementation report

Status: DONE

## Scope delivered

- Added `ReactivationCoordinator` with the exact public methods `ensure_current`, `run_activity_sync`, and `run_tick`.
- Added fixed scheduler kinds `reactivation_activity_sync` and `reactivation_tick`, 10m/5m successors, planner limit 100 and step claim limit 100.
- Reused Task 4 through a small `ActivitySyncCoordinator.sync_once()` seam, avoiding a duplicate legacy scheduler job.
- Added transactional planner lifecycle: outcome refresh, global fail-closed gates, SQL-prefiltered eligible population, unique journey/main step creation, `FOR UPDATE SKIP LOCKED` claim, in-transaction eligibility replay, durable outbound enqueue, and reserved step state.
- Added idempotent delivery acceptance materialization: actual `sent_at`, active/exhausted transitions, at-most-one reminder, and quiet-window scheduling.
- Added worker routing/seeding only when a valid YCLIENTS reader exists. Missing/partial configuration atomically returns the program to `dry_run`, terminalizes unfinished reactivation jobs, and admin readiness explicitly renders `YCLIENTS unavailable` from a fresh scheduler heartbeat without receiving provider secrets.
- Did not add a process, queue, dependency, timer library, external API call, Telegram send, or Task 8 delivery fence/error classifier.

## TDD evidence

- RED: Docker collection failed with `ModuleNotFoundError: No module named 'moroz.reactivation.service'` before production implementation.
- RED: fail-closed YCLIENTS tests failed with missing repository method and missing explicit admin gate.
- RED: eligible-limit fairness test returned no journey because an earlier excluded consent consumed the limit.
- GREEN: final focused planner/worker/scheduler/activity/admin suite after fail-closed hardening: `92 passed in 40.36s`.
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

## Post-review concurrency hardening

- Reproduced the independent review failures before changing production code: the
  planner deadlocked with activation (`40P01`), and stale booking/deletion state
  could create or resurrect a journey; a previous successful sync also masked a
  newer unavailable state.
- Unified the global order with Task 5: settings and active-version fences are
  acquired before outcome, journey, or outbound writes. Real PostgreSQL probes
  exercise both start orders against both activation and mode transitions.
- Added the existing per-customer advisory fence and canonical row-lock order
  (escalation, human mode, consent, activity, journey) before the final
  eligibility replay. Booking, STOP, escalation, and deletion commits are now
  visible before any journey/outbound insert.
- Due-step workers use a non-blocking customer advisory claim followed by the
  canonical control/journey locks and `FOR UPDATE OF step SKIP LOCKED`; a real
  two-transaction probe proves a worker skips a claimed recipient and reserves
  another step.
- Outcome refresh is deterministic and bounded to 100 open journeys per cycle;
  busy recipients and journey rows are skipped instead of globally serializing
  the planner.
- YCLIENTS readiness now follows the latest authoritative heartbeat/status. The
  fail-closed path publishes an observable `yclients_unavailable` marker, so an
  earlier success cannot mask the transition.

### Post-review verification

- Focused PostgreSQL race/lock probes after the final lock-order change:
  `10 passed, 14 deselected in 25.38s`.
- Full journey planner suite after the final non-blocking advisory refinement:
  `24 passed in 57.55s`.
- Expanded worker/scheduler/activity/consent/admin regression:
  `155 passed in 189.84s`.

## Final re-review hardening

- Reproduced both Task 4 identity-collision start orders with the real
  `ActivityRepository.resolve_identity`: the previous multi-recipient planner
  transaction produced `40P01` in both directions.
- Reproduced outcome-close versus `record_delivery_sent`: outcome-first
  produced `40P01`, while delivery-first exposed the inconsistent lock seam.
- Split each planner cycle into bounded read phases plus one short transaction
  per recipient. Every create/reserve transaction freshly locks and rechecks
  settings and the active version before customer advisory, escalation, human
  mode, consent/activity, journey, and step state.
- Outcome refresh remains deterministic and capped at 100 but now commits one
  recipient at a time in customer → controls → journey → step order.
  `record_delivery_sent` uses the same customer → journey → step order, without
  adding Task 8 pre-send behavior.
- Recovery now reopens only the two exact current-bucket scheduler keys when
  they are `skipped` specifically with `yclients_unavailable`. Same-bucket,
  later-bucket, repeated ensure and unrelated-terminal cases are covered.

### Final re-review verification

- RED: exact Task 4 probe returned `DeadlockDetectedError` for both start
  orders; outcome-first delivery returned the same; same-bucket jobs remained
  `skipped`.
- GREEN: full planner and coordinator suite: `35 passed in 75.52s`.
- GREEN: expanded Task 4/5, worker, scheduler, consent and admin regression:
  `151 passed in 191.75s`.

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
