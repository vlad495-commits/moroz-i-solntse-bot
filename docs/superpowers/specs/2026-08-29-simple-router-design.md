# Simple LLM Router Design

Дата: 2026-08-29. Статус: согласовано владельцем.

## Цель

Упростить Router до выбора одного маршрута по строгому LLM-контракту
`{"route":"...","confidence":0.0}`, сохранив scripts-first обработку только
для однозначных сообщений, privacy-границы и текущую параллельную связку с
Input Security.

## Изученная база

- Текущий runtime: `project/src/moroz/messaging/router.py` и
  `project/src/moroz/security/pipeline.py`.
- Потребители: `project/llm/llm.py`, Router provider/config, usage persistence,
  output validator и catalog path.
- Evaluation: `project/admin/eval_runner.py`, `eval_routes.py`, общие шаблоны,
  `project/llm/eval/router_dataset.json` и migration
  `0014_llm_router_evaluations.py`.
- Текущие Router, pipeline, provider, admin, migration и privacy-тесты.
- Документы Router и обязательный контракт адаптации референса.
- Lucky Hair Studio, commit
  `5398f909829f5db1b5052087f5a826c2bbcd5244`: `project/llm/router.py`,
  `project/llm/worker.py`, `project/llm/tests/test_router.py`,
  `project/llm/eval/router_dataset.json` и router-части admin evaluation.

## Gap-анализ референса

| Решение референса | Что уже есть у нас | Пробел | Решение | Причина | Проверка |
|---|---|---|---|---|---|
| Один `route` вместо массива intents | До трёх intents и conflict flag | Лишние состояния | Взять | Один запрос имеет один следующий сценарий | Strict parser и runtime-тесты |
| Контракт `route + confidence` | Strict JSON schema | Schema описывает массив | Адаптировать | Сохраняем строгую валидацию | Invalid-output matrix |
| Перенос и отмена в одном маршруте | Два маршрута | Искусственное разделение | Взять как `booking_management` | Оба управляют существующей записью | Кейсы переноса и отмены |
| Жалобы и просьба человека → escalation | Два маршрута | Разные названия одной границы | Объединить в `escalation` | Единая семантика | Complaint/handoff cases |
| Fallback consultation | `unknown` + clarification | Лишняя неопределённость | Взять с confidence `0.0` | Не блокирует и не эскалирует | Failure tests |
| Router параллельно с Security | Уже есть masked parallel gate | Нет | Оставить | Наша реализация уже безопасно cancel/drain-ит | Parallel/block tests |
| Терпимый parser и raw response | Строгий parser, raw не хранится | Нет | Отклонить | Наша граница безопаснее | Markdown/extra-key rejection |
| Router запускает booking/handoff chains | У нас route — metadata основной LLM | Другая бизнес-архитектура | Отклонить | Владелец подтвердил metadata-only escalation | Pipeline tests |
| Отдельная Router eval-страница | Общая eval-система уже есть | Нужна новая версия без порчи истории | Versioned suite | Старые результаты остаются читаемыми | Migration/admin tests |

## Маршруты

- `consultation` — услуги, цены, подготовка, противопоказания, контакты,
  адрес и расписание;
- `booking` — новая запись;
- `booking_management` — перенос или отмена существующей записи;
- `escalation` — жалоба, претензия, возврат денег или явная просьба человека;
- `smalltalk` — приветствие, прощание, благодарность и короткая реакция;
- `offtopic` — тема вне центра;
- `other` — прочее сообщение по теме центра.

`unknown`, `complaint`, `human_handoff`, `booking_change` и
`booking_cancel` удаляются из текущего runtime-контракта.

## Scripts-first граница

Локально обрабатываются только явно сформулированные случаи:

1. Жалоба или просьба человека сразу даёт `escalation`.
2. Перенос или отмена существующей записи даёт `booking_management`.
3. Явное намерение записаться даёт `booking`.
4. Однозначный вопрос об услуге, цене или публичных контактах даёт
   `consultation`.
5. Только полностью совпавшая короткая вежливая реплика даёт `smalltalk`.

Если не-escalation правила дают разные маршруты, смысл зависит от контекста
или локального совпадения нет, вызывается Router LLM. Локальный off-topic
classifier не добавляется.

