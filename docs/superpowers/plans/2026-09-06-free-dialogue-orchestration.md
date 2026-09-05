# Free Dialogue Orchestration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Перевести Telegram-бота на свободный LLM-диалог без persistent-меню и жёсткой анкеты, сохранив подтверждение и backend-гарантии реальных YCLIENTS-операций.

**Architecture:** Обычный текст один раз проходит security и расширенный strict Router V3; backend мержит разрешённые сущности в независимый booking draft, валидирует каталог/слоты и сам определяет следующий недостающий факт. Контакт и callback остаются узкими детерминированными входами, а create/reschedule/cancel по-прежнему выполняются только существующим `BookingService` после подписанного явного подтверждения.

**Tech Stack:** Python 3.12, aiogram 3.x, PostgreSQL, Redis, RabbitMQ, Docker Compose, pytest, Alembic.

## Global Constraints

- Все runtime-команды и тесты запускаются только через Docker Compose из `project/` с `--env-file ../.env`; прямой `python bot.py` запрещён.
- Не добавлять agent framework, tool-calling SDK, runtime service, storage, таблицу или dependency.
- Не менять `BookingService`, YCLIENTS ownership/idempotency/slot recheck, durable inbox/outbox, privacy fence и scripts-first STOP/medical escalation иначе чем для сохранения их текущего контракта.
- Router не возвращает PII, provider IDs, цену, слот или mutation-команду.
- Любой router failure, low confidence, stale callback или обычный text fail-closed: он не выполняет create/reschedule/cancel.
- Кнопки допустимы только для consent, `request_contact`, короткого выбора до трёх вариантов и явного подтверждения.
- Старые Router V1/V2 datasets immutable; Router V3 добавляется отдельным файлом и additive migration.
- Временные файлы создаются только в корневом `tmp/`; каждое значимое действие сразу записывается в `changelog.md`.
- Staging credentials читаются только самим Compose из `/opt/moroz-staging/.env`, не выводятся в терминал/логи/чат и не копируются в Git.
- Production, GitHub push, платный live LLM eval и неоговорённые YCLIENTS mutations вне scope.

---

## File Map

- `project/src/moroz/messaging/router.py` — единственный strict intent/entity contract Router V3 и parser/validation.
- `project/src/moroz/booking/conversation.py` — чистые функции merge/invalidation, missing-field derivation и time-window filtering.
- `project/src/moroz/booking/telegram.py` — тонкий coordinator текста/contact/callback и построение только контекстных кнопок.
- `project/src/moroz/security/pipeline.py` — один semantic dispatch для разрешённого текста и безопасные natural-language fallbacks.
- `project/worker/main.py` — durable orchestration без menu-aware splitting и повторного dispatch.
- `project/src/moroz/messaging/telegram.py` — Telegram delivery primitives без persistent keyboard.
- `project/llm/eval/router_dataset_v3.json` + migration `0026_router_v3.py` — immutable Router V3 cases и seed админки.
- `project/admin/eval_runner.py` — структурированное сравнение RouteDecision V3.
- `project/tests/**` — новые invariant tests; старые menu/catalog-wizard tests удаляются только после переноса safety assertions.

---

### Task 1: Router V3 strict contract

**Files:**
- Modify: `project/src/moroz/messaging/router.py`
- Modify: `project/tests/unit/messaging/test_router.py`
- Modify: `project/tests/unit/security/test_semantic_dispatch.py`

**Interfaces:**
- Produces: `RouteDecision(route, confidence, action, topics, services, date, time_from, time_to, staff, choice)`.
- Produces: `LLMIntentRouter.route(text: str, context: list[dict[str, str]] | None, draft: dict[str, object] | None) -> RouteDecision`.
- Removes: exact-label `deterministic_route()` menu bypass.

- [x] **Step 1: Add failing schema/parser tests**

