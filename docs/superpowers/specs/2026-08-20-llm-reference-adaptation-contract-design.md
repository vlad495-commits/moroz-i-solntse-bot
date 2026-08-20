# Дизайн контракта адаптации LLM-референса Володи

Дата: 2026-08-20.

## Цель

Закрепить обязательный порядок использования публичного проекта Lucky Hair Studio при проектировании и реализации четырёх следующих пар компонентов Moroz i Solntse:

1. LLM Router + Router Evaluation.
2. LLM Input Security + Security Evaluation.
3. LLM Validator + Validator Evaluation.
4. LLM Compact Context + Compact Evaluation.

Референс должен использоваться как источник проверенных решений, граничных случаев и eval-кейсов, но не как архитектура для прямого копирования.

## Постоянный документ

Создать `docs/project/Контракт адаптации LLM-референса Володи.md`. Он становится обязательным входным gate для каждой из четырёх пар. `Дорожная карта.md` должна прямо ссылаться на контракт перед списком этапов, а `docs/project/Система управления проектом.md` — зафиксировать его роль и владельца.

Нельзя начинать design или писать production-код соответствующего этапа, пока исполнитель не изучил контракт и нужный срез референса.

## Зафиксированный источник

- Проект: Lucky Hair Studio (`project-edu-public`).
- Локальный путь: `D:\Downloads\Telegram Desktop\llm чат бот от Володи\project-edu-public`.
- Анализируемый commit: `5398f909829f5db1b5052087f5a826c2bbcd5244`.
- Сводный аудит: `docs/audits/Аудит решений бота Володи 2026-08-13.md`.

Локальное рабочее дерево референса может содержать посторонние изменения. Поэтому исполнитель обязан читать файлы из commit `5398f90` через Git (`git show`, временный read-only worktree или эквивалентный способ), а не принимать текущее содержимое папки за каноническое.

## Обязательная карта изучения

### Router

- `project/llm/router.py`;
- `project/llm/tests/test_router.py`;
- `project/llm/eval/router_dataset.json`;
- router-части `project/admin/eval_runner.py`, `eval_routes.py`, `eval_database.py` и шаблонов Evaluations.

### Input Security

- `project/llm/security.py`;
- `project/llm/guardrails.py`;
- `project/llm/tests/test_security.py`, `test_guardrails.py` и связанные adversarial-тесты;
- `project/llm/eval/security_dataset.json` и security-части общего eval/admin flow.

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

Если референсный файл перемещён или отсутствует в зафиксированном commit, исполнитель должен найти эквивалент через `git grep` и записать точный найденный путь в design-spec.

## Обязательный результат анализа

До написания design-spec этапа исполнитель составляет таблицу:

| Решение референса | Что уже есть у нас | Подтверждённый пробел | Решение | Причина | Проверка |
|---|---|---|---|---|---|
| Конкретный механизм или кейс | Наш текущий компонент | Чего действительно не хватает | Взять / адаптировать / отклонить | Почему | Test/eval/gate |

Design-spec этапа обязана перечислять:

- изученные файлы референса и commit;
- принятые решения;
- сознательно отклонённые решения;
- перенесённые и адаптированные eval-кейсы;
- дополнительные кейсы, обусловленные Moroz i Solntse;
- границы данных, fallback и наблюдаемости.

## Границы адаптации

- Не копировать монолитный `worker.py` Володи.
- Не создавать параллельный security pipeline.
- Не заменять наши consent, PII masking, inbox/outbox, RabbitMQ, correlation ID, retry и fallback-контракты.
- Не выполнять прямые provider-вызовы из админки в обход текущих runtime-границ.
- Не создавать отдельную независимую eval-подсистему для каждого компонента.
- Расширять существующие `eval_cases/eval_runs/eval_results` и общий admin eval-runner; дополнительные таблицы разрешены только при доказанной невозможности выразить компонент в общей модели и после отдельного решения владельца.
- Переносить идеи, контракты и граничные случаи, а не строки кода механически.
- Не переносить реальные ПД, секреты или клиентские данные в datasets, traces и логи.

## Порядок этапов

Этапы выполняются последовательно:

`Router → Input Security → Validator → Compact Context`.

Каждый следующий этап учитывает интерфейсы и решения предыдущих, но получает собственные design-spec и implementation plan. Runtime-компонент и соответствующий evaluation suite проектируются и принимаются одной парой.

## Gate завершения каждой пары

Этап нельзя отмечать выполненным, пока одновременно не доказано следующее:

- runtime-компонент и его evaluation suite реализованы;
- suite встроен в общий admin eval-runner;
- доступны отдельная статистика компонента и перепрогон проблемных кейсов;
- покрыты позитивные, негативные, контекстные, пограничные кейсы и сбои LLM/provider;
- input, context, trace и отчёты не раскрывают ПД;
- определён и проверен безопасный fallback;
- пройдены focused Docker-тесты, затронутая регрессия, полный Docker gate и независимый review;
- в changelog и дорожной карте записаны фактические результаты и ограничения.

## Изменяемые документы

После одобрения этой спецификации:

1. Создать постоянный контракт в `docs/project/`.
2. Добавить перед четырьмя LLM-парами в `Дорожная карта.md` обязательный входной gate и ссылку на контракт.
3. Зарегистрировать роль контракта в `docs/project/Система управления проектом.md`.
4. Обновить `changelog.md` и сделать отдельный локальный commit без push.

Runtime-код, datasets, `.env`, staging, production и внешние системы в рамках этой документационной задачи не изменяются.
