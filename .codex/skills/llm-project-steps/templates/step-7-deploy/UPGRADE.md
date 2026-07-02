# Апгрейд: ступень 6 → 7 (Деплой)

Подготавливает проект к деплою на VPS: SSH-ключи, бэкапы Postgres, hardening сервера, локальные скиллы для подключения и деплоя.

---

## 1. Новые файлы

```bash
TEMPLATES=~/.codex/skills/llm-project-gradually/templates/step-7-deploy
[ -d "$TEMPLATES" ] || TEMPLATES=<root>/.codex/skills/llm-project-steps/templates/step-7-deploy

# SSH README (гайд)
mkdir -p <root>/ssh
cp "$TEMPLATES/ssh/README.md" <root>/ssh/README.md

# Папка для дампов pg_dump
mkdir -p <root>/project/backups
touch <root>/project/backups/.gitkeep

# 3 локальных скилла
mkdir -p <root>/.codex/skills/ssh-setup <root>/.codex/skills/server-hardening <root>/.codex/skills/deploy
cp "$TEMPLATES/.codex/skills/ssh-setup/SKILL.md"        <root>/.codex/skills/ssh-setup/SKILL.md
cp "$TEMPLATES/.codex/skills/server-hardening/SKILL.md" <root>/.codex/skills/server-hardening/SKILL.md
cp "$TEMPLATES/.codex/skills/deploy/SKILL.md"           <root>/.codex/skills/deploy/SKILL.md
```

---

## 2. Правки в существующих файлах

### `<root>/project/docker-compose.yml`

Перед строкой `volumes:` (последний верхнеуровневый блок) добавить сервис `postgres-backup`:

```yaml
  postgres-backup:
    image: prodrigestivill/postgres-backup-local:16
    restart: unless-stopped
    env_file: ../.env
    environment:
      POSTGRES_HOST: postgres
      POSTGRES_EXTRA_OPTS: '-Z6 --schema=public --blobs'
      SCHEDULE: '@daily'
      BACKUP_KEEP_DAYS: 7
      BACKUP_KEEP_WEEKS: 4
      BACKUP_KEEP_MONTHS: 6
      HEALTHCHECK_PORT: 8080
      TZ: Europe/Moscow
    volumes:
      - ./backups:/backups
    depends_on:
      postgres:
        condition: service_healthy
    logging:
      driver: "json-file"
      options:
        max-size: "5m"
        max-file: "3"
    stop_grace_period: 30s
```

### `<root>/.gitignore`

В соответствующих секциях раскомментировать / добавить:

```
# SSH-ключи (в папке ssh/ — кроме README)
ssh/*
!ssh/README.md

# Бэкапы Postgres (содержимое — дампы)
project/backups/*
!project/backups/.gitkeep
```

---

## 3. .env (доливка — закомментированные опции для off-site бэкапов)

```
# --- Бэкапы Postgres (off-site, опционально) ---
# Раскомментируй и заполни если хочешь дублировать дампы в S3
# (Yandex Cloud / Backblaze B2 / AWS S3). Также добавь эти переменные
# в environment: блок сервиса postgres-backup в docker-compose.yml.
# BACKUP_S3_BUCKET=
# BACKUP_S3_REGION=
# BACKUP_S3_ACCESS_KEY_ID=
# BACKUP_S3_SECRET_ACCESS_KEY=
# BACKUP_S3_ENDPOINT=
```

---

## 4. Миграция БД

Не требуется.

---

## 5. Локальные скиллы

Скопированы в п.1:
- **`/ssh-setup`** — генерация SSH-ключей и подключение к серверу клиента
- **`/server-hardening`** — UFW + fail2ban + SSH-only + non-root + (опц.) Caddy/TLS
- **`/deploy`** — git pull + docker compose up -d на сервере

Триггеры:
- `/ssh-setup`: «настроим SSH», «подключимся к серверу», «сгенерим SSH ключи»
- `/server-hardening`: «настроим безопасность сервера», «закроем порты», «поставим fail2ban»
- `/deploy`: «деплой», «развернём на сервере», «git pull на сервере»

---

## 6. Финальное сообщение клиенту

```
Готово. Ступень 7 (Деплой). Добавил:
- ssh/README.md — гайд по SSH-ключам
- project/backups/ — папка для дампов pg_dump
- Сервис postgres-backup в docker-compose: ежедневные дампы в 03:00 МСК, ротация 7d/4w/6m
- 3 локальных скилла:
  - /ssh-setup       — генерация ключей и подключение к серверу клиента
  - /server-hardening — UFW + fail2ban + SSH-only + non-root + (опц.) Caddy/TLS
  - /deploy          — git pull + docker compose up -d на сервере

Порядок действий для деплоя:
1. Скажи "настроим SSH" — сгенерим ключи и подключимся
2. Скажи "защитим сервер" — UFW, fail2ban, отключим парольный SSH
3. Скажи "деплой" — выкатим проект

Перезапусти локально (с новым postgres-backup): cd project && docker compose up -d --build

Дальше — ступень 8 (Production-ready): финальный аудит и сдача клиенту.
```
