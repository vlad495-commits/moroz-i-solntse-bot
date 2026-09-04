# Late Marketing Consent Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Сделать старую Telegram consent-карточку рабочим редактором постоянного processing/marketing consent без молчания и ложных галочек.

**Architecture:** Временный Redis-state будет отличать отсутствие сессии редактирования от намеренно пустого набора и при первом действии инициализироваться из PostgreSQL. Существующий webhook под customer advisory lock применит только фактическое изменение marketing consent через уже готовые grant/revoke функции, очистит state и поставит один идемпотентный ответ в существующий outbox.

**Tech Stack:** Python 3.12, FastAPI, aiogram 3, asyncpg, Redis, pytest/pytest-asyncio, Docker Compose.

## Global Constraints

- Кодовая база: exact fetched `origin/main@85140cf4d1dc13d4b6d877fbc7d369f580519d1f`.
- Ветка: `codex/fix-late-marketing-consent`; worktree: `.worktrees/fix-late-marketing-consent`.
- Запуск и тесты только через Docker Compose; использовать внешний root `.env`, не копировать его в worktree.
- Не добавлять зависимости, миграции, таблицы, команды или новую consent version.
- Не менять `/marketing`, Reactivation V2, YCLIENTS, eligibility, scheduler и рекламную доставку.
- Сохранить customer deletion guard, advisory lock, explicit proof и fail-closed поведение.
- Все `consent:done` ответы используют существующий idempotency key `telegram:consent_thanks:{update_id}`.
- Не трогать пользовательский `docs/project/LOCAL_ADMIN_START.md`.

---

### Task 1: Изолированный baseline от origin/main

**Files:**
- No code changes.

**Interfaces:**
- Consumes: `origin/main@85140cf4d1dc13d4b6d877fbc7d369f580519d1f`.
- Produces: чистый worktree на `codex/fix-late-marketing-consent` и зелёный baseline consent E2E.

- [ ] **Step 1: Повторно проверить remote и создать worktree**

```powershell
git fetch origin --prune
git rev-parse origin/main
git check-ignore -q .worktrees
git worktree add .worktrees/fix-late-marketing-consent -b codex/fix-late-marketing-consent origin/main
```

Expected: SHA ровно `85140cf4d1dc13d4b6d877fbc7d369f580519d1f`; `.worktrees` ignored; новая ветка создана без изменения root `main`.

- [ ] **Step 2: Подтвердить чистоту и runtime-базу**

```powershell
git -C .worktrees/fix-late-marketing-consent status --short --branch
git -C .worktrees/fix-late-marketing-consent diff --quiet origin/main -- project
```

Expected: clean branch; runtime diff отсутствует.

- [ ] **Step 3: Запустить focused baseline в отдельном Compose namespace**

Run from `.worktrees/fix-late-marketing-consent/project`:

```powershell
docker compose --env-file ../.env -p moroz-consent-fix --profile test run --rm --build test pytest -q tests/e2e/test_privacy_gate.py::test_duplicate_consent_checkbox_callback_is_idempotent tests/e2e/test_privacy_gate.py::test_duplicate_consent_done_callback_is_idempotent tests/e2e/test_privacy_gate.py::test_ads_checkbox_grants_proven_marketing_consent tests/e2e/test_privacy_gate.py::test_marketing_command_and_callbacks_are_explicit_and_idempotent
```

Expected: `4 passed`.

---

### Task 2: RED — поздние opt-in и opt-out на старой карточке

**Files:**
- Modify: `project/tests/e2e/test_privacy_gate.py:15-55`
- Modify: `project/tests/e2e/test_privacy_gate.py:619`

**Interfaces:**
- Consumes: `telegram_consent_callback(...)`, `grant_policy_consent(...)`, `FakeTelegram.edited_reply_markups`, Redis fixture и PostgreSQL fixture.
- Produces: два E2E-контракта `test_old_consent_card_can_grant_marketing_later` и `test_old_consent_card_can_revoke_marketing_later`.

- [ ] **Step 1: Добавить тестовые константы и импорт ответа**

В import из `config` добавить `MARKETING_ENABLED_REPLY`. Рядом с callback constants добавить:

```python
CONSENT_ADS_CLEAR_CALLBACK_DATA = "consent:set:ads:off"
```

- [ ] **Step 2: Добавить падающий тест позднего opt-in**

