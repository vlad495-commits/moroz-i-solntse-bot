# Аудит тестирования Moroz i Solntse Bot

Дата: 2026-08-05 (Europe/Moscow)  
Режим: read-only аудит; исправления, деплой, push и внешние мутации не выполнялись.  
Срез кода: `91012dd` (`main`, рабочий task находится в detached HEAD; локальный `main` указывает на тот же commit).  
Рабочее дерево до отчёта: чистое. Сам отчёт лежит в игнорируемом корневом `tmp/`.

## Короткий итог

Проект имеет сильную локальную автоматизированную базу и подтверждённый staging QA, но статус остаётся: **local-ready, частично staging-tested, production launch blocked**.

- Последняя свежая автоматизированная матрица от 2026-08-04: `569 unit + 87 integration + 111 contract + 172 e2e = 939 passed`, `939 tests collected`.
- Повтор полного suite не выполнялся: после этого прогона runtime-код не менялся, текущий `HEAD` — тот же `91012dd`.
- Telegram staging проверялся вручную 31.07 и targeted/incremental 31.07–01.08; критических ошибок в этих прогонах не найдено.
- Provider-side PII masking подтверждён отдельными Docker gates `243 passed` и `6 passed`.
- YCLIENTS sandbox lifecycle **уже подтверждён**: services/staff/slots → create → get → reschedule → get → cancel → reconciliation, `matches=1`, `active_matches=0`, `success=true`.
- Найдено 9 подтверждённых кодовых дефектов/реализационных разрывов. Самые опасные: публичный endpoint хвоста логов без авторизации, неисполняемый restore под стандартным DB-пользователем, отсутствие автоматического backup и отсутствие runtime-подключения alert router.
- Системный промпт не менялся и не входит в рекомендуемую задачу исправлений.

## 1. Карта уже проведённого тестирования

Уровни доказательности:

- **Высокая** — свежий воспроизводимый Docker gate или внешний lifecycle с точной reconciliation.
- **Средняя/высокая** — ручной staging-прогон с Telegram/admin/server evidence, но первичный отчёт находится в игнорируемом `tmp/` другого checkout.
- **Средняя** — контракт/симуляция подтверждает код, но не реальную внешнюю эксплуатацию.
- **Низкая** — наличие артефакта или документа без фактического прогона.

| Этап | Что проверялось | Способ | Результат | Доказанность |
|---|---|---|---|---|
| Unit | config, ingress/router, buffer, worker supervision, queue, booking models/adapters, security/PII/guardrails/validator, scheduler, admin security, ops contracts, HTML contracts | Docker pytest, отдельная непересекающаяся группа | `569 passed` 04.08 | Высокая |
| Integration | PostgreSQL/Alembic, Redis buffer/prompt reload, RabbitMQ queue, durable inbox/outbox, booking repository, notifications persistence, admin audit/metrics | Docker с реальными disposable Postgres/Redis/RabbitMQ | `87 passed` 04.08 | Высокая локально |
| Contract | YCLIENTS HTTP/adapter, provider response/error/no-blind-retry semantics и прочие внешние контракты | Mock HTTP/contract tests | `111 passed` 04.08 | Средняя/высокая |
| E2E | message delivery, privacy gate, security pipeline, booking create/change/fail-closed, reminders/feedback, admin auth/RBAC/CSRF/audit, ops artifacts | Локальный Docker E2E с fake/mock внешними системами | `172 passed` 04.08 | Высокая для локального контура |
| Telegram staging baseline | `/start`, цены/услуги, medical boundary, booking wording, context, buffer, prompt leak, long input, non-text, pause/unpause, admin evidence | Telegram Web + admin UI + synthetic webhook | 14 сценариев; 13 OK, 1 нюанс Telegram Web; свежих критичных ошибок нет | Средняя/высокая |
| Telegram incremental | buffer hotfix, non-text, admin state/logs | Telegram Web + webhook + admin + server logs | PASS, бот оставлен unpaused | Средняя/высокая |
| Security/guardrails staging | canary/prompt leak, instruction override, medical risk, fake PII, fake price, stop, rate limit/recovery | Telegram Web + read-only SSH | PASS; fresh error scan пустой | Средняя/высокая |
| Provider-side PII | phone/email/name/address/payment/medical не попадают во внешний `LLMRequest`; placeholders восстанавливаются только из текущей сессии | Focused Docker unit/e2e | `243 passed` + `6 passed` | Высокая |
| LLM evals | контакты, цены, услуги, medical boundary, booking wording, prompt safety | admin eval-runner, regex/keyword + LLM judge | Runs 1 и 4: `25/25`; в staging хранятся 53 кейса, 3 запуска, 90 результатов | Средняя; 53 кейса целиком после последних изменений не прогнаны |
| YCLIENTS sandbox | доступность services/staff/slots; create/get/reschedule/get/cancel; exact custom field ownership; final reconciliation | Изолированный consented real sandbox smoke без реальных ПД и без blind retry | Финальный lifecycle PASS, активных тестовых записей не осталось | Высокая для одного test service/staff flow |
| Staging release/recovery | HTTPS/webhook secret, container health, rollback candidate→previous→candidate, safe logs, schema | server runbook + read-only checks | PASS для зафиксированного candidate | Высокая для того среза; текущий local HEAD новее remote/staging |
| Admin | login/TOTP-ready, DB sessions, CSRF, RBAC, audit, `/admin` prefix, metrics contracts | unit/integration/e2e + частичный live UI | Основные contracts PASS; prompt save live дал `write_failed` | Смешанная |
| Scheduler/reminders | planning, lifecycle persistence, dedup, cancellation, feedback/no-show | unit/integration/e2e | PASS локально | Средняя; scheduler отключён на staging |
| Backups/restore | shell syntax и orchestration с fake CLI | E2E с подменёнными `pg_dump/createdb/pg_restore/psql` | Тесты PASS | Низкая/средняя; real restore не доказан, найден дефект пользователя БД |
| Monitoring/alerts | metrics collectors, cooldown/redaction/routing class | unit/integration с fake sender | Contracts PASS | Низкая для эксплуатации; alert router не подключён к runtime |
| Production | env validation, Compose/Caddy/runbooks/checklists | локальные contracts | local-ready | Реальный production не развёрнут и не принят |

