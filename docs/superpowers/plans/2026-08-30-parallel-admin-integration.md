# Parallel Admin Branches Integration Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Безопасно объединить пять завершённых локальных веток админки в одно проверенное дерево и только после общего Docker-регресса перенести его в `main`.

**Architecture:** Сборка выполняется в отдельной интеграционной ветке от актуального локального `main`. Ветки вливаются от наименее связных к наиболее конфликтным; три параллельные Alembic migration превращаются в линейную цепочку `0020 → 0021 → 0022`. `main` получает только уже проверенный итог через `--ff-only`.

**Tech Stack:** Git worktrees, Python 3.12, FastAPI/Jinja2, PostgreSQL/Alembic, Docker Compose, pytest.

## Global Constraints

- Не выполнять push, staging, production, реальные LLM/YCLIENTS/Telegram-вызовы.
- Не удалять исходные feature-ветки до проверки уже объединённого `main`.
- Все тесты запускать только через Docker Compose с внешним корневым `.env`.
- При конфликте `changelog.md` и `Дорожная карта.md` сохранять записи всех веток и синтезировать единый актуальный статус; не выбирать файл целиком через ours/theirs.
- При конфликте runtime/tests сохранять обе функциональности и добавлять интеграционный regression, если существующие тесты не фиксируют их совместную работу.

## Проверенный вход

| Порядок | Ветка | HEAD | Миграция | Зафиксированное evidence |
|---:|---|---|---|---|
| 1 | `codex/admin-evaluations-navigation` | `c7966bd` | нет | admin E2E `108 passed` |
| 2 | `codex/admin-zapisi` | `47a7bc5` | нет | focused `195 passed`; PostgreSQL `26 passed` |
| 3 | `codex/message-llm-analytics` | `096d0ac` | `0020_message_llm_analytics` | feature regression `174 passed`; privacy `12 passed` |
| 4 | `codex/admin-reactivation` | `de6fa97` | `0020_admin_reactivation` → перенумеровать в `0021_admin_reactivation` | exact-state `39 passed`; full `1700 passed` + canonical visual `6 passed` |
| 5 | `codex/admin-statistics` | `0128766` | `0020_admin_statistics` → перенумеровать в `0022_admin_statistics` | admin regression `283 passed`; migrations `31 passed` |

Все пять worktree чистые, `git diff --check main...<branch>` проходит, общий merge-base — `32a33bf`.

---

### Task 1: Создать изолированный интеграционный контур

**Files:**
- Create through Git: `.worktrees/admin-integration`
- No source changes.

**Interfaces:**
- Consumes: актуальный локальный `main` с этим планом.
- Produces: ветка `codex/admin-integration-2026-08-30`.

- [x] **Step 1: Зафиксировать точные входные HEAD и чистоту**

```powershell
$mainRoot = 'D:\AI_Projects\moroz_i_solntse\moroz-i-solntse-bot'
$branches = @(
  'codex/admin-evaluations-navigation',
  'codex/admin-zapisi',
  'codex/message-llm-analytics',
  'codex/admin-reactivation',
  'codex/admin-statistics'
)
git -C $mainRoot status --short
foreach ($branch in $branches) {
  git -C $mainRoot log -1 --oneline $branch
  git -C $mainRoot diff --check "main...$branch"
}
```

Expected: все статусы и diff-check чистые; HEAD совпадают с таблицей выше.

- [x] **Step 2: Создать integration branch/worktree**

```powershell
git -C $mainRoot worktree add "$mainRoot\.worktrees\admin-integration" -b codex/admin-integration-2026-08-30 main
```

- [x] **Step 3: Прогнать короткий baseline**

```powershell
$integrationRoot = "$mainRoot\.worktrees\admin-integration"
Set-Location "$integrationRoot\project"
docker compose --env-file ../.env --profile test run --rm --build test pytest -q tests/integration/test_migrations.py tests/e2e/admin/test_public_prefix.py tests/e2e/admin/test_csrf_rbac_audit.py tests/unit/test_worker.py
```

Expected: PASS до первого merge.

---

### Task 2: Влить Evaluations navigation

**Files:**
- Merge: `project/admin/templates/base.html`
- Merge: `project/admin/templates/eval_list.html`
- Merge: `project/admin/static/styles.css`

**Interfaces:**
- Produces: одна sidebar-ссылка `Evaluations / Эвалы` и внутренние подкладки существующих suites.

- [x] **Step 1: Выполнить merge**

```powershell
git merge --no-ff codex/admin-evaluations-navigation -m "merge: объединена навигация evaluations"
```

Expected: runtime-конфликтов нет; `changelog.md`, roadmap и CSS могут потребовать проверки результата auto-merge.

- [x] **Step 2: Проверить навигацию и публичный prefix**

