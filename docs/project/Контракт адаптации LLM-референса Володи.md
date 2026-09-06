# Контракт адаптации LLM-референса Володи

## Уточнение владельца 2026-09-06: упрощение и ручные знания

Для нового аудита и последующей адаптации Lucky Hair служит исходным образцом простого разделения: консультация основного LLM по вручную редактируемому системному промпту, детерминированные действия записи. Контекстные кнопки и последовательные уточнения разрешены; тезисы «всё отвечает только LLM» и «любые кнопки нарушают референс» неверны.

В Moroz сохраняется YCLIENTS для provider ID, специалистов, слотов, создания/переноса/отмены. Ownership, consent/privacy/security, явное подтверждение, идемпотентность, durable inbox/outbox и обработка неопределённого результата не жертвуются ради сходства. Отличия обосновываются конкретными потребителями и требованиями, а не привычкой к нашей архитектуре.

Консультационные названия, описания, цены, длительности и подготовка переходят в ручной основной промпт собственника; автоматический консультационный каталог YCLIENTS и второй прайс не нужны. Технический каталог нельзя отключать без проверки booking-зависимостей. Router V3 выбирает **один** маршрут; topics и сущности сами по себе не означают несколько маршрутов или доказанную избыточность. Оценивать полную цепочку обработки, включая сложность, перенесённую в handlers.

Первый пакет описан в [узкой spec](../superpowers/specs/2026-09-06-manual-consultation-knowledge-design.md), доказательства — в [аудите](<../audits/Аудит упрощения архитектуры по Lucky Hair 2026-09-06.md>). Полный rollback не выбран. Код — только после разбора результата владельцем, rollout отдельно. Старый AI OS router/dispatch contract с отказом от YCLIENTS не нормативен; новый конкурирующий reference contract не создаётся.

## Первоначальный gate четырёх LLM-пар

Этот документ — обязательный входной gate для четырёх последовательных пар:

1. LLM Router + Router Evaluation.
2. LLM Input Security + Security Evaluation.
3. LLM Validator + Validator Evaluation.
4. LLM Compact Context + Compact Evaluation.

До начала design и написания production-кода исполнитель обязан изучить нужный срез Lucky Hair Studio, сравнить его с текущей архитектурой Moroz i Solntse и зафиксировать решения. Референс даёт идеи, контракты и граничные кейсы, но не заменяет нашу архитектуру.

## Канонический срез референса

- Проект: Lucky Hair Studio (`project-edu-public`).
- Локальный путь: `D:\Downloads\Telegram Desktop\llm чат бот от Володи\project-edu-public`.
- Проверенный при аудите путь: `D:/AI_OS/30_Sources/Automation_Courses/01_Raw_Materials/2026-08-13_lucky_hair_llm_chatbot_reference/project-edu-public`; путь Downloads выше — альтернативная копия, не основание читать иной commit.
- Commit: `5398f909829f5db1b5052087f5a826c2bbcd5244` (`5398f90`).
- Наш сводный аудит: `docs/audits/Аудит решений бота Володи 2026-08-13.md`.

Локальное рабочее дерево референса может содержать посторонние изменения. Канонические файлы нужно читать из commit `5398f90` через `git show`, временный read-only worktree или эквивалентный Git-способ. Нельзя молча использовать текущее содержимое грязной папки.

## Что изучать

### Router

- `project/llm/router.py`;
- `project/llm/tests/test_router.py`;
- `project/llm/eval/router_dataset.json`;
- router-части `project/admin/eval_runner.py`, `eval_routes.py`, `eval_database.py` и шаблонов Evaluations.

### Input Security

- `project/llm/security.py` и `project/llm/guardrails.py`;
- `project/llm/tests/test_security.py`, `test_guardrails.py` и связанные adversarial-тесты;
- `project/llm/eval/security_dataset.json`;
- security-части общего eval/admin flow.

### Validator

- `validate_output` и его integration point в `project/llm/llm.py` и `project/llm/worker.py`;
- `project/llm/tests/test_output_guard.py`, `test_validator_toggle.py` и связанные worker-тесты;
- `project/llm/eval/validator_dataset.json`;
- validator-части общего eval/admin flow.

### Compact Context

- `compact_context` в `project/llm/llm.py`;
- `COMPACT_THRESHOLD` и `COMPACT_KEEP_RECENT` в `project/llm/config.py`;
- место вызова в `project/llm/worker.py`;
- `project/llm/tests/test_compact.py` и связанные worker-тесты.

Если файл перемещён или отсутствует в commit, нужно найти эквивалент через `git grep` и записать точный путь в design-spec.

## Обязательный gap-анализ

До design-spec исполнитель составляет таблицу:

| Решение референса | Что уже есть у нас | Подтверждённый пробел | Решение | Причина | Проверка |
|---|---|---|---|---|---|
| Конкретный механизм или кейс | Наш текущий компонент | Чего действительно не хватает | Взять / адаптировать / отклонить | Почему | Test/eval/gate |

В design-spec этапа обязательно перенести:

- изученные файлы и commit референса;
- принятые и сознательно отклонённые решения;
- адаптированные eval-кейсы референса;
- дополнительные кейсы Moroz i Solntse;
- границы данных, fallback и наблюдаемости.

## Границы адаптации

- Не копировать монолитный `worker.py` Володи.
- Не создавать второй security pipeline.
- Не заменять наши consent, PII masking, inbox/outbox, RabbitMQ, correlation ID, retry и fallback-контракты.
- Не обходить runtime-границы прямыми provider-вызовами из админки.
- Не создавать независимую eval-подсистему для каждого компонента.
- Расширять существующие `eval_cases/eval_runs/eval_results` и общий admin eval-runner. Дополнительные таблицы допустимы только после доказанной невозможности выразить компонент в общей модели и отдельного решения владельца.
- Переносить идеи, контракты и граничные кейсы, а не строки кода механически.
- Не переносить реальные ПД, секреты или клиентские данные в datasets, traces и логи.

## Порядок реализации

`Router → Input Security → Validator → Compact Context`.

Каждый следующий этап учитывает интерфейсы и решения предыдущих, но получает собственные design-spec и implementation plan. Runtime-компонент и его Evaluation проектируются, реализуются и принимаются одной парой.

## Gate завершения каждой пары

Этап нельзя отмечать выполненным, пока одновременно не доказано:

- runtime-компонент и evaluation suite реализованы;
- suite встроен в общий admin eval-runner;
- есть отдельная статистика компонента и перепрогон проблемных кейсов;
- покрыты позитивные, негативные, контекстные, пограничные кейсы и сбои LLM/provider;
- input, context, trace и отчёты не раскрывают ПД;
- определён и проверен безопасный fallback;
- пройдены focused Docker-тесты, затронутая регрессия, полный Docker gate и независимый review;
- `changelog.md` и `Дорожная карта.md` содержат фактические результаты, ограничения и статус без преждевременной галочки.
