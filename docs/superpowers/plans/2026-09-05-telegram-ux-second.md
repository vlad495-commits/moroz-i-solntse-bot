# Telegram UX second package Implementation Plan

> Execute task-by-task using executing-plans; independent reviewer after implementation.

**Goal:** закрыть весь согласованный follow-up UX-аудита.
**Architecture:** существующий coordinator, scenario state и durable outbound; display-only helper без новой БД.
**Tech Stack:** Python 3.12, aiogram, PostgreSQL, Docker Compose.

## Constraints
Только Docker; RED перед runtime-правками; без push, production и YCLIENTS mutations. Секреты не выводить. Временные артефакты только tmp/.

## 1. Каталог и карточки
Files: project/src/moroz/booking/telegram.py, project/src/moroz/booking/catalog.py, project/tests/e2e/booking/test_telegram_booking.py, project/tests/unit/booking/test_telegram_ux_second.py.
- [x] RED: singleton сразу catalog_book; девять категорий один экран; compact варианты без прайса; hydrogen короткие кнопки; walk-in отсутствует в service_choices; display name не меняет CatalogService.
- [x] GREEN: display helper; ветка singleton; единый renderer; возврат через append-only callback action; одна booking CTA.
- [x] Docker focused GREEN и локальный commit.

## 2. Контекст, подбор, подарок и дубли
Files: project/src/moroz/booking/telegram.py, project/worker/main.py, project/tests/e2e/test_catalog_message_flow.py, project/tests/unit/booking/test_telegram_ux_second.py.
- [x] RED: выбранная услуга + «Что это?»; parts-duration не возвращает общий duration; одинаковый старт подбора; gift URL actions; repeated callback без дубля и чужой callback fail-closed.
- [x] GREEN: context через active catalog state, deterministic goal screen, URLs из system.md; минимальная дедупликация сохранённого callback.
- [x] Docker focused GREEN и локальный commit.

## 3. Review и release
- [x] Независимый review diff и исправление находок test-first.
- [x] Свежий Docker build и gate booking/catalog/worker/privacy/menu/delivery; compileall, Compose config, diff-check.
- [x] Exact commit staging rollout по deploy skill: server preflight, bundle, rollback code/env/DB/images, immutable RC, health/schema/webhook/catalog/scheduler/logs.
- [x] Targeted Telegram Web приёмка design без подтверждения записи; ограничения и отдельный address follow-up записаны в отчёте.
- [x] Финальный server audit; roadmap/changelog и QA evidence; локальный документный commit.

Commands (из project/): docker compose -p moroz-ux-second --env-file ../tmp/ux-second.env --profile test build test; docker compose -p moroz-ux-second --env-file ../tmp/ux-second.env --profile test run --rm test pytest tests/unit/booking/test_telegram_ux_second.py -q.

Baseline: обнаружен выключенный Docker daemon; запущен Docker Desktop. До рабочего daemon runtime-код не изменяется.

## 4. Дополнение: плавные переходы записи (отдельный commit)
- [x] RED→GREEN coordinator: booking_card marker только structural reply, snapshot показанных шагов, booking_back, monotonic view revision, постоянное confirmation message.
- [x] RED→GREEN delivery: lookup по chat + scenario под fence, editMessageText, not-modified success, fallback только подтверждённый BadRequest, сеть без повторного send.
- [x] Независимый review, combined Docker gate, отдельный logical commit.
- [x] Включить forward/back/repeat/stale/fallback в приёмку и затем exact staging rollout.

## 5. Дополнение: постоянное меню 2×3 (отдельный commit)
- [x] RED→GREEN нового layout/aliases, deterministic routing и worker menu boundary.
- [x] Docker model serialization: success только запись, neutral остальные, standard emoji fallback.
- [x] Отдельный menu commit; финальный combined Docker gate.
- [x] Telegram Web визуально на desktop и mobile width; наблюдения по отсутствию настоящих Android/iOS записать явно.

Evidence: docs/audits/Telegram UX second — staging acceptance 2026-09-05.md. Runtime558b380; Docker588. Нативные Android/iOS и UI админ-диалога не заявляются проверенными.
