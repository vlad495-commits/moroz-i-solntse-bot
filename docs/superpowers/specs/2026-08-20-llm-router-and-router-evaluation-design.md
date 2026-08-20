# LLM Router + Router Evaluation — спецификация дизайна

## Цель

Добавить в текущий Telegram message pipeline Moroz i Solntse гибридную scripts-first маршрутизацию: однозначные команды, активные workflow и узкие локальные правила завершают выбор сценария без Router LLM; неоднозначные сообщения классифицирует отдельная дешёвая Router LLM. Runtime-компонент и Router Evaluation реализуются и принимаются одной парой.

Роутер только определяет сценарий. Он не выполняет запись, перенос, отмену, handoff, отправку сообщения или другой side effect.

## Scope

В scope входят:

- deterministic dispatcher с формальным результатом `resolved` или `unresolved`;
- отдельный Router LLM с коротким prompt, strict schema и локальной валидацией;
- параллельный запуск Input Security LLM и Router LLM на unresolved-ветке;
- безопасный fallback без ложной уверенности;
- ограниченная трассировка и отдельный учёт usage без ПД;
- Router Evaluation в общих `eval_cases`, `eval_runs`, `eval_results`;
- отдельная статистика suite `router` и перепрогон проблемных router-кейсов.

Вне scope:

- выполнение изменяющих действий самим роутером;
- замена consent, privacy/deletion fence, PII masking, guardrails, validator, inbox/outbox, RabbitMQ, correlation ID или handoff;
- реализация следующей пары Input Security + Security Evaluation;
- новые каналы, autonomous agents, voice/STT, deploy, staging или production rollout;
- изменение judge settings и использование реальных клиентских сообщений в dataset.

## Канонический референс

Изучен Lucky Hair Studio (`project-edu-public`) на commit `5398f909829f5db1b5052087f5a826c2bbcd5244` (`5398f90`) через `git show`, без доверия к локальному рабочему дереву.

Изученные файлы:

- `project/llm/router.py`;
- `project/llm/tests/test_router.py`;
- `project/llm/eval/router_dataset.json`;
- router-части `project/llm/worker.py`;
- router-части `project/llm/eval/run_evals.py`;
- router-части `project/admin/eval_runner.py`;
- router-части `project/admin/eval_routes.py`;
- router-части `project/admin/eval_database.py`;
- `project/admin/templates/eval_router_list.html` и связанные шаблоны.

Дополнительно изучен внутренний Codex snapshot `49ea6d7`. Он не входит в `main`, не считается реализацией и используется только как read-only источник тестовых границ.

## Gap-анализ: берём / адаптируем / отклоняем

| Решение референса | Что уже есть у нас | Подтверждённый пробел | Решение | Причина | Проверка |
|---|---|---|---|---|---|
| Отдельный дешёвый Router LLM | Только локальный `route_message()` в `moroz.messaging.router` | Нет контекстной классификации сложного free text | Адаптировать | LLM вызывается только для `unresolved`, а не для каждого обычного сообщения | Unit + Router Evaluation quality cases |
| До шести недавних реплик и лимит 2000 символов | Security pipeline уже формирует masked context | Роутер контекст не получает | Взять | Ограничивает latency, стоимость и объём недоверенных данных | Context boundary tests |
| Параллельные Input Security и Router вызовы | Текущий `SecurityPipeline` вызывает guard и answer последовательно | Нет параллельной ветки с запретом использования route до ALLOW | Адаптировать | Экономим latency, сохраняя Security как обязательный gate | Controlled concurrency evaluation |
| Один route на сообщение | Наш `RouteDecision` уже поддерживает несколько intents | Один route теряет `faq + booking` и конфликты | Отклонить | Нужны до трёх intents и явное clarification | Multi-intent evaluation |
| Structured Outputs | Текущий `LLMRequest` не передаёт schema | Нет provider schema и строгого router parser | Адаптировать | Provider schema дополняется обязательной локальной проверкой | Invalid-output tests |
| Терпимое извлечение JSON из markdown/мусора | Текущий router LLM отсутствует | Trust-boundary parser не определён | Отклонить | Принимается только один точный JSON object без лишних полей | Parser tests |
| Fallback `consultation`, `confidence=1.0` | Локальный fallback `route_message()` возвращает `unknown` | Нет LLM fallback contract | Отклонить | Ложная уверенность может запустить неверный сценарий | Provider-failure evaluation |
| Router не выполняет workflow | Существующие booking/handoff компоненты отделены | Нужно закрепить side-effect boundary | Взять | Классификатор остаётся чистым и тестируемым | Spy-based no-side-effect tests |
| Отдельный `router_eval_cases` | Есть общие `eval_cases/eval_runs/eval_results` | Общая модель не различает suites и structured payload | Отклонить | Не создаём вторую eval-подсистему | Migration/schema tests |
| Отдельный router dataset и проблемный rerun | Есть versioned eval datasets и общий problem rerun | Нет suite-aware router dataset/statistics | Адаптировать | Dataset остаётся versioned, данные и runs живут в общих таблицах | Dataset/admin eval tests |
| Логирование route/confidence | Есть safe correlation/event logging | Нет router-specific count-only trace | Адаптировать | Пишем только allowlisted metadata без input/raw response | Safe logging tests |

