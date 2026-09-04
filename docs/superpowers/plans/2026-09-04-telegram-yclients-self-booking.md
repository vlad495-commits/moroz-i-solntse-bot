# Telegram YCLIENTS Self-Booking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Завершить запись внутри Telegram: создать, показать, перенести и отменить только собственные записи клиента через существующий YCLIENTS adapter.

**Architecture:** Telegram webhook сохраняет text/callback/contact в существующий durable inbox. Worker перед LLM передаёт booking-ввод одному детерминированному coordinator, который хранит шаги в `booking_scenarios`, читает локальный каталог и вызывает существующий `BookingService`/`YclientsAdapter`; ответы проходят через текущий ordered outbound. LLM не получает контакт и не выполняет mutations.

**Tech Stack:** Python 3.12, aiogram 3.x, FastAPI, asyncpg, PostgreSQL 16, RabbitMQ, Redis, Alembic, Docker Compose, pytest.

## Global Constraints

- Все команды Python, Alembic и pytest выполняются только через Docker Compose.
- Не добавлять новые сервисы, frontend, Mini App, storage, очередь или dependency.
- Один филиал и одна услуга в записи.
- Управлять только будущими активными записями этого Telegram `customer_id`, созданными ботом.
- Любой POST/PUT/DELETE выполняется только после явного подтверждения.
- Не повторять mutation после unknown outcome.
- Логин, пароль, TOTP, YCLIENTS tokens, телефон, имя и raw provider body не выводятся в логи, Git и changelog.
- Каждый production-код пишется только после ожидаемого RED-теста.
- После каждого логического шага обновлять `Дорожная карта.md`, сразу писать безопасную запись в `changelog.md` и делать отдельный commit.
- Source design: `docs/superpowers/specs/2026-09-04-telegram-yclients-self-booking-design.md`.

---

### Task 1: Durable Telegram callback/contact ingress

**Files:**
- Modify: `project/src/moroz/messaging/models.py`
- Modify: `project/src/moroz/messaging/repository.py`
- Modify: `project/src/moroz/messaging/service.py`
- Modify: `project/llm/webhook.py`
- Test: `project/tests/integration/messaging/test_repository.py`
- Test: `project/tests/e2e/test_privacy_gate.py`

**Interfaces:**
- Produces: `IncomingMessage.kind: Literal["text", "callback", "contact"]` and `IncomingMessage.data: Mapping[str, object]`.
- Produces: `MessageService.accept_interaction_consented(message: IncomingMessage) -> bool`.
- Preserves: existing text buffering and `process_message:<update_ids>` task contract.

- [x] **Step 1: Write failing repository tests for structured ingress**

Add tests which create these exact messages and assert the stored JSONB payload contains the fields while the `task_outbox` payload contains only `update_ids`:

```python
callback = replace(
    incoming_message,
    update_id="booking-callback-1",
    kind="callback",
    data={"callback_data": "booking:v1:abc:service:0"},
)
contact = replace(
    incoming_message,
    update_id="booking-contact-1",
    kind="contact",
    data={
        "contact_user_id": "7",
        "phone_number": "+70000000000",
        "first_name": "Тест",
        "last_name": "Клиент",
    },
)
```

Assert duplicate `(channel, external_message_id)` remains idempotent and neither phone nor name appears in `task_outbox.payload::text`.

- [x] **Step 2: Run RED**

```powershell
docker compose --env-file ../.env run --rm test pytest -q `
  tests/integration/messaging/test_repository.py `
  tests/e2e/test_privacy_gate.py -k "booking_interaction"
```

Expected: collection or assertion failures because `IncomingMessage` has no `kind/data` and webhook does not persist booking interactions.

- [x] **Step 3: Add the minimal immutable input fields**

Extend the model without breaking existing keyword constructors:

```python
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

@dataclass(frozen=True, slots=True)
class IncomingMessage:
    # existing fields stay unchanged
    kind: Literal["text", "callback", "contact"] = "text"
    data: Mapping[str, object] = field(default_factory=dict)
```

Serialize `kind` and `dict(data)` in `_insert_incoming`. Add `accept_interaction_consented` which calls `accept_if_consented(..., enqueue_directly=True)` and never enters Redis buffer.

- [x] **Step 4: Persist booking callbacks and own contacts in webhook**

