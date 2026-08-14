# YCLIENTS Booking Reconciliation — Design

## Решение

Расширяем существующий read-only центр записей до единого экрана всех записей YCLIENTS за ограниченное окно: последние 30 дней и следующие 90 дней. Экран отличает записи, созданные ботом, от записей из других каналов и показывает расхождения между локальным booking-состоянием и YCLIENTS.

YCLIENTS остаётся источником истины для provider-facing полей. Эта фаза только читает и сравнивает данные: она не создаёт, не переносит, не отменяет записи и не переписывает локальный booking workflow по результатам сверки.

## Пользовательская ценность

Owner и admin получают один экран, где можно:

- увидеть все записи YCLIENTS в рабочем окне;
- отфильтровать записи бота и записи других каналов;
- увидеть вклад бота без догадок по имени, телефону или времени;
- заметить перенос, отмену, удаление или изменение статуса в YCLIENTS;
- найти локальную запись, отсутствующую в последнем полном снимке YCLIENTS;
- увидеть время последней успешной синхронизации и предупреждение об устаревших данных.

## Рассмотренные подходы

1. **Live read-through при открытии админки.** Отклонён: UI зависел бы от latency, rate limits, credentials и доступности YCLIENTS.
2. **Сохранять чужие записи в существующие `bookings`/`booking_scenarios`.** Отклонён: для них нет локального клиента и сценария бота, поэтому потребовались бы искусственные domain-данные.
3. **Одна отдельная безопасная PostgreSQL-проекция — выбранный вариант.** Она хранит последний целый снимок YCLIENTS, а admin read model объединяет его с существующими локальными таблицами.

## Ponytail-граница

Переиспользуем:

- `YclientsConfig`, `YclientsHttpClient` и существующий rate limiter;
- уже проверенный sandbox-smoke контракт `GET /api/v1/records/{company_id}` с `page`, `count`, `start_date`, `end_date`, `with_deleted=1` и user auth;
- существующие `scheduler`, `worker`, RabbitMQ task routing и `scheduler_jobs`;
- PostgreSQL advisory locks;
- текущие admin session/RBAC/Jinja/root-path helpers;
- существующий центр записей и его keyset pagination.

Не добавляем новый сервис, очередь, кэш, frontend dependency, WebSocket, generic sync framework или ручную кнопку обновления. Новая dependency не требуется.

## Границы фазы

### Входит

- миграция одной таблицы `yclients_booking_projection`;
- read-only постраничное получение записей YCLIENTS;
- автоматический sync-job раз в 10 минут через текущий scheduler/worker;
- полная валидация ответа до записи в БД;
- атомарная замена проекции;
- provenance `bot` / `other` и безопасные reconciliation-состояния;
- фильтры источника и расхождений в существующем admin UI;
- метка последней успешной синхронизации и stale warning после 20 минут;
- автоматическое удаление из проекции данных вне окна за счёт полной замены снимка;
- unit, PostgreSQL integration, scheduler/worker и HTTP/RBAC tests.

### Не входит

- ручной refresh;
- точное разбиение `other` на администратора, виджет, сайт и другие источники;
- телефон, email, комментарий клиента и произвольные YCLIENTS-поля;
- отдельная карточка YCLIENTS-only записи;
- автоматическое исправление локального booking state;
- создание, перенос или отмена из админки;
- KPI/аналитика конверсии;
- staging/production rollout и реальные provider-вызовы во время разработки.

## API read contract

Reader использует существующий `YclientsHttpClient`:

```text
GET /api/v1/records/{company_id}
page=<1..N>
count=100
start_date=<YCLIENTS local date - 30 days>
end_date=<YCLIENTS local date + 90 days>
with_deleted=1
user_auth=true
```

Границы дат вычисляются в `YCLIENTS_TIMEZONE`. Reader идёт до первой страницы короче 100 элементов. Жёсткий safety bound — 100 страниц; полная сотая страница считается неоднозначным результатом и завершает job ошибкой без изменения проекции.

