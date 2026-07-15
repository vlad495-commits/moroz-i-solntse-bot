---
name: deploy
description: Деплоит проект на сервер через Docker + Git. Первый деплой (clone + build) и обновление (git pull + rebuild). Триггеры (примеры, не исчерпывающий список) "деплой", "развернём на сервере", "задеплой", "git pull на сервере", "обновить на сервере", "выкатим на прод". НЕ срабатывай если речь о деплое других проектов или о CI/CD пайплайнах.
---

# Скилл: Деплой проекта на сервер

Разворачивает проект на VPS клиента через Docker + git pull. Поддерживает два режима: **первый деплой** (с нуля) и **обновление** (когда уже задеплоен).

## Предусловия

- `ssh/config` настроен (через скилл `/ssh-setup`)
- Проект запушен в git-репозиторий (приватный GitHub / Gitea / GitLab — не важно)
- На сервере установлен Docker + Docker Compose Plugin

Если предусловия не выполнены — попроси клиента:
- Сначала вызови `/ssh-setup` (если нет SSH-доступа)
- Запушь репо: `git remote -v`, `git push -u origin main`
- На сервере: `curl -fsSL https://get.docker.com | sh && systemctl enable docker && systemctl start docker` (если Docker не установлен)

## Структура диалога

### 0. Quality Gate — проверки кода перед деплоем

ПЕРЕД любым деплоем (первый или обновление) прогони локально 3 проверки.
Любая упала → деплой НЕ продолжаем: показываем клиенту что сломалось,
ждём починки, повторяем гейт.

**0.1 — Линтер (ruff).** Ловит синтаксис и обращение к необъявленным именам.
Запусти ruff (в порядке предпочтения):

```bash
# если ruff уже в PATH:
ruff check --select E9,F --ignore F401,F841 <root>/project/
# иначе через uv — запуск без установки в систему:
uvx ruff check --select E9,F --ignore F401,F841 <root>/project/
```

Если нет ни `ruff`, ни `uv` — поставь: `uv tool install ruff` или (macOS) `brew install ruff`.
НЕ ставь через `pip3 install` напрямую — на системном/homebrew Python ломается (PEP 668).
Набор `E9,F` блокирует реальные поломки; `--ignore F401,F841` не блокирует деплой
из-за безобидных неиспользуемых импортов/переменных.
Непустой вывод → показать клиенту, СТОП.

**0.2 — Компиляция (py_compile):**

```bash
python3 -m compileall -q <root>/project/
```

Ненулевой код → показать ошибку, СТОП.

**0.3 — Валидация docker-compose:**

```bash
docker compose -f <root>/project/docker-compose.yml config -q
```

Ненулевой код → показать ошибку, СТОП.

Все 3 зелёные → переходи к шагу 1.

### 1. Определить режим (первый деплой или обновление)

```bash
DEPLOY_PATH="${DEPLOY_PATH:-/srv/{{PROJECT_SLUG}}}"
ssh -F <root>/ssh/config {{PROJECT_SLUG}} "test -d ${DEPLOY_PATH}/.git && echo EXISTS || echo MISSING"
```

- `EXISTS` → режим **обновление**
- `MISSING` → режим **первый деплой**

### 2А. Первый деплой

#### 2А.1 — Получить URL git-репозитория

```bash
git -C <root> remote get-url origin
```

Если нет remote — попроси клиента запушить проект сначала.

#### 2А.2 — Спросить путь на сервере

> Куда деплоить на сервере? (по умолчанию `/srv/{{PROJECT_SLUG}}`)

#### 2А.3 — Клонировать на сервер

```bash
ssh -F <root>/ssh/config {{PROJECT_SLUG}} "
  sudo mkdir -p ${DEPLOY_PATH}
  sudo chown \$(whoami):\$(whoami) ${DEPLOY_PATH}
  cd ${DEPLOY_PATH}
  git clone <repo-url> .
"
```

Если репо приватное — на сервере должен быть deploy-key или PAT. Спроси клиента как авторизовываться.

#### 2А.4 — Скопировать `.env` на сервер

`.env` НЕ коммитится — его надо передать вручную:

```bash
scp -F <root>/ssh/config <root>/.env {{PROJECT_SLUG}}:${DEPLOY_PATH}/.env
ssh -F <root>/ssh/config {{PROJECT_SLUG}} "chmod 600 ${DEPLOY_PATH}/.env"
```

#### 2А.5 — Запустить стек

```bash
ssh -F <root>/ssh/config {{PROJECT_SLUG}} "
  cd ${DEPLOY_PATH}/project
  docker compose up -d --build
"
```

Сборка ~3-5 мин. Подожди.

