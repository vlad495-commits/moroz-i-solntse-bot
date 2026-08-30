# Реактивация клиентов v2 — design-spec

**Статус:** принято владельцем 2026-08-31 с уточнением общей вкладки «Маркетинговые коммуникации»
**Заменяет по смыслу:** `2026-08-30-admin-reactivation-design.md` для всей новой разработки  
**Исходная реализация:** остаётся безопасным legacy dry-run до завершения перехода

## 1. Решение в одном абзаце

Общий owner-only раздел админки называется «Маркетинговые коммуникации». В этой реализации внутри него появляется только одна рабочая возможность — реактивация действительно уснувших клиентов; ручные рекламные рассылки будут отдельной будущей задачей и не получают пустого UI или speculative-кода сейчас. Реактивация не является произвольной рассылкой и не использует три пересекающихся сегмента. Клиент допускается только при доказуемом отдельном marketing consent, надёжной связи Telegram ↔ YCLIENTS, завершённом визите в прошлом, отсутствии осмысленной активности не менее 90 дней, отсутствии будущей записи, отписки, human mode, открытой эскалации и удаления данных. Программа отправляет одно заранее утверждённое сообщение и максимум одно напоминание через 5 дней, повторно проверяя все условия непосредственно перед каждой отправкой. Любой ответ или новая запись отменяет напоминание. По умолчанию система работает только в `dry_run`; реальные отправки нельзя включить без свежих данных, тестовой отправки владельцу, подтверждённого preview и отдельного письменного legal gate.

## 2. Почему текущую версию нужно заменить

Текущая вкладка полезна как безопасный макет: она owner-only, не отправляет сообщения, отделяет marketing consent от processing consent и использует детерминированный SQL. Но как рабочая реактивация она пока оценивается примерно на **5/10**.

Критичные причины:

- выборка строится по локальным `bookings`, а не по полной клиентской истории YCLIENTS;
- текущая YCLIENTS-проекция не хранит стабильную связь клиента YCLIENTS с Telegram-пользователем и не доказывает 90 дней отсутствия активности;
- сегменты «после визита», «спящие» и «постоянные» пересекаются и смешивают причину контакта с характеристикой клиента;
- `discount_percent`, произвольные лимиты и `llm_instruction` выглядят рабочими настройками, хотя фактически не определяют безопасную доставку;
- нет повторной проверки eligibility перед отправкой, preview, test send, финального подтверждения, emergency stop и полноценной отписки;
- ручное свободное создание consent не даёт достаточного доказательства источника и текста согласия;
- `sent` и `error` в интерфейсе создают ложное ожидание результата, пока реального delivery-контура нет;
- успех не связан с ответом, записью или завершённым визитом.

Цель v2 — не «добавить отправку» к существующей форме, а заменить её корректным lifecycle-контуром.

## 3. Что берём из референсов

### Референс Володи / Lucky Hair Studio

| Решение в референсе | Наше решение |
|---|---|
| Автоматизация выключена по умолчанию | **Берём.** Наш default ещё строже: только `dry_run`. |
| Claim задач через `FOR UPDATE SKIP LOCKED` | **Берём.** Это совместимо с текущими scheduler/worker проекта. |
| Повторная проверка лимитов перед отправкой | **Берём и расширяем** до полной повторной проверки eligibility. |
| Резервирование отправки и уникальные delivery keys | **Берём.** Идемпотентность обязательна для каждого шага journey. |
| Сохранение причин `skip` | **Берём.** Причины нужны в preview и операционном журнале. |
| Обработка ответа клиента | **Берём и усиливаем:** любой inbound отменяет reminder, явный STOP ещё и отзывает consent/создаёт suppression. |
| Прямой вызов Telegram из scheduler | **Не берём.** Отправка идёт только через существующие `outbound_messages` и `task_outbox`. |
| Пересекающиеся сегменты и автоматические скидки | **Не берём.** Остаётся одна бизнес-программа без скидки по умолчанию. |
| Runtime-LLM для персонального текста | **Не берём в доставку.** LLM может только предложить черновик владельцу, без клиентских данных. |
| Упрощённое согласие без полноценного источника/отзыва | **Не берём.** Нужна append-only история consent events и suppression. |
| Повтор после неоднозначного результата Telegram | **Не берём.** `delivery_unknown` блокирует автоматический retry. |

Референс используется как источник инженерных паттернов, а не как источник продуктовой или юридической истины.