```python
async def test_old_consent_card_can_grant_marketing_later(
    client, db, redis_client, fake_telegram
):
    await grant_policy_consent(client, update_id=140)
    assert await db.fetchval("SELECT count(*) FROM marketing_consents") == 0

    assert (
        await client.post(
            "/telegram/webhook",
            json=telegram_consent_callback(
                update_id=142,
                data=CONSENT_ADS_CALLBACK_DATA,
                callback_id="callback-late-ads",
            ),
        )
    ).status_code == 200
    keyboard = fake_telegram.edited_reply_markups[-1][
        "reply_markup"
    ].inline_keyboard
    assert [(row[0].text, row[0].callback_data) for row in keyboard] == [
        (f"☑ {CONSENT_PII_LABEL}", CONSENT_PII_CLEAR_CALLBACK_DATA),
        (f"☑ {CONSENT_ADS_LABEL}", CONSENT_ADS_CLEAR_CALLBACK_DATA),
        (CONSENT_DONE_LABEL, CONSENT_DONE_CALLBACK_DATA),
    ]

    done = telegram_consent_callback(
        update_id=143,
        callback_id="callback-late-done",
    )
    assert (await client.post("/telegram/webhook", json=done)).status_code == 200
    assert (await client.post("/telegram/webhook", json=done)).status_code == 200

    state = await db.fetchrow(
        """
        SELECT consent.active, consent.proof_text_hash,
               event.source_event_id
        FROM marketing_consents AS consent
        JOIN marketing_consent_events AS event
          ON event.id = consent.proof_event_id
        """
    )
    assert tuple(state.values()) == (
        True,
        sha256(MARKETING_CONSENT_CLAUSE.encode()).hexdigest(),
        "143",
    )
    assert await redis_client.get("consent:state:telegram:42:7") is None
    assert [message["text"] for message in fake_telegram.sent_messages] == [
        CONSENT_THANKS,
        MARKETING_ENABLED_REPLY,
    ]
    assert await db.fetchval(
        "SELECT count(*) FROM marketing_consent_events"
    ) == 1
```

- [ ] **Step 3: Добавить падающий тест позднего opt-out**

```python
async def test_old_consent_card_can_revoke_marketing_later(
    client, db, redis_client, fake_telegram
):
    for update_id, data in (
        (150, CONSENT_PII_CALLBACK_DATA),
        (151, CONSENT_ADS_CALLBACK_DATA),
        (152, CONSENT_DONE_CALLBACK_DATA),
    ):
        assert (
            await client.post(
                "/telegram/webhook",
                json=telegram_consent_callback(update_id=update_id, data=data),
            )
        ).status_code == 200

    assert (
        await client.post(
            "/telegram/webhook",
            json=telegram_consent_callback(
                update_id=153,
                data=CONSENT_ADS_CLEAR_CALLBACK_DATA,
            ),
        )
    ).status_code == 200
    keyboard = fake_telegram.edited_reply_markups[-1][
        "reply_markup"
    ].inline_keyboard
    assert [(row[0].text, row[0].callback_data) for row in keyboard] == [
        (f"☑ {CONSENT_PII_LABEL}", CONSENT_PII_CLEAR_CALLBACK_DATA),
        (f"☐ {CONSENT_ADS_LABEL}", CONSENT_ADS_CALLBACK_DATA),
        (CONSENT_DONE_LABEL, CONSENT_DONE_CALLBACK_DATA),
    ]

    assert (
        await client.post(
            "/telegram/webhook",
            json=telegram_consent_callback(update_id=154),
        )
    ).status_code == 200
    consent = await db.fetchrow(
        "SELECT active, suppression_reason FROM marketing_consents"
    )
    assert tuple(consent.values()) == (False, "user_stop")
    assert await redis_client.get("consent:state:telegram:42:7") is None
    assert [message["text"] for message in fake_telegram.sent_messages] == [
        CONSENT_THANKS,
        MARKETING_DISABLED_REPLY,
    ]
```

- [ ] **Step 4: Запустить RED**

Run from worktree `project`:

```powershell
docker compose --env-file ../.env -p moroz-consent-fix --profile test run --rm --build test pytest -q tests/e2e/test_privacy_gate.py::test_old_consent_card_can_grant_marketing_later tests/e2e/test_privacy_gate.py::test_old_consent_card_can_revoke_marketing_later
```

Expected: оба теста FAIL на старом поведении: policy не восстанавливается из durable state, поздний marketing grant/revoke и второй ответ отсутствуют.

- [ ] **Step 5: Commit RED tests**

```powershell
git add project/tests/e2e/test_privacy_gate.py
git commit -m "test: воспроизведен поздний marketing consent"
```