## 2. Подтверждённые кодовые баги к исправлению

### BUG-CODE-01 — `/logs/tail` доступен без авторизации

- **Факт/источник:** `project/admin/logs_routes.py:94-98` не принимает `Request` и не вызывает `get_current_user`, в отличие от `/logs/` и `/metrics`. Глобального auth middleware в `admin/app.py` нет. Caddy публикует весь `/admin/*`.
- **Влияние:** после восстановления file logging любой внешний пользователь сможет читать operational logs без сессии. Возможна утечка chat IDs, внутренних ошибок и служебной информации.
- **Приоритет:** **P0 security** до production.
- **Воспроизведение:** поднять admin с непустым тестовым `bot.log`; запросить `/admin/logs/tail` без cookie. Ожидаемое безопасное поведение — redirect/401/403, текущий код вернёт JSON.
- **Следующий шаг:** кодовая TDD-задача: auth + минимальная роль для endpoint, e2e regression на anonymous/expired/admin/owner.

### BUG-CODE-02 — consent callback не идемпотентен по Telegram `update_id`

- **Факт/источник:** `project/llm/webhook.py:214-236` для checkbox callback делает read→toggle→write Redis и сразу `edit_message_reply_markup`; durable inbox/dedup для callback отсутствует. В истории есть реальный `TelegramBadRequest: message is not modified` на consent-кнопке (31.07).
- **Влияние:** повторная доставка одного callback может второй раз перевернуть галку, вернуть согласие в прежнее состояние или дать 500. Это риск корректности consent и UX.
- **Приоритет:** **P1**, поднять до P0 перед production privacy acceptance.
- **Воспроизведение:** дважды отправить один и тот же synthetic callback payload с одинаковым `update_id`; состояние checkbox после второго запроса не должно меняться и Telegram edit не должен повторяться.
- **Следующий шаг:** добавить durable/idempotent callback handling и regression на sequential/concurrent duplicate; отдельно вызвать `answerCallbackQuery`.

### BUG-CODE-03 — live-сохранение промпта падает с `write_failed`

- **Факт/источник:** live admin уже вернул `write_failed`; открытые пункты roadmap 119–120. Admin запускается как `USER appuser` (`project/admin/Dockerfile:27`) и пишет в bind mount `./llm/prompts:/app/prompts:rw` (`docker-compose.yml:262`). Текущий код `project/admin/prompt_routes.py:71-78` зависит от возможности записать host-owned файл.
- **Влияние:** владелец не может надёжно сохранить новую версию и выполнить hot-reload через admin.
- **Приоритет:** **P1**.
- **Воспроизведение/перепроверка:** на Linux staging проверить UID/GID и `test -w /app/prompts/system.md` из admin-контейнера, затем сохранить безопасный тестовый вариант с rollback. Причина permissions очень вероятна, но UID/GID нужно подтвердвердить read-only до решения.
- **Следующий шаг:** кодовая/ops-конфигурационная TDD-задача на устойчивое writable storage без запуска контейнера root. **Содержимое системного промпта не менять.**