Принимается только envelope `success=true` с `data: list[object]`. Ошибка транспорта, HTTP не 200, malformed envelope, malformed обязательное поле или превышение page bound отклоняют весь снимок.

## Безопасная нормализация

Из каждой записи разрешены только:

- provider `id` как каноническая текстовая форма положительного integer;
- `datetime` и `seance_length`, нормализованные в aware timestamps;
- `attendance` и `deleted`, нормализованные через тот же контракт, что текущий `_visit_status`: deleted → `cancelled`, `-1` → `no_show`, `0/2` → `confirmed`, `1` → `completed`, остальные integer/отсутствие → `unknown`;
- `deleted` как строгий boolean;
- имя клиента как bounded display text;
- имя сотрудника как bounded display text;
- bounded список названий услуг;
- custom field `moroz_booking_key`, разобранный как UUID;
- состояние bot marker: `absent`, `valid`, `invalid`.

Не читаются и не сохраняются телефон, email, комментарий, agreements, произвольные custom fields и полный JSON. Имя, сотрудник и услуги считаются недоверенными display-данными: они не попадают в LLM, логи, audit payload или HTML без Jinja escaping.

Display text очищается от управляющих символов, trim-ится и ограничивается 200 символами. Отсутствующие client/staff/services допустимы, потому что YCLIENTS разрешает запись без привязанного клиента или услуги. Массив услуг ограничен 50 элементами; превышение считается malformed response и отклоняет снимок, а не создаёт неограниченную запись.

## PostgreSQL-проекция

Новая таблица `yclients_booking_projection` содержит:

- `external_id text PRIMARY KEY`;
- `booking_key uuid NULL`;
- `bot_marker_state text NOT NULL CHECK (... IN ('absent','valid','invalid'))`;
- `starts_at timestamptz NOT NULL`;
- `scheduled_end_at timestamptz NULL`;
- `status text NOT NULL CHECK (... IN ('confirmed','cancelled','completed','no_show','unknown'))`;
- `deleted boolean NOT NULL`;
- `client_name text NULL`;
- `staff_name text NULL`;
- `service_names text[] NOT NULL`;
- `synced_at timestamptz NOT NULL`.

Индексы добавляются только для фактических запросов: `(starts_at, external_id)` и partial/index по `booking_key` для непустых ключей. `source` и reconciliation state не хранятся: они вычисляются при чтении из проекции и локальных `bookings`.

В таблице всегда находится только последний успешно полученный 120-дневный снимок. Это автоматически ограничивает хранение YCLIENTS-only имени примерно 30 днями после визита. Retention локальных записей бота остаётся прежним.

## Атомарное обновление

1. Worker получает sync-job и пытается взять session advisory lock `yclients_booking_projection:v1`.
2. Если lock занят, job безопасно завершается как skipped; параллельного API-чтения нет.
3. Под lock worker загружает и нормализует все страницы в памяти с bounded limits.
4. После полной валидации worker открывает одну PostgreSQL-транзакцию.
5. В транзакции удаляется предыдущая проекция и batch-вставляется новый снимок с единым `synced_at`.
6. Commit делает новый снимок видимым целиком. При любой DB-ошибке rollback сохраняет старый снимок.
7. Lock освобождается в `finally`.

PostgreSQL MVCC гарантирует, что admin-запрос видит либо старый, либо новый снимок, но не пустое промежуточное состояние.

## Планирование и идемпотентность

Worker уже получает YCLIENTS credentials и `SchedulerJobRepository`, поэтому scheduler-контейнер не получает новую конфигурацию и не меняется. При старте настроенный worker идемпотентно обеспечивает pending job текущего UTC 10-minute bucket. Обработчик каждой sync-job до provider-чтения обеспечивает job следующего bucket, поэтому временная ошибка текущей синхронизации не останавливает последующие циклы:

```text
kind = yclients_booking_projection_sync
idempotency_key = yclients_booking_projection_sync:<bucket-start>
```

