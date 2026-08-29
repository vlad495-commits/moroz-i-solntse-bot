# Simple Security Pipeline — design

Дата: 2026-08-29.

## Цель

Упростить input-security по философии Lucky Hair Studio commit
`5398f909829f5db1b5052087f5a826c2bbcd5244`: локальные однозначные правила,
узкий masked Security LLM с ответом `OK/BLOCK`, параллельный Router и минимум
runtime-состояний.

## Согласованные решения

- `check_input` остаётся обязательной первой границей и локально обрабатывает
  пустой/длинный ввод, rate limit, prompt injection, чужие ПД, опасные инструкции,
  stop и medical escalation.
- После локального прохода `PiiSession` маскирует ПД до любого внешнего LLM.
- Security LLM проверяет каждую текущую реплику, прошедшую локальную границу.
- Security получает только текущий masked text, без истории и без verdict cache.
- Внешний контракт Security — точное `OK` или `BLOCK`, без JSON и категорий.
  Невалидный ответ считается ошибкой модели.
- Сначала вызывается отдельная дешёвая security-модель, затем существующий reserve.
  При полном отказе используется референсный `fail-open`: сообщение проходит дальше,
  но обязательны статический critical alert и безопасный лог без input, response и
  exception text.
- Если нужен Router LLM, его задача стартует одновременно с Security. Router может
  получать bounded masked history, но его verdict нельзя использовать до Security
  `OK`/fail-open.
- При `BLOCK` Router отменяется и дожидается через существующий cancel-and-drain;
  ошибки asyncio-задач обязательно извлекаются.
- Локальная output validation всегда обязательна. Semantic LLM output-validator
  управляется только env-флагом `OUTPUT_VALIDATOR_ENABLED`, по умолчанию `false`.
  Redis-toggle и выборочная risk state machine не добавляются.

## Не переносим из референса

- Redis-кэш `OK`, потому что требуется проверять каждую реплику.
- Мягкий parser «всё, что не BLOCK, разрешено»; принимаются только точные слова.
- Текст исключения провайдера в alert/log.
- Фоновое отпускание Router при `BLOCK`; используется уже имеющийся безопасный drain.

## Данные и наблюдаемость

- Security request: owned system prompt + один current masked text.
- Router request: current masked text + bounded masked history.
- Alert/log: только allowlisted code, source, model и error type; raw/masked message,
  provider payload, prompt и exception text запрещены.
- Eval category остаётся метаданными dataset, но не частью runtime verdict.

## Риски

- Полный отказ двух security-моделей оставляет только локальную защиту.
- Security-вызов на каждой разрешённой реплике увеличивает latency и стоимость.
- Отменённый Router HTTP-запрос может уже быть принят и оплачен провайдером.
- При выключенном semantic validator снижается контроль тона/связности, но не
  локальные canary, leak, PII, placeholder и fact checks.

