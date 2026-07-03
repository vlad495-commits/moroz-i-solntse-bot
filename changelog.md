# Changelog

История всех значимых действий в проекте. Формат: `[YYYY-MM-DD HH:MM] что произошло` (UTC+3, Москва).

---

[2026-07-02 19:14] Инициализация проекта Moroz i Solntse Bot — ступень 1 (прототип). Развёрнут шаблон бота: bot.py, handlers.py, llm.py, db.py, cache.py, config.py + Docker (3 контейнера: llm, redis, postgres).

[2026-07-02 19:14] Project initialized. Step 1 (prototype). Model: gpt-4.1-mini. Secrets for Telegram and LLM are left empty for local setup.

[2026-07-03 15:47] Deploy preparation: llm healthcheck changed from recent-log freshness to Python bot process check, so an idle but running Telegram polling bot is not marked unhealthy.

[2026-07-03 17:00] Upgrade to step 2 (Admin). Added FastAPI admin panel on port 8080, prompt editor with versions/rollback, bot on/off control, logs view, token usage storage, prompt hot-reload listener, and admin service in Docker Compose.

[2026-07-03 17:25] Roadmap updated after step 2 deploy. Added project rule: update roadmap and changelog after each completed step.

[2026-07-03 17:58] Roadmap expanded for the next target version. Fixed scope: fast baseline Telegram LLM bot plus internal testing admin panel; YCLIENTS API moved after test launch; next major block is evals.

[2026-07-03 18:20] System prompt updated to v1.1 locally. Clarified response style, prompt-safety boundaries, appointment fallback, admin handoff wording, and quality checks without changing code, Docker, or environment files.

[2026-07-03 18:24] System prompt updated to v1.2 locally. Added a quick response algorithm, stronger anti-hallucination rules for appointments/slots, sensitive medical handling, admin handoff examples, and roadmap notes without changing code, Docker, or environment files.

[2026-07-03 18:24] Tried to apply prompt v1.2 through the deployed admin panel. Login worked, but `/prompt/save` returned `write_failed` for `/app/prompts/system.md`; added a roadmap task to check server write permissions/volume before hot-reload can apply the prompt live.

[2026-07-03 18:37] Created `project/data/services_prices.md` as a draft knowledge-base price file from the current prompt. Marked all prices as requiring client confirmation and added a checklist of full price-list data to request from the client.

[2026-07-03 20:22] Начат апгрейд со ступени 2 на ступень 3 (Эвалы): проверены `project/.step`, манифест `step-3-evals/UPGRADE.md` и текущие незакоммиченные изменения.

[2026-07-03 20:22] Апгрейд на ступень 3 (Эвалы). Добавлены eval-runner, CRUD тест-кейсов в админке `/eval/`, таблицы `eval_cases`, `eval_runs`, `eval_results`, SSE-прогресс прогонов, настройки judge-модели и локальный скилл `evals`. `project/.step` обновлён до `3`, дорожная карта обновлена.
