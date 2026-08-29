# Simple LLM Router Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Заменить multi-intent Router на один строгий маршрут, сохранив privacy, Security parallel gate и все реальные сценарии.

**Architecture:** Scripts-first возвращает один `RouteDecision(route, confidence)` только для однозначных сообщений; остальные masked сообщения классифицирует Router LLM по exact JSON schema. Служебные source/reason/usage живут вне LLM-контракта, а текущая Router Evaluation переходит на отдельный versioned suite `router_v2`, не меняя старый dataset, migration `0014` и историю.

**Tech Stack:** Python 3.12, asyncio, dataclasses, OpenAI/Anthropic-compatible provider abstraction, FastAPI/Jinja, PostgreSQL/Alembic, pytest, Docker Compose.

## Global Constraints

- Единственный LLM payload: `{"route":"<allowlisted>","confidence":0.0}`.
- Routes: `consultation`, `booking`, `booking_management`, `escalation`, `smalltalk`, `offtopic`, `other`.
- Fallback: `consultation` с confidence `0.0`; не блокировать и не эскалировать.
- `escalation` остаётся только metadata; DB escalation и `human_mode` не менять.
- Router видит только masked current и bounded masked context: максимум 6 сообщений и 2000 символов.
- Router verdict используется только после Security OK; BLOCK/error cancel-and-drain-ит Router task.
- Security, Compact, answer LLM, local output checks, optional semantic Validator и бизнес-цепочки не переделывать.
- `project/llm/eval/router_dataset.json`, migration `0014` и другие старые migrations/datasets не менять.
- Новых зависимостей, таблиц, provider-вызовов, push, deployment, staging или production-действий нет.

---

### Task 1: Single-route Router core

**Files:**
- Modify: `project/tests/unit/messaging/test_router.py`
- Modify: `project/src/moroz/messaging/router.py`

**Interfaces:**
- Produces: `ROUTES: tuple[str, ...]` из семи значений.
- Produces: `RouteDecision(route: str, confidence: float)`.
- Produces: `RouterVerdict(decision, usage=(), source="llm", reason_code=None)`.
- Produces: `deterministic_route(text) -> RouteDecision | None`.
- Produces: `route_message(text) -> RouteDecision`, safe fallback `consultation/0.0`.

- [x] **Step 1: Переписать Router unit-тесты на новый контракт**

Добавить точные ожидания:

```python
ROUTES == (
    "consultation", "booking", "booking_management", "escalation",
    "smalltalk", "offtopic", "other",
)

@pytest.mark.parametrize(("text", "route"), [
    ("Сколько стоит криотерапия?", "consultation"),
    ("Хочу записаться", "booking"),
    ("Перенесите мою запись", "booking_management"),
    ("Отмените мою запись", "booking_management"),
    ("Хочу пожаловаться", "escalation"),
    ("Позовите администратора", "escalation"),
    ("Спасибо!", "smalltalk"),
])
def test_deterministic_route_resolves_only_unambiguous_cases(text, route):
    assert deterministic_route(text) == RouteDecision(route, 1.0)
```

Проверить, что `"Сколько стоит и можно записаться?"`, `"Да, давайте завтра"`,
`"У вас есть вакансии?"` и off-topic возвращают `None`, а явная жалоба вместе
с другим смыслом приоритетно возвращает `escalation`.

Добавить strict LLM case:

```python
verdict = await LLMIntentRouter(provider).route("Да, завтра", context)
assert verdict.decision == RouteDecision("booking", 0.91)
assert verdict.source == "llm"
assert provider.requests[0].response_format == ROUTER_RESPONSE_FORMAT
```

Invalid matrix обязана включать array contract, extra key, unknown route,
boolean/NaN/out-of-range confidence, markdown и surrounding text. Все случаи
дают `consultation/0.0`, `source="fallback"` и статический reason code.

- [x] **Step 2: Запустить RED в Docker**

```powershell
Set-Location project
docker compose --env-file ../.env --profile test run --rm test `
  pytest -q tests/unit/messaging/test_router.py