## Выбранный подход

Рассмотрены три варианта:

1. Вызывать Router LLM для каждого сообщения, дошедшего до общей обработки, как в Lucky Hair Studio. Это единообразно, но тратит время и токены на уже известные workflow.
2. Использовать только deterministic rules. Это дешевле, но плохо обрабатывает контекст, короткие продолжения и мультинтентные запросы.
3. Гибридный scripts-first router. Это выбранный вариант: stateful и доказуемо однозначные ветки не вызывают Router LLM, а `unresolved` получает контекстную LLM-классификацию.

## Intent-контракт

Allowlist:

- `faq` — услуги, цены, подготовка, адрес и расписание;
- `booking` — новая запись;
- `booking_change` — перенос существующей записи;
- `booking_cancel` — отмена существующей записи;
- `complaint` — жалоба, возврат или конфликт;
- `human_handoff` — явная просьба позвать человека;
- `smalltalk` — приветствие, благодарность или короткая реакция;
- `offtopic` — посторонняя тема;
- `other` — запрос относится к центру, но отдельного сценария нет;
- `unknown` — смысл определить не удалось.

`medical_risk` не является LLM-router intent. Это авторитетный outcome вышестоящего security-слоя; роутер не может его создать, отменить или понизить.

Runtime-результат:

```python
RouteDecision(
    intents=("faq", "booking"),
    requires_clarification=False,
    source="llm",
    confidence=0.91,
    reason_code=None,
)
```

Контракт:

- `intents` содержит от одного до трёх уникальных значений allowlist;
- `source` равен `deterministic`, `llm` или `fallback`;
- `confidence` используется только для telemetry и quality analysis, но не разрешает side effects;
- `reason_code` равен `None` при штатном результате или одному безопасному allowlisted коду;
- конфликт `booking_change + booking_cancel` всегда включает `requires_clarification=True` локальной policy;
- изменяющий workflow требует собственных существующих проверок и подтверждений независимо от route confidence.

## Формальное понятие `resolved` и `unresolved`

Deterministic слой возвращает `resolved`, если сценарий доказуемо задан:

- Telegram-командой или callback payload;
- уже активным durable workflow и ожидаемым шагом;
- privacy, stop, deletion, human-mode или обязательным security outcome;
- узким локальным правилом, которое нашло один совместимый intent и не зависит от предыдущей реплики.

Он возвращает `unresolved`, если:

- точного правила нет;
- найдены конфликтующие intents;
- смысл зависит от предыдущих сообщений;
- сообщение является коротким продолжением вроде «давайте на завтра» или «нет, другую»;
- запрос объединяет несколько сценариев;
- совпадение локального правила не доказывает намерение пользователя.

Числового «процента уверенности regex» нет. Ветка определяется проверяемыми условиями.

## Runtime data flow

```text
durable Telegram message
→ consent / privacy / deletion fence / human mode
→ local guardrails и обязательные security outcomes
→ commands, callbacks и active workflow dispatcher
→ PII masking текущего сообщения и ограниченного недавнего контекста
→ deterministic intent classifier
    resolved:
      → при необходимости Input Security LLM
      → применять route только после Security=ALLOW
    unresolved:
      → параллельно Input Security LLM + Router LLM
      → применять router verdict только после Security=ALLOW
→ выбранный handler / основная answer LLM
→ output validation
→ durable outbound delivery
```

