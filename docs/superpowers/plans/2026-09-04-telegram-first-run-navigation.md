# Telegram First-Run Navigation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Сделать первый запуск последовательным, добавить постоянное меню 2×2 и убрать тупик после выбора walk-in или устаревшей кнопки услуги.

**Architecture:** Webhook выбирает consent prompt или приветствие до durable ingress и прикладывает единый persistent reply markup. `TelegramBookingCoordinator` сохраняет walk-in сценарий на шаге услуги, восстанавливает собственный актуальный шаг при stale callback и использует постоянное меню как глобальную навигацию без изменения подтверждённых записей.

**Tech Stack:** Python 3.12, aiogram 3.x, FastAPI, asyncpg, PostgreSQL, Docker Compose, pytest.

## Global Constraints

- Все Python-тесты и compileall выполняются только через Docker Compose.
- Не сохранять и не передавать в LLM содержание сообщения до processing consent.
- Marketing consent остаётся необязательным и независимым.
- Не добавлять зависимости, сервисы, таблицы, миграции или Mini App.
- Сохранять ownership, deletion, idempotency и явное подтверждение YCLIENTS mutation.
- Production не затрагивать; rollout выполняется только на staging после полного gate.
- Source design: `docs/superpowers/specs/2026-09-04-telegram-first-run-navigation-design.md`.

---

### Task 1: Consent-first `/start` и постоянное меню

**Files:**
- Modify: `project/src/moroz/messaging/telegram.py`
- Modify: `project/llm/webhook.py`
- Test: `project/tests/e2e/test_privacy_gate.py`
- Test: `project/tests/e2e/test_message_delivery.py`

**Interfaces:**
- Produces: `main_menu_options() -> dict[str, object]`.
- Preserves: existing durable `send_static_reply` and consent transaction.

- [ ] **Step 1: Write failing privacy and markup tests**

Add tests proving:

```python
assert first_start.text == CONSENT_PROMPT_WITH_POLICY
assert "inline_keyboard" in first_start.reply_markup.model_dump()
assert await incoming_count(db, update_id) == 0

assert consent_done.text == START_REPLY
assert consent_done.reply_markup.model_dump()["keyboard"] == [
    [{"text": "📅 Записаться"}, {"text": "✨ Услуги и цены"}],
    [{"text": "📍 Адрес и режим"}, {"text": "👩‍💼 Позвать администратора"}],
]
assert consent_done.reply_markup.is_persistent is True
```

Also prove `/start` with existing processing consent returns `START_REPLY` with the same keyboard, while a pre-consent ordinary text still creates no inbox/message row.

- [ ] **Step 2: Run RED**

```powershell
docker compose --env-file ../.env run --rm test pytest -q `
  tests/e2e/test_privacy_gate.py tests/e2e/test_message_delivery.py `
  -k "start or consent_done or main_menu"
```

Expected: assertions fail because `/start` always sends `START_REPLY`, consent completion sends `CONSENT_THANKS`, and no persistent menu helper exists.

- [ ] **Step 3: Add the minimal shared menu helper**

In `moroz.messaging.telegram` return the exact JSON-compatible markup:

```python
def main_menu_options() -> dict[str, object]:
    return {
        "reply_markup": {
            "keyboard": [
                [{"text": "📅 Записаться"}, {"text": "✨ Услуги и цены"}],
                [{"text": "📍 Адрес и режим"}, {"text": "👩‍💼 Позвать администратора"}],
            ],
            "resize_keyboard": True,
            "is_persistent": True,
        }
    }
```

- [ ] **Step 4: Change webhook ordering**

For `/start`, check durable processing consent first. Send `_consent_prompt()` with the existing inline consent keyboard when absent; otherwise send `START_REPLY` with `main_menu_options()`.

For the first successful `consent:done`, enqueue `START_REPLY` with `main_menu_options()` instead of the generic `CONSENT_THANKS`. Preserve existing marketing status replies for later edits of an old consent card.

- [ ] **Step 5: Run GREEN and focused regression**

Run the RED command again, then:

```powershell
docker compose --env-file ../.env run --rm test pytest -q `
  tests/e2e/test_privacy_gate.py tests/e2e/test_message_delivery.py
```

Expected: all selected tests pass.

- [ ] **Step 6: Document and commit**

Update roadmap and changelog, then commit:

```powershell
git add -- project/src/moroz/messaging/telegram.py project/llm/webhook.py `
  project/tests/e2e/test_privacy_gate.py project/tests/e2e/test_message_delivery.py `
  'Дорожная карта.md' changelog.md
git commit -m "feat: согласие показано до приветствия"
```

---

### Task 2: Многоразовый выбор услуг и безопасное восстановление

**Files:**
- Modify: `project/src/moroz/booking/telegram.py`
- Modify: `project/worker/main.py`
- Test: `project/tests/e2e/booking/test_telegram_booking.py`
- Test: `project/tests/unit/test_worker.py`

**Interfaces:**
- Consumes: `main_menu_options()` from Task 1.
- Preserves: `TelegramBookingCoordinator.handle(...) -> BookingReply | None`.

- [ ] **Step 1: Write failing booking tests**

Add tests proving:

```python
first = await choose_walk_in("Солярий")
assert "предварительная запись не нужна" in first.text
assert callback_labels(first)  # актуальный список снова виден
assert (await active_scenario()).state["step"] == "service"

