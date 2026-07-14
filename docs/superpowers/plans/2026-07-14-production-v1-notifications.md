# Scheduler and Notifications Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Реализовать устойчивые scheduler jobs для напоминаний, no-show, единственного feedback и эскалаций.

**Architecture:** Scheduler только claim-ит наступившие PostgreSQL jobs и публикует QueueTask; worker проверяет актуальную запись, выполняет действие и фиксирует результат. Виртуальные часы делают расписание детерминированно тестируемым.

**Tech Stack:** Python datetime/zoneinfo, asyncpg, RabbitMQ, Telegram/YCLIENTS ports, pytest.

## Global Constraints

- Timezone `Europe/Moscow`.
- Старые jobs инвалидируются переносом/отменой.
- Совпавшие утреннее и часовое сообщения объединяются.
- Неизвестный YCLIENTS status не считается no-show.
- Feedback отправляется один раз на customer.

---

### Task 1: Scheduler job repository and claimer

**Files:** Create `project/src/moroz/notifications/models.py`, `repository.py`; Create migration `0004_scheduler_jobs.py`; Test `project/tests/integration/notifications/test_jobs.py`; Modify `project/scheduler/main.py`.

- [ ] Write concurrent claim test proving two schedulers cannot claim the same due job.
- [ ] Run red.
- [ ] Add `scheduler_jobs(id, kind, run_at, payload, idempotency_key UNIQUE, status, attempts, booking_version, created_at)` and implement:

```sql
SELECT id FROM scheduler_jobs
WHERE status='pending' AND run_at <= now()
ORDER BY run_at
FOR UPDATE SKIP LOCKED
LIMIT $1;
```

- [ ] Run migration/test; expect each ID claimed once.
- [ ] Commit `feat: добавлено устойчивое хранилище scheduler jobs`.

### Task 2: Reminder planner

**Files:** Create `project/src/moroz/notifications/planner.py`; Test `project/tests/unit/notifications/test_planner.py`.

- [ ] Test booking at 15:00 creates immediate, -24h, 09:00 and -1h jobs; booking at 09:30 merges morning/hour job; late booking skips past jobs.
- [ ] Run red.
- [ ] Implement:

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

- [ ] Run test; expect exact timestamps and stable idempotency keys `booking:{id}:v{version}:{kind}`.
- [ ] Commit `feat: добавлен график напоминаний`.

### Task 3: Reminder and no-show workers

**Files:** Create `project/src/moroz/notifications/handlers.py`; Test `project/tests/e2e/notifications/test_reminders.py`.

- [ ] Test normal reminder, cancelled booking skip, no-show client+staff, unknown status staff technical alert only.
- [ ] Run red.
- [ ] Implement status recheck before send:

```python
booking = await booking_port.get_booking(job.external_booking_id)
if booking.version != job.booking_version or booking.status == "cancelled":
    return JobResult.skipped("stale")
if job.kind == "no_show_check" and booking.status == "no_show":
    await outbox.client_waiting(booking)
    await outbox.staff_no_show(booking)
```

- [ ] Run E2E; expect exact recipient counts.
- [ ] Commit `feat: добавлены reminder и no-show handlers`.

### Task 4: Feedback once and escalation human mode

**Files:** Create `project/src/moroz/notifications/feedback.py`, `project/src/moroz/escalation/service.py`; Create migration `0005_feedback_escalations.py`; Test `project/tests/e2e/notifications/test_feedback.py`.

- [ ] Test first completed visit schedules feedback +2h, after 21:00 moves to next 10:30, daily later visits never schedule another, rating 1–3 creates escalation.
- [ ] Run red.
- [ ] Add `customers.feedback_requested_at`, `escalations`, `human_mode`; atomically claim feedback:

```sql
UPDATE customers SET feedback_requested_at=now()
WHERE id=$1 AND feedback_requested_at IS NULL
RETURNING id;
```

- [ ] Run tests; expect one feedback and no sales on low rating.
- [ ] Commit `feat: добавлены feedback once и human mode`.

### Task 5: Notifications checkpoint

- [ ] Run all notification tests with virtual clock; expect pass.
- [ ] Advance test clock through a complete booking lifecycle; expect no duplicate jobs/messages.
- [ ] Inspect DLQ behavior for a forced Telegram failure.
- [ ] Update roadmap/changelog with evidence.
- [ ] Commit `docs: зафиксирован notifications checkpoint`.
