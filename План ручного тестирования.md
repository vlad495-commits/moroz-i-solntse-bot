# План проверки релиз-кандидата

Это канонический реестр проверок релиз-кандидата. Он не смешивает автоматические доказательства с внешним smoke и человеческой приёмкой. Отдельный файл `Ручное тестирование человеком.md` остаётся исполнителским чеклистом для человека, а не вторым реестром технических статусов.

## Статусы

- `[x] АВТО-OK` — есть воспроизводимое автоматическое доказательство на указанном commit; результат не переносится на изменившийся код без проверки влияния.
- `[ ] BLOCKED (локальная среда)` — проверка безопасна и локальна, но текущая среда не позволила её выполнить.
- `[ ] EXTERNAL GATE` — проверку можно автоматизировать, но нужны staging/production, отдельный доступ, sandbox, ключ или разрешённая внешняя мутация.
- `[ ] HUMAN ONLY` — результат обязан увидеть и оценить человек; unit/integration/e2e не закрывают такой пункт.

Дата среза: 2026-08-05
Ветка: `codex/audit-code-fixes`
Текущий HEAD: `db4b3b4` (docs-only поверх code commit `4f9fb07`)

## Правила безопасности

- Сначала staging. Production smoke — только после отдельного разрешения и в согласованное окно.
- Использовать тестовые аккаунты и вымышленные персональные данные. Настоящие ПД в зарубежный LLM не отправлять.
- Реальную запись, перенос или отмену в YCLIENTS выполнять только в выделенном sandbox/test-филиале либо после явного разрешения владельца.
- Не отмечать ручной пункт по результату автотеста и не отмечать фактический LLM-eval по dataset/schema/mock-проверке.
- Не считать HTTP-успех отправки алерта доказательством, что человек его получил и понял.

## 1. Автоматически выполнимо локально

### 1.1. Уже подтверждено после code-fix

- [x] **АВТО-OK:** на code commit `4f9fb07` focused regression подтвердил авторизацию `/logs/tail`, явный file-log signal, идемпотентность consent callback и атомарную запись/reload промпта: `4 + 5 + 7 passed`. Состав набора закреплён в Tasks 1–3 файла `docs/superpowers/plans/2026-08-05-audit-code-fixes.md`; основные test paths — `tests/e2e/admin/test_auth.py`, `tests/unit/test_runtime_logging_policy.py`, `tests/e2e/test_privacy_gate.py`, `tests/e2e/admin/test_csrf_rbac_audit.py`, `tests/unit/test_active_sanitization.py`, `tests/integration/messaging/test_prompt_reload.py`. Фактический результат — `changelog.md`, записи `2026-08-05 15:01` и `15:08`.
- [x] **АВТО-OK:** на code commit `4f9fb07` focused regression подтвердил явный `POSTGRES_USER` при restore, ежедневный backup runner/service, runtime-подключение `AlertRouter` и отсутствие polling-пути: `6 + 8 + 7 passed`. Воспроизводимый состав — Tasks 4–6 того же implementation plan и paths `tests/e2e/ops/test_backup_restore.py`, `tests/e2e/ops/test_runbooks.py`, `tests/unit/ops/test_validate_env.py`, `tests/unit/test_worker.py`, `tests/integration/test_alerts.py`, `tests/unit/test_migration_profile.py`, `tests/unit/test_staging.py`; фактические результаты записаны в `changelog.md` в `2026-08-05 15:19`.
- [x] **АВТО-OK:** на code commit `4f9fb07` post-review команда `pytest -q tests/integration` прошла `87 passed`; отдельный повтор ранее упавших visual/docs node IDs с исправленным read-only mount прошёл `17 passed`. Оба результата, причина первого visual failure и границы доказательства записаны в `changelog.md` в `2026-08-05 16:15` (verification entry, Task 8 implementation plan).
- [x] **АВТО-OK:** на code commit `4f9fb07` backup automation набор из `tests/e2e/ops/test_backup_restore.py` и связанных ops-contract tests прошёл `9 passed`; локальные `backup-postgres.sh` + `verify-backup.sh` завершились успешно. Команды и результат закреплены в `changelog.md` в `2026-08-05 16:15` и Task 4 implementation plan. Это доказывает scripts/шифрование/checksum и fake-command restore contract, но не фактическое восстановление данных в пустую PostgreSQL.
- [x] **АВТО-OK:** 2026-08-05 без provider calls проверена текущая структура eval-наборов: `69` main + `20` adversarial = `89` кейсов, `50` critical; обязательные поля валидны, IDs уникальны внутри каждого dataset. Это не является прогоном моделей и не даёт pass rate.
- [x] **АВТО-OK:** `git diff --check` чистый; `project/llm/prompts/system.md` неизменён относительно `91012dd` и в working tree.