Дальше неизменённый scheduler подхватывает due job по штатному пути `scheduler_jobs -> RabbitMQ -> worker`. Новая очередь не создаётся. Повтор startup/retry одного bucket не создаёт вторую job благодаря текущему unique `idempotency_key`; advisory lock защищает от перекрытия соседних bucket. Если worker завершился до обработки, следующий startup снова обеспечивает текущий bucket и восстанавливает цикл.

Успешность и свежесть берутся без отдельной sync-state таблицы. Для непустого снимка authoritative timestamp — единый `MAX(yclients_booking_projection.synced_at)`; для корректного пустого снимка — `finished_at` последней успешной sync-job. Последняя неуспешная job и её allowlisted `last_error_code` берутся из `scheduler_jobs`. Если projection commit прошёл, а terminal update job временно не прошёл, timestamp непустой проекции всё равно честно отражает успешно сохранённый снимок; повтор job остаётся идемпотентным.

## Provenance и reconciliation

Сопоставление использует только `external_id` и `moroz_booking_key`, никогда имя, телефон или время.

### Источник

- `bot`: совпали локальный `bookings.external_id`, локальный `booking_key` и валидный marker;
- `bot`: локальный external ID существует, но marker исчез или конфликтует; такая запись одновременно требует внимания;
- `bot`: в проекции есть валидный marker, но локальная запись отсутствует; это возможный unknown-outcome/local-loss и требует внимания;
- `other`: marker отсутствует и локальной записи с external ID нет;
- invalid marker не относится к `other`, а получает нейтральное происхождение и `identity_conflict`.

### Состояния

- `in_sync`: identity совпала, provider-facing время/окончание/статус совпадают;
- `changed_in_yclients`: identity совпала, но provider-facing поля отличаются;
- `yclients_only`: запись существует только в проекции и marker отсутствует;
- `local_missing`: валидный marker есть, локальной записи нет;
- `provider_missing`: локальная запись есть, но её нет в последнем успешно полученном снимке;
- `identity_conflict`: marker malformed, отсутствует у известной локальной записи или не совпадает с локальным ключом/ID;
- `freshness_unknown`: ещё не было ни одной успешной синхронизации; отсутствие строки не трактуется как `provider_missing`.

Состояния `changed_in_yclients`, `local_missing`, `provider_missing` и `identity_conflict` входят в представление «Требуют внимания». Никакое состояние не запускает mutation автоматически.

## Admin read model и UI

Существующие вкладки сохраняются:

- «Ближайшие»;
- «Требуют внимания»;
- «История».

Объединённая строка получает стабильный внутренний read-model key: `y:<external_id>` для строки из проекции и `l:<local UUID>` для local-only строки. Этот key не хранится как новая колонка и не показывается пользователю. Новый opaque cursor сериализует только aware sort timestamp и bounded row key; decode принимает только точные поля и префиксы `y:`/`l:`. Старый UUID-cursor остаётся совместим только с прежней локальной выборкой и заменяется для unified route новым versioned cursor без попытки угадать формат.

Сортировка и keyset pagination:

- «Ближайшие»: `(starts_at, row_key)` по возрастанию;
- «История»: `(starts_at, row_key)` по убыванию;
- «Требуют внимания»: `(attention_at, row_key)` по убыванию, где `attention_at` — `projection.synced_at` для provider discrepancy и локальный `updated_at` для local workflow issue.

Limit остаётся 50. Поздняя вставка перед cursor не должна дублировать уже просмотренную строку.

Добавляются allowlisted фильтры:

- источник: `all`, `bot`, `other`;
- сверка: `all`, `mismatch`.

Для строки, присутствующей в YCLIENTS-проекции, время/окончание/status берутся из проекции как provider truth. Локальные scenario/phase используются только для bot-owned контекста. YCLIENTS-only строка показывает имя, сотрудника, услуги, время, безопасный status и «Другой канал», но не получает ссылку на Telegram chat или отдельную detail route.

