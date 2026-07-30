# Staging Admin Prompt and Evals Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Перенести из старого серверного контура в staging все 53 eval-кейса, 3 прогона и 90 результатов, добавить старый и текущий промпты в историю версий и сохранить текущий защищённый staging-промпт активным.

**Architecture:** Миграция выполняется точечно между двумя изолированными PostgreSQL-контейнерами на одном сервере. Перед изменениями создаётся полный custom-format backup staging-БД и копии prompt-файлов; eval-данные импортируются одной транзакцией с сохранением ID и синхронизацией sequence. Приложение, сообщения клиентов и production-таблицы не изменяются.

**Tech Stack:** Docker Compose runtime, PostgreSQL 16 (`pg_dump`, `psql`), SSH/Paramiko, FastAPI/Jinja2 admin, Redis prompt history contract.

## Global Constraints

- Работать только с `/opt/moroz-i-solntse-bot` как источником и `/opt/moroz-staging` как целью.
- Не переносить `messages`, `token_usage`, Redis-ключи, admin users/sessions/audit или production-таблицы.
- Не заменять активный `/opt/moroz-staging/project/llm/prompts/system.md`: он содержит старый промпт целиком плюс production canary.
- Любое несовпадение preflight-условий останавливает перенос до записи данных.
- Backup хранить только на сервере в каталоге с mode `0700`; файлы backup — с mode `0600`.
- Не перезапускать bot, worker, admin, PostgreSQL, Redis, RabbitMQ или Caddy.
- Все действия и результаты сразу записывать в `changelog.md`; итог задачи отметить в `Дорожная карта.md`.

---

### Task 1: Preflight и защищённые резервные копии

**Files:**
- Create on server: `/opt/moroz-staging/backups/admin-migration-<UTC timestamp>/staging-before.dump`
- Create on server: `/opt/moroz-staging/backups/admin-migration-<UTC timestamp>/old-evals.sql`
- Create on server: `/opt/moroz-staging/backups/admin-migration-<UTC timestamp>/old-system.md`
- Create on server: `/opt/moroz-staging/backups/admin-migration-<UTC timestamp>/staging-system.md`
- Modify: `changelog.md`

**Interfaces:**
- Consumes: SSH credentials from ignored local `.env`; source and target PostgreSQL container environment.
- Produces: protected rollback directory, exact preflight counts, prompt checksums, exported eval SQL.

- [ ] **Step 1: Resolve exact live targets**

Run read-only checks over SSH:

```bash
cd /opt/moroz-i-solntse-bot && git rev-parse --short=12 HEAD
cd /opt/moroz-staging && git rev-parse --short=12 HEAD
docker inspect -f '{{.Name}} {{.State.Health.Status}}' \
  moroz-i-solntse-bot-postgres-1 moroz-staging-postgres-1 \
  moroz-staging-admin-1 moroz-staging-bot-1 moroz-staging-worker-1
```

Expected: source checkout `ac328049f31f`, staging checkout at the currently deployed commit, all listed containers `healthy`.

- [ ] **Step 2: Verify migration preconditions**

Run exact counts in both PostgreSQL containers:

```sql
SELECT 'eval_cases', count(*) FROM eval_cases
UNION ALL SELECT 'eval_runs', count(*) FROM eval_runs
UNION ALL SELECT 'eval_results', count(*) FROM eval_results
UNION ALL SELECT 'eval_case_reviews', count(*) FROM eval_case_reviews;
```

Expected source counts: `53 / 3 / 90 / 0`.  
Expected staging counts: `0 / 0 / 0 / 0`.

Capture staging counts for every non-eval table as the post-migration invariant.

- [ ] **Step 3: Verify prompt relationship**

Compare the two files by SHA-256 and postрочно. Expected: staging contains every old line plus only the canary prohibition line.

```bash
sha256sum \
  /opt/moroz-i-solntse-bot/project/llm/prompts/system.md \
  /opt/moroz-staging/project/llm/prompts/system.md
```

Stop if the relationship differs from the approved specification.

- [ ] **Step 4: Create protected backup directory**

