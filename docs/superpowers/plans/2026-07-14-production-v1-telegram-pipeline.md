# Reliable Telegram Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Провести текстовый Telegram update через privacy gate, пятисекундный буфер, устойчивые inbox/outbox, RabbitMQ и идемпотентную отправку ответа.

**Architecture:** Bot webhook только принимает и фиксирует сообщения; worker выполняет обработку; PostgreSQL хранит inbox/outbox, Redis склеивает быстрые сообщения, Telegram adapter отправляет сохраненный ответ.

**Tech Stack:** aiogram 3.x, FastAPI/uvicorn, asyncpg, Redis, aio-pika, PostgreSQL, pytest.

## Global Constraints

- Контент до согласия не сохранять.
- Telegram `update_id` и исходящее сообщение имеют уникальные ключи.
- Неопределенный результат отправки не повторять вслепую.
- Нетекстовый ввод возвращает утвержденный шаблон.

---

### Task 1: IncomingMessage, inbox/outbox и идемпотентность

**Files:**
- Create: `project/src/moroz/messaging/models.py`
- Create: `project/src/moroz/messaging/repository.py`
- Create: `project/migrations/versions/0002_messaging_inbox_outbox.py`
- Test: `project/tests/integration/messaging/test_repository.py`

**Interfaces:**
- Produces: `IncomingMessage`, `ScenarioResult`, `MessageRepository.accept() -> bool`, `enqueue_outbound() -> UUID`.

- [ ] **Step 1: Write failing duplicate test**

```python
async def test_accept_same_message_once(message_repo, incoming_message):
    assert await message_repo.accept(incoming_message) is True
    assert await message_repo.accept(incoming_message) is False
```

- [ ] **Step 2: Run red**

Run: `docker compose --env-file ../.env --profile test run --rm test pytest tests/integration/messaging/test_repository.py -q`

Expected: FAIL because migration/repository are absent.

- [ ] **Step 3: Implement tables and repository**

```sql
CREATE TABLE message_inbox (
  id UUID PRIMARY KEY,
  channel TEXT NOT NULL,
  external_message_id TEXT NOT NULL,
  chat_id TEXT NOT NULL,
  payload JSONB NOT NULL,
  status TEXT NOT NULL DEFAULT 'accepted',
  correlation_id UUID NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(channel, external_message_id)
);
CREATE TABLE outbound_messages (
  id UUID PRIMARY KEY,
  channel TEXT NOT NULL,
  chat_id TEXT NOT NULL,
  text TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'pending',
  external_message_id TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Use `INSERT ... ON CONFLICT DO NOTHING RETURNING id` in `accept`.

- [ ] **Step 4: Run migration and test**

Run: `docker compose --env-file ../.env --profile migration run --rm migrate && docker compose --env-file ../.env --profile test run --rm test pytest tests/integration/messaging/test_repository.py -q`

Expected: tests pass.

- [ ] **Step 5: Commit**

```bash
git add project/src/moroz/messaging project/migrations/versions/0002_messaging_inbox_outbox.py project/tests/integration/messaging
git commit -m "feat: добавлены inbox и outbox сообщений"
```

### Task 2: Privacy gate и Telegram webhook

**Files:**
- Create: `project/src/moroz/security/consent.py`
- Create: `project/llm/webhook.py`
- Test: `project/tests/e2e/test_privacy_gate.py`
- Modify: `project/llm/Dockerfile`
- Modify: `project/docker-compose.yml`

**Interfaces:**
- Produces: `ConsentService.has_processing_consent(channel, user_id) -> bool`.
- Consumes: `MessageRepository.accept` only after consent.

- [ ] **Step 1: Write failing no-consent test**

```python
async def test_message_without_consent_is_not_persisted(client, db):
    response = await client.post("/telegram/webhook", json=telegram_text_update("Секретный текст"))
    assert response.status_code == 200
    assert await db.fetchval("SELECT count(*) FROM message_inbox") == 0
    assert fake_telegram.last_text == "Чтобы продолжить, подтвердите обработку данных."
```

- [ ] **Step 2: Run red**

Run: `docker compose --env-file ../.env --profile test run --rm test pytest tests/e2e/test_privacy_gate.py -q`

Expected: FAIL because webhook/consent service are absent.

- [ ] **Step 3: Implement webhook gate**

```python
@app.post("/telegram/webhook")
async def telegram_webhook(update: dict) -> Response:
    envelope = normalize_update(update)
    if not await consent_service.has_processing_consent("telegram", envelope.user_id):
        await telegram.send_consent_prompt(envelope.chat_id)
        return Response(status_code=200)
    await message_service.accept(envelope)
    return Response(status_code=200)
