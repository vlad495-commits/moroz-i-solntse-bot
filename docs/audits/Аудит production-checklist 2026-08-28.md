# Аудит production-checklist — 2026-08-28

## Назначение отчёта

Этот документ сопоставляет все 68 пунктов исторического
[`checklist.md`](../../checklist.md) с текущей версией Moroz i Solntse Bot.

Чек-лист больше не является источником текущего статуса проекта. Актуальные
приоритеты находятся в [`Дорожная карта.md`](../../Дорожная%20карта.md), состав
Telegram Production V1 — в [`План реализации.md`](../../План%20реализации.md),
а обязательные условия запуска — в
[`project/ops/launch-checklist.md`](../../project/ops/launch-checklist.md).

Проверенный локальный HEAD на момент аудита:
`5c742242f4de320bcf58941bab20f9d598051692`.

## Краткий вывод

| Статус | Количество | Смысл |
|---|---:|---|
| ✅ Подтверждено | 32 | Требование реализовано и подтверждается кодом, конфигурацией, тестами или актуальным эксплуатационным evidence |
| 🟡 Частично / реализовано иначе | 24 | Основная возможность есть, но формулировка чек-листа выполнена не полностью, устарела или требует дополнительного production evidence |
| ❌ Не реализовано / не подтверждено | 12 | Требование отсутствует либо нет достаточного доказательства его выполнения |
| **Всего** | **68** | Все верхнеуровневые пункты исходного чек-листа учтены ровно один раз |

Проект существенно превзошёл стартовый чек-лист по durable messaging,
очередям, scheduler, privacy, YCLIENTS-проекции, админке, LLM security и
evaluation suites. При этом production launch пока заблокирован не общим
качеством кодовой базы, а конкретными незакрытыми release gates.

## Главные выводы для владельца

1. **Текущий release blocker — pre-YCLIENTS catalog grounding.** На staging
   placeholder-каталог с единственной услугой `Услуга` перехватывает запросы о
   цене и длительности криокапсулы и водородотерапии, после чего бот просит
   уточнить уже названную услугу. Root cause зафиксирован в дорожной карте;
   Task 6 должен закрыть его test-first.
2. **Production launch ещё не выполнялся.** Не закрыты YCLIENTS acceptance,
   индивидуальный аккаунт заказчицы с TOTP, юридические тексты, live alert
   evidence, инструктаж персонала и подписанный launch checklist.
3. **Локальный `.env` сейчас не позволяет импортировать runtime-конфиг bot.**
   Rendered Compose передаёт `CONTEXT_MESSAGES_LIMIT=20`, а
   `COMPACT_THRESHOLD` для bot остаётся со значением по умолчанию `30`.
   Свежесобранный образ падает с `ValueError: invalid compact context limits`.
   Одноразовый override `CONTEXT_MESSAGES_LIMIT=40` подтвердил, что сам
   technical eval-код исправен. Значения секретов во время проверки не
   читались и в этот отчёт не переносились.
4. **Исторический чек-лист нельзя механически отметить галочками.** Требование
   «4 контейнера» устарело, guardrails больше не являются выключаемым
   дополнением, PostgreSQL стал обязательной durable-границей, а production
   architecture включает worker, scheduler, RabbitMQ, Caddy и backup service.
5. **Полный тестовый suite в рамках этого аудита не запускался.** Свежий
   выбранный Docker-набор дал `559 passed`, technical gate — `31/31`. Последний
   документированный полный gate в дорожной карте — `1649 passed in 998.77s`.

## Методика

Статус выставлялся по фактическому runtime-коду, Alembic migrations, Docker
Compose, Dockerfiles, eval datasets, тестам, runbooks, дорожной карте и
changelog. Наличие обещания в историческом документе само по себе не считалось
доказательством реализации.

Статусы означают:

- **✅ Подтверждено** — требование выполняется в актуальной архитектуре.
- **🟡 Частично / иначе** — возможность есть, но отличается срок, режим,
  архитектурное решение или полнота evidence.
- **❌ Не подтверждено** — реализации или достаточного production evidence нет.

## 1. Проект и база знаний

| № | Пункт чек-листа | Статус | Текущее состояние и комментарий | Доказательство |
|---:|---|:---:|---|---|
| 1 | Все материалы клиента собраны и конвертированы в Markdown | 🟡 | Основные материалы, базы знаний и quick answers перенесены в Markdown. Аудит материалов всё ещё перечисляет неполученные анкеты безопасности, паспорта оборудования, финальный опрос и юридически проверенные тексты. Часть оригиналов остаётся во временных папках и не считается постоянным архивом. | `project/data/source_documents/`; `client_materials_audit_2026-07-14.md`, разделы 13 и 15 |
| 2 | Системный промпт находится в отдельном файле | ✅ | Активный промпт хранится в `project/llm/prompts/system.md` и загружается runtime-кодом. | `project/llm/prompts/system.md`; `project/llm/config.py`; `project/llm/llm.py` |
| 3 | Токены промпта подсчитаны и входят в контекст модели | ❌ | Runtime учитывает usage провайдера, но отдельного воспроизводимого артефакта с токенизацией полного системного промпта и запасом контекстного окна не найдено. | Поиск по коду и проектным документам; отдельный prompt-token gate отсутствует |
| 4 | Бэкап промпта перед каждым изменением | 🟡 | Git и таблица `prompt_versions` дают историю и rollback; admin-flow сначала создаёт версию и только потом атомарно заменяет файл. Для локальных Git-изменений отдельный обязательный pre-change backup не автоматизирован. | `project/admin/prompt_routes.py`; migration `0001_existing_schema.py` |
| 5 | Один модуль — одна ответственность | ✅ | Production-код разделён на messaging, security, booking, notifications, privacy, common, bot, worker, scheduler и admin. Runtime-файлов на 2000 строк нет; крупнейшие production-модули остаются меньше этого порога. | `project/src/moroz/`; `project/worker/main.py`; `project/admin/` |
| 6 | Все секреты находятся вне кода | ✅ | Compose требует секреты из внешнего env-file; server-only credentials не входят в Git. Последний image scan и tracked-files preflight зафиксировали нулевые secret-shaped находки. | `project/docker-compose.yml`; `project/docker-compose.prod.yml`; `AGENTS.md`; `changelog.md` |
| 7 | `.gitignore` покрывает секреты, кэш, тесты, временные файлы и `ssh/*` | 🟡 | `.env`, Python cache, logs, `tmp/` и `project/tmp/` игнорируются. Явных правил для `.pytest_cache`, `.coverage`, `htmlcov/` и `ssh/*` нет. | `.gitignore` |
| 8 | Все лимиты, таймауты и retry находятся в `config.py` | 🟡 | Основные LLM/runtime-настройки собраны в `project/llm/config.py`, но часть параметров остаётся рядом с владельцем поведения: buffer, queue retry, worker pump, scheduler heartbeat, privacy timeouts, admin probes. Это осознанная модульная конфигурация, но буквальное требование не выполнено. | `project/llm/config.py`; `project/src/moroz/messaging/buffer.py`; `project/src/moroz/common/queue.py`; `project/worker/main.py` |

**Итого по разделу:** 3 подтверждено, 4 частично, 1 не подтверждено.

## 2. LLM и обработка сообщений