Эти пункты не повторялись без причины: их покрытие либо относится ровно к последним фиксам, либо проверяемые файлы после доказательства не менялись.

### 1.2. Локальные проверки, которые должны быть свежими на точном release candidate

- [x] **АВТО-OK:** на точном integration HEAD `f086de0` Docker collect-only собрал `967` тестов; непересекающаяся матрица прошла `584 unit + 87 integration + 111 contract + 185 e2e = 967 passed`. Полный unit повторялся с корректным read-only mount `docs` в `/docs`; первый ошибочный mount в `/repo/docs` дал только известные `17` `FileNotFoundError` и не считается результатом gate.
- [x] **АВТО-OK:** объединённый deterministic scheduler/reminder/failure, simulated alert delivery/dedup/channel-failure и mock eval-harness gate на том же HEAD прошёл `136 passed`.
- [x] **АВТО-OK:** в отдельной disposable PostgreSQL выполнены миграции до `0009_production_admin`, создан реальный encrypted backup с SHA-проверкой, затем restore в новую БД `work3_restore`; восстановленная БД содержит тот же Alembic revision и одну контрольную sentinel-запись. Production-база не использовалась.
- [x] **АВТО-OK:** test и migration images собраны в отдельном Compose namespace с синтетическим ignored env; PostgreSQL/Redis/RabbitMQ достигли `healthy`, migration дошла до `0009_production_admin`, base/staging/production Compose render прошёл. После gate test containers/network удалены без `-v`, четыре созданных volume сохранены.
- [x] **АВТО-OK:** compileall, `sh -n`, base/staging/production Compose render и focused security gate `319 passed` завершены; отдельный минимальный code-fix удалил девять действительно неиспользуемых импортов и сохранил публичный `SecurityGateResult` как явный re-export. Pinned Ruff `0.12.7` с `--no-cache --select E9,F` прошёл и для `project`, и для полного root scope `.` (`All checks passed`, exit 0); focused regression дал `157 passed`, а `test_message_delivery.py` собрал все `27` тестов.

Среда проверки: использован только ignored `tmp/work3-final.env` с синтетическими локальными значениями и Compose namespace `moroz-work3-final-20260805`; staging/production/Telegram/YCLIENTS/LLM provider не вызывались. Старые `moroz-auditfix-*` test-volume и четыре новых Work 3 volume сохранены; images/prune не выполнялись.

## 2. Автоматически выполнимо, но требует внешней среды или доступа

Ни один пункт этой группы 2026-08-05 не запускался: staging/production и реальные внешние системы намеренно не затрагивались.

### 2.1. Staging runtime и интеграции

- [ ] **EXTERNAL GATE:** validate/render/build/migrate/healthchecks на точном commit с release `.env`; затем scripted staging smoke/load/failure gates.
- [ ] **EXTERNAL GATE:** Telegram Bot API — `setWebhook`, secret header, update/callback delivery, retry/rate-limit и защита от второго обработчика на выделенном staging-токене.
- [ ] **EXTERNAL GATE:** YCLIENTS sandbox — поиск слота → создание → чтение → перенос → отмена, timezone/branch/service и идемпотентность повторной task/webhook delivery.
- [ ] **EXTERNAL GATE:** provider-boundary capture на разрешённом тестовом LLM-ключе подтверждает, что исходные телефон/email/ФИО не уходят в request/logs.
- [ ] **EXTERNAL GATE:** контролируемые restart/outage Redis, PostgreSQL, RabbitMQ и primary LLM проходят scripted failure matrix без потери или двойной обработки подтверждённых задач.
- [ ] **EXTERNAL GATE:** scheduler отправляет тестовое уведомление один раз в правильное локальное время.