## LLM-контракт

Провайдер получает прежние masked current message и не более шести последних
masked user/assistant сообщений в общем лимите `2000` символов. Транскрипт
остаётся явно недоверенными данными.

Допустимый ответ содержит ровно два поля:

```json
{"route":"consultation","confidence":0.9}
```

`route` обязан входить в allowlist. `confidence` — конечное число от `0.0` до
`1.0`; boolean, `NaN`, дополнительные поля, markdown и текст вокруг JSON
отклоняются. `asyncio.CancelledError` пробрасывается.

Для смешанного сообщения LLM выбирает один ближайший сценарий. Приоритет:
`escalation` → `booking_management` → `booking` → `consultation`.
`escalation` запрещено выбирать только из-за низкой уверенности.

## Внутренний runtime-контракт

`RouteDecision` хранит только `route` и `confidence`. Служебные `source`,
`reason_code` и usage остаются в `RouterVerdict` и не входят в LLM JSON.

После `Security OK` pipeline передаёт основной LLM только allowlisted metadata:

```text
ROUTE route=consultation; source=llm; confidence=high
```

Точная confidence не логируется: используется bucket `high/medium/low`. Для
`offtopic` сохраняется локальный ответ после `Security OK`. Catalog direct reply
применяется только к `consultation`. `escalation` не создаёт DB-запись и не
включает `human mode`.

## Ошибки и наблюдаемость

При provider error, невалидном JSON или внутренней ошибке используется
`consultation` с confidence `0.0`; основной answer path продолжается, ложная
эскалация невозможна. `reason_code` — только статический allowlisted код. Логи
и eval results не содержат input, context, raw provider response, exception
text или ПД.

Security и Router для локально неразрешённого сообщения стартуют параллельно.
Verdict и usage потребляются только после `Security OK`. При Security BLOCK/error
Router task отменяется и drain-ится. Внешняя отмена не скрывается.

## Router Evaluation v2

`project/llm/eval/router_dataset.json` и migration `0014` остаются неизменными.
Добавляется checksum-pinned `router_dataset_v2.json` с single-route кейсами:
FAQ, запись, перенос, отмена, жалоба, handoff, smalltalk, off-topic, other,
context follow-up, mixed priority, prompt safety, masked PII и fallback.

Data-only migration `0019_router_v2` добавляет кейсы в suite `router_v2` и при
downgrade удаляет только результаты, прогоны и кейсы этого suite. Старые suite
`router`, dataset, migration и исторические результаты не меняются.

Текущая Router Evaluation запускает `router_v2`; старые run detail остаются
читаемыми. Comparator проверяет только ожидаемый single `route`; source,
confidence и reason code сохраняются как безопасная диагностика. Отдельный
dataset-test проверяет scripts-first/LLM boundary. Answer, Security, Validator
и Compact suites не меняют контракты.

## Файлы реализации

- `project/src/moroz/messaging/router.py`;
- `project/src/moroz/security/pipeline.py`;
- `project/llm/llm.py`;
- `project/admin/eval_runner.py`, `eval_routes.py` и общие eval templates;
- `project/llm/eval/router_dataset_v2.json`;
- `project/migrations/versions/0019_router_v2.py`;
- затронутые Router, pipeline, provider, admin, migration, privacy и E2E-тесты;
- Router design/plan, архитектурные документы, roadmap и changelog.

Новые зависимости, отдельные таблицы и отдельный eval-runner не создаются.

## Проверка

Test-first покрытие доказывает:

- strict single-route JSON и invalid-output fallback;
- scripts-first boundary и LLM для контекстных/смешанных сообщений;
- FAQ, booking, перенос, отмену, complaint, handoff, smalltalk, off-topic,
  other и fallback;
- только masked bounded input/context;
- parallel Security gate, запрет использования до allow и cancel/drain;
- безопасные metadata, usage, логи и `CancelledError`;
- Router Evaluation v2, problem rerun, старые run details и migration cycle без
  изменения старых datasets/migrations;
- неизменность Security, Compact, answer LLM, local output checks и optional
  semantic Validator;
- focused Docker gate, полный Docker suite и независимый review.

Реальные provider-вызовы, Telegram-проверки, push, deployment, staging и
production не входят в реализацию без отдельного разрешения.