### Индустриальные практики

- Klaviyo строит win-back относительно нормального цикла покупки и повторно применяет profile filters перед каждым сообщением; новый заказ выводит человека из flow: [win-back flow](https://help.klaviyo.com/hc/en-us/articles/115002775192), [profile filters](https://help.klaviyo.com/hc/en-us/articles/115002779051).
- Customer.io разделяет exit conditions и глобальные/канальные message limits: [exit conditions](https://docs.customer.io/journeys/campaign-exit-conditions/), [message limits](https://docs.customer.io/messaging/send/message-limits/set-up-message-limits/).
- Braze рекомендует frequency capping и контрольные группы для оценки реального эффекта: [frequency capping](https://www.braze.com/resources/articles/whats-frequency-capping), [global control groups](https://www.braze.com/resources/articles/using-global-control-groups-to-measure-the-impact-of-customer-engagement).
- В YCLIENTS уже есть отчёт «Период потери клиента» и отдельное приглашение на повторный визит. Наша программа не должна дублировать близкие post-visit сообщения: [период потери клиента](https://support.yclients.com/402), [приглашение на повторный визит](https://support.yclients.com/5-27-28-104--priglashenie-na-povtornyj-vizit/).
- Транспорт обязан соблюдать ограничения Telegram Bot API; это инфраструктурный rate limit, а не редактируемый бизнес-параметр вкладки: [Telegram Bot FAQ](https://core.telegram.org/bots/faq).

## 4. Цели и границы

### Цели

1. Не писать человеку, если хотя бы одно условие допуска не доказано.
2. Возвращать клиента в содержательный диалог или запись, а не увеличивать счётчик отправок.
3. Исключить повторные сообщения после ответа, записи, отписки или перехода к человеку.
4. Сделать каждое решение объяснимым: почему клиент включён, исключён или остановлен.
5. Использовать существующий надёжный inbox/outbox, scheduler, worker, audit и deletion-контур.
6. Дать владельцу безопасный preview и понятную воронку результата.

### Не входит в v2

- произвольные массовые кампании и импорт списков;
- ручные рассылки об акциях: они входят в целевую архитектуру, но получат отдельные design-spec и implementation plan после реактивации;
- отдельные сценарии «после визита» — это зона штатных уведомлений YCLIENTS;
- сегмент «постоянные» как самостоятельный повод написать;
- автоматическая скидка или выбор скидки моделью;
- runtime-персонализация текста LLM;
- WhatsApp, SMS и другие новые каналы;
- причинное утверждение «реактивация принесла X визитов» до появления корректной контрольной группы.

## 5. Операционная модель

Программа существует в одном экземпляре и имеет три режима:

- `dry_run` — рассчитывает кандидатов, причины исключения и прогноз шагов, но не создаёт реальные outbound-сообщения;
- `paused` — новые journey не создаются, все ещё не отправленные шаги отменяются или удерживаются; история и метрики доступны;
- `active` — разрешено создавать и доставлять journey по активной утверждённой версии правил.

Новая установка, миграция и любой сброс legal/data gate всегда переводят программу в `dry_run` или `paused`, но никогда автоматически в `active`.

### Условия перехода в `active`

Одновременно обязательны:

1. есть активная immutable-версия правил и текста;
2. по этой версии выполнен свежий preview не старше 30 минут;
3. водяные знаки полной истории и свежих записей YCLIENTS проходят data-freshness gate;
4. нет identity conflicts среди включённых получателей;
5. выполнен test send только на заранее настроенный Telegram ID владельца;
6. owner видит итоговые included/excluded counts и подтверждает активацию повторным действием;
7. заполнен и подтверждён письменный legal approval reference, дата и ответственный;
8. действие записано в `admin_audit_events`.

Система не делает юридический вывод сама. По статье 18 закона о рекламе рекламораспространитель должен уметь доказать предварительное согласие и прекратить распространение по требованию адресата; поэтому реальный запуск остаётся отдельным gate: [Федеральный закон «О рекламе», статья 18](https://www.consultant.ru/document/cons_doc_LAW_58968/f892dec1383709792452f18d36e7043306e2be0a/). Квалификацию конкретного текста и сценария подтверждает профильный юрист, а не код.

## 6. Идентичность и полнота данных

### Стабильная связь Telegram ↔ YCLIENTS

Eligibility разрешена только при активной однозначной связи:

`telegram user_id → YCLIENTS client_id`

Приоритет источников:

1. `YCLIENTS client_id`, возвращённый при создании или чтении записи через штатный booking flow;
2. контролируемая сверка номера телефона, который клиент сам передал для записи, с единственным клиентом YCLIENTS;
3. ручное разрешение конфликта владельцем в отдельном audited identity workflow — не свободный ввод ID во вкладке реактивации.

Нулевое, множественное или изменившееся совпадение даёт `identity_unverified` / `identity_conflict` и исключает клиента. По одному номеру нельзя молча объединять несколько профилей. После подтверждения хранится стабильный внешний ID; raw phone не копируется в реактивационные таблицы.

### Единая проекция активности

Для каждого связанного Telegram-пользователя проекция содержит:

- `last_completed_visit_at` — последний действительно завершённый визит YCLIENTS;
- `last_meaningful_inbound_at` — последнее принятое входящее действие клиента в Telegram;
- `next_active_booking_at` — ближайшая неотменённая будущая запись;
- `history_synced_at` — полнота исторической части;
- `recent_bookings_synced_at` — свежесть будущих/изменённых записей;
- `source_version` и диагностический sync status.

Текущего окна YCLIENTS `now-30 … now+90` недостаточно. Sync должен получать полную доступную историю для уже связанных клиентов либо официальный агрегат последнего завершённого визита. Если полноту доказать нельзя, клиент не допускается.

Активная доставка останавливается fail-closed, когда:

- историческая проекция старше 24 часов;
- проекция будущих/изменённых записей старше 15 минут;
- последний sync завершился частично или с ошибкой;
- YCLIENTS API недоступен и свежесть вышла за указанные пределы.

Порог свежести — системный safety contract, не поле свободной настройки в UI.

## 7. Consent, отписка и suppression

Marketing consent остаётся отдельным от `processing_consents`.

Действующее согласие требует:

- канала и user ID;
- события `granted`;
- точной версии и hash текста согласия;
- источника (`telegram_explicit` в первой версии);
- уникального source event ID;
- времени события;
- отсутствия более позднего `revoked`;
- отсутствия активного suppression.

Согласие предлагается в Telegram отдельно и необязательно для получения основной услуги. Кнопки должны однозначно выражать согласие и отказ. Существующие вручную заведённые строки без доказуемого source event не становятся автоматически валидными для v2; они помечаются `legacy_unproven` и остаются исключёнными до нового явного согласия.

### Обработка ответа

- любое принятое inbound-сообщение, callback или медиа после первого шага закрывает pending reminder;
- явные команды/фразы `Не писать`, `Отписаться`, `STOP` и callback-кнопка обрабатываются детерминированно **до LLM**;
- явная отписка в одной транзакции создаёт `revoked` event, persistent suppression и отменяет все pending reactivation steps;
- suppression не снимается администратором обычным toggle. Возврат возможен только через новое явное действие самого клиента с актуальным текстом согласия;
- блокировка бота, жалоба или подтверждённая недоставляемость создают отдельную причину suppression.

## 8. Единственный сегмент и формула eligibility

В продуктовой терминологии остаётся один сегмент: **«уснувшие клиенты»**.

Базовая дата активности:

```text
last_activity_at = max(last_completed_visit_at, last_meaningful_inbound_at)
```

Клиент eligible, только если одновременно истинны все условия:

```text
verified Telegram ↔ YCLIENTS link
AND valid active marketing consent
AND last_completed_visit_at IS NOT NULL
AND now - last_activity_at >= inactivity_period (default 90 days)
AND no active/future booking
AND no active journey
AND no journey started inside cooldown (default 90 days)
AND no active suppression
AND no enabled human_mode
AND no open escalation/complaint
AND no deletion/deletion-in-progress marker
AND YCLIENTS data freshness gates pass
AND program version is active and legally allowed
```

`last_meaningful_inbound_at` определяется детерминированно: это успешно принятое входящее сообщение, callback или медиа пользователя после локального dedup. LLM не решает, является ли активность «достаточно значимой».

### Версионируемые правила

Владелец не получает произвольные диапазоны `0…3650`. Допустимые варианты первой версии:

- неактивность: `60 / 90 / 120` дней, default `90`;
- reminder: выключен либо через `3 / 5 / 7` дней, default `5`;
- cooldown: не меньше выбранного периода неактивности, default `90`;
- quiet window: фиксированное `10:30–20:00 Europe/Moscow` в v2;
- максимум: 2 сообщения на journey и 1 активный journey на клиента.

Любое изменение создаёт новую draft-версию и снова требует preview, test send и activation confirmation.

### Стабильные причины исключения

Минимальный allowlist reason codes:

`no_verified_identity`, `identity_conflict`, `no_proven_consent`, `consent_revoked`, `suppressed`, `no_completed_visit`, `recent_activity`, `future_booking`, `active_journey`, `cooldown`, `human_mode`, `open_escalation`, `deletion`, `stale_history`, `stale_recent_bookings`, `partial_sync`, `program_paused`, `legal_gate_closed`.

UI показывает понятное русское объяснение, а БД и метрики используют стабильный code.

## 9. Journey и переходы состояний

Кандидат — результат расчёта, а не постоянный статус клиента. Постоянная запись появляется только при планировании journey.

```text
eligible candidate
       │
       ▼
   scheduled ──► active ──► closed
                     │          ├─ responded
                     │          ├─ booked
                     │          ├─ suppressed
                     │          ├─ exhausted
                     │          ├─ failed
                     │          └─ cancelled
                     │
                     └─ main → wait 5d → optional reminder
```

Operational state и outcome хранятся раздельно:

- `lifecycle_state`: `scheduled | active | closed`;
- `close_reason`: причина завершения;
- `reply_at`, `booking_created_at`, `completed_visit_at` могут появиться позднее и не теряются, даже если journey уже закрыт как `exhausted`.

Порядок:

1. scheduler создаёт journey только по активной версии;
2. первый шаг планируется на ближайшее разрешённое время quiet window;
3. после подтверждённой передачи первого сообщения Telegram начинается окно ожидания;
4. через 5 дней reminder допускается только после полной повторной проверки;
5. ответ, запись, отписка, human mode, escalation, deletion или stale data отменяют reminder;
6. после reminder без события journey закрывается как `exhausted`;
7. новый journey разрешён только после cooldown и повторного прохождения всех правил.

## 10. Контент сообщения

Runtime использует только immutable owner-approved template. Разрешены server-side placeholders из allowlist, например имя центра и безопасная ссылка/кнопка записи. Произвольные Jinja/Python-выражения и клиентские данные в prompt запрещены.

Обязательные элементы:

- спокойное напоминание без давления и ложной срочности;
- понятное объяснение, почему человек получил сообщение;
- один основной CTA;
- кнопки `Записаться`, `Задать вопрос`, `Не писать`;
- отсутствие скидки по умолчанию;
- отдельные тексты main и reminder;
- preview точного финального рендера.

Опциональная кнопка «Предложить черновик через LLM» может появиться после core-версии. Она передаёт модели только бизнес-инструкцию без данных получателя, возвращает несохранённый draft и никогда не публикует/активирует его сама.

## 11. Доставка и идемпотентность

Новая логика не вызывает Telegram напрямую.

Для каждого шага:

1. scheduler claim-ит due row через короткую транзакцию и `FOR UPDATE SKIP LOCKED`;
2. берётся customer advisory lock;
3. внутри одной транзакции повторяются consent, booking, activity, suppression, human mode, escalation, deletion, freshness и global mode checks;
4. шаг резервируется и создаются `outbound_messages` + `task_outbox`;
5. уникальный key: `reactivation:{journey_id}:{step}`;
6. сетевой вызов происходит вне DB lock через существующий delivery worker;
7. provider result обновляет outbox и reactivation step идемпотентно.

Программа не держит DB lock во время сети. Transport rate limits остаются в sender/worker и не редактируются во вкладке.

Уточнение после сверки с PostgreSQL 16 через Context7: `SKIP LOCKED` используется только как механизм конкурентного claim очереди, а не как источник данных для preview или eligibility, потому что он намеренно пропускает уже заблокированные строки. `pg_advisory_xact_lock` берётся только внутри короткой транзакции и автоматически освобождается при её завершении. После claim условия всё равно перечитываются и проверяются в той же транзакции.

### Emergency stop

Stop переводит программу в `paused`, отменяет ещё не claim-нутые шаги и помечает pending reactivation outbox rows для отмены. Непосредственно перед сетевым вызовом sender обязан выполнить source-aware send guard. Операция stop и send guard используют один program-level lock, чтобы закрыть гонку «остановили одновременно с отправкой».

### Неоднозначная доставка

Если соединение оборвалось после возможной передачи запроса Telegram, шаг получает `delivery_unknown`. Автоматического retry нет. Программа удерживает reminder и новые journey этому клиенту до ручной сверки или доказуемого provider result. Это использует уже принятую в проекте семантику ambiguous post-send.

## 12. Раздел «Маркетинговые коммуникации»

Пункт боковой навигации называется «Маркетинговые коммуникации» и остаётся owner-only. Текущий экран посвящён реактивации и состоит из шести блоков. Пустая вкладка «Рассылки» не создаётся: когда её бизнес-правила будут согласованы отдельной задачей, она добавится вторым внутренним разделом на общей consent/outbox/suppression основе. Legacy URL `/reactivation/` перенаправляет на новый canonical route раздела без потери query-параметров, закладок и audit semantics.

### 12.1. Статус

Показывает:

- режим `Только расчёт / На паузе / Активна`;
- legal gate;
- свежесть полной истории и будущих записей YCLIENTS;
- число eligible сейчас;
- активные journey и ближайший запуск;
- число `delivery_unknown`;
- заметную кнопку emergency stop.

Если gate закрыт, UI объясняет конкретную причину и не рисует активную отправку.

### 12.2. Правила

Показывает inactivity, reminder, cooldown и quiet window простым языком. Изменение разрешённых параметров создаёт draft. Никаких `after_visit_days`, сегмента `regular`, скидки, произвольного ignore limit, произвольного monthly limit и runtime LLM instruction.

### 12.3. Сообщение

Для main и reminder отображаются:

- версия, автор, дата и статус `draft/active/retired`;
- editor с безопасной длиной и allowlisted placeholders;
- точный Telegram preview с кнопками;
- test send владельцу;
- при наличии — необязательный LLM draft.

### 12.4. Preview и активация

До активации owner видит:

- included count;
- excluded counts по reason code;
- несколько маскированных примеров без raw phone и полного Telegram ID;
- свежесть данных и scope версии;
- отличие от текущей активной версии;
- расчёт ожидаемых main/reminder, но не `sent`;
- финальное подтверждение с повторным вводом действия, а не скрытый toggle.

Preview привязан к checksum draft-версии и data watermark. Изменение версии или данных делает подтверждение устаревшим.

### 12.5. Journey и результаты

Таблица показывает клиента в маскированном виде, версию, main/reminder, текущее состояние, ответ, запись, завершённый визит, suppression и конкретную причину завершения. Доступны фильтры по периоду, outcome и ошибкам.

### 12.6. Consent и suppression

Показывает version/source/source event, grant/revoke, suppression reason и историю. Свободной формы «ввести user ID и выдать согласие» в основной вкладке нет.

Legacy campaigns/deliveries доступны только как read-only архив с явной пометкой «Черновая версия, реальные сообщения не отправлялись» либо через экспорт для аудита.

## 13. Метрики и смысл результата

### Основная метрика

**Завершённый визит в течение 30 дней после первого сообщения.** Это называется «визит после реактивации», а не доказанный causal lift.

### Вторичные метрики

- новая запись в течение 14 дней;
- любой ответ в течение 7 дней;
- main accepted by Telegram;
- reminder accepted by Telegram;
- отписка;
- жалоба / escalation;
- suppression;
- permanent failure;
- `delivery_unknown`;
- доля исключений по каждой причине.

Telegram API не доказывает прочтение сообщения, поэтому UI не использует ложный термин «прочитано» и отличает `accepted_by_provider` от бизнес-результата.

Воронка:

```text
eligible → journey started → provider accepted → replied → booked → completed visit
```

Test sends, dry-run previews и legacy rows в метрики не входят.

Контрольная группа не включается формально «для галочки». После накопления достаточного объёма и расчёта статистической мощности можно добавить стабильный holdout и показывать incremental lift. До этого абсолютные outcomes остаются честной операционной метрикой.

## 14. Минимальная модель данных v2

Миграция additive от фактической Alembic head; на момент спецификации ожидаемый следующий номер — `0023`.

Ponytail-ограничение: для одной программы не создаётся отдельная CRM-платформа. Существующие singleton settings, current consent state, audit и outbox переиспользуются. Нужны только пять новых таблиц.

### Новые таблицы

1. `customer_activity_projection`
   - ключ `channel + user_id` и уникальный `YCLIENTS client_id` для подтверждённой связи;
   - identity status/source/verified timestamp;
   - last visit/inbound, next booking;
   - history/recent watermarks и sync status;
   - конфликт хранится как неактивное состояние проекции, а не как второй identity-реестр.

2. `marketing_consent_events`
   - append-only `granted/revoked`;
   - text version/hash, source, source event ID, happened_at;
   - идемпотентность по source event.

3. `reactivation_program_versions`
   - immutable policy и два template;
   - status/checksum, author, approved metadata;
   - последний preview checksum, data watermarks и aggregate counts;
   - test-send result и activation metadata;
   - отдельная таблица preview не нужна: история действий уже хранится в `admin_audit_events`, а для gate достаточно последнего preview конкретной draft-версии.

4. `reactivation_journeys`
   - recipient, program version, lifecycle/close reason;
   - anchor/activity timestamps и outcome timestamps;
   - один active journey на recipient.

5. `reactivation_journey_steps`
   - `main/reminder`, scheduled/reserved/sent/unknown/skipped/cancelled/failed;
   - stable reason, outbox ID, idempotency key;
   - unique journey + step.

### Существующие таблицы

- `marketing_consents` остаётся быстрой materialized current-state проекцией, получает proof/source и suppression columns и обновляется транзакционно вместе с event log; отдельная suppression-таблица не нужна;
- `reactivation_settings` остаётся singleton и получает только mutable program state: `mode`, active version, legal gate, program revision и stop metadata; старые бизнес-поля сохраняются для rollback, но v2 их не читает;
- `reactivation_settings`, `reactivation_campaigns`, `reactivation_deliveries` из migration `0021` не удаляются в первом rollout, но v2 их не использует для отправки;
- legacy data не получает автоматически новый consent proof;
- customer deletion расширяется на activity projection, consent events/current state, journeys, steps и связанные pending outbox rows;
- downgrade удаляет только новые структуры/колонки и не переписывает legacy историю.

## 15. Компоненты и границы кода

1. **YCLIENTS activity sync** — читает и материализует связь, последнюю активность и будущие записи; не отправляет сообщения.
2. **Consent service** — единственная точка grant/revoke/suppression и proof validation.
3. **Eligibility service** — чистая детерминированная политика и SQL reason codes; LLM не участвует.
4. **Journey planner** — создаёт due steps, но не вызывает Telegram.
5. **Reactivation dispatcher** — повторный preflight, reserve и постановка в существующий outbox.
6. **Inbound stop hooks** — отменяют reminder/отзывают consent до запуска LLM.
7. **Outcome projector** — связывает inbound и YCLIENTS booking/visit events с journey.
8. **Admin routes/UI** — preview, versioning, activation, pause/stop, test send, история и метрики.

Все POST-действия используют текущие RBAC, CSRF и `admin_audit_events`.

Названия domain/service/table остаются `reactivation_*`, потому что сейчас реализуется только реактивация. Обобщённый `marketing_engine`, универсальный campaign builder и channel abstraction не создаются. Общими для будущих рассылок уже являются существующий outbox и доказуемые marketing consent/suppression rules; остальное появится только в отдельной задаче.

## 16. Поведение при сбоях

| Сбой | Поведение |
|---|---|
| YCLIENTS stale/partial/unavailable | Нет новых отправок; явный data gate alert. |
| Identity conflict | Клиент исключён, требуется отдельное разрешение конфликта. |
| Consent proof неполный | Клиент исключён как `no_proven_consent`. |
| Два scheduler/worker | Один claim за счёт `SKIP LOCKED` и unique step key. |
| Ответ одновременно с reminder | Customer lock + повторный preflight; inbound побеждает, если зафиксирован до reserve. |
| Pause одновременно с send | Program lock + source-aware send guard. |
| Timeout после Telegram POST | `delivery_unknown`, без retry и reminder. |
| Bot blocked / permanent provider error | Suppression + закрытие journey. |
| Template изменён после preview | Preview становится invalid, активация запрещена. |
| Удаление клиента | Отмена pending, удаление recipient data по действующему deletion contract. |

## 17. Безопасность и приватность

- только `owner` видит вкладку и может менять режим;
- raw phone и полный Telegram ID не попадают в preview/logs;
- template и LLM draft не получают историю конкретного клиента;
- клиентские причины исключения не пишутся в application logs с PII;
- consent events и audit metadata хранятся по отдельному retention/legal contract;
- customer deletion охватывает activity/identity projection, consent current state/events, suppression fields, journeys, steps и pending delivery identifiers;
- secrets YCLIENTS и Telegram остаются только в server environment;
- test send разрешён только на server-configured owner ID, не на введённый в форме произвольный адресат.

## 18. Проверки

Вся разработка и проверки — только через Docker.

### Domain/unit

- таблица истинности eligibility и каждый reason code;
- meaningful inbound, STOP, re-consent и suppression;
- state transitions и outcome windows;
- template allowlist/checksum;
- quiet window и cooldown.

### PostgreSQL/integration

- unique identity link, conflict и consent event idempotency;
- параллельные claims через `SKIP LOCKED`;
- advisory/program locks в гонках reply/stop/send;
- reserve + outbox atomicity;
- delivery unknown без retry;
- cancellation после booking/human mode/escalation/deletion;
- metrics не включают dry-run/test/legacy.

### Admin

- owner-only, CSRF, audit;
- stale/legal/preview/test gates;
- preview counts/reasons и маскирование;
- version activation и invalidation;
- emergency stop;
- отсутствие удалённых ложных полей и терминов.

### Migration/privacy

- upgrade/downgrade от единственной head;
- legacy rows сохранены и не становятся sendable;
- customer deletion и retention;
- отсутствие PII/secrets в логах и preview payloads.

### Acceptance

1. shadow `dry_run` не менее 14 дней;
2. ручная сверка выборки с YCLIENTS на согласованной маскированной выборке;
3. тестовая отправка только владельцу;
4. stop/race/delivery-unknown rehearsal;
5. отдельное явное разрешение на staging rollout;
6. отдельное явное разрешение и закрытый legal gate перед первым реальным клиентским сообщением.

## 19. Порядок rollout

1. **Foundation:** additive schema, identity/activity sync, consent events/suppression, deterministic eligibility.
2. **Safe admin:** v2 UI, versioning, preview, test send, metrics shell; режим только `dry_run`.
3. **Journey runtime:** planner, inbox cancellation, outbox dispatch, unknown/stop semantics; всё ещё `dry_run` по умолчанию.
4. **Shadow acceptance:** 14 дней расчётов, data-quality и business review.
5. **Controlled activation:** только после legal approval, свежего preview, test send и отдельного решения владельца; небольшой первый лимит получателей задаётся rollout-процедурой, не постоянным полем программы.
6. **Post-launch review:** ответы, записи, визиты, отписки, complaints, errors и unknown; при нарушении порогов автоматическая пауза.

Пороговые значения первого controlled rollout будут зафиксированы в implementation/runbook plan после design approval и до любого реального запуска.

## 20. Критерии оценки 9/10

Вкладка получает целевую продуктово-техническую оценку **9/10**, когда одновременно выполнено:

- единая непротиворечивая семантика «уснувшего клиента»;
- полная и свежая активность YCLIENTS + Telegram;
- доказуемый consent и мгновенная отписка;
- repeat eligibility check перед каждым сообщением;
- 1+1 journey, cooldown и quiet hours;
- outbox/idempotency/unknown semantics без дублей;
- preview, test, confirm, pause и emergency stop;
- честные outcomes до завершённого визита;
- owner-only audit, privacy и deletion;
- shadow evidence и контролируемый rollout.

До фактических данных нельзя честно обещать 9/10 по коммерческому uplift. Десятая доля качества появится только после достаточного объёма, контрольной группы и доказанного incremental lift. Это сознательно не имитируется в первой версии.

## 21. Зафиксированные продуктовые решения

- одна программа, а не конструктор рассылок;
- общий пункт админки называется «Маркетинговые коммуникации», но пустой функционал ручных рассылок не реализуется;
- default inactivity/cooldown `90` дней;
- main + максимум один reminder через `5` дней;
- quiet window `10:30–20:00 Europe/Moscow`;
- без скидки по умолчанию;
- без runtime-LLM;
- любое inbound отменяет reminder;
- явный STOP отзывает consent и создаёт suppression;
- stale YCLIENTS и `delivery_unknown` работают fail-closed;
- существующий outbox — единственный путь реальной доставки;
- default mode — `dry_run`;
- реальная клиентская отправка требует отдельного legal и owner approval.