```python
def test_router_parses_multi_intent_and_time_window():
    decision = parse_route_decision({
        "route": "booking", "action": "create",
        "topics": ["price"], "services": ["криосауна"],
        "date": "2026-09-08", "time_from": "18:00", "time_to": None,
        "staff": None, "choice": None, "confidence": 0.94,
    })
    assert decision.topics == ("price",)
    assert decision.services == ("криосауна",)
    assert decision.time_from == "18:00"

@pytest.mark.parametrize("field,value", [
    ("time_from", "25:00"), ("time_to", "вечером"),
    ("choice", -1), ("services", ["a", "b", "c", "d"]),
])
def test_router_rejects_invalid_v3_fields(field, value):
    payload = valid_v3_payload() | {field: value}
    with pytest.raises(RouteParseError):
        parse_route_decision(payload)
```

- [x] **Step 2: Run RED**

Run: `docker compose --env-file ../.env run --rm test pytest -q tests/unit/messaging/test_router.py tests/unit/security/test_semantic_dispatch.py`

Expected: FAIL because V3 fields/parser do not exist and menu labels still bypass semantic routing.

- [x] **Step 3: Implement the minimal V3 dataclass, JSON schema and validation**

```python
@dataclass(frozen=True, slots=True)
class RouteDecision:
    route: str
    confidence: float
    action: str = "none"
    topics: tuple[str, ...] = ()
    services: tuple[str, ...] = ()
    date: str | None = None
    time_from: str | None = None
    time_to: str | None = None
    staff: str | None = None
    choice: int | None = None

def _valid_time(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value) is None:
        raise RouteParseError("invalid time")
    return value
```

The strict schema declares every property required, uses nullable types, `additionalProperties: false`, max three services, unique topics, and route/action compatibility checked by Python. Remove `deterministic_route`; the prompt explicitly maps exact windows and requires `null` for vague time.

- [x] **Step 4: Run GREEN and commit**

Run: `docker compose --env-file ../.env run --rm test pytest -q tests/unit/messaging/test_router.py tests/unit/security/test_semantic_dispatch.py`

Expected: PASS.

```powershell
git add project/src/moroz/messaging/router.py project/tests/unit/messaging/test_router.py project/tests/unit/security/test_semantic_dispatch.py changelog.md
git commit -m "feat: расширить контракт Router V3"
```

---

### Task 2: Router V3 dataset, migration and admin eval

**Files:**
- Create: `project/llm/eval/router_dataset_v3.json`
- Create: `project/migrations/versions/0026_router_v3.py`
- Modify: `project/migrate/Dockerfile`
- Modify: `project/admin/eval_runner.py`
- Modify: `project/tests/unit/messaging/test_router_dataset.py`
- Modify: `project/tests/unit/admin/test_router_eval_runner.py`
- Modify: `project/tests/unit/test_migration_profile.py`
- Modify: `project/tests/integration/test_migrations.py`

**Interfaces:**
- Consumes: all Task 1 `RouteDecision` fields.
- Produces: immutable suite name `router_v3`; `router_case_diff(expected, actual) -> dict[str, object]` compares every expected structured field.

- [ ] **Step 1: Add RED tests for dataset shape, structured diff and migration head**

```python
def test_router_v3_dataset_has_required_coverage(router_v3_cases):
    categories = {case["category"] for case in router_v3_cases}
    assert {"time_window", "multi_intent", "correction", "negation", "ownership", "injection"} <= categories
    assert all(set(case["expected"]) >= {"route", "action", "topics", "services"} for case in router_v3_cases)

def test_router_case_diff_reports_time_window():
    diff = router_case_diff(
        {"route": "booking", "time_from": "18:00"},
        {"route": "booking", "time_from": None},
    )
    assert diff == {"time_from": {"expected": "18:00", "actual": None}}
```

- [ ] **Step 2: Run RED**

Run: `docker compose --env-file ../.env run --rm test pytest -q tests/unit/messaging/test_router_dataset.py tests/unit/admin/test_router_eval_runner.py tests/unit/test_migration_profile.py tests/integration/test_migrations.py`

Expected: FAIL because `router_v3`, migration `0026_router_v3` and structured comparison do not exist.

- [ ] **Step 3: Add immutable cases and additive seed migration**

Each dataset item has concrete `id`, `input`, `context`, `category`, `critical`, and `expected`. Include at least these exact critical expectations:

```json
{
  "id": "booking_after_18",
  "input": "Запишите на криосауну 8 сентября после 18:00",
  "context": [],
  "category": "time_window",
  "critical": true,
  "expected": {
    "route": "booking", "action": "create", "topics": [],
    "services": ["криосауна"], "date": "2026-09-08",
    "time_from": "18:00", "time_to": null, "staff": null, "choice": null
  }
}
```

Migration `0026_router_v3` hashes the shipped JSON, inserts/updates only suite `router_v3`, and `downgrade()` deletes only that suite. `project/migrate/Dockerfile` copies the V3 file. Change `ROUTER_EVAL_SUITE = "router_v3"`; save actual V3 fields and compare only fields present in expected data.

- [ ] **Step 4: Run GREEN, verify single head and commit**

Run: `docker compose --env-file ../.env run --rm test pytest -q tests/unit/messaging/test_router_dataset.py tests/unit/admin/test_router_eval_runner.py tests/unit/test_migration_profile.py tests/integration/test_migrations.py`

Run: `docker compose --env-file ../.env run --rm migrate alembic heads`

Expected: tests PASS; output contains exactly `0026_router_v3 (head)`.

```powershell
git add project/llm/eval/router_dataset_v3.json project/migrations/versions/0026_router_v3.py project/migrate/Dockerfile project/admin/eval_runner.py project/tests/unit/messaging/test_router_dataset.py project/tests/unit/admin/test_router_eval_runner.py project/tests/unit/test_migration_profile.py project/tests/integration/test_migrations.py changelog.md
git commit -m "feat: добавить eval-набор Router V3"
```

---

### Task 3: Order-independent booking draft

**Files:**
- Create: `project/src/moroz/booking/conversation.py`
- Create: `project/tests/unit/booking/test_conversation.py`

**Interfaces:**
- Consumes: `RouteDecision` from Task 1 and existing catalog/slot domain objects.
- Produces: `merge_draft(state: dict[str, object], decision: RouteDecision) -> dict[str, object]`.
- Produces: `next_requirement(state: Mapping[str, object]) -> Literal["service", "date", "time", "slot", "contact", "name", "confirm"]`.
- Produces: `filter_slots(slots: Sequence[Slot], time_from: str | None, time_to: str | None) -> list[Slot]`.

- [ ] **Step 1: Add RED tests for merge, correction and filtering**

```python
def test_date_correction_invalidates_slot_but_keeps_service():
    state = {"service_id": "7", "service_name": "Криосауна", "date": "2026-09-08", "slot": {"id": "old"}}
    merged = merge_draft(state, decision(date="2026-09-09"))
    assert merged["service_id"] == "7"
    assert merged["date"] == "2026-09-09"
    assert "slot" not in merged

def test_filter_slots_honours_after_18():
    assert [slot.starts_at.hour for slot in filter_slots(fake_slots(17, 18, 19), "18:00", None)] == [18, 19]

def test_multiple_services_requires_service_choice():
    merged = merge_draft({}, decision(services=("криосауна", "массаж")))
    assert merged["service_candidates"] == ["криосауна", "массаж"]
    assert next_requirement(merged) == "service"
```

- [ ] **Step 2: Run RED**

Run: `docker compose --env-file ../.env run --rm test pytest -q tests/unit/booking/test_conversation.py`

Expected: FAIL with missing module/functions.

- [ ] **Step 3: Implement pure state rules**

```python
def merge_draft(state: dict[str, object], decision: RouteDecision) -> dict[str, object]:
    merged = dict(state)
    if decision.services:
        merged.pop("slot", None)
        if len(decision.services) == 1:
            merged["service_query"] = decision.services[0]
            merged.pop("service_candidates", None)
        else:
            merged["service_candidates"] = list(decision.services)
            merged.pop("service_id", None)
    if decision.date is not None and decision.date != merged.get("date"):
        merged["date"] = decision.date
        merged.pop("slot", None)
    for key in ("time_from", "time_to", "staff"):
        value = getattr(decision, key)
        if value is not None and value != merged.get(key):
            merged[key] = value
            merged.pop("slot", None)
    return merged
```

