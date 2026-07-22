# Real YCLIENTS Booking Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Реализовать официальный real YCLIENTS `BookingPort`, доказать fake HTTP contract и durable fail-closed поведение, затем подготовить и выполнить безопасный sandbox smoke на выделенном test-филиале.

**Architecture:** `BookingService` остаётся provider-neutral. `YclientsAdapter` переводит domain models в официальный availability/book_check/protected CRUD contract; отдельный stdlib HTTP helper отвечает только за auth, encoding, timeout и dual-window rate limit. Slot и owner markers детерминированно кодируются без cache, а любой неопределённый mutation result поднимается как `BookingOutcomeUnknown` в уже существующий PostgreSQL escalation flow.

**Tech Stack:** Python 3.12 stdlib (`asyncio`, `urllib.request`, `http.server`, `json`, `base64`, `zoneinfo`), existing asyncpg/Alembic/pytest, Docker Compose.

## Global Constraints

- Источник HTTP-контракта — только embedded OpenAPI 3.0.3 `https://developers.yclients.com/ru/`; Context7 не содержит официального YCLIENTS source.
- Availability: `book_dates` → `book_staff` → `book_times`; `book_check` перед create/reschedule.
- Mutations/get: только protected `records/record` CRUD с Partner + User Token; публичный `book_record` запрещён.
- `Accept: application/vnd.yclients.v2+json`; rate limits `5 req/s` и `200 req/min`.
- Ни один `POST`/`PUT`/`DELETE` не retry-ится. Mutation transport/timeout/5xx/malformed success → `BookingOutcomeUnknown`.
- GET также не retry-ится: официальный контракт не обосновывает retry policy.
- `customer_id` — internal opaque non-PII ownership ID. Полную клиентскую базу не читать.
- Create требует имя, телефон, optional comment и `personal_data_processing_allowed is True` до внешнего вызова.
- Slot ID deterministic, opaque и decodeable после restart без in-memory cache.
- Новые runtime dependencies и инфраструктура не добавляются; используется stdlib и singleton worker process.
- Docker-only. Каждый run — task-specific Compose namespace с новыми process-only PostgreSQL/Redis/RabbitMQ credentials; shared/prototype/staging containers не трогать.
- Временные файлы — только root `tmp/`; `.env`, токены, DSN, passwords, ПД и secret-shaped values не выводить.
- Local/fake gate должен завершиться до запроса внешних values. Sandbox использует только ignored `.env`/process environment и synthetic test data.
- Staging rollback остаётся открытым без реального distinct `candidate → previous → candidate` app-image evidence; DB downgrade запрещён.
- Каждый task: RED → GREEN → Docker regression → docs/changelog → commit → independent task review.

---

### Task 1: Consented create domain и state-machine gate

**Files:**
- Modify: `project/src/moroz/booking/models.py`
- Modify: `project/src/moroz/booking/service.py`
- Modify: `project/tests/unit/booking/test_mock_adapter.py`
- Modify: `project/tests/e2e/booking/test_create_booking.py`
- Modify: `project/tests/e2e/booking/test_change_booking.py`
- Modify: `changelog.md`
- Modify: `Дорожная карта.md`

**Interfaces:**
- Produces exact required `CreateBooking` fields from the approved spec.
- `BookingService` consumes create scenario state keys `customer_name`, `customer_phone`, `personal_data_processing_allowed`, optional `comment`.
- Consent/contact failure performs no `BookingPort` call and no `executing` checkpoint.

- [x] **Step 1: Write RED tests before production changes**

Update the shared create scenario fixture to carry:

```python
state={
    "slot_query": {
        "service_ids": ["331"],
        "starts_after": "2026-07-29T00:00:00+03:00",
        "starts_before": "2026-07-30T00:00:00+03:00",
        "staff_id": "6544",
    },
    "selected_slot_id": selected_slot_id,
    "customer_name": "Sandbox Customer",
    "customer_phone": "+70000000000",
    "personal_data_processing_allowed": True,
    "comment": "test booking",
}
```

