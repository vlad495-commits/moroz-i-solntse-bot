# Telegram → YCLIENTS Booking Flow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Подключить существующий защищённый `BookingService` к Telegram через durable кнопочный workflow, доказать полный lifecycle на mock/read-only/sandbox-контурах и только затем включить scheduler/reminders.

**Architecture:** Webhook сохраняет text/callback/contact как нормализованные interactions в существующий inbox. Worker сначала проверяет `human_mode`, активный сценарий и opaque action, затем использует безопасный structured LLM-router только как advisory-классификатор; все booking-параметры и mutations обрабатывает отдельный `BookingWorkflow`. Критическое состояние, действия, эскалации, ответы и аудит живут в PostgreSQL; Redis остаётся необязательным кэшем.

**Tech Stack:** Python 3.12, aiogram 3.x, FastAPI, asyncpg/PostgreSQL 16, Redis 7, RabbitMQ, Alembic, pytest/pytest-asyncio, Docker Compose, YCLIENTS HTTP API.

## Global Constraints

- Все запуски проекта, миграций и тестов выполняются только через Docker Compose с `--env-file ../.env` из каталога `project/`.
- Реальные клиентские create/reschedule/cancel запрещены без отдельного явного разрешения; реальные ПД запрещены всегда.
- Sandbox mutations разрешены только отдельным gate после mock и read-only этапов, с fake data, bounded cleanup и reconciliation.
- LLM возвращает только route/confidence, не извлекает booking-параметры и не вызывает YCLIENTS.
- Blocking security verdict, активный сценарий, валидный callback/contact и явная команда имеют приоритет над LLM-router.
- Порог router confidence по умолчанию `0.80`; невалидный ответ, timeout и значение ниже порога дают меню уточнения без mutation.
- `BOOKING_MODE` принимает только `disabled`, `mock`, `real`; default `disabled`, а `real` требует полную YCLIENTS-конфигурацию и успешный read-only preflight до readiness.
- Самостоятельное изменение доступно только для bot-created booking того же Telegram user ID; чужие и несопоставленные записи не раскрываются.
- Каталог динамический, но наружу проходят только YCLIENTS IDs из allowlist; горизонт слотов `14` дней, TTL резюме `30` минут.
- Поздний перенос/отмена менее чем за `3` часа не выполняется автоматически.
- Многосервисная запись создаётся без прикладного лимита; перенос/отмена применяются целиком, частичное изменение эскалируется.
- Scheduler/reminders остаются выключенными до успешного core booking gate и отдельного подтверждения.
- После каждого логического шага обновляются `Дорожная карта.md`, `changelog.md` и создаётся локальный коммит; push не выполняется.

## File map

- `project/src/moroz/booking/catalog.py` — модели каталога и `BookingCatalogPort`.
- `project/src/moroz/booking/mock_catalog.py` — детерминированный mock-каталог.
- `project/src/moroz/booking/yclients_catalog.py` — только read-only YCLIENTS services/staff.
- `project/src/moroz/booking/reconciliation.py` — read-only разбор `outcome unknown` по booking key.
- `project/src/moroz/booking/interaction.py` — типы Telegram interaction, router verdict и workflow reply.
- `project/src/moroz/booking/intent_router.py` — строгий advisory LLM-router.
- `project/src/moroz/booking/workflow_repository.py` — durable scenario/action/ownership/human-mode queries.
- `project/src/moroz/booking/workflow.py` — Telegram state machine и вызов `BookingService` только после confirmation.
- `project/src/moroz/booking/presenter.py` — безопасные тексты и Telegram keyboards.
- `project/migrations/versions/0010_telegram_booking_flow.py` — revision, channel/chat identity, actions и escalation resolution fields.
- `project/llm/webhook.py` — нормализация booking callbacks и собственного contact в inbox.
- `project/worker/main.py` — dispatcher и dependency graph.
- `project/src/moroz/booking/repository.py` — атомарная booking escalation + outbox.
- `project/admin/escalation_routes.py`, `project/admin/templates/escalations.html` — durable reply/resolve UI.
- `project/docker-compose.yml`, `project/llm/config.py` — allowlists, horizon, TTL, router threshold и safe startup wiring.
- `project/tests/...` — unit, integration, E2E, contract и operational gates.
- `docs/testing/telegram-yclients-booking-test-plan.md` — отдельный итоговый тест-план и evidence index.

---

### Task 1: Catalog domain, allowlist config and mock adapter

**Files:**
- Create: `project/src/moroz/booking/catalog.py`
- Create: `project/src/moroz/booking/mock_catalog.py`
- Create: `project/tests/unit/booking/test_catalog.py`
- Modify: `project/llm/config.py`
- Modify: `project/docker-compose.yml`
- Modify: `project/tests/unit/common/test_config.py`

**Interfaces:**
- Produces: `CatalogService(id: str, title: str, duration_minutes: int)`, `CatalogStaff(id: str, name: str)`, `BookingCatalogPort.list_services()`, `BookingCatalogPort.list_staff(service_ids)`, `parse_id_allowlist(raw, name)`.
- Consumes: no new interfaces.

- [ ] **Step 1: Write failing domain/config tests**

```python
def test_allowlist_is_numeric_unique_and_non_empty():
    assert parse_id_allowlist("17, 29", "services") == ("17", "29")
    for invalid in ("", "17,17", "17,nope"):
        with pytest.raises(ValueError):
            parse_id_allowlist(invalid, "services")

@pytest.mark.asyncio
async def test_mock_catalog_filters_staff_for_all_selected_services():
    catalog = MockBookingCatalog(
        services=(CatalogService("1", "Крио", 30), CatalogService("2", "Массаж", 60)),
        staff=(CatalogStaff("7", "Анна", ("1", "2")), CatalogStaff("8", "Ирина", ("1",))),
    )
    assert await catalog.list_staff(("1", "2")) == [
        CatalogStaff("7", "Анна", ("1", "2"))
    ]
```

- [ ] **Step 2: Run RED test in Docker**

Run: `cd project && docker compose --env-file ../.env --profile test run --rm test pytest tests/unit/booking/test_catalog.py tests/unit/common/test_config.py -q`

Expected: FAIL because `moroz.booking.catalog` and allowlist settings do not exist.

- [ ] **Step 3: Add exact catalog contracts and parser**

