# Reactivation V2 Implementation Plan

> **Integration note (2026-08-31):** план исполнялся в изолированной ветке с revision `0023_reactivation_v2`. При объединении с актуальным `origin/main`, где `0023_reactivation_draft` уже развёрнута на staging, migration без изменения её содержательной задачи линеаризована как `0024_reactivation_v2` после `0023_reactivation_draft`. Ниже сохранены исходные команды и evidence плана.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Реализовать owner-only раздел «Маркетинговые коммуникации» с одной безопасной программой реактивации уснувших клиентов: доказуемое рекламное согласие, детерминированный отбор, цепочка `основное сообщение + максимум одно напоминание`, немедленная отписка и измеримый результат до завершённого визита.

**Architecture:** Реактивация строится как тонкий доменный слой поверх существующих PostgreSQL, `scheduler_jobs`, durable outbox, Telegram sender, advisory delivery fence, admin audit и YCLIENTS projection. Новые таблицы хранят только проверенную identity/activity, события рекламного согласия, версии программы и journey/steps; runtime LLM, новый worker, отдельная очередь, универсальные сегменты и ручные массовые кампании не добавляются.

**Tech Stack:** Python 3.12, FastAPI, Jinja2, aiogram 3.27.0, asyncpg, Alembic, PostgreSQL 16, Redis, RabbitMQ, pytest, Docker Compose.

## Global Constraints

- Проект запускается и проверяется только через Docker Compose; прямой запуск Python на хосте запрещён.
- Единственный рабочий контур админки до отдельного rollout — staging; эта реализация локальная, без deploy, push и реальных отправок.
- Канонический owner-only URL — `/marketing/`; `/reactivation/` только перенаправляет на него с сохранением query string.
- Сейчас реализуется только автоматическая реактивация. Ручные рекламные рассылки, произвольные сегменты и общий campaign builder исключены.
- Клиент участвует только при доказанном отдельном marketing consent; processing consent и legacy-строки без proof не подходят.
- Identity только по подтверждённому YCLIENTS `client_id`; сопоставление по телефону и ручное связывание в первой версии исключены.
- Eligibility определяется только кодом и Postgres: завершённый визит, `max(last_completed_visit_at, last_meaningful_inbound_at)`, отсутствие будущей записи, активной journey, suppression, human mode, escalation и удаления.
- Допустимый inactivity threshold: `60`, `90` или `120` дней; default `90`. Cooldown не меньше inactivity threshold; default `90`.
- Цепочка: одно основное сообщение и максимум одно напоминание; reminder выключен либо через `3`, `5` или `7` дней, default `5`.
- Любое входящее действие клиента закрывает pending reminder; явный STOP сначала отзывает consent и ставит suppression, затем прекращает обработку до LLM.
- Тексты статические, versioned и owner-approved; runtime LLM, персональные placeholders и автоматические скидки запрещены.
- Quiet hours фиксированы: отправлять только `10:30–20:00 Europe/Moscow`.
- Историческая проекция YCLIENTS должна быть не старше `24` часов, будущие/изменённые записи — не старше `15` минут; partial/error всегда исключают отправку.
- Начальный режим всегда `dry_run`; `active` требует preview не старше `30` минут, успешный test send, legal approval и owner confirmation.
- Все клиентские отправки идут только через существующие `outbound_messages` + `task_outbox`; idempotency key — `reactivation:{journey_id}:{step_kind}`.
- `delivery_unknown` не повторяется и автоматически переводит программу в `paused`; `TelegramForbiddenError`/`TelegramNotFound` дают recipient suppression; `TelegramBadRequest` останавливает программу; `TelegramRetryAfter` остаётся retryable.
- Во время Telegram API call сохраняется существующий per-customer transaction advisory fence; send дополнительно берёт shared program lock, emergency stop — exclusive program lock.
- Raw phone, тексты диалогов, Telegram/YCLIENTS payload, токены и exception text не сохраняются в реактивационных таблицах и не попадают в логи/alerts.
- Primary outcomes: completed visit за `30` дней; secondary: booking за `14` дней, meaningful reply за `7` дней, opt-out, failed и delivery-unknown.
- Реальные YCLIENTS/Telegram вызовы, staging, production, deploy и git push требуют отдельного явного разрешения владельца.

## File map

- Create `project/migrations/versions/0023_reactivation_v2.py`: additive schema после `0022_admin_statistics`, индексы, constraints, legacy-safe downgrade.
- Create `project/src/moroz/reactivation/__init__.py`: публичные типы и константы пакета.
- Create `project/src/moroz/reactivation/policy.py`: pure validation, checksum, STOP detection, quiet-time и deterministic eligibility.
- Create `project/src/moroz/reactivation/repository.py`: consent/event materialization, program versions, preview, journey/step claims, pre-send guard, outcomes.
- Create `project/src/moroz/reactivation/activity.py`: подтверждение YCLIENTS identity и bounded full-history sync.
- Create `project/src/moroz/reactivation/service.py`: scheduler coordinators, dispatch orchestration, auto-pause и alerts.
- Modify `project/src/moroz/security/consent.py`: marketing grant/revoke/suppress API поверх event log и materialized state.
- Modify `project/llm/webhook.py`: сохранить текущий checkbox рекламы, `/marketing`, STOP и реактивационные callbacks до LLM.
- Modify `project/src/moroz/booking/yclients_records.py`: `client_id`, `record_created_at`, lookup одной записи и full-history pagination.
- Modify `project/src/moroz/booking/projection.py`: проецировать безопасные identity/activity поля без телефона.
- Modify `project/src/moroz/messaging/repository.py`: generic pre-send guard и transactionally linked delivery result.
- Modify `project/src/moroz/messaging/telegram.py`: точная классификация Telegram delivery outcomes.
- Modify `project/worker/main.py`: wiring двух scheduler kinds и reactivation-aware Telegram sender.
- Modify `project/admin/reactivation_database.py`: owner actions, preview и read models; legacy campaigns остаются read-only storage.
- Modify `project/admin/reactivation_routes.py`: канонические `/marketing/` routes и legacy redirect.
- Modify `project/admin/templates/reactivation.html`: новый экран программы, preview/gates/journeys/outcomes/consents.
- Modify `project/admin/templates/base.html`: пункт «Маркетинговые коммуникации».
- Modify `project/admin/app.py`: подключить canonical и legacy routers.
- Modify `project/admin/customer_data_deletion.py`: atomic deletion новых данных и связанных pending outbound.
- Modify `project/src/moroz/retention.py`: bounded retention для закрытых journey/activity/revoked consent events.
- Add focused unit, contract, integration и E2E tests under `project/tests/` alongside the touched modules.
- Update `ТЗ и архитектура.md`, `docs/architecture/moroz-i-solntse-full-architecture.html`, `Дорожная карта.md` and `changelog.md` after runtime acceptance.

---

### Task 1: Additive Reactivation V2 schema

**Files:**
- Create: `project/migrations/versions/0023_reactivation_v2.py`
- Create: `project/tests/unit/admin/test_migration_0023_reactivation_v2.py`
- Create: `project/tests/integration/reactivation/test_schema.py`
- Modify: `project/tests/integration/conftest.py`

**Interfaces:**
- Consumes: migration head `0022_admin_statistics`, existing `marketing_consents`, `reactivation_settings`, `yclients_booking_projection`, `admin_users`, `outbound_messages`.
- Produces: the five V2 tables and additive columns named in the schema contract below; later tasks must use these names exactly.

- [ ] **Step 1: Write migration contract tests**

Assert the exact revision chain, table/column/index names, status constraints, foreign keys and downgrade order:

```python
EXPECTED_TABLES = {
    "customer_activity_projection",
    "marketing_consent_events",
    "reactivation_program_versions",
    "reactivation_journeys",
    "reactivation_journey_steps",
}

def test_reactivation_v2_migration_contract(migration_source: str) -> None:
    assert 'revision = "0023_reactivation_v2"' in migration_source
    assert 'down_revision = "0022_admin_statistics"' in migration_source
    for table in EXPECTED_TABLES:
        assert f'"{table}"' in migration_source
    assert "legacy_unproven" in migration_source
    assert "delivery_unknown" in migration_source
    assert "reactivation:{journey_id}:{step_kind}" not in migration_source
```

In the Postgres test, upgrade to head, introspect `information_schema`/`pg_indexes`, insert one valid row per table, prove invalid enum/check values fail, downgrade to `0022_admin_statistics`, and prove all legacy tables/data still exist.

- [ ] **Step 2: Run the migration tests RED in Docker**

Run from `project/`:

```powershell
docker compose --env-file ../.env build test migrate
docker compose --env-file ../.env run --rm test pytest -q tests/unit/admin/test_migration_0023_reactivation_v2.py tests/integration/reactivation/test_schema.py
```

Expected: collection fails because migration `0023_reactivation_v2.py` does not exist.

- [ ] **Step 3: Implement the exact additive schema**

Use UUID primary keys, timezone-aware timestamps and DB checks. The migration contract is:

```python
REVISION = "0023_reactivation_v2"

ACTIVITY_COLUMNS = (
    "channel", "user_id", "yclients_client_id", "identity_status",
    "identity_source", "identity_verified_at", "last_completed_visit_at",
    "last_meaningful_inbound_at", "next_active_booking_at",
    "history_synced_at", "recent_bookings_synced_at", "source_version",
    "sync_status", "sync_error_code", "created_at", "updated_at",
)
CONSENT_EVENT_COLUMNS = (
    "id", "channel", "user_id", "action", "consent_version",
    "proof_text_hash", "source", "source_event_id", "occurred_at", "created_at",
)
PROGRAM_VERSION_COLUMNS = (
    "id", "version_number", "status", "inactivity_days", "reminder_enabled",
    "reminder_after_days", "cooldown_days", "main_text", "reminder_text",
    "template_checksum", "created_by", "created_at", "activated_by",
    "activated_at", "preview_created_at", "preview_checksum",
    "preview_counts", "preview_population_watermark",
    "preview_history_watermark", "preview_recent_watermark",
    "test_outbound_id", "test_sent_at",
)
JOURNEY_COLUMNS = (
    "id", "channel", "user_id", "program_version_id", "status",
    "close_reason", "activity_anchor_at", "first_sent_at", "replied_at",
    "booked_at", "completed_visit_at", "escalated_at", "created_at",
    "updated_at", "closed_at",
)
STEP_COLUMNS = (
    "id", "journey_id", "step_kind", "status", "due_at", "reserved_at",
    "sent_at", "outbound_id", "idempotency_key", "terminal_reason",
    "created_at", "updated_at",
)
```

Add these constraints/indexes:

```text
customer_activity_projection:
  PK(channel,user_id); identity_status in unverified/verified/conflict;
  sync_status in never/current/partial/error;
  partial unique yclients_client_id where identity_status='verified' and id is not null.
marketing_consent_events:
  action in granted/revoked/suppressed/unsuppressed;
  unique(channel,user_id,action,source,source_event_id).
reactivation_program_versions:
  unique(version_number); status in draft/active/retired;
  partial unique status where status='active';
  inactivity_days in (60,90,120); reminder_after_days null or in (3,5,7);
  cooldown_days >= inactivity_days; text lengths 1..4096;
  FKs created_by/activated_by -> admin_users.id SET NULL;
  FK test_outbound_id -> outbound_messages.id SET NULL.
reactivation_journeys:
  status in scheduled/active/closed;
  close_reason null or in responded/booked/suppressed/exhausted/failed/cancelled/delivery_unknown/escalated;
  partial unique(channel,user_id) where status!='closed';
  FK program_version_id -> program_versions RESTRICT.
reactivation_journey_steps:
  step_kind in main/reminder; status in scheduled/reserved/sent/delivery_unknown/skipped/cancelled/failed;
  unique(journey_id,step_kind); unique(idempotency_key); unique(outbound_id) where not null;
  journey FK CASCADE, outbound FK SET NULL.
```

Alter existing tables exactly as follows:

```text
marketing_consents += source, proof_event_id, proof_text_hash,
  suppressed_at, suppression_reason, suppression_source;
proof_event_id FK -> marketing_consent_events.id SET NULL;
existing rows: source='legacy_unproven', proof_event_id=null, active=false.
reactivation_settings += mode default 'dry_run', active_version_id,
  legal_status default 'pending', legal_reference, legal_approved_at,
  legal_approved_by, program_revision default 1, stopped_at;
mode in dry_run/paused/active; legal_status in pending/approved/rejected;
active_version_id FK -> program_versions RESTRICT;
legal_approved_by FK -> admin_users.id SET NULL.
yclients_booking_projection += client_id, record_created_at.
```

Seed no active version and perform no data inference. Preserve old settings/campaign/delivery columns for rollback compatibility.

- [ ] **Step 4: Run upgrade/downgrade and schema tests GREEN**

```powershell
docker compose --env-file ../.env run --rm test pytest -q tests/unit/admin/test_migration_0021_reactivation.py tests/unit/admin/test_migration_0023_reactivation_v2.py tests/integration/reactivation/test_schema.py
docker compose --env-file ../.env run --rm migrate alembic upgrade head
docker compose --env-file ../.env run --rm migrate alembic current
docker compose --env-file ../.env run --rm migrate alembic heads
```

Expected: all tests pass; both Alembic commands print one head, `0023_reactivation_v2`.

- [ ] **Step 5: Commit the schema**

```powershell
git add project/migrations/versions/0023_reactivation_v2.py project/tests/unit/admin/test_migration_0023_reactivation_v2.py project/tests/integration/reactivation/test_schema.py project/tests/integration/conftest.py changelog.md
git commit -m "feat: добавить схему реактивации v2"
```

---

### Task 2: Pure policy and eligibility contract

**Files:**
- Create: `project/src/moroz/reactivation/__init__.py`
- Create: `project/src/moroz/reactivation/policy.py`
- Create: `project/tests/unit/reactivation/test_policy.py`

**Interfaces:**
- Consumes: only stdlib `dataclasses`, `datetime`, `hashlib`, `json`, `re`, `zoneinfo`.
- Produces: `ProgramPolicy`, `EligibilityInput`, `EligibilityDecision`, `validate_policy`, `template_checksum`, `evaluate_eligibility`, `next_send_at`, `is_stop_request`.

- [ ] **Step 1: Write the boundary matrix RED**

Use fixed UTC datetimes and exact expectations:

```python
@pytest.mark.parametrize(
    ("inactive_days", "eligible", "reason"),
    [(89, False, "recent_activity"), (90, True, "eligible"), (91, True, "eligible")],
)
def test_inactivity_boundary(inactive_days, eligible, reason):
    decision = evaluate_eligibility(
        make_input(inactive_days=inactive_days), ProgramPolicy(), NOW
    )
    assert (decision.eligible, decision.reason) == (eligible, reason)

@pytest.mark.parametrize(
    "change",
    [
        {"deletion_active": True}, {"identity_status": "unverified"},
        {"consent_proven": False}, {"consent_active": False},
        {"suppressed": True}, {"sync_status": "partial"},
        {"history_synced_at": NOW - timedelta(hours=24, seconds=1)},
        {"recent_bookings_synced_at": NOW - timedelta(minutes=15, seconds=1)},
        {"next_active_booking_at": NOW + timedelta(days=1)},
        {"has_active_journey": True},
        {"latest_journey_started_at": NOW - timedelta(days=89)},
        {"human_mode": True}, {"has_open_escalation": True},
    ],
)
def test_every_safety_gate_excludes(change):
    assert evaluate_eligibility(make_input(**change), ProgramPolicy(), NOW).eligible is False
```

Also assert reason priority, `max(visit,inbound)` anchor, Moscow quiet-time edges, policy allowed values, stable checksum, exact STOP phrases (`стоп`, `stop`, `не писать`, `отписаться`, `не присылайте`) and non-match for ordinary questions containing similar words.

- [ ] **Step 2: Run RED in Docker**

```powershell
docker compose --env-file ../.env run --rm test pytest -q tests/unit/reactivation/test_policy.py
```

Expected: import fails for `moroz.reactivation.policy`.

- [ ] **Step 3: Implement typed pure functions**

Use this public contract:

