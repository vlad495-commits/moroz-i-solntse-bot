# Восстановление каталога и диалогов — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** исправить подтверждённые живой приёмкой потери категорий, темы консультации, длительности и смешение цены с оформлением.

**Architecture:** YCLIENTS — источник каталога; свободный текст — LLM-router; операции — проверяемый coordinator. История разговора и структурированное состояние имеют независимые бюджеты, просмотр каталога не обозначается оформлением. Сохранённые реплики уточнения явно указывают ожидаемое действие, чтобы не вводить вторую копию диалога/новую таблицу без необходимости.

**Tech Stack:** Python 3.12, asyncpg/PostgreSQL, aiogram, pytest, Docker. Новых зависимостей нет.

## Global Constraints

- Ветка codex/semantic-booking-repair; владелец выбрал работу в текущей папке, не worktree.
- Пользователь утвердил решение из docs/audits/2026-09-05-dialog-catalog-recovery.md сообщением «делай».
- Только Docker для исполнения Python/тестов; только apply_patch для редактирования.
- Не возвращать business-keyword routing. Не менять реальные записи, согласия и настройки YCLIENTS.
- Не генерировать цены. Не подменять явно заданную длительность похожим вариантом. Fail closed на stale catalog и invalid action.
- Сохранить ownership, privacy gate, idempotency, callback safety и button confirmation.
- Временные brief/report/скрипты — только корневой tmp/. Коммиты с явным списком файлов.

## Task 1: Импорт категорий по фактическому API

Files: project/src/moroz/booking/yclients_catalog.py; project/tests/contract/booking/test_yclients_catalog.py.
Consumes: GET book_services data.services + data.category; service.category_id.
Produces: существующий CatalogRecord.category_name, без изменения схемы БД.

- [ ] RED: добавить fixture с отдельной категорией `{id: 31, title: "Массаж"}` и услугой `{category_id:31}` без вложенного category; assert snapshot.records[0].category_name == "Массаж". Проверить неизвестный ID, дубли/противоречие, malformed category payload, старую вложенную форму.
- [ ] Запустить Docker contract test, зафиксировать ожидаемый FAIL None != Массаж.
- [ ] GREEN: построить ограниченный словарь ID→очищенное название из data.category и передать разрешённую категорию в _record; валидировать конфликт, не принимать raw payload/инструкции как команды.
- [ ] Повторить полный contract файл и unit booking, self-review, локальный commit.

## Task 2: Контекст и семантические переходы

Files: project/src/moroz/messaging/router.py; project/src/moroz/security/pipeline.py; project/src/moroz/booking/telegram.py; project/tests/unit/messaging/test_router.py; project/tests/unit/security/test_pipeline.py; project/tests/e2e/booking/test_semantic_booking.py.

- [ ] RED: actual LLMIntentRouter с capture-provider получает 76 choices + предыдущий вопрос/ответ. Assert наличие услуги из предыдущей реплики и целого JSON состояния; assert каталог обозначен browse, не create.
- [ ] GREEN: передавать состояние отдельным бюджетом, не последним assistant-сообщением истории. Передавать только видимые варианты с их реальными индексами; не обрезать JSON. В prompt закрепить приоритет текущего намерения и уточнения предыдущего вопроса, не считать голое название согласием на booking.
- [ ] RED/GREEN: action/route combinations проверяются на границе; continue без подходящего активного шага не начинает запись. Clarify неизвестного намерения отличается от уточнения отмены.
- [ ] RED/GREEN: в ответе запроса цены без услуги сохраняется смысл вопроса («Чтобы назвать цену…»), неоднозначный выбор цены остаётся консультацией. История не смешивается со state и не содержит нерелевантных полных меню.
- [ ] Regression: consultation во время draft не меняет draft; просмотр существующих записей переключается безопасно; old callbacks и pending confirmation не исполняются повторно.

## Task 3: Точная услуга и длительность

Files: project/src/moroz/booking/catalog.py; project/src/moroz/messaging/router.py; project/worker/main.py; project/tests/unit/booking/; project/tests/e2e/booking/test_semantic_booking.py.

- [ ] RED: запрос "Солярий 10 минут" среди 1/7/10/16/20 минут выбирает ровно 10; точное имя с разделителем | не теряется; отсутствующие минуты не возвращают соседние. Сначала exact/parameter filtering, только потом ограничение числа результатов.
- [ ] GREEN: сохранить числа при техническом matching; явная длительность — фильтр, не soft score. Расширить структурированные параметры router при необходимости, без keyword intent routing. Без однозначного варианта — честное уточнение.
- [ ] RED/GREEN: одинаковые слова в названиях, несколько услуг, цена/длительность, неизвестная услуга, stale snapshot. Worker использует единый результат разрешения без повторного противоречивого exact-фильтра.