```python
@dataclass(frozen=True, slots=True)
class CatalogService:
    id: str
    title: str
    duration_minutes: int

@dataclass(frozen=True, slots=True)
class CatalogStaff:
    id: str
    name: str
    service_ids: tuple[str, ...]

class BookingCatalogPort(Protocol):
    async def list_services(self) -> list[CatalogService]: ...
    async def list_staff(self, service_ids: tuple[str, ...]) -> list[CatalogStaff]: ...

def parse_id_allowlist(raw: str, name: str) -> tuple[str, ...]:
    values = tuple(part.strip() for part in raw.split(",") if part.strip())
    if not values or any(not value.isdigit() for value in values) or len(set(values)) != len(values):
        raise ValueError(f"{name} allowlist must contain unique numeric ids")
    return values
```

Add worker Compose variables `BOOKING_MODE=disabled`,
`YCLIENTS_SERVICE_ALLOWLIST`, `YCLIENTS_STAFF_ALLOWLIST`,
`BOOKING_HORIZON_DAYS=14`, `BOOKING_CONFIRMATION_TTL_SECONDS=1800`,
`BOOKING_ROUTER_CONFIDENCE=0.80`. The bot container needs only Telegram ingress
settings and does not receive YCLIENTS tokens or allowlists.

- [ ] **Step 4: Run GREEN tests and config render**

Run: `cd project && docker compose --env-file ../.env --profile test run --rm test pytest tests/unit/booking/test_catalog.py tests/unit/common/test_config.py -q`

Run: `cd project && docker compose --env-file ../.env config --quiet`

Expected: both commands exit `0`.

- [ ] **Step 5: Update roadmap/changelog and commit**

```bash
git add project/src/moroz/booking/catalog.py project/src/moroz/booking/mock_catalog.py project/tests/unit/booking/test_catalog.py project/tests/unit/common/test_config.py project/llm/config.py project/docker-compose.yml 'Дорожная карта.md' changelog.md
git commit -m "feat: добавлен allowlisted booking catalog contract"
```

### Task 2: Read-only YCLIENTS catalog adapter

**Files:**
- Create: `project/src/moroz/booking/yclients_catalog.py`
- Create: `project/tests/contract/booking/test_yclients_catalog.py`
- Modify: `project/src/moroz/booking/yclients_http.py`
- Modify: `project/src/moroz/booking/models.py`
- Modify: `project/src/moroz/booking/yclients.py`
- Modify: `project/src/moroz/booking/mock_yclients.py`
- Modify: `project/src/moroz/booking/repository.py`
- Modify: `project/src/moroz/notifications/lifecycle.py`
- Modify: `project/src/moroz/notifications/ports.py`
- Modify: `project/tests/contract/booking/test_yclients_adapter.py`
- Modify: `project/tests/unit/booking/test_models.py`
- Modify: `project/tests/unit/booking/test_mock_adapter.py`
- Modify: `project/tests/integration/booking/test_booking_repository.py`
- Modify: `project/tests/unit/notifications/test_ports.py`
- Modify: `project/tests/unit/notifications/test_lifecycle.py`
- Modify: `project/tests/integration/notifications/test_lifecycle_persistence.py`
- Modify: `project/tests/unit/booking/test_yclients_sandbox_smoke.py`

**Interfaces:**
- Consumes: `BookingCatalogPort`, `CatalogService`, `CatalogStaff`, `YclientsHttpClient.request(method, path, user_auth=False)`.
- Produces: `YclientsCatalogAdapter(client, company_id, service_allowlist, staff_allowlist)` and protected booking snapshots containing `service_ids` plus actual `staff_id`.

- [ ] **Step 1: Write failing contract tests for filtering and fail-closed parsing**

```python
@pytest.mark.asyncio
async def test_catalog_returns_only_allowlisted_services_and_staff(fake_http):
    fake_http.queue_json(200, {"success": True, "data": [{"id": 1, "title": "Крио", "duration": 1800}, {"id": 9, "title": "Скрытая", "duration": 600}]})
    fake_http.queue_json(200, {"success": True, "data": [{"id": 7, "name": "Анна", "services": [1]}, {"id": 8, "name": "Скрытый", "services": [1]}]})
    adapter = YclientsCatalogAdapter(fake_http, "42", ("1",), ("7",))
    assert [item.id for item in await adapter.list_services()] == ["1"]
    assert [item.id for item in await adapter.list_staff(("1",))] == ["7"]

@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 500])
async def test_catalog_http_failure_is_temporary_without_cached_success(fake_http, status):
    fake_http.queue(status, b"provider payload")
    with pytest.raises(BookingTemporaryError):
        await YclientsCatalogAdapter(fake_http, "42", ("1",), ("7",)).list_services()

def test_protected_booking_snapshot_contains_services_and_actual_staff(parsed_booking):
    assert parsed_booking.service_ids == ("1", "2")
    assert parsed_booking.staff_id == "7"
```

- [ ] **Step 2: Run RED contract tests in Docker**

Run: `cd project && docker compose --env-file ../.env --profile test run --rm test pytest tests/contract/booking/test_yclients_catalog.py -q`

Expected: FAIL because adapter is absent.

- [ ] **Step 3: Implement read-only endpoints with strict envelopes**

```python
class YclientsCatalogAdapter(BookingCatalogPort):
    async def list_services(self) -> list[CatalogService]:
        response = await self._client.request("GET", f"/api/v1/book_services/{self._company_id}")
        items = _items(_envelope(response))
        return [
            CatalogService(str(item["id"]), _required_text(item.get("title")), _duration_minutes(item))
            for item in items
            if str(item.get("id")) in self._service_allowlist
        ]

    async def list_staff(self, service_ids: tuple[str, ...]) -> list[CatalogStaff]:
        response = await self._client.request("GET", f"/api/v1/book_staff/{self._company_id}")
        selected = set(service_ids)
        return [staff for staff in _parse_staff(_items(_envelope(response)))
                if staff.id in self._staff_allowlist and selected.issubset(staff.service_ids)]
```

All unexpected status/envelope/type failures translate to `BookingTemporaryError`; response bodies and tokens never enter exception text or logs.
Extend `ExternalBooking` with required immutable
`service_ids: tuple[str, ...]` and `staff_id: str`; create/get/reschedule
parsers and mock responses must populate both. Update every constructor in the
listed booking/notification tests rather than adding permissive defaults. This
lets ownership validation compare exact services/master/time and lets «Любой
мастер» show the provider-assigned staff member in the final summary.

- [ ] **Step 4: Run GREEN contract and existing YCLIENTS tests**

Run: `cd project && docker compose --env-file ../.env --profile test run --rm test pytest tests/unit/booking tests/contract/booking tests/integration/booking tests/unit/notifications tests/integration/notifications tests/e2e/notifications -q`

Expected: PASS.

- [ ] **Step 5: Update docs and commit**