### BUG-CODE-04 — save/rollback промпта не атомарны и могут сообщить ложный успех hot-reload

- **Факт/источник:** `prompt_routes.py:100-138` сначала пишет файл, затем создаёт DB-версию; при DB error файл уже изменён. `_publish_reload()` (`40-61`) глотает Redis error, а route всё равно возвращает `saved=<id>`. Rollback устроен так же.
- **Влияние:** расхождение файл↔история версий, незаметно не применённый prompt на bot/worker, повторная операция после 500 и сложный rollback.
- **Приоритет:** **P1**.
- **Воспроизведение:** unit/e2e с успешной записью файла и исключением `create_version`; отдельно исключение Redis publish. Проверить, что UI не сообщает успех и активное состояние не расходится.
- **Следующий шаг:** определить один source of truth и атомарный publish/version contract; добавить failure-path tests. **Не менять текст промпта.**

### BUG-CODE-05 — file logging молча деградирует, admin показывает отсутствующий файл

- **Факт/источник:** manual QA 31.07 зафиксировал отсутствие `/app/logs/bot.log`; `llm/bot.py:32-45` молча проглатывает `OSError`; bot тоже `USER appuser`, каталог `./logs` bind-mounted `:rw`. Admin читает только этот файл (`admin/logs_routes.py`). Docker logs при этом существуют.
- **Влияние:** раздел «Логи» может быть пустым, а причина не видна; оператор теряет единый incident evidence.
- **Приоритет:** **P1 observability**.
- **Воспроизведение:** на Linux staging проверить `test -w /app/logs`, наличие файла и стартовый stderr; убедиться, что admin видит новую sentinel-log line.
- **Следующий шаг:** либо сделать гарантированно writable non-root storage и fail-visible startup warning, либо читать поддерживаемый централизованный источник логов. Не ослаблять контейнер до root.

### BUG-CODE-06 — restore script не задаёт PostgreSQL user

- **Факт/источник:** `project/ops/restore-postgres.sh:37-39` вызывает `createdb`, `pg_restore`, `psql` без `--username "$POSTGRES_USER"`; образ `postgres:16-alpine` имеет пустой `Config.User`, поэтому `docker compose exec postgres sh ...` запускает shell как root, а libpq по умолчанию ищет DB-role `root`. Fake CLI тест не ловит это.
- **Влияние:** штатный restore drill/аварийное восстановление с высокой вероятностью падает до восстановления.
- **Приоритет:** **P0 recovery**.
- **Воспроизведение:** disposable real Postgres из Compose, encrypted тестовый dump, `docker compose exec postgres sh /ops/restore-postgres.sh ...` с отдельной `RESTORE_TARGET_DB`; до исправления ожидать ошибку role `root`/аутентификации.
- **Следующий шаг:** TDD на реальном disposable Postgres, явно передавать DB user во все restore CLI, затем доказать schema/data checksum.

### BUG-CODE-07 — ежедневные backup не автоматизированы

- **Факт/источник:** есть `backup-postgres.sh` и ручной runbook, но ни Compose service, ни scheduler job, ни cron/systemd artifact не вызывает скрипт. Поиск `backup-postgres` находит только runbook/tests/script.
- **Влияние:** обещанные ежедневные резервные копии не создаются автоматически; при аварии может не быть актуального backup.
- **Приоритет:** **P0 production blocker**.
- **Воспроизведение/перепроверка:** на сервере read-only проверить cron/systemd timers и timestamps в backup volume. В репозитории автоматизация отсутствует; внешняя серверная настройка пока не доказана.
- **Следующий шаг:** добавить репозиторно управляемое расписание, retention, verify-after-create, failure alert и integration contract; затем staging evidence за два последовательных запуска.

### BUG-CODE-08 — `AlertRouter` не подключён к runtime

- **Факт/источник:** `project/src/moroz/common/alerts.py:33` содержит класс, но поиск runtime (`src/`, `llm/`, `admin/`, `worker/`, `scheduler/`) не находит ни импорта, ни создания `AlertRouter`; использование есть только в `tests/integration/test_alerts.py`.
- **Влияние:** ошибки очереди/LLM/YCLIENTS/backup не могут реально уйти техническому или бизнес-получателю, несмотря на проходящие contracts.
- **Приоритет:** **P0 operations**.
- **Воспроизведение:** вызвать контролируемый runtime failure/DLQ в локальном контуре с fake sender spy; сейчас нет wiring, который вызовет `emit()`.
- **Следующий шаг:** подключить router к конкретным failure events, добавить redacted end-to-end delivery contract, затем выполнить один согласованный staging alert до тестового получателя.

