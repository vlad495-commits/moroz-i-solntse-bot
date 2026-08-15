# YCLIENTS Catalog Grounding — Design

## Решение

YCLIENTS становится единственным источником изменяемых сведений о записываемых услугах: названий, категорий, цен, длительности и доступных мастеров. Существующий worker раз в час получает bounded read-only снимок booking-каталога, полностью проверяет его и атомарно заменяет одну локальную PostgreSQL-проекцию.

Ответ строится гибридно. Однозначный простой вопрос о цене, длительности или мастере получает детерминированный ответ без LLM. Для сравнения и объяснения несколько подходящих строк каталога передаются в обычный answer-вызов LLM без отдельного catalog-classifier вызова; действующие guard/retry правила сохраняются. Output validator разрешает только цены из выбранных строк. Числовые цены удаляются из системного промпта.

## Пользовательская ценность

- изменение услуги или цены в YCLIENTS автоматически доходит до бота не позднее следующей часовой синхронизации;
- простой ценовой вопрос получает быстрый точный ответ без расхода LLM-токенов;
- сложный вопрос сохраняет естественный ответ LLM, но цена и длительность опираются на каталог;
- краткий сбой YCLIENTS не останавливает FAQ: используется последний успешный снимок;
- после 24 часов без успешного снимка бот перестаёт подтверждать изменяемые каталоговые факты и предлагает администратора;
- сотруднику не нужно дублировать цены в системном промпте или отдельном редакторе админки.

## Рассмотренные подходы

1. **Live API-вызов на каждое сообщение.** Отклонён: latency и доступность YCLIENTS попадут в критический путь ответа, а одинаковые вопросы создадут повторные provider-вызовы.
2. **Автоматически переписывать системный промпт.** Отклонён: изменяемые данные смешиваются с инструкциями, растёт контекст, усложняются reload/rollback и проверка выдуманных цен.
3. **Одна локальная PostgreSQL-проекция — выбранный вариант.** Она обновляется в фоне существующими scheduler/worker и читается внутри текущего message transaction.

## Ponytail-граница

Переиспользуем `YclientsConfig`, `YclientsHttpClient`, текущий rate limiter, read-only endpoints `book_services`/`book_staff`, `scheduler_jobs`, RabbitMQ task routing, worker, scheduler, PostgreSQL advisory lock, input guard, FAQ router, PII session, LLM gateway, output validator и существующую message/outbox transaction.

Добавляется ровно одна таблица. Не добавляются новый контейнер, очередь, dependency, Redis-кэш, vector search, embeddings, отдельный LLM-классификатор, admin editor или ручная кнопка refresh.

## Границы первой фазы

### Входит

- только услуги, доступные для онлайн-записи через YCLIENTS;
- название и категория услуги;
- цена или диапазон цены конкретного мастера;
- длительность услуги у конкретного мастера;
- имя доступного мастера;
- автоматический полный снимок раз в час;
- детерминированные ответы на однозначные вопросы;
- bounded catalog context для сложного LLM-ответа;
- динамическая allowlist цен в output validator;
- fail-closed поведение при неоднозначности, отсутствии и устаревании;
- удаление числовых цен из системного промпта;
- unit, contract, PostgreSQL integration, worker/scheduler и end-to-end tests.

### Не входит

- абонементы, сертификаты, депозиты, товары, акции и платежи как отдельные финансовые сущности;
- live availability конкретного времени: она остаётся в booking-сценарии;
- редактирование каталога или ручной refresh из admin;
- свободное fuzzy-сопоставление, embeddings и vector database;
- descriptions и произвольные provider-поля в LLM-контексте;
- staging/production rollout и реальные YCLIENTS-вызовы во время разработки.

Если абонемент или пакет заведён как обычная записываемая услуга, он попадает в каталог на общих основаниях.

## Provider read contract

Один часовой снимок использует booking API без user auth:

```text
GET /api/v1/book_staff/{company_id}?without_seances=1
GET /api/v1/book_services/{company_id}?staff_id=<staff_id>
```

Первый запрос возвращает доступных для записи мастеров. Для каждого принятого мастера выполняется один запрос услуг с `staff_id`, чтобы получить фактическую цену и длительность этого мастера. Это не входит в путь клиентского сообщения.

Safety bounds:

- не более 100 мастеров;
- не более 200 услуг на мастера;
- не более 5 000 уникальных пар `(service_id, staff_id)`;
- provider IDs — канонические положительные integers длиной не более 64 цифр;
- display text очищается от управляющих символов и ограничивается 200 символами;
- длительность — целое число секунд, кратное 60, от 1 до 1 440 минут;
- цена — конечное неотрицательное decimal-значение не более `99 999 999.99` RUB;
- `price_min <= price_max`;
- повтор пары `(service_id, staff_id)` отклоняет весь снимок.

Принимается только HTTP 200, `success=true` и ожидаемая data-shape. Транспортная ошибка, иной status, malformed envelope, превышение bound или malformed обязательное поле отклоняют весь снимок без изменения базы. Raw response body не логируется и не сохраняется.