```bash
git add project/src/moroz/booking/yclients_catalog.py project/src/moroz/booking/yclients_http.py project/src/moroz/booking/models.py project/src/moroz/booking/yclients.py project/src/moroz/booking/mock_yclients.py project/src/moroz/booking/repository.py project/src/moroz/notifications/lifecycle.py project/src/moroz/notifications/ports.py project/tests/contract/booking/test_yclients_catalog.py project/tests/contract/booking/test_yclients_adapter.py project/tests/unit/booking/test_models.py project/tests/unit/booking/test_mock_adapter.py project/tests/unit/booking/test_yclients_sandbox_smoke.py project/tests/integration/booking/test_booking_repository.py project/tests/unit/notifications/test_ports.py project/tests/unit/notifications/test_lifecycle.py project/tests/integration/notifications/test_lifecycle_persistence.py 'Дорожная карта.md' changelog.md
git commit -m "feat: добавлен read-only каталог YCLIENTS"
```

### Task 3: Durable Telegram interaction ingress

**Files:**
- Modify: `project/src/moroz/messaging/models.py`
- Modify: `project/src/moroz/messaging/repository.py`
- Modify: `project/src/moroz/messaging/service.py`
- Modify: `project/llm/webhook.py`
- Modify: `project/tests/e2e/test_privacy_gate.py`
- Create: `project/tests/e2e/booking/test_telegram_ingress.py`

**Interfaces:**
- Produces: `IncomingMessage.kind: Literal["text", "callback", "contact"]`, `data: Mapping[str, object]`; persisted payload contains kind/data and keeps `text` as a non-secret display string.
- Consumes: existing consent gate, inbox dedupe and `MessageService.accept`.

- [ ] **Step 1: Write failing callback/contact ingress tests**

```python
@pytest.mark.asyncio
async def test_booking_callback_is_accepted_into_inbox_without_direct_execution(client, database):
    response = await client.post("/telegram/webhook", headers=SECRET, json=booking_callback("booking:opaque123"))
    assert response.status_code == 200
    row = await fetch_inbox(database)
    assert row["payload"]["kind"] == "callback"
    assert row["payload"]["data"] == {"callback_data": "booking:opaque123"}

@pytest.mark.asyncio
async def test_contact_must_belong_to_sender(client, database):
    await client.post("/telegram/webhook", headers=SECRET, json=contact_update(sender_id=10, contact_user_id=11))
    assert await inbox_count(database) == 0
```

- [ ] **Step 2: Run RED ingress tests**

Run: `cd project && docker compose --env-file ../.env --profile test run --rm test pytest tests/e2e/booking/test_telegram_ingress.py tests/e2e/test_privacy_gate.py -q`

Expected: FAIL because booking callback/contact is not normalized.

- [ ] **Step 3: Extend the immutable ingress model and webhook branches**

```python
@dataclass(frozen=True, slots=True)
class IncomingMessage:
    update_id: str
    message_id: str
    channel: str
    chat_id: str
    user_id: str
    text: str
    received_at: datetime
    correlation_id: UUID
    kind: Literal["text", "callback", "contact"] = "text"
    data: Mapping[str, object] = field(default_factory=dict)
```

Consent callbacks remain handled before this branch. A `booking:` callback becomes `kind="callback"`; a contact becomes `kind="contact"` only when `message.contact.user_id == message.from_user.id`. Both require private chat, processing consent and the same durable `accept()` path; webhook never calls workflow/YCLIENTS.

- [ ] **Step 4: Run GREEN ingress and messaging regression**

Run: `cd project && docker compose --env-file ../.env --profile test run --rm test pytest tests/e2e/booking/test_telegram_ingress.py tests/e2e/test_privacy_gate.py tests/integration/messaging -q`

Expected: PASS.

- [ ] **Step 5: Update docs and commit**

```bash
git add project/src/moroz/messaging/models.py project/src/moroz/messaging/repository.py project/src/moroz/messaging/service.py project/llm/webhook.py project/tests/e2e/booking/test_telegram_ingress.py project/tests/e2e/test_privacy_gate.py 'Дорожная карта.md' changelog.md
git commit -m "feat: добавлен durable ingress booking interactions"
```

### Task 4: Migration and workflow repository

**Files:**
- Create: `project/migrations/versions/0010_telegram_booking_flow.py`
- Create: `project/src/moroz/booking/workflow_repository.py`
- Create: `project/tests/integration/booking/test_workflow_repository.py`
- Modify: `project/tests/integration/test_migrations.py`

**Interfaces:**
- Produces: `BookingAction`, `WorkflowSession`, `BookingWorkflowRepository.start()`, `get_active()`, `checkpoint()`, `issue_action()`, `consume_action()`, `list_owned_active_bookings()`, `is_human_mode()`.
- Consumes: existing `booking_scenarios`, `bookings`, `booking_events`.

- [ ] **Step 1: Write migration/repository RED tests**

```python
@pytest.mark.asyncio
async def test_action_is_owner_revision_and_expiry_bound(repository, clock):
    scenario = await repository.start("create", "telegram", "10", "10", "start:1")
    action = await repository.issue_action(scenario.id, scenario.revision, "choose_service", {"service_id": "1"}, clock.now() + timedelta(minutes=30))
    assert await repository.consume_action(action.id, "telegram", "10", "10") == action
    assert await repository.consume_action(action.id, "telegram", "11", "11") is None

@pytest.mark.asyncio
async def test_only_one_active_scenario_per_telegram_owner(repository):
    first = await repository.start("create", "telegram", "10", "10", "start:1")
    second = await repository.start("create", "telegram", "10", "10", "start:1")
    assert second.id == first.id
```

- [ ] **Step 2: Run RED migration tests**

Run: `cd project && docker compose --env-file ../.env --profile test run --rm test pytest tests/integration/test_migrations.py tests/integration/booking/test_workflow_repository.py -q`

Expected: FAIL because revision/action storage is absent.

- [ ] **Step 3: Add schema and repository locking**

Migration adds `channel`, `chat_id`, `revision default 0`, `expires_at` to `booking_scenarios`; creates `booking_actions(id text primary key, scenario_id uuid, customer_id text, channel text, chat_id text, revision int, action_kind text, payload jsonb, expires_at timestamptz, consumed_at timestamptz, result jsonb)`; adds partial unique active-scenario index for phases `collecting/awaiting_confirmation/executing`; adds `resolved_by` and `resolution_reason` to `escalations`.

```python
async def consume_action(self, action_id: str, channel: str, chat_id: str, customer_id: str) -> BookingAction | None:
    async with self._database.acquire() as connection:
        async with connection.transaction():
            row = await connection.fetchrow(ACTION_FOR_UPDATE_SQL, action_id)
            if row is None or row["channel"] != channel or row["chat_id"] != chat_id or row["customer_id"] != customer_id:
                return None
            if row["expires_at"] <= self._now() or row["revision"] != row["scenario_revision"]:
                return None
            return _action_from_row(row)
```

