# New YCLIENTS Sandbox Account Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Подготовить пустой test-only аккаунт YCLIENTS, доказать безопасный GET-only доступ к каталогу, слотам и records, а после отдельного разрешения выполнить один bounded lifecycle с cleanup.

**Architecture:** Кабинет получает минимальный синтетический каталог через авторизованный браузер. Новый Docker entrypoint выполняет только GET и не содержит mutation-методов; существующий `yclients-smoke` остаётся единственным permission-gated lifecycle runner. Credentials живут только во внешнем ignored `.env`, evidence остаётся санитизированным.

**Tech Stack:** Python 3.12, Docker Compose, pytest, `YclientsHttpClient`, `YclientsAdapter`, YCLIENTS web cabinet/API.

## Global Constraints

- Все проектные команды и тесты запускать только через Docker Compose.
- Аккаунт — только sandbox; реальные клиенты, ПД и коммерческие записи запрещены.
- Tokens не выводить в чат, Git, terminal output, логи или evidence.
- Сначала разрешены UI-настройка тестовых сущностей и GET-only API calls.
- Перед новым POST/PUT/DELETE требуется отдельное явное разрешение пользователя.
- Любой auth/transport/envelope/match failure останавливает flow fail-closed.
- После логического шага сразу обновлять roadmap/changelog и делать локальный commit; push запрещён.
- Telegram booking и scheduler в staging не включать автоматически.

---

### Task 1: Отдельный GET-only records preflight

**Files:**
- Create: `project/src/moroz/booking/yclients_sandbox_preflight.py`
- Create: `project/tests/unit/booking/test_yclients_sandbox_preflight.py`
- Modify: `project/docker-compose.yml`
- Modify: `project/tests/unit/test_migration_profile.py`
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`

**Interfaces:**
- Consumes: `YclientsConfig.from_env()`, `YclientsSmokeBackend.list_services()`, `list_slots()`, `reconcile_booking_key()`.
- Produces: `SandboxPreflightSettings.from_env()`, `run_preflight() -> PreflightResult`, module CLI и Compose profile `yclients-sandbox-preflight`.
- Safety: backend protocol не содержит create/reschedule/cancel; runtime transport выполняет только GET.

- [ ] **Step 1: Написать RED unit-контракты**

```python
def test_settings_require_read_credentials_and_exact_sandbox_marker():
    settings = SandboxPreflightSettings.from_env(_env())
    assert settings.service_id == "331"
    assert settings.window_days == 14


@pytest.mark.asyncio
async def test_preflight_reads_services_slots_and_records_without_mutation():
    backend = FakeReadBackend()
    result = await run_preflight(
        SandboxPreflightSettings.from_env(_env()),
        backend=backend,
        now=lambda: NOW,
        uuid_factory=lambda: RUN_ID,
    )
    assert result.exit_code == 0
    assert backend.calls == ["list_services", "list_slots", "preflight_records"]
    assert result.summary["matches"] == 0
    assert result.summary["active_matches"] == 0
```

Также добавить cases: missing token/company/service, non-exact `sandbox`, window `0/15`, меньше двух слотов, records error/malformed/mismatch, sanitized CLI output.

- [ ] **Step 2: Запустить RED только через Docker**

```powershell
docker compose -p moroz-yclients-new-sandbox --env-file ../tmp/task7.env --profile test run -T --rm test pytest -q tests/unit/booking/test_yclients_sandbox_preflight.py tests/unit/test_migration_profile.py
```

Expected: FAIL из-за отсутствующего module/profile; внешних API calls нет.

- [ ] **Step 3: Реализовать минимальный GET-only entrypoint**

```python
@dataclass(frozen=True, slots=True)
class SandboxPreflightSettings:
    config: YclientsConfig = field(repr=False)
    service_id: str
    window_days: int

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "SandboxPreflightSettings":
        if env.get("YCLIENTS_ENVIRONMENT_LABEL", "") != "sandbox":
            raise ValueError("YCLIENTS_ENVIRONMENT_LABEL=sandbox is required")
        service_id = env.get("YCLIENTS_TEST_SERVICE_ID", "").strip()
        window = env.get("YCLIENTS_TEST_WINDOW_DAYS", "").strip()
        if not service_id.isdigit() or int(service_id) <= 0 or str(int(service_id)) != service_id:
            raise ValueError("YCLIENTS_TEST_SERVICE_ID must be a positive integer")
        if not window.isdigit() or not 1 <= int(window) <= 14:
            raise ValueError("YCLIENTS_TEST_WINDOW_DAYS must be an integer from 1 to 14")
        return cls(YclientsConfig.from_env(env), service_id, int(window))
