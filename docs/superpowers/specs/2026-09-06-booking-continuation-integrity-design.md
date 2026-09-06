# Узкий пакет B0: целостность продолжения переноса

Статус: одобрен владельцем и реализован локально 2026-09-06 в codex/reference-simplification, fix-коммит ad2a4be. Без merge/push/rollout.

## Зачем

Клиент начал перенос записи и следующим сообщением назвал дату. Это должно продолжить перенос той же записи, а не начать создание новой. Если клиент меняет дату перед подтверждением, старые кнопки и выбранный слот должны перестать действовать, но сведения об исходной записи нельзя потерять.

Основание: §4.2 [исходного аудита](<../../audits/Аудит упрощения архитектуры по Lucky Hair 2026-09-06.md>), P1-3/P1-4 [побочного отчёта](<../../audits/Аудит локального проекта 2026-09-06.md>). Кандидат анализа 785f619; измерение pipeline d208f8d. Все 193 целевых теста проходят, но не воспроизводят перечисленные дефекты полностью.

## Решение

1. `continue` продолжает существующую операцию по сохранённому scenario.kind. Даже если Router вернул `booking/continue`, активный перенос не превращается в create. Отсутствие активного сценария не разрешает скрытую операцию: штатное уточнение/безопасный ответ.
2. Исходные сведения существующей записи (её ID и исходное время) сохраняются при изменении параметров нового слота. Дата/время/специалист нового выбора инвалидируют только новый выбор и старые кнопки.
3. Изменение данных переноса, уже ожидающего подтверждения, возвращает его к сбору/повторному подтверждению. Старое подтверждение не исполняет изменённую операцию.
4. Явный новый запрос `create` не подменяется `continue`: существующий контракт осознанного переключения сохраняется.

Не вводить второй Router, новый оркестратор, новую БД или отдельную LLM для извлечения даты. Не менять PUBLIC_ACTIONS, topics/services, промпт Router и immutable router datasets в этом пакете: исправляется использование уже принятого решения backend-координатором. Вопрос передачи routing state в Router Evaluation остаётся отдельным пунктом перед изменением Router-контракта.

## Почему не копировать референс буквально

Lucky Hair 5398f90, project/llm/worker.py:717–726, при активном переносе отвечает просьбой нажать кнопку на любой текст. Это проще, но клиент не может продолжить перенос обычной датой. В Moroz сохраняется такой ввод и технические гарантии YCLIENTS. Сценарий полезен сам по себе; наличие существующего consumer не является обоснованием.

## Не входит

- замена topics булевым признаком, удаление legacy service/actions;
- изменение входных кнопок и меню;
- исправления выбора неизвестного специалиста и четвёртой записи;
- редактор промпта, цены, программы Светы;
- paid evals, Telegram/YCLIENTS mutations, push, merge, rollout.

## Проверяемый результат

- Создать тестовую существующую запись, начать перенос, передать дату отдельным `booking/continue`: scenario ID/kind и связь с записью сохраняются, create_calls=0.
- То же для `booking_management/continue`; прежний рабочий маршрут не ломается.
- После выбора нового слота и явного подтверждения выполняется ровно один reschedule исходной записи; не возникает KeyError из-за потерянного исходного времени.
- Исправить дату/время после awaiting_confirmation: старый callback отклоняется, новое подтверждение переносит на новый слот.
- Повтор того же входа/подтверждения не повторяет provider mutation.
- FAQ между шагами не удаляет draft; явный create и новая запись без активного переноса сохраняют прежнее поведение.

Использовать реальные coordinator/repository/service в disposable Docker-контуре и существующий fake YCLIENTS adapter. Не выдавать scripted Router verdict за подтверждённое качество живой классификации.

## План локальных коммитов

1. RED: добавить воспроизведение continue/kind и изменения подтверждаемого переноса в booking E2E, проверить падение по ожидаемой причине.
2. Исправить минимально handle/management dispatch и сброс параметров слота; сохранить действующие ownership, consent, confirmation и idempotency. Зелёный focused gate → один fix-коммит с regression-тестами.
3. Прогнать unit/booking, conversational/management E2E, semantic dispatch и worker mixed/outage. Независимый read-only review, исправление замечаний, повтор затронутых тестов.
4. Обновить исходный аудит, roadmap и changelog отдельным документным итогом. Полный итоговый release gate нужен перед объединением/выпуском всей ветки; локальная коррекция не означает rollout.

Работа остаётся в codex/reference-simplification. Main не изменяется; не создавать ещё одно рабочее дерево для последовательного исправления того же пакета.

## Результат проверки 2026-09-06

Минимальное исправление в TelegramBookingCoordinator: продолжение выбирает обработчик по сохранённому kind/mode, отсутствие активного сценария даёт уточнение. На границе переноса сохраняется исходное starts_at; исправление нового слота снимает старое подтверждение. Неизменённый повтор сохраняет актуальную кнопку. BookingService, Router schema/prompt и datasets не менялись.

Девять новых E2E в test_booking_continuation_integrity.py используют реальные coordinator/repository/service и fake YCLIENTS. Корректный RED — 8 failed / 1 passed; focused GREEN — 9 passed (32.59s). После дополнительных assertions повторного continue расширенный gate — **297 passed (270.46s)**, exit 0. Проверены unit/booking, все booking E2E, semantic dispatch, mixed answer outage, pipeline, Router path counts и worker. Независимый read-only review: spec/quality одобрены, замечаний нет.

Воспроизводимая команда из project/ (только изолированный Docker-контур):

```powershell
docker compose --env-file ../tmp/audit-test.env -p moroz-reference-test -f docker-compose.yml -f docker-compose.audit-test.yml run --rm -v ../tmp:/reports -w /workspace test pytest --rootdir=/workspace -q -p no:cacheprovider /workspace/tests/unit/booking /workspace/tests/e2e/booking /workspace/tests/unit/security/test_semantic_dispatch.py /workspace/tests/unit/security/test_mixed_answer_outage.py /workspace/tests/unit/security/test_pipeline.py /workspace/tests/unit/security/test_router_path_counts.py /workspace/tests/unit/test_worker.py --tb=short --show-capture=no --junitxml=/reports/b0-broad.xml
```

Это проверка backend при заданном RouteDecision, не оценка живой LLM-классификации и не live YCLIENTS acceptance. Полный suite всей ветки после B0 ещё не запускался; итоговый release gate остаётся перед объединением/выпуском.