Consuming marks `consumed_at` only in the same transaction that checkpoints scenario and saves `result`; replay reads the saved result without repeating a port call.

- [ ] **Step 4: Run GREEN repository tests**

Run: `cd project && docker compose --env-file ../.env --profile test run --rm test pytest tests/integration/test_migrations.py tests/integration/booking/test_workflow_repository.py -q`

Expected: PASS, including concurrent revision tests.

- [ ] **Step 5: Update docs and commit**

```bash
git add project/migrations/versions/0010_telegram_booking_flow.py project/src/moroz/booking/workflow_repository.py project/tests/integration/test_migrations.py project/tests/integration/booking/test_workflow_repository.py 'Дорожная карта.md' changelog.md
git commit -m "feat: добавлено durable состояние Telegram booking flow"
```

### Task 5: Structured advisory LLM-router

**Files:**
- Create: `project/src/moroz/booking/intent_router.py`
- Create: `project/src/moroz/booking/interaction.py`
- Create: `project/tests/unit/booking/test_intent_router.py`
- Modify: `project/llm/llm.py`
- Modify: `project/src/moroz/security/pipeline.py`

**Interfaces:**
- Produces: `IntentVerdict(route, confidence)`, `StructuredIntentRouter.route(masked_text, masked_context)`, exported `route_intent()`.
- Consumes: `LLMRequest`, `LLMResponse`, `PrimaryReserveGateway`, `PiiSession`, deterministic `check_input`/`route_message` safety signals.

- [ ] **Step 1: Write strict schema/fallback RED tests**

```python
@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["not-json", '{"route":"booking_create","confidence":2}', '{"route":"tool","confidence":1}'])
async def test_invalid_router_response_falls_back_to_unknown(body):
    router = StructuredIntentRouter(FakeGateway(body), threshold=0.80)
    assert await router.route("хочу записаться", []) == IntentVerdict("unknown", 0.0)

@pytest.mark.asyncio
async def test_low_confidence_never_selects_booking():
    router = StructuredIntentRouter(FakeGateway('{"route":"booking_create","confidence":0.79}'), threshold=0.80)
    assert await router.route("может быть", []) == IntentVerdict("unknown", 0.79)
```

- [ ] **Step 2: Run RED router tests**

Run: `cd project && docker compose --env-file ../.env --profile test run --rm test pytest tests/unit/booking/test_intent_router.py tests/unit/security/test_pipeline.py -q`

Expected: FAIL because structured router is absent.

- [ ] **Step 3: Implement enum-only JSON parsing and masked request**

```python
ROUTES = frozenset({"booking_create", "booking_reschedule", "booking_cancel", "faq", "other", "complaint", "medical_risk", "unknown"})

async def route(self, text: str, context: list[dict[str, str]]) -> IntentVerdict:
    session = PiiSession()
    masked = session.mask(text).text
    try:
        response = await self._gateway.complete(LLMRequest(messages=router_messages(masked, mask_context(session, context)), purpose="router"))
        payload = json.loads(response.text)
        route = payload["route"]
        confidence = float(payload["confidence"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, LLMUnavailable, NonRetryableLLMError):
        return IntentVerdict("unknown", 0.0)
    if route not in ROUTES or not 0.0 <= confidence <= 1.0:
        return IntentVerdict("unknown", 0.0)
    return IntentVerdict(route if confidence >= self._threshold else "unknown", confidence)
```

The system prompt explicitly forbids parameter extraction and includes only the enum schema. `check_input` and deterministic complaint/medical signals run before this call; a blocking result is never overwritten.

- [ ] **Step 4: Run GREEN router/security tests**

Run: `cd project && docker compose --env-file ../.env --profile test run --rm test pytest tests/unit/booking/test_intent_router.py tests/unit/messaging/test_router.py tests/unit/security/test_pipeline.py -q`

Expected: PASS.

- [ ] **Step 5: Update docs and commit**

```bash
git add project/src/moroz/booking/intent_router.py project/src/moroz/booking/interaction.py project/tests/unit/booking/test_intent_router.py project/llm/llm.py project/src/moroz/security/pipeline.py 'Дорожная карта.md' changelog.md
git commit -m "feat: добавлен безопасный structured booking router"
```

### Task 6: Presenter and create-booking state machine

**Files:**
- Create: `project/src/moroz/booking/presenter.py`
- Create: `project/src/moroz/booking/workflow.py`
- Create: `project/tests/unit/booking/test_presenter.py`
- Create: `project/tests/unit/booking/test_workflow_create.py`

**Interfaces:**
- Produces: `WorkflowReply(text, delivery_options)`, `BookingWorkflow.handle(interaction)`, opaque inline keyboards.
- Consumes: catalog, workflow repository, `BookingService`, `SlotQuery`, `BookingIdentity`.

- [ ] **Step 1: Write RED tests for create UX and confirmation TTL**

```python
@pytest.mark.asyncio
async def test_create_collects_services_master_slot_name_owned_contact_and_summary(workflow):
    reply = await workflow.start_create(owner("10"), "command:10")
    assert button_texts(reply) == ["Крио", "Массаж", "Готово", "Отмена"]
    await choose(reply, "Крио")
    await choose_service_done()
    assert "Любой мастер" in button_texts(await current_reply())
    await choose_master("Любой мастер")
    await choose_slot("slot-1")
    assert (await send_text("Мария")).request_contact is True
    summary = await send_contact(user_id="10", phone="+70000000000")
    assert "+7******0000" in summary.text
    assert button_texts(summary) == ["Подтвердить", "Изменить", "Отмена"]

@pytest.mark.asyncio
async def test_expired_summary_never_calls_booking_service(workflow, service, clock):
    confirmation = await ready_confirmation(workflow)
    clock.advance(minutes=31)
    reply = await press(confirmation, "Подтвердить")
    service.handle.assert_not_awaited()
    assert "истекло" in reply.text.lower()
```

- [ ] **Step 2: Run RED workflow tests**

Run: `cd project && docker compose --env-file ../.env --profile test run --rm test pytest tests/unit/booking/test_presenter.py tests/unit/booking/test_workflow_create.py -q`

Expected: FAIL because presenter/workflow are absent.

- [ ] **Step 3: Implement deterministic transitions**

```python
async def _confirm_create(self, session: WorkflowSession, action: BookingAction) -> WorkflowReply:
    if action.expires_at <= self._now():
        return await self._restart_slot_selection(session, "Срок подтверждения истёк. Выберите время заново.")
    result = await self._booking_service.handle(session.id, confirmed=True)
    return await self._present_result(session, action, result)
```