```powershell
Set-Location project
docker compose --env-file ../.env --profile test run --rm --build test pytest -q tests/e2e/admin/test_eval_navigation.py tests/e2e/admin/test_router_eval_routes.py tests/e2e/admin/test_security_eval_routes.py tests/e2e/admin/test_validator_eval_routes.py tests/e2e/admin/test_compact_eval_routes.py tests/e2e/admin/test_public_prefix.py
Set-Location ..
```

Expected: PASS; legacy eval URLs сохранены.

---

### Task 3: Влить недельный календарь «Записи»

**Files:**
- Merge: `project/admin/booking_routes.py`
- Merge: `project/admin/booking_views.py`
- Merge: `project/admin/bookings_database.py`
- Merge: `project/worker/main.py`
- Merge: `project/tests/unit/test_worker.py`

**Interfaces:**
- Produces: weekly YCLIENTS projection calendar и `AdminBookingCommandService` в worker.
- Preserves: Evaluations navigation from Task 2.

- [x] **Step 1: Выполнить merge и проверить общие документы/CSS**

```powershell
git merge --no-ff codex/admin-zapisi -m "merge: добавлен недельный календарь записей"
```

- [x] **Step 2: Прогнать booking/worker gate**

```powershell
Set-Location project
docker compose --env-file ../.env --profile test run --rm --build test pytest -q tests/unit/admin/test_booking_views.py tests/unit/booking/test_admin_commands.py tests/e2e/admin/test_admin_bookings.py tests/integration/admin/test_admin_bookings_postgres.py tests/integration/booking/test_booking_repository.py tests/contract/booking/test_yclients_adapter.py tests/unit/test_worker.py
Set-Location ..
```

Expected: PASS; ручные записи не планируют Telegram-уведомления и retry не создаёт дубль.

---

### Task 4: Влить LLM-аналитику сообщений как migration `0020`

**Files:**
- Merge manually: `project/worker/main.py`
- Merge manually: `project/tests/unit/test_worker.py`
- Merge: `project/admin/app.py`
- Merge: `project/admin/database.py`
- Keep: `project/migrations/versions/0020_message_llm_analytics.py`

**Interfaces:**
- Preserves: `AdminBookingCommandService` wiring from Task 3.
- Produces: exact `token_usage.source_message_id` linkage and per-message admin analytics.

- [x] **Step 1: Выполнить merge без автоматического коммита**

```powershell
git merge --no-ff --no-commit codex/message-llm-analytics
```

- [x] **Step 2: Разрешить worker-конфликт объединением, а не выбором стороны**

Итоговый `worker/main.py` обязан одновременно:

- передавать и сохранять `source_message_id` в `_persist_token_usage`;
- создавать tracked user message через `RETURNING id`;
- принимать `admin_booking_commands` в `MessageTaskHandler`;
- обрабатывать `ADMIN_BOOKING_COMMAND_KINDS`;
- возвращать пять элементов из `_build_yclients_services` и передавать command service в handler.

Итоговый `test_worker.py` обязан сохранять оба набора контрактов.

- [x] **Step 3: Сохранить все CSS/changelog/roadmap additions и завершить merge**

```powershell
git add -A
git commit -m "merge: добавлена LLM-аналитика сообщений"
```

- [x] **Step 4: Устранить будущую add/add коллизию migration-теста**

```powershell
git mv project/tests/unit/admin/test_migration_0020.py project/tests/unit/admin/test_migration_0020_message_llm_analytics.py
git commit -m "test: уточнено имя migration-контракта аналитики"
```

- [x] **Step 5: Прогнать совместный analytics/booking gate**

```powershell
Set-Location project
docker compose --env-file ../.env --profile test run --rm --build test pytest -q tests/unit/admin/test_migration_0020_message_llm_analytics.py tests/unit/admin/test_message_llm_analytics.py tests/unit/booking/test_admin_commands.py tests/unit/test_worker.py tests/integration/test_migrations.py tests/integration/test_worker_usage_postgres.py tests/integration/admin/test_message_llm_analytics_postgres.py tests/integration/admin/test_admin_bookings_postgres.py tests/integration/test_retention_postgres.py tests/integration/admin/test_customer_data_deletion_postgres.py tests/e2e/admin/test_message_llm_analytics_ui.py tests/e2e/admin/test_admin_bookings.py tests/e2e/test_message_delivery.py
Set-Location ..
```

Expected: PASS; `alembic heads` возвращает только `0020_message_llm_analytics`.

---

### Task 5: Влить реактивацию как migration `0021`