| № | Пункт чек-листа | Статус | Текущее состояние и комментарий | Доказательство |
|---:|---|:---:|---|---|
| 9 | OpenAI основной, Anthropic fallback | ✅ | Gateway и legacy compatibility поддерживают OpenAI и Anthropic как primary/reserve. Конкретная пара выбирается моделями и server-only credentials. | `project/src/moroz/security/llm_gateway.py`; `project/llm/llm.py`; `project/tests/unit/security/test_llm_gateway.py` |
| 10 | Каскадный retry с exponential backoff | ❌ | Для одного LLM-запроса выполняется primary и при retryable error один вызов reserve. SDK retries намеренно выключены; exponential backoff есть у очереди и prompt reload, но не у LLM gateway. | `PrimaryReserveGateway.complete`; compact design spec; `project/src/moroz/common/queue.py` |
| 11 | При падении обоих провайдеров пользователь получает fallback | ✅ | `LLMUnavailable` перехватывается security pipeline и превращается в bounded safe reply. | `project/src/moroz/security/pipeline.py` |
| 12 | Форматы OpenAI и Anthropic преобразуются при fallback | ✅ | System message отделяется для Anthropic, ответы обоих SDK приводятся к общему `LLMResponse`. | `_anthropic_messages`, `_openai_response`, `_anthropic_response` в `llm_gateway.py` |
| 13 | Единая функция обработки текстовых сообщений | ✅ | Durable worker вызывает единый `generate_response`, который проводит сообщение через общий security pipeline. | `project/worker/main.py`; `project/llm/llm.py` |
| 14 | Input guardrail → LLM → Output guardrail | ✅ | Сначала local/semantic input security, затем routing и answer LLM, затем deterministic и semantic output validation с одной регенерацией. | `project/src/moroz/security/pipeline.py`; security E2E tests |
| 15 | Буферизация быстрых сообщений 4–8 секунд | 🟡 | Буферизация и склейка реализованы через Redis/durable outbox, но окно равно 2 секундам. | `project/src/moroz/messaging/buffer.py`, `BUFFER_SECONDS = 2` |
| 16 | Typing indicator | ✅ | Webhook отправляет Telegram `typing` после durable acceptance; ошибка индикатора не ломает сообщение. | `project/llm/webhook.py`; `project/llm/handlers.py` |
| 17 | Недостаток средств вызывает CRITICAL и не ретраится | ❌ | Специальной классификации billing/quota exhaustion нет. HTTP 429 относится к retryable и может вызвать reserve. | `_retryable_status` в `project/src/moroz/security/llm_gateway.py` |

**Итого по разделу:** 6 подтверждено, 1 частично, 2 не подтверждено.

## 3. Хранилища

| № | Пункт чек-листа | Статус | Текущее состояние и комментарий | Доказательство |
|---:|---|:---:|---|---|
| 18 | Redis хранит последние N сообщений, TTL и reconnect | 🟡 | Ограничение N, reconnect и safe reads/writes есть. Для `chat:{chat_id}:messages` применяется `LTRIM`, но TTL не устанавливается. TTL есть у message buffer и служебных ключей. | `project/llm/cache.py`; `project/src/moroz/messaging/buffer.py` |
| 19 | PostgreSQL хранит сообщения, токены и security incidents | 🟡 | `messages` и `token_usage` присутствуют. Актуальной migration для `security_incidents` нет; admin поддерживает её только как optional legacy table. Security failures передаются через alerts/telemetry. | migration `0001_existing_schema.py`; `project/admin/database.py` |
| 20 | Индекс `chat_id + created_at` | ✅ | Индексы созданы для `messages` и `token_usage`. | migration `0001_existing_schema.py` |
| 21 | Ошибка записи в БД не роняет бота | 🟡 | Legacy helper выполняет safe-write. В production durable pipeline PostgreSQL является source of truth, поэтому ошибка durable acceptance корректно блокирует обработку, а не продолжает её с риском потери сообщения. | `project/llm/db.py`; `project/src/moroz/messaging/repository.py` |
| 22 | Падение Redis или БД не роняет бота | ❌ | Redis имеет несколько fallback-путей и supervisor recovery. PostgreSQL обязателен для consent, inbox/outbox и privacy ordering; при его потере health становится unavailable и полноценная обработка останавливается. Это deliberate fail-closed design. | `project/llm/webhook.py`; `project/tests/e2e/ops/test_degradation.py` |
| 23 | Ежедневное удаление записей старше года | 🟡 | Идемпотентная ежедневная scheduler-задача реализована. Текущий дефолт `DATA_RETENTION_DAYS=1095`, то есть три года, а не один. | `project/src/moroz/retention.py`; `project/worker/main.py`; Compose |