Every button comes from `issue_action`; callback contains `booking:<action.id>` only. Selection state stores service IDs, staff choice, 14-day aware time range, selected opaque slot ID, name, phone and processing consent. `Назад` creates a new revision; `executing` has no back path. Multi-service selection has pagination and no application count cap.

- [ ] **Step 4: Run GREEN workflow and existing service tests**

Run: `cd project && docker compose --env-file ../.env --profile test run --rm test pytest tests/unit/booking/test_presenter.py tests/unit/booking/test_workflow_create.py tests/e2e/booking/test_create_booking.py -q`

Expected: PASS.

- [ ] **Step 5: Update docs and commit**

```bash
git add project/src/moroz/booking/presenter.py project/src/moroz/booking/workflow.py project/tests/unit/booking/test_presenter.py project/tests/unit/booking/test_workflow_create.py 'Дорожная карта.md' changelog.md
git commit -m "feat: добавлен create workflow с явным подтверждением"
```

### Task 7: Worker dispatcher and dependency wiring

**Files:**
- Modify: `project/worker/main.py`
- Modify: `project/tests/unit/test_worker.py`
- Create: `project/tests/e2e/booking/test_telegram_create_flow.py`

**Interfaces:**
- Produces: `MessageDispatcher.dispatch(interaction, context, recent_count)` with deterministic priority.
- Consumes: `BookingWorkflow`, `route_intent`, consultant LLM, workflow repository, real/mock adapters.

- [ ] **Step 1: Write RED priority and end-to-end worker tests**

```python
@pytest.mark.asyncio
async def test_active_scenario_bypasses_router_and_consultant(dispatcher, router, consultant):
    await dispatcher.dispatch(callback_interaction("booking:abc"), [], 1)
    router.assert_not_awaited()
    consultant.assert_not_awaited()

@pytest.mark.asyncio
async def test_router_booking_route_starts_workflow_without_answer_llm(dispatcher, router, consultant):
    router.return_value = IntentVerdict("booking_create", 0.94)
    reply = await dispatcher.dispatch(text_interaction("хочу записаться"), [], 1)
    assert "Выберите услуги" in reply.text
    consultant.assert_not_awaited()
```

- [ ] **Step 2: Run RED worker tests**

Run: `cd project && docker compose --env-file ../.env --profile test run --rm test pytest tests/unit/test_worker.py tests/e2e/booking/test_telegram_create_flow.py -q`

Expected: FAIL because worker always invokes consultant.

- [ ] **Step 3: Insert dispatcher before consultant**

```python
if await self._workflow_repository.is_human_mode(customer_id):
    result = WorkflowReply("Сообщение передано администратору.", {})
else:
    result = await self._dispatcher.dispatch(interaction, context, int(recent_message_count))
```

`_process_message` validates one `user_id`, requires private Telegram `chat_id == user_id`, reconstructs `Interaction` from persisted kind/data, and stores user/assistant history plus keyboard in `delivery_options`. Explicit commands/callback/contact/active scenario precede deterministic safety, structured router and consultant. `unknown` returns clarification buttons.

- [ ] **Step 4: Run GREEN create/message delivery regressions**

Run: `cd project && docker compose --env-file ../.env --profile test run --rm test pytest tests/unit/test_worker.py tests/e2e/booking/test_telegram_create_flow.py tests/e2e/test_message_delivery.py -q`

Expected: PASS.

- [ ] **Step 5: Update docs and commit**

```bash
git add project/worker/main.py project/tests/unit/test_worker.py project/tests/e2e/booking/test_telegram_create_flow.py 'Дорожная карта.md' changelog.md
git commit -m "feat: подключён booking workflow к Telegram worker"
```

### Task 8: Owned list/view, whole-record reschedule and cancel

**Files:**
- Modify: `project/src/moroz/booking/workflow.py`
- Modify: `project/src/moroz/booking/workflow_repository.py`
- Modify: `project/src/moroz/booking/presenter.py`
- Create: `project/tests/unit/booking/test_workflow_change.py`
- Create: `project/tests/e2e/booking/test_telegram_change_flow.py`

**Interfaces:**
- Produces: create/reschedule/cancel entry routes and whole-record summaries.
- Consumes: local `bookings`, protected `BookingService.handle(..., identity=BookingIdentity(customer_id, True))`.

- [ ] **Step 1: Write RED ownership, late-rule and whole-record tests**

```python
@pytest.mark.asyncio
async def test_foreign_booking_is_not_listed_or_disclosed(workflow_repository):
    await seed_booking(owner="20", external_id="secret")
    assert await workflow_repository.list_owned_active_bookings("10") == []

@pytest.mark.asyncio
async def test_my_bookings_view_uses_protected_snapshot(workflow, booking_port):
    await seed_owned_booking(services=("1", "2"), staff_id="7")
    reply = await workflow.list_bookings(owner("10"))
    assert "Крио" in reply.text and "Массаж" in reply.text and "Анна" in reply.text
    assert booking_port.get_booking.await_count == 1

@pytest.mark.asyncio
async def test_reschedule_preserves_all_services_and_reselects_staff_and_slot(workflow):
    await seed_owned_booking(services=("1", "2"), staff_id="7")
    reply = await workflow.start_reschedule(owner("10"), "command:change")
    await choose_booking(reply)
    assert "Крио" in reply.text and "Массаж" in reply.text
    assert "Любой мастер" in button_texts(await next_reply())

@pytest.mark.asyncio
async def test_late_cancel_escalates_without_delete(workflow, booking_port):
    await seed_owned_booking(starts_in=timedelta(hours=2, minutes=59))
    reply = await confirm_cancel(workflow)
    booking_port.cancel_booking.assert_not_awaited()
    assert "администратор" in reply.text.lower()
```

- [ ] **Step 2: Run RED change tests**

Run: `cd project && docker compose --env-file ../.env --profile test run --rm test pytest tests/unit/booking/test_workflow_change.py tests/e2e/booking/test_telegram_change_flow.py -q`

Expected: FAIL because worker workflow only supports create.

- [ ] **Step 3: Add owned selection and confirmation paths**

```python
identity = BookingIdentity(customer_id=interaction.user_id, confirmed=True)
result = await self._booking_service.handle(session.id, confirmed=True, identity=identity)
```

The explicit `/bookings` command and permanent «Мои записи» button use the same
owned list and protected GET; they never pass through the LLM-router. List query
filters local `customer_id`, `status='confirmed'`, bot-created booking key and
future start. Protected result must exactly match external ID, booking key,
services, actual staff, status and start time before details are shown.
Reschedule copies the complete service tuple, then collects new staff/slot and
presents old/new summary. Cancel uses a separate `Да, отменить запись` action.
Partial service-change commands create escalation without mutation.