---

### Task 3: GREEN — гидратация durable state и применение marketing consent

**Files:**
- Modify: `project/llm/webhook.py:279-290`
- Modify: `project/llm/webhook.py:454-557`
- Test: `project/tests/e2e/test_privacy_gate.py`

**Interfaces:**
- Consumes: `ConsentService`, `grant_explicit_marketing`, `opt_out_marketing`, `ReactivationRepository.record_inbound`, `MessageRepository.enqueue_outbound_in_transaction`.
- Produces: `consent_checked(chat_id, user_id) -> set[str] | None` и `durable_consent_state(connection, user_id) -> tuple[bool, bool]`.

- [ ] **Step 1: Научить Redis-loader различать отсутствующий и пустой state**

```python
async def consent_checked(chat_id: int, user_id: int) -> set[str] | None:
    raw = await webhook_app.state.redis.get(
        _consent_state_key(chat_id, user_id)
    )
    if raw is None:
        return None
    return {item for item in raw.split(",") if item}
```

- [ ] **Step 2: Добавить один локальный durable-state helper**

```python
async def durable_consent_state(
    connection, user_id: str
) -> tuple[bool, bool]:
    row = await connection.fetchrow(
        """
        SELECT
            EXISTS (
                SELECT 1 FROM processing_consents
                WHERE channel = 'telegram'
                  AND user_id = $1
                  AND consent_version = $2
            ) AS processing_active,
            COALESCE((
                SELECT active
                       AND proof_event_id IS NOT NULL
                       AND proof_text_hash IS NOT NULL
                       AND suppressed_at IS NULL
                FROM marketing_consents
                WHERE channel = 'telegram' AND user_id = $1
            ), false) AS marketing_active
        """,
        user_id,
        PROCESSING_CONSENT_VERSION,
    )
    return bool(row["processing_active"]), bool(row["marketing_active"])
```

- [ ] **Step 3: Инициализировать старые checkbox-кнопки из durable state**

В `_CONSENT_CALLBACK_TARGETS` ветке под существующим lock заменить получение/переключение `checked` на:

```python
stored_checked = await consent_checked(
    callback.message.chat.id,
    callback.from_user.id,
)
processing_active, marketing_active = await durable_consent_state(
    connection, str(callback.from_user.id)
)
state_was_missing = stored_checked is None
if stored_checked is None:
    checked = set()
    if processing_active:
        checked.add("pii")
    if marketing_active:
        checked.add("ads")
else:
    checked = stored_checked

before = set(checked)
if processing_active:
    checked.add("pii")
if not (kind == "pii" and processing_active):
    if enabled:
        checked.add(kind)
    else:
        checked.discard(kind)
if checked == before and not state_was_missing:
    return Response(status_code=200)
await save_consent_checked(
    callback.message.chat.id,
    callback.from_user.id,
    checked,
)
```

Существующий `edit_message_reply_markup(..., _consent_keyboard(checked))` оставить после транзакции.

- [ ] **Step 4: Заменить ранний return `consent:done` на reconcile постоянного состояния**

Сохранить существующие lock/deletion/outbox границы. Внутри транзакции:

```python
stored_checked = await consent_checked(
    callback.message.chat.id,
    callback.from_user.id,
)
processing_active, marketing_active = await durable_consent_state(
    connection, str(callback.from_user.id)
)
if stored_checked is None:
    checked = set()
    if processing_active:
        checked.add("pii")
    if marketing_active:
        checked.add("ads")
else:
    checked = stored_checked
if processing_active:
    checked.add("pii")

if not processing_active and "pii" not in checked:
    needs_pii = True
else:
    created_processing = not processing_active
    if created_processing:
        await webhook_app.state.consent_service.grant_processing_consent(
            "telegram",
            str(callback.from_user.id),
            PROCESSING_CONSENT_VERSION,
            connection=connection,
        )

    wants_marketing = "ads" in checked
    event = {
        "user_id": str(callback.from_user.id),
        "source_event_id": str(update.update_id),
        "occurred_at": received_at,
    }
    if wants_marketing and not marketing_active:
        await grant_explicit_marketing(connection, **event)
    elif marketing_active and not wants_marketing:
        await webhook_app.state.reactivation_repository.record_inbound(
            "telegram",
            str(callback.from_user.id),
            received_at,
            "marketing_disable",
            connection=connection,
        )
        await opt_out_marketing(connection, **event)

    await webhook_app.state.redis.delete(
        _consent_state_key(
            callback.message.chat.id,
            callback.from_user.id,
        )
    )
    if created_processing:
        reply = CONSENT_THANKS
    elif wants_marketing:
        reply = MARKETING_ENABLED_REPLY
    else:
        reply = MARKETING_DISABLED_REPLY
    outbound_id = await webhook_app.state.message_repository.enqueue_outbound_in_transaction(
        connection,
        channel="telegram",
        chat_id=str(callback.message.chat.id),
        text=reply,
        idempotency_key=f"telegram:consent_thanks:{update.update_id}",
        delivery_options=None,
    )
```