```python
DEFAULT_MAIN_TEXT = (
    "Здравствуйте! Вы давно не были в «Мороз и Солнце». "
    "Если захотите вернуться, я помогу подобрать процедуру и удобное время. "
    "Можно сразу начать запись или задать вопрос."
)
DEFAULT_REMINDER_TEXT = (
    "Ненавязчиво напомню: если захотите вернуться в «Мороз и Солнце», "
    "я помогу с выбором процедуры и записью. "
    "Если такие сообщения не нужны, нажмите «Не писать»."
)
REACTIVATION_RENDERER_VERSION = "reactivation-renderer-v1"

@dataclass(frozen=True, slots=True)
class ProgramPolicy:
    inactivity_days: Literal[60, 90, 120] = 90
    reminder_after_days: Literal[3, 5, 7] | None = 5
    cooldown_days: int = 90
    main_text: str = DEFAULT_MAIN_TEXT
    reminder_text: str = DEFAULT_REMINDER_TEXT

@dataclass(frozen=True, slots=True)
class EligibilityInput:
    identity_status: Literal["unverified", "verified", "conflict"]
    consent_active: bool
    consent_proven: bool
    suppressed: bool
    last_completed_visit_at: datetime | None
    last_meaningful_inbound_at: datetime | None
    next_active_booking_at: datetime | None
    history_synced_at: datetime | None
    recent_bookings_synced_at: datetime | None
    sync_status: Literal["never", "current", "partial", "error"]
    has_active_journey: bool
    latest_journey_started_at: datetime | None
    human_mode: bool
    has_open_escalation: bool
    deletion_active: bool

@dataclass(frozen=True, slots=True)
class EligibilityDecision:
    eligible: bool
    reason: str
    activity_anchor_at: datetime | None

REASON_PRIORITY = (
    "deletion", "no_verified_identity", "identity_conflict",
    "no_proven_consent", "consent_revoked", "suppressed",
    "stale_history", "stale_recent_bookings", "partial_sync",
    "no_completed_visit",
    "recent_activity", "future_booking", "active_journey",
    "cooldown", "human_mode", "open_escalation",
)
```

The exact evaluator signature is `evaluate_eligibility(value: EligibilityInput, policy: ProgramPolicy, now: datetime) -> EligibilityDecision`; it validates timezone-aware timestamps, checks the fixed 24-hour/15-minute freshness contract and computes cooldown from `latest_journey_started_at`.

`template_checksum` must hash canonical JSON with the policy, both texts, renderer version and fixed button label/callback contract. A code change to final rendering/buttons must bump `REACTIVATION_RENDERER_VERSION` and invalidate old previews. `next_send_at` converts to `Europe/Moscow`, clamps before `10:30` to that day, after `20:00` to next day `10:30`, then converts back to UTC. STOP normalization is NFKC + lowercase + collapsed whitespace + stripped terminal punctuation; match only the allowlisted full normalized phrase.

- [ ] **Step 4: Run policy tests GREEN**

```powershell
docker compose --env-file ../.env run --rm test pytest -q tests/unit/reactivation/test_policy.py
```

Expected: all policy cases pass.

- [ ] **Step 5: Commit policy**

```powershell
git add project/src/moroz/reactivation project/tests/unit/reactivation/test_policy.py changelog.md
git commit -m "feat: добавить правила отбора реактивации"
```

---

### Task 3: Proven marketing consent and immediate opt-out

**Files:**
- Modify: `project/src/moroz/security/consent.py`
- Modify: `project/llm/config.py`
- Modify: `project/llm/webhook.py`
- Create: `project/tests/unit/security/test_marketing_consent.py`
- Modify: `project/tests/e2e/test_privacy_gate.py`
- Create: `project/tests/integration/reactivation/test_marketing_consent.py`

**Interfaces:**
- Consumes: existing processing-consent flow, Telegram callback update IDs, migration tables from Task 1.
- Produces: `MARKETING_CONSENT_VERSION = "marketing-v1"`; `ConsentService.grant_marketing`, `revoke_marketing`, `suppress_marketing`, `unsuppress_marketing`, `get_marketing_status`; `/marketing` status controls and STOP short-circuit.

- [ ] **Step 1: Write consent state-machine tests RED**

Cover exact durable transitions and idempotency:

```python
async def test_ads_checkbox_grants_proven_marketing_consent(app, telegram):
    await send_callback(app, data="consent:toggle:ads", update_id=101)
    await send_callback(app, data="consent:done", update_id=102)
    state = await consent_service.get_marketing_status("telegram", USER_ID)
    assert state.active is True
    assert state.consent_version == "marketing-v1"
    assert state.proof_text_hash == sha256(rendered_marketing_clause.encode()).hexdigest()
    assert state.source_event_id == "102"
    assert telegram.answered_callback_ids == [CALLBACK_1, CALLBACK_2]

async def test_stop_revokes_and_suppresses_before_llm(app, llm):
    await send_text(app, "Не писать", update_id=103)
    assert await current_state() == (False, "user_stop")
    assert llm.calls == []
```

Also test: unchecked ads does not grant; duplicate source event adds no duplicate; `/marketing` enable creates a new grant/proof; disable revokes+suppress; explicit new opt-in emits `unsuppressed` then `granted`; admin cannot grant; every callback query is answered; callback retries are idempotent.

- [ ] **Step 2: Run consent tests RED in Docker**

```powershell
docker compose --env-file ../.env run --rm test pytest -q tests/unit/security/test_marketing_consent.py tests/e2e/test_privacy_gate.py tests/integration/reactivation/test_marketing_consent.py
```

Expected: new marketing methods and `/marketing` controls are absent.

- [ ] **Step 3: Implement event-first consent materialization**

Expose this state and version:

```python
MARKETING_CONSENT_VERSION = "marketing-v1"

@dataclass(frozen=True, slots=True)
class MarketingConsentState:
    consent_id: UUID | None
    active: bool
    consent_version: str | None
    proof_text_hash: str | None
    source: str | None
    source_event_id: str | None
    suppressed: bool
    suppression_reason: str | None
```

Use these exact methods:

```text
ConsentService.grant_marketing(*, channel: str, user_id: str,
  proof_text: str, source: str, source_event_id: str, occurred_at: datetime,
  connection: asyncpg.Connection | None = None) -> MarketingConsentState
ConsentService.revoke_marketing(*, channel: str, user_id: str,
  source: str, source_event_id: str, occurred_at: datetime,
  connection: asyncpg.Connection | None = None) -> MarketingConsentState
ConsentService.suppress_marketing(*, channel: str, user_id: str, reason: str,
  source: str, source_event_id: str, occurred_at: datetime,
  connection: asyncpg.Connection | None = None) -> MarketingConsentState
ConsentService.unsuppress_marketing(*, channel: str, user_id: str,
  proof_text: str, source: str, source_event_id: str, occurred_at: datetime,
  connection: asyncpg.Connection | None = None) -> MarketingConsentState
```

Each method either uses the supplied transaction connection or opens one transaction, acquires the existing customer advisory lock, inserts an idempotent `marketing_consent_events` row, then upserts `marketing_consents`. Revoke/suppress also cancel pending reactivation steps in that transaction. A proven active state requires non-null `proof_event_id` and `proof_text_hash`; legacy rows remain inactive. Do not log proof text or user ID.

In `webhook.py`, preserve the exact rendered advertising clause used in the current checkbox, hash that string, and persist processing consent plus the optional marketing grant in the same customer-locked transaction when `consent:done` is confirmed with `ads=True`. Add deterministic `/marketing` buttons `marketing:enable` and `marketing:disable`. Call `await telegram.answer_callback_query(callback.id)` for every handled callback. Detect STOP after deletion handling and before pause/non-text/LLM branches.

- [ ] **Step 4: Run consent regressions GREEN**

```powershell
docker compose --env-file ../.env run --rm test pytest -q tests/unit/security/test_marketing_consent.py tests/e2e/test_privacy_gate.py tests/integration/reactivation/test_marketing_consent.py
```

Expected: new tests and existing processing-consent tests pass.

- [ ] **Step 5: Commit consent**

```powershell
git add project/src/moroz/security/consent.py project/llm/config.py project/llm/webhook.py project/tests/unit/security/test_marketing_consent.py project/tests/e2e/test_privacy_gate.py project/tests/integration/reactivation/test_marketing_consent.py changelog.md
git commit -m "feat: добавить доказуемое рекламное согласие"
```

---

### Task 4: Verified YCLIENTS identity and bounded activity sync

**Files:**
- Modify: `project/src/moroz/booking/yclients_records.py`
- Modify: `project/src/moroz/booking/projection.py`
- Create: `project/src/moroz/reactivation/activity.py`
- Modify: `project/tests/contract/booking/test_yclients_records.py`
- Modify: `project/tests/unit/booking/test_projection_sync.py`
- Create: `project/tests/unit/reactivation/test_activity_sync.py`
- Create: `project/tests/integration/reactivation/test_activity_projection.py`

**Interfaces:**
- Consumes: YCLIENTS `GET /api/v1/record/{company_id}/{record_id}` and paginated `GET /api/v1/records/{company_id}?client_id=...&page=...&count=100&with_deleted=1`; existing local `bookings.external_id` and `moroz_booking_key` ownership proof.
- Produces: `ProjectionRecord.client_id`, `ProjectionRecord.record_created_at`, `ClientActivitySnapshot`, `YclientsClientHistoryReader`, `ActivitySyncCoordinator.ensure_current/run`.

- [ ] **Step 1: Extend fake-provider contract tests RED**

Use synthetic payloads only:

```python
def test_projection_record_exposes_safe_identity_fields():
    record = parse_record({
        "id": 77,
        "create_date": "2026-01-02T09:00:00+03:00",
        "client": {"id": 55, "phone": "+79990000000", "name": "Test"},
    })
    assert record.client_id == "55"
    assert record.record_created_at.isoformat() == "2026-01-02T06:00:00+00:00"
    assert not hasattr(record, "phone")

async def test_full_history_is_bounded_and_uses_client_id(http):
    snapshot = await reader.read_history("55", now=NOW)
    assert http.query_params["client_id"] == "55"
    assert http.request_count <= MAX_HISTORY_PAGES
    assert snapshot.last_completed_visit_at == COMPLETED_AT
```

Test empty client, nullable/malformed `create_date`, conflicting client IDs for one Telegram customer, one client claimed by two customers, pagination, deleted/cancelled records, future active booking, partial/error watermark and no raw phone in objects/logs. Missing `create_date` becomes `None` and simply cannot prove the 14-day booking outcome; malformed non-null values fail the provider page safely.

- [ ] **Step 2: Run activity tests RED in Docker**

```powershell
docker compose --env-file ../.env run --rm test pytest -q tests/contract/booking/test_yclients_records.py tests/unit/booking/test_projection_sync.py tests/unit/reactivation/test_activity_sync.py tests/integration/reactivation/test_activity_projection.py
```

Expected: missing `client_id`, `record_created_at` and activity module failures.

- [ ] **Step 3: Implement safe adapter extensions and sync**

Use these contracts and limits:

```python
MAX_HISTORY_PAGES = 20
ACTIVITY_SYNC_BATCH = 25
ACTIVITY_SYNC_INTERVAL = timedelta(minutes=10)
HISTORY_FRESHNESS = timedelta(hours=24)
RECENT_BOOKINGS_FRESHNESS = timedelta(minutes=15)
ACTIVITY_SOURCE_VERSION = "yclients-client-history-v1"

@dataclass(frozen=True, slots=True)
class ClientActivitySnapshot:
    yclients_client_id: str
    last_completed_visit_at: datetime | None
    next_active_booking_at: datetime | None
    history_synced_at: datetime
    source_version: str
    sync_status: Literal["current", "partial", "error"]
    error_code: str | None = None
```

Current projection may verify `(telegram,user_id) -> client_id` only when the YCLIENTS record contains a valid `moroz_booking_key` joined to the same local `bookings` owner. For older bot bookings outside the projection window, fetch the latest local `external_id` with the single-record endpoint and apply the same ownership rule. Once identity is verified, activity/future-booking aggregation uses all YCLIENTS records carrying that stable `client_id`, including records created outside the bot. Never infer identity from phone/name.

Identity update locks the candidate and any row carrying the same external ID. If two Telegram users resolve to one YCLIENTS client, or one verified Telegram user later resolves to a different client ID, mark every affected row `conflict` in the same transaction and exclude all of them; do not keep the first link silently active.

`ActivitySyncCoordinator.run` first upserts missing unverified projection rows from known marketing-consent users, then claims at most `25` unverified rows or verified histories approaching the 24-hour cutoff. It resolves identity when provable, then reads at most `20 × 100` records per verified client. Any truncation yields `partial`, excludes the client and stores only an allowlisted `history_page_limit` code. Network/auth/provider errors store allowlisted codes and never advance the successful history watermark. The existing 10-minute recent projection owns `recent_bookings_synced_at`; it must stay within the separate 15-minute gate. Each writer updates only its owned columns so Telegram inbound timestamps cannot be overwritten by YCLIENTS sync.

- [ ] **Step 4: Run contract/unit/integration tests GREEN**

```powershell
docker compose --env-file ../.env run --rm test pytest -q tests/contract/booking/test_yclients_records.py tests/unit/booking/test_projection_sync.py tests/unit/reactivation/test_activity_sync.py tests/integration/reactivation/test_activity_projection.py
```

Expected: all pass; fake request inspection proves `client_id` filter and no phone persistence.

- [ ] **Step 5: Commit activity projection**

```powershell
git add project/src/moroz/booking/yclients_records.py project/src/moroz/booking/projection.py project/src/moroz/reactivation/activity.py project/tests/contract/booking/test_yclients_records.py project/tests/unit/booking/test_projection_sync.py project/tests/unit/reactivation/test_activity_sync.py project/tests/integration/reactivation/test_activity_projection.py changelog.md
git commit -m "feat: связать реактивацию с активностью YCLIENTS"
```

---

### Task 5: Versioned program, deterministic preview and activation gates

**Files:**
- Create: `project/src/moroz/reactivation/repository.py`
- Modify: `project/admin/reactivation_database.py`
- Modify: `project/docker-compose.yml`
- Modify: `project/docker-compose.prod.yml`
- Modify: `project/tests/unit/admin/test_reactivation_database_module.py`
- Modify: `project/tests/unit/test_migration_profile.py`
- Modify: `project/tests/integration/admin/test_reactivation_database.py`
- Create: `project/tests/integration/reactivation/test_preview.py`

**Interfaces:**
- Consumes: policy functions from Task 2; schema from Task 1; existing admin audit writer and `BUSINESS_ALERT_CHAT_ID` configuration.
- Produces: `create_draft`, `preview_version`, `queue_test_send`, `record_test_sent`, `approve_legal`, `activate_version`, `set_mode`, `get_dashboard`.

- [ ] **Step 1: Write repository/admin gate tests RED**

Assert one exclusion reason per recipient and every activation gate:

```python
async def test_preview_counts_each_recipient_once(repository):
    preview = await repository.preview_version(VERSION_ID, actor_id=OWNER_ID, now=NOW)
    assert preview.total == 12
    assert sum(preview.excluded_by_reason.values()) + preview.eligible == 12
    assert preview.excluded_by_reason["no_proven_consent"] == 2
    assert preview.excluded_by_reason["future_booking"] == 1

@pytest.mark.parametrize(
    "missing_gate",
    ["fresh_preview", "same_checksum", "current_watermarks", "test_sent", "legal_approved"],
)
async def test_activation_fails_closed(repository, missing_gate):
    with pytest.raises(ActivationBlocked) as error:
        await repository.activate_version(VERSION_ID, OWNER_ID, NOW)
    assert error.value.code == missing_gate
```

Also assert version immutability after activation, only one active version, no activation by admin role, no test gate when alert chat is blank, `BUSINESS_ALERT_CHAT_ID` is passed to admin in base and production Compose, no journey/outbox in dry-run preview, audit before/after without message text, masked live samples, preview expiry at exactly `30:00`, and preview invalidation after consent/inbound/booking/journey mutation.

- [ ] **Step 2: Run preview/gate tests RED in Docker**

```powershell
docker compose --env-file ../.env run --rm test pytest -q tests/unit/admin/test_reactivation_database_module.py tests/unit/test_migration_profile.py tests/integration/admin/test_reactivation_database.py tests/integration/reactivation/test_preview.py
```

Expected: V2 repository and activation gates are missing.

- [ ] **Step 3: Implement the minimal repository contract**

Use one deterministic SQL population beginning at `marketing_consents LEFT JOIN customer_activity_projection`; legacy/unproven users must remain visible as excluded. Apply `REASON_PRIORITY` from Task 2 and return:

```python
class ActivationBlocked(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)

@dataclass(frozen=True, slots=True)
class PreviewResult:
    version_id: UUID
    created_at: datetime
    template_checksum: str
    total: int
    eligible: int
    planned_main: int
    planned_reminder: int
    excluded_by_reason: dict[str, int]
    population_watermark: datetime | None
    history_watermark: datetime | None
    recent_watermark: datetime | None
    masked_samples: tuple[str, ...]
```

Persist only aggregate counts/checksum/watermarks on `reactivation_program_versions`; `population_watermark` is the maximum relevant consent/activity/journey mutation timestamp. Build `preview_checksum` with stdlib HMAC-SHA256 keyed by existing `ADMIN_SESSION_SECRET` over canonical ordered rows containing only opaque consent UUID, decision/reason, activity anchor, booking/freshness state and the template checksum—never raw Telegram/YCLIENTS IDs. Recompute the HMAC during activation, so consent, inbound, booking, journey or template changes invalidate the preview even when provider sync times are unchanged. `planned_main` equals eligible and `planned_reminder` equals eligible only when reminder is enabled, but both are forecasts and never labeled sent. Derive masked samples for the response and discard them. `queue_test_send` reads the existing `BUSINESS_ALERT_CHAT_ID` from the admin container environment, enqueues the main text to it with idempotency `reactivation-test:{version_id}:{checksum}` and stores `test_outbound_id`; only the delivery callback may set `test_sent_at`. Add the same optional environment mapping to the admin service in both Compose files; do not introduce another recipient setting.

