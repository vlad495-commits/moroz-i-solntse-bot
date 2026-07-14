---
name: server-hardening
description: "Ужесточение безопасности сервера — non-root sudo-юзер, UFW (firewall), SSH hardening (key-only, no root password), fail2ban, опционально смена SSH-порта, pg_dump cron, Caddy/TLS. Каждый шаг с обязательным подтверждением и проверкой через отдельную SSH-сессию, чтобы не залочить себя. ОБЯЗАТЕЛЬНО используй когда пользователь говорит про безопасность сервера, hardening, firewall, fail2ban, закрытие портов. Триггеры (примеры, не исчерпывающий список) настроим безопасность сервера, ужесточи сервер, server hardening, закроем порты, настроим UFW, поставим fail2ban, hardening сервера, защитим сервер. НЕ срабатывай если речь про деплой кода (это скилл /deploy), про безопасность приложения (admin-пароли — это /production-ready), или про создание SSH-ключа локально."
---

# Скилл: Server Hardening

Ужесточает свежий сервер до безопасной конфигурации. Каждый шаг — последовательно, с подтверждением, с проверкой через отдельную SSH-сессию. Цель — НЕ залочить пользователя из сервера.

---

## ⚠️ ЖЕЛЕЗНЫЕ ПРАВИЛА (не нарушать)

Эти правила — основа скилла. Нарушение любого из них может привести к потере доступа к серверу. Без физ. доступа или recovery-консоли провайдера восстановление невозможно.

### 1. SECOND-SESSION RULE
Прежде чем что-либо менять — клиент ОБЯЗАН открыть вторую SSH-сессию в отдельном терминале и держать её живой до конца скилла. Если в первой сессии что-то сломается — через вторую откатим. Без этого — НЕ ПРОДОЛЖАТЬ.

### 2. ALLOW-BEFORE-ENABLE
Любое правило firewall (`ufw allow`) добавляется ДО включения firewall (`ufw enable`). Никогда не enable пустой UFW. Никогда не меняй default policy на `deny` без `allow` для SSH.

### 3. TEST-BEFORE-DISABLE
Перед отключением чего-либо (пароль для SSH, root-логин, старый SSH-порт) — проверить через НОВУЮ SSH-сессию что новый способ доступа работает. Только после ✓ — отключать старый.

### 4. ONE-CHANGE-PER-STEP
Один шаг = одно изменение + явное подтверждение клиента. Никаких пакетных применений «давай сразу всё накатим».

### 5. ROLLBACK READY
Перед каждым изменением — показать клиенту команду отката. Например: «Если после restart sshd тебя выкинет и новая сессия не подключится — во второй сессии выполни `<команда отката>`».

---

## Что меняется

- На сервере: `/etc/ufw/`, `/etc/ssh/sshd_config`, `/etc/fail2ban/jail.d/ssh.conf`, `/etc/crontab`, опц. `/etc/caddy/`, новый sudo-юзер.
- Локально (опц.): `caddy/Caddyfile`, `docker-compose.yml` (добавление сервиса caddy).

---

## Шаг 0 — Пресервация доступа (ОБЯЗАТЕЛЬНО)

Скажи клиенту дословно:

> ⚠️ **СТОП. Прежде чем мы начнём — открой ВТОРУЮ SSH-сессию к серверу прямо сейчас, в отдельном терминале/окне.**
>
> Команда та же что для основной сессии (`ssh -i ssh/id_ed25519_<project> root@<IP>` или твоя).
>
> **Не закрывай эту вторую сессию до самого конца скилла.** Если в первой сессии что-то пойдёт не так после моих изменений — через вторую сделаем rollback.
>
> Подтверди: «вторая сессия открыта и работает».

Если клиент НЕ подтвердил — **не продолжай**. Переспроси. Это критично.

Также спроси:
> Какой у сервера IP/hostname и под каким юзером ты сейчас залогинен (`whoami`)? Это нужно для конфига fail2ban (твой IP в ignoreip).

Запомни: `<SERVER_IP>`, `<CLIENT_IP>` (узнать через `who am i` или ipify), `<CURRENT_USER>` (обычно `root`).

---

## Шаг 1 — Non-root sudo-юзер (рекомендовано, опц.)

Спроси:
> Сейчас ты сидишь под `<CURRENT_USER>`. По best-practices безопаснее создать отдельного sudo-юзера и работать под ним, а root оставить только для экстренных случаев.
>
> Это **рекомендовано, но не обязательно**. Если SSH-ключ + закрыт парольный вход + fail2ban — root тоже защищён.
>
> Создаём sudo-юзера? (да / пропустить)