Unified route ограничивает и provider-, и local-only строки тем же окном -30/+90 дней. Более старая локальная история остаётся в PostgreSQL по действующему retention, но не входит в этот оперативный экран; отдельный исторический архив в этой фазе не создаётся.

Bot-owned строка сохраняет текущую ссылку на локальную карточку и chat только при безопасном `customer_chat_id`. Provider ID по-прежнему не выводится в общем списке.

Вверху показывается:

- время последней успешной синхронизации;
- предупреждение при отсутствии успешного снимка;
- stale warning, если успех старше 20 минут;
- нейтральное сообщение о последней ошибке только по allowlisted error code, без provider body.

Admin route никогда не вызывает YCLIENTS. Недоступность provider не мешает читать предыдущий снимок.

## RBAC, аудит и приватность

- Экран доступен только `owner` и `admin` через существующие session/RBAC helpers.
- Все новые route остаются GET-only; mutation и CSRF-форма не добавляются.
- Список следует текущей политике admin read models и не создаёт per-row audit.
- Открытие существующей локальной карточки продолжает fail-closed писать `booking.view`.
- YCLIENTS-only detail route отсутствует, поэтому новый sensitive-detail audit contract не требуется.
- Sync-job не пишет имена/услуги/provider body в логи, error codes или `scheduler_jobs.payload`.
- Customer deletion локальной истории не удаляет provider truth из YCLIENTS-проекции; после удаления связь с локальным chat исчезает, а строка отображается как provider-only/marker anomaly до выхода из 30-дневного окна. Сам provider не изменяется.

## Ошибки и наблюдаемость

Allowlisted terminal/retry error codes:

- `yclients_transport`;
- `yclients_http_status`;
- `yclients_response_shape`;
- `yclients_page_bound`;
- `yclients_projection_write`.

Provider body, query credentials и персональные поля не логируются. Existing retry/DLQ policy применяется без отдельного механизма. Старый снимок и последний успешный timestamp сохраняются при любой ошибке.

## Тестовая стратегия

- Unit: parser/normalizer, timezone window, page termination/bound, status redaction, name/service bounds, UUID marker states, provenance/reconciliation matrix.
- HTTP adapter: exact GET path/query/user auth, pagination, `with_deleted=1`, fail-closed transport/status/envelope/shape.
- PostgreSQL integration: migration, exact schema/constraints/indexes, advisory lock, atomic replacement, rollback preserving old snapshot, bounded retention window, no phone/raw JSON columns.
- Scheduler/worker: one job per bucket, existing queue kind, duplicate idempotency, overlapping-run skip, retry/terminal codes, no new queue.
- Admin integration/HTTP: unified list, three views, source/mismatch filters, provider-truth fields, stale/no-snapshot warnings, root path, owner/admin RBAC, no chat/detail link for YCLIENTS-only, Jinja escaping and absence of raw fields.
- Regression: existing booking repository/lifecycle, customer deletion/events, escalation/admin security, documented Compose commands.
- Final: focused Docker gate, independent review, fresh full Docker suite and migration upgrade/current proof.

Все provider tests используют fake transport/fixtures. Реальный read-only sandbox preflight прав, пагинации и response shape остаётся отдельным явным шагом и не выполняется в этой feature-работе.

## Приёмочные критерии

Фаза готова, когда:

1. Успешный снимок всех страниц за 30/90 дней атомарно виден в админке.
2. Ошибка на любой странице или при записи оставляет предыдущий снимок неизменным.
3. Записи бота и других каналов различаются по ID/key contract без эвристик по ПД.
4. Расхождения видимы и ничего не изменяют автоматически.
5. YCLIENTS-only данные ограничены именем и рабочими booking-полями; телефон, email, комментарии и raw JSON отсутствуют.
6. Admin UI не выполняет provider-вызовы и работает по последнему снимку при outage.
7. Не добавлены новый сервис, очередь или dependency.
8. Docker gates, независимый review и Git hygiene завершены без Critical/Important замечаний.
