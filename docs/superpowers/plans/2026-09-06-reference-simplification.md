# План реализации доказательного аудита Lucky Hair

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. В этой задаче выбран последовательный режим в текущем чате: отдельные агенты не требуются.

**Goal:** приблизить архитектуру Moroz к подтверждённым принципам Lucky Hair, убрав дублирующие консультационные механизмы и сохранив надёжную запись через YCLIENTS.

**Architecture:** основной LLM отвечает по одному ручному промпту; Router выбирает маршрут, а booking coordinator выполняет операции с реальным провайдером. Существующие Security, Validator, Compact и durable delivery сохраняются. По последующему решению владельца admin editor и hot reload удаляются: файл промпта применяется при запуске worker. Критерий успеха: каждый оставленный механизм нужен конкретному клиентскому сценарию или обязательной гарантии, а более простой вариант референса применён либо отклонён по проверяемой причине. Число полей, строк и потребителей — вспомогательные сведения, не доказательство необходимости или успеха.

**Tech Stack:** Python 3.12, aiogram 3.x, существующий LLM gateway, PostgreSQL, Redis, RabbitMQ, Docker Compose, pytest. Новые библиотеки и сервисы приложения не нужны.

## Global Constraints

- Источник решений: [доказательный аудит](<../../audits/Аудит упрощения архитектуры по Lucky Hair 2026-09-06.md>), §§2–8. Контракт первого пакета: [spec](../specs/2026-09-06-manual-consultation-knowledge-design.md). Живой статус ведётся в [дорожной карте](<../../../Дорожная карта.md>).
- Канонический Lucky Hair: commit `5398f909829f5db1b5052087f5a826c2bbcd5244` в `D:/AI_OS/30_Sources/Automation_Courses/01_Raw_Materials/2026-08-13_lucky_hair_llm_chatbot_reference/project-edu-public`. Читать через `git show`, не грязный working tree.
- Основание локального runtime аудита — `82205a5`; на момент планирования HEAD `069145d`, последующие изменения до текущей плановой правки документные. Staging не считать равным локальному HEAD.
- Ответы Светы не блокируют реализацию. Использовать v1.8-draft.2; спорные цены и доступность не достраивать. Владелец уже отправил [вопросы](<../../../Вопросы Свете.md>).
- Не возвращать постоянное меню, не делать rollback свободного диалога, не заменять YCLIENTS локальными записями референса.
- Сохранять ownership, consent, явное подтверждение, live-проверку слота, идемпотентность, unknown outcome, STOP/privacy, durable inbox/outbox.
- Все исполнения проекта только Docker. Тестовая инфраструктура отдельная, без host ports и доступа к рабочим БД/Redis; один другой номер Redis DB не заменяет изоляцию экземпляра.
- Paid LLM, Telegram/YCLIENTS mutations, push и rollout — только по отдельному разрешению. Локальные fake-provider тесты входят в реализацию.
- Новые расходники только в корневом `tmp/`. Перед переносом кандидата проверить его наличие; если утрачен, восстановить по сохранённым материалам с повторной сверкой, не подставлять старый укороченный вариант.
- Рабочие изменения пользователя сохранять. Коммитить явный список своих файлов после каждого законченного тестового цикла. Логировать действия в `changelog.md`.

## Что уже сделано и не выполняется повторно

| Результат | Доказательство | Как используется |
|---|---|---|
| Удалены постоянное меню и большой wizard, реализован свободный сбор draft | Runtime `82205a5`, roadmap, предыдущий план свободного диалога | Это исходное состояние, не задача данного плана |
| Выполнено сравнение с Lucky Hair | Аудит 06.09 и канонический commit | Не повторять общий аудит с нуля |
| Получен серверный каталог 76 услуг / 9 категорий | Снимок 06.09 06:00:18 МСК | Разовое основание прайса, не новый runtime-источник консультации |
| Проверены материалы и подготовлен кандидат | Аудит знаний, v1.8-draft.2 | Перенести при реализации пакета A |
| Подготовлены и отправлены вопросы Свете | Файл вопросов, сообщение владельца | Ответы — последующая правка содержимого |
| При исходном планировании были editor/history/reload/rollback | Прежние admin/prompt_routes.py и llm/llm.py | Требование отменено владельцем; удалить UI/API/reload по задаче 5, сохранить файл и историю БД |