Инициализировать перед транзакцией:

```python
needs_pii = False
outbound_id = None
```

После транзакции сохранить один ответ:

```python
if needs_pii:
    await send_static_reply(
        update_id=update.update_id,
        chat_id=callback.message.chat.id,
        text=CONSENT_NEED_PII_REPLY,
        reply_kind="consent_need_pii",
    )
elif outbound_id is not None:
    await deliver_static_reply(outbound_id)
```

- [ ] **Step 5: Запустить GREEN двух новых тестов**

```powershell
docker compose --env-file ../.env -p moroz-consent-fix --profile test run --rm --build test pytest -q tests/e2e/test_privacy_gate.py::test_old_consent_card_can_grant_marketing_later tests/e2e/test_privacy_gate.py::test_old_consent_card_can_revoke_marketing_later
```

Expected: `2 passed`.

- [ ] **Step 6: Запустить весь consent/privacy E2E-файл**

```powershell
docker compose --env-file ../.env -p moroz-consent-fix --profile test run --rm --build test pytest -q tests/e2e/test_privacy_gate.py
```

Expected: все тесты файла PASS, включая duplicate callback, policy-only, clean ads opt-in, `/marketing`, deletion и stale-consent upgrade.

- [ ] **Step 7: Commit GREEN**

```powershell
git add project/llm/webhook.py
git commit -m "fix: сохранен поздний marketing consent"
```

---

### Task 4: Verification, merge gate и staging handoff

**Files:**
- Modify in root main: `changelog.md`
- Modify in root main: `Дорожная карта.md`
- No additional runtime files.

**Interfaces:**
- Consumes: feature commits из Tasks 2-3.
- Produces: проверенный feature HEAD, готовый к merge в локальный `main`; push/deploy остаются отдельным внешним шагом.

- [ ] **Step 1: Запустить security/marketing regression**

```powershell
docker compose --env-file ../.env -p moroz-consent-fix --profile test run --rm --build test pytest -q tests/unit/security/test_marketing_consent.py tests/integration/reactivation/test_marketing_consent.py tests/e2e/test_privacy_gate.py tests/e2e/reactivation/test_client_flow.py
```

Expected: все выбранные тесты PASS.

- [ ] **Step 2: Запустить статические Docker-gates**

```powershell
docker compose --env-file ../.env -p moroz-consent-fix run --rm --build test python -m compileall -q llm src
docker compose --env-file ../.env -p moroz-consent-fix config --quiet
git diff --check origin/main...HEAD
```

Expected: три команды exit `0`.

- [ ] **Step 3: Зафиксировать evidence в root docs**

В root `changelog.md` записать RED failure, GREEN counts, точный feature SHA и отсутствие staging/production изменений. В `Дорожная карта.md` отметить code-fix выполненным, но human staging retest оставить открытым до rollout.

- [ ] **Step 4: Проверить feature diff**

```powershell
git diff --stat origin/main...codex/fix-late-marketing-consent
git diff --check origin/main...codex/fix-late-marketing-consent
```

Expected: runtime diff ограничен `project/llm/webhook.py` и `project/tests/e2e/test_privacy_gate.py`; diff-check чистый.

- [ ] **Step 5: Merge только после зелёного gate**

```powershell
git checkout main
git merge --no-ff codex/fix-late-marketing-consent -m "merge: исправлен поздний marketing consent"
```

Expected: локальные QA/docs-коммиты сохранены, пользовательский untracked файл не включён, merge не содержит миграций.

- [ ] **Step 6: Остановиться перед внешними изменениями**

Не выполнять `git push` и staging rollout без подтверждённого release-шагa. Передать владельцу локальный merge SHA, Docker evidence и ручной сценарий:

```text
policy only → Готово → старая карточка → ads on → Готово
```

Expected after rollout: policy остаётся checked, marketing consent/proof создаётся, бот отвечает «Рекламные сообщения включены.».
