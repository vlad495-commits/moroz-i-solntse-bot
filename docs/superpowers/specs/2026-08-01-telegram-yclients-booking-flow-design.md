# Сквозной Telegram → YCLIENTS booking flow

**Дата:** 2026-08-01
**Статус:** дизайн одобрен пользователем
**Этап:** сначала устойчивый booking flow; scheduler и напоминания включаются только после его подтверждения

## Цель

Подключить уже реализованные `BookingService`, `BookingRepository`, mock/real
YCLIENTS adapters и защищённый lifecycle к реальному Telegram message path. Результат
должен давать клиенту полностью кнопочный сценарий создания, переноса и отмены
записи, а системе — доказуемую идемпотентность, принадлежность, аудит и
fail-closed поведение при сбоях внешнего провайдера.

LLM участвует только в классификации свободного текста и консультациях. После
выбора booking-маршрута услуги, мастера, слоты, контакты, подтверждение и любые
изменяющие действия обрабатываются детерминированно.

## Зафиксированные продуктовые решения

- Самостоятельно переносить и отменять можно только записи, ранее созданные
  ботом для того же Telegram user ID. Старые, несопоставленные и чужие записи
  передаются администратору без раскрытия деталей.
- Услуги и мастера читаются из YCLIENTS, но клиенту показываются только ID из
  конфигурационного allowlist.
- Booking UI строится на кнопках. Свободный текст только выбирает маршрут либо
  обслуживает строго ожидаемый текстовый шаг.
- Имя вводится текстом. Телефон принимается через штатную Telegram-кнопку
  «Поделиться контактом» и только когда `contact.user_id` совпадает с
  отправителем.
- При эскалации атомарно создаётся durable задача, отправляется служебное
  Telegram-уведомление и включается `human_mode`.
- `human_mode` снимается только явным действием сотрудника в админке; действие
  журналируется.
- Allowlist услуг и мастеров задаётся конфигурацией по YCLIENTS ID.
- В выборе мастера первым пунктом показывается «Любой мастер», затем конкретные
  разрешённые мастера. В резюме всегда отображается фактически назначенный
  мастер.
- Горизонт поиска — 14 дней.
- Резюме действительно 30 минут. После истечения требуется новый выбор и новая
  проверка слота.
- Одна запись может содержать несколько услуг без отдельного прикладного лимита.
  Действуют ограничения YCLIENTS и техническая пагинация Telegram.
- Перенос и отмена многосервисной записи применяются ко всей записи. Частичные
  изменения передаются администратору.
- При переносе сохраняется набор услуг, но мастер и слот выбираются заново.
- Поздний перенос или отмена менее чем за три часа автоматически не выполняется.
- Вход в сценарий доступен через консервативный intent path и явную
  кнопку/команду.

## Архитектура

### Общий поток

```text
Telegram webhook
    → durable message inbox / task outbox
    → existing worker dispatcher
        ├─ active booking / callback / contact / command
        │    → deterministic BookingWorkflow
        └─ ordinary text
             → guardrails + PII masking
             → security LLM || structured router LLM
                 ├─ booking_create/reschedule/cancel → BookingWorkflow
                 ├─ complaint/medical_risk           → escalation/safe path
                 ├─ FAQ/other                        → current answer LLM
                 └─ invalid/ambiguous/failure        → clarification buttons
```

Webhook только валидирует, нормализует и устойчиво принимает interaction. Он не
вызывает YCLIENTS и не исполняет booking state machine.

`BookingWorkflow` работает внутри существующего worker, но как отдельный handler
и coordinator. Новый контейнер или отдельный сервис не добавляется. Общий
`process_message` не разрастается booking-логикой: dispatcher выбирает один из
изолированных handlers.

### Гибридный LLM-router

Приоритет маршрутизации:

1. Активный durable booking scenario.
2. Валидный opaque callback action.
3. Telegram contact, ожидаемый текущим шагом.
4. Явная booking-команда или постоянная кнопка.
5. Детерминированные safety-сигналы: жалоба и медицинский риск.
6. Structured LLM-router для остального свободного текста.

Router получает только замаскированный текущий текст и ограниченный безопасный
контекст. Он возвращает строгую схему:

```json
{
  "route": "booking_create|booking_reschedule|booking_cancel|faq|other|complaint|medical_risk|unknown",
  "confidence": 0.0
}
```

Router не извлекает booking-параметры, не пишет scenario и не вызывает tools.
`confidence` валидируется в диапазоне `0..1`; стартовый конфигурируемый порог —
`0.80`. Невалидная схема, timeout, недоступность или значение ниже порога
приводят к уточняющему меню, а не к mutation. Основная консультирующая LLM
запускается только после вердикта и только для консультационных маршрутов.

Любой блокирующий verdict security-контура имеет приоритет над результатом
router, независимо от `route` и `confidence`.

Решение адаптирует удачный паттерн Lucky Hair Studio: security и router могут
работать параллельно на PII-масках, а кнопочная цепочка заменяет основную LLM на
booking-маршрутах. В отличие от референса, критическое состояние не хранится
только в Redis, callback не содержит provider IDs, mutation требует резюме и
подтверждения, а ответы проходят durable outbox.

### Компоненты

#### `BookingWorkflow`

Отвечает за Telegram-oriented state machine:

- выбор услуг, мастера, даты и слота;
- пагинацию и возврат на предыдущие collecting-шаги;
- сбор имени и собственного Telegram contact;
- формирование резюме и 30-минутного confirmation window;
- подготовку create/reschedule/cancel scenario;
- передачу подтверждённой операции в существующий `BookingService`;
- преобразование `ScenarioResult` в безопасный Telegram response и следующую
  клавиатуру.

`BookingWorkflow` не реализует повторно ownership, mutation error mapping,
late-change rule, external aggregate locks или outcome-unknown. Эти гарантии
остаются в `BookingService` и `BookingRepository`.

#### `BookingCatalogPort`

Отдельный read-only порт предоставляет каталог услуг и мастеров. Real YCLIENTS
implementation читает официальные booking endpoints, нормализует поля и
применяет allowlist до возврата данных workflow. Mock implementation отдаёт
детерминированные fixtures.

`BookingPort` сохраняет существующий mutation-oriented контракт. Получение
каталога не смешивается с create/get/reschedule/cancel.

Каталог разрешено кратко кешировать в Redis. Потеря Redis приводит к повторному
read-only чтению и не теряет scenario. Слоты читаются свежими перед показом и
повторно проверяются непосредственно перед mutation.

#### Durable interaction actions

Telegram `callback_data` содержит только короткий непрозрачный идентификатор:

```text
booking:<random-action-id>
```

PostgreSQL связывает action ID со следующими полями:

- scenario ID;
- ожидаемый customer/channel identity;
- ожидаемая phase/revision;
- безопасный action kind;
- JSON payload из нормализованных внутренних значений;
- срок действия;
- статус использования и сохранённый результат.

Provider slot IDs и booking IDs не доверяются напрямую callback payload.
Устаревший, чужой, подменённый или уже завершённый action не меняет состояние.
Повтор корректного action возвращает сохранённый результат.

## Идентичность и принадлежность

Первый релиз работает только в Telegram private chat. `customer_id` для
созданной ботом записи привязан к Telegram user ID, а место доставки — к
проверенному private chat ID. Webhook и worker отвергают несогласованную пару
`chat_id/user_id`.

Create сохраняет эту принадлежность в PostgreSQL до внешнего POST. YCLIENTS
получает только непрозрачный `moroz_booking_key`; PostgreSQL остаётся источником
истины о владельце.

Перенос и отмена доступны только когда:

- запись существует в локальной таблице `bookings`;
- она создана bot booking flow;
- Telegram user ID совпадает с локальным owner;
- protected YCLIENTS GET подтверждает exact external ID и
  `moroz_booking_key`;
- актуальные status/start/services согласованы с локальным scenario.

Любая неоднозначность возвращает одинаковый безопасный ответ и эскалацию без
раскрытия факта существования чужой записи.

## Durable state machine

Collecting checkpoints:

```text
services
→ master
→ date
→ slot
→ customer_name
→ customer_contact
→ awaiting_confirmation
→ executing
→ confirmed | escalated
```

Для reschedule перед `master` сохраняется выбранная принадлежащая запись; набор
услуг неизменяем. Для cancel после выбора записи сразу формируется отдельное
резюме и confirmation action.