Отметки выполнения находятся у задач ниже, текущий статус — в дорожной карте. Предыдущие `2344 passed` не являются проверкой нового кандидата.

## Очередь всего аудита

| Этап | Конкретный результат | Условие перехода |
|---|---|---|
| A | Один консультационный путь без catalog grounding, работающий ручной прайс, сохранённая запись | Задачи 1–6 ниже и свежий локальный gate |
| B | Карта оставшихся aliases/topics и сборки mixed reply; удаление только доказанных дублей | Задача 7; любое изменение публичного Router-контракта — отдельная узкая spec |
| C | Проверенный вход/возврат в запись и обоснованная частота технического sync | Задача 8; текущие значения сохраняются, если упрощение не даёт доказанного выигрыша |
| Release | Исправлены релизные блокеры, пройдена приёмка точного кандидата | Задача 9; отдельное разрешение на staging |

Архитектурная цель достигнута не после одного промпта: по каждому отличию из §2 аудита должно быть решение «упрощено / сохранено после сравнения альтернатив», с проверкой соответствующего пользовательского пути. Обоснование «поле читает существующий код» недостаточно: проверить, нельзя ли упростить сам этот код вместе с полем.

## Карта файлов пакета A

| Файл | Ответственность / планируемое изменение |
|---|---|
| `project/worker/main.py` | Убрать resolver консультационного каталога и его передачу LLM; сохранить booking coordinator и markup |
| `project/llm/llm.py` | Убрать параметр catalog и reload listener; сохранить согласованную загрузку prompt/facts при старте |
| `project/src/moroz/security/pipeline.py` | Убрать catalog block/direct reply/facts merge; сохранить ответ действия при сбое answer LLM |
| `project/src/moroz/security/validator.py` | Ограниченно поддержать расчёты минут из ручного тарифа, не ослабляя остальные проверки |
| `project/admin/eval_runner.py` | Тот же prompt-only контракт, без альтернативного catalog аргумента |
| `project/src/moroz/booking/catalog.py` | После анализа потребителей удалить только consultation-only части; оставить технический lookup |
| `project/llm/prompts/system.md` | Перенести согласованный временный кандидат с неизвестными условиями, без старого catalog блока |
| `project/tests/unit/security/test_pipeline.py`, `test_validator.py`, `test_system_prompt_catalog.py`, `test_eval_catalog.py` | Заменить прежние ожидания grounding новыми контрактами, сохранить защитные проверки |
| `project/tests/unit/test_worker.py`, `project/tests/e2e/test_catalog_message_flow.py` | Проверить отсутствие ground у консультации и сохранение booking |
| `project/tests/unit/test_prompt_file_only.py`, `test_active_sanitization.py` | Загрузка prompt/facts и смена тарифа; старые reload tests удаляются вместе с механизмом |
| `project/docker-compose.audit-test.yml` | Создать только тестовый override из задачи 1 |

## Задача 1 — безопасный тестовый контур

**Files:** создать `project/docker-compose.audit-test.yml`; проверить `project/Dockerfile.test`, `project/tests/integration/conftest.py`, `project/tests/e2e/test_security_pipeline.py`. Не менять рабочий `.env`.

**Выполнено 06.09:** baseline 56 passed; integration каталога 9 passed. Одноразовый env хранится в root tmp/audit-test.env; рабочий .env не копируется. Запуск с -w /workspace и --rootdir=/workspace обязателен для загрузки общих fixtures. Миграции LF проходят.

**Interfaces:** вход — текущий checkout; выход — отдельный Compose project `moroz-reference-test`, в котором test использует исключительно test postgres/redis/rabbitmq. Во всех командах ниже рабочая папка `project/`.

- [x] Прочитать `using-git-worktrees`, подготовить отдельный `codex/reference-simplification` checkout по правилам навыка. Перенести файл кандидата из root tmp исходного checkout в root tmp рабочего через apply_patch. Проверить `git status`, исходную ветку/commit, не перезаписать пользовательские изменения.
- [x] Создать override (пароль ниже — только для одноразовой локальной тестовой сети без опубликованных портов):