Add focused behavior tests:

```python
async def test_create_requires_personal_data_consent_before_port_or_checkpoint(repo):
    scenario = _scenario(personal_data_processing_allowed=False)
    await repo.create_scenario(scenario)
    port = CountingMockYclientsAdapter([_slot("slot-9", 14)])

    result = await BookingService(port, repo).handle(scenario.id, confirmed=True)

    assert result.status == "needs_input"
    assert result.next_action == "request_personal_data_consent"
    assert port.create_calls == 0
    assert (await repo.get_scenario(scenario.id)).phase == "awaiting_confirmation"


async def test_create_passes_minimum_customer_data_to_port(repo):
    port = CapturingMockYclientsAdapter([_slot("slot-9", 14)])
    scenario = _scenario()
    await repo.create_scenario(scenario)

    await BookingService(port, repo).handle(scenario.id, confirmed=True)

    assert port.last_create == CreateBooking(
        customer_id=scenario.customer_id,
        slot_id="slot-9",
        idempotency_key=scenario.idempotency_key,
        customer_name="Sandbox Customer",
        customer_phone="+70000000000",
        personal_data_processing_allowed=True,
        comment="test booking",
    )
```

Add an equivalent missing-name/phone test with `next_action="collect_booking_contact"`. Update all direct mock seed commands to use this local helper with synthetic values:

```python
def _create_command(
    customer_id: str = "customer-1",
    slot_id: str = "slot-ok",
    idempotency_key: str = "create-1",
) -> CreateBooking:
    return CreateBooking(
        customer_id=customer_id,
        slot_id=slot_id,
        idempotency_key=idempotency_key,
        customer_name="Sandbox Customer",
        customer_phone="+70000000000",
        personal_data_processing_allowed=True,
    )
```

- [x] **Step 2: Run RED in disposable Compose namespace**

Run focused unit/E2E files in `moroz-yclients-real-t1-red` after setting fresh process-only infrastructure values. Expected: `CreateBooking` rejects new keywords or service sends the old three-field command. Record only test counts and expected assertion/type failure.

- [x] **Step 3: Implement the minimum model and service change**

Use this exact dataclass:

```python
@dataclass(frozen=True, slots=True)
class CreateBooking:
    customer_id: str
    slot_id: str
    idempotency_key: str
    customer_name: str
    customer_phone: str
    personal_data_processing_allowed: bool
    comment: str | None = None
```

Before the `executing` checkpoint in `_handle_create`:

```python
name = str(scenario.state.get("customer_name", "")).strip()
phone = str(scenario.state.get("customer_phone", "")).strip()
if not name or not phone:
    return ScenarioResult(
        status="needs_input",
        message="Нужны имя и телефон для записи.",
        next_action="collect_booking_contact",
        events=(),
    )
if scenario.state.get("personal_data_processing_allowed") is not True:
    return ScenarioResult(
        status="needs_input",
        message="Для записи нужно согласие на обработку персональных данных.",
        next_action="request_personal_data_consent",
        events=(),
    )
```

Construct `CreateBooking` with the exact values above and normalize an empty/missing comment to `None`. Do not add a new phase or migration.

- [x] **Step 4: Run GREEN and all existing booking tests**

Run the focused files, then `pytest tests/unit/booking tests/integration/booking tests/e2e/booking -q` in a new namespace. Expected: exit 0 and no new warnings. Clean only task namespaces.

- [x] **Step 5: Document, verify, commit and review**

Append RED/GREEN evidence, mark Task 1 complete, run `git diff --check` and a secret-shaped diff scan, commit:

```text
feat: добавлены данные и согласие для записи
```

Generate a task diff package and require reviewer verdicts: spec compliance `✅`, task quality `Approved`, no open Critical/Important.

---

### Task 2: Stdlib HTTP config, auth и dual-window limiter

