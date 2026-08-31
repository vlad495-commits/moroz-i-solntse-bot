# Task 11 report: deletion, retention and privacy

## Scope

- Reused the existing Redis deletion marker, buffer lock, per-customer PostgreSQL advisory fence and one atomic customer-deletion transaction.
- Reused the existing `DATA_RETENTION_DAYS` cleanup path; no second privacy framework, worker, process, queue or migration was introduced.
- Kept the legacy reactivation archive compatible and read-only in the admin UI.

## Implementation

- Customer deletion now collects every outbound linked through the recipient's Reactivation V2 journey before deleting outbox rows, outbound rows, journeys/steps, activity projection, consent events and materialized consent.
- Existing zero-row verification now includes every recipient-linked Reactivation V2 table and linked outbound/outbox row before commit.
- Retention uses batches of 1000 for existing message/token cleanup and for closed journeys, stale activity without active consent/open journey, and old consent events without current active consent.
- Active consent proof, open journeys, fresh closed journeys, unrelated recipients and the existing admin audit retention contract remain untouched.
- Public deletion and retention failures remain aggregate/safe; no phone, recipient ID, message/proof text or provider exception is logged.

## TDD evidence

- RED: Docker focused gate produced `5 failed, 18 passed`; the missing V2 lifecycle was observed directly. Two failures also exposed stale post-0023 test fixture/expectation and were corrected.
- GREEN: Docker focused deletion/retention/privacy gate produced `23 passed in 48.35s`.
- Bounded regression: `tests/e2e/test_message_delivery.py` plus `tests/e2e/admin/test_marketing_reactivation.py` produced `77 passed in 127.94s`, including the blocked-send deletion race and legacy redirect/archive behavior.
- Final technical gate: Docker `compileall`, Compose config validation and `git diff --check` all exited `0`.

## Debt / exclusions

- Minor debt only: one retention run deletes at most 1000 rows per category; backlog drains through the already scheduled daily cleanup cycles. Increase frequency or add measured operational paging only if observed backlog requires it.
- Task 12, deploy, push, merge, staging/production and real provider calls were not performed.
