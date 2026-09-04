# Telegram Booking Moscow Time Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Показывать клиенту даты записи только по Москве в читаемом формате и не отправлять повторное немедленное подтверждение после переноса.

**Architecture:** Один чистый formatter переводит timezone-aware `datetime` в `Europe/Moscow`; booking service и notification renderer используют его только на границе Telegram-текста. Repository сообщает planner, является ли сохранение первичным созданием, чтобы перенос перепланировал будущие напоминания без нового `booking_created`.

**Tech Stack:** Python 3.12, stdlib `zoneinfo`, pytest, Docker Compose.

## Global Constraints

- Внутренние datetime, БД, YCLIENTS API и idempotency keys остаются timezone-aware ISO.
- Клиентский формат: `ДД.ММ.ГГГГ в ЧЧ:ММ`, timezone `Europe/Moscow`.
- Новые зависимости и миграции запрещены.
- Не изменять файлы параллельной задачи: `project/llm/webhook.py`, `project/src/moroz/messaging/telegram.py`, `project/src/moroz/booking/telegram.py`, `project/worker/main.py` и её тесты.

---

### Task 1: Единый клиентский formatter

**Files:**
- Create: `project/src/moroz/booking/time_display.py`
- Create: `project/tests/unit/booking/test_time_display.py`
- Modify: `project/src/moroz/booking/service.py:381-447`
- Modify: `project/src/moroz/notifications/ports.py:110-121`
- Test: `project/tests/e2e/booking/test_change_booking.py`

**Interfaces:**
- Consumes: timezone-aware `datetime` или его ISO-строку из scenario state.
- Produces: `format_booking_time(value: datetime | str) -> str`.

- [ ] **Step 1: Write the failing formatter and message tests**

```python
def test_formats_utc_as_moscow_time():
    assert format_booking_time("2026-09-10T09:00:00+00:00") == "10.09.2026 в 12:00"

def test_rejects_naive_datetime():
    with pytest.raises(ValueError, match="timezone-aware"):
        format_booking_time(datetime(2026, 9, 10, 12))
```

Update create/reschedule/cancel/reminder assertions to require the same Moscow format and reject raw `T`, `+00:00`, `+03:00` output.

- [ ] **Step 2: Run RED in Docker**

Run:
```bash
docker compose --env-file ../.env run --rm test pytest -q tests/unit/booking/test_time_display.py tests/e2e/booking/test_change_booking.py
```

Expected: FAIL because `moroz.booking.time_display` does not exist and current messages contain ISO strings.

- [ ] **Step 3: Implement the minimal formatter**

```python
from datetime import datetime
from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo("Europe/Moscow")

def format_booking_time(value: datetime | str) -> str:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("booking time must be timezone-aware")
    return parsed.astimezone(MOSCOW).strftime("%d.%m.%Y в %H:%M")
```

Use this function in `_create_terminal_result`, `_change_terminal_result` and `_reminder_text` without changing stored values.

- [ ] **Step 4: Run GREEN in Docker**

Run the Step 2 command plus the focused notification tests. Expected: PASS.

- [ ] **Step 5: Commit only Task 1 files**

```bash
git add project/src/moroz/booking/time_display.py project/src/moroz/booking/service.py project/src/moroz/notifications/ports.py project/tests/unit/booking/test_time_display.py project/tests/e2e/booking/test_change_booking.py
git commit -m "fix: время записей показано по Москве"
```

### Task 2: Без повторного подтверждения после переноса

**Files:**
- Modify: `project/src/moroz/notifications/planner.py:13-40`
- Modify: `project/src/moroz/booking/repository.py:376-410`
- Modify: `project/tests/unit/notifications/test_planner.py`
- Modify: `project/tests/integration/booking/test_booking_repository.py:282-325`

**Interfaces:**
- Consumes: `plan_booking_notifications(..., include_created: bool = True)`.
- Produces: первичное создание сохраняет `booking_created`; reschedule создаёт только будущие reminder/outcome jobs.

- [ ] **Step 1: Write failing planner and repository tests**

```python
jobs = plan_booking_notifications(
    booking_key=booking_key,
    starts_at=starts_at,
    now=now,
    include_created=False,
)
assert "booking_created" not in {job.kind for job in jobs}
```

In the repository reschedule test assert that new-time jobs exclude `booking_created` while old-time pending jobs are stale and future new-time jobs remain.

- [ ] **Step 2: Run RED in Docker**

Run:
```bash
docker compose --env-file ../.env run --rm test pytest -q tests/unit/notifications/test_planner.py tests/integration/booking/test_booking_repository.py::test_reschedule_replaces_old_notification_schedule
```

Expected: FAIL because `include_created` is not accepted and reschedule currently inserts `booking_created`.

- [ ] **Step 3: Implement the minimal planner flag**

Add keyword `include_created: bool = True`; filter `booking_created` when false. In `_complete_with_connection`, pass `include_created=scenario.kind == "create"` into `_sync_notification_jobs`, then into the planner. Do not alter create/cancel behavior.

- [ ] **Step 4: Run GREEN and focused regression in Docker**

Run the Step 2 command, then:
```bash
docker compose --env-file ../.env run --rm test pytest -q tests/unit/booking tests/unit/notifications tests/integration/booking tests/e2e/booking
```

Expected: PASS.

- [ ] **Step 5: Commit only Task 2 files**

```bash
git add project/src/moroz/notifications/planner.py project/src/moroz/booking/repository.py project/tests/unit/notifications/test_planner.py project/tests/integration/booking/test_booking_repository.py
git commit -m "fix: перенос не дублирует подтверждение записи"
```

### Task 3: Финальная проверка и проектные документы

**Files:**
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`

**Interfaces:**
- Consumes: зелёные focused tests и актуальный параллельный working tree.
- Produces: проверенный локальный кандидат без staging rollout до объединения параллельной задачи.

- [ ] **Step 1: Re-read shared files and preserve concurrent additions**

Read the current tails of `Дорожная карта.md` and `changelog.md`; append only this task's result.

- [ ] **Step 2: Run final Docker and static gates**

```bash
docker compose --env-file ../.env run --rm test pytest -q tests/unit/booking tests/unit/notifications tests/integration/booking tests/e2e/booking
docker compose --env-file ../.env config --quiet
git diff --check
```

Expected: all commands exit `0`; unrelated parallel changes remain untouched.

- [ ] **Step 3: Update roadmap and changelog**

Mark the Moscow-time task complete with exact test counts. Record root cause, fix, verification and that staging was not yet updated.

- [ ] **Step 4: Commit only shared documentation after checking concurrent state**

```bash
git add "Дорожная карта.md" changelog.md docs/superpowers/plans/2026-09-04-telegram-booking-time-display.md
git commit -m "docs: завершён московский формат времени записей"
```