`activate_version` must lock settings and version rows, re-read all five gates, retire any previous active version, activate the requested one and increment `program_revision`. `set_mode("active")` is rejected unless the active version still satisfies the same gates. Legal approval stores only owner ID, timestamp, status and reference; it does not claim that software made the legal decision.

- [ ] **Step 4: Run repository/admin tests GREEN**

```powershell
docker compose --env-file ../.env run --rm test pytest -q tests/unit/admin/test_reactivation_database_module.py tests/unit/test_migration_profile.py tests/integration/admin/test_reactivation_database.py tests/integration/reactivation/test_preview.py tests/integration/messaging/test_outbox.py
```

Expected: new tests and existing outbox/admin tests pass.

- [ ] **Step 5: Commit versioning and preview**

```powershell
git add project/src/moroz/reactivation/repository.py project/admin/reactivation_database.py project/docker-compose.yml project/docker-compose.prod.yml project/tests/unit/admin/test_reactivation_database_module.py project/tests/unit/test_migration_profile.py project/tests/integration/admin/test_reactivation_database.py project/tests/integration/reactivation/test_preview.py changelog.md
git commit -m "feat: добавить preview и активацию реактивации"
```

---

### Task 6: Canonical «Маркетинговые коммуникации» admin screen

**Files:**
- Modify: `project/admin/reactivation_routes.py`
- Modify: `project/admin/templates/reactivation.html`
- Modify: `project/admin/templates/base.html`
- Modify: `project/admin/app.py`
- Modify: `project/admin/static/styles.css`
- Modify: `project/tests/unit/admin/test_reactivation_routes.py`
- Modify: `project/tests/e2e/admin/test_csrf_rbac_audit.py`
- Create: `project/tests/e2e/admin/test_marketing_reactivation.py`

**Interfaces:**
- Consumes: admin functions from Task 5, existing CSRF/RBAC/audit patterns.
- Produces: `GET /marketing/`, version/preview/test/activate/legal/mode POST actions, safe consent revoke action and `/reactivation/` redirect.

- [ ] **Step 1: Write route/RBAC/render tests RED**

Lock the exact route map and owner-only behavior:

```python
EXPECTED_POSTS = {
    "/marketing/versions",
    "/marketing/versions/{version_id}/preview",
    "/marketing/versions/{version_id}/test",
    "/marketing/versions/{version_id}/activate",
    "/marketing/legal",
    "/marketing/mode",
    "/marketing/consents/{consent_id}/revoke",
}

def test_legacy_route_preserves_query(client):
    response = client.get("/reactivation/?status=active", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/marketing/?status=active"
```

Assert owner gets `200`, admin gets `403`, anonymous redirects to login, all POSTs require CSRF, navigation label is exactly «Маркетинговые коммуникации», no «Рассылки» tab/campaign builder/discount/LLM fields exist, and screen includes status, gates, rules/texts, preview exclusions, journeys, outcomes, consent history/revoke and a collapsed read-only legacy archive.

- [ ] **Step 2: Run admin tests RED in Docker**

```powershell
docker compose --env-file ../.env run --rm test pytest -q tests/unit/admin/test_reactivation_routes.py tests/e2e/admin/test_csrf_rbac_audit.py tests/e2e/admin/test_marketing_reactivation.py
```

Expected: `/marketing/` is absent and old UI assertions fail.

- [ ] **Step 3: Implement the canonical route and one focused page**

Use two routers without introducing a tab framework:

```python
router = APIRouter(prefix="/marketing", tags=["marketing"])
legacy_router = APIRouter(prefix="/reactivation", tags=["marketing-legacy"])

@legacy_router.get("/")
async def legacy_reactivation(request: Request) -> RedirectResponse:
    suffix = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(f"/marketing/{suffix}", status_code=307)
```

The page shows:

```text
1. Header: Реактивация клиентов + dry_run/active/paused badge + emergency stop.
2. Readiness gates: YCLIENTS freshness, proven consents, preview freshness,
   test delivery, legal approval.
3. Draft editor: inactivity 60/90/120, reminder off/3/5/7,
   cooldown >= inactivity, main/reminder text.
4. Preview: eligible, excluded by reason, masked examples, timestamps/watermarks.
   Show planned main/reminder as forecast and a field-by-field diff from active version.
5. Activation controls: test send, legal reference, owner confirmation.
6. Results: journeys and outcome funnel.
7. Consent lookup/history with revoke only; no admin grant.
8. Collapsed legacy archive with counts/status and the label
   «Черновая версия, реальные сообщения не отправлялись»; no actions.
```

Reuse current form components, status badges, pagination and admin audit. Consent revoke accepts only the opaque `marketing_consents.id`, loads channel/user server-side and performs the same customer-locked revoke+suppress transaction; raw Telegram ID never enters the URL/access log. Activation requires a fresh CSRF-protected confirmation value exactly equal to `АКТИВИРОВАТЬ`; a boolean toggle is insufficient.

- [ ] **Step 4: Run admin regressions GREEN**

```powershell
docker compose --env-file ../.env run --rm test pytest -q tests/unit/admin/test_reactivation_routes.py tests/e2e/admin/test_csrf_rbac_audit.py tests/e2e/admin/test_marketing_reactivation.py tests/unit/admin
```

Expected: all pass; the old canonical path is covered only by redirect tests.

- [ ] **Step 5: Commit admin screen**

```powershell
git add project/admin/reactivation_routes.py project/admin/templates/reactivation.html project/admin/templates/base.html project/admin/app.py project/admin/static/styles.css project/tests/unit/admin/test_reactivation_routes.py project/tests/e2e/admin/test_csrf_rbac_audit.py project/tests/e2e/admin/test_marketing_reactivation.py changelog.md
git commit -m "feat: обновить раздел маркетинговых коммуникаций"
```

---

### Task 7: Journey planner and scheduler integration

**Files:**
- Create: `project/src/moroz/reactivation/service.py`
- Modify: `project/src/moroz/reactivation/repository.py`
- Modify: `project/worker/main.py`
- Modify: `project/tests/unit/test_worker.py`
- Create: `project/tests/unit/reactivation/test_service.py`
- Create: `project/tests/integration/reactivation/test_journey_planner.py`

**Interfaces:**
- Consumes: existing `scheduler_jobs`, Task 2 policy, Task 4 activity sync, Task 5 active version/repository, existing `enqueue_outbound_in_transaction`.
- Produces: scheduler kinds `reactivation_activity_sync` and `reactivation_tick`; `ReactivationCoordinator.ensure_current`, `run_activity_sync`, `run_tick`.

- [ ] **Step 1: Write planner lifecycle tests RED**

Cover the complete deterministic progression:

```python
async def test_tick_creates_one_main_step_and_outbox(repository, coordinator):
    await coordinator.run_tick(reactivation_tick_job(NOW))
    journey = await load_journey(USER_ID)
    assert journey.status == "scheduled"
    assert await step_kinds(journey.id) == ["main"]
    assert await outbox_keys() == [f"reactivation:{journey.id}:main"]

async def test_main_sent_schedules_one_reminder_from_actual_sent_time(repository):
    await repository.record_delivery_sent(OUTBOUND_ID, SENT_AT)
    reminder = await load_step(JOURNEY_ID, "reminder")
    assert reminder.due_at == next_send_at(SENT_AT + timedelta(days=5))
```

Also assert: dry-run creates no journey/outbox; inactive/legal-gate failure creates none; max `100` new journeys per tick; `FOR UPDATE SKIP LOCKED` prevents double claim; concurrent ticks create one journey; restart/replay is idempotent; no reminder when disabled; no reminder after reply/booking/STOP/unknown/failed; stale activity fails closed; next scheduler job is always created.

- [ ] **Step 2: Run planner tests RED in Docker**

```powershell
docker compose --env-file ../.env run --rm test pytest -q tests/unit/reactivation/test_service.py tests/integration/reactivation/test_journey_planner.py tests/unit/test_worker.py
```

Expected: coordinator and scheduler kinds are absent.

- [ ] **Step 3: Implement bounded jobs on the existing worker**

Use fixed operational constants, not admin-configurable rate machinery:

```python
REACTIVATION_ACTIVITY_SYNC_KIND = "reactivation_activity_sync"
REACTIVATION_TICK_KIND = "reactivation_tick"
REACTIVATION_TICK_INTERVAL = timedelta(minutes=5)
PLANNER_LIMIT = 100
STEP_CLAIM_LIMIT = 100
```

