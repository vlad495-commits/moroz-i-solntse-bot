# Ответ администратора из эскалации — дизайн

## Цель

Дать ролям `owner` и `admin` возможность ответить клиенту из открытой эскалации через существующий ordered transactional outbox. Бот не отвечает параллельно человеку и возвращается в автоматический режим только после подтверждённой доставки ответа.

## Границы

Используем существующие `escalations`, `human_mode`, `messages`, `outbound_messages`, `task_outbox`, `admin_audit_events`, worker и Telegram transport. Новых таблиц, миграций, очередей, фоновых процессов, зависимостей и прямых provider-вызовов из admin нет.

Не входят в работу: назначение ответственного, внутренние комментарии, SLA, вложения, массовые ответы, новые каналы, staging/production/YCLIENTS и автоматический retry результата с неизвестной доставкой.

## Рассмотренные варианты

### A. Существующий outbox и delivery completion — выбран

Admin в одной PostgreSQL-транзакции блокирует клиента, создаёт обычный `outbound_messages` и связанный `task_outbox`, а также безопасный audit `escalation.reply_queued`. Идемпотентный ключ `admin_handoff_reply:{escalation_id}:{reply_token}` связывает ответ с эскалацией без новой схемы. После успешного provider response существующий worker одной транзакцией отмечает outbound как `sent`, пишет ответ в `messages`, закрывает эскалацию, пишет `escalation.reply_delivered` и при отсутствии других открытых обращений выключает human mode.

Плюсы: сохраняются текущие порядок, retry, terminal unknown-delivery и минимум сущностей. Минус: delivery completion должен строго распознавать только собственный namespaced key.

### B. Отдельная таблица `handoff_replies` — отклонён

Она дала бы явную state machine ответа, но продублировала бы `outbound_messages`, потребовала миграцию и синхронизацию двух статусов без доказанной пользы для одного Telegram-сценария.

### C. Прямая отправка из admin — запрещён

Прямой Telegram/provider вызов обошёл бы outbox, единый порядок, worker retry и идемпотентность.

## PostgreSQL human mode

Существующий worker сейчас не проверяет `human_mode`; это обязательный gap текущего контура.

Для каждого `process_message` worker уже берёт общий advisory lock клиента. Под этим lock он дополнительно читает `human_mode`:

- если режим выключен или строки нет, выполняется прежний LLM-путь;
- если режим включён, входящий текст один раз записывается в `messages` с ролью `user`, соответствующий inbox переводится в `processed`, но LLM, token usage и bot outbound не создаются.

Создание эскалации использует тот же advisory lock. Поэтому эскалация и начавшаяся обработка сообщения получают единый порядок: либо LLM-транзакция завершается раньше включения режима, либо worker увидит включённый режим и не вызовет LLM.

## Постановка ответа в очередь

В строке открытой эскалации появляется серверная форма:

- `reply_text`: после `strip()` от 1 до 4096 символов;
- `reply_token`: случайный UUID, созданный при рендере страницы и повторно используемый браузером при double submit;
- существующий CSRF token.

`POST /escalations/{escalation_id}/reply` доступен только `owner` и `admin`. В одной транзакции он:

1. Находит customer ID без вывода его в URL или audit и берёт общий advisory lock клиента.
2. Блокирует открытую эскалацию и строку `human_mode`; ответ разрешён только при активном ручном режиме.
3. Через `MessageRepository.enqueue_outbound_in_transaction` создаёт обычные `outbound_messages` и `task_outbox` с namespaced idempotency key.
4. Только для новой outbound-записи пишет `escalation.reply_queued`. Audit содержит escalation ID, outbound ID и фиксированный статус, но не customer ID, текст ответа, payload или provider data.

Повтор того же POST возвращает тот же outbound ID без второго сообщения, task или audit. Неизвестная эскалация даёт `404`, уже неактивная — `409`, неверный CSRF — `403` до БД.

Старая кнопка и POST немедленного `resolve` удаляются из рабочего интерфейса: иначе бот мог бы вернуться до доставки.