### BUG-CODE-09 — polling mode обходит production privacy/durable pipeline

- **Факт/источник:** `llm/Dockerfile:32` поддерживает `TELEGRAM_MODE=polling`; `llm/handlers.py:47-112` сохраняет текст и вызывает LLM без processing-consent, Redis buffer, durable inbox/outbox и worker path. Webhook mode содержит эти gates отдельно.
- **Влияние:** случайный запуск polling создаёт второй, существенно менее безопасный режим и может отправить данные во внешний LLM без consent.
- **Приоритет:** **P1**, P0 если polling допускается в любом реальном контуре.
- **Воспроизведение:** запустить только тестовый polling-контур и отправить текст от пользователя без consent; handler сразу пишет DB и вызывает LLM.
- **Следующий шаг:** либо удалить/запретить polling в production builds/config validation, либо провести его через общий ingress/pipeline. Не дублировать security logic.

## 3. Документационные дефекты — не кодовые баги

| ID | Факт | Влияние | Приоритет | Следующий шаг |
|---|---|---|---|---|
| DOC-01 | `AGENTS.md` всё ещё описывает ступень 1 и 3 контейнера; roadmap — «ступень 3» и десятки уже реализованных функций как open; `checklist.md` почти целиком unchecked | Ошибочные решения и повтор работ | P1 | Отдельная docs-only синхронизация после code-fix задачи |
| DOC-02 | HTML-аудит 04.08 указывает YCLIENTS live mutation smoke как P0, хотя master/changelog доказывают финальный create/get/reschedule/get/cancel/reconciliation PASS | Ложный launch blocker, недооценка фактического покрытия | P1 | Исправить только статус/evidence, не повторять mutation без новой причины |
| DOC-03 | Roadmap утверждает, что Phase 7–8 код остаётся только в `codex/phase7-8-readiness`; `6aac533` является ancestor текущего `HEAD`, а локальный `main` уже содержит работу | Неверное представление о ветках/интеграции | P1 | Обновить source of truth после отдельной проверки deployment version |
| DOC-04 | `backup-runbook.md` называет `PGPASSWORD` обязательным, но scripts его не требуют; restore при этом не передаёт `POSTGRES_USER` | Сбивает диагностику реального restore-дефекта | P2 | Уточнить runbook вместе с code fix BUG-CODE-06 |

## 4. Исправлено и подтверждено ранее

| Находка | Статус | Доказательство |
|---|---|---|
| PII могла уйти provider-side | Исправлено/подтверждено | Docker `243 passed` + critical classes `6 passed`; staging recheck 01.08 |
| Guardrails bypass/prompt leak/external resource tricks | Исправлено/подтверждено | security suite + targeted Telegram QA; commits `42766a5`, `7af4f55`, `8c09bc5` |
| Buffer 5 секунд и отсутствие раннего typing | Исправлено/подтверждено | buffer 2 sec + typing; `29 passed`; staging hotfix и incremental QA |
| Битые consent-тексты из env | Исправлено/подтверждено | Compose env overrides убраны; ручная Telegram проверка |
| YCLIENTS staff/book_check/unknown outcome/ownership/post-cancel gaps | Исправлено/подтверждено | contract/full gates и финальный real lifecycle с exact reconciliation |
| Документационные Windows paths в Docker test command | Исправлено/подтверждено | focused contract `6 passed`; финальная матрица `939 passed` |
| 16 unit FAIL из-за отсутствующего docs bind mount | Ложная тревога окружения | те же 16 тестов прошли с корректным `/repo/docs` mount |
| Старый Redis auth mismatch локального admin | Проблема окружения | пароль running Redis отличался от текущего `.env`; не runtime defect кода |

## 5. Исправлено, но требует повторной внешней проверки

| Область | Почему не считать полностью закрытой |
|---|---|
| Последние security/start/docs изменения | Локальные gates есть, но current local `main` на 25 commits впереди `origin/main`; точный deploy commit staging не доказан в этом аудите |
| Staging admin Phase 7–8 | Локальные RBAC/TOTP/metrics contracts сильные, но roadmap фиксирует незавершённую актуализацию staging |
| YCLIENTS | Один полный sandbox lifecycle закрыт; права/поведение для нескольких услуг, сертификатов, абонементов и депозитов не доказаны |
| Consent callback traceback | После 10:34 UTC 31.07 не повторялся, но idempotency defect в текущем callback code остаётся (BUG-CODE-02) |