The exact public methods in this task are `ReactivationCoordinator.ensure_current(now: datetime) -> None`, `run_activity_sync(job: SchedulerJob) -> JobResult` and `run_tick(job: SchedulerJob) -> JobResult`.

`ensure_current` idempotently seeds both jobs. `run_activity_sync` delegates to Task 4 and schedules the next `+10m` job. `run_tick` in one bounded cycle: schedules next `+5m`; refreshes reply/booking/completed outcomes from projections; closes/cancels journeys; returns early unless mode is active and all global gates pass; inserts at most `100` eligible journeys; claims at most `100` due steps with `FOR UPDATE SKIP LOCKED`; repeats recipient eligibility inside the claim transaction; enqueues outbox and marks step `reserved`. Provider acceptance of main moves the journey to `active` and schedules at most one reminder from actual `sent_at`; when reminder is disabled it closes immediately as operationally `exhausted` while outcome windows remain open. Acceptance of reminder also closes it as `exhausted`.

Register both kinds in the existing `MessageTaskHandler` system-scheduler branch. Seed them only when valid YCLIENTS configuration built the activity reader; otherwise keep the program fail-closed in dry-run and show `YCLIENTS unavailable` in the admin gate. Do not add a process, queue, dependency or timer library.

- [ ] **Step 4: Run planner/worker tests GREEN**

```powershell
docker compose --env-file ../.env run --rm test pytest -q tests/unit/reactivation/test_service.py tests/integration/reactivation/test_journey_planner.py tests/unit/test_worker.py tests/unit/test_scheduler.py
```

Expected: all pass, including concurrent/replay assertions.

- [ ] **Step 5: Commit planner and scheduling**

```powershell
git add project/src/moroz/reactivation/service.py project/src/moroz/reactivation/repository.py project/worker/main.py project/tests/unit/reactivation/test_service.py project/tests/integration/reactivation/test_journey_planner.py project/tests/unit/test_worker.py changelog.md
git commit -m "feat: добавить планировщик journey реактивации"
```

---

### Task 8: Pre-send fence, Telegram outcomes and emergency stop

**Files:**
- Modify: `project/src/moroz/messaging/repository.py`
- Modify: `project/src/moroz/messaging/telegram.py`
- Modify: `project/src/moroz/reactivation/repository.py`
- Modify: `project/src/moroz/reactivation/service.py`
- Modify: `project/tests/integration/messaging/test_outbox.py`
- Create: `project/tests/unit/reactivation/test_delivery.py`
- Create: `project/tests/integration/reactivation/test_delivery_fence.py`

**Interfaces:**
- Consumes: existing `claim_outbound_delivery`, `fence_claimed_outbound`, `mark_outbound_sent`, `mark_outbound_delivery_unknown`, customer advisory key, Task 7 linked step/outbound.
- Produces: generic optional pre-send guard, shared/exclusive program advisory lock, terminal `mark_outbound_failed`, atomic journey-step result updates and auto-pause.

- [ ] **Step 1: Write delivery race/error matrix RED**

Use a blocking fake Telegram call to prove lock behavior:

```python
async def test_stop_waits_for_inflight_send_and_blocks_next_send(harness):
    send = asyncio.create_task(harness.send_blocked(OUTBOUND_1))
    await harness.telegram_started.wait()
    stop = asyncio.create_task(harness.stop_program(OWNER_ID))
    assert not stop.done()
    harness.release_telegram.set()
    await send
    await stop
    assert await harness.send(OUTBOUND_2) == "cancelled_by_guard"

@pytest.mark.parametrize(
    ("error", "outbound", "step", "program", "suppressed"),
    [
        (TelegramForbiddenError, "failed", "failed", "active", True),
        (TelegramNotFound, "failed", "failed", "active", True),
        (TelegramBadRequest, "failed", "failed", "paused", False),
        (TelegramRetryAfter, "pending", "reserved", "active", False),
        (TelegramNetworkError, "delivery_unknown", "delivery_unknown", "paused", False),
        (TimeoutError, "delivery_unknown", "delivery_unknown", "paused", False),
    ],
)
async def test_delivery_classification(
    error, outbound, step, program, suppressed, delivery_harness
):
    result = await delivery_harness.raise_from_telegram(error)
    assert result.outbound_status == outbound
    assert result.step_status == step
    assert result.program_mode == program
    assert result.recipient_suppressed is suppressed
```

Also assert pre-send recheck catches consent revoke, suppression, inbound, future booking, stale activity, deletion, human mode and escalation; unknown is never requeued; test-send sets `test_sent_at` only after success; alert payload contains only allowlisted codes/counts.

- [ ] **Step 2: Run delivery tests RED in Docker**

```powershell
docker compose --env-file ../.env run --rm test pytest -q tests/unit/reactivation/test_delivery.py tests/integration/reactivation/test_delivery_fence.py tests/integration/messaging/test_outbox.py
```

Expected: current sender releases generic Telegram errors and has no program fence/result hook.

- [ ] **Step 3: Extend the existing fence without domain leakage**

Add optional callbacks to the sender/repository seam rather than importing reactivation into generic messaging:

```python
PreSendGuard = Callable[[asyncpg.Connection, OutboundMessage], Awaitable[bool]]
DeliveryHook = Callable[
    [asyncpg.Connection, OutboundMessage, Literal["sent", "failed", "delivery_unknown"], str | None, datetime],
    Awaitable[None],
]
```

Extend the existing method without changing its first argument: `MessageRepository.fence_claimed_outbound(outbound: OutboundMessage, *, pre_send_guard: PreSendGuard | None = None) -> AsyncIterator[OutboundMessage | None]`.

For linked client reactivation outbound, the guard takes the shared program advisory lock, then the existing per-customer transaction advisory lock, reloads program/version/consent/activity/journey/step/human/escalation/deletion state and returns false on any failure. Before returning false it atomically marks the outbound `cancelled`, the linked step `cancelled` with the stable eligibility reason and closes the journey when no later step is possible; the already-published queue task then finishes as skipped. Test-send outbound is linked to a program version but follows a separate guard: configured owner chat, unchanged version checksum and non-retired version; it never requires owner marketing consent. The surrounding transaction/connection stays open across the Telegram call exactly like the current privacy fence.

Emergency stop takes the exclusive program advisory lock, sets mode `paused`, increments revision, records `stopped_at`, marks pending reactivation outbound `cancelled`, cancels all scheduled/reserved steps whose outbound has not started, and audits the owner action. It never cancels an already completed network call.

Classify aiogram 3.27 errors explicitly. For a linked client-reactivation terminal error, mark outbound and linked step failed and close the journey in one transaction; Forbidden/NotFound additionally suppress the recipient. BadRequest pauses globally. Network/timeout/cancellation ambiguity becomes `delivery_unknown`, closes the affected journey without reminder, is never retried and pauses globally. A test-send error updates only the version test state and never suppresses the configured owner; a failed test cannot satisfy activation. Non-reactivation outbound retains its current delivery semantics. `TelegramRetryAfter` releases to pending and re-raises so the existing Rabbit retry/DLQ contour handles it; no second retry system is added.

After the existing startup `reconcile_stale_outbound_deliveries`, call `ReactivationCoordinator.reconcile_delivery_unknowns()`: it finds linked outbounds already moved to `delivery_unknown`, idempotently updates their steps/journeys and pauses the program before new scheduler work. This closes the crash window without adding another outbox reconciler.

- [ ] **Step 4: Run fence/outbox regressions GREEN**

```powershell
docker compose --env-file ../.env run --rm test pytest -q tests/unit/reactivation/test_delivery.py tests/integration/reactivation/test_delivery_fence.py tests/integration/messaging/test_outbox.py tests/unit/messaging
```

Expected: full matrix passes and existing admin replies/notifications retain current delivery behavior.

- [ ] **Step 5: Commit delivery safety**

```powershell
git add project/src/moroz/messaging/repository.py project/src/moroz/messaging/telegram.py project/src/moroz/reactivation/repository.py project/src/moroz/reactivation/service.py project/tests/unit/reactivation/test_delivery.py project/tests/integration/reactivation/test_delivery_fence.py project/tests/integration/messaging/test_outbox.py changelog.md
git commit -m "feat: защитить доставку и остановку реактивации"
```

---

### Task 9: Inbound cancellation and deterministic client buttons

**Files:**
- Modify: `project/llm/webhook.py`
- Modify: `project/src/moroz/reactivation/repository.py`
- Modify: `project/tests/e2e/test_privacy_gate.py`
- Create: `project/tests/e2e/reactivation/test_client_flow.py`

