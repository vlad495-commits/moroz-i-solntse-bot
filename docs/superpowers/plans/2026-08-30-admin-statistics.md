# Admin Statistics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить честную owner-only периодную статистику и сохраняемую расчётную оценку экономии оператора.

**Architecture:** `admin/statistics.py` держит чистые периодные/денежные расчёты. `admin/database.py` — единственный SQL-слой period snapshot и singleton-настроек; `app.py` ограничивает доступ, CSRF и audit, `stats.html` рендерит значения.

**Tech Stack:** Python 3.12, FastAPI, Jinja2, PostgreSQL/asyncpg, Alembic, pytest, Docker Compose.

## Global Constraints

- Работать только в `codex/admin-statistics`; без push, merge, deploy, staging и production.
- Проверки: только `cd project && docker compose --env-file ../.env run --rm test pytest ...`.
- Период — включённые даты Europe/Moscow; `owner` — единственная роль чтения и изменения.
- Неизвестный тариф, отсутствующая таблица или настройка всегда дают «Нет данных» с причиной, без fallback.
- Автоматизированные диалоги, часы и рубли подписаны «Расчётная оценка».
- Перед merge сделать rebase и согласовать migration `0020_admin_statistics`: параллельная message-analytics ветка также резервирует номер `0020`.

---

### Task 1: Period, known-only pricing, operator formula

**Files:**
- Create: `project/admin/statistics.py`
- Create: `project/tests/unit/admin/test_statistics.py`

**Interfaces:** Produces `StatisticsPeriod.from_dates(start: date, end: date)`, `calculate_known_usage_cost(rows)`, `calculate_operator_estimate(dialogues, minutes, rate)`.

- [ ] **Step 1: Write failing tests**

```python
def test_period_uses_moscow_day_boundaries():
    p = StatisticsPeriod.from_dates(date(2026, 8, 1), date(2026, 8, 31))
    assert p.starts_at.isoformat() == "2026-07-31T21:00:00+00:00"
    assert p.ends_at.isoformat() == "2026-08-31T21:00:00+00:00"

def test_unknown_model_makes_cost_unavailable():
    assert calculate_known_usage_cost([UsageRow("unknown", 1, 1, 0)]).cost_rub is None

def test_operator_estimate_uses_explicit_formula():
    r = calculate_operator_estimate(3, Decimal("20"), Decimal("600"))
    assert (r.hours, r.savings_rub) == (Decimal("1"), Decimal("600"))
```

- [ ] **Step 2: Verify RED** — `cd project && docker compose --env-file ../.env run --rm test pytest -q tests/unit/admin/test_statistics.py`; expected missing-module FAIL.
- [ ] **Step 3: Implement minimal code**

```python
def from_dates(start, end):
    if end < start: raise ValueError("statistics period")
    return StatisticsPeriod(
        datetime.combine(start, time.min, MOSCOW).astimezone(UTC),
        datetime.combine(end + timedelta(days=1), time.min, MOSCOW).astimezone(UTC),
    )
```

Use explicit rouble tariff mapping by model; reject the whole aggregate if any model is unknown, never use legacy default pricing. Absent settings return `Нет данных: заполните минуты оператора и ставку.`; otherwise compute `dialogues * minutes / 60` and `hours * rate` with `Decimal`.

- [ ] **Step 4: Verify GREEN and commit** — rerun the Step 2 command; expected PASS. Commit `feat: расчёты статистики админки` with both files.

### Task 2: Additive storage and authoritative snapshot

**Files:**
- Create: `project/migrations/versions/0020_admin_statistics.py`
- Modify: `project/admin/database.py`
- Create: `project/tests/unit/admin/test_migration_0020.py`
- Create: `project/tests/integration/admin/test_statistics_postgres.py`

**Interfaces:** Consumes `StatisticsPeriod`. Produces `get_statistics_snapshot(period)`, `get_statistics_settings()`, `save_statistics_settings(minutes, rate)`.

- [ ] **Step 1: Write failing integration tests**

```python
async def test_snapshot_excludes_staff_and_escalated_chats():
    snapshot = await admin_database.get_statistics_snapshot(period)
    assert snapshot["automatic_replies"] == 3
    assert snapshot["automated_dialogues"] == 1

async def test_snapshot_reports_missing_security_table():
    snapshot = await admin_database.get_statistics_snapshot(period)
    assert snapshot["security_incidents"] is None
    assert snapshot["security_incidents_reason"] == "Нет данных: Security-инциденты ещё не сохраняются."
```

- [ ] **Step 2: Verify RED** — `cd project && docker compose --env-file ../.env run --rm test pytest -q tests/unit/admin/test_migration_0020.py tests/integration/admin/test_statistics_postgres.py`; expected absent migration/method FAIL.
- [ ] **Step 3: Implement migration and repository**

```python
op.create_table("admin_statistics_settings",
    sa.Column("id", sa.Boolean(), primary_key=True, server_default=sa.true()),
    sa.Column("minutes_per_dialogue", sa.Numeric(10, 2)),
    sa.Column("hourly_rate_rub", sa.Numeric(12, 2)),
    sa.CheckConstraint("id", name="ck_admin_statistics_settings_singleton"),
    sa.CheckConstraint("minutes_per_dialogue > 0", name="ck_admin_statistics_minutes_positive"),
    sa.CheckConstraint("hourly_rate_rub > 0", name="ck_admin_statistics_rate_positive"))
```