## 6. Риски и неподтверждённые проверки

### P0

| Проверка | Где | Что нужно | Участие/доступ |
|---|---|---|---|
| Anonymous access regression для admin endpoints | Локально | проверить все JSON/SSE/HTMX endpoints, начиная с `/logs/tail` | Без внешних доступов |
| Real disposable restore | Локально Docker | backup → verify → restore в отдельную БД → schema/data checksum | Без внешних доступов |
| Backup schedule + failure path | Локально, затем staging | два плановых backup, retention, verify и failure alert | Staging после code fix |
| Runtime alert delivery | Локально spy, затем staging | controlled failure → redacted alert → receipt | Нужен тестовый Telegram recipient |
| Staging smoke/load/failure matrix | Staging | актуальный checkout, 30 inbound/min, 20 active chats, Redis/RabbitMQ/LLM/YCLIENTS degradation и recovery | Server access; без клиентских мутаций |
| Изолированный restore drill серверного dump | Staging/server | восстановить не в primary DB, проверить Alembic/schema/counts | Server access + человек для sign-off |
| Production credentials/TLS/uptime/legal/launch sign-off | Production | ротация секретов, домен/TLS, внешний monitor, TOTP, юридические тексты, runbooks | Владелец/техвладелец |

### P1

| Проверка | Где | Что нужно | Участие/доступ |
|---|---|---|---|
| Prompt save/version/reload failure matrix | Локально + staging | file/DB/Redis failure, restart, rollback, active-version evidence | Staging owner; текст промпта не менять |
| Полный прогон 53 eval-кейсов | Staging/admin | свежий run после code fixes, разбор FAIL/спорных | LLM judge key/cost; prompt-only fixes вне scope |
| Scheduler/reminders | Staging | booking_created/day_before/morning/hour/no-show/feedback, cancel/reschedule dedup | Test Telegram + YCLIENTS sandbox; scheduler сейчас disabled-in-staging |
| Deployment drift | Read-only staging | точные image IDs/commit/schema против local `91012dd` | SSH/read-only |
| External uptime monitor | Внешне | независимый probe `/healthz` и доказанная доставка alert | Настройка сервиса + получатель |

## 7. Не баги / проблемы окружения / нюансы

- Telegram Web может дробить сообщение длиннее 4000 символов; бот правильно отклоняет длинную часть, но обрабатывает короткий хвост. Это клиентский нюанс; продуктовая защита от хвоста пока риск, не подтверждённый дефект Telegram ingress.
- Первые YCLIENTS sandbox failures были вызваны staff/service schedule, permissions и provider outcome uncertainty; после исправления внешнего test-контура и безопасной диагностики финальный lifecycle прошёл.
- Ошибки tool/workdir, PowerShell glob, Windows path separators, Docker daemon off, missing local RabbitMQ env и command timeouts относятся к среде аудита, не к runtime.
- Два длинных `pytest -q` были прерваны внешними лимитами без итоговой строки; итог доказан отдельными непересекающимися группами и collect-only без двойного счёта.

## 8. Не входит в текущий кодовый scope

- Любые изменения `project/llm/prompts/system.md`, tone-of-voice, формулировок услуг, CTA, medical wording или эталонных ответов.
- Если свежие 53 eval-кейса выявят проблему, исправимую только системным промптом, её нужно пометить **«prompt/content — вне текущего кодового scope»** и вынести в отдельное согласование.
- Уточнение клиентских цен, акций, противопоказаний, юридических текстов и бизнес-правил — входные данные владельца, не кодовый баг.

## 9. Рекомендованная следующая задача

Запустить отдельную **code-only P0/P1 fix task без изменения системного промпта**:

1. закрыть anonymous `/logs/tail`;
2. сделать consent callbacks идемпотентными;
3. исправить real PostgreSQL restore и добавить integration test;
4. добавить управляемое расписание backup;
5. подключить `AlertRouter` к runtime failure events;
6. стабилизировать prompt/log storage и failure semantics без изменения текста промпта;
7. запретить небезопасный polling либо провести его через общий privacy/durable pipeline.

После локального TDD и полного Docker gate — отдельная staging-задача на smoke/load/failure, real restore и test alert delivery. YCLIENTS mutation smoke без новой причины не повторять.