- [ ] **Step 4: Run GREEN change and protected lifecycle tests**

Run: `cd project && docker compose --env-file ../.env --profile test run --rm test pytest tests/unit/booking/test_workflow_change.py tests/e2e/booking/test_telegram_change_flow.py tests/e2e/booking/test_change_booking.py -q`

Expected: PASS.

- [ ] **Step 5: Update docs and commit**

```bash
git add project/src/moroz/booking/workflow.py project/src/moroz/booking/workflow_repository.py project/src/moroz/booking/presenter.py project/tests/unit/booking/test_workflow_change.py project/tests/e2e/booking/test_telegram_change_flow.py 'Дорожная карта.md' changelog.md
git commit -m "feat: добавлены безопасные перенос и отмена записи"
```

### Task 9: Atomic escalation, durable human mode and admin resolution

**Files:**
- Modify: `project/src/moroz/booking/repository.py`
- Modify: `project/src/moroz/escalation/service.py`
- Create: `project/admin/escalation_routes.py`
- Create: `project/admin/templates/escalations.html`
- Modify: `project/admin/app.py`
- Create: `project/tests/integration/booking/test_booking_escalation.py`
- Create: `project/tests/e2e/admin/test_escalation_flow.py`

**Interfaces:**
- Produces: atomic escalation transaction, `GET /escalations/`, `POST /escalations/{id}/reply`, `POST /escalations/{id}/resolve`.
- Consumes: `outbound_messages`, `task_outbox`, `human_mode`, `admin_audit_events`, RBAC/CSRF.

- [ ] **Step 1: Write RED atomicity/admin tests**

```python
@pytest.mark.asyncio
async def test_booking_escalation_atomically_sets_all_durable_records(database, service):
    result = await trigger_outcome_unknown(service)
    assert result.error_code == "booking_outcome_unknown"
    assert await count("escalations", status="open") == 1
    assert await human_mode_enabled(result_customer_id) is True
    assert await outbound_exists("staff:booking_outcome_unknown")
    assert await outbound_exists("client:booking_outcome_unknown")

@pytest.mark.asyncio
async def test_resolve_requires_csrf_writes_audit_and_disables_human_mode(admin_client):
    response = await admin_client.post(resolve_url, data={"csrf_token": csrf, "reason": "Проверено вручную"})
    assert response.status_code == 302
    assert await escalation_status() == "resolved"
    assert await human_mode_enabled(customer_id) is False
    assert await audit_action() == "escalation.resolve"
```

- [ ] **Step 2: Run RED escalation/admin tests**

Run: `cd project && docker compose --env-file ../.env --profile test run --rm test pytest tests/integration/booking/test_booking_escalation.py tests/e2e/admin/test_escalation_flow.py -q`

Expected: FAIL because booking escalation is not connected to escalation/human/outbox and admin has no resolution UI.

- [ ] **Step 3: Implement one transaction and audited admin actions**

`BookingRepository._escalate_with_connection` updates scenario, inserts `booking_events`, inserts/upserts `escalations` and `human_mode`, and enqueues both outbounds plus `send_outbound` tasks on the same connection. Staff text contains reason code and internal scenario UUID only; client text never promises a slot.

```python
await connection.execute("UPDATE escalations SET status='resolved', resolved_at=now(), resolved_by=$2, resolution_reason=$3 WHERE id=$1 AND status='open'", escalation_id, user.id, reason)
await connection.execute("UPDATE human_mode SET enabled=false WHERE escalation_id=$1", escalation_id)
await record_audit(actor_id=user.id, action="escalation.resolve", object_type="escalation", object_id=str(escalation_id), before={"status": "open"}, after={"status": "resolved", "reason": reason}, ip_address=request_ip_address(request), user_agent=request_user_agent(request))
```

Admin reply uses `MessageRepository.enqueue_outbound` semantics in the admin DB layer and does not disable human mode. Resolve is explicit, owner/manager-only, CSRF-protected and has no TTL automation.

- [ ] **Step 4: Run GREEN escalation/admin/security tests**

Run: `cd project && docker compose --env-file ../.env --profile test run --rm test pytest tests/integration/booking/test_booking_escalation.py tests/e2e/admin/test_escalation_flow.py tests/e2e/admin/test_csrf_rbac_audit.py -q`

Expected: PASS.

- [ ] **Step 5: Update docs and commit**

```bash
git add project/src/moroz/booking/repository.py project/src/moroz/escalation/service.py project/admin/escalation_routes.py project/admin/templates/escalations.html project/admin/app.py project/tests/integration/booking/test_booking_escalation.py project/tests/e2e/admin/test_escalation_flow.py 'Дорожная карта.md' changelog.md
git commit -m "feat: добавлена durable booking escalation и human mode"
```

### Task 10: Replay, race, failures and restart E2E gate

**Files:**
- Create: `project/tests/e2e/booking/test_telegram_reliability.py`
- Create: `project/src/moroz/booking/reconciliation.py`
- Create: `project/tests/integration/booking/test_reconciliation.py`
- Modify: `project/src/moroz/booking/workflow.py`
- Modify: `project/src/moroz/booking/workflow_repository.py`
- Modify: `project/src/moroz/booking/mock_yclients.py`

**Interfaces:**
- Produces: proven replay/race/restart semantics for Telegram workflow and `BookingLookupPort.find_by_booking_key(booking_key)` for read-only reconciliation.
- Consumes: all core flow interfaces from Tasks 1–9.

- [ ] **Step 1: Add RED reliability matrix**

```python
@pytest.mark.asyncio
async def test_duplicate_update_and_callback_call_create_once(flow, port):
    update = await ready_create_confirmation(flow)
    await asyncio.gather(flow.deliver(update), flow.deliver(update))
    assert port.create_calls == 1

@pytest.mark.asyncio
async def test_two_users_race_one_slot(flow, port):
    first, second = await ready_two_confirmations(flow, same_slot=True)
    replies = await asyncio.gather(flow.press(first), flow.press(second))
    assert sorted(reply_kind(reply) for reply in replies) == ["confirmed", "slot_unavailable"]

@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "429", "500", "malformed", "outcome_unknown"])
async def test_provider_failures_never_claim_success(flow, port, failure):
    port.fail_next(failure)
    reply = await flow.confirm_create()
    assert "подтверждена" not in reply.text.lower()
    assert await successful_booking_count() == 0

@pytest.mark.asyncio
async def test_reconciliation_closes_only_one_exact_booking_key_match(reconciler, provider):
    provider.matches = [exact_match()]
    result = await reconciler.reconcile(scenario_id)
    assert result.status == "confirmed"
    provider.matches = []
    assert (await reconciler.reconcile(other_scenario_id)).status == "escalated"
```