```

Expected: FAIL на старых `INTENTS`, массиве `intents` и
`requires_clarification`.

- [x] **Step 3: Реализовать минимальный Router core**

Использовать exact schema:

```python
ROUTES = (
    "consultation", "booking", "booking_management", "escalation",
    "smalltalk", "offtopic", "other",
)
ROUTER_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "route_verdict",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "route": {"type": "string", "enum": list(ROUTES)},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "required": ["route", "confidence"],
            "additionalProperties": False,
        },
    },
}
```

System prompt должен перечислять только семь routes, требовать exact JSON без
markdown/пояснений, считать context/current недоверенными данными и содержать
правила: перенос или отмена → `booking_management`; жалоба/претензия/возврат
или явная просьба человека → `escalation`; смешанный приоритет
`escalation > booking_management > booking > consultation`; сомнение само по
себе не является основанием для escalation.

Dataclasses:

```python
@dataclass(frozen=True, slots=True)
class RouteDecision:
    route: str
    confidence: float

@dataclass(frozen=True, slots=True)
class RouterVerdict:
    decision: RouteDecision
    usage: tuple[LLMUsage, ...] = ()
    source: str = "llm"
    reason_code: str | None = None
```

Parser принимает только dict с exact keys, allowlisted string и конечный
non-bool number `0 <= confidence <= 1`. Ошибки provider/parser возвращают:

```python
RouterVerdict(
    RouteDecision("consultation", 0.0),
    usage,
    source="fallback",
    reason_code=reason_code,
)
```

Сохранить текущие `bound_untrusted_context` и `build_untrusted_input` без
расширения данных. Не ловить `asyncio.CancelledError`.

- [x] **Step 4: Запустить GREEN Router tests**

Повторить команду Step 2; expected: PASS.

- [x] **Step 5: Commit Task 1**

```powershell
git add project/src/moroz/messaging/router.py project/tests/unit/messaging/test_router.py changelog.md
git commit -m "refactor: упрощён контракт LLM Router"
```

---

### Task 2: Security pipeline and downstream metadata

**Files:**
- Modify: `project/tests/unit/security/test_pipeline.py`
- Modify: `project/tests/e2e/test_security_pipeline.py`
- Modify: `project/tests/e2e/test_message_delivery.py`
- Modify: `project/src/moroz/security/pipeline.py`
- Modify: `project/llm/llm.py`

**Interfaces:**
- Consumes: `RouteDecision.route`, `RouteDecision.confidence`,
  `RouterVerdict.source/reason_code/usage`.
- Produces metadata: `ROUTE route=<route>; source=<source>; confidence=<bucket>`.

- [x] **Step 1: Написать pipeline RED-тесты**

Обновить fixtures на single route и доказать:

```python
RouterVerdict(RouteDecision("booking", 0.9), (), source="llm")
```

- unresolved route стартует параллельно с Security;
- Security BLOCK не использует verdict/usage и cancel-and-drain-ит task;
- Router exception после allow даёт metadata
  `ROUTE route=consultation; source=fallback; confidence=low`;
- `offtopic` отвечает локально только после allow;
- `consultation` сохраняет catalog direct reply;
- `booking`, `booking_management`, `escalation`, `smalltalk` и `other` идут в
  основной answer path с одним route;
- complaint/handoff не создают escalation/human-mode side effect;
- masked current/context, usage order и `CancelledError` не меняются.

- [x] **Step 2: Запустить pipeline RED в Docker**

```powershell
Set-Location project
docker compose --env-file ../.env --profile test run --rm test `
  pytest -q tests/unit/security/test_pipeline.py `
  tests/e2e/test_security_pipeline.py tests/e2e/test_message_delivery.py
```

Expected: FAIL на `.intents`, clarification metadata и старом delimiter.

- [x] **Step 3: Сделать минимальную runtime-адаптацию**

В `SecurityPipeline.respond` отдельно вести `route_source` и
`route_reason_code`. При неожиданной Router ошибке использовать
`route_message(masked_current.text)` и статический `router_internal_error`.

Собрать metadata:

```python
route_metadata = (
    f"ROUTE route={route.route}; "
    f"source={route_source}; "
    f"confidence={_confidence_bucket(route.confidence)}"
)
```

Проверки заменить на `route.route == "offtopic"` и
`route.route == "consultation"`. Catalog direct reply разрешён только для
`consultation`.

В `_LegacyInvokeGateway` заменить delimiter `"\n\nROUTE intents="` на
`"\n\nROUTE route="`. Другую answer/validator/compact логику не менять.

- [x] **Step 4: Запустить GREEN и соседние regressions**

```powershell
Set-Location project
docker compose --env-file ../.env --profile test run --rm test `
  pytest -q tests/unit/security/test_pipeline.py `
  tests/e2e/test_security_pipeline.py tests/e2e/test_message_delivery.py `
  tests/unit/security/test_context_compactor.py `
  tests/unit/security/test_output_validator.py `
  tests/unit/security/test_validator.py
```