**Files:**
- Create: `project/src/moroz/booking/yclients_http.py`
- Create: `project/tests/contract/booking/test_yclients_http.py`
- Modify: `project/docker-compose.yml`
- Modify: `project/tests/unit/test_migration_profile.py`
- Modify: `changelog.md`
- Modify: `Дорожная карта.md`

**Interfaces:**
- Produces `YclientsConfig.from_env(env: Mapping[str, str]) -> YclientsConfig`.
- Produces `YclientsRateLimiter.acquire() -> None`.
- Produces `YclientsHttpClient.request(method, path, *, query=(), json_body=None, user_auth=False) -> HttpResponse`.
- Produces internal transport exception `YclientsTransportError` without URL/token/body text.

- [x] **Step 1: Write RED config/rate/auth tests**

Use a stdlib `ThreadingHTTPServer` bound to `127.0.0.1` and capture only method/path/headers/body. Required tests:

```python
def test_config_requires_tokens_without_leaking_them():
    with pytest.raises(ValueError, match="YCLIENTS_USER_TOKEN is required"):
        YclientsConfig.from_env({
            "YCLIENTS_PARTNER_TOKEN": "partner-value",
            "YCLIENTS_COMPANY_ID": "123",
        })
    config = YclientsConfig.from_env({
        "YCLIENTS_PARTNER_TOKEN": "partner-value",
        "YCLIENTS_USER_TOKEN": "user-value",
        "YCLIENTS_COMPANY_ID": "123",
    })
    assert "partner-value" not in repr(config)
    assert "user-value" not in repr(config)


async def test_http_uses_exact_partner_and_partner_user_headers(fake_server):
    await client.request("GET", "/partner")
    await client.request("GET", "/protected", user_auth=True)
    assert fake_server.requests[0].headers["Authorization"] == "Bearer partner-value"
    assert fake_server.requests[1].headers["Authorization"] == (
        "Bearer partner-value, User user-value"
    )
    assert all(
        request.headers["Accept"] == "application/vnd.yclients.v2+json"
        for request in fake_server.requests
    )
```

Use a fake monotonic clock whose injected sleep advances time. Acquire six times and assert a `1.0` delay before request 6; acquire 201 times with the second-window safely advanced and assert the minute window delays before request 201.

- [x] **Step 2: Run RED**

Run `tests/contract/booking/test_yclients_http.py` in `moroz-yclients-real-t2-red`. Expected: import failure for `moroz.booking.yclients_http`.

- [x] **Step 3: Implement exact config and HTTP primitives**

Core public types:

```python
@dataclass(frozen=True, slots=True)
class YclientsConfig:
    base_url: str
    partner_token: str = field(repr=False)
    user_token: str = field(repr=False)
    company_id: int
    timezone_name: str = "Europe/Moscow"
    timeout_seconds: float = 10.0

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "YclientsConfig":
        def required(name: str) -> str:
            value = env.get(name, "").strip()
            if not value:
                raise ValueError(f"{name} is required")
            return value

        company_id = int(required("YCLIENTS_COMPANY_ID"))
        timeout_seconds = float(env.get("YCLIENTS_TIMEOUT_SECONDS", "10"))
        timezone_name = env.get("YCLIENTS_TIMEZONE", "Europe/Moscow")
        if company_id <= 0 or timeout_seconds <= 0:
            raise ValueError("YCLIENTS numeric settings must be positive")
        ZoneInfo(timezone_name)
        return cls(
            base_url=env.get("YCLIENTS_BASE_URL", "https://api.yclients.com").rstrip("/"),
            partner_token=required("YCLIENTS_PARTNER_TOKEN"),
            user_token=required("YCLIENTS_USER_TOKEN"),
            company_id=company_id,
            timezone_name=timezone_name,
            timeout_seconds=timeout_seconds,
        )


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: bytes


class YclientsTransportError(Exception):
    pass
```