## Подтверждение доставки

`MessageRepository.mark_outbound_sent` сохраняет прежнее поведение для всех обычных сообщений. Только валидный ключ `admin_handoff_reply:{escalation_uuid}:{reply_uuid}` запускает дополнительные действия в той же транзакции, где outbound переходит из `sending` в `sent`:

1. Проверяется, что channel/chat совпадают с выбранной эскалацией.
2. Ответ один раз добавляется в `messages` как `assistant`; user ID и username берутся из последней известной строки этого диалога.
3. Эскалация переводится в `resolved` и получает `resolved_at`.
4. Пишется безопасный `escalation.reply_delivered` с actor/request metadata из парного queued audit.
5. Если других открытых эскалаций клиента нет, `human_mode.enabled=false`, а `expires_at=now()+5 minutes`. Если открытые обращения остались, human mode остаётся включённым и cooldown не начинается.

Условный переход outbound только из `sending` делает повтор delivery callback идемпотентным: история, audit и resolve не дублируются.

Активный worker строит LLM-контекст из PostgreSQL `messages`, поэтому подтверждённый ответ становится частью следующего LLM-запроса. После успешной транзакции worker best-effort удаляет legacy Redis-ключ `chat:{chat_id}:messages`; при его следующем чтении legacy-код восстановит контекст из PostgreSQL. Ошибка cache invalidation логируется без текста и не откатывает durable delivery.

## Cooldown повторной эскалации

Пять минут после delivery-confirmed возврата хранятся в существующем `human_mode.expires_at`. `EscalationService` под общим customer lock не создаёт новую low-rating эскалацию, когда режим выключен и `expires_at > now()`. После окна или при активном human mode прежнее поведение сохраняется. Новая таблица или Redis TTL не нужны.

## Ошибки и безопасность

- Network timeout/unknown delivery оставляет эскалацию и human mode открытыми; слепого resend нет.
- Определённая ошибка до отправки возвращает outbound в `pending` по прежнему retry-контракту.
- Ошибка транзакции enqueue не оставляет outbound без task или audit.
- Ошибка delivery-completion не оставляет частично записанную историю, resolve или audit.
- Ответ HTML-экранируется в истории; raw текст не попадает в audit/log.
- RBAC и CSRF используют существующие функции, admin не получает Telegram token сверх текущего Compose allowlist и не вызывает provider.

## Событийный журнал

Ответ виден в истории через `messages`. Customer event read-model дополнительно связывает безопасные audit действий `escalation.reply_queued` и `escalation.reply_delivered` с клиентом через существующую таблицу `escalations`; customer ID в audit для этого не сохраняется.

## Проверки

- Unit: строгий parser idempotency key, длина/пустой ответ, Redis key invalidation без утечки текста.
- Integration messaging: confirmed delivery атомарно пишет history/audit/resolve/cooldown; unknown/failure ничего не закрывает; duplicate callback не дублирует; другая открытая эскалация удерживает human mode; неверное соответствие fail-closed.
- E2E worker: human mode сохраняет user message, помечает inbox processed и не вызывает LLM/outbound; concurrent enable/process сериализованы общим advisory lock.
- Integration admin: enqueue атомарен и идемпотентен, audit безопасен, inactive/not-found корректны.
- E2E admin: owner/admin видят форму, RBAC/CSRF/validation/root path/escaping соблюдены, немедленного resolve больше нет.
- Финал: affected Docker gates, полный Docker suite, `git diff --check`, независимый review Critical/Important.

## Критерии готовности

- Ни один admin route не вызывает Telegram/provider напрямую.
- Ответ проходит существующие outbox, worker transport, строгий порядок и идемпотентность.
- Пока human mode включён, worker не вызывает LLM и не отвечает клиенту.
- История, event journal и следующий LLM-контекст содержат только подтверждённо доставленный admin reply.
- Бот возвращается только после `sent`, только после последней открытой эскалации и получает пяти минутный cooldown.
- Новых таблиц, очередей, зависимостей и внешних изменений нет.