Keep this module free of I/O and framework classes. `next_requirement` derives state instead of trusting persisted legacy `step`.

- [ ] **Step 4: Run GREEN and commit**

Run: `docker compose --env-file ../.env run --rm test pytest -q tests/unit/booking/test_conversation.py`

Expected: PASS.

```powershell
git add project/src/moroz/booking/conversation.py project/tests/unit/booking/test_conversation.py changelog.md
git commit -m "feat: добавить независимый черновик записи"
```

---

### Task 4: Replace the Telegram wizard with a thin conversational coordinator

**Files:**
- Modify: `project/src/moroz/booking/telegram.py`
- Modify: `project/tests/e2e/booking/test_semantic_booking.py`
- Modify: `project/tests/e2e/booking/test_telegram_booking.py`
- Delete: menu/catalog-only test modules or cases identified by `rg -n "catalog_|main_menu|persistent_menu|available_date|staff_page" project/tests` after equivalent safety assertions exist.

**Interfaces:**
- Consumes: Task 1 decision and Task 3 draft helpers.
- Produces: `TelegramBookingCoordinator.handle_semantic(customer_id: UUID, decision: RouteDecision, text: str) -> BookingReply | None`.
- Preserves: existing signed callback revision/ownership validation, contact validation and calls into `BookingService`.

- [ ] **Step 1: Add end-to-end RED cases for free ordering and safe callbacks**

```python
async def test_one_sentence_booking_offers_only_matching_slots(flow):
    reply = await flow.semantic("Запиши меня на криосауну 8 сентября после 18:00")
    assert reply.text_mentions("8 сентября", "после 18:00")
    assert 1 <= len(reply.inline_choices) <= 3
    assert all(choice.starts_at.hour >= 18 for choice in reply.inline_choices)

async def test_question_during_draft_answers_without_losing_state(flow):
    await flow.semantic("Хочу криосауну во вторник")
    reply = await flow.semantic("А сколько это стоит?")
    assert reply.contains_grounded_price
    assert flow.state["service_name"] == "Криосауна"
    assert flow.state["date"] == "2026-09-08"

async def test_stale_legacy_catalog_callback_never_mutates(flow):
    reply = await flow.callback("catalog_service:old")
    assert "напишите" in reply.text.lower()
    assert flow.provider.mutation_calls == []
```

- [ ] **Step 2: Run RED**

Run: `docker compose --env-file ../.env run --rm test pytest -q tests/e2e/booking/test_semantic_booking.py tests/e2e/booking/test_telegram_booking.py`

Expected: FAIL because the coordinator still enters service/staff/date wizard and ignores time windows.

- [ ] **Step 3: Delete menu/catalog navigation and wire the draft state machine**

Remove `persistent_menu_command`, `_MENU_BOOK`, `_MENU_LABELS`, catalog callbacks, category pagination, full service/staff/date button renderers and menu fallbacks. The coordinator resolves at most three catalog candidates, filters real slots, persists bounded `last_choices`, and returns typed replies:

```python
@dataclass(frozen=True, slots=True)
class BookingReply:
    text: str
    options: dict[str, object] = field(default_factory=dict)
    outcome: Literal["answer", "clarify", "choices", "contact", "confirm"] = "answer"
```

Only callbacks bound to customer/scenario/revision can select a short choice or confirm. Text can prepare a confirmation but cannot execute it. Legacy callback prefixes return `STALE_REPLY` plus `ReplyKeyboardRemove`, never resurrect state.

- [ ] **Step 4: Run GREEN and focused safety regression**

Run: `docker compose --env-file ../.env run --rm test pytest -q tests/e2e/booking/test_semantic_booking.py tests/e2e/booking/test_telegram_booking.py tests/unit/booking/test_service.py tests/integration/booking`

Expected: PASS; fake provider reports one mutation only after each explicit current confirmation.

- [ ] **Step 5: Confirm deletion and commit**

Run: `rg -n "catalog_category|catalog_service|catalog_book|persistent_menu_command|_MENU_BOOK|_MENU_LABELS" project/src project/worker`