Use callback prefix `booking:v1:`. Acknowledge callback first, then persist it through `accept_interaction_consented`. For a contact message persist only after existing private-chat, deletion and processing-consent gates. Store `contact_user_id`, phone and names; set `text=""`. Do not accept a booking contact from a group.

Non-booking callbacks keep the current consent/marketing/reactivation behavior.

- [x] **Step 5: Run GREEN and regression**

```powershell
docker compose --env-file ../.env run --rm test pytest -q `
  tests/integration/messaging/test_repository.py `
  tests/integration/messaging/test_buffer.py `
  tests/e2e/test_privacy_gate.py
```

Expected: all selected tests pass; existing text batching is unchanged.

- [x] **Step 6: Document and commit**

```powershell
git add -- project/src/moroz/messaging project/llm/webhook.py `
  project/tests/integration/messaging project/tests/e2e/test_privacy_gate.py `
  'Дорожная карта.md' changelog.md
git commit -m "feat: добавлен durable ввод записи из Telegram"
```

---

### Task 2: Booking-flow persistence and catalog choices

**Files:**
- Create: `project/migrations/versions/0025_telegram_booking_flow.py`
- Modify: `project/src/moroz/booking/repository.py`
- Modify: `project/src/moroz/booking/catalog.py`
- Test: `project/tests/integration/test_migrations.py`
- Test: `project/tests/integration/booking/test_booking_repository.py`
- Test: `project/tests/integration/booking/test_catalog_projection.py`

**Interfaces:**
- Produces: `BookingRepository.get_active_for_customer(customer_id: str) -> BookingScenario | None`.
- Produces: `BookingRepository.list_future_owned(customer_id: str, now: datetime) -> list[tuple[ExternalBooking, Mapping[str, object]]]`.
- Produces: `CatalogRepository.list_services(connection) -> tuple[CatalogService, ...]`.
- Enforces: at most one `collecting|awaiting_confirmation|executing` scenario per customer.

- [x] **Step 1: Write migration RED**

Add assertions that Alembic head is `0025_telegram_booking_flow` and PostgreSQL contains:

```sql
CREATE UNIQUE INDEX uq_booking_scenarios_open_customer
ON booking_scenarios (customer_id)
WHERE phase IN ('collecting', 'awaiting_confirmation', 'executing')
```

Also prove two open scenarios for one customer fail, while a new scenario after `failed`, `confirmed` or `escalated` succeeds.

- [x] **Step 2: Run migration RED**

```powershell
docker compose --env-file ../.env run --rm test pytest -q `
  tests/integration/test_migrations.py -k telegram_booking_flow
```

Expected: fail because revision `0025_telegram_booking_flow` and its unique index do not exist.

- [x] **Step 3: Add the additive revision**

Create revision `0025_telegram_booking_flow` with `down_revision = "0024_reactivation_v2"`. `upgrade()` creates only the partial unique index; `downgrade()` drops only that index. Do not rewrite existing booking data. Pre-migration test must assert no duplicate open customer exists, so production migration fails closed instead of choosing a winner.

- [x] **Step 4: Write repository/catalog RED**

Tests must prove:

```python
assert (await repo.get_active_for_customer("42")).id == active.id
assert await repo.get_active_for_customer("missing") is None
owned = await repo.list_future_owned("42", NOW)
assert [item[0].external_id for item in owned] == ["own-future"]
assert [service.service_name for service in await catalog.list_services(connection)] == [
    "Криокапсула",
]
```

Exclude cancelled, past and another customer's bookings. Return the last scenario state with each booking so Telegram can render service/staff names without decoding provider payloads.

- [x] **Step 5: Implement minimal read methods and run GREEN**

Use existing row mappers and `_group_records`; add no second catalog model. Order active scenario by `created_at DESC, id DESC`, future bookings by `starts_at, external_id`, services by current catalog grouping.

```powershell
docker compose --env-file ../.env run --rm test pytest -q `
  tests/integration/test_migrations.py `
  tests/integration/booking/test_booking_repository.py `
  tests/integration/booking/test_catalog_projection.py
```

Expected: all selected tests pass and Alembic reports one head.

- [x] **Step 6: Document and commit**

```powershell
git add -- project/migrations/versions/0025_telegram_booking_flow.py `
  project/src/moroz/booking/repository.py project/src/moroz/booking/catalog.py `
  project/tests/integration 'Дорожная карта.md' changelog.md