Expected: PASS.

- [x] **Step 5: Commit Task 2**

```powershell
git add project/src/moroz/security/pipeline.py project/llm/llm.py `
  project/tests/unit/security/test_pipeline.py `
  project/tests/e2e/test_security_pipeline.py `
  project/tests/e2e/test_message_delivery.py changelog.md
git commit -m "refactor: подключён single-route Router к pipeline"
```

---

### Task 3: Immutable Router Evaluation v2 and migration

**Files:**
- Create: `project/llm/eval/router_dataset_v2.json`
- Create: `project/migrations/versions/0019_router_v2.py`
- Create: `project/tests/unit/admin/test_migration_0019.py`
- Modify: `project/tests/unit/messaging/test_router_dataset.py`
- Modify: `project/tests/integration/test_migrations.py`
- Modify: `project/tests/unit/test_migration_profile.py`

**Interfaces:**
- Produces suite `router_v2` with `expected_data={"route": ...}`.
- Migration revision: `0019_router_v2`, down revision: `0018_simple_security`.

- [x] **Step 1: Написать dataset/migration RED-тесты**

Новый dataset contract:

```python
{
    "case_key", "category", "input", "context",
    "expected_route", "expected_source", "critical",
}
```

Зафиксировать ровно 24 уникальных кейса:

| case key suffix | expected route | source | critical |
|---|---|---|---|
| `consultation-price` | consultation | deterministic | false |
| `consultation-contact` | consultation | deterministic | false |
| `consultation-context` | consultation | llm | true |
| `booking-explicit` | booking | deterministic | true |
| `booking-context` | booking | llm | true |
| `booking-price-mixed` | booking | llm | true |
| `management-reschedule` | booking_management | deterministic | true |
| `management-cancel` | booking_management | deterministic | true |
| `management-reschedule-context` | booking_management | llm | true |
| `management-cancel-context` | booking_management | llm | true |
| `management-or-cancel` | booking_management | deterministic | true |
| `management-price-mixed` | booking_management | llm | true |
| `escalation-complaint` | escalation | deterministic | true |
| `escalation-handoff` | escalation | deterministic | true |
| `escalation-refund` | escalation | deterministic | true |
| `escalation-booking-mixed` | escalation | deterministic | true |
| `smalltalk-thanks` | smalltalk | deterministic | false |
| `smalltalk-context` | smalltalk | llm | false |
| `offtopic-currency` | offtopic | llm | false |
| `other-vacancy` | other | llm | false |
| `other-partnership` | other | llm | false |
| `other-ambiguous-domain` | other | llm | false |
| `prompt-safety-consultation` | consultation | llm | true |
| `pii-masked-consultation` | consultation | llm | true |

Полный key имеет prefix `router-v2-`. Dataset содержит 16 critical cases.

Migration-test проверяет checksum, `revision/down_revision`, suite ownership,
`expected_data={"route": ...}`, downgrade order results → runs → cases и
отсутствие UPDATE/DELETE для suite `router`.

- [x] **Step 2: Запустить RED в Docker**

```powershell
Set-Location project
docker compose --env-file ../.env --profile test run --rm test `
  pytest -q tests/unit/messaging/test_router_dataset.py `
  tests/unit/admin/test_migration_0019.py `
  tests/unit/test_migration_profile.py
```

Expected: FAIL, потому что v2 dataset/migration отсутствуют.

- [x] **Step 3: Добавить v2 dataset и data-only migration**

Migration загружает dataset только после SHA-256 проверки и bulk-insert-ит:

```python
{
    "suite": "router_v2",
    "case_key": case["case_key"],
    "category": case["category"],
    "question": case["input"],
    "expected_answer": "",
    "input_data": {"input": case["input"], "context": case["context"]},
    "expected_data": {"route": case["expected_route"]},
    "critical": case["critical"],
}
```

