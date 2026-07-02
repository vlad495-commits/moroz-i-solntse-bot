# Апгрейд: ступень 7 → 8 (Production-ready)

Финальная ступень. Никаких новых файлов кода — только локальный скилл `/production-ready` для финального аудита, смены дефолтных паролей и чек-листа готовности к сдаче клиенту.

---

## 1. Новые файлы

```bash
TEMPLATES=~/.codex/skills/llm-project-gradually/templates/step-8-production-ready
[ -d "$TEMPLATES" ] || TEMPLATES=<root>/.codex/skills/llm-project-steps/templates/step-8-production-ready

# Локальный скилл /production-ready
mkdir -p <root>/.codex/skills/production-ready
cp "$TEMPLATES/.codex/skills/production-ready/SKILL.md" <root>/.codex/skills/production-ready/SKILL.md
```

---

## 2. Правки в существующих файлах

Нет правок. Все необходимые компоненты уже есть.

---

## 3. .env (доливка)

Нет новых переменных. Все существующие — будут проверены через `/production-ready`.

---

## 4. Миграция БД

Не требуется.

---

## 5. Локальные скиллы

`/production-ready` — скопирован выше (см. п.1).

Триггеры: «готовим к проду», «production ready», «финалим проект», «финальный аудит».

**Важно:** этот скилл — урезанная версия. Часть функций (резервная LLM, retry, ADMIN_TG_CHAT_ID) уехала в `/reliability` на ступени 6. Сервер и SSH — в скиллы `/server-hardening`, `/ssh-setup`, `/deploy`.

---

## 6. Финальное сообщение клиенту

```
Готово. Ступень 8 (Production-ready). Это последняя ступень — проект полностью развёрнут со всеми компонентами.

Локальный скилл /production-ready — финальный аудит:
- Меняет дефолтные пароли админки (ADMIN_USERNAME, ADMIN_PASSWORD)
- Генерирует ADMIN_SESSION_SECRET
- Прогоняет финальный чек-лист готовности

Прямо сейчас скажи "финалим проект" / "production ready" — пройдёмся по чек-листу и сменим дефолты.

После этого:
1. Если ещё не задеплоен — /deploy
2. Передай клиенту: URL админки, логин/пароль, имя бота, контакты
3. Сделай тестовый прогон эвалов (опц.)

🎉 Поздравляю! Все 8 ступеней пройдены.
```