### Если «да»:

1. Спроси: какое имя юзера? (предложи `deploy` или slug проекта)
2. Создай юзера:
   ```bash
   useradd -m -s /bin/bash <USER>
   usermod -aG sudo <USER>
   ```
3. Скопируй SSH-ключ от root к юзеру:
   ```bash
   mkdir -p /home/<USER>/.ssh
   cp /root/.ssh/authorized_keys /home/<USER>/.ssh/authorized_keys
   chown -R <USER>:<USER> /home/<USER>/.ssh
   chmod 700 /home/<USER>/.ssh
   chmod 600 /home/<USER>/.ssh/authorized_keys
   ```
4. Установи пароль для sudo (или настрой NOPASSWD — спроси клиента):
   - Вариант A — пароль: `passwd <USER>` (клиент введёт пароль)
   - Вариант B — NOPASSWD: `echo "<USER> ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/<USER>` (удобнее, но менее безопасно)

   Спроси клиента какой вариант. По умолчанию — **с паролем**.

5. **🔴 КРИТИЧНАЯ ПРОВЕРКА.** Скажи клиенту:
   > **Открой ТРЕТЬЮ SSH-сессию в новом терминале** и зайди под новым юзером:
   > ```
   > ssh -i <твой ключ> <USER>@<SERVER_IP>
   > ```
   > После входа выполни: `sudo -i` — должен спросить пароль (или войти сразу если NOPASSWD).
   >
   > Подтверди: «зашёл под `<USER>` и `sudo -i` работает».

6. Только если ✓ — продолжай. Иначе rollback:
   ```bash
   userdel -r <USER>
   rm -f /etc/sudoers.d/<USER>
   ```

### Если «пропустить»:
Идём к шагу 2 под текущим юзером.

---

## Шаг 2 — UFW (firewall)