git commit -m "feat: добавлено состояние Telegram-записи"
```

---

### Task 3: Deterministic create-booking coordinator

**Files:**
- Create: `project/src/moroz/booking/telegram.py`
- Create: `project/tests/unit/booking/test_telegram_booking.py`
- Test: `project/tests/e2e/booking/test_telegram_booking.py`

**Interfaces:**
- Produces: `BookingReply(text: str, delivery_options: dict[str, object])`.
- Produces: `TelegramBookingCoordinator.handle(connection, *, customer_id: str, user_id: str, update_id: str, text: str, kind: str, data: Mapping[str, object]) -> BookingReply | None`.
- Consumes: `BookingRepository`, `CatalogRepository`, `BookingService`, `BookingPort`.

- [ ] **Step 1: Write RED for start, choices and stale callbacks**

Use a real PostgreSQL repository and `MockYclientsAdapter`. Prove:

```python
reply = await coordinator.handle(
    connection,
    customer_id="42",
    user_id="7",
    update_id="100",
    text="Хочу записаться",
    kind="text",
    data={},
)
assert reply.text == "Выберите услугу"
assert await repo.get_active_for_customer("42") is not None
assert llm.calls == []
```

Callbacks use `booking:v1:<scenario_hex>:<action>:<choice_index>`. Assert wrong customer, wrong scenario, wrong step and out-of-range choice return a safe stale-button reply and perform no provider call.

- [ ] **Step 2: Run RED**

```powershell
docker compose --env-file ../.env run --rm test pytest -q `
  tests/unit/booking/test_telegram_booking.py `
  tests/e2e/booking/test_telegram_booking.py
```

Expected: collection failure because the coordinator module does not exist.

- [ ] **Step 3: Implement start and server-owned choices**

Create one coordinator file. Store in `BookingScenario.state`:

```python
{
    "step": "service",
    "choices": [{"service_id": "331", "label": "Криокапсула"}],
    "source": "telegram",
}
```

Callbacks contain only scenario ID, action and index. Resolve every index against current `state["choices"]`; never accept service/staff/slot provider IDs directly from callback data.

- [ ] **Step 4: Write RED for service → staff → date → slot**

Assert the exact progression:

```text
service -> staff (Любой специалист first) -> available_date -> slot -> contact
```

Call `BookingPort.list_slots` with the selected single service, optional staff and a bounded 14-day UTC-aware interval. Show at most seven dates and eight slots per date. Store display names, `slot_query` and the signed `selected_slot_id` in scenario state.

- [ ] **Step 5: Implement slot progression and run focused GREEN**

Reuse `SlotQuery` and adapter-returned signed `Slot.id`. When no slots exist, return a clear retry/admin reply and keep the flow at staff/date selection. Do not invent availability and do not call LLM.

- [ ] **Step 6: Write RED for contact and confirmation**

Prove:

- a Telegram contact is accepted only when `contact_user_id == user_id`;
- a foreign contact is rejected;
- manual Russian numbers normalize from `8XXXXXXXXXX`, `7XXXXXXXXXX` or ten digits to `+7XXXXXXXXXX`;
- invalid input stays on contact step;
- missing contact name moves to `name` step;
- summary masks the phone as `+7******1234`;
- only `confirm` calls `BookingService.handle(..., confirmed=True)`;
- duplicate confirm returns the stored terminal result without a second provider mutation.

- [ ] **Step 7: Implement contact and create confirmation**

Use stdlib digit normalization; add no phone library. Read durable processing consent before accepting contact. At completed collection set `phase="awaiting_confirmation"` and state keys required by existing `BookingService`: `slot_query`, `selected_slot_id`, `customer_name`, `customer_phone`, `personal_data_processing_allowed=True`, plus safe display labels.

- [ ] **Step 8: Run Task 3 GREEN**

```powershell
docker compose --env-file ../.env run --rm test pytest -q `
  tests/unit/booking/test_telegram_booking.py `
  tests/e2e/booking/test_telegram_booking.py `
  tests/e2e/booking/test_create_booking.py
```

Expected: all selected tests pass; fake adapter shows exactly one create mutation.

- [ ] **Step 9: Document and commit**

```powershell
git add -- project/src/moroz/booking/telegram.py `
  project/tests/unit/booking/test_telegram_booking.py `
  project/tests/e2e/booking/test_telegram_booking.py `
  'Дорожная карта.md' changelog.md