```

`run_preflight` обязан: прочитать exact service, получить два различных будущих slot, выполнить records reconciliation по свежему UUID, принять только `matches=0/active_matches=0`, вернуть один фиксированный JSON. Любое исключение превращается в санитизированный non-zero; mutation methods отсутствуют.

- [ ] **Step 4: Добавить отдельный Compose service**

```yaml
  yclients-sandbox-preflight:
    profiles: ["yclients-sandbox-preflight"]
    image: "${COMPOSE_PROJECT_NAME:-moroz-i-solntse}-worker:local"
    build:
      context: .
      dockerfile: worker/Dockerfile
    restart: "no"
    command: ["python", "-m", "moroz.booking.yclients_sandbox_preflight"]
    environment:
      YCLIENTS_PARTNER_TOKEN: ${YCLIENTS_PARTNER_TOKEN:-}
      YCLIENTS_USER_TOKEN: ${YCLIENTS_USER_TOKEN:-}
      YCLIENTS_COMPANY_ID: ${YCLIENTS_COMPANY_ID:-}
      YCLIENTS_BASE_URL: ${YCLIENTS_BASE_URL:-}
      YCLIENTS_TIMEZONE: ${YCLIENTS_TIMEZONE:-}
      YCLIENTS_TIMEOUT_SECONDS: ${YCLIENTS_TIMEOUT_SECONDS:-}
      YCLIENTS_TEST_SERVICE_ID: ${YCLIENTS_TEST_SERVICE_ID:-}
      YCLIENTS_ENVIRONMENT_LABEL: ${YCLIENTS_ENVIRONMENT_LABEL:-}
      YCLIENTS_TEST_WINDOW_DAYS: ${YCLIENTS_TEST_WINDOW_DAYS:-}
```

- [ ] **Step 5: Запустить GREEN и safety regression**

```powershell
docker compose -p moroz-yclients-new-sandbox --env-file ../tmp/task7.env --profile test run -T --rm test pytest -q tests/unit/booking/test_yclients_sandbox_preflight.py tests/unit/booking/test_yclients_sandbox_smoke.py tests/contract/booking/test_yclients_adapter.py tests/unit/test_migration_profile.py
```

Expected: PASS; lifecycle runner по-прежнему требует exact consent.

- [ ] **Step 6: Проверить runtime и закоммитить**

```powershell
docker compose -p moroz-yclients-new-sandbox --env-file ../tmp/task7.env --profile yclients-sandbox-preflight config --quiet
docker compose -p moroz-yclients-new-sandbox --env-file ../tmp/task7.env build yclients-sandbox-preflight
docker compose -p moroz-yclients-new-sandbox --env-file ../tmp/task7.env run -T --rm --no-deps yclients-sandbox-preflight python -m compileall -q /app
git diff --check
```

Expected: все четыре команды exit `0`.

```powershell
git add project/src/moroz/booking/yclients_sandbox_preflight.py project/tests/unit/booking/test_yclients_sandbox_preflight.py project/docker-compose.yml project/tests/unit/test_migration_profile.py "Дорожная карта.md" changelog.md
git commit -m "feat: добавлен GET-only preflight YCLIENTS sandbox"
```

---

### Task 2: Создать минимальные сущности в пустом YCLIENTS

**Files:**
- Modify: `docs/testing/telegram-yclients-booking-test-plan.md`
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`

**Interfaces:**
- Consumes: авторизованная браузерная сессия нового test-only YCLIENTS.
- Produces: один company ID, один service ID, один staff ID, минимум два разных будущих слота в 14-дневном окне.

- [ ] **Step 1: Выполнить read-only browser preflight**

Проверить новый аккаунт, отсутствие реальных клиентов/записей и доступность разделов филиала, услуг, сотрудников и расписания. Не читать cookies, browser storage, пароли и unrelated tabs.

- [ ] **Step 2: Создать синтетический каталог**

```text
Филиал: Moroz API Sandbox
Услуга: Тестовая услуга API
Длительность: 60 минут
Сотрудник: Тестовый мастер
Клиенты: не создавать
```

Связать услугу только с тестовым сотрудником. Не копировать данные заказчика.

- [ ] **Step 3: Настроить расписание**

Открыть минимум два различных будущих слота по 60 минут в ближайшие 14 дней и визуально подтвердить связь услуги с сотрудником.

- [ ] **Step 4: Записать санитизированное evidence и commit**

Tracked docs содержат только `companies=1 services=1 staff=1 slots>=2` и timestamp. Exact IDs остаются во внешнем `.env`.

```powershell
git add docs/testing/telegram-yclients-booking-test-plan.md "Дорожная карта.md" changelog.md
git commit -m "docs: подготовлен пустой YCLIENTS sandbox"
```

---

### Task 3: Подготовить credentials и allowlists

**Files:**
- Modify outside Git: `D:/AI_Projects/moroz_i_solntse/moroz-i-solntse-bot/.env`
- Modify: `changelog.md` только санитизированным статусом.

**Interfaces:**
- Consumes: существующий Partner Token API-приложения, новый User Token аккаунта, company/service/staff IDs.
- Produces: exact `YCLIENTS_COMPANY_ID`, `YCLIENTS_TEST_SERVICE_ID`, service/staff allowlists, label `sandbox`, window `14`.

- [ ] **Step 1: Получить User Token официальным flow YCLIENTS**

Если нужны password/CAPTCHA/email/SMS/2FA, остановиться на этом экране и попросить пользователя пройти проверку. Пароль не читать и не сохранять.