**Interfaces:**
- Consumes: Task 3 STOP/marketing callbacks; Task 7 journeys; existing booking/LLM flow.
- Produces: `record_inbound(channel,user_id,occurred_at,kind)`, callbacks `reactivation:book`, `reactivation:ask`, `marketing:disable` and immediate reminder cancellation.

- [ ] **Step 1: Write real-update E2E tests RED**

Drive Telegram update dictionaries through the actual webhook:

```python
async def test_any_inbound_cancels_reminder_before_llm(e2e):
    await e2e.seed_sent_main_with_due_reminder()
    await e2e.post_text("Подскажите, пожалуйста")
    assert await e2e.step_status("reminder") == "cancelled"
    assert await e2e.journey_field("replied_at") == e2e.received_at
    assert e2e.llm.calls == 1

async def test_book_button_uses_existing_booking_flow(e2e):
    await e2e.post_callback("reactivation:book")
    assert e2e.telegram.answered_callback_ids == [e2e.callback_id]
    assert e2e.telegram.last_text == "Напишите, пожалуйста, какую процедуру хотите и на какой день — помогу подобрать время."
    assert await e2e.step_status("reminder") == "cancelled"
```

Also test ask button, disable button, STOP text, duplicate callback, callback after closed journey, inbound while bot paused, non-text inbound, and that no callback injects a fake user message into LLM.

- [ ] **Step 2: Run client-flow tests RED in Docker**

```powershell
docker compose --env-file ../.env run --rm test pytest -q tests/e2e/test_privacy_gate.py tests/e2e/reactivation/test_client_flow.py
```

Expected: fixed callbacks and durable inbound hook are absent.

- [ ] **Step 3: Implement the smallest deterministic callback flow**

The sent messages use fixed inline keyboards:

```python
MAIN_BUTTONS = (
    ("Записаться", "reactivation:book"),
    ("Задать вопрос", "reactivation:ask"),
    ("Не писать", "marketing:disable"),
)
REMINDER_BUTTONS = MAIN_BUTTONS
```

Immediately after deletion handling, call `record_inbound` for every deduplicated accepted message/callback, including when the bot is paused or the content is non-text. The repository upserts an unverified activity-projection row when absent, advances `last_meaningful_inbound_at` monotonically, sets `replied_at` only inside the 7-day attribution window, cancels scheduled/reserved reminder and closes the journey with `responded` when appropriate.

Callbacks answer Telegram first, then persist the transition, then send a static prompt. `reactivation:book` and `reactivation:ask` do not call LLM themselves; the next real user message continues through the existing router/booking flow. `marketing:disable` reuses Task 3 revoke+suppress transaction.

- [ ] **Step 4: Run webhook/E2E regressions GREEN**

```powershell
docker compose --env-file ../.env run --rm test pytest -q tests/e2e/test_privacy_gate.py tests/e2e/reactivation/test_client_flow.py tests/e2e/booking
```

Expected: all pass; every callback ID is answered once.

- [ ] **Step 5: Commit client flow**

```powershell
git add project/llm/webhook.py project/src/moroz/reactivation/repository.py project/tests/e2e/test_privacy_gate.py project/tests/e2e/reactivation/test_client_flow.py changelog.md
git commit -m "feat: завершать реактивацию после ответа клиента"
```

---

### Task 10: Outcome attribution and operational dashboard

**Files:**
- Modify: `project/src/moroz/reactivation/repository.py`
- Modify: `project/src/moroz/reactivation/service.py`
- Modify: `project/admin/reactivation_database.py`
- Modify: `project/admin/templates/reactivation.html`
- Create: `project/tests/integration/reactivation/test_outcomes.py`
- Modify: `project/tests/e2e/admin/test_marketing_reactivation.py`

**Interfaces:**
- Consumes: linked verified client identity, YCLIENTS `record_created_at`, completed visit time, inbound timestamps and journey first sent time.
- Produces: `refresh_outcomes`, `get_outcome_funnel`, paginated journey list with terminal reasons and honest attribution windows.

- [ ] **Step 1: Write attribution-window tests RED**

Fix the exact boundaries and exclusions:

```python
@pytest.mark.parametrize(
    ("event", "offset", "field", "counted"),
    [
        ("reply", timedelta(days=7), "replied_at", True),
        ("reply", timedelta(days=7, seconds=1), "replied_at", False),
        ("booking", timedelta(days=14), "booked_at", True),
        ("booking", timedelta(days=14, seconds=1), "booked_at", False),
        ("completed", timedelta(days=30), "completed_visit_at", True),
        ("completed", timedelta(days=30, seconds=1), "completed_visit_at", False),
    ],
)
async def test_outcome_windows(
    event, offset, field, counted, outcome_harness
):
    result = await outcome_harness.project(event=event, occurred_at=SENT_AT + offset)
    assert result.has_timestamp(field) is counted
```

Assert a booking counts only when its `record_created_at >= first_sent_at`; a completed visit counts only after first send; the same event is idempotent; dry-run/test/legacy rows never enter metrics; funnel denominators show `main_sent`, not planned recipients; opt-out, failed and unknown are separate.

- [ ] **Step 2: Run outcome tests RED in Docker**

```powershell
docker compose --env-file ../.env run --rm test pytest -q tests/integration/reactivation/test_outcomes.py tests/e2e/admin/test_marketing_reactivation.py
```

Expected: outcome refresh/funnel fields are absent.

- [ ] **Step 3: Implement derived metrics without a stats table**

Use the journey columns as immutable first-hit timestamps and derive aggregates in SQL:

```python
@dataclass(frozen=True, slots=True)
class OutcomeFunnel:
    journey_started: int
    main_sent: int
    reminder_sent: int
    replied_7d: int
    booked_14d: int
    completed_30d: int
    opted_out: int
    suppressed: int
    escalated: int
    failed: int
    delivery_unknown: int
```

`refresh_outcomes` processes only open/recent journeys in a bounded batch, joins by verified `yclients_client_id`, and writes each timestamp with `COALESCE(existing, candidate)`. It may close a journey after booking/completed/opt-out/escalation but must retain all already-attributed fields. The admin page shows latest-preview eligible separately, absolute funnel counts, percentages over `main_sent`, and labels step `sent` as «принято Telegram», never «прочитано». Add bounded filters: period `7/30/90` days, outcome `all/replied/booked/completed/opted_out/escalated`, delivery `all/failed/delivery_unknown`; reject any other values. Delivery-unknown is a red operational state; do not imply causal uplift or add revenue math without a control group.

- [ ] **Step 4: Run outcomes/admin tests GREEN**

```powershell
docker compose --env-file ../.env run --rm test pytest -q tests/integration/reactivation/test_outcomes.py tests/e2e/admin/test_marketing_reactivation.py tests/integration/admin/test_reactivation_database.py
```

Expected: exact boundary cases and rendered funnel pass.

- [ ] **Step 5: Commit outcomes**

```powershell
git add project/src/moroz/reactivation/repository.py project/src/moroz/reactivation/service.py project/admin/reactivation_database.py project/admin/templates/reactivation.html project/tests/integration/reactivation/test_outcomes.py project/tests/e2e/admin/test_marketing_reactivation.py changelog.md
git commit -m "feat: добавить метрики результата реактивации"
```

---

### Task 11: Deletion, retention and privacy guarantees

**Files:**
- Modify: `project/admin/customer_data_deletion.py`
- Modify: `project/src/moroz/retention.py`
- Modify: `project/tests/unit/admin/test_customer_data_deletion.py`
- Modify: `project/tests/integration/admin/test_customer_data_deletion_postgres.py`
- Modify: `project/tests/unit/test_retention.py`
- Modify: `project/tests/integration/test_retention_postgres.py`
- Create: `project/tests/unit/reactivation/test_privacy.py`

**Interfaces:**
- Consumes: existing customer advisory deletion fence and `DATA_RETENTION_DAYS`.
- Produces: atomic removal of recipient-linked reactivation/outbox data and bounded retention of closed records without exposing PII.

- [ ] **Step 1: Write deletion/retention/privacy tests RED**

Seed every new table plus pending/sent outbound and assert exact cleanup:

```python
async def test_customer_deletion_removes_reactivation_graph(database):
    await seed_customer_with_reactivation(database)
    result = await delete_customer_data("telegram", USER_ID)
    assert result.remaining_rows == 0
    assert await count("customer_activity_projection", USER_ID) == 0
    assert await count("marketing_consent_events", USER_ID) == 0
    assert await count("marketing_consents", USER_ID) == 0
    assert await count("reactivation_journeys", USER_ID) == 0
    assert await count_linked_outbound(USER_ID) == 0
```

Test deletion racing a blocked Telegram send, retained unrelated users, closed journey retention cutoff, revoked consent-event cutoff, active consent preservation under automatic retention, and caplog/alert snapshots without phone/user ID/message/proof text/provider error.

