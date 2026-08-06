# Admin Operations Release Candidate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Собрать один проверяемый release candidate из актуального `main`, Work 3 и staging `/start` recovery, устранить `STAGING_IMAGE_TAG` drift и подтвердить существующее операционное ядро админки без production и P2-экранов.

**Architecture:** Локальная ветка начинается с recovery-коммита `ce81786` и сохраняет историю `main`; Work 3 интегрируется обычным merge с осмысленным разрешением конфликтов. Staging получает точный commit через Git bundle без push, все app-образы собираются из одного checkout и получают один immutable tag. Секретный `START_REPLY` остаётся server-only конфигурацией; release identity включает commit, image tag и безопасный SHA-256 fingerprint конфигурации.

**Tech Stack:** Git, Python 3.12, Docker Compose, FastAPI/Jinja2, PostgreSQL, Redis, RabbitMQ, Caddy, pytest, POSIX shell.

## Global Constraints

- Только Docker для запуска и проверок проекта.
- Production не трогать; rollout ограничен Compose project `moroz-staging` и `/opt/moroz-staging`.
- Не push-ить `main` или release-ветку без отдельного запроса.
- Не менять содержимое системного промпта и клиентские данные.
- Не раскрывать `.env`, TOTP seed, пароли, токены или полные логи.
- До rollout обязательны clean preflight, точный rollback bundle и свежие Docker gates.
- Bootstrap отключается только после доказанного входа индивидуального owner; если безопасная передача TOTP невозможна, этот шаг остаётся human gate.
- P2-экраны эскалаций, записей, базы знаний и рассылок не входят в работу.

---

### Task 1: Release Line Integration

**Files:**
- Modify: `AGENTS.md`
- Modify: `changelog.md`
- Modify: `Дорожная карта.md`
- Preserve: `Ручное тестирование человеком 2.md`

**Interfaces:**
- Consumes: `ce81786`, `codex/work3-integration=0df3ba9`.
- Produces: один clean local release-candidate commit, содержащий обе истории.

- [ ] Сохранить read-only инвентарь родителей, runtime diff и staging recovery evidence.
- [ ] Выполнить merge `codex/work3-integration` в `codex/admin-ops-rc` без squash.
- [ ] Разрешить конфликты в пользу актуального Work 3 runtime и актуальных main/recovery QA-документов; объединить changelog и roadmap без потери записей.
- [ ] Проверить `git diff --check`, отсутствие изменений `project/llm/prompts/system.md` и наличие обоих родителей в истории.
- [ ] Зафиксировать логический шаг отдельным локальным коммитом merge.

### Task 2: Durable Staging Image Pin

**Files:**
- Create: `project/ops/pin-staging-image-tag.sh`
- Modify: `project/ops/staging-runbook.md`
- Modify: `project/tests/unit/test_staging.py`
- Modify: `changelog.md`
- Modify: `Дорожная карта.md`

**Interfaces:**
- Consumes: server `.env` path and validated immutable image tag.
- Produces: `pin-staging-image-tag.sh ENV_PATH TAG`, which atomically replaces or appends only `STAGING_IMAGE_TAG`, preserves file mode, and fails closed on an invalid path/tag.

- [ ] Добавить failing contract test: runbook требует persistent pin и post-rollout equality checkout/tag/runtime.
- [ ] Запустить focused test в Docker и подтвердить RED из-за отсутствующего script/contract.
- [ ] Реализовать минимальный POSIX shell script с temporary file в том же каталоге, `awk`, `chmod --reference` и atomic `mv`.
- [ ] Добавить shell integration test на replace/add/invalid tag без чтения секретных значений в вывод.
- [ ] Запустить focused Docker test и подтвердить GREEN.
- [ ] Обновить runbook точным preflight/pin/verification/rollback порядком.
- [ ] Зафиксировать логический шаг локальным коммитом.

### Task 3: Local Candidate Verification

**Files:**
- Modify: `changelog.md`
- Modify: `Дорожная карта.md`

**Interfaces:**
- Consumes: clean RC checkout.
- Produces: immutable tag `admin-ops-rc-<12-char-commit>` и локальные доказательства.

- [ ] Запустить pinned Ruff `E9,F`, compileall и Compose render через Docker.
- [ ] Запустить focused admin/start/prompt/log/metrics/ops tests через Docker.
- [ ] Запустить полный непересекающийся Docker pytest suite и проверить `0 failed`.
- [ ] Собрать `bot`, `worker`, `admin`, `migrate` из exact checkout с одним tag.
- [ ] Проверить image users, image IDs, source checksums и отсутствие secret-shaped данных в metadata/history.
- [ ] Выполнить independent code review; закрыть Critical/Important findings через TDD.
- [ ] Зафиксировать verification evidence локальным коммитом.

### Task 4: Staging Rollout And Drift Closure

**Files:**
- Modify: server checkout `/opt/moroz-staging` through Git fetch from a protected Git bundle.
- Modify: server-only `/opt/moroz-staging/.env` only through the tracked pin script; keep approved `START_REPLY` unchanged.
- Create: protected rollback bundle under `/opt/moroz-staging-state/rollbacks/`.
- Modify: `changelog.md`
- Modify: `Дорожная карта.md`

**Interfaces:**
- Consumes: verified RC commit/tag and current exact staging image/config inventory.
- Produces: checkout SHA = RC commit; `STAGING_IMAGE_TAG` = RC tag; bot/worker/admin/migrate image tags = RC tag.

- [ ] Повторить read-only preflight: clean tracked checkout, current images/IDs, schema, health, disk, backup paths and current config fingerprints.
- [ ] Создать внешний mode-700 rollback bundle с Git/status/image/config checksums без секретов.
- [ ] Доставить RC как Git bundle, fetch exact commit и checkout только после reverse/rollback checks.
- [ ] Собрать exact candidate images, проверить migration compatibility и не запускать schema downgrade.
- [ ] Атомарно закрепить `STAGING_IMAGE_TAG` новым script, сохранив approved `START_REPLY` fingerprint.
- [ ] Пересоздать только app services `bot worker admin` из одного tag; stores и Caddy не пересоздавать.
- [ ] При несовпадении SHA/tag/runtime или health немедленно выполнить exact rollback.
- [ ] Зафиксировать rollout evidence локальным коммитом.

### Task 5: Operational Admin Technical Acceptance

**Files:**
- Modify: `changelog.md`
- Modify: `Дорожная карта.md`

**Interfaces:**
- Consumes: unified staging RC and protected bootstrap credentials.
- Produces: technical acceptance matrix and explicit human gates.

- [ ] Проверить anonymous/expired auth для login, `/logs/tail`, metrics и SSE.
- [ ] Через защищённую временную owner-сессию проверить dialogs, chat detail audit, stats, logs and metrics.
- [ ] Сохранить текущее содержимое prompt, выполнить same-content save с reload ACK, затем rollback; проверить file/runtime hashes и audit rows без вывода prompt.
- [ ] Выполнить pause → paused evidence → unpause и подтвердить финальный unpaused state.
- [ ] Проверить role denial для owner-only маршрутов без создания P2 UI.
- [ ] Безопасно определить возможность создания индивидуального owner/admin с TOTP. Не выводить seed; bootstrap не отключать до доказанного owner login и rollback.
- [ ] Выполнить финальные health/source/tag/schema/log checks и independent review результата.
- [ ] Зафиксировать, что выполнено автоматически, а что требует человека/TOTP, отдельным локальным коммитом.

