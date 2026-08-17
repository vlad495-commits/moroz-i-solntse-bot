# Готовая HTML-архитектура продукта — дизайн

## Цель

Обновить `docs/moroz-i-solntse-full-architecture.html`, чтобы один автономный файл одновременно показывал фактический staging-контур и согласованную целевую архитектуру продукта.

## Статусный язык

- зелёный `РАБОТАЕТ` — реализация подтверждена кодом и локальным либо staging evidence;
- жёлтый `КОД ЕСТЬ · НУЖНА ПРОВЕРКА` — код существует, но внешний или production gate не закрыт;
- серый пунктирный `ПЛАН` — согласованный компонент ещё не реализован.

## Стабильный каркас

Каркас остаётся прежним: канальные адаптеры → privacy/consent → durable inbox и буфер → RabbitMQ/worker → scripts-first routing и защищённый LLM → доменные сценарии/YCLIENTS → durable outbox → канал. PostgreSQL хранит критическое состояние, Redis — временное, RabbitMQ — задачи.

## Добавляемые целевые блоки

1. Единый `customer_id`, отдельные channel identities, подтверждение связи, обратимые merge/split и аудит.
2. Реактивация и ручные кампании: draft/preview/test-send, marketing consent и unsubscribe, выбор аудитории, уникальный ключ кампании и клиента, scheduler, outbox, лимиты, stop/pause, остановка при ответе, аналитика.
3. VK, Instagram и WhatsApp как planned-адаптеры общего inbox/outbox pipeline.
4. Allowlisted technical journal по `trace_id`/`correlation_id` без raw prompt, ПД и секретов.
5. Актуальный статус YCLIENTS: каталог и lifecycle-код отделены от незакрытого production evidence и проблемной staging booking projection.

## Исключения

Голосовые, SpeechKit и чат сайта не входят в согласованный четырёхканальный backlog и удаляются из схемы. Новые библиотеки, JavaScript и внешние ресурсы не добавляются.

## Проверка

Существующий Python/HTML contract расширяется test-first. После зелёного Docker-прогона файл проверяется в локальном Docker-preview на широком и узком viewport: без горизонтального переполнения, наложений и потери статусной легенды.
