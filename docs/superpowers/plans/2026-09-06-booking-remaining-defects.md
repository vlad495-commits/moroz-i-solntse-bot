# B2: план оставшихся дефектов записи

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** не терять предпочтение специалиста и открыть управление четвёртой записью.

**Architecture:** существующий coordinator, durable scenario, revision callback. Без новой LLM/таблицы.

**Tech Stack:** Python, PostgreSQL, pytest в Docker, fake YCLIENTS.

## Global Constraints

- Реальные mutations/сервер/main не менять. Сохранить ownership/confirmation/idempotency.
- По три записи на странице; глобальные indices в Router и callback совпадают.
- Старые callback action codes не перенумеровывать, offset по умолчанию 0.

## Задача 1: сохранённое предпочтение

Files: project/src/moroz/booking/telegram.py; project/tests/e2e/booking/test_staff_preference_integrity.py.

- [x] RED: четыре параметризованных кейса неизвестного/неоднозначного имени, затем даты, затем однозначного имени/«любой специалист».
- [x] Убрать условие decision.staff is not None из проверки после merge_draft: достаточно state.get('service_id') and not self._resolve_staff_preference(state).
- [x] GREEN: pytest текущего нового файла в изолированном Docker; отдельно commit.

## Задача 2: страницы своих записей

Files: project/src/moroz/booking/telegram.py; новый project/tests/e2e/booking/test_booking_management_pages.py.

- [x] RED: создать четыре будущих записи реальным сервисом с fake provider; получить следующую страницу и выбрать четвёртую, проверить отмену/перенос только после подтверждения.
- [x] Не обрезать сохранённые owned choices до трёх. Хранить booking_offset в scenario. _choice_reply показывает values[offset:offset+3], используя enumerate(..., start=offset), и контекстные page callbacks.
- [x] Добавить booking_page в конец _CALLBACK_ACTIONS. Обрабатывать только collecting/step=booking; принимать соседнее существующее смещение страницы, иначе _recover_callback. Сохранить offset через checkpoint.
- [x] Включить offset в revision только для новых сценариев с этим полем, чтобы не менять старые revisions без необходимости. Routing context отображает ту же страницу с глобальными индексами.
- [x] _apply_choice принимает только видимый booking index; чужая/устаревшая/отрицательная/выходящая за границы кнопка не изменяет состояние/провайдера. Не обходить предел callback 64 bytes/MAX_CHOICE_INDEX; описать поведение на пределе.
- [x] GREEN страницы + staff + B0/conversational/unit booking; review обоих исправлений, отдельный commit страниц.

## Проверки и завершение

Префикс запуска из project/: docker compose --env-file ../tmp/audit-test.env -p moroz-reference-test -f docker-compose.yml -f docker-compose.audit-test.yml run --rm -v ../tmp:/reports -w /workspace test pytest --rootdir=/workspace -q -p no:cacheprovider.

Focused аргумент — /workspace/tests/e2e/booking/test_staff_preference_integrity.py либо /workspace/tests/e2e/booking/test_booking_management_pages.py --tb=short --show-capture=no.

- [ ] Свести B1/B2 в расширенный gate, затем полный suite текущего кандидата; записать точное evidence.
- [ ] Обновить roadmap, исходный аудит и changelog. Внешняя приёмка и выпуск требуют отдельного разрешения; локальную готовность не выдавать за rollout.