- [ ] **Step 2: Run deletion/privacy tests RED in Docker**

```powershell
docker compose --env-file ../.env run --rm test pytest -q tests/unit/admin/test_customer_data_deletion.py tests/integration/admin/test_customer_data_deletion_postgres.py tests/unit/test_retention.py tests/integration/test_retention_postgres.py tests/unit/reactivation/test_privacy.py
```

Expected: new rows survive current deletion/retention logic.

- [ ] **Step 3: Extend the existing atomic deletion and retention query**

Inside the existing per-customer advisory transaction: collect linked journey outbound IDs first; delete corresponding `task_outbox` rows; delete pending/terminal `outbound_messages`; delete journeys (steps cascade), activity projection, consent events and materialized consent; run the existing zero-row verification before commit.

Automatic retention preserves the existing contract: when `DATA_RETENTION_DAYS <= 0` it changes nothing; otherwise it uses the configured cutoff and bounded batches:

```text
delete closed reactivation_journeys when closed_at < cutoff;
delete customer_activity_projection when updated_at < cutoff and no active consent/journey;
delete marketing_consent_events when created_at < cutoff and current consent is inactive;
preserve active consent proof until revoke, customer deletion or a separately approved legal retention change;
preserve admin_audit_events under the existing audit retention contract.
```

Store only aggregate deleted-row counts in logs. Do not log channel/user ID or database exception text.

- [ ] **Step 4: Run deletion/retention/privacy tests GREEN**

```powershell
docker compose --env-file ../.env run --rm test pytest -q tests/unit/admin/test_customer_data_deletion.py tests/integration/admin/test_customer_data_deletion_postgres.py tests/unit/test_retention.py tests/integration/test_retention_postgres.py tests/unit/reactivation/test_privacy.py
```

Expected: all pass, including send/delete race and unrelated-user preservation.

- [ ] **Step 5: Commit privacy lifecycle**

```powershell
git add project/admin/customer_data_deletion.py project/src/moroz/retention.py project/tests/unit/admin/test_customer_data_deletion.py project/tests/integration/admin/test_customer_data_deletion_postgres.py project/tests/unit/test_retention.py project/tests/integration/test_retention_postgres.py project/tests/unit/reactivation/test_privacy.py changelog.md
git commit -m "feat: закрыть privacy lifecycle реактивации"
```

---

### Task 12: Full acceptance, documentation and rollout readiness

**Files:**
- Create: `project/tests/e2e/reactivation/test_reactivation_v2.py`
- Modify: `ТЗ и архитектура.md`
- Modify: `docs/architecture/moroz-i-solntse-full-architecture.html`
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`
- Modify: `docs/superpowers/plans/2026-08-31-reactivation-v2.md`

**Interfaces:**
- Consumes: all Tasks 1–11.
- Produces: release evidence for a local dry-run candidate; real-provider/staging activation remains a separately authorized gate.

- [x] **Step 1: Add the final real-pipeline E2E matrix**

Use local fakes for Telegram/YCLIENTS but real FastAPI handlers, worker handler, Rabbit task shape and Postgres:

```python
E2E_CASES = (
    "consent_grant_and_revoke",
    "eligibility_89_90_91_days",
    "main_then_reminder",
    "reply_cancels_reminder",
    "booking_cancels_reminder",
    "stop_suppresses_before_llm",
    "future_booking_excluded",
    "human_mode_and_escalation_excluded",
    "customer_deletion_blocks_send",
    "duplicate_restart_and_stale_claim",
    "delivery_unknown_pauses_without_retry",
    "dry_run_has_no_outbound",
)

@pytest.mark.parametrize("case", E2E_CASES)
async def test_reactivation_v2_case(case, reactivation_harness):
    result = await reactivation_harness.run(case)
    assert result.passed, result.safe_failure_code
```

The harness must assert DB state, outbox/task counts, Telegram fake calls, callback acknowledgements, audit actions and absence of raw PII in captured logs.

- [x] **Step 2: Run the focused final gate**

```powershell
docker compose --env-file ../.env build test admin worker scheduler migrate
docker compose --env-file ../.env run --rm test pytest -q tests/unit/reactivation tests/integration/reactivation tests/e2e/reactivation tests/unit/admin/test_reactivation_routes.py tests/integration/admin/test_reactivation_database.py tests/integration/messaging/test_outbox.py
```

Expected: all focused tests pass with zero warnings attributable to Reactivation V2.

- [x] **Step 3: Update the owner documents with the implemented contract**

Document only verified behavior:

```text
ТЗ и архитектура.md:
  Marketing Communications -> Reactivation -> scheduler_jobs -> worker
  -> eligibility/pre-send fence -> outbound/task_outbox -> Telegram
  -> inbound/YCLIENTS outcomes.
Architecture HTML:
  same nodes, shared program lock and customer delivery fence.
Дорожная карта.md:
  mark implementation complete only after all local gates pass;
  keep real YCLIENTS, staging dry-run, owner activation and newsletters open.
changelog.md:
  append timestamped commands/results/counts; no credentials or client data.
```

Add an explicit rollout checklist: migration backup/compatibility; staging `dry_run`; real read-only YCLIENTS sync authorization; preview review; test message; legal reference; at least 14 days dry-run observation; owner activation; first batch observation; emergency-stop rehearsal. The first production activation under this plan is allowed only when the fresh preview has at most `25` eligible recipients; if it has more, remain in `dry_run` until a separate audited pilot-cap/allowlist decision is approved. Do not activate or deploy while writing docs.

- [x] **Step 4: Run complete Docker verification**

```powershell
docker compose --env-file ../.env config --quiet
docker compose --env-file ../.env run --rm test python -m compileall -q -f /workspace/src /workspace/admin /workspace/llm /workspace/worker /workspace/scheduler
docker compose --env-file ../.env run --rm test pytest -q
docker compose --env-file ../.env run --rm migrate alembic current
docker compose --env-file ../.env run --rm migrate alembic heads
git diff --check
git status --short
```

Expected: Compose/compile/diff checks exit `0`; full suite passes; current/heads both show only `0023_reactivation_v2`; status contains only the intended documentation/test changes before the final commit.

- [x] **Step 5: Perform final self-review and commit**

Review the complete diff for correctness, authorization boundaries, PII leakage, migration downgrade, idempotency and over-engineering. Fix every finding test-first, rerun its focused test, then rerun Step 4.

```powershell
git add project/tests/e2e/reactivation/test_reactivation_v2.py 'ТЗ и архитектура.md' docs/architecture/moroz-i-solntse-full-architecture.html 'Дорожная карта.md' changelog.md docs/superpowers/plans/2026-08-31-reactivation-v2.md
git commit -m "docs: закрыть реализацию реактивации v2"
```

Expected: local candidate is fully tested but remains `dry_run`; no push, deploy, real provider call or customer send occurred.

## Requirement-to-task trace

| Requirement | Tasks |
|---|---:|
| Separate proven marketing consent, revoke, suppression, STOP | 1, 3, 9 |
| Stable Telegram ↔ YCLIENTS identity and full activity | 1, 4 |
| Deterministic 90-day eligibility and exact exclusions | 2, 5, 7 |
| Versioned static templates and no runtime LLM/discount | 2, 5, 6 |
| Preview/test/legal/owner gates and default dry-run | 5, 6 |
| Main + maximum one reminder, cooldown, quiet hours | 2, 7, 9 |
| Durable idempotent outbox, pre-send recheck and emergency stop | 7, 8 |
| Telegram error classification and no retry after ambiguity | 8 |
| Reply/booking/completed outcomes | 9, 10 |
| Owner-only UI and legacy redirect | 6 |
| Customer deletion, retention and no PII leakage | 8, 11 |
| Docker-only acceptance and staged rollout gates | 12 |
| Newsletters remain a separate future task | Global Constraints, 6, 12 |

## Deliberate Ponytail cuts

- No `marketing_campaigns` abstraction, campaign tabs or empty newsletters screen; add them only when manual broadcasts receive their own approved spec.
- No phone matching/manual identity editor; add only if verified YCLIENTS `client_id` leaves a measured, commercially significant uncovered segment.
- No runtime LLM, personalization placeholders or discount engine; static owner-approved copy is safer and sufficient for the first measurable win-back loop.
- No preview, metrics or suppression side tables beyond the five approved tables; reuse program-version JSON, journeys, materialized consent, admin audit and existing outbox.
- No new worker, queue, cron service, rate-limit subsystem, feature-flag framework or dependency; fixed bounded jobs reuse the production reliability contour already present.