`YclientsRateLimiter` keeps two `deque[float]`, evicts timestamps `<= now-window`, holds one `asyncio.Lock`, sleeps for `max(second_delay, minute_delay)`, and appends the accepted timestamp to both windows. Defaults are exactly `5`, `1.0`, `200`, `60.0`; only clock/sleep are injectable for tests.

`YclientsHttpClient`:

```python
async def request(
    self,
    method: str,
    path: str,
    *,
    query: Sequence[tuple[str, object]] = (),
    json_body: Mapping[str, object] | None = None,
    user_auth: bool = False,
) -> HttpResponse:
    await self._limiter.acquire()
    return await asyncio.to_thread(
        self._request_sync, method, path, query, json_body, user_auth
    )
```

Use `urlencode(query, doseq=True)`, compact UTF-8 JSON, required headers, `urllib.request.Request` and bounded timeout. Convert `HTTPError` into `HttpResponse(status, body)` because it is a definite HTTP response. Convert `URLError`, `TimeoutError`, `OSError`, `http.client.HTTPException` into a message-less `YclientsTransportError` chained from the original. Never retry or log.

Compose passes optional `YCLIENTS_*` only to `worker`; test/migrate/cutover env sets remain byte-for-byte unchanged. Update the static env-contract test accordingly.

- [x] **Step 4: Run GREEN and unit/static regressions**

Run the contract file, `tests/unit/common/test_config.py`, `tests/unit/test_migration_profile.py`, and `docker compose --env-file ../.env -p moroz-yclients-real-t2 config --quiet` with fresh non-secret placeholder YCLIENTS values in process environment. Expected: exit 0.

- [x] **Step 5: Document, commit and review**

Commit after `git diff --check` and scoped secret scan:

```text
feat: добавлен HTTP-контур YCLIENTS
```

Task review must independently inspect the rolling-window algorithm, header isolation and token-safe errors.

---

### Task 3: Availability, slot codec, create и get contract

**Files:**
- Create: `project/src/moroz/booking/yclients.py`
- Create: `project/tests/contract/booking/test_yclients_adapter.py`
- Modify: `changelog.md`
- Modify: `Дорожная карта.md`

**Interfaces:**
- Produces `YclientsAdapter(BookingPort)` with constructor `YclientsAdapter(config: YclientsConfig, *, http: YclientsHttpClient | None = None)`.
- Implements `list_slots`, `create_booking`, `get_booking` in this task.
- Private slot marker `yclients:v1:` and owner marker `moroz:v1:` are deterministic and restart-safe.

- [x] **Step 1: Write RED fake HTTP contract tests**

Script exact official envelopes for `book_dates`, `book_staff`, `book_times`, `book_check`, create-record and get-record. Prove:

```python
slots = await first_adapter.list_slots(query)
assert [(slot.staff_id, slot.duration_minutes) for slot in slots] == [("6544", 60)]
assert slots[0].id.startswith("yclients:v1:")

restarted = YclientsAdapter(config)
booking = await restarted.create_booking(CreateBooking(
    customer_id="customer-7",
    slot_id=slots[0].id,
    idempotency_key="local-key-only",
    customer_name="Sandbox Customer",
    customer_phone="+70000000000",
    personal_data_processing_allowed=True,
    comment="contract test",
))
assert booking.customer_id == "customer-7"
assert booking.slot_id == slots[0].id
```

Assert the request sequence and auth exactly:

```text
GET  /api/v1/book_dates/123
GET  /api/v1/book_staff/123
GET  /api/v1/book_times/123/6544/2026-07-29
POST /api/v1/book_check/123                 Partner only
POST /api/v1/records/123                    Partner + User
GET  /api/v1/record/123/9001                Partner + User
```

Create body must equal the official minimal mapping: numeric `staff_id`, services containing only numeric `id`, client name/phone, `save_if_busy=false`, local datetime string, `seance_length`, `send_sms=false`, optional comment, `attendance=0`, encoded `api_id`, and the two explicit agreement booleans. Assert no `Idempotency-Key` header and no local idempotency key in body.