**Files:**
- Merge manually: `project/admin/app.py`
- Merge manually: `project/admin/templates/base.html`
- Merge manually: `project/admin/customer_data_deletion.py`
- Merge manually: `project/tests/integration/admin/test_customer_data_deletion_postgres.py`
- Rename: `project/migrations/versions/0020_admin_reactivation.py` → `project/migrations/versions/0021_admin_reactivation.py`
- Rename: `project/tests/unit/admin/test_migration_0020.py` → `project/tests/unit/admin/test_migration_0021_reactivation.py`

**Interfaces:**
- Consumes: `0020_message_llm_analytics`.
- Produces: `0021_admin_reactivation` and owner-only `/reactivation/`.

- [x] **Step 1: Начать merge без коммита**

```powershell
git merge --no-ff --no-commit codex/admin-reactivation
```

- [x] **Step 2: Линеаризовать migration**

```powershell
git mv project/migrations/versions/0020_admin_reactivation.py project/migrations/versions/0021_admin_reactivation.py
git mv project/tests/unit/admin/test_migration_0020.py project/tests/unit/admin/test_migration_0021_reactivation.py
rg -n "0020_admin_reactivation|test_migration_0020" project
```

В файле после rename должны быть точные значения:

```python
revision = "0021_admin_reactivation"
down_revision = "0020_message_llm_analytics"
```

Заменить найденные ссылки реактивации на `0021_admin_reactivation`; общий `test_migrations.py` должен ожидать эту head, но сохранять колонки/FK migration `0020_message_llm_analytics`.

- [x] **Step 3: Разрешить runtime/UI/privacy-конфликты**

Итог обязан сохранять одновременно:

- `summarize_usage_groups` и enrichment chat messages;
- `reactivation_router` import/include;
- одну ссылку Evaluations и owner-only ссылку Reactivation в sidebar;
- удаление linked token usage и всех consent/delivery-данных реактивации;
- CSS всех трёх уже влитых вкладок без дублирующихся правил.

- [x] **Step 4: Завершить merge и проверить**

```powershell
git add -A
git commit -m "merge: добавлена админка реактивации"
Set-Location project
docker compose --env-file ../.env --profile test run --rm --build test pytest -q tests/unit/admin/test_migration_0020_message_llm_analytics.py tests/unit/admin/test_migration_0021_reactivation.py tests/unit/admin/test_reactivation_database_module.py tests/unit/admin/test_reactivation_routes.py tests/integration/test_migrations.py tests/integration/admin/test_reactivation_database.py tests/integration/admin/test_customer_data_deletion_postgres.py tests/e2e/admin/test_message_llm_analytics_ui.py tests/e2e/admin/test_eval_navigation.py
Set-Location ..
```

Expected: PASS; единственная head — `0021_admin_reactivation`.

---

### Task 6: Влить статистику как migration `0022`

**Files:**
- Merge manually: `project/admin/app.py`
- Merge manually: `project/admin/database.py`
- Merge manually: `project/admin/static/styles.css`
- Rename: `project/migrations/versions/0020_admin_statistics.py` → `project/migrations/versions/0022_admin_statistics.py`
- Rename: `project/tests/unit/admin/test_migration_0020_statistics.py` → `project/tests/unit/admin/test_migration_0022_statistics.py`

**Interfaces:**
- Consumes: `0021_admin_reactivation`.
- Produces: `0022_admin_statistics` as the only Alembic head and owner-only period statistics routes.

- [x] **Step 1: Начать merge без коммита и линеаризовать migration**

```powershell
git merge --no-ff --no-commit codex/admin-statistics
git mv project/migrations/versions/0020_admin_statistics.py project/migrations/versions/0022_admin_statistics.py
git mv project/tests/unit/admin/test_migration_0020_statistics.py project/tests/unit/admin/test_migration_0022_statistics.py
rg -n "0020_admin_statistics|test_migration_0020_statistics" project
```

После rename migration должна содержать:

```python
revision = "0022_admin_statistics"
down_revision = "0021_admin_reactivation"
```

Обновить migration test и общий schema/head contract на `0022_admin_statistics`.

- [x] **Step 2: Совместить app/database**

Итоговый `app.py` обязан:

- импортировать `summarize_usage_groups`;
- включать `reactivation_router` и `statistics_router`;
- сохранять chat-detail enrichment;
- удалить старый inline `/stats` route и только действительно неиспользуемый `require_role` import.

Итоговый `database.py` обязан сохранять per-message `usage_groups/llm_usage_state` и новые bounded statistics snapshot/settings queries.

- [x] **Step 3: Завершить merge и прогнать admin/migration gate**