#### 2А.6 — Проверить

```bash
ssh -F <root>/ssh/config {{PROJECT_SLUG}} "cd ${DEPLOY_PATH}/project && docker compose ps"
```

Все контейнеры должны быть `Up`. Если нет — `docker compose logs <service> --tail 50`.

### 2Б. Обновление

#### 2Б.1 — Pull изменений

Сначала запомни текущий коммит — точку отката, если обновление сломает прод:

```bash
ssh -F <root>/ssh/config {{PROJECT_SLUG}} "cd ${DEPLOY_PATH} && git rev-parse --short HEAD"
```

Запиши вывод как `<PREV_COMMIT>` (понадобится в 2Б.4).

```bash
ssh -F <root>/ssh/config {{PROJECT_SLUG}} "
  cd ${DEPLOY_PATH}
  git fetch origin
  git status
"
```

Если есть локальные изменения на сервере — предупреди клиента и спроси (stash / discard).

```bash
ssh -F <root>/ssh/config {{PROJECT_SLUG}} "
  cd ${DEPLOY_PATH}
  git pull origin main
"
```

#### 2Б.2 — Пересобрать и поднять

При первом обновлении после переименования Compose-сервиса `llm` → `bot` один раз останови прежний стек с удалением orphan-контейнера. На следующих обновлениях эту команду пропускай:

```bash
ssh -F <root>/ssh/config {{PROJECT_SLUG}} "cd ${DEPLOY_PATH}/project && docker compose down --remove-orphans"
```

```bash
ssh -F <root>/ssh/config {{PROJECT_SLUG}} "
  cd ${DEPLOY_PATH}/project
  docker compose up -d --build
"
```

#### 2Б.3 — Проверить

```bash
ssh -F <root>/ssh/config {{PROJECT_SLUG}} "
  cd ${DEPLOY_PATH}/project
  docker compose ps
  docker compose logs bot --tail 30
"
```

#### 2Б.4 — Откат (если обновление сломало прод)

Если после 2Б.3 контейнеры не поднялись (`docker compose ps` показывает не `Up`)
или в логах ошибки — не отлаживай на проде. Откатись на `<PREV_COMMIT>` из 2Б.1:

```bash
ssh -F <root>/ssh/config {{PROJECT_SLUG}} "
  cd ${DEPLOY_PATH}
  git checkout <PREV_COMMIT>
  cd project
  docker compose up -d --build
  docker compose ps
"
```

Прод вернулся на рабочую версию. Дальше чини поломку локально и деплой заново,
когда починишь — оставлять прод сломанным ради отладки нельзя.

---

### 3. Записать в changelog.md

```
[YYYY-MM-DD HH:MM] Деплой на сервер <host>: <первый/обновление>. Путь: <DEPLOY_PATH>. Коммит: <git rev-parse --short HEAD>.
```

### 4. Финальное сообщение

**Первый деплой:**
```
Готово. Развернул проект на сервере <host>:<DEPLOY_PATH>.
- Стек: docker compose ps на сервере покажет bot, admin, redis, postgres, postgres-backup
- Админка пока не доступна снаружи (висит на 127.0.0.1:8080). Чтобы посмотреть локально:
  ssh -F ssh/config -L 8080:127.0.0.1:8080 {{PROJECT_SLUG}}
  → потом открой http://localhost:8080 в браузере
- Если нужен публичный домен — настрой Caddy/Nginx с TLS (см. скилл /server-hardening, раздел про Caddy)

Бот должен начать отвечать в Telegram. Если нет — ssh -F ssh/config {{PROJECT_SLUG}} 'cd <DEPLOY_PATH>/project && docker compose logs bot --tail 50'.
```

**Обновление:**
```
Готово. Обновил проект на сервере <host>:<DEPLOY_PATH>.
- Pull: <X файлов изменено>
- Контейнеры пересобраны и подняты
- Простой: ~10-30 секунд (только во время рестарта)

Если что-то не работает — ssh -F ssh/config {{PROJECT_SLUG}} 'cd <DEPLOY_PATH>/project && docker compose logs <service>'.
```

## Важно

- Перед первым деплоем — **обязательно** пройди `/server-hardening`. Не деплой на голый сервер с открытым root и парольным SSH.
- На сервере секреты в `.env` должны быть с правами 600 (только владелец).
- НЕ коммить `.env` — он передаётся через SCP отдельно.
- Если на сервере поменяли `.env` руками — `docker compose restart bot admin` чтобы применить.
- При первом деплое спроси клиента, нужны ли публичный домен и HTTPS — если да, после деплоя направь к скиллу `/server-hardening` (раздел Caddy).