```bash
backup_dir=/opt/moroz-staging/backups/admin-migration-$(date -u +%Y%m%dT%H%M%SZ)
install -d -m 700 "$backup_dir"
```

Resolve and record the absolute directory. Do not use a broad or unresolved path for later file operations.

- [ ] **Step 5: Back up the full staging database**

```bash
umask 077
docker exec moroz-staging-postgres-1 sh -lc \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
  > "$backup_dir/staging-before.dump"
test -s "$backup_dir/staging-before.dump"
chmod 600 "$backup_dir/staging-before.dump"
```

Verify the archive without restoring:

```bash
docker exec -i moroz-staging-postgres-1 pg_restore --list \
  < "$backup_dir/staging-before.dump" >/dev/null
```

Expected: exit `0`.

- [ ] **Step 6: Back up both prompts and export old eval data**

```bash
install -m 600 /opt/moroz-i-solntse-bot/project/llm/prompts/system.md \
  "$backup_dir/old-system.md"
install -m 600 /opt/moroz-staging/project/llm/prompts/system.md \
  "$backup_dir/staging-system.md"
docker exec moroz-i-solntse-bot-postgres-1 sh -lc \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --data-only --column-inserts \
   --table=public.eval_cases --table=public.eval_runs \
   --table=public.eval_results --table=public.eval_case_reviews' \
  > "$backup_dir/old-evals.sql"
test -s "$backup_dir/old-evals.sql"
chmod 600 "$backup_dir/old-evals.sql"
```

Record file sizes and SHA-256 checksums without printing file contents.

- [ ] **Step 7: Log and commit the completed backup checkpoint**

Update `changelog.md` with the protected backup directory, preflight counts, hashes and verification result. Do not commit secrets or backup contents.

```bash
git add changelog.md
git commit -m "ops: подготовлен backup переноса admin данных"
```

---

### Task 2: Транзакционный импорт eval-данных

**Files:**
- Read on server: `/opt/moroz-staging/backups/admin-migration-<UTC timestamp>/old-evals.sql`
- Modify: `changelog.md`

**Interfaces:**
- Consumes: verified SQL export from Task 1 and empty staging eval tables.
- Produces: 53 cases, 3 runs, 90 results, 0 reviews with valid foreign keys and synchronized sequences.

- [ ] **Step 1: Recheck empty target immediately before import**

Repeat the four staging counts. Expected: all `0`. Stop if any count changed.

- [ ] **Step 2: Import in one transaction**

Pipe the verified dump into target `psql` with a single transaction and fail-fast behavior:

```bash
docker exec -i moroz-staging-postgres-1 sh -lc \
  'psql -v ON_ERROR_STOP=1 -1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  < "$backup_dir/old-evals.sql"
```

Expected: exit `0`; any insert or foreign-key error rolls back the complete import.

- [ ] **Step 3: Synchronize identity sequences**

Run in one transaction:

```sql
BEGIN;
SELECT setval(pg_get_serial_sequence('eval_cases','id'),
              COALESCE((SELECT max(id) FROM eval_cases), 1),
              EXISTS (SELECT 1 FROM eval_cases));
SELECT setval(pg_get_serial_sequence('eval_runs','id'),
              COALESCE((SELECT max(id) FROM eval_runs), 1),
              EXISTS (SELECT 1 FROM eval_runs));
SELECT setval(pg_get_serial_sequence('eval_results','id'),
              COALESCE((SELECT max(id) FROM eval_results), 1),
              EXISTS (SELECT 1 FROM eval_results));
SELECT setval(pg_get_serial_sequence('eval_case_reviews','id'),
              COALESCE((SELECT max(id) FROM eval_case_reviews), 1),
              EXISTS (SELECT 1 FROM eval_case_reviews));
COMMIT;
```

- [ ] **Step 4: Verify counts and referential integrity**

Expected counts: `53 / 3 / 90 / 0`.

```sql
SELECT count(*) FROM eval_results r
LEFT JOIN eval_runs run ON run.id = r.run_id
WHERE run.id IS NULL;

SELECT count(*) FROM eval_results r
LEFT JOIN eval_cases c ON c.id = r.case_id
WHERE r.case_id IS NOT NULL AND c.id IS NULL;
```

