# YCLIENTS Lifecycle Ingestion Design

**Date:** 2026-07-27

**Status:** approved

## Goal

Connect the completed, no-show, and unknown YCLIENTS visit outcomes to the
existing Phase 6 scheduler and notification flow before Production Admin work
starts.

## Constraints

- Work locally through Docker only.
- The next Alembic revision is `0008_yclients_lifecycle`, based on
  `0007_scheduler_notifications`.
- Do not mutate staging, production, or any provider resource.
- Do not execute live YCLIENTS requests during implementation or verification.
- Keep the current scheduler and worker services; do not add a polling daemon.
- Reuse the current YCLIENTS record ownership check based on
  `custom_fields.moroz_booking_key`.
- Keep production database rollouts forward-only.

## Source Contract

The YCLIENTS visit statuses are:

- `attendance = -1`: client did not arrive.
- `attendance = 0`: client is expected.
- `attendance = 1`: client arrived and the service was provided.
- `attendance = 2`: client confirmed the booking.

The provider's `deleted` flag takes precedence over attendance.

The adapter maps records as follows:

| Provider record | Local booking status |
|---|---|
| `deleted = true` | `cancelled` |
| `attendance = -1` | `no_show` |
| `attendance = 1` | `completed` |
| `attendance = 0` or `2` | `confirmed` |
| Missing or unsupported integer attendance | `unknown` |

A malformed attendance type, malformed record, transport error, or unsafe
ownership result is a temporary error. Existing RabbitMQ retry and DLQ handling
remain responsible for those failures.

References:

- YCLIENTS status reference: `https://support.yclients.com/442`
- YCLIENTS webhook field reference: `https://support.yclients.com/993`
- Alembic named CHECK operations:
  `https://alembic.sqlalchemy.org/en/latest/ops.html`

## Architecture

### Scheduled read-through

Normal reminders continue to read only PostgreSQL. The worker performs a
read-only `GET /api/v1/record/{company_id}/{record_id}` only for
`no_show_check` and `visit_outcome_check`.

The lifecycle port:

1. Loads the current local booking by `booking_key`.
2. Stops without a provider call when the booking is missing, cancelled, or the
   job is stale after a reschedule.
3. Reads the exact YCLIENTS record with the existing protected adapter.
4. Verifies `moroz_booking_key`.
5. Persists a changed lifecycle status only while the same local booking and
   start time are still current.
6. Returns the provider-derived scheduled end time for outcome planning.

### Outcome checks

The existing `no_show_check` remains scheduled at the visit start.

- `no_show`: send the existing idempotent client and staff messages.
- `completed`: persist the outcome and schedule feedback once.
- `cancelled`: skip as stale.
- `unknown`: send one idempotent staff alert and stop.
- `confirmed`: schedule `visit_outcome_check` relative to the provider-derived
  visit end.

Outcome checks use fixed offsets from the scheduled visit end:

1. 15 minutes
2. 2 hours
3. 24 hours

Each job payload contains its zero-based outcome-check index. If the booking is
still `confirmed`, the handler schedules the next offset. After the final
check, it sends one idempotent unresolved-outcome alert to staff and stops.
There is no unbounded business retry loop.

Transport and malformed-response failures use the existing worker retry limit;
they do not consume the business outcome-check index.

### Completion and feedback

YCLIENTS exposes the scheduled start and `seance_length`, not an authoritative
actual completion timestamp. For `attendance = 1`, the scheduled end
(`starts_at + seance_length`) is the deterministic `completed_at`.

The handler calls the existing `FeedbackService.schedule_after_visit`. That
service already provides:

- one feedback request per customer,
- a stable scheduler job,
- a two-hour delay,
- quiet-hours handling.

### Persistence and migration

Migration `0008_yclients_lifecycle` replaces the named
`ck_bookings_status` constraint:

```text
confirmed, cancelled
```

with:

```text
confirmed, cancelled, completed, no_show, unknown
```

The migration uses Alembic's named `drop_constraint` and
`create_check_constraint` operations. Its local-test downgrade normalizes the
three new statuses to `confirmed` before restoring the old constraint.
Production rollout remains forward-only.

The same migration adds nullable `bookings.scheduled_end_at TIMESTAMPTZ`.
Existing rows populate it on their first lifecycle read. Persisting the
provider-derived scheduled end lets a duplicate Rabbit delivery recover after
a crash between the status update and follow-up or feedback scheduling.

No new table, service, queue, or dependency is introduced.

## Idempotency and Concurrency

- Lifecycle follow-up keys include booking key, booking start, and check index.
- Existing notification outbox keys prevent duplicate client/staff messages.
- Existing feedback uniqueness prevents duplicate feedback requests.
- Status persistence is conditional on the booking key and unchanged start
  time, so a concurrent reschedule cannot revive a stale outcome.
- A locally cancelled booking never gets overwritten by a delayed provider
  response.
- Duplicate Rabbit deliveries remain harmless.

## Error Handling

- Provider transport, authentication, response-shape, and ownership errors are
  raised for bounded Rabbit retry and eventual DLQ.
- Unsupported integer or missing attendance becomes `unknown` and alerts staff.
- A final still-confirmed outcome alerts staff once as unresolved.
- Logs contain booking/job identifiers and safe reason codes, never tokens,
  customer names, phone numbers, or provider response bodies.

## Verification

All verification runs through Docker and fake/local dependencies:

- unit tests for status mapping and scheduled end calculation;
- handler tests for completed, no-show, unknown, stale, and bounded follow-ups;
- PostgreSQL integration tests for status transitions, idempotency, and
  reschedule/cancel races;
- fake HTTP contract tests proving lifecycle reads use only GET;
- Alembic upgrade/current and local downgrade/upgrade coverage;
- focused notification/booking/worker tests;
- complete Docker pytest suite;
- worker/scheduler image build and compile gate;
- exact Compose namespace cleanup.

No staging, production, live Telegram, or live YCLIENTS action is part of this
checkpoint.
