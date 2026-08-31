# Task 8 implementation report

Status: DONE

## Scope delivered

- Added an optional generic pre-send guard and atomic delivery hook without importing reactivation into messaging.
- Added explicit Telegram outcome classification: permanent recipient failures, retry-after, ambiguous network/timeout/cancellation, and unchanged generic-message retry semantics.
- Added a shared/exclusive program advisory fence. Delivery holds the shared lock through the network seam; emergency stop takes the exclusive lock and cannot overtake an in-flight send.
- Rechecked program mode, legal/version state, consent, suppression, inbound activity, booking freshness, deletion, human mode, escalation, journey and step immediately before the fake Telegram seam.
- Terminalized linked outbound, step and journey outcomes atomically. Forbidden/NotFound suppress the recipient; BadRequest and ambiguous delivery auto-pause with allowlisted audit fields only.
- Kept `TelegramRetryAfter` on the existing broker retry path and never requeued ambiguous delivery.
- Made test sends version/checksum/chat-bound and allowed `test_sent_at` only after confirmed success.
- Made emergency stop idempotent and limited cancellation to not-started work. If stop wins after network success but before the sent hook, the actual send is recorded and no reminder is created.
- Added startup reconciliation for the generic stale-claim crash window. Only still-reserved linked steps are reconciled, so a repeated startup is a no-op.
- Preserved the Task 7 canonical order: program → customer → controls → journey → step → outbound.

## TDD evidence

- Baseline selected Docker regression: `38 passed in 83.89s`.
- RED: focused collection failed with `ImportError: cannot import name 'classify_delivery_error'`.
- RED: repeated startup reconciliation returned `1` instead of the required idempotent `0`.
- GREEN: final Task 8 delivery/messaging/worker suite: `172 passed in 66.93s`.
- GREEN: broad reactivation/messaging/e2e/worker regression before the final count-only refinement: `366 passed in 388.85s`.
- GREEN: final count refinement probe: `1 passed, 22 deselected in 6.26s`.

All provider behavior was exercised with fake Telegram transports and PostgreSQL-backed tests. No real Telegram, YCLIENTS, LLM, staging or production call was made.

## Concurrency and idempotency evidence

- A blocking fake proves stop waits for an in-flight network call and cancels the next delivery.
- The opposite start order proves an exclusive transition blocks the sender and the final guard observes the new mode.
- Crash/redelivery probes prove stale `sending` becomes `delivery_unknown`, is projected exactly once, and is never resent.
- Repeated stop, delivery hooks, startup reconciliation and outbox relay do not duplicate state transitions, reminders, audits or queue effects.
- Exact once is not claimed across an ambiguous external Telegram response; the safe terminal state is deliberately `delivery_unknown` with no retry.

## Static/runtime checks

- `docker compose ... config --quiet`: passed.
- Docker `python -m compileall -q -f /app/src /app/admin /app/llm /app/worker`: passed.
- `git diff --check`: passed (only Windows LF/CRLF notices).

## Changed files

- `project/src/moroz/messaging/repository.py`
- `project/src/moroz/messaging/telegram.py`
- `project/src/moroz/reactivation/repository.py`
- `project/src/moroz/reactivation/service.py`
- `project/worker/main.py`
- `project/tests/unit/reactivation/test_delivery.py`
- `project/tests/integration/reactivation/test_delivery_fence.py`
- `project/tests/integration/messaging/test_outbox.py`
- `project/tests/unit/reactivation/test_service.py`
- `changelog.md`

## Residual risks and deferrals

- Telegram cannot provide transactional exactly-once delivery across a lost response; ambiguous outcomes are intentionally terminal and pause the program for operator review.
- Real-provider and staging/prod verification is intentionally deferred because Task 8 forbids external calls.
- Task 9+ outcome analytics and admin presentation were not changed.

## Independent re-review hardening

- Replaced the unlocked read-before-hook sequence with a guarded
  `sending → terminal` transition that owns the outbound row before invoking
  the hook; outbound and linked effects still commit or roll back together.
- Removed `delivery_options` from the trust boundary. The worker now supplies
  an authoritative repository linkage check; forged or stale JSON remains on
  generic retry semantics.
- Test-send failures of every supported Telegram class now affect only the
  version proof/outbound status and never pause the client program.
- Preserved an accepted Telegram send across consent, STOP and other terminal
  controls. The step records `sent/sent_at`, the journey closes with stable
  precedence, and no reminder is created after consent, inbound, booking,
  human mode, escalation, deletion, legal or version changes.
- Delivery suppression now writes one idempotent immutable
  `marketing_consent_events` record through `ConsentService`, materializes the
  consent and closes/cancels remaining journey work in the same transaction.
- Delivery logs contain allowlisted code/count fields only. Raw outbound UUID,
  chat/user IDs, text and provider details are excluded.
- Concurrent startup reconciliation increments its count only for the caller
  that performs the real reserved-step transition.
- Removed the unused `DeliveryErrorDecision.retry` duplicate. Legacy
  `record_test_sent`/`record_delivery_sent` APIs remain intentionally because
  repository and admin compatibility tests still consume them; their later
  retirement is recorded as Ponytail debt, not expanded into Task 8.

### Re-review TDD and verification

- RED: unit trust/minor matrix produced `10 failed, 1 passed`; forged delivery
  options had no authoritative checker and `retry` still existed.
- RED: consent-first accepted-send probe produced
  `outbound=sent / step=cancelled / sent_at=NULL`.
- RED: immutable delivery suppression event was absent; terminal/reconcile,
  test-send program isolation and concurrent count probes also failed.
- RED: post-network control matrix initially failed 4 of 5 cases, followed by
  legal/version probes failing 2 of 2 before the final runtime recheck.
- GREEN: core independent re-review matrix: `28 passed, 21 deselected in 52.05s`.
- GREEN: expanded Task 8/generic/worker/consent regression:
  `203 passed in 142.69s`.
- GREEN: broad Task 5–8, PostgreSQL race, messaging, e2e and worker regression:
  `387 passed in 436.96s`.
- GREEN: final focused suite after legal/version and privacy hardening:
  `168 passed, 83 deselected in 246.02s`.

Additional changed files in re-review:

- `project/src/moroz/security/consent.py`
- `project/tests/e2e/test_message_delivery.py`
