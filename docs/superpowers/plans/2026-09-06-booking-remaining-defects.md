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

Запускать из project/ командой из раздела «Задача 1 — безопасный тестовый контур» [канонического плана](2026-09-06-reference-simplification.md), заменив только путь теста указанным ниже. Это единственный владелец команд изолированного контура; не подставлять рабочий env. Для полного gate использовать команду задачи 6 того же плана с обязательной пересборкой test image.

Focused аргумент — /workspace/tests/e2e/booking/test_staff_preference_integrity.py либо /workspace/tests/e2e/booking/test_booking_management_pages.py --tb=short --show-capture=no.

- [x] B1/B2 сведены в expanded gates и полный rebuilt suite c5ac62e: 2379 passed/1 document failure; после исправления документа весь unit suite 1315 passed. Это full + post-fix evidence, не зелёный повтор полного suite.
- [x] Roadmap, исходный аудит и changelog обновлены; [итоговый отчёт](<../../audits/Приёмка упрощения архитектуры 2026-09-06.md>). Внешняя приёмка и выпуск требуют отдельного разрешения; локальная готовность не означает rollout.
