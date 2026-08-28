# Remove Review Cases Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Полностью удалить модуль «Review кейсов» из админки и удалить его таблицу PostgreSQL, не меняя другие функции.

**Architecture:** Удаление идёт по двум независимым границам: HTTP/UI-модуль перестаёт регистрироваться и исчезает из шаблонов, затем additive Alembic-миграция удаляет принадлежащую ему таблицу. Историческую baseline-миграцию `0001` не переписываем: развёрнутые базы должны штатно перейти с `0012` на новый head.

**Tech Stack:** Python 3.12, FastAPI, Jinja2, Alembic, PostgreSQL, pytest, Docker Compose.

> Release-коррекция 2026-08-28: Task 2 superseded для совместимости image-only
> rollback. Revision `0013` сохраняет неиспользуемую таблицу и её строки; удалить
> storage можно отдельной contract migration только после истечения rollback-window
> предыдущего admin image. Runtime/UI-код модуля остаётся удалённым.

## Global Constraints

- Менять только модуль «Review кейсов» и его таблицу `eval_case_reviews`.
- Не менять технические Evaluations, «Записи», guardrails со значением `review` и другие разделы.
- Все проверки запускать только через Docker Compose.
- Не добавлять зависимости и новые абстракции.

---

### Task 1: Удалить HTTP/UI-модуль Review Cases

**Files:**
- Modify: `project/tests/e2e/admin/test_csrf_rbac_audit.py`
- Modify: `project/admin/app.py`
- Modify: `project/admin/templates/base.html`
- Modify: `project/admin/static/styles.css`
- Delete: `project/admin/review_routes.py`
- Delete: `project/admin/review_database.py`
- Delete: `project/admin/templates/review_eval_list.html`

**Interfaces:**
- Consumes: существующий FastAPI `admin_app.app` и базовый шаблон админки.
- Produces: приложение без маршрутов `/review/...` и меню без ссылки «Review кейсов».

- [ ] **Step 1: Написать падающий тест**

Добавить проверку:

```python
def test_review_cases_module_is_not_exposed():
    base = (admin_app._BASE_DIR / "templates" / "base.html").read_text(
        encoding="utf-8"
    )
    paths = {route.path for route in admin_app.app.routes}

    assert "Review кейсов" not in base
    assert not any(path.startswith("/review") for path in paths)
```

- [ ] **Step 2: Запустить RED через Docker**

Run: `docker compose --env-file ../.env run --rm --no-deps test pytest tests/e2e/admin/test_csrf_rbac_audit.py::test_review_cases_module_is_not_exposed -q`

Expected: `FAIL`, потому что ссылка и `/review` ещё зарегистрированы.

- [ ] **Step 3: Выполнить минимальное удаление**

Удалить import/include `review_router` из `admin/app.py`, ссылку из `base.html`, три файла модуля и только review-specific CSS-селекторы. Сохранить `.review-tabs` и `.review-tab`, потому что их использует `bookings.html`; в групповых селекторах убрать только `.review-*`, не меняя остальные декларации.

- [ ] **Step 4: Запустить GREEN через Docker**

Run: `docker compose --env-file ../.env run --rm --no-deps test pytest tests/e2e/admin/test_csrf_rbac_audit.py::test_review_cases_module_is_not_exposed -q`

Expected: `1 passed`.

---

### Task 2: Удалить таблицу review-кейсов миграцией

**Files:**
- Create: `project/migrations/versions/0013_remove_eval_case_reviews.py`
- Modify: `project/tests/integration/test_migrations.py`

**Interfaces:**
- Consumes: Alembic revision `0012_projection_suppression` и существующую таблицу `eval_case_reviews`.
- Produces: revision `0013_remove_eval_case_reviews`, где актуальная схема не содержит таблицу.

- [ ] **Step 1: Написать падающий migration-тест**

Добавить тест, который поднимает БД до `0012_projection_suppression`, вставляет review-запись, обновляет до `head` и проверяет отсутствие таблицы:

```python
async def test_review_cases_table_is_removed(disposable_database_url):
    run_alembic(disposable_database_url, "upgrade", "0012_projection_suppression")
    conn = await asyncpg.connect(disposable_database_url)
    try:
        await conn.execute("INSERT INTO eval_case_reviews DEFAULT VALUES")
    finally:
        await conn.close()

    run_alembic(disposable_database_url, "upgrade", "head")
    conn = await asyncpg.connect(disposable_database_url)
    try:
        assert await conn.fetchval("SELECT to_regclass('public.eval_case_reviews')") is None
        assert await conn.fetchval("SELECT version_num FROM alembic_version") == (
            "0013_remove_eval_case_reviews"
        )
    finally:
        await conn.close()
```

- [ ] **Step 2: Запустить RED через Docker**

Run: `docker compose --env-file ../.env run --rm test pytest tests/integration/test_migrations.py::test_review_cases_table_is_removed -q`

Expected: `FAIL`, таблица всё ещё существует и head равен `0012_projection_suppression`.

- [ ] **Step 3: Добавить минимальную миграцию**

```python
"""Remove the retired eval case review module storage."""

from alembic import op
import sqlalchemy as sa

revision = "0013_remove_eval_case_reviews"
down_revision = "0012_projection_suppression"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("eval_case_reviews")


def downgrade() -> None:
    op.create_table(
        "eval_case_reviews",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("case_id", sa.BigInteger(), sa.ForeignKey("eval_cases.id", ondelete="CASCADE")),
        sa.Column("status", sa.String(32), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("reviewer", sa.String(64)),
        sa.Column("comment", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("proposed_question", sa.Text()),
        sa.Column("proposed_answer", sa.Text()),
        sa.Column("category", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("idx_eval_case_reviews_case_id", "eval_case_reviews", ["case_id"], unique=True, postgresql_where=sa.text("case_id IS NOT NULL"))
    op.create_index("idx_eval_case_reviews_status", "eval_case_reviews", ["status", sa.text("updated_at DESC")])
```

- [ ] **Step 4: Запустить GREEN и затронутые проверки через Docker**

Run: `docker compose --env-file ../.env run --rm test pytest tests/integration/test_migrations.py::test_review_cases_table_is_removed tests/e2e/admin/test_csrf_rbac_audit.py -q`

Expected: все выбранные тесты проходят.

---

### Task 3: Завершить документацию и общий gate

**Files:**
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`

**Interfaces:**
- Consumes: результаты Task 1–2.
- Produces: закрытая задача дорожной карты и запись фактических проверок.

- [ ] **Step 1: Проверить остаточные ссылки**

Run: `rg -n "Review кейсов|review_eval_list|review_routes|review_database|/review/evals" project`

Expected: совпадений в активном runtime-коде нет.

- [ ] **Step 2: Запустить полный Docker test gate**

Run: `docker compose --env-file ../.env run --rm test pytest -q`

Expected: `0 failed`, `0 errors`.

- [ ] **Step 3: Обновить документы проекта**

Отметить задачу удаления выполненной в `Дорожная карта.md` и дописать в `changelog.md` удалённые файлы, migration head и точные результаты тестов.

- [ ] **Step 4: Зафиксировать логический шаг локальным коммитом**

```bash
git add project/admin project/migrations/versions/0013_remove_eval_case_reviews.py project/tests Дорожная\ карта.md changelog.md
git commit -m "feat: удалить review кейсы из админки"
```

Не выполнять push или staging/production rollout без отдельного запроса.