Категория допускается пустой. Пустой корректный снимок допустим и считается успешным. Provider descriptions, images, phones, emails и произвольные metadata игнорируются.

## PostgreSQL-проекция

Миграция `0011_yclients_service_catalog` создаёт одну таблицу:

```text
yclients_service_catalog
  service_id        text          NOT NULL
  staff_id          text          NOT NULL
  service_name      text          NOT NULL
  category_name     text          NULL
  staff_name        text          NOT NULL
  price_min         numeric(10,2) NOT NULL
  price_max         numeric(10,2) NOT NULL
  duration_minutes  integer       NOT NULL
  synced_at         timestamptz   NOT NULL
  PRIMARY KEY (service_id, staff_id)
```

CHECK constraints повторяют числовые границы. Нормализованный поиск выполняется по bounded снимку в Python после одного SELECT, поэтому trigram/full-text индекс не нужен.

Таблица хранит только последний успешный снимок. История цен не создаётся: YCLIENTS остаётся источником истины, а аудит изменений каталога не заявлен.

## Атомарная синхронизация

1. Job пытается получить session advisory lock `yclients_service_catalog:v1`.
2. Если lock занят, job завершается `skipped` до provider-вызова.
3. Следующий часовой job планируется до чтения provider, чтобы ошибка не остановила цикл.
4. Под lock reader получает и полностью валидирует bounded снимок в памяти.
5. Одна PostgreSQL-транзакция удаляет старые строки и batch-вставляет новый снимок с единым `synced_at`.
6. Commit публикует снимок целиком; rollback сохраняет предыдущую версию.
7. Lock освобождается в `finally` на normal и exceptional paths.

Job contract:

```text
kind = yclients_service_catalog_sync
bucket = UTC hour start
idempotency_key = yclients_service_catalog_sync:<bucket ISO8601>
```

При старте настроенный worker обеспечивает job текущего часового bucket. Дальше существующий scheduler публикует его в текущую очередь. Новая очередь и отдельный polling-loop не создаются.

Allowlisted error codes: `yclients_catalog_transport`, `yclients_catalog_http_status`, `yclients_catalog_response_shape`, `yclients_catalog_bound`, `yclients_catalog_write`. Незнакомая ошибка сохраняется только как имя локального exception type по действующему worker contract. Display-поля и provider body не попадают в job payload, error code или лог.

## Свежесть

Для непустого каталога authoritative timestamp — общий `MAX(synced_at)`. Для корректного пустого снимка используется `finished_at` последней успешной `yclients_service_catalog_sync` job. `skipped`, `failed`, `pending` и `claimed` не считаются успешным снимком.

- возраст до 24 часов включительно: снимок разрешён;
- старше 24 часов или успешного снимка нет: изменяемые факты запрещены;
- ошибка синхронизации не очищает последний успешный снимок;
- freshness вычисляется по переданному aware UTC `now`.

При stale/no-snapshot ценовой или мастер-вопрос получает детерминированный ответ: бот не может надёжно подтвердить актуальные данные и предлагает администратора. Неизвестная цена не заменяется данными системного промпта.

## Поиск и неоднозначность

Поиск не использует отдельную LLM-классификацию. Нормализация: Unicode casefold, `ё` → `е`, только буквенно-цифровые токены; короткие токены не участвуют в оценке, кроме полного совпадения. Service/staff IDs не показываются и не принимаются как пользовательские идентификаторы.

Кандидаты группируются по услуге, варианты мастеров остаются внутри услуги. Ранжирование использует exact normalized phrase и пересечение токенов. Максимум пять услуг передаётся дальше.

- один уверенный кандидат + простой вопрос о цене/длительности/мастере → детерминированный шаблон;
- несколько равных кандидатов → просьба выбрать из максимум пяти названий;
- нет кандидатов → каталог не придумывает услугу; ценовой вопрос получает уточнение;
- сложный сравнительный вопрос → максимум пять услуг как catalog context для обычного answer-вызова без дополнительной catalog-классификации.

Matcher fail closed: сомнительное совпадение никогда не превращается в уверенную цену.

## Порядок security pipeline

```text
input guard -> stop/medical escalation -> PII masking -> route -> catalog decision
            -> deterministic reply OR one LLM answer -> output validation -> restore PII
```

Worker читает каталог в той же PostgreSQL-транзакции, где фиксирует message/outbox, и передаёт immutable `CatalogGrounding` в `generate_response`. Решение об ответе принимает `SecurityPipeline` только после input guard; каталог не обходит jailbreak/medical/stop правила.

В LLM передаётся отдельный system data block максимум из пяти услуг. Он содержит только очищенные названия, категории, имена мастеров, decimal-цены и длительность. Provider description и raw JSON отсутствуют. Блок явно объявлен недоверенными данными, а не инструкциями.

