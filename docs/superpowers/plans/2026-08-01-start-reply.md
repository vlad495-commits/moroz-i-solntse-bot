# Start Reply Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Установить согласованное приветствие `/start` с пустыми строками между приветствием, списком возможностей и финальным приглашением.

**Architecture:** Существующие обработчики уже отправляют статическую переменную `START_REPLY`. Изменение ограничивается корневым `.env`; код маршрутизации и доставки не затрагивается.

**Tech Stack:** Docker Compose, Python 3.12, aiogram/FastAPI Telegram ingress, dotenv.

## Global Constraints

- Проект запускается и проверяется только через Docker.
- Меняется только `START_REPLY` в корневом `.env`.
- Сообщение содержит ровно три смысловых блока и одну пустую строку между соседними блоками.
- Значение `.env` не добавляется в Git; в Git фиксируются только спецификация, план, дорожная карта и changelog.

---

### Task 1: Настроить и проверить приветствие `/start`

**Files:**
- Modify: `.env:63`
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`

**Interfaces:**
- Consumes: `START_REPLY: str`, который Docker Compose передаёт сервису `bot`.
- Produces: статический текст, который существующие `handle_start` и webhook ingress отправляют по `/start`.

- [x] **Step 1: Зафиксировать RED-проверку текущей конфигурации**

Выполнить Docker Compose-проверку точного ожидаемого значения до изменения `.env`:

```powershell
$env:RABBITMQ_USER = "test_user"
$env:RABBITMQ_PASSWORD = "test_password"
$env:RABBITMQ_URL = "amqp://test_user:test_password@rabbitmq:5672/"
$env:TELEGRAM_WEBHOOK_SECRET = "test_webhook_secret_32_chars_long"
$expected = "Здравствуйте! Я онлайн-ассистент центра «Мороз и Солнце» ❄️☀️`n`nПомогу вам:`n• узнать об услугах, ценах и программах`n• подобрать подходящую процедуру`n• записаться на удобное время`n• перенести или отменить запись`n• узнать о сертификатах и подготовке к визиту`n`nНапишите, что вас интересует — я подскажу, с чего начать."
$actual = ((docker compose --env-file ../.env config --format json | ConvertFrom-Json).services.bot.environment.START_REPLY)
if ($actual -eq $expected) { exit 0 } else { Write-Error "START_REPLY differs from approved copy" }
```

Expected: exit code `1`, потому что в `.env` пока находится прежнее короткое приветствие.

- [x] **Step 2: Записать минимальное изменение**

Заменить только строку `START_REPLY` в `.env` на значение с `\n\n` между тремя блоками:

```dotenv
START_REPLY="Здравствуйте! Я онлайн-ассистент центра «Мороз и Солнце» ❄️☀️\n\nПомогу вам:\n• узнать об услугах, ценах и программах\n• подобрать подходящую процедуру\n• записаться на удобное время\n• перенести или отменить запись\n• узнать о сертификатах и подготовке к визиту\n\nНапишите, что вас интересует — я подскажу, с чего начать."
```

- [x] **Step 3: Проверить GREEN через Docker Compose**

Повторить команду Step 1.

Expected: exit code `0`; значение полностью совпадает, включая две пустые строки между блоками.

- [x] **Step 4: Проверить значение в контейнере**

```powershell
docker compose --env-file ../.env run --rm --no-deps bot python -c "import os; value=os.environ['START_REPLY']; assert value.count(chr(10) * 2) == 2; print('START_REPLY runtime check passed')"
```

Expected: `START_REPLY runtime check passed`, exit code `0`.

- [x] **Step 5: Обновить проектные документы и закоммитить логический шаг**

Отметить задачу выполненной в `Дорожная карта.md`, записать результат проверки в `changelog.md`, затем выполнить:

```powershell
git add docs/superpowers/specs/2026-08-01-start-reply-design.md docs/superpowers/plans/2026-08-01-start-reply.md 'Дорожная карта.md' changelog.md
git commit -m "config: обновлено приветствие команды start"
```

Expected: создан один локальный коммит; `.env` остаётся игнорируемым и не попадает в Git.