git commit -m "feat: добавлена запись внутри Telegram"
```

---

### Task 4: Own bookings, reschedule and cancel

**Files:**
- Modify: `project/src/moroz/booking/telegram.py`
- Modify: `project/src/moroz/booking/repository.py`
- Test: `project/tests/unit/booking/test_telegram_booking.py`
- Test: `project/tests/e2e/booking/test_telegram_booking.py`
- Test: `project/tests/e2e/booking/test_change_booking.py`

**Interfaces:**
- Extends: `TelegramBookingCoordinator.handle(...)` with `booking_management` and `my_bookings`.
- Preserves: existing `BookingIdentity`, three-hour cutoff and unknown-outcome escalation.

- [ ] **Step 1: Write ownership RED**

Seed own future, other-customer, cancelled and past bookings. Assert `Мои записи` renders only the own future active record and callback selection cannot address another external ID even when forged.

- [ ] **Step 2: Run ownership RED**

```powershell
docker compose --env-file ../.env run --rm test pytest -q `
  tests/e2e/booking/test_telegram_booking.py -k "my_bookings or ownership"
```

Expected: fail because management flow is absent.

- [ ] **Step 3: Implement server-owned booking selection**

Store choices as local booking identifiers from `list_future_owned`; callback carries only choice index. Render service/staff labels from the last scenario state. When none exist, answer that the bot can manage only records created through this Telegram chat and provide the administrator contact.

- [ ] **Step 4: Write reschedule/cancel RED**

Assert:

```python
assert reschedule_result.status == "ok"
assert cancel_result.status == "ok"
assert fake_port.reschedule_calls == 1
assert fake_port.cancel_calls == 1
```

Also assert action without explicit confirmation makes zero mutation calls, a change inside three hours returns `late_booking_change`, and `BookingOutcomeUnknown` becomes `booking_outcome_unknown` without retry.

- [ ] **Step 5: Implement using existing BookingService**

Create `reschedule` and `cancel` `BookingScenario` objects with current local `external_id`, `starts_at`, service/staff display state and exact original `booking_key`. Pass `BookingIdentity(customer_id, confirmed=True)` only after the repository ownership query. For reschedule reuse the same service and return to staff/date/slot steps; do not support service change.

- [ ] **Step 6: Run GREEN and regression**

```powershell
docker compose --env-file ../.env run --rm test pytest -q `
  tests/e2e/booking/test_telegram_booking.py `
  tests/e2e/booking/test_change_booking.py `
  tests/integration/booking/test_booking_repository.py
```

Expected: all pass; mutations occur once and only for owned records.

- [ ] **Step 7: Document and commit**

```powershell
git add -- project/src/moroz/booking/telegram.py `
  project/src/moroz/booking/repository.py project/tests `
  'Дорожная карта.md' changelog.md
git commit -m "feat: добавлено управление своими записями"
```

---

### Task 5: Worker routing and Telegram keyboards

**Files:**
- Modify: `project/worker/main.py`
- Modify: `project/src/moroz/messaging/telegram.py`
- Modify: `project/src/moroz/messaging/router.py`
- Modify: `project/llm/prompts/system.md`
- Test: `project/tests/unit/test_worker.py`
- Test: `project/tests/e2e/test_message_delivery.py`
- Test: `project/tests/e2e/test_catalog_message_flow.py`

**Interfaces:**
- Worker consumes `IncomingMessage.kind/data` from persisted inbox payload.
- Worker invokes `TelegramBookingCoordinator` before catalog grounding and LLM.
- Telegram delivery accepts `InlineKeyboardMarkup`, `ReplyKeyboardMarkup` and `ReplyKeyboardRemove` from durable `delivery_options`.

- [ ] **Step 1: Write worker routing RED**

Tests must prove an active flow consumes ordinary text, `route_message(...).route == "booking"` starts a flow, and `booking_management` opens own bookings. In all three cases assert `llm.calls == []`; unrelated consultation still calls the LLM exactly once.

- [ ] **Step 2: Write keyboard delivery RED**

Persist and claim three outbounds containing:

```python
{"reply_markup": {"inline_keyboard": [[{"text": "Подтвердить", "callback_data": "booking:v1:x:confirm:0"}]]}}
{"reply_markup": {"keyboard": [[{"text": "Поделиться номером", "request_contact": True}]], "resize_keyboard": True, "one_time_keyboard": True}}
{"reply_markup": {"remove_keyboard": True}}
```

Assert aiogram receives the matching markup class and no mutable caller dictionary changes persisted JSON.

- [ ] **Step 3: Run RED**

```powershell
docker compose --env-file ../.env run --rm test pytest -q `
  tests/unit/test_worker.py `
  tests/e2e/test_message_delivery.py -k "booking or reply_keyboard"
