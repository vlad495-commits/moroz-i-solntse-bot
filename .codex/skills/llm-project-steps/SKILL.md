---
name: llm-project-steps
description: "Codex local workflow for upgrading the current LLM chatbot project created with llm-project-gradually to the next step: admin panel, evals, buffer, guardrails, reliability, deploy, or production-ready. Use inside a generated project when the user says next step, upgrade, step N, add admin/evals/buffer/guardrails/deploy, prepare production, or similar. The current step is stored in project/.step. Do not use to create a new project from scratch."
---

# Скилл: llm-project-steps (апгрейд по ступеням)

Локальный скилл внутри проекта, разворачиваемый главным скиллом `llm-project-gradually`. Отвечает за **апгрейды** проекта по ступеням 2-8 (создание прототипа делает родительский глобальный скилл).

Каждая ступень добавляет новые компоненты — ученик идёт от простого к сложному, не пугаясь сразу всех файлов.

---

## Ступени проекта

| № | Ступень | Что добавляет |
|---|---|---|
| 1 | Прототип ← уже развёрнут | Бот + LLM + Postgres + Redis + Docker |
| 2 | Админка | FastAPI: диалоги, статистика, редактор промпта, тумблер бот вкл/выкл |
| 3 | Эвалы | Тест-кейсы, прогоны через LLM-судью, прогресс через SSE |
| 4 | Буфер | Склейка быстрых сообщений в один LLM-запрос |
| 5 | Безопасность LLM | Guardrails: защита от jailbreak и утечки промпта |
| 6 | Надёжность LLM | Резервный провайдер, retry, алерты в Telegram |
| 7 | Деплой | Сервер, SSH, бэкапы Postgres, hardening |
| 8 | Production-ready | Финальный аудит, смена паролей, чек-лист сдачи |

Текущая ступень проекта хранится в `project/.step` (один символ: `1`-`8`).

---

## Алгоритм апгрейда

### 1. Прочитай текущую ступень
```bash
cat <root>/project/.step
```
- Если файл отсутствует или содержит не цифру 1-8 → проект не в формате llm-project-gradually, спроси клиента: «Не могу определить ступень. Это проект из llm-project-gradually?»

### 2. Определи целевую ступень
- «следующий шаг» / «апгрейд» → текущая + 1
- «step N» / «ступень N» / явное упоминание (например, «нужен буфер» = ступень 4) → N
- Если N ≤ текущая → сообщи: «Ты уже на ступени X. Нечего апгрейдить. Можешь перейти на N+1.»
- Если N > текущая+1 → предупреди: «Это перепрыг через ступени. Лучше идти по очереди (X → X+1 → X+2). Если уверен — могу сделать сразу N, но настройки промежуточных ступеней не настроятся через локальные скиллы.»

### 3. Найди манифест целевой ступени
Шаблоны лежат в **локальной копии** (этот скилл) и/или в **глобальной** (если установлена):
```bash
ls <root>/.codex/skills/llm-project-steps/templates/step-N-*/UPGRADE.md
# Fallback на глобальный:
ls ~/.codex/skills/llm-project-gradually/templates/step-N-*/UPGRADE.md
```

Если не найден — сообщи: «Шаблон ступени N ещё не готов в скилле.»

### 4. Сделать git commit (страховка)
```bash
cd <root>
git add -A
git commit -m "auto: pre-upgrade backup (ступень $(cat project/.step) → N)" || true
```
Если git не инициализирован — пропусти молча (не критично).

### 5. Применить UPGRADE.md
Прочитай `templates/step-N-*/UPGRADE.md` и выполни инструкции последовательно:
- **Новые файлы** — копируй из `templates/step-N-*/<путь>` в `<root>/<путь>`.
- **Правки существующих** — открой указанный файл клиента и добавь/замени код по инструкции. Если ученик правил файл и вставка не находит контекст — спроси: «В твоём `<файл>.py` я не нашёл блок XYZ — куда вставить нужный кусок?»
- **`.env` доливка** — для каждой строки нового ключа: если ключа в `.env` нет → допиши в конец, если есть → оставь.
- **Новые локальные скиллы** — копируй из `templates/step-N-*/.codex/skills/<скилл>/` в `<root>/.codex/skills/<скилл>/`.
- **Миграции БД** — выполни команду из манифеста (`docker compose exec bot python -c ...`).

**Не трогай** при апгрейде: `prompts/system.md`, `data/`, `changelog.md`, `Дорожная карта.md`, `tmp/`, `logs/`.

### 6. Обнови `project/.step`
```bash
echo "N" > <root>/project/.step
```

### 7. Запиши в `changelog.md`
```
[YYYY-MM-DD HH:MM] Апгрейд на ступень N (<название>). Добавлено: <список новых файлов и компонентов>.
```

### 8. Перезапусти стек
```bash
cd <root>/project && docker compose up -d --build
```
Подожди 10-20 секунд. Проверь `docker compose ps` — все сервисы `Up`.

### 9. Сообщи клиенту что готово

Шаблон финального сообщения для каждой ступени — внутри её `UPGRADE.md` (раздел «Финальное сообщение»).

---

## Особые случаи

**Auto-canary после ступени 5.** Когда апгрейдишь до 5+ и в проекте уже был написан системный промпт (`prompts/system.md` непустой) — напомни клиенту:
> Теперь у нас есть guardrails. Чтобы добавить canary-токены защиты от утечки промпта — перезапусти `/llm-setup` и выбери раздел «Промпт». Он сгенерит и впишет их в `guardrails.py`.

---

## Структура файлов скилла (внутри проекта)

```
<root>/.codex/skills/llm-project-steps/
├── SKILL.md                           ← этот файл (логика апгрейдов)
└── templates/
    ├── step-2-admin/UPGRADE.md
    ├── step-3-evals/UPGRADE.md
    ├── step-4-buffer/UPGRADE.md
    ├── step-5-guardrails/UPGRADE.md
    ├── step-6-reliability/UPGRADE.md
    ├── step-7-deploy/UPGRADE.md
    └── step-8-production-ready/UPGRADE.md
```

**Не лежит здесь:** `step-1-prototype/` (он уже развёрнут в корне) и `_archive_full_project/` (это для разработки скилла, не для проекта ученика).