Add only absent created_at indexes for `messages`, `token_usage`, `outbound_messages`, `escalations`. All queries use half-open `$1 <= created_at AND created_at < $2`. Count automatic replies only from sent `reply:%`; automated dialogues are distinct reply chats excluding same-period `escalations.customer_id` and sent `admin_handoff_reply:%`. Call `to_regclass` before querying Security; return its unavailable reason if absent. Return model-grouped usage raw; settings use one `INSERT ... ON CONFLICT (id) DO UPDATE` and positive decimals only.

- [ ] **Step 4: Verify GREEN and commit** — rerun Step 2; expected PASS. Commit `feat: периодные данные статистики`.

### Task 3: Owner routes, CSRF, audit

**Files:**
- Modify: `project/admin/app.py`
- Create: `project/tests/e2e/admin/test_admin_statistics.py`

**Interfaces:** Consumes Tasks 1–2; produces `GET /stats?start=YYYY-MM-DD&end=YYYY-MM-DD` and `POST /stats/settings`.

- [ ] **Step 1: Write failing route tests**

```python
async def test_stats_rejects_admin_before_database_read(monkeypatch):
    monkeypatch.setattr(admin_app, "get_current_user", lambda _: admin_user())
    monkeypatch.setattr(admin_app.database, "get_statistics_snapshot", unexpected_read)
    assert (await client.get("/stats")).status_code == 403

async def test_owner_can_save_settings_with_csrf():
    r = await client.post("/stats/settings", data={"csrf_token":"csrf", "minutes_per_dialogue":"20", "hourly_rate_rub":"600"})
    assert r.status_code == 303
```

- [ ] **Step 2: Verify RED** — `cd project && docker compose --env-file ../.env run --rm test pytest -q tests/e2e/admin/test_admin_statistics.py`; expected missing POST/date FAIL.
- [ ] **Step 3: Implement minimal handlers** — require owner before DB calls; default to first Moscow day of month through today; malformed/reversed dates return 422. POST uses `secrets.compare_digest`, validates positive Decimal, writes settings, audit action `statistics.settings_updated`, object `statistics_settings/singleton`, and redirects 303.
- [ ] **Step 4: Verify GREEN and commit** — rerun Step 2; expected PASS. Commit `feat: owner-настройки статистики`.

### Task 4: Transparent UI, no navigation change

**Files:**
- Modify: `project/admin/templates/stats.html`
- Modify: `project/admin/static/styles.css`
- Modify: `project/tests/e2e/admin/test_admin_statistics.py`

- [ ] **Step 1: Add failing output assertions**

```python
assert 'name="start"' in response.text
assert 'name="minutes_per_dialogue"' in response.text
assert "Автоматизированные диалоги (расчётная оценка)" in response.text
assert "сэкономленные часы = автоматизированные диалоги × минуты оператора / 60" in response.text
assert "Нет данных: Security-инциденты ещё не сохраняются." in response.text
```

- [ ] **Step 2: Verify RED** — rerun Task 3 test command; expected old all-time template FAIL.
- [ ] **Step 3: Implement UI** — retain existing `/stats` item in `base.html`; add GET dates and separate POST settings form with CSRF. Render factual cards, then exact disclosure: `Расчётная оценка: диалог считается автоматизированным, если бот успешно ответил, а в периоде не было эскалации и ответа сотрудника. Это не доказательство полного решения обращения.` Render both formulas and each `Нет данных` reason; operator money uses `₽`.
- [ ] **Step 4: Verify GREEN and commit** — rerun Task 3 test command; expected PASS. Commit `feat: периодная вкладка статистики`.

### Task 5: Docker regression and records

**Files:**
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`

- [ ] **Step 1: Run targeted Docker gate** — `cd project && docker compose --env-file ../.env run --rm test pytest -q tests/unit/admin/test_statistics.py tests/unit/admin/test_migration_0020.py tests/integration/admin/test_statistics_postgres.py tests/e2e/admin/test_admin_statistics.py tests/unit/admin/test_system_metrics.py tests/e2e/admin/test_auth.py`; expected PASS.
- [ ] **Step 2: Run migration/admin regression** — `cd project && docker compose --env-file ../.env run --rm test pytest -q tests/integration/test_migrations.py tests/unit/admin tests/integration/admin tests/e2e/admin`; expected PASS and one Alembic head.
- [ ] **Step 3: Record actual outputs** — mark task in roadmap with exact results/migration and migration/navigation merge risks; append UTC+3 changelog, including no external actions. Commit `docs: зафиксирована проверка статистики`.

## Self-review

- Coverage: Task 1 period/tariff/formulas; Task 2 data sources/settings/unavailable Security; Task 3 RBAC/CSRF/audit; Task 4 transparent copy; Task 5 Docker evidence and docs.
- Type consistency: Task 1 produces `StatisticsPeriod`; Task 2 consumes it and produces snapshot/settings; routes/UI consume those results.
