# Telegram Walk-in Services Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Показать солярий, коллариум и коллагенарий как три понятные позиции в Telegram-разделе `Записаться`, не создавая для них записи YCLIENTS.

**Architecture:** Один чистый классификатор в `yclients_catalog.py` распознаёт только три подтверждённых walk-in семейства и извлекает фактические минуты из названия. Catalog reader сохраняет эти позиции для цен, а Telegram coordinator сворачивает их в три кнопки и завершает walk-in сценарий до любого вызова booking adapter.

**Tech Stack:** Python 3.12, aiogram 3.x, asyncpg/PostgreSQL, pytest, Docker Compose.

## Global Constraints

- Проект и тесты запускаются только через Docker.
- YCLIENTS и клиентские данные не изменяются до отдельного разрешения mutation smoke.
- Солярий, коллариум и коллагенарий остаются в `Записаться`, но не вызывают staff/date/time/contact/create.
- Десятки минутных provider-позиций сворачиваются в три family-кнопки.
- Одна незавершённая цепочка на клиента; после завершения разрешены отдельные будущие записи.
- Новые таблицы, миграции, зависимости и настройки YCLIENTS не добавляются.

---

### Task 1: Нормализация walk-in услуг в каталоге

**Files:**
- Modify: `project/src/moroz/booking/yclients_catalog.py`
- Test: `project/tests/contract/booking/test_yclients_catalog.py`

**Interfaces:**
- Produces: `walk_in_family(service_name: str) -> str | None`
- Produces: `walk_in_minutes(service_name: str) -> int | None`
- Preserves: `CatalogRecord.duration_minutes: int` as a positive value.

- [x] **Step 1: Write the failing contract tests**

Add tests proving:

```python
@pytest.mark.asyncio
async def test_uses_title_minutes_for_confirmed_walk_in_service():
    fake = FakeHttp([
        response([staff()]),
        response({"services": [service(title="Солярий | 1 минута", seance_length=0)]}),
    ])
    snapshot = await YclientsCatalogReader(config(), http=fake).read(NOW)
    assert snapshot.records[0].duration_minutes == 1


@pytest.mark.asyncio
async def test_rejects_zero_duration_for_unknown_service():
    fake = FakeHttp([
        response([staff()]),
        response({"services": [service(title="Неизвестная услуга", seance_length=0)]}),
    ])
    with pytest.raises(YclientsCatalogError) as raised:
        await YclientsCatalogReader(config(), http=fake).read(NOW)
    assert raised.value.code == "yclients_catalog_response_shape"
```

Also parameterize case and separators for the three exact families.

- [x] **Step 2: Run RED**

Run:

```powershell
cd project
docker compose --env-file ../.env run --build --rm test pytest tests/contract/booking/test_yclients_catalog.py -q
```

Expected: new walk-in test fails because zero duration is rejected.

- [x] **Step 3: Implement the minimum parser**

In `yclients_catalog.py`, use stdlib `re` and normalized title prefixes:

```python
_WALK_IN_FAMILIES = {
    "солярий": "solarium",
    "коллариум": "collarium",
    "коллагенарий": "collagenarium",
}


def walk_in_family(service_name: str) -> str | None:
    normalized = service_name.strip().casefold()
    return next(
        (family for prefix, family in _WALK_IN_FAMILIES.items()
         if normalized.startswith(prefix)),
        None,
    )


def walk_in_minutes(service_name: str) -> int | None:
    if walk_in_family(service_name) is None:
        return None
    match = re.search(r"\b(\d{1,4})\s+минут", service_name.casefold())
    if match is None:
        return None
    value = int(match.group(1))
    return value if 1 <= value <= 1440 else None
```

In `_record`, use title minutes for recognized walk-in services; keep the existing strict `seance_length` path for every other service.

- [x] **Step 4: Run GREEN**

Run the same Docker test command. Expected: all catalog contract tests pass.

- [x] **Step 5: Commit**

```powershell
git add project/src/moroz/booking/yclients_catalog.py project/tests/contract/booking/test_yclients_catalog.py changelog.md
git commit -m "fix: поддержаны поминутные услуги YCLIENTS"
```

