# SSH-ключи и доступы к серверу

Эта папка — **в `.gitignore`** (кроме этого README). Сюда кладутся приватные ключи и инструкции для деплоя клиентского проекта.

## Что класть сюда

```
ssh/
├── README.md              ← этот файл (в git)
├── id_ed25519_{{PROJECT_SLUG}}      ← приватный ключ (НЕ в git)
├── id_ed25519_{{PROJECT_SLUG}}.pub  ← публичный ключ (можно в git)
└── server.md              ← опционально: IP, юзер, путь, особенности
```

## Подключение

```bash
# Использовать ключ для конкретного сервера:
ssh -i ssh/id_ed25519_{{PROJECT_SLUG}} root@<IP_СЕРВЕРА>

# Или прописать в ~/.ssh/config:
# Host {{PROJECT_SLUG}}
#   HostName <IP>
#   User root
#   IdentityFile ~/path-to-project/ssh/id_ed25519_{{PROJECT_SLUG}}
```

---

## Деплой на сервер — основное

Полная инструкция по деплою — в скилле `/deploy`. Здесь — что специфично для этого проекта.

### Доп. контейнеры на сервере (которых нет локально)

При деплое в прод нужны контейнеры сверх локальных 4-х:

1. **`caddy`** — reverse-proxy с HTTPS (auto-cert от Let's Encrypt).
   - Терминирует HTTPS, прокидывает админку на 443 порт.
   - Образ: `caddy:2-alpine`.
   - Конфиг: `caddy/Caddyfile` — задать домен админки.
   - Без Caddy — админка на сервере доступна только по `localhost:8080` (через SSH-туннель).

2. **`postgres-backup`** (опционально, рекомендуется) — ежедневный pg_dump с ротацией.
   - Образ: `prodrigestivill/postgres-backup-local`.
   - Делает бэкап раз в сутки, хранит 7 дней.

### Чек-лист первого деплоя

- [ ] SSH-ключ положен на сервер: `ssh-copy-id -i ssh/id_ed25519_{{PROJECT_SLUG}}.pub root@<IP>`
- [ ] Сервер обновлён: `apt update && apt upgrade -y`
- [ ] Docker и docker-compose установлены: `curl -fsSL https://get.docker.com | sh`
- [ ] Firewall настроен (см. блок «UFW» ниже)
- [ ] fail2ban установлен (см. блок «fail2ban» ниже)
- [ ] SSH по паролю отключён (см. блок «SSH hardening» ниже)
- [ ] Репо клонирован на сервер: `git clone git@github.com:USER/{{PROJECT_SLUG}}.git`
- [ ] `.env` загружен на сервер с production-значениями (НЕ через git, через scp): `scp .env root@<IP>:~/{{PROJECT_SLUG}}/.env`
- [ ] Права на `.env`: `chmod 600 .env && chown root:root .env`
- [ ] Caddy-конфиг настроен с доменом
- [ ] `docker compose up -d --build`
- [ ] Проверить логи: `docker compose logs -f`
- [ ] Cron для бэкапов БД настроен (см. блок «Бэкапы PostgreSQL» ниже)
- [ ] Создан тестовый бэкап БД и проверено восстановление.

---

### UFW (firewall)

Открываем только нужные порты, всё остальное — закрыто:

```bash
apt install -y ufw
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp     # SSH
ufw allow 80/tcp     # Caddy → ACME challenge + redirect → 443
ufw allow 443/tcp    # Caddy → HTTPS
ufw enable
ufw status verbose
```

**ВАЖНО: НЕ открывать 5432 (Postgres), 6379 (Redis), 8080 (admin)** — они только внутри docker-сети.

### fail2ban (защита SSH от брутфорса)

```bash
apt install -y fail2ban
cat > /etc/fail2ban/jail.d/ssh.conf <<EOF
[sshd]
enabled = true
port = 22
maxretry = 3
findtime = 10m
bantime = 24h
EOF
systemctl enable --now fail2ban
fail2ban-client status sshd
```

### SSH hardening

В `/etc/ssh/sshd_config`:
```
PasswordAuthentication no
PermitRootLogin prohibit-password
PubkeyAuthentication yes
```
Затем: `systemctl restart sshd`. Проверь что заходишь по ключу ДО закрытия пароля (иначе можешь себя залочить).

### Бэкапы PostgreSQL

Самый простой вариант — `pg_dump` через cron хост-системы (бэкап-контейнер не обязателен):

```bash
mkdir -p /var/backups/{{PROJECT_SLUG}}
chmod 700 /var/backups/{{PROJECT_SLUG}}

# В crontab (crontab -e):
# Каждый день в 03:00 — pg_dump, хранение 30 дней
0 3 * * * cd /root/{{PROJECT_SLUG}}/project && \
  docker compose exec -T postgres pg_dump -U "$$POSTGRES_USER" "$$POSTGRES_DB" \
  | gzip > /var/backups/{{PROJECT_SLUG}}/db_$(date +\%Y\%m\%d).sql.gz && \
  find /var/backups/{{PROJECT_SLUG}} -name "db_*.sql.gz" -mtime +30 -delete
```

Восстановление:
```bash
gunzip -c /var/backups/{{PROJECT_SLUG}}/db_20260501.sql.gz | \
  docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"
```

Альтернатива — контейнер `prodrigestivill/postgres-backup-local` (см. выше).

### Обновление существующего деплоя

```bash
ssh root@<IP>
cd {{PROJECT_SLUG}}
git pull
docker compose up -d --build
docker compose logs -f
```

### Что в `.env` на сервере отличается от локального

- `ADMIN_USERNAME` / `ADMIN_PASSWORD` — поменять на сильные значения
- `ADMIN_SESSION_SECRET` — сгенерировать через `openssl rand -hex 32`
- `POSTGRES_PASSWORD` — сильный пароль
- `LLM_API_KEY` (и `RESERVE_API_KEY` если резерв используется) — production-ключи (не dev)
- `TELEGRAM_BOT_TOKEN` — production-бот (не тестовый)
- `ADMIN_TG_CHAT_ID` — реальный чат для алертов

---

## Безопасность

- Никогда не коммить приватные ключи и `.env` в git.
- Бэкапы БД хранить **в зашифрованном виде** (или на изолированном S3 bucket).
- Логи docker (`docker compose logs`) могут содержать API-ключи — не выкладывать в публичные места.