Для выбранного блока строится `StructuredFacts` и объединяется с неизменяемыми contact/slot facts. Цена вне выбранного catalog context получает существующий `unverified_price` и один validator retry; повторное нарушение даёт safe fallback.

Детерминированные ответы строятся кодом из Decimal/integer и очищенных строк и также проходят `validate_output` с catalog facts.

## Форматы ответа

Одна цена:

```text
«Криотерапия» — 1 500 ₽, длительность 3 минуты. Доступные специалисты: Анна, Мария.
```

Диапазон:

```text
«Массаж лица» стоит от 2 000 до 2 500 ₽, длительность — 30–45 минут.
У Анны — 2 000 ₽, у Марии — 2 500 ₽.
```

Неоднозначность:

```text
Уточните, пожалуйста, какую услугу вы имеете в виду: «Криотерапия», «Локальный криомассаж» или «Криосауна»?
```

Stale/no snapshot:

```text
Сейчас не могу надёжно подтвердить актуальную стоимость или список специалистов. Пожалуйста, уточните у администратора.
```

Порядок детерминирован: нормализованное имя, затем provider ID как скрытый tie-breaker.

## Системный промпт и контекст

Из `project/llm/prompts/system.md` удаляются числовые цены и указания считать их источником истины. Остаются роль, тон, описание процедур, противопоказания, контакты и правила эскалации. Добавляется правило: изменяемые цена, длительность и доступные специалисты берутся только из catalog data block; при его отсутствии значения не угадываются.

В `messages` сохраняется фактический ответ бота, поэтому следующий ход видит уже отправленный текст. Catalog block не пишется в историю/Redis отдельным сообщением; на каждом новом сообщении он строится заново из текущего снимка. Старая цена из истории не становится authoritative fact.

## Транзакционность и идемпотентность сообщения

- inbox rows блокируются и проверяются в ingress order;
- human mode проверяется до генерации ответа;
- каталог читается без внешнего API;
- user/assistant history, token usage, durable outbound и inbox completion фиксируются одной существующей транзакцией;
- duplicate `process_message` сохраняет текущий idempotency key и не создаёт второй outbound;
- детерминированный ответ записывает нулевое LLM usage через существующую `LLMResponse`.

Синхронизация каталога не пишет клиентские сообщения и не использует outbound transport.

## Ошибки и деградация

- provider/read/validation/write error: job retry по существующей политике, старый снимок остаётся;
- lock busy: `skipped`, provider не вызывается;
- stale/no snapshot: изменяемые каталоговые факты запрещены;
- неоднозначный запрос: уточнение без LLM;
- LLM недоступен при сложном вопросе: существующий safe fallback;
- validator отклонил цену дважды: safe fallback;
- PostgreSQL недоступен: существующая retry/idempotency политика, неподтверждённый ответ не отправляется.

## Приватность и аудит

Каталог не содержит клиентских данных. Имена мастеров и услуг считаются недоверенными display-данными: очищаются, ограничиваются, не логируются и не исполняются как инструкции.

Новой admin mutation нет, поэтому новые RBAC/CSRF/audit события не требуются. Существующие prompt edit audit и human-mode contracts не меняются. Токены YCLIENTS не попадают в test container, Git, документацию или логи.

## Проверки

TDD должен доказать:

- parser принимает корректные price range/duration/staff данные и отклоняет malformed/bounds/duplicates;
- provider calls используют только GET, partner auth и bounded число запросов;
- атомарная замена сохраняет старый снимок при ошибке и освобождает lock;
- hourly bucket/idempotency не создают дублей, следующий job планируется до чтения;
- empty snapshot, 24h boundary и failed/skipped jobs трактуются корректно;
- matcher ограничивает пять кандидатов и fail closed при неоднозначности;
- input guard выполняется раньше детерминированного ответа;
- simple price/duration/staff question не вызывает LLM;
- complex question не создаёт отдельный catalog-classifier вызов и передаёт в обычный answer-вызов только bounded data; существующие guard/validator retry остаются;
- неразрешённая цена блокируется validator;
- stale/no-snapshot не допускает цену из prompt/history;
- human mode не генерирует catalog/LLM ответ;
- history/outbox/inbox/token usage остаются атомарными и идемпотентными;
- prompt больше не содержит числовой прайс;
- migration/architecture/Compose contracts не добавляют service/queue/dependency;
- полный Docker suite проходит после focused gates.

## Критерии готовности

- одна новая таблица, один scheduler kind, без новых runtime-компонентов;
- синхронизация раз в час и stale threshold ровно 24 часа;
- YCLIENTS — единственный источник изменяемых price/duration/staff facts;
- простой точный вопрос отвечает без LLM;
- сложный вопрос не добавляет отдельный LLM-вызов и использует максимум пять услуг в обычном answer-контексте;
- output validator не пропускает цену вне выбранного каталога;
- сбой синхронизации не разрушает предыдущий снимок;
- нет provider/staging/production вызовов, push или deploy;
- roadmap, changelog, migration proof, свежий полный Docker gate и независимый review закрыты до merge.
