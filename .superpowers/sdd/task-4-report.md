# Task 4 lifecycle report - checkpoint 0008

## Status

`DONE_WITH_CONCERNS`

The lifecycle regression gates are complete and the final full Docker suite is
green. Per the explicit handoff scope, the roadmap and phase plan remain open,
the `moroz_lifecycle_0008` namespace remains intact, and the broad review is
deferred.

## Verified Docker gates

- Lifecycle collection RED: unit and integration modules named
  `test_lifecycle.py` produced one pytest collection error (`import file
  mismatch`). The integration module was renamed to
  `tests/integration/notifications/test_lifecycle_persistence.py`; the
  immediate regression rerun passed: `16 passed in 13.83s`.
- Focused lifecycle suite: `204 passed`, `0 failed`, `179.56s`.
- Migration upgrade/current: exit `0`; current revision
  `0008_yclients_lifecycle (head)`. The local database upgraded
  `0006 -> 0007 -> 0008` after rebuilding the stale local migration image.
- Worker and scheduler images built successfully; `python -m compileall -q
  /app` exited `0` for both services.
- First full suite: exit `1`; `835 passed, 1 failed in 399.38s`. The failure
  was `test_official_compose_commands_use_approved_env_file`, caused by the
  Task 4 Compose examples in the lifecycle implementation plan using a
  non-canonical `--env-file` position/path.
- Minimal documentation correction: Task 4 Compose examples now use
  `docker compose --env-file ../.env --project-name moroz_lifecycle_0008 ...`.
  The focused Docker regression gate passed: `6 passed in 0.11s`.
- Final full suite: exit `0`; `836 passed in 388.82s (0:06:28)`. Complete
  stdout and the recorded exit code are retained in
  `tmp/task-4-full-pytest.log`.

## Safety and scope

- All Compose invocations used only the local namespace
  `moroz_lifecycle_0008` with process-local test credentials.
- No external/provider, YCLIENTS mutation, Telegram, staging, or production
  action was run.
- No production source was changed in this handoff. The only test change is
  the collection-safe integration-test rename.

## Deferred by request

- No roadmap or phase-plan completion update.
- No namespace cleanup or image/volume removal.
- No push or merge.
- Broad independent review remains pending.

## Concerns

Docker Compose emits normal container lifecycle status lines through stderr;
PowerShell labels those lines `NativeCommandError`, although the final Compose
command and pytest both exited `0`. The saved log contains the authoritative
pytest result and `PYTEST_EXIT_CODE=0`.

## Post-review fix wave - 2026-07-28

### Implemented findings

- Important: `REMINDER_KINDS` now skip bookings whose status is not
  `confirmed`, so completed, no-show, and unknown lifecycle bookings cannot
  receive reminders.
- Minor: direct `ExternalBooking.scheduled_end_at` boundary coverage rejects a
  naive value, a value equal to `starts_at`, and a value before `starts_at`;
  an aware end after the start remains valid.
- Minor docs: the lifecycle plan now uses
  `test_lifecycle_persistence.py` for its integration-test references and its
  conditional-update example matches the confirmed-only implementation.

### TDD and verification evidence

- RED, after building the local test image: focused handler/model run exited
  `1` with `3 failed, 17 passed in 0.11s`. The three parameterized statuses
  (`completed`, `no_show`, `unknown`) each showed an incorrect reminder send.
- GREEN: the same focused Docker run exited `0` with `20 passed in 0.13s`.
- Documented Compose command tests: exit `0`, `6 passed in 0.14s`.
- Fresh full Docker suite: exit `0`, `843 passed in 421.56s (0:07:01)`.

All runs used only the existing `moroz_lifecycle_0008` namespace and the
specified process-local local-test credentials. This worktree has no ignored
`../.env`, so actual Compose executions intentionally relied on those
process-local variables; the documented-command gate confirms the checked-in
documentation still uses the required `--env-file ../.env` form. No cleanup,
external/provider/Telegram, staging, production, push, or merge action was
performed.
