# Scheduler and Notifications Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Реализовать устойчивые scheduler jobs для напоминаний, no-show, единственного feedback и эскалаций.

**Architecture:** Стартовая точка фазы — `origin/main` / `HEAD` `e8d95de3a580cd2b90feabaf91e43db611fcb8b2`, текущий Alembic head — `0006_yclients_booking_key`; следующая schema migration должна быть ровно `0007_scheduler_notifications` с `down_revision = "0006_yclients_booking_key"`. Scheduler только claim-ит наступившие PostgreSQL jobs и публикует QueueTask; worker проверяет актуальную запись через существующую локальную booking-модель и mock/fake ports, выполняет действие и фиксирует результат. Виртуальные часы делают расписание детерминированно тестируемым.

**Tech Stack:** Python datetime/zoneinfo, asyncpg, RabbitMQ, Telegram/YCLIENTS ports, pytest.

## Global Constraints

- Timezone `Europe/Moscow`.
- Старые jobs инвалидируются переносом/отменой.
- Совпавшие утреннее и часовое сообщения объединяются.
- Неизвестный YCLIENTS status не считается no-show.
- Feedback отправляется один раз на customer.
- Работать локально только через Docker/Compose test profiles; не выполнять staging, production, live Telegram, live YCLIENTS или LLM-provider mutations.
- Использовать существующие `bookings.booking_key`, `bookings.status`, `bookings.starts_at`, `bookings.customer_id` и `booking_events`; не вводить зависимость от несуществующей таблицы `customers`.

---

### Task 1: Scheduler job repository and claimer

**Files:** Create `project/src/moroz/notifications/models.py`, `repository.py`; Create migration `project/migrations/versions/0007_scheduler_notifications.py`; Modify `project/tests/integration/test_migrations.py`; Test `project/tests/integration/notifications/test_jobs.py`; Modify `project/scheduler/main.py`.

- [x] Write concurrent claim test proving two schedulers cannot claim the same due job.
- [x] Run red.
- [x] Add one additive migration `0007_scheduler_notifications` with `down_revision = "0006_yclients_booking_key"` that creates:
  - `scheduler_jobs(id UUID PRIMARY KEY, kind TEXT, run_at TIMESTAMPTZ, payload JSONB, idempotency_key TEXT UNIQUE, status TEXT, attempts INTEGER, booking_key UUID NULL, booking_starts_at TIMESTAMPTZ NULL, claimed_at TIMESTAMPTZ NULL, finished_at TIMESTAMPTZ NULL, last_error_code TEXT NULL, created_at TIMESTAMPTZ, updated_at TIMESTAMPTZ)`;
  - `notification_feedback_requests(id UUID PRIMARY KEY, customer_id TEXT UNIQUE, booking_key UUID NULL, requested_at TIMESTAMPTZ, created_at TIMESTAMPTZ)`;
  - `escalations(id UUID PRIMARY KEY, source TEXT, customer_id TEXT, booking_key UUID NULL, status TEXT, reason_code TEXT, payload JSONB, created_at TIMESTAMPTZ, resolved_at TIMESTAMPTZ NULL)`;
  - `human_mode(customer_id TEXT PRIMARY KEY, enabled BOOLEAN, reason_code TEXT, escalation_id UUID NULL, enabled_at TIMESTAMPTZ, expires_at TIMESTAMPTZ NULL)`.
- [x] Add indexes for due scheduler claims and booking invalidation: `(status, run_at)`, `(booking_key, status)`.
- [x] Implement claimer:

```sql
SELECT id FROM scheduler_jobs
WHERE status='pending' AND run_at <= now()
ORDER BY run_at
FOR UPDATE SKIP LOCKED
LIMIT $1;
```

- [x] Run migration/test; expect each ID claimed once.
- [x] Commit `feat: добавлено устойчивое хранилище scheduler jobs`.

### Task 2: Reminder planner

**Files:** Create `project/src/moroz/notifications/planner.py`; Test `project/tests/unit/notifications/test_planner.py`.

- [x] Test booking at 15:00 creates immediate, -24h, 09:00 and -1h jobs; booking at 09:30 merges morning/hour job; late booking skips past jobs.
- [x] Run red.
- [x] Implement:

```python
times = {
    "booking_created": now,
    "day_before": starts_at - timedelta(hours=24),
    "morning": datetime.combine(starts_at.date(), time(9), tzinfo=MOSCOW),
    "hour_before": starts_at - timedelta(hours=1),
    "no_show_check": starts_at,
}
return merge_close_jobs([job for job in times.items() if job[1] >= now], within=timedelta(minutes=15))
```

- [x] Run test; expect exact timestamps and stable idempotency keys `booking:{booking_key}:{starts_at}:{kind}`.
- [x] Commit `feat: добавлен график напоминаний`.

### Task 3: Reminder and no-show workers

**Files:** Create `project/src/moroz/notifications/handlers.py`; Test `project/tests/e2e/notifications/test_reminders.py`.

- [x] Test normal reminder, cancelled booking skip, no-show client+staff, unknown status staff technical alert only.
- [x] Run red.
- [x] Implement status recheck before send:

```python
booking = await booking_port.get_booking(job.external_booking_id)
if booking.version != job.booking_version or booking.status == "cancelled":
    return JobResult.skipped("stale")
if job.kind == "no_show_check" and booking.status == "no_show":
    await outbox.client_waiting(booking)
    await outbox.staff_no_show(booking)
```

- [x] Run E2E; expect exact recipient counts.
- [x] Commit `feat: добавлены reminder и no-show handlers`.

### Task 4: Feedback once and escalation human mode

**Files:** Create `project/src/moroz/notifications/feedback.py`, `project/src/moroz/escalation/service.py`; Reuse migration `project/migrations/versions/0007_scheduler_notifications.py` from Task 1; Test `project/tests/e2e/notifications/test_feedback.py`.

- [x] Test first completed visit schedules feedback +2h, after 21:00 moves to next 10:30, daily later visits never schedule another, rating 1–3 creates escalation.
- [x] Run red.
- [x] Atomically claim feedback in `notification_feedback_requests` instead of `customers.feedback_requested_at`:

```sql
INSERT INTO notification_feedback_requests
    (id, customer_id, booking_key, requested_at, created_at)
VALUES ($1, $2, $3, now(), now())
ON CONFLICT (customer_id) DO NOTHING
RETURNING id;
```

- [x] Run tests; expect one feedback and no sales on low rating.
- [x] Commit `feat: добавлены feedback once и human mode`.

### Task 5: Notifications checkpoint

- [ ] Run all notification tests with virtual clock; expect pass.
- [ ] Advance test clock through a complete booking lifecycle; expect no duplicate jobs/messages.
- [ ] Inspect DLQ behavior for a forced Telegram failure.
- [ ] Run `docker compose --env-file ../.env run --rm test alembic -c /workspace/alembic.ini upgrade head`; expect exact head `0007_scheduler_notifications` in the isolated test database only.
- [ ] Run the full Docker pytest gate; expect no skipped Phase 6 tests.
- [ ] Update roadmap/changelog with evidence.
- [ ] Commit `docs: зафиксирован notifications checkpoint`.