Expected: no matches.

```powershell
git add -A project/src/moroz/booking project/tests/e2e/booking project/tests/unit/booking project/tests/integration/booking changelog.md
git commit -m "refactor: заменить анкету свободной записью"
```

---

### Task 5: One semantic text path through security and worker

**Files:**
- Modify: `project/src/moroz/security/pipeline.py`
- Modify: `project/worker/main.py`
- Modify: `project/tests/unit/security/test_semantic_dispatch.py`
- Modify: `project/tests/e2e/test_message_delivery.py`

**Interfaces:**
- Consumes: Task 1 router and Task 4 `handle_semantic`.
- Produces: one router call per allowed ordinary text batch; contact/callback remain deterministic.

- [ ] **Step 1: Add RED call-count and fallback tests**

```python
async def test_allowed_text_calls_router_once_and_keeps_multi_intent(runtime):
    result = await runtime.send("Сколько стоит криосауна и запишите во вторник после 18:00")
    assert runtime.router.calls == 1
    assert result.contains_grounded_price
    assert runtime.booking_draft["time_from"] == "18:00"

async def test_router_failure_has_no_menu_and_no_mutation(runtime):
    runtime.router.raise_invalid_json()
    result = await runtime.send("хочу записаться")
    assert "меню" not in result.text.lower()
    assert result.reply_markup.get("keyboard") is None
    assert runtime.provider.mutation_calls == []
```

- [ ] **Step 2: Run RED**

Run: `docker compose --env-file ../.env run --rm test pytest -q tests/unit/security/test_semantic_dispatch.py tests/e2e/test_message_delivery.py`

Expected: FAIL because worker still has menu splitting, pre/post coordinator dispatch and menu fallbacks.

- [ ] **Step 3: Remove duplicated orchestration**

Keep order `STOP/privacy/medical -> mask -> router -> typed dispatch -> outbound`. Delete imports/calls of `persistent_menu_command` and `main_menu_options`, menu boundary splitting and conditional second coordinator call. Replace fallback copy with:

```python
ROUTER_FALLBACK_REPLY = (
    "Я не совсем понял запрос. Напишите, пожалуйста, что хотите узнать или на какую услугу записаться. "
    "Если удобнее, я передам вопрос администратору."
)
```

- [ ] **Step 4: Run GREEN and commit**

Run: `docker compose --env-file ../.env run --rm test pytest -q tests/unit/security/test_semantic_dispatch.py tests/e2e/test_message_delivery.py tests/integration/test_stop_ordering.py tests/e2e/test_privacy_gate.py`

Expected: PASS.

```powershell
git add project/src/moroz/security/pipeline.py project/worker/main.py project/tests/unit/security/test_semantic_dispatch.py project/tests/e2e/test_message_delivery.py changelog.md
git commit -m "refactor: оставить один semantic text path"
```

---

### Task 6: Remove persistent Telegram keyboard

**Files:**
- Modify: `project/src/moroz/messaging/telegram.py`
- Modify: first-run/consent handlers found by `rg -n "main_menu_options|Готово|ReplyKeyboard" project/src project/bot project/worker`
- Modify: corresponding tests in `project/tests/e2e/test_privacy_gate.py` and `project/tests/unit/messaging/`.

**Interfaces:**
- Produces: `remove_keyboard_options() -> dict[str, object]` using Telegram `ReplyKeyboardRemove` payload.
- Removes: `main_menu_options()`.

- [ ] **Step 1: Add RED tests for `/start` and consent completion**

```python
async def test_repeat_start_removes_legacy_keyboard(app):
    reply = await app.start(consented=True)
    assert reply.reply_markup == {"remove_keyboard": True}
    assert "Например" in reply.text

async def test_consent_completion_invites_free_text(app):
    reply = await app.accept_processing_consent()
    assert reply.reply_markup == {"remove_keyboard": True}
    assert "запис" in reply.text.lower()
    assert "цен" in reply.text.lower()
```

- [ ] **Step 2: Run RED**

Run: `docker compose --env-file ../.env run --rm test pytest -q tests/e2e/test_privacy_gate.py tests/unit/messaging`