```

Expected: failures because worker has no coordinator and sender validates only inline markup.

- [ ] **Step 4: Wire coordinator before LLM**

Build one `YclientsAdapter` in `_build_yclients_services` and pass it both to existing lifecycle/admin components and `TelegramBookingCoordinator`. If all three YCLIENTS variables are absent, coordinator is `None` and booking intent returns the safe unavailable/admin response; partial configuration keeps the existing startup failure.

Parse `kind/data` defensively in `_process_message`. Interaction updates are never concatenated with text batches. Enqueue `BookingReply` through the existing repository using `reply:<process_message key>`.

- [ ] **Step 5: Support native contact keyboard**

In `deliver_claimed_outbound`, validate markup by shape:

```python
if "inline_keyboard" in reply_markup:
    markup = InlineKeyboardMarkup.model_validate(reply_markup)
elif "keyboard" in reply_markup:
    markup = ReplyKeyboardMarkup.model_validate(reply_markup)
elif reply_markup.get("remove_keyboard") is True:
    markup = ReplyKeyboardRemove.model_validate(reply_markup)
else:
    raise ValueError("unsupported Telegram reply markup")
```

Do not pass arbitrary keys directly to Telegram.

- [ ] **Step 6: Update the prompt boundary**

Replace claims that the bot cannot see availability or create records with the exact implemented boundary: it can create and manage only records made by the same user through this Telegram bot. Keep the public YCLIENTS link as fallback for unsupported/external records, not as the primary successful path.

- [ ] **Step 7: Run Task 5 GREEN**

```powershell
docker compose --env-file ../.env run --rm test pytest -q `
  tests/unit/test_worker.py `
  tests/e2e/test_message_delivery.py `
  tests/e2e/test_catalog_message_flow.py `
  tests/e2e/booking/test_telegram_booking.py
```

Expected: all selected tests pass; booking messages bypass LLM and consultation behavior remains unchanged.

- [ ] **Step 8: Document and commit**

```powershell
git add -- project/worker/main.py project/src/moroz/messaging/telegram.py `
  project/src/moroz/messaging/router.py project/llm/prompts/system.md `
  project/tests 'Дорожная карта.md' changelog.md
git commit -m "feat: подключена Telegram-запись к YCLIENTS"
```

---

### Task 6: Privacy, retention and failure gates

**Files:**
- Modify: `project/admin/customer_data_deletion.py`
- Modify: `project/src/moroz/retention.py`
- Modify: `project/tests/integration/admin/test_customer_data_deletion_postgres.py`
- Modify: `project/tests/integration/test_retention_postgres.py`
- Modify: `project/tests/e2e/booking/test_yclients_fail_closed.py`
- Modify: `project/ops/failure-gates.md`

**Interfaces:**
- Customer deletion removes contact payload and new open/terminal booking flow data under the existing customer advisory fence.
- Retention removes expired interaction payloads using the existing configured retention boundary.
- YCLIENTS failure leaves consultation/FAQ operational and booking mutation stopped.

- [ ] **Step 1: Write privacy RED**

Seed phone/name in `message_inbox.payload`, `booking_scenarios.state`, booking events and outbound text for customer `42`, plus unrelated control rows. Run existing deletion service and assert every sentinel for `42` is absent while control data remains.

- [ ] **Step 2: Write retention/failure RED**

Assert expired booking interaction inbox/outbound rows are removed, fresh rows remain, and missing/stale YCLIENTS configuration yields a safe booking response without an LLM invention or provider mutation.

- [ ] **Step 3: Run RED**

```powershell
docker compose --env-file ../.env run --rm test pytest -q `
  tests/integration/admin/test_customer_data_deletion_postgres.py `
  tests/integration/test_retention_postgres.py `
  tests/e2e/booking/test_yclients_fail_closed.py -k telegram_booking
```