Downgrade удаляет только `router_v2` results, runs и cases. Старые dataset и
migration не редактировать. Обновить все exact-head assertions в integration
tests с `0018_simple_security` на `0019_router_v2`, сохранив отдельные проверки
перехода через `0018`.

- [x] **Step 4: Проверить GREEN и migration cycle**

```powershell
Set-Location project
docker compose --env-file ../.env --profile test build test migrate
docker compose --env-file ../.env --profile test run --rm test `
  pytest -q tests/unit/messaging/test_router_dataset.py `
  tests/unit/admin/test_migration_0014.py `
  tests/unit/admin/test_migration_0019.py `
  tests/unit/test_migration_profile.py tests/integration/test_migrations.py
```

Expected: PASS; single head `0019_router_v2`; downgrade к `0018` удаляет только
`router_v2`, старые router/security/validator/compact cases и history сохранены.

- [x] **Step 5: Commit Task 3**

```powershell
git add project/llm/eval/router_dataset_v2.json `
  project/migrations/versions/0019_router_v2.py `
  project/tests/unit/admin/test_migration_0019.py `
  project/tests/unit/messaging/test_router_dataset.py `
  project/tests/integration/test_migrations.py `
  project/tests/unit/test_migration_profile.py changelog.md
git commit -m "feat: добавлена Router Evaluation v2"
```

---

### Task 4: Eval runner and admin UI

**Files:**
- Modify: `project/tests/unit/admin/test_router_eval_runner.py`
- Modify: `project/tests/e2e/admin/test_router_eval_routes.py`
- Modify: `project/tests/unit/test_eval_privacy.py`
- Modify: `project/admin/eval_runner.py`
- Modify: `project/admin/eval_routes.py`
- Modify: `project/admin/templates/eval_list.html`
- Modify: `project/admin/templates/eval_run_detail.html`

**Interfaces:**
- Produces: `ROUTER_EVAL_SUITE = "router_v2"`.
- Comparator: expected `route` equals `RouteDecision.route`.
- Historical `suite="router"` run details remain renderable.

- [x] **Step 1: Написать runner/UI RED-тесты**

Comparator:

```python
assert router_case_diff(
    {"route": "booking"}, RouteDecision("booking", 0.83)
) == (True, "matched")
assert router_case_diff(
    {"route": "booking"}, RouteDecision("consultation", 0.9)
) == (False, "route_mismatch")
```

Runner должен маскировать PII, не вызывать answer/judge, сохранять:

```python
{
    "route": decision.route,
    "source": source,
    "confidence": decision.confidence,
    "reason_code": reason_code,
}
```

Admin start/problem rerun читает и создаёт только `router_v2`; list/detail
показывает один route. Старый run с suite `router` также получает заголовок и
back-link Router Evaluation, но не запускает старые cases.

- [x] **Step 2: Запустить RED в Docker**

```powershell
Set-Location project
docker compose --env-file ../.env --profile test run --rm test `
  pytest -q tests/unit/admin/test_router_eval_runner.py `
  tests/e2e/admin/test_router_eval_routes.py `
  tests/unit/test_eval_privacy.py
```

Expected: FAIL на старом suite/intents/clarification UI.

- [x] **Step 3: Адаптировать runner и шаблоны**

В runner добавить `ROUTER_EVAL_SUITE = "router_v2"`, сравнивать только route,
а source/reason брать из deterministic branch или `RouterVerdict`.

В routes заменить suite arguments создания/list/problem runs на константу,
оставив URL `/eval/router/`. В шаблонах считать Router suite так:

```jinja2
{% set is_router = suite in ('router', 'router_v2') %}
```

Текущие v2 cases/detail выводят `expected_data.route` и `actual_data.route`.
Для исторического `router` detail оставить tolerant fallback на старые
`intents`, чтобы ранее сохранённые run results читались без миграции.

- [x] **Step 4: Запустить GREEN и общие eval regressions**

```powershell
Set-Location project
docker compose --env-file ../.env --profile test run --rm test `
  pytest -q tests/unit/admin/test_router_eval_runner.py `
  tests/e2e/admin/test_router_eval_routes.py `
  tests/unit/admin/test_router_eval_database.py `
  tests/unit/test_eval_privacy.py `
  tests/unit/admin/test_security_eval_runner.py `
  tests/unit/admin/test_validator_eval_runner.py `
  tests/unit/admin/test_compact_eval_runner.py