---

### Task 2: Три walk-in кнопки в Telegram

**Files:**
- Modify: `project/src/moroz/booking/telegram.py`
- Test: `project/tests/e2e/booking/test_telegram_booking.py`

**Interfaces:**
- Consumes: `walk_in_family(service_name: str) -> str | None`
- Produces: private `TelegramBookingCoordinator._service_choices(...)`.
- Walk-in choice shape: `{"walk_in": family, "label": public_label}`.

- [x] **Step 1: Write the failing E2E test**

Seed multiple minute variants from all three families plus `Криокапсула`. Assert:

```python
assert [choice["label"] for choice in scenario.state["choices"]] == [
    "Коллагенарий",
    "Коллариум",
    "Солярий",
    "Криокапсула",
]
```

Select each walk-in callback and assert the reply contains `предварительная запись не нужна` and `10:00 до 21:00`, scenario phase is terminal, and adapter counters remain `(0, 0, 0, 0)`.

- [x] **Step 2: Run RED**

Run:

```powershell
cd project
docker compose --env-file ../.env run --build --rm test pytest tests/e2e/booking/test_telegram_booking.py -q
```

Expected: minute variants appear separately and selecting one proceeds to staff.

- [x] **Step 3: Implement grouping and terminal reply**

Import `walk_in_family`. Build exactly one choice per family using labels:

```python
_WALK_IN_LABELS = {
    "solarium": "Солярий",
    "collarium": "Коллариум",
    "collagenarium": "Коллагенарий",
}
```

Regular services keep `_service_choice`. In `_choose_service`, before reading `variants`, handle `choice.get("walk_in")`: checkpoint the scenario with phase `failed`, error code `walk_in_no_booking`, event `booking_walk_in_selected`, return the no-booking/hours text, and make zero calls to `BookingPort`.

- [x] **Step 4: Run GREEN and regression**

```powershell
cd project
docker compose --env-file ../.env run --build --rm test pytest tests/e2e/booking/test_telegram_booking.py tests/unit/booking tests/contract/booking -q
```

Expected: all selected tests pass; ordinary create/reschedule/cancel behavior is unchanged.

- [x] **Step 5: Commit**

```powershell
git add project/src/moroz/booking/telegram.py project/tests/e2e/booking/test_telegram_booking.py changelog.md
git commit -m "feat: услуги без записи показаны в Telegram"
```

---

### Task 3: Verification and staging GET-only acceptance

**Files:**
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`

**Interfaces:**
- Consumes the existing Docker and staging rollout/runbook.
- Produces no YCLIENTS mutations.

- [ ] **Step 1: Run repository verification**

```powershell
cd project
docker compose --env-file ../.env run --build --rm test pytest tests/contract/booking/test_yclients_catalog.py tests/e2e/booking/test_telegram_booking.py tests/unit/booking -q
docker compose --env-file ../.env config --quiet
docker compose --env-file ../.env run --rm test python -m compileall -q src tests
```

Expected: zero failures and zero command errors.

- [ ] **Step 2: Build and deploy the exact candidate to staging**

Follow the existing commit-pinned staging runbook: save rollback evidence, build an immutable `rc-<commit>` image, deploy only the candidate services, and keep `TELEGRAM_YCLIENTS_BOOKING_ENABLED=false`.

Expected: 8/8 services healthy, exact image IDs, schema unchanged, HTTPS/admin/webhook green.

- [ ] **Step 3: Run GET-only catalog/projection acceptance**

Run the guarded GET-only readiness script and normal scheduler projection. Record only statuses, counts and safe booleans.

Expected: catalog sync no longer fails on `Солярий | 1 минута`; no POST/PUT/DELETE reaches YCLIENTS; safe-log counters stay zero.

- [ ] **Step 4: Update project records and commit**

Mark the walk-in roadmap item complete, preserve the production token-rotation blocker, append exact verification evidence to `changelog.md`, then:

```powershell
git add 'Дорожная карта.md' changelog.md
git commit -m "docs: подтверждены услуги без записи на staging"
```

Expected: clean worktree. Mutation smoke remains a separate explicit-consent gate.
