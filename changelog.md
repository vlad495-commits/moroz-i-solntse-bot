# Changelog

История всех значимых действий в проекте. Формат: `[YYYY-MM-DD HH:MM] что произошло` (UTC+3, Москва).

---

[2026-07-02 19:14] Инициализация проекта Moroz i Solntse Bot — ступень 1 (прототип). Развёрнут шаблон бота: bot.py, handlers.py, llm.py, db.py, cache.py, config.py + Docker (3 контейнера: llm, redis, postgres).

[2026-07-02 19:14] Project initialized. Step 1 (prototype). Model: gpt-4.1-mini. Secrets for Telegram and LLM are left empty for local setup.

[2026-07-03 15:47] Deploy preparation: llm healthcheck changed from recent-log freshness to Python bot process check, so an idle but running Telegram polling bot is not marked unhealthy.

[2026-07-03 17:00] Upgrade to step 2 (Admin). Added FastAPI admin panel on port 8080, prompt editor with versions/rollback, bot on/off control, logs view, token usage storage, prompt hot-reload listener, and admin service in Docker Compose.

[2026-07-03 17:25] Roadmap updated after step 2 deploy. Added project rule: update roadmap and changelog after each completed step.
