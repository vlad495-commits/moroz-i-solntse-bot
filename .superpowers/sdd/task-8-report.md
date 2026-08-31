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

## Second re-review hardening

- Restored the canonical terminal lock order without weakening the atomic
  delivery transaction. Messaging supplies a guarded terminal-transition
  callback; the reactivation hook invokes it only after program, customer,
  controls, journey and step locks. The generic messaging path invokes the
  same transition directly without a reactivation dependency.
- A managed-link lookup failure or cancellation now releases the claimed
  outbound back to `pending` before the provider seam and propagates the
  original error. The provider fake proves that no network attempt occurs.
- A persistent sent-hook failure or cancellation now falls back to a durable
  bare `delivery_unknown` transition. The reserved step is intentionally left
  for startup reconciliation, which projects it exactly once. A sender cannot
  return a terminal result unless the fallback write is proven durable.
- Confirmed main delivery now materializes `journey.first_sent_at` even when a
  concurrent terminal control has already closed the journey; no reminder is
  scheduled in that state.

### Second re-review TDD and concurrency evidence

- RED: managed-check runtime error and cancellation left the outbound in
  `sending`; both probes now release it to `pending` before the provider seam.
- RED: persistent sent-hook runtime error left `outbound= sending`; the
  cancellation variant propagated without a durable terminal row. Both now
  persist `delivery_unknown`, retain the reserved step and reconcile once.
- RED: accepted main sends in both consent/revoke start orders left
  `first_sent_at=NULL`; both now retain the accepted send timestamp.
- GREEN: managed-check unit matrix: `4 passed, 9 deselected in 4.06s`.
- GREEN: terminal/reconcile race matrix: `3 passed, 44 deselected in 13.14s`.
- GREEN: persistent-hook and closed-journey timestamp matrix:
  `6 passed, 41 deselected in 19.50s`.
- GREEN: real PostgreSQL canonical-order matrix covering sent, failed and
  delivery-unknown against deletion, consent, escalation and human-mode
  writers in both start orders: `24 passed, 47 deselected in 75.17s`; no
  PostgreSQL `40P01` deadlock occurred.
- GREEN: expanded Task 8/messaging/worker/consent/e2e regression:
  `279 passed in 360.45s`.
- GREEN: broad Task 5–8/reactivation, PostgreSQL race, messaging, e2e and
  worker regression: `417 passed in 576.16s`.
- Final Docker `compileall`, Compose config validation and `git diff --check`
  passed.

One obsolete blocking test process from an interrupted RED probe initially
held PostgreSQL transactions; only the temporary worktree test containers
were stopped, then the complete evidence run was repeated from a clean test
process. No external Telegram, YCLIENTS, LLM, staging or production call was
made, and Task 9+ remains unchanged.

## Third re-review hardening

- Centralized the terminal callback contract in `MessageRepository`: a hook
  must invoke its transition exactly once, and the transaction verifies the
  expected persisted status before returning. Missing, duplicate or rolled
  back transitions raise instead of producing a success-shaped result.
- Reused one post-provider recovery path for every completion outcome. An
  accepted or ambiguous provider attempt falls back to verified
  `delivery_unknown`; a known provider rejection whose hook cannot complete
  is returned to verified `pending` and propagated so broker redelivery can
  actually reclaim it.
- A real RabbitMQ consumer probe proves the first failed completion is
  republished while the outbound is reclaimable, and the second delivery
  claims it and reaches `failed`; the consumer never ACKs an outbound left in
  `sending`.

### Third re-review TDD evidence

- RED: all six direct no-call/double-call contract probes returned without an
  error; the provider matrix and Rabbit consumer left completion failures in
  `sending` (`22 failed`). The first attempted run used the stale test image
  and collected only deselections; it was rebuilt before recording RED.
- GREEN: callback no-call/double-call plus exception/cancellation before and
  after transition for sent, permanent-failure and ambiguous outcomes, with
  real Rabbit redelivery: `22 passed, 71 deselected in 70.76s`.
- GREEN: preserved PostgreSQL canonical lock-order matrix:
  `24 passed, 69 deselected in 77.62s`, including both start orders and no
  `40P01`. The three deletion-first cases now correctly raise rather than
  return success after the outbound was already lawfully deleted.
