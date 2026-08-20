# Eval Catalog and Guardrails Design

## Цель

Привести локальные эвалы в соответствие с runtime после подключения YCLIENTS catalog grounding, закрыть 18 adversarial bypass и исправить шесть содержательных сигналов без обращения к реальным интеграциям.

## Границы

- Не обращаться к YCLIENTS, Telegram, staging или production.
- Не менять секреты и judge settings.
- Не ослаблять и не редактировать `adversarial_dataset.json`.
- Не возвращать динамические цены, длительности и мастеров в системный prompt.
- Существующий `dataset.json` не перезаписывать; новые catalog-кейсы хранить отдельно.

## Архитектура

### Catalog eval

`admin/eval_runner.py` получает необязательный `CatalogGrounding` и передаёт его в существующий `SecurityPipeline.respond(..., catalog=...)`. Обычная админка продолжает работать без synthetic данных. Отдельный `catalog_dataset.json` содержит шесть reusable-кейсов и только вымышленные услуги/цены.

Проверяются: приоритет свежего каталога, отсутствие выдуманной услуги, ambiguity, stale/missing, недоверенные инструкции внутри catalog data и output validation цены вне выбранных facts.

### Structural eval

Consent, primary/reserve, providers unavailable, nonretryable provider и non-text ingress не оцениваются LLM-судьёй. Общий structural evaluator возвращает детерминированный PASS/FAIL и используется CLI и admin runner.

### Guardrails

Все 20 universal adversarial inputs должны блокироваться локально до LLM. Правила группируются по двум признакам: поддельный privileged/debug context и запрос/изменение внутренних инструкций, переменных или prompt. Обычные вопросы центра остаются разрешены отдельными negative-контрактами.

### Постоянные знания и safe replies

В prompt возвращаются только стабильные сведения, случайно потерянные при catalog cutover: адрес, направления выбора, стартовая длительность загара и состав программ. Цены программ остаются catalog-only. Ответы на prompt leak, medical promise и неподтверждённый slot становятся полезнее, но не обещают действие или результат.

## Ошибки и безопасность

- Некорректный synthetic catalog case падает fail-closed.
- Structural case не вызывает primary или judge.
- Catalog data остаётся `UNTRUSTED_CATALOG_DATA` и не расширяет allowlist контактов.
- Guard decision не хранит пользовательский текст.
- Реальные provider credentials не попадают в отчёты и логи.

## Проверка

Каждый блок реализуется TDD: отдельный Docker RED, минимальный GREEN и расширенный regression gate. Финал — fresh admin judge-run, dedicated catalog eval, adversarial CLI и точная очистка Compose namespace.