- [ ] **Step 2: Записать secrets напрямую во внешний `.env`**

Если token нельзя безопасно перенести без показа в tool output, пользователь вставляет его непосредственно в `YCLIENTS_USER_TOKEN=`. Остальные несекретные IDs и allowlists заполняет агент.

- [ ] **Step 3: Проверить только invariants без вывода values**

Expected sanitized status: `required=8 missing=0 duplicates=0 partner_ne_user=true label=sandbox window=14`.

- [ ] **Step 4: Обновить changelog**

Записать только status/counts; `.env` не добавлять в Git.

```powershell
git add changelog.md
git commit -m "config: подключён новый YCLIENTS sandbox"
```

---

### Task 4: Выполнить внешний GET-only gate

**Files:**
- Modify: `docs/testing/telegram-yclients-booking-test-plan.md`
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`

**Interfaces:**
- Consumes: два GET-only Compose profiles и внешний ignored `.env`.
- Produces: санитизированные catalog/slot counts и records `matches=0/active_matches=0` либо fail-closed non-zero.

- [ ] **Step 1: Запустить partner catalog GET**

```powershell
docker compose -p moroz-yclients-new-sandbox --env-file "D:\AI_Projects\moroz_i_solntse\moroz-i-solntse-bot\.env" --profile yclients-readonly run -T --rm yclients-readonly
```

Expected: exit `0`, environment `sandbox`, service/staff `1/1`, slots `>=2`.

- [ ] **Step 2: Запустить User Token records GET**

```powershell
docker compose -p moroz-yclients-new-sandbox --env-file "D:\AI_Projects\moroz_i_solntse\moroz-i-solntse-bot\.env" --profile yclients-sandbox-preflight run -T --rm yclients-sandbox-preflight
```

Expected: exit `0`, `success=true`, `matches=0`, `active_matches=0`. Любой non-zero останавливает этап.

- [ ] **Step 3: Запустить affected local regression**

```powershell
docker compose -p moroz-yclients-new-sandbox --env-file ../tmp/task7.env --profile test run -T --rm test pytest -q tests/unit/booking/test_yclients_sandbox_preflight.py tests/unit/booking/test_yclients_sandbox_smoke.py tests/contract/booking tests/e2e/booking/test_yclients_fail_closed.py
```

Expected: PASS.

- [ ] **Step 4: Записать evidence и выполнить exact cleanup**

До commit удалить только namespace `moroz-yclients-new-sandbox`, затем проверить counts `containers=0 volumes=0 networks=0 images=0` и сразу записать их в `changelog.md`.

```powershell
docker compose -p moroz-yclients-new-sandbox --env-file ../tmp/task7.env --profile test down -v --rmi local --remove-orphans
```

- [ ] **Step 5: Закоммитить GET-only evidence вместе с cleanup result**

```powershell
git add docs/testing/telegram-yclients-booking-test-plan.md "Дорожная карта.md" changelog.md
git commit -m "test: подтверждён GET-only доступ нового YCLIENTS sandbox"
```

---

### Task 5: Permission-gated lifecycle

**Files:**
- Modify after run: `docs/testing/telegram-yclients-booking-test-plan.md`
- Modify after run: `Дорожная карта.md`
- Modify after run: `changelog.md`

**Interfaces:**
- Consumes: успешный Task 4 и новое явное mutation-разрешение.
- Produces: полный lifecycle с `matches=1`, `active_matches=0` либо fail-closed evidence.

- [ ] **Step 1: Остановиться и запросить точное разрешение**

```text
Разрешаю один sandbox lifecycle в новом test-only YCLIENTS аккаунте с фейковыми данными и bounded cleanup.
```

- [ ] **Step 2: После разрешения выполнить одну команду**

```powershell
docker compose -p moroz-yclients-new-sandbox --env-file "D:\AI_Projects\moroz_i_solntse\moroz-i-solntse-bot\.env" --profile yclients-smoke run -T --rm yclients-smoke
```

Expected: create/get/reschedule/get/cancel confirmed, final deleted/cancelled, `matches=1`, `active_matches=0`. Non-zero не повторять вслепую.

- [ ] **Step 3: Запустить booking/reminder regression**

```powershell
docker compose -p moroz-yclients-new-sandbox --env-file ../tmp/task7.env --profile test run -T --rm test pytest -q tests/unit/booking tests/contract/booking tests/integration/booking tests/e2e/booking tests/e2e/notifications/test_booking_flow_reminders.py
```

- [ ] **Step 4: Записать evidence, cleanup и commit**

Сначала удалить exact namespace и записать проверенные counts `0/0/0/0` в `changelog.md`, затем создать один evidence commit.

```powershell
git add docs/testing/telegram-yclients-booking-test-plan.md "Дорожная карта.md" changelog.md
git commit -m "test: подтверждён lifecycle нового YCLIENTS sandbox"
```

- [ ] **Step 5: Не включать staging автоматически**

После успеха отдельно согласовать limited Telegram smoke, booking enable и затем scheduler/reminders.