PostgreSQL хранит всё критическое состояние. Redis используется только для
ускорения каталога/контекста и не является единственным источником ни одного
booking checkpoint.

Checkpoint создаётся после каждого значимого выбора. Нажатие «Назад» разрешено
только в collecting-фазах и создаёт новую revision. После `executing` возврат и
blind retry запрещены.

## Пользовательские сценарии

### Создание

1. Пользователь запускает `booking_create` текстом, командой или кнопкой.
2. Workflow показывает разрешённые услуги с мультивыбором и кнопкой «Готово».
3. YCLIENTS подтверждает, что выбранная комбинация участвует в общем availability
   flow; несовместимая комбинация не создаёт mutation.
4. Пользователь выбирает «Любой мастер» либо конкретного разрешённого мастера.
5. Workflow показывает даты и слоты на 14 дней с пагинацией.
6. После слота запрашиваются имя и собственный Telegram contact.
7. Резюме показывает услуги, фактического мастера, дату/время, имя и маскированный
   телефон.
8. Доступны `Подтвердить`, `Изменить`, `Отмена`.
9. Confirmation старше 30 минут не исполняется.
10. После подтверждения `BookingService` повторно читает availability, делает
    checkpoint `executing`, вызывает YCLIENTS и сохраняет terminal snapshot.
11. Только однозначный provider success даёт сообщение о подтверждённой записи.

### Перенос

1. Показываются только активные bot-created bookings данного Telegram owner.
2. Пользователь выбирает запись.
3. Набор услуг сохраняется целиком.
4. Мастер и слот выбираются заново.
5. Резюме содержит старые и новые условия.
6. Менее чем за три часа workflow не начинает mutation и создаёт эскалацию.
7. После подтверждения ownership, актуальная запись и слот проверяются повторно.

### Отмена

1. Показываются только активные bot-created bookings данного owner.
2. Отдельное резюме перечисляет отменяемую запись.
3. Mutation доступна только через `Да, отменить запись`.
4. Менее чем за три часа создаётся эскалация без provider DELETE.
5. Success сообщается только после однозначного ответа YCLIENTS и terminal local
   snapshot.

Частичное изменение услуг внутри многосервисной записи не поддерживается и
эскалируется.

## Ошибки и fail-closed

- Timeout, `429`, `5xx` или malformed response при каталоге/availability/protected
  GET до mutation дают временную недоступность без обещания слота.
- Ошибка `book_check` до mutation не создаёт запись.
- Documented slot conflict переводит scenario к выбору свежих альтернатив.
- Transport failure, timeout, `5xx`, unexpected status или malformed success
  после отправки mutation дают `booking_outcome_unknown`.
- `outcome unknown` запрещает автоматический повтор исходной mutation.
- Mismatch owner, external ID, `moroz_booking_key`, услуг, мастера, времени или
  статуса эскалируется.
- Scenario, найденный в `executing` после рестарта, не исполняется повторно.
- Router failure никогда не становится booking mutation.

Read-only reconciliation по `moroz_booking_key` может закрыть неизвестный
результат только при ровно одном однозначном совпадении. Ноль или несколько
совпадений оставляют эскалацию открытой.

## Идемпотентность и конкуренция

- Telegram update дедуплицируется существующим inbox key.
- Interaction task и outbound reply имеют стабильные idempotency keys.
- Action token имеет сохранённый terminal result.
- Scenario сериализуется PostgreSQL lock и revision check.
- Внешняя запись сериализуется существующим namespaced lock по `external_id`.
- Mutation использует стабильный scenario idempotency key, но это не выдаётся за
  provider exactly-once.
- Terminal scenario replay не вызывает port повторно.
- Два пользователя могут увидеть один слот. Оба проходят свежий `book_check`;
  provider conflict проигравшего возвращает свежие альтернативы.
- Mock/integration контур дополнительно проверяет конфликт на нижнем DB/domain
  уровне по паттерну референсного проекта, но YCLIENTS остаётся источником
  доступности в real flow.

## Эскалация и human mode

Booking escalation должна одной транзакцией:

1. Перевести scenario в `escalated`.
2. Добавить безопасный `booking_events` reason code.
3. Создать открытую запись `escalations`.
4. Включить durable `human_mode` для customer ID.
5. Поставить служебное уведомление сотруднику в Telegram outbox.
6. Поставить нейтральный клиентский ответ в Telegram outbox.

Пока `human_mode` активен, новые сообщения сохраняются, но LLM и booking
mutations не запускаются. Админка показывает эскалацию и новые сообщения.
Сотрудник отвечает через устойчивый исходящий path и завершает режим кнопкой
«Закрыть эскалацию и вернуть бота». Закрытие, actor, время и причина попадают в
admin audit. Автоматического TTL-возврата нет.

## Конфигурация

К существующим YCLIENTS settings добавляются явные allowlists услуг и мастеров,
14-дневный horizon и 30-минутный confirmation TTL. Production/staging startup
fail-closed проверяет:

- непустые numeric YCLIENTS IDs;
- отсутствие дублей;
- доступность разрешённых IDs через read-only preflight;
- настроенный staff Telegram chat для booking escalations;
- полную YCLIENTS конфигурацию для real booking mode.

Значения токенов, телефонов и provider payload не выводятся в config errors или
логи.

## Scheduler и уведомления

Repository уже планирует notification jobs из подтверждённого booking snapshot,
но scheduler остаётся выключенным в staging до доказательства основного flow.

После подтверждения booking flow отдельный этап включает и проверяет:

- подтверждение сразу после create;
- замену старых jobs при reschedule;
- завершение pending jobs при cancel;
- отсутствие jobs для `outcome unknown`;
- дедупликацию повторных scheduler deliveries;
- доставку только владельцу подтверждённой записи;
- read-only lifecycle GET и no-show/completed processing.

## Проверка и порядок внешних действий

### A. Local mock через Docker

- unit: router schema/fallback, state transitions, catalog allowlist, action
  validation, contact ownership, summary/expiry;
- integration: migrations, checkpoints, action replay, inbox/outbox, audit,
  escalations/human mode, locks;
- E2E: Telegram create/get/reschedule/get/cancel на mock adapter;
- duplicate Telegram update и callback replay;
- два synthetic owner на одном слоте;
- чужая запись;
- timeout/429/5xx/malformed/outcome unknown;
- restart from every durable checkpoint;
- admin reply and audited human-mode resolution.

### B. Read-only YCLIENTS

- получить и отфильтровать услуги/мастеров;
- проверить многосервисную availability;
- получить даты/слоты на 14 дней;
- доказать отсутствие mutation requests;
- не читать и не выводить чужие records/ПД.

### C. Sandbox mutations

Только после отдельного явного разрешения пользователя и только на выделенном
test-филиале с фейковыми данными:

```text
create → protected get → reschedule → protected get → cancel → reconciliation
```

До запуска задаются точный cleanup target, bounded date window и уникальный
`moroz_booking_key`. После любого исхода выполняется bounded read-only
reconciliation. При outcome unknown blind cancel запрещён; создаётся ручной
reconciliation checklist. Реальные клиентские записи и реальные ПД не
используются.

### D. Scheduler/reminders

Запускается только после успешного этапа C и отдельного подтверждения. Проверяет
create/reschedule/cancel job lifecycle, replay, stale jobs, ownership и
read-only provider lifecycle.

## Обязательные доказательства результата

Отдельный итоговый тест-план и отчёт должны содержать воспроизводимые Docker
команды и evidence для:

- create/get/reschedule/get/cancel;
- duplicate/replay;
- slot race двух пользователей;
- чужой записи;
- YCLIENTS timeout/429/5xx;
- outcome unknown и reconciliation;
- полного Telegram UI;
- PostgreSQL inbox/outbox/booking events/escalations/admin audit;
- scheduler/reminders после подтверждённой записи.

## Не входит в первый implementation checkpoint

- Управление старыми или созданными не ботом YCLIENTS-записями.
- SMS-подтверждение телефона и межканальное объединение профилей.
- Частичное изменение услуг одной записи.
- Новый booking microservice или отдельный booking container.
- Включение scheduler/reminders до устойчивого booking flow.
- Любые реальные client mutations или реальные ПД.
- Push, production rollout или staging mutation без отдельного разрешения.
