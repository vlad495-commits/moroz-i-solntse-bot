# Task 5 report: versioned program, preview and activation gates

- Base: `8661046`; branch/worktree: `codex/reactivation-v2` / `.worktrees/reactivation-v2`.
- Scope: только Task 5. UI Task 6, runtime sends/providers, staging/production, deploy и push не затрагивались.

## TDD

- Initial RED in Docker: collection failed with expected `ModuleNotFoundError: moroz.reactivation.repository`.
- Debug GREEN wave: `45 passed / 3 failed`; root causes were wrong Compose service placement, PyYAML `!override` parsing in the test, and an untyped PostgreSQL `CASE`. Each was fixed at source; focused repeat: `3 passed`.
- Self-review RED: delivery callback could not instantiate the repository without `ADMIN_SESSION_SECRET`; reproduced as the expected constructor `TypeError`.
- Self-review GREEN: `record_test_sent` works without the admin secret, while preview/HMAC without it fails closed; focused repeat: `2 passed`.

## Implemented contract

- `ReactivationRepository` implements `create_draft`, deterministic `preview_version`, `queue_test_send`, delivery-owned `record_test_sent`, `approve_legal`, transactional `activate_version`, gated `set_mode` and owner-only `get_dashboard`.
- Preview starts from `marketing_consents LEFT JOIN customer_activity_projection`, applies the existing policy priority, assigns exactly one decision/reason per consent and persists aggregates/checksum/watermarks only.
- HMAC-SHA256 uses `ADMIN_SESSION_SECRET` and canonical rows with opaque consent UUID plus safe decision/activity/booking/freshness state. A capture regression proves raw Telegram and YCLIENTS identifiers never enter the HMAC payload.
- Activation and `set_mode("active")` lock settings/version rows and recheck `fresh_preview`, `same_checksum`, `current_watermarks`, `test_sent` and `legal_approved`. The `30:00` boundary is expired.
- Test send targets only `BUSINESS_ALERT_CHAT_ID`, uses existing `outbound_messages` + `task_outbox`, and only a sent-delivery callback sets `test_sent_at`. Blank alert chat skips the test gate.
- Preview creates no journey/outbox. Masked samples are response-only. Audits contain safe before/after metadata and no program message text.
- Admin gets the existing optional `BUSINESS_ALERT_CHAT_ID` mapping in base and production Compose; no second recipient setting was added.

## Verification

- Final Task 5 + existing outbox suite: `54 passed in 107.36s`.
- Affected admin/RBAC/audit regressions: `28 passed in 5.77s`.
- Preview security/gate suite after strengthened assertions: `17 passed in 46.75s` before the final full run; the final full run includes the two later callback/secret cases.
- `python -m compileall -q src/moroz/reactivation admin/reactivation_database.py`: exit `0` in Docker.
- Merged base+production Compose structural config: `config --no-interpolate --quiet`, exit `0`.
- Normal production interpolation stopped only because the local file intentionally lacks `BACKUP_ENCRYPTION_KEY`; no secret was invented or persisted.
- `git diff --check`: clean (only the repository's existing LF/CRLF warnings).

Commit message: `feat: добавить preview и активацию реактивации`.