```yaml
services:
  test:
    environment:
      DATABASE_URL: postgresql://audit_test:audit_test@postgres:5432/audit_test
      POSTGRES_USER: audit_test
      POSTGRES_PASSWORD: audit_test
      POSTGRES_DB: audit_test
      REDIS_URL: redis://:audit_test@redis:6379/0
      RABBITMQ_URL: amqp://audit_test:audit_test@rabbitmq:5672/
      PYTHONPATH: /workspace:/workspace/src:/workspace/llm:/workspace/admin
  postgres:
    environment:
      POSTGRES_USER: audit_test
      POSTGRES_PASSWORD: audit_test
      POSTGRES_DB: audit_test
  redis:
    environment:
      REDIS_PASSWORD: audit_test
  rabbitmq:
    environment:
      RABBITMQ_DEFAULT_USER: audit_test
      RABBITMQ_DEFAULT_PASS: audit_test
```

- [x] Проверить `docker compose --env-file ../tmp/audit-test.env -p moroz-reference-test -f docker-compose.yml -f docker-compose.audit-test.yml config --quiet`. Не выводить полный config с рабочими секретами. Проверить выбранные endpoints test и labels создаваемых volumes/network; ни один не должен быть external или принадлежать рабочему контуру.
- [x] Запустить только инфраструктуру тестового проекта:

```powershell
docker compose --env-file ../tmp/audit-test.env -p moroz-reference-test -f docker-compose.yml -f docker-compose.audit-test.yml up -d postgres redis rabbitmq
docker compose --env-file ../tmp/audit-test.env -p moroz-reference-test -f docker-compose.yml -f docker-compose.audit-test.yml run --rm --build -w /workspace test pytest --rootdir=/workspace -q -p no:cacheprovider /workspace/tests/unit/security/test_pipeline.py
```

Ожидание: baseline результатов сохранён; существующее падение не приписывается новой правке. Не запускать bot/worker/admin/scheduler этого Compose project.

- [x] До integration/E2E проверить известный checksum-блокер Windows из побочного аудита. Если он воспроизводится, исправить воспроизводимость LF отдельным коммитом с проверкой checksum; не менять принятую запись checksum в существующей БД и не скрывать сбой skip-маркером. Unit-работу можно продолжать, но общий gate остаётся незакрытым.
- [x] Коммит `test: isolate reference simplification checks`; log и roadmap. Очистка — только `down` точно этого Compose project, без `-v` по умолчанию и без удаления рабочего Redis.

## Задача 2 — заменить консультационный путь одним LLM

**Files:** worker/main.py, llm/llm.py, security/pipeline.py, admin/eval_runner.py, prompts/system.md и unit/e2e файлы из карты.

**Interfaces:** `SecurityPipeline.respond(user_message, context, *, recent_message_count=1, dispatch=None, booking_context=None)` — без `catalog`; аналогично удалить `catalog` из `generate_response`, `_generate_bot_response`, `run_case`. Возвращаемый `LLMResponse` не меняется. `dispatch` и `booking_context` сохраняются.

- [x] Добавить в `test_pipeline.py` контракт отсутствия consultation API:

```python
def test_pipeline_has_no_catalog_argument():
    import inspect
    assert "catalog" not in inspect.signature(SecurityPipeline.respond).parameters

@pytest.mark.asyncio
async def test_consultation_uses_owned_prompt_and_answer_gateway():
    gateway = CapturingGateway("Солярий — 42 ₽ за минуту.")
    owned = "Солярий — 42 ₽ за минуту."
    from moroz.security.validator import extract_structured_facts
    subject = SecurityPipeline(gateway, owned, extract_structured_facts(owned))
    result = await subject.respond("Сколько стоит солярий?", [])
    answers = [r for r in gateway.requests if r.purpose == "answer"]
    assert len(answers) == 1
    assert owned in answers[0].messages[0]["content"]
    assert "UNTRUSTED_CATALOG_DATA" not in answers[0].messages[0]["content"]
    assert result.text == "Солярий — 42 ₽ за минуту."
```

- [x] Запустить команду test из задачи 1 с `pytest -q /workspace/tests/unit/security/test_pipeline.py -k 'no_catalog_argument or owned_prompt'`. Ожидание до изменения: signature test FAIL, после — оба PASS.
- [x] В pipeline удалить ветку от `catalog_block = ""` до сборки `owned_system`, включая `direct_reply`. Сборка должна остаться:

```python
active_facts = self.facts
owned_system = "\n\n".join(
    part for part in (self.system_prompt, route_metadata) if part
)
```