## Параллельный Security + Router contract

На `unresolved`-ветке после local guardrails и обязательного PII masking одновременно создаются две task:

- Input Security LLM;
- Router LLM.

Обе получают только masked current message и ограниченный masked context. Router task может завершиться первой, но её результат нельзя читать, публиковать в trace или передавать downstream до завершения Input Security.

Матрица результата:

| Security | Router | Итог |
|---|---|---|
| `ALLOW` | валидный verdict | Использовать route |
| `ALLOW` | provider error или invalid verdict | Deterministic fallback, затем `unknown` |
| `BLOCK` | любой результат | Отбросить route и вернуть только security-safe response |
| Security error | любой результат | Security fail-closed; отбросить route |
| Оба вызова завершились ошибкой | — | Security fail-closed; route отсутствует |
| Внешняя cancellation | — | Отменить и дождаться обеих task, затем пробросить cancellation |

При `BLOCK` или security error Router task отменяется best-effort и обязательно дренируется, чтобы не оставить необработанное исключение. Отмена task не является доказательством, что внешний HTTP-запрос не успел состояться.

Никакой workflow, основная answer LLM, handoff, запись в outbox или другой side effect не запускается до положительного Security verdict.

## Принятый остаточный риск

Заблокированный запрос может успеть попасть в Router provider, поскольку Router и Input Security стартуют параллельно. В provider попадает только masked текст и ограниченный masked context после обязательного PII gate.

Риск принят владельцем ради уменьшения latency при следующих компенсирующих мерах:

- Router не получает основной system prompt, базу знаний, секреты, tools или raw PII;
- Router использует отдельный короткий immutable prompt;
- Router является tool-less classifier и не выполняет side effects;
- Router verdict не используется до `Security=ALLOW`;
- при `BLOCK` verdict полностью отбрасывается;
- raw router input/output и provider exception text не попадают в логи или eval reports.

## Router LLM boundary

Router получает:

- masked current message;
- до шести последних masked реплик ролей `user/assistant`;
- общий bounded transcript не длиннее 2000 символов;
- отдельный короткий system prompt с описанием allowlist.

Router не получает:

- основной prompt ассистента;
- базу знаний или catalog payload;
- секреты, credentials или tools;
- raw PII;
- внутренние exception details.

LLM output — strict JSON object без дополнительных полей:

```json
{
  "intents": ["faq", "booking"],
  "confidence": 0.91
}
```

Provider-side JSON schema не заменяет локальную валидацию. Локальный parser отклоняет markdown, текст вокруг JSON, неизвестные/повторные intents, больше трёх intents, boolean вместо числа, `NaN`/`Infinity`, confidence вне `0..1`, пустой объект и дополнительные поля.

## Fallback

Если `Security=ALLOW`, но Router упал или вернул invalid output:

1. Повторно используется чистый deterministic classifier на уже masked input.
2. Если он теперь даёт единственный безопасный result, возвращается `source="fallback"`, `confidence=None` и безопасный `reason_code`.
3. Если однозначного результата нет, возвращается `unknown` с требованием нейтрального уточнения.

Fallback не возвращает `consultation + confidence=1.0`, не запускает изменяющее действие и не повышает route до handoff без отдельного основания.

## Downstream mapping

- `faq`, `smalltalk`, `other` и допустимый `unknown` передают route metadata основной answer LLM;
- `booking`, `booking_change`, `booking_cancel` выбирают существующий workflow, но не обходят его проверки/подтверждения;
- `complaint` и `human_handoff` выбирают существующий durable handoff;
- `offtopic` получает короткий безопасный ответ без основной answer LLM;
- `requires_clarification=True` запрещает запуск конфликтующего workflow до уточнения.

## Usage и наблюдаемость

Каждый внешний вызов сохраняет собственную usage-запись с purpose `security`, `router` или `answer`, фактической моделью и token counts. Локальные deterministic решения не создают нулевых provider-usage строк.

Allowlisted trace содержит только:

- `source`;
- intents;
- `requires_clarification`;
- bucketed confidence;
- безопасный `reason_code`;
- duration и count-only outcome.

Текст пользователя, masked transcript, raw provider response, exception message и секреты в runtime logs не записываются.

## Общая eval-модель

Новая таблица `router_eval_cases` не создаётся. Additive migration расширяет существующую модель:

