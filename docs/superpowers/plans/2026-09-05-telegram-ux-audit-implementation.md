# Telegram UX Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Убрать найденные UX-тупики Telegram-бота и сделать каталог, запись и связь с администратором короче и понятнее.

**Architecture:** Сохраняем существующие `TelegramBookingCoordinator`, каталог YCLIENTS и durable outbound. Новое поведение добавляется в общие функции координатора и Telegram delivery; LLM получает уточнённые инструкции, но цены и слоты остаются детерминированно привязаны к актуальному каталогу/адаптеру.

**Tech Stack:** Python 3.12, aiogram 3, asyncpg, pytest, Docker Compose.

## Global Constraints

- Запуск и тесты только через Docker Compose с `--env-file ../.env`.
- Не добавлять зависимости и миграции.
- Не создавать, переносить и отменять реальные записи во время проверки.
- Не менять production или staging в рамках локальной реализации.
- Цены берутся только из свежего каталога YCLIENTS; отсутствующие тарифы не достраиваются.
- Изменения вести в `codex/telegram-ux-audit-implementation`, каждый законченный блок коммитить отдельно и сразу фиксировать в `changelog.md`.

---

### Task 1: Однозначный выход из оформления

**Files:**
- Modify: `project/src/moroz/booking/telegram.py`
- Modify: `project/src/moroz/messaging/router.py`
- Test: `project/tests/e2e/booking/test_telegram_booking.py`
- Test: `project/tests/unit/messaging/test_router.py`

**Interfaces:**
- Consumes: `TelegramBookingCoordinator.handle(...)`, `RouteDecision(action="cancel_draft")`.
- Produces: текстовый shortcut `Отменить действие`, inline callback `cancel_draft`, ответ `Оформление остановлено. Новая запись не создана.`.

- [x] **Step 1: Write failing tests** for exact text cancellation and an inline exit button on collecting steps; assert no adapter mutations.
- [x] **Step 2: Run RED** with `docker compose --env-file ../.env run --rm test pytest tests/e2e/booking/test_telegram_booking.py tests/unit/messaging/test_router.py -q` and confirm the new assertions fail.
- [x] **Step 3: Implement minimal behavior** by deterministically routing exact exit labels, adding a `cancel_draft` callback action, and appending `Выйти из оформления` to collecting booking keyboards but not catalog or existing-booking confirmation.
- [x] **Step 4: Run GREEN** with the same command and confirm all selected tests pass.
- [x] **Step 5: Update `changelog.md` and commit** as `fix: понятный выход из оформления записи`.

### Task 2: Ограничение слотов «после HH:MM»

**Files:**
- Modify: `project/src/moroz/booking/telegram.py`
- Test: `project/tests/e2e/booking/test_semantic_booking.py`

**Interfaces:**
- Consumes: raw Telegram text and stored `available_slots`.
- Produces: scenario field `requested_time_after` (`HH:MM`), filtered slot choices, header suffix `после HH:MM`, explicit no-slot reply with buttons to show all times or return to date selection.

- [ ] **Step 1: Write failing tests** for `после 18:00`, repeated `только после 18`, persistence across service/date selection, and absence of unsuitable slot buttons.
- [ ] **Step 2: Run RED** with `docker compose --env-file ../.env run --rm test pytest tests/e2e/booking/test_semantic_booking.py -q`.
- [ ] **Step 3: Implement minimal parser and filter** in the coordinator using stdlib `re`/`datetime`; retain all YCLIENTS slots in state so a later change of the limit can refilter without a provider mutation.
- [ ] **Step 4: Run GREEN** and confirm the targeted file passes.
- [ ] **Step 5: Update `changelog.md` and commit** as `fix: учитывать желаемое время в Telegram-записи`.

### Task 3: Компактный каталог и явная навигация

**Files:**
- Modify: `project/src/moroz/booking/telegram.py`
- Test: `project/tests/e2e/booking/test_semantic_booking.py`

**Interfaces:**
- Consumes: existing category/family/service choices and callback revision.
- Produces: price-bearing compact button labels, two buttons per row where they fit, `← Категории`, `Ещё варианты →`, page indicator, `Адрес и маршрут` for walk-in detail.

