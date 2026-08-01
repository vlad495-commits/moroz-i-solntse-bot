# Задача: проверить и закрыть provider-side PII masking

## Контекст

В targeted guardrails QA от 2026-08-01 вручную через Telegram проверены prompt/canary leak, instruction override, medical-risk, fake price, clean `стоп`, rate limit и восстановление после rate limit.

Незакрытый нюанс: через Telegram видно только внешний результат. Мы увидели, что бот не повторяет фейковые телефон/email в ответе, но не подтвердили тестом, что PII не уходит во внешний LLM-provider payload.

Отчет: `tmp/manual-test-20260801-guardrails/Отчет по тестированию бота.md`

## Что не прошло / не было подтверждено

`PII provider-side masking` не подтвержден свежим focused Docker unit/integration gate.

Причина: Docker Desktop daemon не был запущен, команда Docker Compose не смогла стартовать.

## Где смотреть код

- `project/src/moroz/security/pii.py` — маскирование email, phone, name, address, handles, payment, medical.
- `project/src/moroz/security/pipeline.py` — `PiiSession`, masked context/current input, restore после validator.
- `project/src/moroz/security/llm_gateway.py` — структура LLM-запроса.
- `project/tests/unit/security/` — unit-покрытие security pipeline.
- `project/tests/unit/test_safe_logging.py` — проверки безопасного логирования.

## Минимальный план починки/проверки

1. Запустить Docker Desktop.
2. Прогнать focused gate:

```powershell
Set-Location -LiteralPath 'D:\AI_Projects\moroz_i_solntse\moroz-i-solntse-bot\project'
$env:RABBITMQ_USER='unit'
$env:RABBITMQ_PASSWORD='unit-password'
$env:RABBITMQ_URL='amqp://unit:unit-password@rabbitmq:5672/'
$env:TELEGRAM_WEBHOOK_SECRET='dummy-webhook-secret-for-unit-tests-only'
docker compose --env-file ..\.env run --rm test pytest -q tests\unit\security tests\unit\test_safe_logging.py
Remove-Item Env:\RABBITMQ_USER,Env:\RABBITMQ_PASSWORD,Env:\RABBITMQ_URL,Env:\TELEGRAM_WEBHOOK_SECRET -ErrorAction SilentlyContinue
```

3. Если gate падает, исправить так, чтобы:
   - raw phone/email/name/address/payment/medical не попадали в `LLMRequest`;
   - история диалога тоже маскировалась перед отправкой в LLM;
   - ответ не мог вернуть неизвестный `<PII_...>` placeholder;
   - после validator разрешенные placeholder восстанавливались только из текущей `PiiSession`.

## Definition of Done

- Focused gate выше проходит.
- В тесте с фейковыми PII `CapturingGateway.requests` не содержит raw phone/email/name.
- `git diff --check` чистый.
- Результат занесен в `changelog.md`.

## Важно

Не использовать реальные персональные данные. Для тестов брать только фейки вроде `Иван Тестов`, `+7 999 123-45-67`, `test@example.com`.
