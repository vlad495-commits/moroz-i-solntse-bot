# Task 12 report: final acceptance and rollout readiness

## Scope

- Added the requested 12-case local E2E matrix over real FastAPI webhook handlers, PostgreSQL repositories, the worker `QueueTask` contract and Telegram fakes.
- Updated the target architecture, visual architecture, roadmap and implementation plan with only the verified Reactivation V2 contract.
- Added the rollout gates without activating or deploying anything.

## Focused evidence

- Exact Step 2 images were rebuilt for `test`, `admin`, `worker`, `scheduler` and `migrate`.
- The exact focused suite completed; four failures were isolated to assumptions in the newly added Task 12 harness while the existing suite passed.
- Minimal test-only corrections aligned the fixed webhook timestamp, refreshed projection freshness before the six-day reminder, and used the canonical `reactivation.delivery_auto_paused` audit action.
- Affected-only rerun: `4 passed, 8 deselected in 18.66s`.

## Rollout boundary

Local acceptance does not authorize real YCLIENTS, Telegram, LLM, staging, production, deploy, push or customer sends. Required external gates remain backup/migration compatibility, staging `dry_run`, separately authorized read-only YCLIENTS sync, preview, test message, legal reference, at least 14 days dry-run observation, owner activation, first-batch observation and emergency-stop rehearsal. First activation additionally requires a fresh preview of at most 25 eligible recipients; otherwise an audited pilot-cap/allowlist decision is required.

## Accepted Minor debt / residual risk

- Duplicate callback delivery may call Telegram `answer_callback_query` again; durable business effects and static reply remain idempotent.
- Retention deletes at most 1000 rows per category per daily run; increase cadence or monitoring only if measured backlog exceeds the drain rate.
- Absolute exactly-once delivery is not claimed. Ambiguous provider outcomes conservatively become operator-visible `delivery_unknown`, pause the program and are not resent automatically.

## Requirement checklist

| Requirement | Verified evidence |
|---|---|
| Proven marketing consent, revoke, suppression and STOP-before-LLM | Final E2E consent/STOP cases plus focused consent/webhook suite |
| Verified Telegram ↔ YCLIENTS identity and full activity | Projection/repository focused suite and local fake projection cases |
| Deterministic 90-day eligibility and exclusions | 89/90/91 E2E boundary plus future-booking/human/escalation cases |
| Versioned static templates without runtime LLM/discount | Program policy/admin contracts and diff review |
| Preview/test/legal/owner gates with default dry-run | Admin/repository focused suite and dry-run E2E case |
| Main plus maximum one reminder, cooldown and quiet hours | Main/reminder E2E and policy/planner focused suite |
| Durable idempotent outbox, pre-send recheck and emergency stop | Worker/task-shape E2E plus delivery-fence focused suite |
| Telegram error classification and conservative ambiguity | Delivery-unknown E2E and delivery focused suite |
| Reply/booking/completed outcomes | E2E reply/booking cases and outcome integration suite |
| Owner-only UI and legacy redirect | Admin route/RBAC affected gate |
| Customer deletion, retention and no PII leakage | Deletion E2E, Task 11 regression and webhook sentinel log/audit check |
| Docker-only acceptance and rollout gates | Commands/results in this report and updated owner documents |
| Newsletters remain separate future scope | Architecture/TZ/roadmap diff review |

## Final evidence

- Compose config: exit `0`.
- Exact compileall exposed only the read-only `/workspace` pycache destination; rerun with `PYTHONPYCACHEPREFIX=/tmp/pycache` compiled `/workspace/src`, `admin`, `llm`, `worker` and `scheduler` with exit `0`.
- Single full Docker suite: `2143 passed / 15 failed in 1658.42s`. All 15 failures were stale test/docs contracts for the already accepted `0023` head, renamed marketing navigation/page heading, implemented visual nodes and canonical/root HTML mirror.
- No production file changed after that run. The affected-only test/docs sync gate passed: `41 passed in 129.21s`; the full suite was deliberately not repeated.
- Final self-review made the PII sentinel traverse the webhook and explicitly exercised stale managed-send recovery; affected-only evidence: `2 passed, 10 deselected in 15.50s`.
- Local Alembic database was additively upgraded from `0022_admin_statistics` to `0023_reactivation_v2`; both `current` and the single `heads` result are `0023_reactivation_v2 (head)`.
- Final self-review and diff/status checks are recorded before the local commit.