### 2.2. Operations

- [ ] **EXTERNAL GATE:** backup-service сам создаёт зашифрованный backup по расписанию в серверной test/staging среде.
- [ ] **EXTERNAL GATE:** выбранный backup восстановлен в отдельную пустую серверную PostgreSQL с явным `POSTGRES_USER`; сверены миграции, counts, тестовые сущности, логи и отсутствие утечки ключа.
- [ ] **EXTERNAL GATE:** controlled worker failure доставляет один технический алерт разрешённому test recipient; dedup-window, повтор после окна и отказ alert-channel проверены автоматически. Подтверждение человеком вынесено в группу 3.
- [ ] **EXTERNAL GATE:** внешний uptime monitor видит `/healthz` и создаёт тестовый incident/alert по согласованному маршруту.

### 2.3. Фактические LLM-evals

Evals должны идти на зафиксированных dataset version, моделях, provider settings и judge threshold. Локальная schema/mock-проверка из группы 1 не закрывает ни один пункт ниже.

- [ ] **EXTERNAL GATE:** основная модель — услуги/цены, тон, контекст, медицинские границы и честный отказ.
- [ ] **EXTERNAL GATE:** роутер — primary/reserve/tool selection, false route и недоступность маршрута.
- [ ] **EXTERNAL GATE:** output validator — невалидный формат, фейковая запись, опасные утверждения, служебные утечки и false positives.
- [ ] **EXTERNAL GATE:** Guardrails/PII — jailbreak, prompt extraction, obfuscation, phone/email/ФИО и смешанные кейсы; измерены recall и false positives.
- [ ] **EXTERNAL GATE:** fallback/reserve — timeout, 429, 5xx и malformed primary response без дубля внешнего действия; полный отказ даёт safe fallback.
- [ ] **EXTERNAL GATE:** сохранены dataset SHA/version, модели/провайдеры, judge, thresholds, число кейсов, pass rate, critical failures и артефакт прогона.

### 2.4. Production

- [ ] **EXTERNAL GATE:** production smoke запускается только после отдельного разрешения: точный commit, migrations, healthchecks, один безопасный Telegram flow, admin/metrics/logs, свежий backup и отсутствие новых `ERROR/CRITICAL`/дублей.

## 3. Только вручную человеком

- [ ] **HUMAN ONLY:** все 36 пользовательских сценариев из `Ручное тестирование человеком.md` пройдены человеком на staging и имеют статусы `OK / Ошибка / Нюанс / Не проверено`.
- [ ] **HUMAN ONLY:** человек визуально оценил consent, тон, длину, понятность, корректность услуг/цен, медицинские границы, отсутствие фейковой записи, контекст, buffer UX, non-text и pause/resume.
- [ ] **HUMAN ONLY:** два реальных тестовых Telegram-аккаунта не смешивают видимый контекст; ссылки и кнопки открываются туда, куда ожидается; admin UI показывает диалог, порядок и понятные ошибки.
- [ ] **HUMAN ONLY:** назначенный получатель подтвердил фактическое получение, понятность и безопасное содержание тестового технического алерта. API ACK или simulated sender этого не доказывают.
- [ ] **HUMAN ONLY:** ответственный за backup/restore подтвердил RPO/RTO, дату backup, длительность restore, rollback-команду и готовность к запуску.
- [ ] **HUMAN ONLY:** финальное решение о выпуске принято по одному release candidate после сверки автоматических, внешних и human-only доказательств.

## Дефект и критерий выпуска

Для дефекта записать: группу проверки, среду и commit, время МСК, тестовый аккаунт/ID без ПД, точные шаги, ожидаемое/фактическое, screenshot/log correlation ID и критичность.

Релиз готов только когда группа 1 свежая и зелёная на точном release candidate, обязательные external gates группы 2 пройдены, human-only группа 3 подписана человеком, backup/restore drill доказан, человек получил тестовый алерт, а production smoke либо явно разрешён и пройден, либо формально оставлен назначенным launch gate.
