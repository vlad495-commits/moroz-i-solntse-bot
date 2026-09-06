# Свободный LLM-диалог и надёжная Telegram-запись

> Историческая спецификация реализованного локального пакета. Позднее владелец отложил staging rollout; исходная строка разрешения ниже не является текущим разрешением. Решение 2026-09-06 допускает последовательные уточнения и контекстные кнопки, выбирает ручную консультационную KB вместо YCLIENTS grounding. Продолжение — [новая узкая spec](2026-09-06-manual-consultation-knowledge-design.md), текущий статус — только `Дорожная карта.md`. Остальной текст сохранён как история, не команда повторно исполнять старый пакет.

Дата: 2026-09-05  
Статус: одобрено владельцем 2026-09-06, разрешены реализация и staging rollout

## 1. Цель

Перевести Telegram-бота с постоянного меню и жёсткой анкеты на conversation-first модель:

- клиент пишет своими словами и в любом порядке;
- LLM определяет намерения и извлекает только разрешённые структурированные параметры;
- backend проверяет каталог, слоты, права, согласия и достаточность данных;
- любая реальная create/reschedule/cancel-операция выполняется только после явного callback-подтверждения;
- кнопки появляются только для короткого контекстного выбора, Telegram-контакта, consent и подтверждения.

Целевой принцип: **LLM управляет разговором, backend управляет реальностью**.

## 2. Решение по миграции

Полный git-откат к состоянию до Telegram self-booking не выполняется. Он одновременно удалит:

- кнопочный UI и анкетную оркестрацию;
- актуальный YCLIENTS catalog grounding;
- durable inbox/outbox и recovery-исправления;
- ownership, idempotency, STOP/privacy-защиту;
- проверенную семантическую маршрутизацию и её eval-контур.

Выбран **forward cleanup**: сохранить проверенное транзакционное ядро и направленно демонтировать menu/catalog/FSM-слой. Новый слой не накладывается поверх старого.

## 3. Три рассмотренных подхода

### 3.1. Полный rollback

Отклонён: слишком высокий риск регрессий и потери полезных safety-гарантий.

### 3.2. Добавить новый agent поверх coordinator

Отклонён: старые menu, callback и step-переходы останутся в коде, а число конфликтующих источников решения вырастет.

### 3.3. Forward cleanup с расширением текущего роутера

Выбран: минимально меняет проверенные trust boundaries, удаляет основной источник сложности и не требует нового agent framework, storage или queue.

## 4. Пользовательский UX

### 4.1. Первый запуск

- До processing consent сохраняется текущая privacy-форма.
- После consent бот снимает reply keyboard через `ReplyKeyboardRemove` и отправляет короткое приглашение писать своими словами.
- Повторный `/start` также снимает legacy reply keyboard.
- Persistent-меню и постоянной кнопки «Записаться» нет.
- Приветствие даёт два текстовых примера, но не имитирует меню.

### 4.2. Обычный диалог

- Цены, длительность, адрес, режим, подбор процедуры, подготовка и передача человеку начинаются только из текста.
- Бот использует уже названные параметры и спрашивает только то, без чего нельзя продолжить.
- Вопрос во время записи не закрывает черновик; ответ не должен навязывать немедленное продолжение оформления.
- Явное изменение услуги, даты, времени или специалиста заменяет ранее собранное значение и инвалидирует зависимые поля.

### 4.3. Контекстные кнопки

Кнопки допустимы только для:

- consent/privacy;
- выбора из не более трёх однозначно подписанных услуг или записей;
- выбора из 2–3 ближайших реальных слотов, соответствующих запросу;
- Telegram `request_contact`;
- подтверждения create/reschedule/cancel;
- явного выбора передачи администратору, если такой выбор нужен по контексту.

Полные каталоги, категории, пагинация, кнопочный выбор специалиста и календарь не возвращаются.

## 5. Целевой контракт роутера

Текущий `RouteDecision` расширяется, а не заменяется новым framework. Strict JSON содержит:

- `route`: основной маршрут;
- `action`: `none`, `create`, `view`, `reschedule`, `cancel`, `cancel_draft`, `continue` или `clarify` в допустимой для маршрута комбинации;
- `topics`: массив из `price`, `duration`, `description`, `preparation`, `contraindications`, `address`, `hours`, `staff` для multi-intent-ответа;
- `services`: 0–3 названия/описания услуг без provider ID;
- `date`: точная дата `YYYY-MM-DD` или `null`;
- `time_from` и `time_to`: границы `HH:MM` или `null`;
- `staff`: имя/предпочтение без provider ID или `null`;
- `choice`: индекс из последнего короткого набора предложений или `null`;
- `confidence`: конечное число 0–1.

Роутер не возвращает имя, телефон, YCLIENTS ID, цену, слот или команду на mutation. PII остаётся за текущей masked security-границей.

Дата, время и индекс строго валидируются. «После 18:00» нормализуется в `time_from=18:00`; «до 15:00» — в `time_to=15:00`; точный интервал заполняет оба поля. Неточные «после работы» и «ближе к вечеру» не превращаются в выдуманные часы: бот уточняет границу.

## 6. Состояние записи

`booking_scenarios` и уникальное ограничение одного открытого сценария на клиента сохраняются. Новая таблица или migration для runtime-state не нужны.

Вместо жёсткого `step` как главного диспетчера backend хранит черновик с независимыми полями:

- валидированные service/staff ID и публичные названия;
- дата и time window;
- выбранный слот;
- имя, телефон и processing-consent marker;
- последний короткий набор вариантов;
- данные для create/reschedule/cancel, уже используемые `BookingService`.

После каждого semantic turn backend мержит только валидные новые поля, инвалидирует зависимые значения и сам вычисляет следующее недостающее условие. LLM не управляет `phase`, provider ID, idempotency key и переходом в `executing`.

Если в booking-запросе указано несколько услуг, бот отвечает на информационную часть и уточняет, какую одну услугу оформить сейчас; он не создаёт несколько YCLIENTS-операций и не угадывает приоритет.

## 7. Data flow

1. Webhook проверяет Telegram secret, private chat, deletion fence, pause и consent; text/contact/callback попадают в текущий durable inbox.
2. Worker выполняет scripts-first STOP и входную security-проверку.
3. Contact и callback идут в узкий детерминированный handler; обычный text всегда идёт в semantic router после security.
4. Router получает masked current message, bounded conversation context и bounded draft state и возвращает strict decision.
5. Для consultation backend подгружает только нужные актуальные данные и основная LLM формулирует единый ответ.
6. Для booking backend мержит валидные поля в черновик, запрашивает YCLIENTS только при достаточных усовиях и возвращает typed outcome: answer, clarify, offer choices, request contact или confirm.
7. Основная LLM формулирует естественный текст в границах typed outcome. Кнопки строит backend, не LLM.
8. Outbound сохраняет порядок, privacy fence и idempotency. YCLIENTS mutation остаётся внутри существующего `BookingService`.

## 8. Что удаляется из runtime

- `main_menu_options` и все его вызовы;
- `persistent_menu_command`, `_MENU_BOOK`, `_MENU_LABELS`;
- menu-aware splitting буфера и menu-specific STOP/fallback-ветки worker;
- catalog states/callback: `catalog_category`, `catalog_service`, `catalog_book`;
- `_start_catalog`, `_recover_catalog_root`, `_catalog_choice`, catalog pagination и render-ветки;
- полный кнопочный список услуг, специалистов и дат;
- ответы с предложением «воспользуйтесь кнопками меню»;
- tests, fixtures и helpers, которые защищают только удаляемый UI.

Доставка reply/inline markup в `moroz.messaging.telegram` остаётся, потому что она нужна consent, contact и контекстным callback.

## 9. Ошибки и fallback

- Сбой или invalid output роутера никогда не создаёт и не меняет запись. Бот просит переформулировать запрос или предлагает связь с администратором без меню.
- Низкая уверенность приводит к одному короткому уточнению, а не к запуску wizard.
- Неоднозначная услуга даёт короткий выбор или текстовое уточнение; backend не угадывает provider ID.
- Недоступный или устаревший каталог блокирует цену и booking-решение, но не общую консультацию из утверждённой базы знаний.
- Занятый слот не повторяет mutation: backend получает свежие слоты и просит новый выбор.
- Unknown outcome, late change, ownership mismatch и недоступность YCLIENTS сохраняют текущий fail-closed/эскалационный контракт.
- Старый callback из menu/wizard-истории не восстанавливает сценарий и не выполняет mutation; ответ просит описать желание текстом.