Удалить параметр и больше не передавать его из всех трёх callers. В worker удалить вложенный `resolve_catalog` и добавление `llm_options["catalog"]`; оставить `recent_message_count`, booking routing context и dispatch. Удалить неиспользуемые imports только после поиска потребителей.

- [x] Перенести полный v1.8-draft.2 в `project/llm/prompts/system.md` через apply_patch. В шапке отличить локальную реализацию от deployment; сохранить 16 разделов, canary, 12 описаний и оговорки. Не объявлять ответы Светы полученными.
- [x] В worker-тестах использовать repository fake, чей `ground` вызывает `AssertionError("consultation must not query catalog")`; обычная консультация должна дойти до answer gateway, booking — до технического `list_services`. Удалить только ожидания `catalog-local`/вызова ground, не сами privacy/ownership сценарии.
- [x] В `test_eval_catalog.py` и `test_system_prompt_catalog.py` заменить прежние ожидания каталога проверками нового интерфейса и отсутствия `UNTRUSTED_CATALOG_DATA`. Сохранить проверки контактов, неизвестных цен и canary.
- [x] Запустить unit/security, unit/test_worker.py и e2e/test_catalog_message_flow.py через изолированный test. Ожидание: PASS; при отказе DB gate результат не считать полным.
- [x] Коммит `refactor: use manual prompt for consultation`; перечислить изменённые callers и удалённые ветки в changelog.

**Evidence задачи 2 (2026-09-06):** база diff `74b12fb`; RED нового API-контракта — 1 failed / 1 passed, после удаления catalog — 2 passed. Единый целевой gate — **691 passed in 82.17s**, exit 0; Compose config и git diff --check — exit 0. Read-only reviewer не нашёл важных runtime-дефектов; его пробел покрытия закрыт E2E настоящего coordinator/list_services с запрещённым ground, draft/markup и create_calls=0. Кандидат побайтово сравнен после нормализации переводов строк: отличается только статусом локального применения. Нет изменений booking-кода, миграций или Router-контракта. Paid LLM, live Telegram/YCLIENTS и rollout не выполнялись; общий suite задачи 6 ещё не запускался.

Точная команда gate (из project/ рабочего дерева):

```powershell
docker compose --env-file ../tmp/audit-test.env -p moroz-reference-test -f docker-compose.yml -f docker-compose.audit-test.yml run --rm --build -w /workspace test pytest --rootdir=/workspace -q -p no:cacheprovider /workspace/tests/unit/security /workspace/tests/unit/test_eval_privacy.py /workspace/tests/unit/test_worker.py /workspace/tests/e2e/test_catalog_message_flow.py /workspace/tests/unit/booking/test_catalog_matching.py /workspace/tests/unit/booking/test_catalog_sync.py /workspace/tests/integration/booking/test_catalog_lookup.py /workspace/tests/integration/booking/test_catalog_projection.py /workspace/tests/contract/booking/test_yclients_catalog.py
```

## Задача 3 — расчёт минут без глобального разрешения новых цен

**Files:** validator.py, test_validator.py, pipeline.py, prompts/system.md.

**Interfaces:** сохранить `extract_structured_facts(*sources, slots=())` и `validate_output(...)`. Добавить к `StructuredFacts` поле `minute_rates: tuple[tuple[str, str], ...] = ()` — услуга и Decimal-совместимая строка тарифа. Поле строится только из явно маркированных строк ручного промпта, а не из истории/ответа/YCLIENTS. Имена дополнительных локальных helpers: `_minute_rates(sources)` и `_validated_minute_prices(text, rates)`.

Решение: не добавлять все кратные 42/51 в общий allowlist. Для производной суммы требовать явную связку «длительность + услуга + итог» и проверить произведение через Decimal. Если короткое «10 минут — 420 ₽» невозможно привязать к услуге надёжно, разрешённый формат ответа — «10 минут солярия — 420 ₽». Это всё ещё один короткий ответ, а не таблица. Не использовать Router для назначения медицински допустимого времени.