**Итого по разделу:** 1 подтверждено, 4 частично, 1 не подтверждено.

## 4. Безопасность

| № | Пункт чек-листа | Статус | Текущее состояние и комментарий | Доказательство |
|---:|---|:---:|---|---|
| 24 | Запреты и untrusted-data sandwich в системном промпте | ✅ | Промпт запрещает раскрытие инструкций; runtime отделяет owned system data от `UNTRUSTED_*` блоков. | `project/llm/prompts/system.md`; security pipeline/router |
| 25 | Набор regex guardrails, выключенный по умолчанию | 🟡 | Функциональное покрытие prompt leak, role override, privileged context, obfuscation и known attacks есть, но taxonomy отличается. Защита всегда включена и усилена LLM classifier; старый `/guardrails` больше не нужен. | `project/src/moroz/security/guardrails.py`; `input_security.py` |
| 26 | Всегда активный sanitize zero-width, XML/HTML и separators | 🟡 | NFKC и zero-width normalization работают всегда перед local checks; PII маскируется. Буквального удаления всех XML/HTML tags и separators нет: безопасность обеспечивается untrusted boundaries и output validation. | `guardrails.py`; `pii.py`; `router.py` |
| 27 | MAX_INPUT_LENGTH | ✅ | Webhook отсекает длинный ввод до durable LLM pipeline; guardrail повторяет bounded check. | `project/llm/webhook.py`; `guardrails.py` |
| 28 | Детекция утечки промпта и canary | ✅ | Deterministic validator блокирует canary, prompt leak phrases и technical artifacts; semantic validator проверяет каждый provider answer. | `validator.py`; `output_validator.py`; validator dataset |
| 29 | Доменные ограничения ответа | ✅ | Проверяются медицинские обещания, неподтверждённые цены, слоты, контакты, PII и технические артефакты. | `project/src/moroz/security/validator.py` |
| 30 | Единое безопасное сообщение отказа | ✅ | Input blocks сводятся к одному `INPUT_BLOCK_REPLY` без раскрытия причины. | `project/src/moroz/security/pipeline.py` |
| 31 | Инцидент записывается в БД и отправляется админу | 🟡 | Alert callbacks и rate-limited Telegram routing реализованы. Актуальной обязательной таблицы security incidents и единого durable incident-write пути нет. | `project/src/moroz/common/alerts.py`; `project/worker/main.py`; migrations |
| 32 | Rate-limited alerts | ✅ | `AlertRouter` использует Redis `SET NX EX` с cooldown и редактирует PII. | `project/src/moroz/common/alerts.py`; `project/tests/integration/test_alerts.py` |
| 33 | Два уровня CRITICAL / WARNING | 🟡 | Severity передаётся явно, но production wiring использует `CRITICAL`, `ERROR` и lowercase `critical`; строгого enum из двух уровней нет. | `project/worker/main.py` |
| 34 | Глобальный error handler с классификацией ошибок | 🟡 | Worker имеет supervision, retry/DLQ, scheduler error codes и alert wrapper. У webhook FastAPI нет единого глобального catch-all handler для всех routes. | `project/worker/main.py`; `project/llm/webhook.py` |
| 35 | Fallback пользователю при любой необработанной ошибке | 🟡 | LLM/security failures дают fallback. Generic worker failure проходит retry/DLQ и alert, но после terminal failure пользовательская заглушка не гарантируется во всех ветках. | `security/pipeline.py`; `worker/main.py`; queue tests |
| 36 | Graceful shutdown | ✅ | Bot, worker и scheduler закрывают DB, Redis, RabbitMQ, Telegram session и background tasks с bounded timeout. | `webhook.py`; `worker/main.py`; `scheduler/main.py` |