## 10. Переход с legacy UI

- Production launch ещё не было, поэтом массовая миграция пользователей не нужна.
- Перед staging rollout read-only gate проверяет отсутствие `executing` booking scenario.
- Открытые на staging legacy-черновики `collecting` и `awaiting_confirmation` закрываются одной явной транзакцией с безличным `error_code=ui_migration`; подтверждённые записи не меняются.
- Скрипт/команда cutover сначала показывает count целевых строк, а затем выполняет тот же exact predicate после явного rollout-разрешения.
- Первая staging-приёмка начинается с `/start`, чтобы Telegram снял legacy reply keyboard.

## 11. Тестирование и эвалы

### 11.1. Router V3

Существующие immutable router datasets не переписываются. Добавляется отдельный Router V3 dataset и additive seed migration для текущей админки эвалов. Набор покрывает:

- свободные формулировки всех основных маршрутов;
- данные записи в любом порядке и в одной фразе;
- точные time windows и неточные пожелания;
- multi-intent: цена + запись, описание + длительность, адрес + режим;
- коррекции услуги/даты/времени/специалиста;
- отрицание записи, cancel draft и отличие от отмены реальной записи;
- просмотр, перенос и отмену только своих записей;
- короткие context-dependent follow-up;
- ошибочную раскладку, низкую уверенность, prompt injection и невалидный JSON.

Router V3 имеет отдельные critical cases для mutation, ownership, negation и time-window. Локальные fake-provider tests обязаны быть зелёными; платный live LLM eval запускается только после отдельного разрешения.

### 11.2. Runtime tests

- unit: strict router schema/parser, merge/invalidation черновика, time-window filtering, callback version/ownership;
- integration: один активный черновик, replay, deletion, retention, STOP и восстановление после сбоя;
- E2E: свободное create/view/reschedule/cancel, вопрос внутри черновика, коррекция параметра, ambiguous service, нет слотов, stale callback, duplicate update и unknown outcome;
- regression: consent, security, catalog grounding, messaging delivery, scheduler, admin и YCLIENTS adapter/service;
- полный Docker suite, Ruff, compileall, Compose config, Alembic single head и review.

Тесты полного menu/catalog wizard удаляются вместе с ним. Safety-инварианты из этих тестов переносятся в короткие tests нового flow, а не исчезают вместе с UI.

## 12. Критерии готовности

- После `/start` и consent нет persistent reply keyboard.
- Любой разрешённый security-контуром обычный текст проходит один semantic router; scripts-first STOP/privacy и medical escalation сохраняют свой приоритет, а menu-регулярок и menu-bypass нет.
- Данные записи принимаются в любом порядке; бот не спрашивает уже известное.
- «После 18:00», «до 15:00» и точный интервал фильтруют реальные слоты и отражаются в ответе.
- Цена + запись и другие multi-intent-запросы не теряют ни ответ, ни черновик.
- Вопрос внутри записи не проваливает пользователя обратно в wizard и не сбрасывает данные.
- Кнопки показывают только актуальные короткие варианты или подтверждение.
- Текст, stale callback, router fallback и retry не выполняют mutation.
- Create/reschedule/cancel по-прежнему проходят ownership, consent, slot recheck, idempotency и confirmation-гарантии.
- При неуспехе бот понятно уточняет или эскалирует, но не ссылается на удалённое меню.
- Итоговый runtime + tests diff от базы до cleanup имеет отрицательный net LOC; отчёт отдельно показывает удалённые UI-ветки и сохранённые safety-гарантии.

## 13. Границы текущей работы

В scope:

- свободные create/view/reschedule/cancel в Telegram;
- консультации и multi-intent;
- демонтаж legacy persistent/menu/catalog wizard;
- Router V3 contract, dataset, admin eval seed и automated tests;
- Docker-верификация и подготовка staging-кандидата.

Вне scope:

- оплата, рассылки, новые каналы и Mini App;
- запись на несколько услуг одной YCLIENTS-операцией;
- управление записями, созданными вне этого бота;
- новый agent framework, tool-calling SDK, queue, runtime service, storage или таблица;
- GitHub push, production rollout, live paid LLM eval и неоговорённые YCLIENTS mutation. Staging rollout отдельно разрешён владельцем 2026-09-06.