Add official example/schema variants: epoch and ISO booking dates/datetimes, numeric/string IDs, duplicate time removal, exact aware query-bound filtering. Invalid slot/owner markers and `success=false` fail closed.

- [x] **Step 2: Run RED**

Run `tests/contract/booking/test_yclients_adapter.py` in `moroz-yclients-real-t3-red`. Expected: missing module/class.

- [x] **Step 3: Implement deterministic codecs and availability**

Slot payload is exact compact JSON:

```python
payload = {
    "services": sorted({int(value) for value in service_ids}),
    "staff": int(staff_id),
    "start": int(starts_at.timestamp()),
    "duration": duration_seconds,
}
raw = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
message = b"yclients-slot:v1\0" + str(company_id).encode() + b"\0" + raw.encode()
tag = hmac.new(user_token.encode(), message, hashlib.sha256).digest()[:16]
slot_id = "yclients:v1:" + b64(raw.encode()) + "." + b64(tag)
```

Decode restores padding, verifies the company-bound HMAC with `compare_digest`, validates exact key set/types/positive values and canonical sorted unique services, then returns an immutable internal slot payload. User Token is the already-required signing key; rotation intentionally invalidates old slot IDs. Owner marker uses the same base64 rules over UTF-8 `customer_id`, rejects blank/oversized/non-UTF8 data, and never decodes phone/name.

Availability follows the six steps in the spec: `book_dates`/`book_times` use repeated `service_ids`, while `book_staff` uses one comma-separated `service_ids` value per OpenAPI `explode=false`. It filters `bookable is True` and local dates before time fan-out, uses `ZoneInfo(config.timezone_name)`, exact range filtering and stable `(starts_at, staff_id, slot_id)` ordering. Because official `book_dates` requires paired `date_from`/`date_to`, missing `starts_before` is rejected before HTTP rather than inventing a horizon.

- [x] **Step 4: Implement create/get response mapping**

Before create, call `book_check` with:

```python
{"appointments": [{
    "id": 1,
    "services": [331],
    "staff_id": 6544,
    "datetime": 1785315600,
}]}
```

Accept `book_check` only on `201` + `success=true`; map meta error codes `433/436/437/438` to `SlotUnavailable`. Create accepts only `201` and one unambiguous record object (direct object or one-item official data array). Get accepts only `200`. Normalize record services/staff/datetime/seance length/deleted/api_id into `ExternalBooking` and reconstructed slot ID.

Use shared response helpers with an explicit `mutation_sent` flag: malformed create success or create transport/5xx becomes `BookingOutcomeUnknown`; malformed read becomes `BookingTemporaryError`.

- [x] **Step 5: Run GREEN and current booking regressions**

Run both YCLIENTS contract files and all existing booking tests. Expected: exit 0. Recreate adapters between list/create/get assertions to prove no cache dependency.

- [x] **Step 6: Document, commit and review**

Commit:

```text
feat: реализованы слоты и создание YCLIENTS
```

Reviewer must verify exact official endpoints/auth/body, deterministic codecs, timezone handling, PII minimization and mutation-unknown classification.

---

### Task 4: Protected reschedule/cancel и durable outcome-unknown E2E

**Files:**
- Modify: `project/src/moroz/booking/yclients.py`
- Modify: `project/tests/contract/booking/test_yclients_adapter.py`
- Create: `project/tests/e2e/booking/test_yclients_fail_closed.py`
- Modify: `changelog.md`
- Modify: `Дорожная карта.md`

**Interfaces:**
- Completes `reschedule_booking` and `cancel_booking`.
- Preserves current record client/comment/api_id without client-list lookup.
- Guarantees exactly one mutation request per method invocation.

- [x] **Step 1: Write RED protected CRUD/error tests**

Contract sequence for reschedule:

```text
GET  /api/v1/record/123/9001
POST /api/v1/book_check/123
PUT  /api/v1/record/123/9001
```

Assert PUT preserves exact current client name/phone, comment and ownership `api_id`, replaces target slot fields, sets `save_if_busy=false`, and does not add `Idempotency-Key`. Cancel sends exactly one `DELETE /api/v1/record/123/9001` and accepts only `204`.

Add parameterized mapping:

```python
@pytest.mark.parametrize("code", [433, 436, 437, 438])
async def test_book_check_conflicts_are_slot_unavailable(code, scripted_server):
    scripted_server.enqueue_json(422, {
        "success": False,
        "meta": {"errors": [{"code": code, "message": "slot conflict"}]},
    })
    with pytest.raises(SlotUnavailable):
        await adapter.create_booking(command)

async def test_create_connection_drop_is_outcome_unknown_and_not_retried(scripted_server):
    scripted_server.enqueue_json(201, {"success": True, "data": {}})  # book_check
    scripted_server.enqueue_disconnect_after_body()
    with pytest.raises(BookingOutcomeUnknown):
        await adapter.create_booking(command)
    assert scripted_server.count("POST", "/api/v1/records/123") == 1


async def test_put_500_is_outcome_unknown_and_not_retried(scripted_server):
    scripted_server.enqueue_json(200, current_record)
    scripted_server.enqueue_json(201, {"success": True, "data": {}})
    scripted_server.enqueue_json(500, {"success": False})
    with pytest.raises(BookingOutcomeUnknown):
        await adapter.reschedule_booking(command)
    assert scripted_server.count("PUT", "/api/v1/record/123/9001") == 1


async def test_delete_connection_drop_is_outcome_unknown_and_not_retried(scripted_server):
    scripted_server.enqueue_disconnect_after_body()
    with pytest.raises(BookingOutcomeUnknown):
        await adapter.cancel_booking(command)
    assert scripted_server.count("DELETE", "/api/v1/record/123/9001") == 1


async def test_read_transport_failure_is_temporary_and_not_retried(scripted_server):
    scripted_server.enqueue_disconnect_after_body()
    with pytest.raises(BookingTemporaryError):
        await adapter.get_booking("9001")
    assert scripted_server.count("GET", "/api/v1/record/123/9001") == 1
```

The fake server counts received methods; every mutation assertion requires count `== 1`.

- [x] **Step 2: Write the cross-layer RED E2E**

Use disposable PostgreSQL, `YclientsAdapter` against a fake server that returns successful availability/book_check, fully reads the create body, records one POST, then closes the socket without a response:

```python
result = await service.handle(scenario.id, confirmed=True)
repeat = await service.handle(scenario.id, confirmed=True)

assert (result.status, result.error_code) == ("escalated", "booking_outcome_unknown")
assert repeat == result
assert fake_server.create_count == 1
assert [event.event_type for event in await repo.list_events(scenario.id)].count(
    "admin_attention_required"
) == 1
```

- [x] **Step 3: Run RED**

Run focused contract cases and E2E in `moroz-yclients-real-t4-red`. Expected: missing reschedule/cancel branches or incorrect exception mapping.

- [x] **Step 4: Implement minimum protected change mapping**

Reschedule gets the exact current record first and rejects missing/invalid `moroz:v1:` ownership. Build PUT from target slot plus only preserved record fields required by official schema. Do not carry finance, documents, labels, arbitrary custom fields or unrelated provider response fields.

Cancel sends no preliminary mutation and no cleanup retry. Mapping rules are exact:

```text
GET/read transport, timeout, 429, 5xx, malformed envelope -> BookingTemporaryError
book_check transport, timeout, 5xx -> BookingTemporaryError
HTTP 404 -> BookingNotFound
book_check 433/436/437/438 -> SlotUnavailable
mutation transport, timeout, 5xx, malformed success, unexpected status -> BookingOutcomeUnknown
definite 400/401/403/409/422/429 without slot code -> BookingTemporaryError
```

