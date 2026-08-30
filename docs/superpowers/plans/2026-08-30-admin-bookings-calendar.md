# Admin Bookings Calendar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Превратить существующую вкладку «Записи» в недельный YCLIENTS-календарь с ручным созданием и действиями по статусу через worker.

**Architecture:** Админка читает текущую объединённую проекцию и пишет только валидированные `scheduler_jobs` плюс audit. Worker, уже владеющий YCLIENTS credentials, выполняет команды существующим адаптером и инициирует обновление проекции. Новых ролей, внешних сервисов и параллельной базы записей нет.

**Tech Stack:** Python 3.12, FastAPI/Jinja2, asyncpg/PostgreSQL, aiogram worker, существующий YCLIENTS adapter, Docker Compose, pytest.

## Global Constraints

- Все проверки запускаются только через Docker.
- Рабочая ветка: `codex/admin-zapisi`; merge/push/staging/production запрещены.
- YCLIENTS остаётся источником истины; admin не получает его секреты.
- Только существующие роли `owner` и `admin`; обе видят все записи.
- Минимальный diff и переиспользование существующих таблиц/очереди.

---

### Task 1: Недельное read-модель и календарь

**Files:**
- Modify: `project/admin/booking_views.py`
- Modify: `project/admin/bookings_database.py`
- Modify: `project/admin/booking_routes.py`
- Modify: `project/admin/templates/bookings.html`
- Modify: `project/admin/static/styles.css`
- Test: `project/tests/unit/admin/test_booking_views.py`
- Test: `project/tests/integration/admin/test_admin_bookings_postgres.py`

**Interfaces:**
- Produces: `week_bounds(value, now)`, `calendar_layout(items, week_start)`, `list_calendar_bookings(database, week_start, week_end)`.

- [x] Написать unit-тесты понедельника, перехода недель, московской зоны и раскладки карточек по дням/минутам.
- [x] Запустить Docker RED и подтвердить ожидаемое отсутствие функций.
- [x] Реализовать минимальные helpers и SQL exact `[week_start, week_end)` поверх существующего unified CTE.
- [x] Переключить GET route/template на неделю, добавить навигацию, карточки, состояние пустой недели и responsive CSS.
- [x] Запустить focused Docker GREEN и закоммитить `feat: добавлен недельный календарь записей`.

### Task 2: Ручная запись и действия через worker

**Files:**
- Create: `project/src/moroz/booking/admin_commands.py`
- Modify: `project/src/moroz/booking/yclients.py`
- Modify: `project/worker/main.py`
- Modify: `project/admin/bookings_database.py`
- Modify: `project/admin/booking_routes.py`
- Modify: `project/admin/templates/bookings.html`
- Modify: `project/admin/booking_views.py`
- Test: `project/tests/unit/booking/test_admin_commands.py`
- Test: `project/tests/unit/booking/test_yclients_adapter.py`
- Test: `project/tests/unit/admin/test_booking_views.py`
- Test: `project/tests/unit/test_worker.py`

**Interfaces:**
- Consumes: `scheduler_jobs`, `BookingService`, `BookingRepository`, `YclientsAdapter`, `ProjectionSyncCoordinator`.
- Produces: job kinds `admin_booking_create` and `admin_booking_status`; `YclientsAdapter.set_visit_status(external_id, status)`.

- [x] Написать RED-тесты валидного payload, точного выбора слота, идемпотентного создания и mapping `completed/no_show/cancelled`.
- [x] Проверить POST boundary: существующие RBAC/CSRF, allowlist полей, audit без PII и enqueue без YCLIENTS secrets в admin.
- [x] Реализовать атомарную постановку job и загрузку существующего service catalog для формы.
- [x] Реализовать worker command service: create через `BookingService`, status/cancel через `YclientsAdapter`, локальное событие для bot-owned записи и внеочередной projection job.
- [x] Добавить формы и действия в карточку; для terminal status скрыть недопустимые кнопки.
- [x] Запустить Docker GREEN и закоммитить `feat: добавлены команды управления записями`.

### Task 3: Документы и закрывающая проверка

**Files:**
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`
- Verify: relevant admin/booking/worker tests and Compose config.

**Interfaces:**
- Produces: воспроизводимое evidence и список конфликтных shared files.

- [x] Обновить roadmap фактическим результатом и ограничениями ролей/YCLIENTS.
- [x] Записать RED/GREEN, baseline checksum blocker и отсутствие внешних действий в changelog.
- [x] Запустить свежий Docker focused regression и Compose config-check.
- [x] Проверить diff, секреты и общую навигацию; выполнить code review и исправить Critical/Important замечания.
- [x] Закоммитить `docs: зафиксирована вкладка записей` и сообщить ветку, коммиты, тесты и merge-конфликты.