> Сейчас все порты сервера открыты наружу. UFW закроет всё кроме явно разрешённого.
>
> Стандартный набор для нашего проекта:
> - **22/tcp** — SSH (без него ты потеряешь доступ! ВКЛЮЧАЕМ ПЕРВЫМ)
> - **80/tcp** — HTTP (Let's Encrypt ACME challenge + redirect → 443)
> - **443/tcp** — HTTPS (если будет публичный домен)
>
> Postgres (5432), Redis (6379), админка (8080) — НЕ открываем наружу, они только внутри docker-сети.
>
> Что-нибудь дополнительно открыть? (например кастомный API-порт)

Запиши доп. порты в `<EXTRA_PORTS>`.

### 2.1 Установка UFW

```bash
apt update && apt install -y ufw
```

### 2.2 Правила (СНАЧАЛА allow, ПОТОМ enable)

⚠️ Выполняй ИМЕННО в этом порядке:

```bash
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
# + каждый <EXTRA_PORT>:
# ufw allow <PORT>/tcp
```

### 2.3 Проверка ДО enable

```bash
ufw show added
```

Скажи клиенту:
> Покажи мне вывод. Должно быть минимум `ufw allow 22/tcp`. Если этого правила НЕТ — стоп, разбираемся, **не включаем UFW** иначе потеряешь доступ.

### 2.4 Включение

> ⚠️ Сейчас включу UFW. Ты сидишь по 22 порту, правило для 22 добавлено — должно быть ок. Но на всякий случай: твоя ВТОРАЯ сессия открыта? Если что — там выполнишь `ufw disable`.
>
> Подтверди: «включаем UFW».

```bash
ufw --force enable
```

(`--force` чтобы не требовал подтверждения y/n)

### 2.5 🔴 КРИТИЧНАЯ ПРОВЕРКА

Скажи клиенту:
> **Открой НОВУЮ SSH-сессию** (третью, если хочешь) и проверь что заходит. Существующие сессии останутся живыми, потому что соединение уже установлено — нам важно проверить именно НОВОЕ подключение.
>
> Если новая сессия НЕ заходит — во второй сессии срочно: `ufw disable`. Потом разбираемся.
>
> Подтверди: «новая сессия зашла, UFW работает».

### Rollback (если что-то не так)
В живой сессии: `ufw disable`

---

## Шаг 3 — Смена SSH-порта (опционально)

Спроси:
> Менять стандартный порт SSH (22) на нестандартный? Это **косметическая мера** — снижает шум от ботов-сканеров в логах, но НЕ защищает от таргетированной атаки (любой nmap найдёт нестандартный порт за минуту).
>
> Минусы: придётся помнить порт, прописывать в `~/.ssh/config`, в CI/CD, в скриптах деплоя. Забудешь — не зайдёшь.
>
> По умолчанию: **пропускаем**. Если хочешь — какой порт? (стандартное предложение: 22022)

### Если «пропустить» — идём к шагу 4.

### Если «меняем»:

Алгоритм parallel-ports — слушаем оба порта параллельно, потом выключаем старый.

1. **СНАЧАЛА UFW:**
   ```bash
   ufw allow <NEW_PORT>/tcp
   ```

2. **В sshd_config — оба порта параллельно:**
   ```
   Port 22
   Port <NEW_PORT>
   ```
   (НЕ убираем 22, добавляем строку)

3. Сохрани backup:
   ```bash
   cp /etc/ssh/sshd_config /etc/ssh/sshd_config.bak.$(date +%s)
   ```

4. Restart sshd:
   ```bash
   systemctl restart sshd
   ```

5. **🔴 КРИТИЧНАЯ ПРОВЕРКА.** Скажи клиенту:
   > Открой новую SSH-сессию на новом порту:
   > ```
   > ssh -p <NEW_PORT> <USER>@<SERVER_IP>
   > ```
   > Зашло? Подтверди.
   >
   > Если НЕТ — во второй сессии: `cp /etc/ssh/sshd_config.bak.* /etc/ssh/sshd_config && systemctl restart sshd && ufw delete allow <NEW_PORT>/tcp`. Возвращаемся к шагу 4.

6. Только если ✓ — убираем старый порт:
   - В `sshd_config` убрать строку `Port 22` (оставить только `Port <NEW_PORT>`)
   - `systemctl restart sshd`
   - `ufw delete allow 22/tcp`

7. **Проверка:** новая сессия на старом 22 НЕ заходит, на новом — заходит. Подтверждение.

---

## Шаг 4 — SSH hardening (key-only)

> Сейчас сделаем три критичных вещи:
> 1. Запретим вход по паролю (только ключи).
> 2. Запретим root-логин по паролю.
> 3. Включим аутентификацию по ключу явно.
>
> Опасный момент: если твой SSH-ключ почему-то перестанет работать — ты не сможешь зайти по паролю как раньше.

### 4.1 Проверка ключа ПЕРЕД ужесточением

Скажи клиенту:
> Прежде чем ужесточать — убедимся что ключ работает 100%.
>
> Открой новую SSH-сессию с явным указанием ключа:
> ```
> ssh -i <путь_к_ключу> -o PasswordAuthentication=no <USER>@<SERVER_IP>
> ```
> (флаг `-o PasswordAuthentication=no` запретит пароль на стороне клиента)
>
> Зашло БЕЗ запроса пароля? Подтверди: «ключ работает, пароль не запрашивался».

Если НЕТ — стоп. Разбираемся с ключами (могут быть проблемы с правами, формат ключа, не та копия). НЕ ужесточаем.

### 4.2 Backup конфига

```bash
cp /etc/ssh/sshd_config /etc/ssh/sshd_config.bak.$(date +%s)
```

### 4.3 Применить hardening

В `/etc/ssh/sshd_config` установить (через sed, чтобы не сломать остальное):

```bash
sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sed -i 's/^#*PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
sed -i 's/^#*PubkeyAuthentication.*/PubkeyAuthentication yes/' /etc/ssh/sshd_config
sed -i 's/^#*ChallengeResponseAuthentication.*/ChallengeResponseAuthentication no/' /etc/ssh/sshd_config
```

> Если есть sudo-юзер — поменяй `prohibit-password` на `no` (полный запрет root). Спроси клиента.

### 4.4 Restart sshd

```bash
sshd -t  # проверка синтаксиса конфига ДО рестарта
systemctl restart sshd
```

### 4.5 🔴 КРИТИЧНАЯ ПРОВЕРКА

Скажи клиенту:
> Открой новую SSH-сессию **по ключу**:
> ```
> ssh -i <ключ> <USER>@<SERVER_IP>
> ```
> Зашло без запроса пароля? Подтверди.
>
> Дополнительно проверь что пароль теперь НЕ работает:
> ```
> ssh -o PreferredAuthentications=password -o PubkeyAuthentication=no <USER>@<SERVER_IP>
> ```
> Должна быть ошибка «Permission denied (publickey)». Если так — hardening применён.

Если новая сессия по ключу НЕ заходит — во второй сессии rollback:
```bash
cp /etc/ssh/sshd_config.bak.* /etc/ssh/sshd_config
systemctl restart sshd
```

---

## Шаг 5 — fail2ban

> fail2ban следит за SSH-логами и банит IP откуда подбирают пароли/ключи. Защита от брутфорса.
>
> Важно: твой IP добавим в `ignoreip` чтобы fail2ban случайно не забанил тебя при опечатке.

### 5.1 Узнать твой IP

Если не узнали в шаге 0:
```bash
who am i  # покажет IP откуда залогинен
# или попроси клиента: curl ifconfig.me на его локальной машине
```

Запомни `<CLIENT_IP>`.

### 5.2 Установка

```bash
apt install -y fail2ban
```

### 5.3 Конфиг

```bash
cat > /etc/fail2ban/jail.d/ssh.conf <<EOF
[DEFAULT]
ignoreip = 127.0.0.1/8 ::1 <CLIENT_IP>

[sshd]
enabled = true
port = <SSH_PORT>
maxretry = 3
findtime = 10m
bantime = 24h
EOF
```

(подставь `<CLIENT_IP>` и `<SSH_PORT>` — 22 или новый)

### 5.4 Запуск

```bash
systemctl enable --now fail2ban
fail2ban-client status sshd
```

Должен показать: jail активен, banned: 0.

---

## Шаг 6 — pg_dump cron (бэкапы Postgres)

> Раз в сутки в 03:00 — pg_dump базы в `/var/backups/<project>/`. Хранение 30 дней. Это критично — без бэкапа в случае повреждения БД восстановить нечего.

### 6.1 Создать директорию

```bash
mkdir -p /var/backups/<PROJECT_SLUG>
chmod 700 /var/backups/<PROJECT_SLUG>
```

### 6.2 Cron-задача

```bash
crontab -l > /tmp/cron.tmp 2>/dev/null || true
cat >> /tmp/cron.tmp <<'EOF'
0 3 * * * cd /root/<PROJECT_SLUG> && docker compose exec -T postgres pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" | gzip > /var/backups/<PROJECT_SLUG>/db_$(date +\%Y\%m\%d).sql.gz && find /var/backups/<PROJECT_SLUG> -name "db_*.sql.gz" -mtime +30 -delete
EOF
crontab /tmp/cron.tmp
rm /tmp/cron.tmp
```

(подставь `<PROJECT_SLUG>` и путь к проекту)

### 6.3 Тест — создать ручной бэкап ПРЯМО СЕЙЧАС

```bash
cd /root/<PROJECT_SLUG>
docker compose exec -T postgres pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" | gzip > /var/backups/<PROJECT_SLUG>/test_backup.sql.gz
ls -lh /var/backups/<PROJECT_SLUG>/test_backup.sql.gz
```

Файл должен быть >0 байт. Если так — cron работает корректно.

### 6.4 Off-site копия и проверка восстановления

**Off-site.** Бэкап из 6.2 лежит на ТОМ ЖЕ сервере, что и БД. Сервер умер
(диск, провайдер, удалённая VM) — пропали и база, и бэкап разом. Нужна копия
вне сервера.

Спроси клиента куда дублировать — облако (S3 / Backblaze B2 / Yandex Object
Storage) или вторая машина. Допиши в конец cron-задачи из 6.2 через `&&`:

```
# облако через rclone (apt install -y rclone; rclone config — настроить remote):
&& rclone copy /var/backups/<PROJECT_SLUG> <REMOTE>:<BUCKET>/<PROJECT_SLUG> --max-age 25h
# либо на вторую машину:
&& rsync -az /var/backups/<PROJECT_SLUG>/ <USER>@<BACKUP_HOST>:/backups/<PROJECT_SLUG>/
```

**Проверка восстановления.** Бэкап, который ни разу не разворачивали, — не
бэкап. Прямо сейчас проверь, что дамп рабочий — разверни его в отдельную БД:

```bash
docker compose exec -T postgres psql -U "$POSTGRES_USER" -c "CREATE DATABASE restore_test;"
gunzip -c /var/backups/<PROJECT_SLUG>/test_backup.sql.gz | docker compose exec -T postgres psql -U "$POSTGRES_USER" -d restore_test
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d restore_test -c "\dt"
docker compose exec -T postgres psql -U "$POSTGRES_USER" -c "DROP DATABASE restore_test;"
```

Если `\dt` показал таблицы — бэкап рабочий. Только теперь его можно считать
бэкапом.

---

## Шаг 7 — Caddy / TLS (опционально, только если нужен публичный домен)

Спроси:
> Настраиваем публичный домен с авто-TLS через Caddy? Если бот только для тестов или клиент не закупил домен — пропускаем.
>
> Если да — какой домен? (например, `admin.<client>.ru`)

### Если «пропустить» — идём к шагу 8.

### Если «настраиваем»:

1. Создай локально в проекте `caddy/Caddyfile`:
   ```
   <DOMAIN> {
       reverse_proxy admin:8080
   }
   ```

2. Добавь сервис caddy в `docker-compose.yml`:
   ```yaml
   caddy:
     image: caddy:2-alpine
     restart: unless-stopped
     ports:
       - "80:80"
       - "443:443"
     volumes:
       - ./caddy/Caddyfile:/etc/caddy/Caddyfile:ro
       - caddy_data:/data
       - caddy_config:/config
     depends_on:
       - admin
   ```
   В `volumes:` секции файла добавь `caddy_data:` и `caddy_config:`.

3. Убери из секции admin строку `127.0.0.1:8080:8080` (теперь админка торчит только в docker-сеть, наружу через Caddy).

4. На сервере:
   ```bash
   git pull
   docker compose up -d
   docker compose logs caddy
   ```

5. Проверка: `https://<DOMAIN>` отдаёт админку, сертификат от Let's Encrypt валиден.

---

## Шаг 8 — Финальный аудит

Покажи клиенту чек-лист с фактическими галочками:

```
Server hardening — итог:

Доступ
[✓/✗] Вторая SSH-сессия использовалась для проверок
[✓/✗] Non-root sudo-юзер: <name> или «работаем под root»
[✓/✗] SSH-ключ работает, парольный вход отключён
[✓/✗] PermitRootLogin: prohibit-password / no

Firewall
[✓/✗] UFW активен
[✓/✗] Открыты только: 22 (или <NEW>), 80, 443, <EXTRA>
[✓/✗] 5432, 6379, 8080 НЕ открыты наружу

SSH-порт
[✓/✗] Порт: 22 (стандартный) или <NEW_PORT>

Защита от брутфорса
[✓/✗] fail2ban активен
[✓/✗] <CLIENT_IP> в ignoreip

Бэкапы
[✓/✗] pg_dump cron в 03:00, retention 30 дней
[✓/✗] Тестовый бэкап создан

TLS / домен
[✓/✗] Caddy с авто-TLS для <DOMAIN> (или «пропущено, нет домена»)
```

### Финальная проверка

> ⚠️ **Финальный тест.** Закрой ВСЕ открытые SSH-сессии (и первую, и вторую, и третью) и открой новую с нуля по новым правилам:
> ```
> ssh -i <ключ> -p <SSH_PORT> <USER>@<SERVER_IP>
> ```
> Зашло? Если ДА — hardening завершён успешно.
>
> Если НЕТ — у тебя остались старые сессии открытые? Если ВСЕ закрыты и новая не заходит — нужен recovery через консоль провайдера. **Поэтому до финальной проверки ОБЯЗАТЕЛЬНО держим хотя бы одну живую сессию.**

---

## Шаг 9 — Внешний мониторинг доступности (рекомендация)

Алерты бота (`alerts.py`) шлются «изнутри» — если умер весь контейнер `bot`,
отозван Telegram-токен или упал сам сервер, отправить алерт уже некому.

Закрывается бесплатным внешним пингом. Рекомендация клиенту:

- **Healthchecks.io** — бот раз в N минут «пингует» URL; пинг пропал → тебе
  приходит уведомление. Точнее всего: добавь в проект фоновую задачу
  `curl <healthchecks-url>` раз в 1-5 мин.
- **UptimeRobot / Better Stack** — внешняя проверка TCP-порта или HTTP-эндпоинта
  (если есть публичный домен с админкой). Проще, но видит только «порт открыт»,
  не «бот реально жив».

Мониторинг вне сервера и вне проекта — поэтому переживает падение и сервера, и
контейнера. Минимум — Healthchecks.io heartbeat, ~5 минут на настройку.

---

## Что делать если всё-таки залочило

Если клиент всё же залочил себя:

1. Не паниковать. Зайти в **консоль провайдера** (Hetzner Robot, DigitalOcean Console, AWS Session Manager, Timeweb VNC и т.д.) — это веб-интерфейс с прямым доступом к VM.
2. Откатить последнее изменение:
   - `ufw disable` если проблема в firewall
   - `cp /etc/ssh/sshd_config.bak.* /etc/ssh/sshd_config && systemctl restart sshd` если в SSH
3. Если консоли нет — связаться с поддержкой провайдера.

---

## Важно

- НИКОГДА не выполняй несколько критичных команд подряд без подтверждения. Один шаг = одно изменение = одна проверка.
- НИКОГДА не отключай способ доступа который сейчас используешь, не проверив новый способ через отдельную сессию.
- Backup конфигов перед правкой — обязательно.
- Скилл может прерваться на любом шаге если клиент скажет «стоп». Это нормально — частичный hardening лучше чем сломанный сервер.