**Итого по разделу:** 7 подтверждено, 6 частично, 0 не подтверждено.

## 5. Деплой

| № | Пункт чек-листа | Статус | Текущее состояние и комментарий | Доказательство |
|---:|---|:---:|---|---|
| 37 | Docker состоит из четырёх контейнеров | 🟡 | Требование устарело. Базовый runtime включает bot, worker, scheduler, admin, PostgreSQL, Redis и RabbitMQ; production добавляет Caddy и backup. Это необходимое развитие архитектуры, а не недостающая функция. | `project/docker-compose.yml`; `docker-compose.prod.yml` |
| 38 | Non-root для bot и admin | ✅ | Bot/admin работают как UID 10001; worker/scheduler также non-root, backup сбрасывает привилегии до postgres. | Dockerfiles; documented staging image check |
| 39 | Healthcheck каждого сервиса | 🟡 | Healthchecks есть у bot, worker, scheduler, admin, PostgreSQL, Redis и RabbitMQ. У production Caddy и backup собственного healthcheck нет; job/profile services оцениваются exit-кодом. | Compose files |
| 40 | Docker log rotation | ✅ | Для постоянных runtime и infrastructure services задан `json-file` с `max-size` и `max-file`. | Compose files |
| 41 | Persistent volumes Redis/PostgreSQL | ✅ | `redisdata`, `pgdata`, `rabbitdata`, Caddy и backup volumes объявлены явно. | Compose files |
| 42 | `depends_on: condition: service_healthy` | ✅ | Критичные зависимости ожидают healthy state; проверено Compose contract tests. | Compose files; `test_migration_profile.py` |
| 43 | БД и Redis не открыты наружу | ✅ | В базовом/production Compose у PostgreSQL и Redis нет host port mapping. Admin bind в production ограничен `127.0.0.1`. | Compose files |
| 44 | VPS SSH только по ключам | ❌ | В репозитории нет свежего безопасного production evidence конфигурации `sshd`. Staging runtime evidence не доказывает этот пункт. | Production evidence отсутствует |
| 45 | Firewall пропускает только 22, 80, 443 | ❌ | Нет актуального inventory/command output, подтверждающего production firewall policy. | Production evidence отсутствует |
| 46 | fail2ban | ❌ | Установка и активность fail2ban не подтверждены. | Production evidence отсутствует |
| 47 | Автоматические security updates | ❌ | Настройка unattended security updates не подтверждена. | Production evidence отсутствует |
| 48 | Server `.env` имеет права 600 | 🟡 | Staging runbook требует `chmod 600`, а rollout сохранял protected copies mode `0600`. Production launch ещё не выполнялся, поэтому production evidence отсутствует. | `project/ops/staging-runbook.md`; changelog |
| 49 | Caddy/Nginx с auto-cert HTTPS | ✅ | Caddy production contract существует; staging HTTPS, routes и webhook secret rejection подтверждены runtime-smoke. | `project/ops/Caddyfile`; staging evidence в roadmap/changelog |
| 50 | Ежедневные бэкапы с ротацией 7 дней | 🟡 | Отдельный backup service создаёт encrypted dump ежедневно. Дефолтная ротация — 30 дней, не 7; окончательный retention должен утвердить владелец. | `docker-compose.prod.yml`; `backup-postgres.sh`; `backup-runbook.md` |
| 51 | Тестовый backup и restore | 🟡 | Shell contracts и fake-command E2E проходят, есть безопасный restore в отдельную БД. Реальный production restore drill остаётся незакрытым launch gate. | `tests/e2e/ops/test_backup_restore.py`; `project/ops/launch-checklist.md` |

**Итого по разделу:** 6 подтверждено, 5 частично, 4 не подтверждено.