Also cover foreign action, foreign booking, stale revision, expired action, `executing` after restart, Redis unavailable, outbox replay and exact audit reason codes.

- [ ] **Step 2: Run RED E2E matrix**

Run: `cd project && docker compose --env-file ../.env --profile test run --rm test pytest tests/e2e/booking/test_telegram_reliability.py -q`

Expected: at least one case fails before final concurrency/replay hardening.

- [ ] **Step 3: Make minimal locking/replay corrections**

Use existing inbox unique key, scenario `FOR UPDATE`, action revision, action
terminal result, booking external advisory lock and mock slot occupancy lock. An
`executing` scenario calls `BookingService`, which converts it to
`booking_outcome_unknown`; it never replays the mutation.

`BookingReconciler` performs read-only provider search by the opaque
`moroz_booking_key`. Exactly one match must also equal expected owner binding,
services, staff, start and status before the repository writes a terminal local
snapshot and resolves the escalation. Zero, multiple or mismatched matches keep
human mode/escalation open; reconciliation never calls cancel/reschedule.
`BookingLookupPort` is a separate read-only protocol in `reconciliation.py`;
the real and mock adapters satisfy it without expanding the mutation-oriented
`BookingPort` contract.

- [ ] **Step 4: Run core booking gate**

Run: `cd project && docker compose --env-file ../.env --profile test run --rm test pytest tests/unit/booking tests/contract/booking tests/integration/booking tests/e2e/booking -q`

Expected: PASS with no deselected booking tests.

- [ ] **Step 5: Update docs and commit**

```bash
git add project/tests/e2e/booking/test_telegram_reliability.py project/src/moroz/booking/reconciliation.py project/tests/integration/booking/test_reconciliation.py project/src/moroz/booking/workflow.py project/src/moroz/booking/workflow_repository.py project/src/moroz/booking/mock_yclients.py 'Дорожная карта.md' changelog.md
git commit -m "test: доказана надёжность Telegram booking flow"
```

### Task 11: Read-only YCLIENTS preflight and evidence

**Files:**
- Create: `project/src/moroz/booking/yclients_readonly_check.py`
- Create: `project/tests/unit/booking/test_yclients_readonly_check.py`
- Modify: `project/docker-compose.yml`
- Modify: `project/worker/main.py`
- Create: `docs/testing/telegram-yclients-booking-test-plan.md`

**Interfaces:**
- Produces: Compose profile `yclients-readonly`, JSON summary containing counts/IDs only, exit `0` only when allowlisted catalog and 14-day availability are readable.
- Consumes: real catalog and slot adapters; no mutation method.

- [ ] **Step 1: Write RED command tests proving method allowlist**

```python
@pytest.mark.asyncio
async def test_readonly_check_calls_only_get(fake_backend):
    result = await run_readonly_check(fake_backend, service_ids=("1",), staff_ids=("7",), horizon_days=14)
    assert result.ok is True
    assert set(fake_backend.methods) == {"GET"}
    assert "phone" not in json.dumps(result.summary).lower()
```

- [ ] **Step 2: Run RED unit test**

Run: `cd project && docker compose --env-file ../.env --profile test run --rm test pytest tests/unit/booking/test_yclients_readonly_check.py -q`

Expected: FAIL because read-only command is absent.

- [ ] **Step 3: Implement bounded read-only command and Compose profile**

The command lists allowlisted services/staff and slots from `now` through
`now + 14 days`, records only item counts and configured IDs, rejects
redirects/unexpected envelopes, and has no reference to
create/reschedule/cancel functions. `BOOKING_MODE=real` runs the same read-only
catalog preflight during worker startup and refuses readiness when configured
IDs are absent, duplicated or inaccessible; mock mode does not require network.
Add a test-plan evidence table with command, timestamp, environment label, exit
code and sanitized result.

- [ ] **Step 4: Run local command tests, then request external permission/config before live read-only call**

Run local: `cd project && docker compose --env-file ../.env --profile test run --rm test pytest tests/unit/booking/test_yclients_readonly_check.py tests/contract/booking/test_yclients_catalog.py -q`

Expected: PASS.

After confirming the configured company is the intended test/sandbox company, run: `cd project && docker compose --env-file ../.env --profile yclients-readonly run --rm yclients-readonly`

Expected: exit `0`, sanitized counts, no POST/PUT/PATCH/DELETE in captured transport evidence. Do not run this external command if configuration or company scope is uncertain.

- [ ] **Step 5: Update docs and commit**

```bash
git add project/src/moroz/booking/yclients_readonly_check.py project/tests/unit/booking/test_yclients_readonly_check.py project/docker-compose.yml project/worker/main.py docs/testing/telegram-yclients-booking-test-plan.md 'Дорожная карта.md' changelog.md
git commit -m "test: добавлен read-only gate YCLIENTS"
```

### Task 12: Permission-gated sandbox lifecycle with bounded cleanup

**Files:**
- Modify: `project/src/moroz/booking/yclients_sandbox_smoke.py`
- Modify: `project/tests/unit/booking/test_yclients_sandbox_smoke.py`
- Modify: `docs/testing/telegram-yclients-booking-test-plan.md`

**Interfaces:**
- Produces: permission-gated `create → get → reschedule → get → cancel → reconciliation` evidence using fake identity and unique `moroz_booking_key`.
- Consumes: existing sandbox smoke backend and real protected adapter.

- [ ] **Step 1: Add RED safety-bound tests**

```python
def test_sandbox_requires_explicit_consent_fake_identity_and_bounded_window(base_env):
    for patch in ({"YCLIENTS_SANDBOX_CONSENT": ""}, {"YCLIENTS_TEST_PHONE": "+79991234567"}, {"YCLIENTS_TEST_WINDOW_DAYS": "31"}):
        env = {**base_env, **patch}
        with pytest.raises(ValueError):
            SandboxSmokeSettings.from_env(env)

@pytest.mark.asyncio
async def test_unknown_outcome_reconciles_without_blind_cancel(backend):
    backend.create_outcome_unknown = True
    result = await run_smoke(settings(), backend)
    assert backend.reconcile_calls == 1
    assert backend.cancel_calls == 0
    assert result.manual_reconciliation_required is True
```

- [ ] **Step 2: Run RED sandbox unit tests only**

Run: `cd project && docker compose --env-file ../.env --profile test run --rm test pytest tests/unit/booking/test_yclients_sandbox_smoke.py -q`

Expected: FAIL on the new bounds before implementation.

- [ ] **Step 3: Add exact settings and reconciliation report**

