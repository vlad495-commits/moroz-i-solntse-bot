# Production Telegram V1 — LLM Security Design Checkpoint

**Статус:** техническая актуализация согласованного дизайна от 2026-07-14

**Основание:** пользователь поручил реализовать Phase 5 строго по `ТЗ и архитектура.md`, согласованной `2026-07-14-production-telegram-v1-design.md` и фазовому плану. Нового продуктового выбора этот checkpoint не вводит.

## 1. Фактическое стартовое состояние

Production Telegram path после фаз 1–4:

```text
Telegram webhook
-> privacy/consent gate
-> durable inbox
-> Redis buffer
-> transactional task outbox
-> RabbitMQ
-> worker MessageTaskHandler
-> llm.generate_response
-> durable messages/token usage/outbound
-> Telegram sender
```

До согласия текст не сохраняется и не передаётся LLM. После согласия worker передаёт во внешнюю модель исходный объединённый текст и raw history; резервный provider в runtime пока не подключён. Admin eval runner использует отдельный provider path, поэтому защита только в `project/llm/llm.py` не покрыла бы judge/eval boundary.

## 2. Рассмотренные подходы

### A. Патч только legacy `llm.py`

Минимальный diff, но guardrails, masking и validator останутся связаны с одним entrypoint. Admin eval/judge path продолжит обходить общие правила, а worker orchestration и typed routing останутся неявными.

### B. Полная новая LLM-платформа

Отдельные сервисы для router, guard, main model, validator, knowledge retrieval и metrics. Это ближе к дальней целевой схеме, но создаёт новые процессы, зависимости и интерфейсы без необходимости для Telegram V1.

### C. Один общий in-process security pipeline — выбран

Общий пакет `moroz.security` получает тонкие adapters существующих OpenAI/Anthropic clients. Worker, polling compatibility path и eval runner используют один pipeline. Scripts-first проверки остаются локальными; внешняя guard-классификация вызывается только для неоднозначных instruction-shaped запросов и получает уже замаскированный текст.

Подход C соответствует согласованному модульному монолиту, покрывает все текущие внешние LLM boundaries и не добавляет сервисов или библиотек.

## 3. Компоненты

### PII session

- Маскирует текущий текст и всю передаваемую history до любого внешнего вызова.
- Использует стабильные typed placeholders внутри одного pipeline invocation.
- Покрывает телефон, email, ФИО по явным контекстным маркерам, адрес, social/channel handles, платёжные номера и чувствительные медицинские фрагменты.
- Хранит mapping только в памяти текущего invocation.
- После output validation восстанавливает только placeholders, которые появились в текущем пользовательском сообщении.
- Любой неизвестный или неразрешённый placeholder блокирует output.

### Provider gateway

- SDK clients создаются с `max_retries=0`.
- Primary вызывается один раз.
- Reserve вызывается один раз только после connection/timeout, HTTP `408`, `409`, `429` или `5xx`.
- Auth, permission, malformed request/response и programming errors не переключаются на reserve.
- Если оба допустимых вызова недоступны, pipeline возвращает безопасный fallback без unbounded retry.

### Scripts-first guard and router

- Повторяет trust-boundary проверки длины, пустого ввода и rate limit перед LLM.
- Локально распознаёт явный jailbreak, prompt leak, stop request и опасную медицинскую формулировку.
- Явные безопасные запросы проходят без guard-model call.
- Неоднозначный instruction-shaped запрос может получить один masked guard call с ответом только `ALLOW` или `BLOCK`.
- Router детерминированно возвращает ordered intents для complaint/medical risk, booking/change/cancel и FAQ. Router не выполняет YCLIENTS mutations и не обещает слот.

### Structured facts and output validator

- Цены и допустимые публичные контакты загружаются из versioned `project/data/services_prices.md`/системного prompt source, а не из ответа LLM.
- Доступные booking slots передаются только сценарием; при их отсутствии любое утверждение о свободном точном времени блокируется.
- Validator проверяет canary/internal-instruction leak, unknown PII placeholders, новые raw contacts, медицинские гарантии и неподтверждённые цены/слоты.
- После первого invalid output разрешена ровно одна повторная генерация с коротким machine-owned reason code.
- Второй invalid output даёт безопасную заглушку. Raw provider response и пользовательский текст не логируются.

### Shared orchestration

Один `SecurityPipeline.respond()` используется runtime и eval path. Он возвращает совместимые `text` и token usage, чтобы существующая durable запись сообщений и расходов не меняла контракт. Local block/fallback также материализуется как устойчивый outbound и помечает inbox processed, а не создаёт бесконечный Rabbit retry.

## 4. Data flow

```text
consented persisted input/history
-> local validation + rate evidence
-> PII session masks current input and history
-> local guard decision
-> optional masked guard call
-> deterministic route metadata
-> primary/reserve gateway
-> output validator
-> at most one corrected generation
-> validated current-turn placeholder restore
-> durable worker transaction/outbox
```

Raw consented history остаётся в PostgreSQL по действующей retention policy, но наружу уходит только masked copy. Mapping placeholders не сохраняется в Redis/PostgreSQL и не переиспользуется между задачами.

## 5. Error policy

- Local block: безопасный статический ответ, external calls `0`.
- Guard provider unavailable: fail closed для неоднозначного security input.
- Primary retryable failure: один reserve call.
- Non-retryable provider failure: безопасный fallback без reserve.
- Invalid output: один retry; повторный invalid output — fallback.
- Unknown placeholder: fallback и safe reason code.
- Security path никогда не пишет raw input, raw output, mapping, endpoint или exception message в лог.

## 6. Verification

- Unit: PII stability/restore, gateway classification, scripts-first zero-call paths, router priority, validator codes, retry bound.
- E2E: consent boundary остаётся прежней; worker передаёт provider только masked input/history; local block/fallback устойчиво завершает inbox/outbox; provider calls ограничены.
- Eval: критические security cases имеют отдельный machine-readable признак; gate требует `100%` critical и `>=95%` total.
- Docker-only: targeted RED/GREEN, затем полный no-cache Compose suite, migration head, compile/config/import/static secret scan и exact namespace cleanup.
- Никаких staging/production/provider mutations в Phase 5 verification.