## 6. Тестирование и сдача

| № | Пункт чек-листа | Статус | Текущее состояние и комментарий | Доказательство |
|---:|---|:---:|---|---|
| 52 | Основной eval dataset содержит 50–100+ вопросов | ✅ | `dataset.json` содержит 69 кейсов. Дополнительно существуют catalog 6, router 20, security 40, validator 60 и compact 40. | `project/llm/eval/*dataset.json` |
| 53 | Adversarial dataset содержит 20+ атак | ✅ | `adversarial_dataset.json` содержит ровно 20 jailbreak-кейсов. | Dataset count и dataset contract tests |
| 54 | Программная проверка keywords, длины и маркеров | ✅ | Есть expected/forbidden keywords, regex, structural checks, typed parsers, bounded outputs и immutable dataset contracts. | `project/admin/eval_runner.py`; `project/llm/eval/run_evals.py` |
| 55 | LLM-judge отдельно оценивает relevance, accuracy, role adherence | 🟡 | Judge сравнивает ключевую информацию и фактическую корректность и выдаёт общий score. Три отдельные именованные метрики не сохраняются. Role adherence в значительной части проверяют security/validator suites. | `JUDGE_SYSTEM_POLICY` в `project/admin/eval_runner.py` |
| 56 | Pass rate выше 80% перед deploy | ✅ | Текущий gate строже: не менее 95% total и 0 critical failures. Последние real-provider Router/Security/Validator/Compact acceptance gates прошли. | `project/src/moroz/security/eval_gate.py`; roadmap/changelog |
| 57 | Ноль пробоев adversarial tests | ✅ | Свежий local technical gate: adversarial `20/20`, общий `31/31`, critical failures `0`. | Docker-проверка этого аудита |
| 58 | Все тесты проходят | 🟡 | Свежий выбранный Docker-набор: `559 passed`. Полный suite в этом аудите не повторялся; последнее документированное полное evidence — `1649 passed in 998.77s`. Поэтому пункт нельзя считать заново полностью доказанным для текущего HEAD. | Команда аудита; `Дорожная карта.md` |
| 59 | Алерты проверены реальной тестовой отправкой | ❌ | Unit/integration contracts проходят, но подписанного live test-alert evidence для production recipients нет. Пункт остаётся unchecked в launch checklist. | `project/ops/launch-checklist.md` |
| 60 | Документация по доступам, deploy, backup и monitoring | ✅ | Есть deploy/staging/rollback/incident/backup runbooks, env validation, smoke и release checklist. Секретные доступы намеренно не записываются в Git. | `project/ops/` |
| 61 | Проведён инструктаж клиента/персонала | ❌ | Инструктаж manual escalation и outage procedure остаётся незакрытым launch gate. | `project/ops/launch-checklist.md` |
| 62 | Changelog актуален | ✅ | Значимые изменения, тесты, deploy и решения фиксируются с UTC+3 timestamp; последние rollout, QA, YCLIENTS и push действия отражены. | `changelog.md` |

**Итого по разделу:** 7 подтверждено, 2 частично, 2 не подтверждено.

## 7. Юридические и privacy-требования