second = await press_original_service_button("Криокапсула")
assert second.text == "Выберите специалиста"
assert "неактуальна" not in second.text
```

Add one stale callback case with no active scenario that returns a fresh `Выберите услугу` list. Add worker/coordinator coverage that `📅 Записаться` restarts an unfinished flow and that another global menu label releases the unfinished flow to the ordinary router without mutation.

- [ ] **Step 2: Run RED**

```powershell
docker compose --env-file ../.env run --rm test pytest -q `
  tests/e2e/booking/test_telegram_booking.py tests/unit/test_worker.py `
  -k "walk_in or stale or main_menu"
```

Expected: current walk-in scenario is `failed`, original sibling callback returns `STALE_REPLY`, and global menu is swallowed by the active booking flow.

- [ ] **Step 3: Keep walk-in selection active**

Checkpoint a `booking_walk_in_selected` event without changing `phase` or `step`, and return the walk-in explanation with `_choice_options(scenario, "service")`. Remove the instruction to press `Записаться` again.

- [ ] **Step 4: Recover stale callbacks and global navigation**

Pass `connection` and `update_id` into `_handle_callback`. If parsing, ownership or current-step validation fails, call a small recovery path that renders the owner’s active current step with its buttons or starts a fresh service list when no active flow exists.

Recognize the four exact menu labels before step-specific text handling. `📅 Записаться` closes only an unfinished scenario and calls `_start`; the other three labels close the unfinished scenario with `menu_navigation` and return `None` so the existing worker router handles them.

- [ ] **Step 5: Restore the main menu after temporary keyboards**

Use `main_menu_options()` on cancellation and terminal booking replies that previously sent `remove_keyboard`. Keep inline confirmation buttons unchanged.

- [ ] **Step 6: Run GREEN and booking regression**

Run the RED command again, then:

```powershell
docker compose --env-file ../.env run --rm test pytest -q `
  tests/e2e/booking/test_telegram_booking.py `
  tests/e2e/test_message_delivery.py `
  tests/unit/test_worker.py
```

Expected: all selected tests pass and mutation call counts are unchanged.

- [ ] **Step 7: Document and commit**

Update roadmap and changelog, then commit:

```powershell
git add -- project/src/moroz/booking/telegram.py project/worker/main.py `
  project/tests/e2e/booking/test_telegram_booking.py project/tests/unit/test_worker.py `
  'Дорожная карта.md' changelog.md
git commit -m "fix: кнопки записи остаются полезными"
```

---

### Task 3: Release gate и staging QA

**Files:**
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`
- Create: `tmp/manual-test-20260904-telegram-navigation/Отчет по тестированию бота.md`

**Interfaces:**
- Verifies: exact committed candidate only.
- Preserves: server-only credentials and staging-only rollout.

- [ ] **Step 1: Run full local Docker gate**

```powershell
docker compose --env-file ../.env run --rm test pytest -q
docker compose --env-file ../.env run --rm test python -m compileall -q src tests
docker compose --env-file ../.env config -q
git diff --check
```

Expected: zero failures and exit `0` for every command.

- [ ] **Step 2: Review exact diff**

Compare the implementation base with `HEAD`, verify every design requirement, and resolve every Critical or Important finding before rollout.

- [ ] **Step 3: Commit final durable documentation and push `main`**

Record exact test counts and commit SHA in roadmap/changelog. Push only the reviewed commits with ordinary non-force `git push origin main`.

- [ ] **Step 4: Deploy exact commit to staging**

Follow `project/ops/staging-runbook.md`: preserve rollback artifacts, build immutable `rc-<full-sha>` images, run migration/compatibility gates, cut over only bot/worker components that need the code, and verify 8/8 services, schema, HTTPS, webhook and safe logs. Do not touch production.

- [ ] **Step 5: Run targeted manual Telegram QA**

Using Telegram Web, verify:

1. fresh `/start` shows consent first;
2. `Готово` shows welcome and the 2×2 menu;
3. `📅 Записаться` opens services;
4. `Солярий`, then another original service button, both work;
5. repeated `📅 Записаться` restores a fresh list;
6. `✨ Услуги и цены`, `📍 Адрес и режим`, and `👩‍💼 Позвать администратора` reach the intended routes;
7. bot is left unpaused, message order is visible in admin, and no fresh error logs appear.

Write evidence to the ignored `tmp/` report and log only a safe summary in `changelog.md`.

- [ ] **Step 6: Final verification**

Re-run the focused Docker tests after any review or QA fix, verify staging image IDs match the exact candidate, and update the roadmap task from `В работе` to the evidence-backed result.