## Task 4: Каталог и видимые карточки

Files: project/src/moroz/booking/telegram.py; project/src/moroz/booking/catalog.py; project/tests/e2e/booking/test_semantic_booking.py; project/tests/e2e/booking/test_telegram_booking.py.

- [ ] RED/GREEN: девять реальных категорий, walk-in family → минуты в числовом порядке; скрыть техническое повторение цены/ресурса, не давать депозиту выдуманную процедурную длительность.
- [ ] RED/GREEN: slot header содержит услугу, DD.MM.YYYY и Московское время; старый callback объясняет обновление, не принимает старый выбор.
- [ ] RED/GREEN: catalog facts разделяют service и staff/resource; без буквальных ** и двойных точек в Telegram-ответах. Не разрушать ссылки и смысл текста.

## Task 4a: STOP закрывает только незавершённое оформление

Владелец явно утвердил: «Да, закрывать и черновик» (2026-09-05).

- [ ] Сохранить немедленный marketing opt-out до pause/consent gate; одновременно закрывать только собираемый или ожидающий подтверждения черновик клиента.
- [ ] Не трогать executing/завершённые сценарии и реальные записи; ответ честно различает закрытый черновик и уже выполняемую операцию.
- [ ] Проверить сериализацию с worker, идемпотентный повтор STOP, устаревшие кнопки и отложенные сообщения до STOP: они не должны восстановить закрытое оформление. После STOP новая явная просьба записаться разрешена.
- [ ] TDD и независимый review; только синтетические согласия/записи в Docker, реальное согласие владельца в QA не менять.

## Task 5: Интеграция и приёмка

- [ ] Исправить подтверждённые дефекты самого gate: отсутствующий migration_source в трёх тестах 0024 и отсутствие TELEGRAM_YCLIENTS_BOOKING_ENABLED в двух строгих worker-allowlists. Не менять миграцию, safety-switch/defaultfalse и границы секретов; отдельные RED/GREEN/review. Детали в tmp/recovery-final-gate-fixes.md.
- [ ] Все targeted RED/GREEN результаты записаны; независимый review spec/quality после каждого блока, общий review после интеграции.
- [ ] Docker gate: tests/contract/booking tests/unit/booking tests/unit/messaging tests/unit/security tests/e2e/booking tests/e2e/test_message_delivery.py. Сохранить точную команду/число тестов; старый зелёный suite не заменяет текущий.
- [ ] Компиляция, git diff --check, Compose config; не запускать Telegram polling локально.
- [ ] Живая LLM/Telegram приёмка точного кандидата по матрице audit; перед staging cutoff использовать deploy workflow/rollback. Production не выкладывать. Админ-TOTP, реальные mutations и multi-user проверки не объявлять пройденными без доступа/разрешения.
- [ ] После rollout обновить локальную проекцию каталога штатным sync с GET-only обращением к YCLIENTS и штатной блокировкой; не ждать следующего часового тика. Проверить категории и свежесть до Telegram-приёмки. Не изменять настройки/записи YCLIENTS.
- [ ] Roadmap/changelog обновить по факту; честно разделить локально исправлено, развёрнуто, проверено живой LLM и ещё не проверено.

## Progress ledger

- Task 1 complete: 38b7ee3..8aa7ec7, review spec compliant / quality approved, no findings, Docker 120 passed. Ruff unavailable in image; full lint remains final gate.
- Task 2 complete: f43e7d9..a18dcc1, review spec compliant / quality approved after stale-service fix. Docker 678 passed before fix + 42 covering passed on final fix, RED 3 failures reproduced stale/removed/updated catalog issue. Live model/Telegram verification pending.
- Task 3 complete: c7e5a34..dad9a0b, review spec compliant / quality approved, no findings. Final covering106 passed. Real GET probe all76 exact names passed at c3f3531; final integrated probe remains pending. Collarium2/7 tariffs absent in source, owner informed.
- Task 4 complete: 682e7f2..5588ae4, independent re-review spec/quality pass, all Important resolved. Final review-fix6 passed/29.33s; earlier covering45/16 passed; global integrated suite pending. Full evidence tmp/recovery-task4-report.md.
- Task 4a complete: b2eb3f1..62ae4fb, independent re-review spec compliant / quality approved. Covering34 passed + reversed queue1 passed + final provenance21 passed. Retention ordinary retry window confirmed1095days; arbitrary replay beyond marker retention explicitly remains final-review boundary in tmp/recovery-retention-assessment.md.
- Task5 in progress; includes5 concrete full-unit gate defects from tmp/recovery-final-gate-fixes.md, then final review/gate/staging/QA.
