# Staging-only доступ к админке

## Цель

Исключить повторную путаницу между устаревшими локальными credentials прототипа и действующим staging-контуром.

## Решение

- Единственный рабочий контур админки до production rollout — staging.
- Единственный источник staging credentials — server-only файл `/opt/moroz-staging/.env`.
- Локальные `ADMIN_USERNAME`, `ADMIN_PASSWORD` и Compose-default `ADMIN_SESSION_SECRET` обнуляются и не используются как копия staging-секретов.
- Sessionless bootstrap-cookie принимается только при полностью настроенных непустых credentials и session secret длиной не меньше 32 символов.
- В `AGENTS.md` явно фиксируются staging URL, граница контуров и запрет направлять пользователя к локальному `.env` за staging-доступом.
- Staging-пароль не копируется в Git, документацию, changelog или сообщения.

## Проверка

- Локальный `.env` больше не содержит рабочую пару `admin/admin`; базовый Compose не содержит публичный session secret.
- Правила проекта однозначно называют server-only источник staging credentials.
- Живой staging login остаётся рабочим; staging `.env` и runtime не изменяются.
- Production не затрагивается.

## Не входит в задачу

- Создание индивидуального owner/TOTP.
- Отключение staging bootstrap до доказанного входа owner.
- Production rollout или push.
