# Router legacy cleanup: план B1

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** оставить один источник services и только публичные действия.

**Architecture:** существующий Router/parser/valid_route_action, без новых компонентов.

**Tech Stack:** Python, pytest, Docker.

## Global Constraints

- Не менять публичную schema, immutable datasets/migrations, topics-контракт, сервер и main.
- Сохранять guards choice, confirmation, ownership; cancel_draft проверяет весь services.

## Задача 1: parser и callers

Files: project/src/moroz/messaging/router.py; project/tests/unit/messaging/test_router.py; затронутые исполняемые callers в project/tests/unit/security/test_semantic_dispatch.py и project/tests/e2e/test_catalog_message_flow.py.

- [ ] RED: legacy service и price/duration/staff/clarify_cancel отклоняются; valid_route_action(RouteDecision('booking', .99, 'cancel_draft', services=('A', 'B'))) возвращает False.
- [ ] Удалить поле service/__post_init__/legacy parsing; services = data.get('services', []), убрать legacy actions из ROUTE_ACTIONS; для cancel_draft вернуть not decision.services and decision.date is None.
- [ ] Заменить исполняемые calls service='X' на services=('X',), консультационные actions на none + topics. Проверить позиционные вызовы RouteDecision, не допустить сдвига аргументов.
- [ ] GREEN: Docker test pytest --rootdir=/workspace -q -p no:cacheprovider /workspace/tests/unit/messaging/test_router.py /workspace/tests/unit/security /workspace/tests/unit/admin/test_router_eval_runner.py /workspace/tests/e2e/test_catalog_message_flow.py --tb=short --show-capture=no (общий изолированный Compose префикс из B0).
- [ ] Read-only review, исправить замечания, повторить затронутые тесты; root фиксирует отдельный коммит, roadmap и changelog.
