# Цены и понятные уточнения — implementation plan

**Goal:** закрыть три замечания ручной приёмки: цены в консультации, меню прайса, повтор вопроса о свободном времени.
**Architecture:** используем существующие router/pipeline/catalog и durable Telegram coordinator. LLM извлекает название услуги из текущего вопроса и контекста; каталог загружается после security/router, даёт только проверенные данные. Категории и карточки — технические callback существующего сценария, без новой таблицы и без создания записи до подтверждения.
**Tech Stack:** Python, aiogram, PostgreSQL, Docker, pytest; без новых зависимостей.

Пользователь одобрил предложенный UX категорий и карточек и попросил исправить всё. Ветка `codex/semantic-booking-repair`; реализация inline по executing-plans. Альтернатива полного прайса одним сообщением отклонена из-за длины; копия цен в промпте — из-за устаревания.

- [x] RED: worker/pipeline консультация «Сколько стоит?» с семантической услугой получает каталог; blocked/fallback не загружают его.
- [x] GREEN: `worker/main.py` передаёт lazy catalog resolver, `security/pipeline.py` вызывает его после маршрутизации. Router service используется и для консультации по контексту; цена не извлекается из истории.
- [x] RED/GREEN: `tests/e2e/booking/test_semantic_booking.py` — меню категорий, цены/длительность, карточка и переход в запись; неоднозначный массаж сохраняет дату и объясняет необходимость выбора.
- [x] Implement: `booking/telegram.py` — категории по YCLIENTS, постраничные услуги с ценами, карточка с вариантами и существующим переходом в запись. Свежесть проверяется при открытии меню/категории/карточки и повторном выводе; старые callback и ownership проверяются существующим механизмом.
- [x] Docker regression: unit security/messaging/booking, e2e booking/catalog/message_delivery — 796 passed in 599.50s; последние изменения: 24 passed и финальный targeted 3 passed.
- [x] Diff/review проверены; runtime commit `06e5c13` развёрнут на том же staging, `YCLIENTS_CATALOG_GROUNDING_ENABLED=true` подтверждён внутри worker. 8/8 healthy, schema 0025, HTTPS/admin, webhook, scheduler и safe logs проходят. Production и GitHub не изменены.
- [ ] Повторная ручная приёмка владельцем с реальной LLM.

При неоднозначном контексте бот уточняет услугу, а не выбирает её молча. Диапазон цен отображается с пояснением вариантов. Walk-in услуги остаются без слотов. Проверки не создают реальные записи и не вызывают платную LLM.

Read-only review: выявлены и подтверждены RED два случая — retry меню возвращал failed-сценарий, а повторный вывод/пагинация использовали cached prices. Исправлены replay и обязательный fresh-render; отдельно RED/GREEN подтвердил clamp страницы при сокращении каталога. Целевой gate 24 passed, финальные 3 regression passed; Compose/compileall/runtime Ruff проходят.
