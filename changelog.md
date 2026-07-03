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

[2026-07-03 20:35] Сгенерирован стартовый synthetic baseline для эвалов: 25 тест-кейсов по контактам, записи, ценам, объяснению услуг, медицинским границам, подготовке, сертификатам, жалобам, неизвестным услугам и защите промпта. Кейсы добавлены в `project/llm/eval/dataset.json`.

[2026-07-03 20:35] Стартовые 25 eval-кейсов импортированы в таблицу `eval_cases`; раздел админки `/eval/` показывает набор для первого прогона.

[2026-07-03 21:01] Выполнен первый eval-прогон через админский eval-runner: run_id=1, 25/25 PASS, 0 FAIL. Regex-слой закрыл 17 кейсов, LLM-judge закрыл 8 спорных кейсов. Подготовлен отчет `project/llm/eval/run_1_report.md`.

[2026-07-03 21:15] Eval-кейсы переведены в формат полноценного эталонного ответа: поле `expected_answer` теперь содержит готовую модельную реплику для клиента, а не инструкцию/рекомендацию для judge.

[2026-07-03 21:18] По результату нового eval-прогона с полноценными эталонами найдено проседание в кейсе неизвестной услуги LPG: бот не дал контакт администратора. Системный промпт усилен правилом для неизвестных услуг: честно не выдумывать наличие/цену и сразу давать публичные контакты администратора.

[2026-07-03 21:20] Ужесточен eval-кейс неизвестной услуги LPG: теперь regex-слой требует конкретный публичный контакт администратора, а не только рекомендацию связаться.

[2026-07-03 21:35] В админку evals добавлена кнопка `Прогнать проблемные`: она запускает одним прогоном только кейсы, у которых последний результат был `fail` или `error`. Если проблемных кейсов нет, кнопка отключена.

[2026-07-03 21:53] Добавлена клиентская площадка ревью eval-кейсов /review/evals: отдельная таблица eval_case_reviews, карточки существующих кейсов, статусы согласования, комментарии, предложенные эталонные ответы и форма новых кейсов без запуска LLM.

[2026-07-03 22:03] Упрощена клиентская площадка ревью eval-кейсов: поле категории скрыто из формы и карточек, новые предложения сохраняются с технической категорией general, добавлено удаление черновых предложений заказчика.
[2026-07-03 23:42] На странице входа админ-панели заголовок заменён с разговорного «Вход в админку» на более профессиональное «Вход в панель управления».
[2026-07-03 23:34] Обновлён дизайн админ-панели: добавлен левый тёмно-синий sidebar, брендовая светлая dashboard-стилистика, компактные карточки на странице диалогов; backend бота, маршруты форм, БД и project/llm не менялись.