Expected: new sentinel assertions fail against the pre-change cleanup graph.

- [ ] **Step 4: Extend existing cleanup paths only**

Add the new payload relationships to the existing deletion transaction and retention batches; do not create a second cleanup service. Preserve lock ordering and projection suppression. Update failure-gates documentation with the Telegram booking fallback.

- [ ] **Step 5: Run privacy GREEN and broad regression**

```powershell
docker compose --env-file ../.env run --rm test pytest -q `
  tests/integration/admin/test_customer_data_deletion_postgres.py `
  tests/integration/test_retention_postgres.py `
  tests/e2e/booking `
  tests/e2e/test_privacy_gate.py
```

Expected: all selected tests pass; safe logs contain no sentinel phone/name.

- [ ] **Step 6: Document and commit**

```powershell
git add -- project/admin/customer_data_deletion.py `
  project/src/moroz/retention.py project/tests project/ops/failure-gates.md `
  'Дорожная карта.md' changelog.md
git commit -m "fix: закрыты privacy-гейты Telegram-записи"
```

---

### Task 7: Release verification and customer-owned YCLIENTS acceptance

**Files:**
- Modify: `docs/superpowers/plans/2026-09-04-telegram-yclients-self-booking.md`
- Modify: `docs/superpowers/plans/2026-08-20-customer-owned-yclients-app-onboarding.md`
- Modify: `project/ops/launch-checklist.md`
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`

**Interfaces:**
- Produces a reviewed commit-pinned candidate.
- Reuses the existing customer-owned app onboarding sequence; does not store credentials in Git.

- [ ] **Step 1: Run focused Docker gate**

```powershell
docker compose --env-file ../.env run --rm test pytest -q `
  tests/contract/booking `
  tests/unit/booking `
  tests/integration/booking `
  tests/e2e/booking `
  tests/unit/test_worker.py `
  tests/e2e/test_privacy_gate.py
```

Expected: exit `0`, no skips introduced by this feature.

- [ ] **Step 2: Run migration and static gates**

```powershell
docker compose --env-file ../.env --profile test run --rm migrate upgrade head
docker compose --env-file ../.env run --rm test python -m compileall -q src llm worker admin scheduler
docker compose --env-file ../.env config --quiet
git diff --check
```

Expected: single head `0025_telegram_booking_flow`; all commands exit `0`.

- [ ] **Step 3: Run the full Docker suite once**

```powershell
docker compose --env-file ../.env run --rm test pytest -q
```

Expected: exit `0`. Record exact count and duration, not an estimate.

- [ ] **Step 4: Review the exact branch diff**

Check ownership, stale callbacks, duplicate updates, contact validation, secret/PII output, unknown mutation outcomes, deletion/retention and rollback. Resolve every Critical/Important finding test-first before continuing.

- [ ] **Step 5: Complete customer-owned read-only acceptance**

Follow Tasks 1–5 of `2026-08-20-customer-owned-yclients-app-onboarding.md`: customer-owned private/free app, minimal permissions including records-list, exact `moroz_booking_key`, protected staging env backup, worker recreation, GET-only services/staff/slots/fields/records and successful projection scheduler. Output only statuses, counts and booleans.

- [ ] **Step 6: Run one explicitly authorized mutation acceptance**

After fresh owner confirmation, run the existing Docker `yclients-smoke` exactly once. Require `success=true`, `manual_review_required=false`, `matches=1`, `active_matches=0`. Never retry an unknown result.

- [ ] **Step 7: Perform manual Telegram staging acceptance**

With one synthetic customer verify: create with contact button, duplicate callback, occupied slot refresh, `Мои записи`, reschedule, cancel, late-change escalation and unrelated consultation. Confirm no external link is needed on the successful path and no synthetic active booking remains.

- [ ] **Step 8: Rehearse rollback and finish docs**

Rehearse candidate → previous → candidate images and protected `.env` restore without DB downgrade. Update the onboarding plan, launch checklist, roadmap and changelog with safe evidence only.

- [ ] **Step 9: Commit completion evidence**

```powershell
git add -- docs project/ops/launch-checklist.md 'Дорожная карта.md' changelog.md
git commit -m "test: подтверждена Telegram-запись через YCLIENTS"
git status --short --branch
```

Expected: clean feature branch. Push, PR, staging rollout and production deployment remain separate explicitly authorized actions.