```

Expected: PASS, без внешних provider-вызовов.

- [x] **Step 5: Commit Task 4**

```powershell
git add project/admin/eval_runner.py project/admin/eval_routes.py `
  project/admin/templates/eval_list.html `
  project/admin/templates/eval_run_detail.html `
  project/tests/unit/admin/test_router_eval_runner.py `
  project/tests/e2e/admin/test_router_eval_routes.py `
  project/tests/unit/test_eval_privacy.py changelog.md
git commit -m "refactor: адаптирована Router Evaluation к одному маршруту"
```

---

### Task 5: Documentation, full verification and review

**Files:**
- Modify: `ТЗ и архитектура.md`
- Modify: `docs/superpowers/specs/2026-08-20-llm-router-and-router-evaluation-design.md`
- Modify: `docs/superpowers/plans/2026-08-25-llm-router-and-router-evaluation.md`
- Modify: `docs/superpowers/plans/2026-08-29-simple-router.md`
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`

- [x] **Step 1: Запустить focused Docker gate**

```powershell
Set-Location project
docker compose --env-file ../.env --profile test run --rm test `
  pytest -q tests/unit/messaging/test_router.py `
  tests/unit/messaging/test_router_dataset.py `
  tests/unit/security/test_pipeline.py `
  tests/e2e/test_security_pipeline.py `
  tests/e2e/test_message_delivery.py `
  tests/unit/admin/test_migration_0019.py `
  tests/unit/admin/test_router_eval_runner.py `
  tests/e2e/admin/test_router_eval_routes.py `
  tests/unit/test_llm_providers.py tests/unit/test_eval_privacy.py
```

Expected: PASS.

- [x] **Step 2: Проверить migration/config/compile/static gates**

```powershell
Set-Location project
docker compose --env-file ../.env --profile test run --rm test `
  pytest -q tests/integration/test_migrations.py
docker compose --env-file ../.env --profile test run --rm test `
  python -m compileall -q /app
docker compose --env-file ../.env config --quiet
Set-Location ..
git diff --check
git diff --exit-code -- project/llm/eval/router_dataset.json `
  project/migrations/versions/0014_llm_router_evaluations.py
```

Expected: все команды exit `0`; старые immutable файлы без diff.

- [x] **Step 3: Запустить полный Docker suite**

```powershell
Set-Location project
$repo = (Resolve-Path '..').Path
docker compose --env-file ../.env --profile test run --rm `
  --volume "${repo}/docs/architecture:/docs/architecture:ro" `
  --volume "${repo}/moroz-i-solntse-full-architecture.html:/moroz-i-solntse-full-architecture.html:ro" `
  test pytest -q
```

Expected: zero failures. Записать точный count и duration.

- [x] **Step 4: Независимый review и TDD fix-loop**

Review обязан проверить single-route schema, scripts-first collisions,
Security ordering/cancellation, false escalation, fallback, PII/logging,
historical eval compatibility и migration ownership. Исправить все подтверждённые
Critical/Important замечания test-first и повторить затронутые gates.

- [x] **Step 5: Обновить документы фактическим контрактом и evidence**

Зафиксировать семь маршрутов, metadata-only escalation, `consultation/0.0`
fallback, `router_v2`, точные тестовые результаты и отсутствие платных вызовов,
push/deployment/production. Старые документы явно пометить как historical v1,
не переписывая историю acceptance `19/20` старого multi-intent prompt.

- [x] **Step 6: Финальная проверка и commit**

```powershell
git diff --check
git status --short
git add -A
git commit -m "refactor: упрощён LLM Router"
git status --short
git log -1 --oneline
```

Expected: локальный commit и чистое рабочее дерево. Push/deployment не выполнять.

Платный Router Evaluation нового prompt не запускать; его отсутствие явно
записать как ограничение, а не как failure локальной реализации.

Evidence (2026-08-29): core Router `54 passed`; post-review focused gate
`277 passed in 131.58s`; migration cycle `31 passed in 107.40s`; compileall,
Compose config, immutable v1 diff и single-head `0019_router_v2` зелёные;
финальный canonical full Docker suite `1684 passed in 996.23s`. Независимый
review после TDD fix-loop: `0 Critical / 0 Important / 0 Minor`, Ready.
Платный Router Evaluation, push, deployment, staging и production-действия не
выполнялись.