- [x] **Step 5: Run GREEN and complete booking/migration regression**

Run `tests/contract/booking`, `tests/unit/booking`, `tests/integration/booking`, `tests/e2e/booking`, and `tests/integration/test_migrations.py` in a clean namespace. Expected: exit 0, Alembic remains `0005_booking_state (head)`, no schema change.

- [x] **Step 6: Document, commit and review**

Commit:

```text
feat: добавлены protected изменения YCLIENTS
```

Reviewer must inspect no-retry evidence, ambiguity classification, preserved ownership, single-record reads and durable PostgreSQL escalation.

---

### Task 5: Docker sandbox smoke tooling и local completion gate

**Files:**
- Create: `project/src/moroz/booking/yclients_sandbox_smoke.py`
- Create: `project/tests/unit/booking/test_yclients_sandbox_smoke.py`
- Modify: `project/docker-compose.yml`
- Modify: `project/tests/unit/test_migration_profile.py`
- Modify: `docs/superpowers/plans/2026-07-14-production-v1-yclients-booking.md`
- Modify: `План реализации.md`
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`

**Interfaces:**
- Produces `python -m moroz.booking.yclients_sandbox_smoke` inside Compose profile `yclients-smoke`.
- Reads only `YCLIENTS_*` environment values; returns exit 0 only after full create/get/reschedule/get/cancel/duplicate proof.
- Emits one redacted JSON summary containing operation statuses and counts, never tokens, phone, name, comment, raw provider body or foreign records.

- [x] **Step 1: Write RED smoke-orchestrator tests with fake adapter/HTTP**

Test exact order and compensation policy:

```python
assert calls == [
    "list_services",
    "list_slots",
    "create_booking",
    "get_booking",
    "reschedule_booking",
    "get_booking",
    "cancel_booking",
    "get_cancelled_booking",
    "count_duplicate_marker",
]
```

Prove it requires two distinct future slots, `YCLIENTS_TEST_SERVICE_ID`, synthetic `YCLIENTS_TEST_NAME`, `YCLIENTS_TEST_PHONE`, and explicit `YCLIENTS_SANDBOX_CONSENT=yes`. Prove `BookingOutcomeUnknown` aborts without blind cancel and output redaction removes every configured secret/test PII value.

- [x] **Step 2: Run RED**

Run `tests/unit/booking/test_yclients_sandbox_smoke.py`. Expected: missing module/profile.

- [x] **Step 3: Implement bounded smoke flow**

Use `book_services` for configured service validation, `list_slots` for staff/times, `BookingPort` methods for CRUD, and a protected records query restricted to the two slot dates only for the final exact `api_id` duplicate count. Discard nonmatching records without logging or returning them.

Generate one unique smoke correlation from `uuid4`; use it only in `idempotency_key`, safe comment and `api_id` marker. Require non-production base URL/company fixture by explicit `YCLIENTS_SANDBOX_CONSENT=yes`; do not infer consent.

On known failure before mutation, exit nonzero. On successful create followed by definite later failure, a single explicit cancel is allowed only when current external ID is known and no prior mutation outcome is unknown. On any `BookingOutcomeUnknown`, print redacted `manual_review_required=true`, perform no further mutation and exit nonzero.

Compose profile uses the existing worker image and optional interpolation defaults so normal config rendering does not require tokens. It passes YCLIENTS values only to `worker` and `yclients-smoke`, never test/migrate/cutover.

- [x] **Step 4: Run GREEN and canonical local/fake verification**

With fresh process-only infra credentials and no YCLIENTS live tokens:

```powershell
docker compose --env-file ../.env -p moroz-yclients-real-final --profile test build --no-cache test
docker compose --env-file ../.env -p moroz-yclients-real-final --profile test run --rm test pytest -q
docker compose --env-file ../.env -p moroz-yclients-real-final config --quiet
docker compose --env-file ../.env -p moroz-yclients-real-final build --no-cache worker
docker compose --env-file ../.env -p moroz-yclients-real-final run --rm --no-deps --entrypoint python worker -m compileall -q /app
docker compose --env-file ../.env -p moroz-yclients-real-final run --rm --no-deps --entrypoint python worker -c "from moroz.booking.yclients import YclientsAdapter"
```

Expected: full pytest exit 0, config valid, worker image builds/imports as non-root, `0005_booking_state (head)`, secret/log/static gates clean. Remove only `moroz-yclients-real-*` containers/volumes/networks/images and confirm 0/0/0/0.

- [x] **Step 5: Independent whole-branch review/fix-loop**

Generate a review package from `b42f031` to current HEAD. Reviewer checks the complete approved spec and reports Critical/Important/Minor plus readiness. Dispatch one fixer for the complete findings list, require covering Docker tests in its report, regenerate package and repeat until `0 Critical / 0 Important / 0 Minor` or an honest blocker remains.

- [x] **Step 6: Record local completion and commit**

Mark real adapter local/fake gate complete with exact test evidence. Keep sandbox/live and staging rollback gates open. Commit:

```text
docs: подтверждён local real YCLIENTS adapter
```

---

### Task 6: Consented YCLIENTS sandbox smoke evidence

**Files:**
- Modify: `docs/superpowers/plans/2026-07-14-production-v1-yclients-booking.md`
- Modify: `План реализации.md`
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`

