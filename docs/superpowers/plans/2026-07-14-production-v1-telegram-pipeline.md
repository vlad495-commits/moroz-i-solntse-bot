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
- Produces: durable `task_outbox` rows for RabbitMQ tasks separately from Telegram `outbound_messages`.

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
CREATE TABLE task_outbox (
  id UUID PRIMARY KEY,
  kind TEXT NOT NULL,
  payload JSONB NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'pending',
  published_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Use `INSERT ... ON CONFLICT DO NOTHING RETURNING id` in `accept`. `enqueue_outbound()` creates the Telegram outbound row and its `send_outbound` task-outbox row in one PostgreSQL transaction. Do not use `outbound_messages` as the RabbitMQ transactional outbox.

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
- Create: `project/migrations/versions/0003_processing_consents.py`
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

Handle consent callback separately and store channel, user ID, consent version and grant time in PostgreSQL. Switch bot command to `uvicorn webhook:app`; keep polling only behind `TELEGRAM_MODE=polling` for local emergency use, default webhook in production. After consent, persist each normalized Telegram update through `MessageRepository.accept()` before placing it in the Redis buffer; a duplicate `update_id` must not be buffered twice.

- [ ] **Step 4: Run E2E test**

Run: `docker compose --env-file ../.env --profile test run --rm test pytest tests/e2e/test_privacy_gate.py -q`

Expected: no-consent content absent; consent callback persists only the consent record.

- [ ] **Step 5: Commit**

```bash
git add project/src/moroz/security/consent.py project/migrations/versions/0003_processing_consents.py project/llm/webhook.py project/tests/e2e/test_privacy_gate.py project/llm/Dockerfile project/docker-compose.yml
git commit -m "feat: добавлен Telegram webhook и privacy gate"
```

### Task 3: Redis buffer и durable enqueue

**Files:**
- Create: `project/src/moroz/messaging/buffer.py`
- Create: `project/src/moroz/messaging/service.py`
- Create: `project/src/moroz/messaging/outbox.py`
- Test: `project/tests/integration/messaging/test_buffer.py`
- Test: `project/tests/integration/messaging/test_outbox.py`
- Modify: `project/llm/webhook.py`
- Modify: `project/tests/e2e/test_privacy_gate.py`

**Interfaces:**
- Produces: `MessageBuffer.append(chat_id, update_id, text) -> None`; Telegram `message_id` remains payload metadata and never replaces the globally unique `update_id`.
- Produces: `MessageBuffer.flush(chat_id) -> BufferedMessage | None`.
- Consumes: `QueuePort.publish(task: QueueTask) -> None` through the transactional outbox relay.
- Wires: after consent, webhook calls `MessageService.accept(envelope)`; the service first persists through `MessageRepository.accept()`, buffers only a newly accepted `update_id`, and creates a durable single-message task when Redis is unavailable.

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
async def append(self, chat_id: str, update_id: str, text: str) -> None:
    key = f"buffer:{chat_id}"
    async with self.redis.pipeline(transaction=True) as pipe:
        pipe.rpush(key, json.dumps({"update_id": update_id, "text": text}))
        pipe.expire(key, 30)
        pipe.set(f"{key}:deadline", self.clock.now().timestamp() + 5, ex=30)
        await pipe.execute()
```

On flush, acquire `lock:buffer:{chat_id}`, atomically read/delete the Redis list and create one durable `process_message` row in `task_outbox` for the already persisted inbox `update_id` values. `OutboxRelay.publish_pending()` publishes pending task rows through `QueuePort.publish()` and marks them published only after publisher confirmation. If Redis is unavailable after the inbox insert, bypass batching and create a single-message task-outbox row so the accepted message is not lost. A duplicate `update_id` returns before Redis append or task creation.

- [ ] **Step 4: Run test including concurrent flush**

Run: `docker compose --env-file ../.env --profile test run --rm test pytest tests/integration/messaging/test_buffer.py -q`

Expected: joined text once; second concurrent flush returns `None`.

- [ ] **Step 5: Commit**

```bash
git add project/src/moroz/messaging/buffer.py project/src/moroz/messaging/service.py project/src/moroz/messaging/outbox.py project/llm/webhook.py project/tests/integration/messaging/test_buffer.py project/tests/integration/messaging/test_outbox.py project/tests/e2e/test_privacy_gate.py
git commit -m "feat: добавлен буфер быстрых Telegram-сообщений"
```

### Task 4: Worker и идемпотентная отправка

**Files:**
- Create: `project/src/moroz/messaging/telegram.py`
- Create: `project/migrations/versions/0004_pipeline_order_and_delivery_claim.py`
- Test: `project/tests/e2e/test_message_delivery.py`
- Modify: `project/src/moroz/messaging/buffer.py`
- Modify: `project/llm/webhook.py`
- Modify: `project/worker/main.py`
- Modify: `project/worker/Dockerfile`
- Modify: `project/worker/requirements.txt`
- Modify: `project/docker-compose.yml`

**Interfaces:**
- Consumes: `OutboxRelay.publish_pending(limit: int = 100) -> int` from Task 3.
- Produces: `TelegramSender.send(outbound_id: UUID) -> DeliveryResult`.
- Produces: worker handlers for `process_message` and `send_outbound` tasks.
- Runtime owner: the worker runs one bounded periodic pump that discovers due Redis buffers, calls `MessageBuffer.flush(...)`, then calls `OutboxRelay.publish_pending(...)`. This is the explicit restart-safe runtime bridge required by Task 3; do not add another service or use the future scheduler phase.

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

For `process_message`, lock one chat in PostgreSQL, reject an already-materialized reply before calling the LLM, load the existing PostgreSQL conversation context, call the existing `project/llm/llm.py` adapter once, and atomically create `outbound_messages` plus a `send_outbound` task. Keep the existing message/token history behavior, but do not add guardrails, a reserve provider or a scenario router in this phase. For `send_outbound`, move the generic durable claim/send/finalize helper out of the webhook into `moroz.messaging.telegram` and reuse it from both webhook and worker. If network result is unknown, set `delivery_unknown` and emit a safe structured error event; do not create a fresh send task. A definitive rate-limit error may be scheduled with Telegram-provided delay.

The worker runtime must connect only the dependencies it now owns (PostgreSQL, Redis, RabbitMQ, Telegram and the existing LLM adapter), start the due-buffer/outbox pump alongside the RabbitMQ consumer, and cancel/close both paths on shutdown. Redis discovery failure must not prevent publishing already-durable database tasks. Tests must prove restart recovery by creating a due Redis buffer and a pending task before a fresh pump instance starts.

Review gate: PostgreSQL is the source of truth for task identity, persisted text and monotonic ingress order. Under the chat lock the worker may process only the earliest still-accepted inbox rows represented by the task; overlapping or fully processed groups must never feed the same update to the LLM twice. Redis deadline discovery must have a hard per-tick bound. Worker restart must converge stale `sending` rows to terminal `delivery_unknown` without blind resend, preserve the existing prompt hot-reload behavior, and support every provider advertised by the existing LLM adapter.

Outage gate: Redis TTL expiry or total Redis loss must not strand an `accepted` inbox row. The worker owns a bounded PostgreSQL recovery sweep for expired accepted rows and can enqueue deterministic single-message tasks without Redis. Telegram V1 accepts only private chats until multi-user context semantics are designed. Shutdown cancels consumer, pump and prompt listener together under one deadline shorter than Compose `stop_grace_period`; every resource close is attempted and the original runtime error wins over cleanup errors.

- [ ] **Step 4: Run E2E and queue retry tests**

Run: `docker compose --env-file ../.env --profile test run --rm test pytest tests/e2e/test_message_delivery.py tests/integration/test_queue.py -q`

Expected: all pass; one accepted buffered request produces one saved outbound and one external send, while duplicate task delivery does not call either the LLM or Telegram twice.

- [ ] **Step 5: Commit**

```bash
git add project/src/moroz/messaging project/llm/webhook.py project/tests/e2e/test_message_delivery.py project/worker project/docker-compose.yml
git commit -m "feat: добавлена идемпотентная доставка Telegram"
```

### Task 5: Pipeline checkpoint

**Files:**
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`

- [ ] **Step 1:** Run `docker compose --env-file ../.env --profile test run --rm test pytest tests/unit tests/integration/messaging tests/e2e/test_privacy_gate.py tests/e2e/test_message_delivery.py -q`; expect all pass.
- [ ] **Step 2:** Run `docker compose --env-file ../.env build bot`, `docker compose --env-file ../.env run --rm --no-deps --entrypoint python bot -m compileall -q /app` and `docker compose --env-file ../.env run --rm --no-deps --entrypoint python bot -c "import cache, config, db, handlers, llm"`; then run `docker compose --env-file ../.env up -d --build postgres redis rabbitmq admin worker scheduler` and `docker compose --env-file ../.env ps`. Expect smoke success and required non-bot services running. Live bot E2E remains blocked until a separate Telegram test token exists.
- [ ] **Step 3:** Through the Docker E2E harness with fake LLM and fake Telegram, send one consented test update twice; expect one inbox row, one LLM call and one outbound send. Live bot E2E remains blocked until a separate Telegram test token exists.
- [ ] **Step 4:** Record evidence in roadmap/changelog.
- [ ] **Step 5:** Commit with `git commit -m "docs: зафиксирован reliable pipeline checkpoint"`.