Expected: FAIL because persistent 2x2 keyboard is still emitted.

- [ ] **Step 3: Replace menu markup with removal and natural examples**

```python
def remove_keyboard_options() -> dict[str, object]:
    return {"reply_markup": {"remove_keyboard": True}}

WELCOME_TEXT = (
    "Здравствуйте! Напишите своими словами, чем помочь. "
    "Например: «Сколько стоит криосауна?» или «Запишите во вторник после 18:00»."
)
```

Retain generic inline keyboard and `request_contact` delivery. Do not retain menu labels as hidden commands.

- [ ] **Step 4: Run GREEN, scan and commit**

Run: `docker compose --env-file ../.env run --rm test pytest -q tests/e2e/test_privacy_gate.py tests/unit/messaging`

Run: `rg -n "main_menu_options|📅 Записаться|✨ Услуги и цены|📍 Адрес и режим|👩‍💼 Позвать администратора" project/src project/worker`

Expected: tests PASS; scan has no runtime matches.

```powershell
git add project/src/moroz/messaging project/src/moroz/consent project/bot project/worker project/tests changelog.md
git commit -m "refactor: убрать постоянное Telegram-меню"
```

---

### Task 7: Prompt, docs and deletion audit

**Files:**
- Modify: `project/llm/prompts/system.md`
- Modify: `ТЗ и архитектура.md`
- Modify: `docs/architecture/moroz-i-solntse-full-architecture.html`
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`
- Delete: remaining fixtures/helpers protecting only removed menu/catalog wizard.

**Interfaces:**
- Documents the runtime contract built in Tasks 1–6.

- [ ] **Step 1: Update prompt with explicit conversational boundaries**

Add these concrete rules without duplicating backend logic:

```markdown
- Отвечай на все совместимые намерения пользователя одним естественным сообщением.
- Не отправляй пользователя в меню или анкету: постоянного меню нет.
- Не спрашивай повторно услугу, дату, время или специалиста, если backend передал их как уже известные.
- Не обещай запись, перенос или отмену до backend-confirmation outcome.
- Если время выражено неточно («после работы», «вечером»), задай один короткий вопрос о границе времени.
```

- [ ] **Step 2: Update architecture sources and remove obsolete test code**

Document `security -> Router V3 -> consultation/booking draft -> BookingService confirmation`, contextual buttons, and no persistent menu. Use `git diff --stat <pre-cleanup-base>..HEAD -- project/src project/worker project/bot project/tests` to identify whether deleted UI branches outweigh additions; record exact counts in roadmap/changelog.

- [ ] **Step 3: Run documentation/source scans and commit**

Run: `rg -n "воспользуйтесь кнопками меню|откройте список кнопкой|persistent menu|catalog_category|catalog_service|catalog_book" project/src project/worker project/llm/prompts`

Expected: no stale runtime/prompt copy.

Run: `git diff --check`

Expected: exit 0.

```powershell
git add -A project/llm/prompts "ТЗ и архитектура.md" docs/architecture "Дорожная карта.md" changelog.md project/tests
git commit -m "docs: зафиксировать conversation-first архитектуру"
```

---

### Task 8: Full local release gate

**Files:**
- Modify only if a gate exposes a defect: the owning runtime/test file plus `changelog.md`.

**Interfaces:**
- Produces: one exact candidate SHA eligible for staging.

- [ ] **Step 1: Rebuild and run the full suite**

Run: `docker compose --env-file ../.env build test`

Run: `docker compose --env-file ../.env run --rm test pytest -q`

Expected: all tests PASS; no skipped critical Router V3 or booking mutation invariant.

- [ ] **Step 2: Run static/config/schema gates**

Run: `docker run --rm -v "${PWD}:/app" -w /app ghcr.io/astral-sh/ruff:0.12.7 check src tests admin bot worker scheduler`

Run: `docker compose --env-file ../.env run --rm test python -m compileall -q src tests admin bot worker scheduler`

Run: `docker compose --env-file ../.env config --quiet`

Run: `docker compose --env-file ../.env run --rm migrate alembic heads`

Expected: every command exits 0 and Alembic reports only `0026_router_v3 (head)`.

- [ ] **Step 3: Verify removal and preserved safety tests**

Run: `rg -n "main_menu_options|persistent_menu_command|catalog_category|catalog_service|catalog_book" project/src project/worker`

Expected: no matches.

Run: `docker compose --env-file ../.env run --rm test pytest -q tests/integration/booking tests/unit/booking/test_service.py tests/e2e/test_privacy_gate.py tests/integration/test_stop_ordering.py`

Expected: PASS.

- [ ] **Step 4: Record evidence and commit only necessary gate fixes/docs**

```powershell
git status --short
git diff --check
git log -1 --format=%H
```

Expected: clean worktree, diff-check exit 0, exact candidate SHA captured in changelog. Do not push.

---

### Task 9: Safe staging cutover and acceptance

**Files:**
- Read: `docs/superpowers/plans/2026-09-05-dialog-catalog-recovery.md` (последний подтверждённый exact bundle/immutable staging rollout).
- Create temporary evidence only under `tmp/staging-free-dialogue-<timestamp>/`.
- Modify after success: `Дорожная карта.md`, `changelog.md`.

**Interfaces:**
- Consumes: exact clean candidate SHA from Task 8.
- Produces: immutable staging image tag, rollback bundle and evidence; production remains untouched.

- [ ] **Step 1: Run read-only preflight without printing secrets**

Verify exact staging path/owner, `.env` mode `600`, disk, clean checkout, Docker availability, eight current services, current schema and webhook health. Query counts only:

```sql
SELECT status, count(*)
FROM booking_scenarios
WHERE status IN ('collecting', 'awaiting_confirmation', 'executing')
GROUP BY status;
```

Expected: `executing = 0`; otherwise stop before build/cutover and report blocker.

- [ ] **Step 2: Create rollback artifacts and deliver exact candidate**

Create protected server-side code/env/database/image manifests according to the runbook. Transfer a complete-history Git bundle, verify its SHA-256 server-side, fetch the exact candidate commit from the bundle, and build immutable `rc-<candidate-sha>` images. Do not use or modify GitHub credentials.

- [ ] **Step 3: Apply migration and close only legacy drafts atomically**

Run migration profile to `0026_router_v3`. First repeat the exact count predicate, then execute one transaction:

```sql
BEGIN;
SELECT count(*) AS target_count
FROM booking_scenarios
WHERE status IN ('collecting', 'awaiting_confirmation');