**Граница сложности:** распознавать один понятный формат тарифа «Солярий — 42 ₽ за минуту» и один формат расчёта «13 минут солярия — 546 ₽» (с нормализацией пробелов, регистра и обычных знаков препинания). Не строить универсальный разборщик русского языка, словесных числительных, скидок, пакетов и произвольного порядка фраз. Для неподдержанного выражения использовать существующую ограниченную повторную генерацию в понятном формате или безопасное уточнение, а не дописывать исключения. Не требовать от собственницы JSON, формул, ID или служебной разметки в промпте. Если этот узкий контракт нельзя реализовать небольшой локальной проверкой без расширения схемы знаний, остановить разрастание и отдельно пересмотреть решение.

- [x] Добавить отдельные параметризованные проверки:

```python
@pytest.mark.parametrize("text,ok", [
    ("13 минут солярия — 546 ₽.", True),
    ("13 минут солярия — 547 ₽.", False),
    ("13 минут коллариума — 546 ₽.", False),
    ("Криокапсула — 546 ₽.", False),
    ("13 минут солярия — 546 ₽. LED-маска — 546 ₽.", False),
])
def test_minute_price_is_bound_to_service_and_occurrence(text, ok):
    facts = extract_structured_facts(
        "Солярий — 42 ₽ за минуту.\nКоллариум — 51 ₽ за минуту."
    )
    assert validate_output(text, facts, frozenset()).ok is ok
```

- [x] Запустить `test_validator.py -k minute_price`; правильный новый расчёт до реализации должен FAIL. Неверные суммы должны оставаться запрещёнными.
- [x] Реализовать парсинг только явных тарифных строк трёх услуг (`солярий`, `коллариум`, `коллагенарий`), без нечётких совпадений и без float. Проверять **каждое вхождение суммы**, а не множество чисел: производное разрешение не должно легализовать вторую такую же цену у другой услуги. Нулевая/отрицательная длительность, дробные минуты без утверждённого формата, неоднозначный тариф, скидка и перепутанная услуга должны давать существующий `invented_price`.

Ядро арифметики в helper:

```python
from decimal import Decimal

def _minute_total(rate: str, minutes: str) -> Decimal | None:
    if not minutes.isascii() or not minutes.isdigit():
        return None
    count = int(minutes)
    if count <= 0:
        return None
    return Decimal(rate.replace(",", ".")) * count
```

Результат используется только для совпавшего span конкретного выражения, не объединяется с `facts.prices`. Старый контроль raw PII, canary, контактов, гарантий и слотов остаётся до/после проверки цены как сейчас. Общий семантический контроль фиксированных цен не объявлять реализованным этим helper.

- [x] Добавить тесты: тариф 43 вместо 42 → 13 минут = 559; удалённый тариф → 546 запрещено; сумма из истории не разрешается; 5 и 10 минут двух услуг проверяются отдельно. Согласовать примеры промпта с форматом, понятным validator. Проверить неподдержанную формулировку: ограниченный retry/fallback, без разрешения непроверенной суммы и без нового парсера.
- [x] Запустить test_validator.py, test_pipeline.py и test_output_validator.py. Коммит `fix: validate minute totals against owned tariffs` только при зелёном gate.

**Evidence задачи 3 (2026-09-06):** RED подтверждён до реализации; review-дефект с границей «руб.» воспроизведён (2 failed / 24 passed) и исправлен. Docker gate: test_validator.py + test_pipeline.py + test_output_validator.py + test_system_prompt_catalog.py — **188 passed in 3.53s**. Decimal, тарифы только из owned prompt; повтор одинаковой суммы у другой услуги запрещён. Примеры не содержат дублирующих фиксированных итогов; business-сведения сохранены. Последующее решение владельца заменило проверку editor/reload задачей 5 — удалением механизма и проверкой загрузки файла.

## Задача 4 — не терять действие при сбое консультации

**Files:** pipeline.py, test_pipeline.py, unit/test_worker.py, существующие booking E2E.

**Interfaces:** `_combine_reply(answer: str, local_reply: str | None) -> str` уже существует; не создавать новый composer. Local reply сформирован trusted dispatch, а worker отдельно сохраняет его markup.

- [x] Расширить mixed booking test двумя ошибками gateway: `LLMUnavailable` и `NonRetryableLLMError`. Проверить, что ответ содержит прежний local next step и что gateway outage не вызывает повторный dispatch/мутацию. Использовать существующие fake gateway/router/dispatch из test_pipeline.py.
- [x] Запустить `test_pipeline.py -k 'unavailable or nonretryable or dispatch'`: новый тест должен воспроизвести потерю local reply.
- [x] В существующей ветке исключения выполнить точечную замену:

```python
except (LLMUnavailable, NonRetryableLLMError):
    return _aggregate(
        accumulated,
        _combine_reply(SAFE_OUTPUT_FALLBACK, local_reply),
        "security-fallback",
    )
```

- [x] Проверить mixed ответы в worker: «цена + запись», «подготовка внутри draft», ошибка answer LLM после подготовки подтверждения. Assert: текст и клавиатура описывают одну операцию, draft сохранён, booking success только из результата backend, повторного provider вызова нет.
- [x] Прогнать unit pipeline/worker и booking E2E в отдельной инфраструктуре. Коммит `fix: preserve booking reply on answer outage`. Это закрывает P2-2 побочного аудита только при соответствующем regression evidence, остальные дефекты не закрывает.

**Evidence задачи 4 (2026-09-06):** unit RED 4 failed; worker/coordinator RED 2 failed / 1 passed. Исправление — одна строка existing _combine_reply. Проверены обе ошибки answer gateway, new/draft/awaiting_confirmation, точное совпадение ответа и markup, сохранение draft id/phase, один dispatch при replay и create_calls=0. Docker gate после пересборки: **664 passed in 290.09s** (unit/security, unit/test_worker.py, e2e/test_catalog_message_flow.py, e2e/booking). Read-only review без critical/important; явный assert awaiting_confirmation после замечания — **3 passed in 14.68s**. Закрывает только P2 сохранности mixed reply; не весь аудит и не live-приёмку. Полный suite пакета A остаётся задачей 6.

Точная команда gate из project/ рабочего дерева:

```powershell
docker compose --env-file ../tmp/audit-test.env -p moroz-reference-test -f docker-compose.yml -f docker-compose.audit-test.yml run --rm --build -w /workspace test pytest --rootdir=/workspace -q -p no:cacheprovider /workspace/tests/unit/security /workspace/tests/unit/test_worker.py /workspace/tests/e2e/test_catalog_message_flow.py /workspace/tests/e2e/booking --tb=short --show-capture=no
```

## Задача 5 — обновление промпта и очистка хвостов

**Изменение требований владельцем 2026-09-06:** редактор админки не нужен. Прежние пункты editor save/rollback/reload UI отменены, не реализовывать их заново. Промпт меняется в файле проекта и применяется при выпуске.

**Files:** admin/app.py, templates/base.html, prompt_routes.py, prompt_database.py, prompt_edit.html, prompt_version.html, docker-compose.yml; admin E2E и active_sanitization. Worker/llm cleanup — следующий отдельный шаг.

- [x] Добавить HTTP-контракты удаления /prompt GET/POST и проверку навигации owner/admin/operator; наблюдать RED перед удалением.
- [x] Удалить регистрацию router, меню, модуль handlers/publisher, приватный CRUD, два шаблона; заменить тесты удалённых функций контрактами 404. Сохранить CSRF/RBAC остальных разделов.
- [x] Сохранить файл промпта и чтение eval_runner; admin volume перевести в ro. Не удалять данные prompt_versions и не править миграции.
- [x] Зафиксировать Docker admin E2E + активную загрузку промпта; локальный коммит удаления редактора.

Evidence удаления редактора: RED 6 failed / 2 passed; промежуточный gate 185 passed / 1 stale navigation assertion, ожидание обновлено. После пересборки test image: **186 passed in 9.60s** — весь e2e/admin, unit/test_active_sanitization.py, unit/test_llm_providers.py, unit/security/test_system_prompt_catalog.py, integration/messaging/test_prompt_reload.py. Compose config и admin prompt read-only mount проверены. Сам prompt и БД не менялись; worker listener пока сохранён для следующего отдельного cleanup.
- [x] Проверить оставшийся worker reload listener/lifecycle по потребителям, удалить неиспользуемое после исчезновения publisher отдельным тестируемым шагом. Проверить файл → prompt+facts при старте и новый тариф без admin editor. Не добавлять новую очередь или механизм доставки промпта.

- [x] Выполнить поиск потребителей:

```powershell
rg -n 'CatalogGrounding|direct_reply|resolve_catalog|catalog_grounding_enabled|YCLIENTS_CATALOG_GROUNDING_ENABLED|merge_structured_facts' worker llm admin src tests
```