- [ ] **Step 1: Write failing tests** asserting no duplicate textual tariff list for a walk-in family, compact price labels, category return, page indicator and walk-in address action.
- [ ] **Step 2: Run RED** for the catalog tests in `tests/e2e/booking/test_semantic_booking.py`.
- [ ] **Step 3: Implement minimal rendering** by reusing `_price_summary`, adding catalog-only row packing/navigation callbacks and keeping callback payloads under 64 bytes.
- [ ] **Step 4: Run GREEN**, including stale callback and numeric ordering tests.
- [ ] **Step 5: Update `changelog.md` and commit** as `feat: упростить Telegram-каталог услуг`.

### Task 4: Сравнение тарифов и безопасный подбор

**Files:**
- Modify: `project/src/moroz/booking/catalog.py`
- Modify: `project/llm/prompts/system.md`
- Test: `project/tests/unit/booking/test_catalog_matching.py`
- Test: `project/tests/e2e/test_catalog_message_flow.py`

**Interfaces:**
- Consumes: `CatalogGrounding.direct_reply`, свежие варианты одного семейства и consultation route.
- Produces: один ответ со всеми основными тарифами семейства; для запроса «не знаю, что выбрать» — короткое потребностное уточнение или 2–3 варианта только из grounded catalog facts.

- [ ] **Step 1: Write failing tests** for the exact audited phrases and ensure stale/missing catalog never exposes an old price.
- [ ] **Step 2: Run RED** with `docker compose --env-file ../.env run --rm test pytest tests/unit/booking/test_catalog_matching.py tests/e2e/test_catalog_message_flow.py -q`.
- [ ] **Step 3: Implement minimum grounding and prompt rules**: group same-family price variants, separate combo services, and instruct the assistant to answer need-first instead of requesting a known service name.
- [ ] **Step 4: Run GREEN** and verify existing exact-price/99-minute protections remain green.
- [ ] **Step 5: Update `changelog.md` and commit** as `feat: показывать варианты и цены без лишнего уточнения`.

### Task 5: Клиентские подписи и честные контакты

**Files:**
- Modify: `project/src/moroz/booking/telegram.py`
- Modify: `project/src/moroz/messaging/telegram.py`
- Modify: `project/src/moroz/messaging/router.py`
- Modify: `project/llm/prompts/system.md`
- Modify: `project/llm/config.py`
- Test: `project/tests/e2e/booking/test_telegram_booking.py`
- Test: `project/tests/e2e/test_message_delivery.py`
- Test: `project/tests/e2e/test_privacy_gate.py`

**Interfaces:**
- Consumes: service variants, main menu, outbound `delivery_options`.
- Produces: skipped redundant single-resource choice, menu label `Связаться с администратором`, concise default greeting, link previews disabled for ordinary bot answers, plain URL contact copy.

- [ ] **Step 1: Write failing tests** for automatic selection of a single variant, truthful contact label/copy, concise start fallback and `link_preview_options.is_disabled=True` delivery.
- [ ] **Step 2: Run RED** for the three focused files.
- [ ] **Step 3: Implement minimal copy and delivery changes**; accept the legacy administrator label as an alias so old persistent keyboards keep working.
- [ ] **Step 4: Run GREEN** and confirm consent HTML delivery remains unchanged.
- [ ] **Step 5: Update `changelog.md` and commit** as `feat: сделать подписи Telegram понятными клиенту`.

### Task 6: Combined verification and documentation

**Files:**
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`
- Modify: `docs/superpowers/plans/2026-09-05-telegram-ux-audit-implementation.md`

**Interfaces:**
- Consumes: all completed tasks.
- Produces: reproducible Docker evidence and exact local branch status.

- [ ] **Step 1: Run focused combined gate** for all modified tests in a freshly rebuilt Docker test image.
- [ ] **Step 2: Run broader booking/catalog/delivery regression**, Ruff, `compileall`, `docker compose config`, and `git diff --check`.
- [ ] **Step 3: Review every audit item** and document any deliberately unimplemented part with the concrete reason.
- [ ] **Step 4: Update roadmap, plan checkboxes and changelog**, then commit as `docs: завершить реализацию UX-аудита Telegram`.
- [ ] **Step 5: Do not push or deploy**; hand the local branch to the owner for review and selection of staging rollout.