- GREEN: messaging/worker/consent/e2e closure regression:
  `208 passed in 162.76s`.
- GREEN: combined Task 8/messaging/worker/consent/e2e regression:
  `301 passed in 424.16s`.
- GREEN: broad Task 5–8/reactivation, PostgreSQL race, messaging, e2e and
  worker regression: `439 passed in 644.84s`.
- Final Docker `compileall`, Compose config validation and `git diff --check`
  passed. No external provider, staging or production call was made, and Task
  9+ was not changed.

## Fourth re-review hardening

- Added one explicit `TaskRecoveryRequired` signal to the existing Rabbit
  retry envelope. A confirmed republish adds an internal
  `x-recovery-required` header; only a broker retry with `x-retry-count > 0`
  hydrates non-serialized `QueueTask` metadata. The JSON payload cannot forge
  the signal, and an ordinary duplicate cannot trigger recovery.
- Recovery redelivery bypasses claim and provider execution for that specific
  outbound. It uses the existing atomic delivery hook/callback to persist and
  project `delivery_unknown`, including the case where a cancelled shielded
  write committed the outbound status but not the domain projection.
- If recovery storage is still unavailable, the dedicated signal is
  republished again. It is also retained on the last retry's durable DLQ
  message for safe replay instead of degrading into an ordinary claim-skip.

### Fourth re-review TDD evidence

- RED: real RabbitMQ/PostgreSQL recovery exception and cancellation after
  sent, permanent-rejection and ambiguous provider outcomes left all six
  outbounds in `sending` after the second delivery; the ordinary duplicate
  control correctly remained untouched (`6 failed, 1 passed`).
- GREEN: the same six-outcome recovery matrix plus the ordinary-duplicate
  control: `7 passed, 93 deselected in 25.45s`; the exact rebuilt rerun was
  `7 passed, 169 deselected in 27.89s`. Every recovery used one provider call,
  reached outbound/step `delivery_unknown`, and needed no restart.
- GREEN: callback, real Rabbit and canonical lock-order closure matrix:
  `53 passed, 122 deselected in 153.22s`.
- GREEN: exact queue/Rabbit/worker suite, including last-retry DLQ signal
  retention: `76 passed in 5.17s`.
- GREEN: combined Task 8/queue/messaging/worker/consent/e2e regression:
  `331 passed in 483.05s`.
- GREEN: broad exact-tree Task 5–8/reactivation, PostgreSQL race, messaging,
  e2e and worker regression: `446 passed in 694.18s`.
- Final Docker `compileall`, Compose config validation and `git diff --check`
  passed. No external provider, staging or production call was made, and Task
  9+ was not changed.

## Fifth re-review hardening

- Broker `redelivered` metadata now supplies a non-serialized conservative
  recovery candidate when the explicit recovery header could not be confirmed
  before the original message was reject-requeued. Explicit recovery headers
  and retry-count validation keep precedence; payload JSON still cannot forge
  either internal flag.
- The candidate is acted on only after the exact outbound is still `sending`
  and the existing repository-backed managed-link check accepts that durable
  row. A linked send is terminalized as `delivery_unknown` without another
  provider call; `pending` follows the normal claim/send path, terminal rows
  remain idempotent no-ops, and generic `sending` rows are untouched.
- A transient managed-link lookup failure preserves `TaskRecoveryRequired`,
  so the existing confirmed retry envelope cannot silently degrade to an
  ordinary claim-skip. Repeated recovery-storage failure likewise restores the
  explicit marker on the next successfully confirmed republish.
- The deliberate conservative trade-off is documented: if Rabbit marks a
  duplicate redelivered while another managed attempt is genuinely still in
  flight, the durable row becomes `delivery_unknown`. This may classify an
  eventual acceptance as ambiguous, but prevents a second provider call and
  suppresses unsafe automatic reminders. Fresh duplicates with
  `redelivered=false` retain the previous no-op behavior.

### Fifth re-review TDD evidence

- RED from the independent real Rabbit/PostgreSQL probe: after a provider call
  and failed confirmed recovery republish, reject-requeue returned the original
  message without the recovery flag (`expected True, observed False`).