UPDATE booking_scenarios
SET status = 'failed', error_code = 'ui_migration', updated_at = now()
WHERE status IN ('collecting', 'awaiting_confirmation');
COMMIT;
```

Do not update confirmed/completed/executing scenarios or booking records.

- [ ] **Step 4: Cut over and run technical gates**

Recreate only services named by the runbook with the immutable tag. Verify 8/8 running+healthy, exact image IDs, schema `0026_router_v3`, HTTPS health/admin, unsigned webhook rejection, Telegram webhook `pending_update_count=0` and no last error, scheduler completion, fresh catalog, and zero critical/error/privacy/duplicate/mutation alarms in bounded logs.

- [ ] **Step 5: Run targeted Telegram acceptance without unapproved mutation**

Start with `/start` and verify the legacy keyboard disappears. Exercise text-only consultation, price + booking draft, exact `после 18:00`, correction, ambiguous service, question inside draft, router fallback and stale legacy callback. Verify no provider mutation occurs until an explicit current confirmation. Do not confirm a live create/reschedule/cancel unless it is the already-authorized targeted smoke with the owner's own contact and no active duplicate.

- [ ] **Step 6: Record exact rollout state and commit docs**

Record candidate SHA/tag, image IDs, rollback path, schema, health count, webhook result, safe-log result, draft migration count and manual acceptance outcome without credentials or PII.

```powershell
git add "Дорожная карта.md" changelog.md
git commit -m "docs: зафиксировать rollout свободного диалога"
```

Expected: staging runs the exact verified candidate; local docs contain evidence; production and GitHub remain unchanged.
