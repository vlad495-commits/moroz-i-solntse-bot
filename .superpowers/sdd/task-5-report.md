# Task 5 report: versioned program, preview and activation gates

- Base: `8661046`; branch/worktree: `codex/reactivation-v2` / `.worktrees/reactivation-v2`.
- Scope: только Task 5. UI Task 6, runtime sends/providers, staging/production, deploy и push не затрагивались.

## TDD

- Initial RED in Docker: collection failed with expected `ModuleNotFoundError: moroz.reactivation.repository`.
- Debug GREEN wave: `45 passed / 3 failed`; root causes were wrong Compose service placement, PyYAML `!override` parsing in the test, and an untyped PostgreSQL `CASE`. Each was fixed at source; focused repeat: `3 passed`.
- Self-review RED: delivery callback could not instantiate the repository without `ADMIN_SESSION_SECRET`; reproduced as the expected constructor `TypeError`.
- Self-review GREEN: `record_test_sent` works without the admin secret, while preview/HMAC without it fails closed; focused repeat: `2 passed`.
- Review RED reproduced five gaps: a non-max source-row mutation did not invalidate preview, sent-test proof accepted changed delivery fields, a consent writer could commit inside the activation recheck gap, duplicate callbacks emitted misleading audit, and the admin wrapper still exposed scalar policy arguments.
- Review GREEN adds lossless safe source fingerprints, exact delivery proof, a writer-independent PostgreSQL activation fence, idempotent callback/requeue audit, and a single required `ProgramPolicy` wrapper input.
- Final lock-order RED used the real `EscalationService` writes and a deletion-order probe; PostgreSQL returned `40P01` for writer-first `activate`, `set_mode("active")` and deletion interleavings with the original reverse fence order (`3 failed / 2 activation-first controls passed`).
- Final lock-order GREEN uses the existing writer-compatible order and the same coordinated suite passes all five interleavings using backend-specific PostgreSQL lock wait events rather than timing guesses.

## Implemented contract

- `ReactivationRepository` implements `create_draft`, deterministic `preview_version`, `queue_test_send`, delivery-owned `record_test_sent`, `approve_legal`, transactional `activate_version`, gated `set_mode` and owner-only `get_dashboard`.
- Preview starts from `marketing_consents LEFT JOIN customer_activity_projection`, applies the existing policy priority, assigns exactly one decision/reason per consent and persists aggregates/checksum/watermarks only.
- HMAC-SHA256 uses `ADMIN_SESSION_SECRET` and canonical rows with opaque consent UUID, individual consent/activity timestamps, and deterministic safe state for every journey, human-mode and escalation row. Multiplicity is preserved; raw Telegram/YCLIENTS identifiers and message text are excluded.
- Activation and `set_mode("active")` lock settings/version rows, then acquire fixed-order `SHARE` locks on `escalations → human_mode → marketing_consents → customer_activity_projection → reactivation_journeys` before rechecking `fresh_preview`, `same_checksum`, `current_watermarks`, `test_sent` and `legal_approved`. This matches `EscalationService` and customer deletion; consent and activity writers add no conflicting reverse multi-table order. Coordinated regressions prove whichever transaction starts first completes while the other waits, with stable activation gates and no deadlock. The `30:00` boundary is expired.
- Test send targets only `BUSINESS_ALERT_CHAT_ID`, uses existing `outbound_messages` + `task_outbox`, and only a sent-delivery callback sets `test_sent_at`. The activation proof is bound to the current exact Telegram channel, configured chat, main text, outbound ID, idempotency key and sent status. Blank alert chat skips the test gate.
- Duplicate delivery callbacks and requeue of the same already-sent outbound perform no state change and emit no false audit event.
- `admin.reactivation_database.create_draft` has one required `ProgramPolicy` input plus actor/time; the Task 6 UI remains out of scope.
- Preview creates no journey/outbox. Masked samples are response-only. Audits contain safe before/after metadata and no program message text.
- Admin gets the existing optional `BUSINESS_ALERT_CHAT_ID` mapping in base and production Compose; no second recipient setting was added.

## Verification

- Final Task 5 + existing outbox suite after lock-order fix: `72 passed in 118.14s`.
- Affected admin/RBAC/audit regressions after lock-order fix: `28 passed in 5.17s`.
- Coordinated lock-order suite after replacing timing assertions with PostgreSQL wait events: `5 passed in 13.63s`.
- Preview security/gate suite after strengthened assertions: `17 passed in 46.75s` before the final full run; the final full run includes the two later callback/secret cases.
- `python -m compileall -q src/moroz/reactivation admin/reactivation_database.py`: exit `0` in Docker.
- Merged base+production Compose structural config: `config --no-interpolate --quiet`, exit `0`.
- Normal production interpolation stopped only because the local file intentionally lacks `BACKUP_ENCRYPTION_KEY`; no secret was invented or persisted.
- `git diff --check`: clean (only the repository's existing LF/CRLF warnings).

Commits:

- `feat: добавить preview и активацию реактивации`
- `fix: закрыть гонки активации реактивации`
- `fix: согласовать порядок блокировок активации`