Удалять helper/type/import только если после предыдущих задач нет production-потребителей. Оставить list_services, reader, sync, service/staff IDs и provider DTO. Тесты технического каталога сохраняются. Если deployment flag временно оставлен для совместимости, пометить его no-op для консультации и записать причину, не использовать как выключатель booking.
- [x] Прогнать unit/security, unit/booking/test_catalog_boundary.py, test_catalog_sync.py, integration/booking/test_catalog_lookup.py, test_catalog_projection.py. Коммит `refactor: remove unused consultation catalog consumers`.

**Evidence cleanup (2026-09-06):** reload RED 1 failed/1 passed → 79 passed (6.75s); catalog API boundary RED 1 failed. Объединённый gate 627 passed/6 failed выявил ошибки адаптации тестов, они исправлены; повтор всех затронутых файлов — **42 passed (92.03s)**. Review critical/important нет. Удалены listener/ack/lifecycle-hook, ground/match_catalog/output DTO/helpers, merge facts и no-op флаг (Compose/validate_env/examples/PowerShell allowlist). Реальные list_services/DTO/grouping/hourly sync/TTL сохранены. Matcher unit-тесты удалены вместе с API; технические freshness tests перенесены на list_services, human-mode/atomicity/replay/booking E2E сохранены. Это не результат полного suite — он запускается в задаче 6.

## Задача 6 — приёмка пакета A и запись результата

**Files:** тесты выше, дорожная карта, changelog; сохранить evidence в `docs/audits/Приёмка ручных консультаций 2026-09-06.md` (создать при фактической проверке).

- [x] Выполнить весь suite нового checkout в изолированном test container:

```powershell
docker compose --env-file ../tmp/audit-test.env -p moroz-reference-test -f docker-compose.yml -f docker-compose.audit-test.yml run --rm --build -w /workspace test pytest --rootdir=/workspace -q -p no:cacheprovider /workspace/tests
git diff --check
```

Зафиксировать commit, exact command, pass/fail/skip, происхождение кода /workspace, границы fake-provider проверки. Падение из-за checksum не считать pass и не суммировать результаты разных деревьев.
- [x] Пройти матрицу spec: известное/неизвестное, минута/расчёт/сравнение, неверная цена, загрузка файла/отсутствие редактора, mixed draft, LLM outage, YCLIENTS outage, STOP/injection/чужая запись. Для LLM-качества без разрешения paid evals отметить «live не проверено», а не «бот отвечает идеально».
- [x] Проверить структурное доказательство: consultation не вызывает ground, каждый простой разрешённый FAQ идёт в answer LLM, каталог не входит в его prompt, booking по-прежнему использует list_services и живой слот. До/после перечислить удалённые ветки и сохранённые границы.
- [x] Использовать requesting-code-review/verification-before-completion по правилам навыков на стадии реализации. Исправить найденные дефекты, повторить затронутые проверки.
- [x] Обновить roadmap и changelog; локальный коммит результата. Не применять промпт/код на staging автоматически. Сообщить владельцу границы выполненного и перейти к аналитической задаче 7, не запрашивая заново цель проекта.

**Evidence задачи 6:** полный suite c283fdd — 2334 passed/3 failed (2517.08s); все три document-contract failures исправлены. Повтор двух файлов — 16 passed (1.75s); весь unit suite — 1296 passed (47.17s), review Critical/Important нет. Новый полный повтор после документной коррекции не запускался; production runtime не менялся. Exact команды и границы — в отчёте приёмки. Это локальная проверка, не rollout и не завершение задач 7–9.

## Задача 7 — оставшиеся дубли Router и mixed reply

**Files для чтения:** messaging/router.py, booking/conversation.py, booking/telegram.py, security/pipeline.py, worker/main.py; канонические `project/llm/router.py`, `project/llm/worker.py` референса. Документ результата — дополнение §4 исходного аудита, а не второй общий аудит.