### `eval_cases`

- `suite VARCHAR(32) NOT NULL DEFAULT 'answer'`;
- `case_key VARCHAR(96) NULL`;
- `input_data JSONB NOT NULL DEFAULT '{}'`;
- `expected_data JSONB NOT NULL DEFAULT '{}'`;
- `critical BOOLEAN NOT NULL DEFAULT FALSE`;
- partial unique index `(suite, case_key)` для непустого `case_key`.

Существующие answer-кейсы сохраняют текущие поля и получают `suite='answer'`. Router-кейс использует `question` как безопасный display text, `expected_answer=''`, а structured contract хранит в JSONB.

### `eval_runs`

- `suite VARCHAR(32) NOT NULL DEFAULT 'answer'`.

### `eval_results`

- `actual_data JSONB NOT NULL DEFAULT '{}'`.

Текущие foreign keys `eval_results.case_id → eval_cases.id` и `eval_results.run_id → eval_runs.id` сохраняются. Общий admin runner, progress, detail view и task supervision переиспользуются.

## Router dataset

`project/llm/eval/router_dataset.json` — versioned synthetic dataset без реальных ПД. Каждый кейс содержит:

```json
{
  "case_key": "router-context-booking-001",
  "category": "context",
  "input": "Да, давайте завтра",
  "context": [
    {"role": "assistant", "content": "Хотите записаться на криотерапию?"}
  ],
  "expected_intents": ["booking"],
  "expected_clarification": false,
  "expected_source": "llm",
  "critical": false
}
```

Initial additive migration загружает immutable snapshot dataset в общую `eval_cases`. Контрактный тест подтверждает совпадение migration seed и versioned JSON. Любое последующее изменение dataset выполняется новой additive data migration; история существующих run/results не переписывается.

## Router Evaluation

Suite `router` не вызывает основную answer LLM и LLM judge. PASS определяется детерминированным comparator по structured result.

Quality categories:

- simple deterministic;
- ambiguous free text;
- context continuation;
- multi-intent;
- conflicting booking actions;
- FAQ и booking combinations;
- complaint и explicit human handoff;
- smalltalk, offtopic, other и unknown;
- prompt-injection-like input как недоверенные данные;
- provider invalid output и unavailable fallback.

Structural categories используют controlled fake providers и проверяют:

- реальный параллельный старт Security и Router на `unresolved`;
- запрет чтения/использования route до завершения Security;
- полное отбрасывание route при `BLOCK`;
- отсутствие PII, основного prompt, базы знаний, tools и секретов в router request;
- отсутствие side effects до `ALLOW`;
- `ALLOW + Router error`;
- `BLOCK + Router success/error`;
- `Security error + Router success/error`;
- одновременный сбой обоих вызовов;
- корректную cancellation и дренирование task;
- отсутствие raw input/output/errors в logs и reports.

Отдельная статистика фильтруется по `eval_runs.suite='router'`. «Прогнать проблемные» выбирает только активные router-кейсы, у которых последний result внутри suite имеет `fail` или `error`; answer suite не смешивается с router suite.

## Error handling

- Router provider errors переводятся в безопасные типизированные codes без exception text.
- Invalid output считается Router failure и проходит fallback contract.
- Eval runner failure не влияет на runtime.
- Background eval task сохраняет terminal status и безопасный error type.
- Миграция additive: существующие answer cases/runs/results не удаляются и не перезаписываются.
- Любой route conflict или низкая telemetry confidence не разрешает side effect.

## Acceptance gates

Пара Router + Router Evaluation считается завершённой только после одновременного выполнения:

1. Runtime router и suite `router` реализованы.
2. Quality и structural cases проходят в общем admin eval-runner.
3. Отдельная router statistics и problem-only rerun подтверждены.
4. PII/secrets/raw provider data отсутствуют в requests, traces и reports согласно контракту.
5. Parallel Security/Router ordering и side-effect gate доказаны controlled tests.
6. Provider/invalid-output/cancellation fallbacks доказаны.
7. Focused Docker tests, затронутая регрессия, migration cycle и полный Docker suite проходят без failures.
8. Независимый review не оставляет Critical/Important findings.
9. `Дорожная карта.md` и `changelog.md` обновлены фактическим evidence; staging/production не меняются без отдельного решения.