**Interfaces:**
- No runtime code unless the real sandbox exposes an official-contract mismatch reproduced first by a fake HTTP RED test.
- Consumes credentials/fixture IDs only from user-populated ignored `.env`; they are never pasted into chat or command output.

- [x] **Step 1: External readiness gate after local completion**

Check only presence, never values, of:

```text
YCLIENTS_PARTNER_TOKEN
YCLIENTS_USER_TOKEN
YCLIENTS_COMPANY_ID
YCLIENTS_TEST_SERVICE_ID
YCLIENTS_TEST_NAME
YCLIENTS_TEST_PHONE
YCLIENTS_SANDBOX_CONSENT=yes
```

If any is absent, ask one concise question instructing the user to add missing values directly to ignored `.env` and confirm readiness without sharing them. This is the only legitimate external blocker before smoke.

Readiness check 2026-07-22: `.env` exists, but all seven required sandbox keys are absent and explicit consent is not set. No values were printed or logged; this step remains open until the user populates the ignored file and confirms readiness.

Repeated readiness check after user confirmation: all seven required keys are present and explicit consent equals `yes`. Only presence/count/consent status was emitted; values remain unprinted and unlogged.

- [ ] **Step 2: Run smoke in a dedicated profile**

Run exactly one `yclients-smoke` container in namespace `moroz-yclients-sandbox-<timestamp>`. Capture only the redacted summary and exit code. Never use real customer PII.

- [ ] **Step 3: Verify evidence**

Require: services/staff/slots read; two distinct slots; one create; exact get; one reschedule; exact get at new instant; one cancel; cancelled/deleted confirmation; duplicate marker count exactly one; `manual_review_required=false`; no secret-shaped or PII output.

- [ ] **Step 4: Handle mismatch through TDD**

If sandbox contradicts the official fixture/shape, do not patch live-first. Add a fake HTTP test reproducing the exact redacted mismatch, observe RED, implement minimum mapping, run focused + full Docker gates, task review, then rerun a new consented smoke. Do not retry an outcome-unknown mutation.

- [ ] **Step 5: Final documentation checkpoint**

If smoke passes, mark YCLIENTS phase live-complete and record timestamp, redacted IDs/counts/statuses and suite/review evidence. Keep Staging rollback open unless a separate real distinct app-image `candidate → previous → candidate` cycle was actually executed. Commit:

```text
docs: подтверждён sandbox smoke YCLIENTS
```

No merge or push without a separate explicit user request.