- [ ] Для `services`, legacy `service`, `topics`, `action/choice`, date/time/staff и веток соединения ответа заполнить таблицу: клиентский сценарий → наблюдаемый нужный результат → текущий механизм → более простой вариант референса → конкретная потеря/выигрыш → проверка → решение. Карту producer/consumer использовать только для поиска связей и безопасной правки, не как оправдание сохранения.
- [ ] Сопоставить «цена + запись», изменение предпочтений, ответ только датой, continue переноса, consultation внутри draft. Измерить число реально выполняемых LLM вызовов и источников состояния на пути; не использовать длину JSON как метрику.
- [ ] Для используемого поля также проверить упрощение его потребителя или устранение всей ветки. Например, дата/время оправданы не чтением в conversation.py, а сценарием «запиши завтра после 18:00» без повторного ввода уже названного. Сравнить с более простым последовательным сбором референса: лишние уточнения, потеря ограничений, дополнительные LLM-вызовы, число источников состояния и отказоустойчивость. Не считать текущую реализацию единственным способом обеспечить сценарий.
- [ ] Если alias/ветка не обеспечивает нужного сценария или гарантии либо сценарий сохраняется проще, подготовить удаление/замену вместе с потребителем и regression-проверкой. Для изменения Router schema, семантики извлечения или UI сначала оформить отдельную узкую spec/план; наличие согласования не подменяет сравнение альтернатив.
- [ ] Оставлять механизм только при указанном сценарии, рассмотренном более простом варианте и конкретной проверяемой причине его отклонения. Допустимый итог — отсутствие дальнейшего упрощения после такого сравнения, но не по одному списку consumers. Не вводить новый orchestration framework ради схожести с референсом.

## Задача 8 — вход в запись и техническая свежесть

- [ ] Проверить нынешний вход обычным текстом, возврат после FAQ и продолжение с контекстной кнопкой; не возвращать постоянное меню. Зафиксировать конкретные затруднения, а не предполагать необходимость новой кнопки.
- [ ] Если текущий UX удовлетворяет сценариям, сохранить. Изменение кнопки или последовательности отдельно показать владельцу перед кодом.
- [ ] Подтвердить существующие sync 1 час и TTL 24 часа по коду. Описать последствия пропущенного sync, изменения услуги и недоступности провайдера. Консультация уже не зависит от этого snapshot.
- [ ] По умолчанию сохранить текущую частоту. Любое изменение требует согласованного SLA свежести и согласованного TTL с запасом на сбой; нельзя просто включить суточный sync при TTL 24 часа. Не менять расписание на сервере в аналитической задаче.

## Задача 9 — релизные условия и завершение общей цели

- [ ] Сверить открытые дефекты побочного аудита: изоляция Redis, checksum Windows, kind при continue, фазы переноса, незавершённый выбор специалиста, доступ к четвёртой записи. Они не являются основанием переписать архитектуру целиком, но релизные блокеры нельзя забыть. Для оставшихся — отдельные исправления с regression-тестами до rollout.
- [ ] Свести различия референса из §2 аудита: Router, dispatch, консультация, запись, UI, вопросы внутри записи, перенос/отмена, редактор, надёжность. Для каждого сохранённого механизма привести сценарий/гарантию, рассмотренную более простую альтернативу и доказательство конкретной потери при её применении. Ни число удалённых строк, ни наличие вызова в коде не закрывают этот критерий.
- [ ] Представить владельцу проверенный exact кандидат, оставшиеся бизнес-неизвестные и результаты live-проверок либо их отсутствие. Только после отдельного разрешения использовать deploy/manual-qa навыки и staging rollout с backup owner prompt, rollback и проверкой runtime.
- [ ] Согласованные ответы Светы вносить отдельными content-коммитами через тот же ручной источник, с повторной проверкой цен/составов. Отсутствие ответа не разрешает обещать спорные услуги.

## Проверка полноты плана

| Требование spec | Задачи |
|---|---|
| Единый источник, сохранность содержания, отсутствие автоматического прайса | 2, 5, 6 |
| Поминутные цены и проверка произведений | 3, 5, 6 |
| Все consultation callers и admin runner | 2, 5 |
| Reload/ack/rollback, пара prompt+facts, owner protection | 5 |
| Mixed reply, сохранность draft и fallback | 4, 6 |
| Технический каталог, TTL, YCLIENTS guarantees | 2, 5, 6, 8 |
| Последующие этапы исходного аудита | 7–9 |
| Изоляция, известные блокеры, отдельный rollout | 1, 6, 9 |

Выполненные пункты помечены выше; весь аудит не завершён. Текущий статус и следующий шаг — в дорожной карте. Режим — последовательно в текущем чате; после каждого логического результата обновлять roadmap. Повторно спрашивать исходный фронт работ не требуется.