- GREEN: real Rabbit/PostgreSQL publish-failure and DB-authoritative state
  matrix for sent, permanent-rejection and ambiguous outcomes, pending,
  managed/generic sending, terminal and repeated recovery failure:
  `9 passed, 99 deselected in 31.38s`; provider remained exactly once.
- GREEN: queue/Rabbit/worker regression: `76 passed in 5.30s`.
- GREEN: callback, recovery and all 24 canonical PostgreSQL lock-order cases:
  `61 passed, 47 deselected in 169.42s`, with no `40P01`.
- GREEN: expanded delivery/messaging/worker/consent/e2e regression:
  `340 passed in 453.81s`.
- GREEN: broad exact-tree reactivation/messaging/e2e/worker regression:
  `454 passed in 653.78s`.
- Self-review RED proved a candidate managed-link DB error escaped as an
  ordinary exception; the focused GREEN rerun was `3 passed, 11 deselected in
  3.90s`, followed by the real Rabbit v5 matrix at `9 passed, 99 deselected in
  28.85s`.
- Final Docker `compileall`, Compose config validation and `git diff --check`
  passed. No external provider, Telegram, YCLIENTS, LLM, staging or production
  call was made, and Task 9+ was not changed.

## Sixth re-review hardening

- The first recovery-candidate lookup is now inside the same explicit recovery
  boundary as the managed-link check and durable recovery write. A runtime
  failure or `asyncio.CancelledError` becomes `TaskRecoveryRequired`, so the
  existing Rabbit retry path adds the recovery marker instead of degrading to
  an ordinary claim-skip.
- Normal DB-authoritative decisions are unchanged: no row, `pending` or a
  terminal row follows the existing normal path; only the exact linked
  `sending` outbound is recovered. Generic delivery failures do not become
  recovery attempts.
- Recovery catches are limited to `Exception` and `asyncio.CancelledError`.
  `SystemExit` and `KeyboardInterrupt` remain process-control signals and are
  not converted into queue recovery.

### Sixth re-review TDD and verification evidence

- The first test run had an invalid four-item retry-delay tuple and failed in
  test setup before exercising production behavior. After correcting the test
  to the queue's existing three-retry contract, the real Rabbit/PostgreSQL RED
  reproduced all three required cases: runtime lookup failure stranded
  `sending/reserved`, cancellation escaped raw, and repeated lookup plus
  confirmed-publish failure also stranded the delivery (`3 failed`).
- GREEN: lookup exception, cancellation, repeated lookup failure and confirmed
  republish failure all preserve recovery and finish outbound/step as
  `delivery_unknown` without restart or a second provider call:
  `3 passed, 108 deselected in 17.39s`.
- GREEN: v5/v6 fallback and DB-authoritative negative/positive matrix:
  `12 passed, 99 deselected in 39.65s`.
- GREEN: explicit process-control boundary:
  `3 passed, 13 deselected in 4.12s`.
- GREEN: callback/recovery plus all 24 canonical PostgreSQL lock-order cases:
  `63 passed, 48 deselected in 203.15s`, with no `40P01`.
- GREEN: expanded delivery/messaging/worker/consent/e2e regression:
  `346 passed in 516.65s`.
- GREEN: the single final broad exact-tree reactivation/messaging/e2e/worker
  regression: `460 passed in 717.35s`.
- Final Docker `compileall`, Compose config validation and `git diff --check`
  passed. No external provider, Telegram, YCLIENTS, LLM, staging or production
  call was made, and Task 9+ was not changed.

### Final conservative delivery contract

- `sent` is recorded only when provider acceptance and the durable terminal
  projection are proven; `failed` is recorded only for a proven permanent
  rejection whose terminal projection completes.
- If provider acceptance or terminal persistence is ambiguous, delivery is
  durably classified as `delivery_unknown`. The worker does not call the
  provider again for that recovery attempt, the journey stops, no automatic
  reminder is scheduled, and the case remains visible for operator handling.
- This is an at-most-one-provider-call safety policy for the covered recovery
  window, not an absolute exactly-once guarantee across an external provider
  and PostgreSQL. A live-attempt broker redelivery can conservatively choose
  `delivery_unknown`; that documented false-unknown trade-off is preferred to
  a duplicate customer message.
