# Admin Bookings Read Model — Design

## Решение

Первую фазу профессионального центра записей делаем как безопасный read-only экран локальной PostgreSQL-проекции. Он показывает, что бот уже создал, перенёс, отменил или сверил через существующий booking pipeline, но не обращается к YCLIENTS при открытии страницы и не обещает полноту всего календаря центра.

YCLIENTS остаётся источником правды. Отдельные следующие фазы будут отвечать за синхронизацию записей, созданных вне бота, и за операции переноса/отмены из админки. Эти фазы не входят в текущую реализацию и требуют отдельной проверки sandbox API и прав аккаунта.

## Почему поэтапно

Рассмотрены три подхода:

1. **Локальная read-only проекция — выбранный вариант.** Переиспользует существующие `bookings`, `booking_scenarios` и `booking_events`, работает при недоступном YCLIENTS и не может случайно изменить реальную запись.
2. **Живой запрос к YCLIENTS при каждом открытии экрана.** Даёт более свежие данные, но превращает админку в зависимую от latency, rate limits, credentials и provider outage. Не выбран для первой фазы.
3. **Сразу полная синхронизация и управление.** Даёт максимальную функциональность, но смешивает read model, reconciliation и опасные mutations в одну большую задачу. Разделён на последующие фазы.

## Пользовательская ценность

Owner и admin получают единое место, где можно:

- увидеть ближайшие известные боту записи;
- найти записи, требующие внимания;
- проверить локальный статус отмены, завершения или неизвестного исхода;
- открыть связанную карточку клиента и событийный журнал;
- понять, когда локальная копия обновлялась последний раз;
- получить provider booking ID для ручной сверки в YCLIENTS.

Экран явно сообщает: «Локальная проекция записей бота. Полный календарь и окончательный статус находятся в YCLIENTS». Пустой экран не трактуется как доказательство отсутствия записей в YCLIENTS.

## Границы текущей фазы

Входит:

- owner/admin RBAC;
- пункт меню «Записи»;
- bounded список с keyset pagination;
- представления `upcoming`, `attention`, `history`;
- фильтр по allowlisted статусу;
- безопасная карточка одной записи по внутреннему UUID;
- ссылка на существующую карточку клиента;
- allowlisted timeline booking events;
- безопасный audit просмотра карточки записи;
- адаптивный Jinja UI без обязательного JavaScript;
- unit, PostgreSQL integration и HTTP/RBAC/escaping тесты.

Не входит:

- live/read-through запрос к YCLIENTS;
- импорт записей, созданных вручную в YCLIENTS;
- редактирование, перенос, отмена и создание записи из админки;
- каталог названий услуг и специалистов;
- новая таблица, миграция, очередь или зависимость;
- staging/production rollout и реальные provider-вызовы.

## Read model

### Список

Источник — `bookings` с `LEFT JOIN booking_scenarios` по `last_scenario_id`. Запрос выбирает только явные колонки:

- внутренний `bookings.id`;
- `customer_id`;
- `external_id` только для карточки, не для URL;
- `starts_at`, `scheduled_end_at`, `status`, `updated_at`;
- `kind`, `phase`, `error_code` последнего сценария.

`snapshot`, `state` и event `payload` не выбираются и не передаются в шаблон.

Представления:

- `upcoming` — подтверждённые или неизвестные записи с будущим `starts_at`, сначала ближайшие;
- `attention` — status `unknown` либо последний scenario в `executing`, `failed` или `escalated`, сначала недавно обновлённые;
- `history` — остальные записи, сначала самые поздние.

Дополнительный status-фильтр принимает только `confirmed`, `cancelled`, `completed`, `no_show`, `unknown`. Любое другое значение возвращает 422 до обращения к БД.

Пагинация — односторонний opaque base64 cursor на паре сортировки и внутреннем UUID, limit 50. Cursor строго валидируется; malformed cursor возвращает 422. Поздние вставки не должны дублировать уже просмотренные элементы.

### Карточка записи

URL использует внутренний UUID: `/bookings/{booking_id}`. Карточка показывает:

- безопасные поля списка;
- provider booking ID для ручной сверки;
- локальную метку свежести `updated_at`;
- ссылку `/chats/{customer_id}`, если ID совместим с текущим Telegram chat route;
- последний scenario с allowlisted русскими названиями kind/phase/error;
- timeline из `booking_events`: время и allowlisted title без raw payload.

Неизвестные значения отображаются нейтрально как «Неизвестный статус» или «Системное событие», но исходная строка не выводится.

## Безопасность и аудит

- Доступ имеют только роли `owner` и `admin` через существующие session/RBAC helpers.
- Все запросы параметризованы; HTML экранируется Jinja.
- GET не содержит CSRF, потому что не меняет booking/YCLIENTS state.
- Просмотр карточки создаёт `booking.view` в существующем `admin_audit_events` с internal booking UUID, actor, IP и user-agent. Customer ID, external ID, snapshot и payload в audit не пишутся.
- Если audit карточки нельзя записать, подробная карточка fail closed и не отдаёт чувствительные данные. Список остаётся обычной авторизованной read-моделью, как текущий список диалогов.
- Отсутствующий booking возвращает 404; недоступная БД — 503, а не ложный пустой список.

## Поток данных

1. Booking workflow работает как сейчас и атомарно обновляет `bookings`/scenario/events.
2. Администратор открывает `/bookings/`.
3. Admin backend читает bounded allowlisted проекцию только из PostgreSQL.
4. Шаблон показывает локальную свежесть и предупреждение о границе YCLIENTS.
5. При открытии детали backend в одной DB-транзакции читает запись и фиксирует безопасный audit.
6. Ни admin route, ни template не вызывают YCLIENTS или transport provider.

## Ошибки и неоднозначность

- `unknown` и `booking_outcome_unknown` всегда попадают в «Требуют внимания».
- `scheduled_end_at = NULL` отображается как отсутствие данных, длительность не угадывается.
- Отсутствие service/staff names не компенсируется чтением raw JSON и не заполняется выдуманными значениями.
- Если у клиента нет локальной истории сообщений, booking остаётся видимым, а ссылка на диалог помечается как потенциально пустая.
- Provider outage не влияет на чтение уже сохранённых данных.

## Тестовая стратегия

- Unit: allowlist labels, view/status/cursor validation, unknown-value redaction.
- Integration: три представления, status filter, stable keyset pagination, detail projection, 404, safe audit и rollback/fail-closed при audit error.
- HTTP: login redirect, owner/admin access, запрет другим ролям, root-path links, Jinja escaping, 422 malformed filters/cursors, отсутствие raw snapshot/state/payload.
- Regression: customer deletion, customer event journal, escalation/admin RBAC и booking repository.
- Финал: focused Docker gate, независимый review и свежий полный Docker suite.

## Последующие фазы

1. **YCLIENTS reconciliation:** отдельный read adapter/job, provenance и freshness, импорт/сверка записей вне бота без второго календаря.
2. **Admin mutations:** повторная live-проверка, preview/confirm, idempotent transfer/cancel через существующий YCLIENTS adapter, audit и fail-closed unknown outcome.

Каждая последующая фаза получает отдельную spec/TDD-задачу и не меняет границы этой read-only реализации.