Require `YCLIENTS_SANDBOX_CONSENT=I_UNDERSTAND_THIS_CREATES_TEST_BOOKINGS`, explicit sandbox company marker, reserved fake phone prefix from project config, `YCLIENTS_TEST_WINDOW_DAYS` in `1..14`, two future slots, unique run UUID and final reconciliation summary. Cleanup targets only the created booking key/external ID from this run.

- [ ] **Step 4: Run unit gate, then stop for separate mutation permission**

Run unit: `cd project && docker compose --env-file ../.env --profile test run --rm test pytest tests/unit/booking/test_yclients_sandbox_smoke.py -q`

Expected: PASS.

Only after the user explicitly authorizes sandbox mutations for the identified test company and fake dataset, run: `cd project && docker compose --env-file ../.env --profile yclients-smoke run --rm yclients-smoke`

Expected: all six lifecycle checkpoints successful or a fail-closed report with bounded reconciliation; never infer permission from approval of this plan.

- [ ] **Step 5: Record sanitized evidence and commit**

```bash
git add project/src/moroz/booking/yclients_sandbox_smoke.py project/tests/unit/booking/test_yclients_sandbox_smoke.py docs/testing/telegram-yclients-booking-test-plan.md 'Дорожная карта.md' changelog.md
git commit -m "test: усилен bounded sandbox lifecycle YCLIENTS"
```

### Task 13: Scheduler/reminders post-booking gate

**Files:**
- Modify: `project/src/moroz/booking/repository.py`
- Modify: `project/src/moroz/notifications/ports.py`
- Modify: `project/src/moroz/notifications/lifecycle.py`
- Modify: `project/docker-compose.yml`
- Create: `project/tests/e2e/notifications/test_booking_flow_reminders.py`
- Modify: `docs/testing/telegram-yclients-booking-test-plan.md`

**Interfaces:**
- Produces: reminders only for confirmed owned booking snapshots; safe job replacement/cancellation and replay handling.
- Consumes: existing notification planner/repository/scheduler, proven core booking flow and successful sandbox gate.

- [ ] **Step 1: Keep scheduler disabled until preconditions are recorded**

Before editing Compose, verify test-plan evidence contains successful Tasks 10–12 and obtain separate user approval to enable scheduler in the target environment. Without both, run unit/E2E tests but leave deployment profile disabled.

- [ ] **Step 2: Write RED lifecycle tests**

```python
@pytest.mark.asyncio
async def test_create_reschedule_cancel_replaces_jobs_without_duplicates(flow, jobs):
    booking = await flow.create_confirmed()
    assert await jobs.pending_for(booking.booking_key) == EXPECTED_CREATE_KINDS
    moved = await flow.reschedule_confirmed(booking)
    assert await jobs.pending_starts(moved.booking_key) == {moved.starts_at}
    await flow.cancel_confirmed(moved)
    assert await jobs.pending_for(moved.booking_key) == []

@pytest.mark.asyncio
async def test_outcome_unknown_and_foreign_owner_schedule_nothing(flow, jobs):
    await flow.create_outcome_unknown()
    await flow.try_foreign_change()
    assert await jobs.all_pending() == []
```

- [ ] **Step 3: Run RED scheduler tests**

Run: `cd project && docker compose --env-file ../.env --profile test run --rm test pytest tests/e2e/notifications/test_booking_flow_reminders.py -q`

Expected: FAIL if job lifecycle or ownership is not fully connected to Telegram flow.

- [ ] **Step 4: Implement minimal job gating and run GREEN notification suite**

Only `BookingRepository.confirm` syncs jobs from a confirmed terminal snapshot. Reschedule marks old pending jobs skipped before inserting the new plan; cancel skips all pending jobs; escalated/outcome-unknown states insert none. `NotificationOutbox` sends to stored verified chat ID for the same owner and keeps deterministic idempotency keys.

Run: `cd project && docker compose --env-file ../.env --profile test run --rm test pytest tests/unit/notifications tests/integration/notifications tests/e2e/notifications -q`

Expected: PASS.

- [ ] **Step 5: Enable only after gate, update docs and commit**

After explicit approval, remove only the staging-disable mechanism chosen by the deployment config; do not start production services in this commit.

```bash
git add project/src/moroz/booking/repository.py project/src/moroz/notifications/ports.py project/src/moroz/notifications/lifecycle.py project/docker-compose.yml project/tests/e2e/notifications/test_booking_flow_reminders.py docs/testing/telegram-yclients-booking-test-plan.md 'Дорожная карта.md' changelog.md
git commit -m "feat: подключены reminders к подтверждённому booking flow"
```

### Task 14: Full Docker verification, evidence report and handoff

**Files:**
- Modify: `docs/testing/telegram-yclients-booking-test-plan.md`
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`

**Interfaces:**
- Produces: reproducible evidence for every required scenario and a clean local commit series.
- Consumes: all prior tasks.

- [ ] **Step 1: Run formatting/static repository checks**

Run: `git diff --check`

Run: `cd project && docker compose --env-file ../.env config --quiet`

Expected: exit `0` for both.

- [ ] **Step 2: Run focused booking and notification suites**

Run: `cd project && docker compose --env-file ../.env --profile test run --rm test pytest tests/unit/booking tests/contract/booking tests/integration/booking tests/e2e/booking tests/unit/notifications tests/integration/notifications tests/e2e/notifications -q`

Expected: PASS, no unexpected deselections.

- [ ] **Step 3: Run full project suite with the repository-required docs mount**

Run: `cd project && docker compose --env-file ../.env --profile test run --rm -v "${PWD}/../docs:/repo/docs:ro" test pytest -q`

Expected: PASS. If an already documented unrelated test remains excluded, name the exact node ID, reproduce it separately and do not report a clean full gate.

- [ ] **Step 4: Complete evidence index**

For each of create/get/reschedule/get/cancel, duplicate/replay, two-user slot race, foreign booking/action, timeout/429/5xx/malformed, outcome unknown/reconciliation, Telegram UI, PostgreSQL inbox/outbox/events/escalations/audit and scheduler/reminders, record the exact Docker command, test node IDs, timestamp, pass count and sanitized DB assertions. Record read-only/sandbox evidence only if their explicit gates were actually run.

- [ ] **Step 5: Final review and local commit**

Run: `git status --short`

Expected: only intended documentation/status changes before commit, then empty output after commit.

```bash
git add docs/testing/telegram-yclients-booking-test-plan.md 'Дорожная карта.md' changelog.md
git commit -m "docs: зафиксированы доказательства Telegram YCLIENTS flow"
```

Do not push. Use `verification-before-completion` before claiming success and `requesting-code-review` before integration.
