# Telegram UX second package Implementation Plan

> Execute task-by-task using executing-plans; independent reviewer after implementation.

**Goal:** закрыть весь согласованный follow-up UX-аудита.
**Architecture:** существующий coordinator, scenario state и durable outbound; display-only helper без новой БД.
**Tech Stack:** Python 3.12, aiogram, PostgreSQL, Docker Compose.

## Constraints
Только Docker; RED перед runtime-правками; без push, production и YCLIENTS mutations. Секреты не выводить. Временные артефакты только tmp/.

## 1. Каталог и карточки
Files: project/src/moroz/booking/telegram.py, project/src/moroz/booking/catalog.py, project/tests/e2e/booking/test_telegram_booking.py, project/tests/unit/booking/test_telegram_ux_second.py.
- [ ] RED: singleton сразу catalog_book; девять категорий один экран; compact варианты без прайса; hydrogen короткие кнопки; walk-in отсутствует в service_choices; display name не меняет CatalogService.
- [ ] GREEN: display helper; ветка singleton; единый renderer; возврат через append-only callback action; одна booking CTA.
- [ ] Docker focused GREEN и локальный commit.

## 2. Контекст, подбор, подарок и дубли
Files: project/src/moroz/booking/telegram.py, project/worker/main.py, project/tests/e2e/test_catalog_message_flow.py, project/tests/unit/booking/test_telegram_ux_second.py.
- [ ] RED: выбранная услуга + «Что это?»; parts-duration не возвращает общий duration; одинаковый старт подбора; gift URL actions; repeated callback без дубля и чужой callback fail-closed.
- [ ] GREEN: context через active catalog state, deterministic goal screen, URLs из system.md; минимальная дедупликация сохранённого callback.
- [ ] Docker focused GREEN и локальный commit.

## 3. Review и release
- [ ] Независимый review diff и исправление находок test-first.
- [ ] Свежий Docker build и gate booking/catalog/worker/privacy/menu/delivery; compileall, Compose config, diff-check.
- [ ] Exact commit staging rollout по deploy skill: server preflight, bundle, rollback code/env/DB/images, immutable RC, health/schema/webhook/catalog/scheduler/logs.
- [ ] Telegram Web приёмка всех контрактов design без подтверждения записи.
- [ ] Финальный server audit; roadmap/changelog и QA evidence; локальный документный commit.

Commands (из project/): docker compose -p moroz-ux-second --env-file ../tmp/ux-second.env --profile test build test; docker compose -p moroz-ux-second --env-file ../tmp/ux-second.env --profile test run --rm test pytest tests/unit/booking/test_telegram_ux_second.py -q.

Baseline: обнаружен выключенный Docker daemon; запущен Docker Desktop. До рабочего daemon runtime-код не изменяется.

## 4. Дополнение: плавные переходы записи (отдельный commit)
- [ ] RED→GREEN coordinator: booking_card marker только structural reply, snapshot показанных шагов, booking_back, monotonic view revision, постоянное confirmation message.
- [ ] RED→GREEN delivery: lookup по chat + scenario под fence, editMessageText, not-modified success, fallback только подтверждённый BadRequest, сеть без повторного send.
- [ ] Независимый review, combined Docker gate, отдельный logical commit.
- [ ] Включить forward/back/repeat/stale/fallback в приёмку и затем exact staging rollout.