```

Handle consent callback separately and store consent version/time. Switch bot command to `uvicorn webhook:app`; keep polling only behind `TELEGRAM_MODE=polling` for local emergency use, default webhook in production.

- [ ] **Step 4: Run E2E test**

Run: `docker compose --env-file ../.env --profile test run --rm test pytest tests/e2e/test_privacy_gate.py -q`

Expected: no-consent content absent; consent callback persists only the consent record.

- [ ] **Step 5: Commit**

```bash
git add project/src/moroz/security/consent.py project/llm/webhook.py project/tests/e2e/test_privacy_gate.py project/llm/Dockerfile project/docker-compose.yml
git commit -m "feat: добавлен Telegram webhook и privacy gate"
```

### Task 3: Redis buffer и durable enqueue

**Files:**
- Create: `project/src/moroz/messaging/buffer.py`
- Create: `project/src/moroz/messaging/service.py`
- Test: `project/tests/integration/messaging/test_buffer.py`

**Interfaces:**
- Produces: `MessageBuffer.append(chat_id, message_id, text) -> None`.
- Produces: `MessageBuffer.flush(chat_id) -> BufferedMessage | None`.
- Consumes: `QueuePort.publish(task: QueueTask) -> None` through the transactional outbox relay.

- [ ] **Step 1: Write failing virtual-time test**

```python
async def test_buffer_joins_fast_messages(buffer, clock):
    await buffer.append("42", "1", "Хочу")
    clock.advance(seconds=2)
    await buffer.append("42", "2", "на крио")
    clock.advance(seconds=5)
    assert (await buffer.flush("42")).text == "Хочу\nна крио"
```

- [ ] **Step 2: Run red**

Run: `docker compose --env-file ../.env --profile test run --rm test pytest tests/integration/messaging/test_buffer.py -q`

Expected: FAIL because buffer is absent.

- [ ] **Step 3: Implement Redis list + deadline**

```python
async def append(self, chat_id: str, message_id: str, text: str) -> None:
    key = f"buffer:{chat_id}"
    async with self.redis.pipeline(transaction=True) as pipe:
        pipe.rpush(key, json.dumps({"id": message_id, "text": text}))
        pipe.expire(key, 30)
        pipe.set(f"{key}:deadline", self.clock.now().timestamp() + 5, ex=30)
        await pipe.execute()
```

On flush, acquire `lock:buffer:{chat_id}`, atomically read/delete list, persist inbox and transactional outbox row, then publish via outbox relay.

- [ ] **Step 4: Run test including concurrent flush**

Run: `docker compose --env-file ../.env --profile test run --rm test pytest tests/integration/messaging/test_buffer.py -q`

Expected: joined text once; second concurrent flush returns `None`.

- [ ] **Step 5: Commit**

```bash
git add project/src/moroz/messaging/buffer.py project/src/moroz/messaging/service.py project/tests/integration/messaging/test_buffer.py
git commit -m "feat: добавлен буфер быстрых Telegram-сообщений"
```

### Task 4: Worker, outbox relay и идемпотентная отправка

**Files:**
- Create: `project/src/moroz/messaging/outbox.py`
- Create: `project/src/moroz/messaging/telegram.py`
- Test: `project/tests/e2e/test_message_delivery.py`
- Modify: `project/worker/main.py`

**Interfaces:**
- Produces: `OutboxRelay.publish_pending(limit: int = 100) -> int`.
- Produces: `TelegramSender.send(outbound_id: UUID) -> DeliveryResult`.

- [ ] **Step 1: Write failing duplicate-delivery test**

```python
async def test_worker_does_not_send_sent_outbound_twice(outbound_repo, worker, fake_telegram):
    outbound_id = await outbound_repo.create("42", "Ответ", "reply:inbox-1")
    await worker.handle_send(outbound_id)
    await worker.handle_send(outbound_id)
    assert fake_telegram.sent == [("42", "Ответ")]
```

- [ ] **Step 2: Run red**

Run: `docker compose --env-file ../.env --profile test run --rm test pytest tests/e2e/test_message_delivery.py -q`

Expected: FAIL because sender/handler are absent.

- [ ] **Step 3: Implement claim/send/finalize**

```python
row = await repo.claim(outbound_id)  # pending -> sending, SELECT FOR UPDATE SKIP LOCKED
if row is None or row.status == "sent":
    return
result = await telegram.send_message(row.chat_id, row.text)
await repo.mark_sent(row.id, result.message_id)
```

If network result is unknown, set `delivery_unknown` and alert; do not create a fresh send task. A definitive rate-limit error may be scheduled with Telegram-provided delay.

- [ ] **Step 4: Run E2E and queue retry tests**

Run: `docker compose --env-file ../.env --profile test run --rm test pytest tests/e2e/test_message_delivery.py tests/integration/test_queue.py -q`

Expected: all pass; one external send.

- [ ] **Step 5: Commit**

```bash
git add project/src/moroz/messaging project/tests/e2e/test_message_delivery.py project/worker/main.py
git commit -m "feat: добавлена идемпотентная доставка Telegram"
```

### Task 5: Pipeline checkpoint

**Files:**
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`

- [ ] **Step 1:** Run `docker compose --env-file ../.env --profile test run --rm test pytest tests/unit tests/integration/messaging tests/e2e/test_privacy_gate.py tests/e2e/test_message_delivery.py -q`; expect all pass.
- [ ] **Step 2:** Run `docker compose --env-file ../.env build bot`, `docker compose --env-file ../.env run --rm --no-deps --entrypoint python bot -m compileall -q /app` and `docker compose --env-file ../.env run --rm --no-deps --entrypoint python bot -c "import cache, config, db, handlers, llm"`; then run `docker compose --env-file ../.env up -d --build postgres redis rabbitmq admin worker scheduler` and `docker compose --env-file ../.env ps`. Expect smoke success and required non-bot services running. Live bot E2E remains blocked until a separate Telegram test token exists.
- [ ] **Step 3:** Send one consented test update twice; expect one inbox row and one outbound send.
- [ ] **Step 4:** Record evidence in roadmap/changelog.
- [ ] **Step 5:** Commit with `git commit -m "docs: зафиксирован reliable pipeline checkpoint"`.
