# Production Telegram V1 Master Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Выпустить production-ready Telegram AI-оператора центра «Мороз и Солнце» с YCLIENTS, устойчивой обработкой сообщений, сервисными уведомлениями, защищённым LLM-контуром и операционной админкой.

**Architecture:** Модульный монолит в одном репозитории с общим Python-пакетом и четырьмя Docker-процессами: `bot`, `admin`, `worker`, `scheduler`. PostgreSQL хранит критическое состояние, Redis — временное, RabbitMQ — доставляет фоновые задачи, YCLIENTS остаётся источником правды для записи.

**Tech Stack:** Python 3.12, aiogram 3.x, FastAPI, Jinja2, asyncpg, Alembic, Redis 7, RabbitMQ, aio-pika, PostgreSQL 16, Docker Compose, pytest.

## Global Constraints

- Проект запускается и проверяется только через Docker Compose; прямой `python bot.py` запрещён.
- Первый клиентский канал — только Telegram.
- Голос, ЮKassa, массовые рассылки, реактивация, новые каналы и медицинские анкеты не реализуются в V1.
- До согласия текст пользователя не сохраняется и не передаётся внешней LLM.
- PostgreSQL — единственное хранилище критического состояния; Redis и RabbitMQ не заменяют его.
- YCLIENTS — источник актуальных слотов, записей и статусов.
- Изменяющие действия требуют явного подтверждения и повторной проверки внешнего состояния.
- Один диалог обрабатывается последовательно; разные диалоги могут идти параллельно.
- Ответ LLM исправляется не более одного раза; затем fallback или эскалация.
- Напоминания: сразу, за 24 часа, в 09:00, за 1 час, no-show в момент начала.
- Post-visit оценка и ссылка Яндекс Карт запрашиваются один раз за всю историю клиента.
- Production блокируется без YCLIENTS API, юридических текстов, ротации секретов, алертов и проверенного восстановления бэкапа.
- Runtime использует текущие aiogram/FastAPI/Jinja2/asyncpg; отдельный SPA и новый ORM не добавляются.
- Все миграции выполняются Alembic отдельным шагом; runtime не создаёт таблицы.
- Каждая задача плана идёт через red → green → полный Docker-check → отдельный коммит.

## Плановая структура

```text
project/
├── src/moroz/
│   ├── common/          # config, db pool, logging, ids, clock
│   ├── messaging/       # incoming, inbox/outbox, buffer, Telegram pipeline
│   ├── booking/         # state machine и YCLIENTS adapter
│   ├── security/        # consent, PII, guardrails, validator
│   ├── notifications/   # scheduler jobs, reminders, feedback
│   └── escalation/      # human mode и служебные задачи
├── llm/                 # bot entrypoint и LLM provider adapters
├── admin/               # FastAPI/Jinja2 UI
├── worker/              # RabbitMQ consumer entrypoint
├── scheduler/           # due-job publisher entrypoint
├── migrations/          # Alembic revisions
├── tests/unit/
├── tests/integration/
├── tests/contract/
├── tests/e2e/
└── ops/                 # Caddy, backup, restore, runbooks, smoke/load
```

## Общие контракты

```python
@dataclass(frozen=True, slots=True)
class IncomingMessage:
    message_id: str
    channel: str
    chat_id: str
    user_id: str
    text: str
    received_at: datetime
    correlation_id: UUID

@dataclass(frozen=True, slots=True)
class ScenarioResult:
    status: Literal["ok", "needs_input", "escalated", "failed"]
    message: str
    next_action: str | None
    events: tuple[DomainEvent, ...]
    error_code: str | None = None

class BookingPort(Protocol):
    async def list_slots(self, query: SlotQuery) -> list[Slot]: ...
    async def create_booking(self, command: CreateBooking) -> ExternalBooking: ...
    async def reschedule_booking(self, command: RescheduleBooking) -> ExternalBooking: ...
    async def cancel_booking(self, command: CancelBooking) -> None: ...
    async def get_booking(self, external_id: str) -> ExternalBooking: ...

class QueuePort(Protocol):
    async def publish(self, task: QueueTask) -> None: ...

class LLMPort(Protocol):
    async def complete(self, request: LLMRequest) -> LLMResponse: ...
```

Имена и типы этих контрактов сохраняются во всех фазовых планах.

## Порядок исполнения

| № | План | Проверяемый результат | Зависит от |
|---|---|---|---|
| 1 | [Foundation](2026-07-14-production-v1-foundation.md) | Общий пакет, Docker test service, Alembic, RabbitMQ, worker/scheduler skeleton | — |
| 2 | [Reliable Telegram Pipeline](2026-07-14-production-v1-telegram-pipeline.md) | Webhook → privacy gate → buffer → inbox/outbox → queue → delivery | 1 |
| 3 | [YCLIENTS Booking](2026-07-14-production-v1-yclients-booking.md) | Создание, перенос, отмена, конфликт и fallback через mock/real adapter | 1–2 |
| 4 | [LLM Security](2026-07-14-production-v1-llm-security.md) | Маскирование, guardrails, primary/reserve, router, validator, evals | 1–2 |
| 5 | [Scheduler & Notifications](2026-07-14-production-v1-notifications.md) | Напоминания, no-show, feedback once, эскалации | 1–4 |
| 6 | [Production Admin](2026-07-14-production-v1-admin.md) | Роли, TOTP, диалоги, записи, знания, health и аудит | 1–5 |
| 7 | [Operations & Release](2026-07-14-production-v1-operations.md) | TLS, алерты, backup/restore, load/failure tests, launch checklist | 1–6 |

## Review checkpoints

- После плана 1: старый функционал запускается, schema создаётся только миграциями, тесты работают в Docker.
- После плана 2: один текстовый Telegram-запрос проходит устойчивый pipeline без YCLIENTS.
- После плана 3: mock YCLIENTS проходит E2E; реальный adapter включается только при доступе.
- После плана 4: критические security/eval кейсы проходят, fallback проверен.
- После плана 5: виртуальные часы подтверждают все напоминания и единственный feedback.
- После плана 6: роли и TOTP закрывают ПД, операционные экраны работают.
- После плана 7: backup восстановлен, нагрузка пройдена, launch gates подписаны.

## Общая проверка после каждого плана

```powershell
Set-Location project
docker compose --profile test build test
docker compose --profile test run --rm test pytest -q
docker compose config --quiet
docker compose up -d --build
docker compose ps
docker compose logs --since=5m bot admin worker scheduler
```

Ожидается: pytest завершился без FAIL, Compose-конфигурация валидна, обязательные контейнеры healthy/running, в свежих логах нет traceback.