| № | Пункт чек-листа | Статус | Текущее состояние и комментарий | Доказательство |
|---:|---|:---:|---|---|
| 63 | Пользовательское соглашение / условия использования | ❌ | Утверждённого legal-файла не найдено; legal texts остаются launch gate. | Client materials audit; launch checklist |
| 64 | Медицинский дисклеймер | 🟡 | Промпт и validator соблюдают wellness-границы, не дают диагнозов и переводят к врачу/специалисту. Это behavioural safety, но не заменяет утверждённый юридический дисклеймер. | `project/llm/prompts/system.md`; validator/security datasets |
| 65 | Политика обработки персональных данных | ❌ | Consent flow и `POLICY_URL` реализованы, но default URL — `https://example.com/privacy`; утверждённого текста политики в репозитории нет. | `project/llm/config.py`; launch checklist |
| 66 | Автоудаление данных через установленный срок, default 1 год | 🟡 | Scheduled retention и manual deletion реализованы, но default равен 1095 дням. Юридически утверждённый срок должен быть согласован отдельно. | `project/src/moroz/retention.py`; `DATA_RETENTION_DAYS` |
| 67 | Минимизация ПДн в LLM-контексте | ✅ | PII маскируется до Router, classifier, compactor, answer model и judge; raw значения не сохраняются в eval details и telemetry. | `project/src/moroz/security/pii.py`; pipeline/privacy tests |
| 68 | Контакты поддержки в сообщениях бота | ✅ | Системный промпт содержит телефон, Telegram, WhatsApp, сайт и правило давать конкретный канал при handoff. | `project/llm/prompts/system.md` |

**Итого по разделу:** 2 подтверждено, 2 частично, 2 не подтверждено.

## Свежие проверки, выполненные в рамках аудита

1. `docker compose --env-file ../.env --profile test config --quiet` — exit `0`.
2. Выбранный Docker test slice по security, providers, sanitization, retention,
   logging, migration profile, ops, backup/restore и alerts —
   `559 passed in 17.03s`.
3. Первая попытка technical eval на старом локальном image выявила stale image.
   После обязательной пересборки проявился фактический local env mismatch:
   `CONTEXT_MESSAGES_LIMIT=20` при default `COMPACT_THRESHOLD=30`.
4. Повтор с одноразовым несекретным override
   `CONTEXT_MESSAGES_LIMIT=40` — adversarial `20/20`, structural `5/5`,
   catalog `6/6`, общий gate `31/31`, pass rate `100%`, critical failures `0`.
5. `git status --short` и `git diff --check` перед созданием отчёта были
   чистыми.

Проверки не выполняли внешних LLM-вызовов, Telegram-сообщений, YCLIENTS
мутаций, staging/production deploy или push.

## Рекомендуемый порядок закрытия пробелов

### P0 — до продолжения release acceptance

1. Исправить pre-YCLIENTS catalog-grounding blocker test-first и повторить
   затронутые ручные сценарии.
2. Устранить local Compose/config mismatch: согласовать
   `CONTEXT_MESSAGES_LIMIT`, `COMPACT_THRESHOLD` и `COMPACT_KEEP_RECENT` либо
   изменить границу валидации так, чтобы bot не зависел от неиспользуемого
   compactor-конфига.

### P1 — обязательные production gates

1. Завершить YCLIENTS read-only/lifecycle acceptance и аккаунт заказчицы с
   TOTP по уже согласованному onboarding plan.
2. Утвердить policy, terms и медицинский disclaimer; заменить placeholder
   `POLICY_URL`.
3. Подтвердить VPS hardening: SSH keys only, firewall, fail2ban и security
   updates.
4. Выполнить real restore drill, test alert и инструктаж персонала.
5. Повторить полный Docker suite и подписать launch checklist на точном
   production candidate.

### P2 — улучшения после release blocker loop

1. Добавить воспроизводимый prompt-token budget check.
2. Решить, нужен ли LLM exponential retry перед reserve; не добавлять его без
   лимита latency и стоимости.
3. Добавить отдельную billing/quota classification для non-retryable alerting.
4. Согласовать TTL Redis-контекста и юридический retention вместо исторических
   значений по умолчанию.
5. Решить, нужен ли durable `security_incidents` ledger или достаточно
   существующих безопасных alerts, audit и evaluation telemetry.

## Вердикт

Текущая версия проекта является зрелым staging release candidate с сильной
автоматической проверкой, но не завершённым production release. Старый
`checklist.md` полезен как историческая аудитная матрица; текущие решения по
релизу должны приниматься только по дорожной карте, фазовым планам, runbooks и
этому сопоставлению с фактическими доказательствами.