Expected: both queries return `0`.

- [ ] **Step 5: Log and commit the eval import checkpoint**

```bash
git add changelog.md
git commit -m "ops: перенесены eval данные в staging"
```

---

### Task 3: История промпта без изменения активного файла

**Files:**
- Read on server: protected `old-system.md` and `staging-system.md`
- Modify: staging table `prompt_versions`
- Modify: `changelog.md`

**Interfaces:**
- Consumes: the two verified prompt backups.
- Produces: idempotent `legacy import` and `staging production` history rows while leaving the active file untouched.

- [ ] **Step 1: Capture the active staging prompt hash**

```bash
sha256sum /opt/moroz-staging/project/llm/prompts/system.md
```

Store the hash for final comparison.

- [ ] **Step 2: Insert both prompt versions idempotently**

Encode each UTF-8 prompt as Base64 locally/in the migration driver and send SQL to target `psql` over standard input:

```sql
BEGIN;
INSERT INTO prompt_versions (content, author, comment)
SELECT convert_from(decode(:'legacy_b64', 'base64'), 'UTF8'),
       'migration',
       'legacy import from /opt/moroz-i-solntse-bot'
WHERE NOT EXISTS (
    SELECT 1 FROM prompt_versions
    WHERE content = convert_from(decode(:'legacy_b64', 'base64'), 'UTF8')
);

INSERT INTO prompt_versions (content, author, comment)
SELECT convert_from(decode(:'staging_b64', 'base64'), 'UTF8'),
       'migration',
       'staging production version with canary'
WHERE NOT EXISTS (
    SELECT 1 FROM prompt_versions
    WHERE content = convert_from(decode(:'staging_b64', 'base64'), 'UTF8')
);
COMMIT;
```

Pass values through `psql --set`, never interpolate raw prompt text into a shell command.

- [ ] **Step 3: Verify history and unchanged active prompt**

Expected:

- exactly one history row for each prompt content;
- comments identify legacy and production versions;
- active staging prompt SHA-256 matches the hash captured in Step 1;
- no Redis publish and no container restart occurred.

- [ ] **Step 4: Log and commit the prompt-history checkpoint**

```bash
git add changelog.md
git commit -m "ops: сохранены версии промпта staging admin"
```

---

### Task 4: Live acceptance and closure

**Files:**
- Modify: `changelog.md`
- Modify: `Дорожная карта.md`

**Interfaces:**
- Consumes: migrated staging database and authenticated staging admin.
- Produces: acceptance evidence, completed roadmap task and rollback-ready handoff.

- [ ] **Step 1: Verify unchanged non-eval data**

Compare the current counts for every non-eval table with the preflight snapshot. Expected: no changes except two new `prompt_versions` rows and audit/session activity caused by verification.

- [ ] **Step 2: Verify the live admin**

Using the authenticated in-app browser:

- `/admin/eval/` shows 53 cases and 3 historical runs;
- each of the 3 run detail pages opens and their combined result count is 90;
- `/admin/prompt/` shows the active staging prompt and both imported history versions;
- existing navigation pages continue to return successfully.

- [ ] **Step 3: Verify runtime health**

```bash
docker inspect -f '{{.Name}} {{.State.Health.Status}} {{.RestartCount}}' \
  moroz-staging-admin-1 moroz-staging-bot-1 moroz-staging-worker-1 \
  moroz-staging-postgres-1 moroz-staging-redis-1 \
  moroz-staging-rabbitmq-1 moroz-staging-caddy-1
```

Expected: every container `healthy`; restart counts unchanged from preflight.

- [ ] **Step 4: Close documentation**

Mark the consolidation task complete in `Дорожная карта.md`. Record exact counts, prompt hashes, live UI checks and protected rollback directory in `changelog.md`.

- [ ] **Step 5: Run final repository checks**

```bash
git diff --check
git status --short
```

Expected: only the intended documentation changes before commit.

- [ ] **Step 6: Commit closure**

```bash
git add changelog.md "Дорожная карта.md"
git commit -m "ops: завершен перенос старой админки в staging"
```