```powershell
git add -A
git commit -m "merge: добавлена периодная статистика"
Set-Location project
docker compose --env-file ../.env --profile test run --rm --build test pytest -q tests/unit/admin/test_migration_0020_message_llm_analytics.py tests/unit/admin/test_migration_0021_reactivation.py tests/unit/admin/test_migration_0022_statistics.py tests/unit/admin/test_stats_calculations.py tests/integration/test_migrations.py tests/integration/admin/test_statistics_postgres.py tests/integration/admin/test_message_llm_analytics_postgres.py tests/integration/admin/test_reactivation_database.py tests/e2e/admin/test_admin_statistics.py tests/e2e/admin/test_csrf_rbac_audit.py tests/e2e/admin/test_public_prefix.py
docker compose --env-file ../.env --profile test run --rm test alembic -c /workspace/alembic.ini heads
Set-Location ..
```

Expected: тесты PASS; вывод Alembic — ровно `0022_admin_statistics (head)`.

---

### Task 7: Общий интеграционный regression и review

**Files:**
- Modify only if a combined regression exposes a real integration defect.
- Update: `Дорожная карта.md`
- Update: `changelog.md`

**Interfaces:**
- Produces: один проверенный integration HEAD без unresolved conflicts, нескольких Alembic heads и потери функциональности.

- [x] **Step 1: Проверить весь diff и отсутствие конфликтных маркеров**

```powershell
git status --short
git diff --check main..HEAD
rg -n "^(<<<<<<<|=======|>>>>>>>)" . -g '!tmp/**'
git diff --name-status main..HEAD
```

Expected: нет незакоммиченных файлов, whitespace errors и conflict markers.

- [x] **Step 2: Проверить Compose, syntax и migration chain**

```powershell
Set-Location project
docker compose --env-file ../.env config --quiet
docker compose --env-file ../.env --profile test run --rm test python -X pycache_prefix=/tmp/admin-integration-pycache -m compileall -q /workspace/admin /workspace/worker /workspace/src /workspace/migrations
docker compose --env-file ../.env --profile test run --rm test alembic -c /workspace/alembic.ini heads
Set-Location ..
```

Expected: exit `0`; ровно одна head `0022_admin_statistics`.

- [x] **Step 3: Прогнать полный canonical Docker suite**

```powershell
Set-Location project
docker compose --env-file ../.env --profile test run --rm --build --volume "..\docs:/docs:ro" --volume "..\moroz-i-solntse-full-architecture.html:/moroz-i-solntse-full-architecture.html:ro" test pytest -q
Set-Location ..
```

Expected: все тесты PASS; никакие внешние provider/YCLIENTS/Telegram endpoints не вызываются.

- [x] **Step 4: Провести ручной local smoke админки**

Проверить owner/admin роли и страницы: Диалоги с message analytics, Записи, Статистика, Реактивация, Evaluations tabs. Проверить отсутствие дублирующихся sidebar-ссылок, корректный public root prefix, CSRF отказ без токена и отсутствие секретов/PII в HTML.

- [x] **Step 5: Зафиксировать exact evidence**

Обновить roadmap/changelog точными test counts, integration HEAD и единственной migration head; commit:

```powershell
git add 'Дорожная карта.md' changelog.md
git commit -m "test: проверена интеграция вкладок админки"
```

---

### Task 8: Перенести проверенное дерево в `main`

**Files:**
- No content edits expected.

**Interfaces:**
- Consumes: полностью проверенный `codex/admin-integration-2026-08-30`.
- Produces: локальный `main` с тем же exact tree.

- [x] **Step 1: Убедиться, что `main` не изменился параллельно**

```powershell
git -C $mainRoot status --short
git -C $mainRoot merge-base --is-ancestor main codex/admin-integration-2026-08-30
```

Expected: clean и exit `0`. Если `main` изменился, остановиться, влить новый `main` в integration branch и повторить Task 7.

- [x] **Step 2: Fast-forward local main**

```powershell
git -C $mainRoot merge --ff-only codex/admin-integration-2026-08-30
```

- [x] **Step 3: Повторить merged-state smoke**

```powershell
Set-Location "$mainRoot\project"
docker compose --env-file ../.env --profile test run --rm --build test pytest -q tests/e2e/admin/test_eval_navigation.py tests/e2e/admin/test_admin_bookings.py tests/e2e/admin/test_message_llm_analytics_ui.py tests/e2e/admin/test_admin_statistics.py tests/unit/admin/test_reactivation_routes.py tests/integration/test_migrations.py tests/unit/test_worker.py
```

Expected: PASS на exact `main`.

- [x] **Step 4: Очистить только integration worktree после успешного smoke**

```powershell
Set-Location $mainRoot
git worktree remove "$mainRoot\.worktrees\admin-integration"
git worktree prune
git branch -d codex/admin-integration-2026-08-30
```

Исходные пять feature-веток оставить до отдельного подтверждения владельца. Push/deploy выполняются отдельной задачей.
