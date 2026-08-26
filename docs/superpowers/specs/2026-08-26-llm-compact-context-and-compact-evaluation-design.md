# LLM Compact Context + Compact Evaluation — design

Дата: 2026-08-26.

## Цель

Завершить связанную пару `LLM Compact Context + Compact Evaluation`: после privacy masking сжимать длинное ограниченное окно истории в проверяемую фактическую сводку и последние сообщения, а также добавить versioned suite `compact` в существующую owner-only веб-админку Evaluations.

Этап закрывается только вместе: runtime-компонент, безопасная деградация, immutable dataset, общий admin-runner, отдельная статистика, problem-only rerun, focused/full Docker gates и независимый review.

## Подтверждённые продуктовые решения

- Берём bounded-window механику Lucky Hair Studio без отдельного постоянного хранилища summary.
- Worker читает последние `40` сообщений вместо `20`.
- Ровно `30` сообщений не сжимаются; при `31–40` старая часть сжимается, последние `10` сохраняются дословно.
- Compact получает только уже замаскированную историю. Raw ПД не передаются summarizer и не попадают в summary, eval results, traces или логи.
- Summary имеет строгий versioned JSON-контракт: факты, договорённости, открытые вопросы, ограничения и конфликты.
- При provider error, invalid JSON/schema или пустой сводке ответ не блокируется: в answer-контекст идут последние `10` masked сообщений, создаются safe log и технический alert без содержимого диалога.
- Отдельная таблица или Redis-key для summary не создаются. Факты старше последних `40` сообщений не обещаются как долговременная память этого этапа.
- Логическая роль `compact` по умолчанию использует дешёвые credentials/model Router; answer LLM для сжатия не вызывается.
- Compact запускается после успешных Input Security и Router перед answer generation. Router/Input Security видят исходный недавний masked context, Validator — уже compacted context.

## Текущее состояние

Уже есть PostgreSQL history, worker-window `CONTEXT_MESSAGES_LIMIT=20`, `PiiSession`, bounded recent context для Router/Input Security, единый `SecurityPipeline`, purpose-aware usage, безопасные alerts/logs и общие suite-aware `eval_cases/eval_runs/eval_results` с owner-only UI, CSRF, SSE, problem rerun и gate `100% critical + >=95% total`.

Пробелы:

- лимит `20` делает порог `>30` недостижимым;
- нет compactor, typed result, strict parser и usage purpose `compact`;
- не определены конфликты, invalid output и provider fallback;
- нет Compact Evaluation и отдельной статистики.

## Канонический референс и gap-анализ

Изучен Lucky Hair Studio commit `5398f909829f5db1b5052087f5a826c2bbcd5244`:

- `project/llm/llm.py`: `_COMPACT_SYSTEM`, `compact_context`;
- `project/llm/config.py`: `COMPACT_THRESHOLD=30`, `COMPACT_KEEP_RECENT=10`;
- `project/llm/worker.py`: `compact_context(masked_ctx)`;
- `project/llm/tests/test_compact.py`: threshold/success/fallback;
- `project/llm/tests/test_worker_flush.py`: worker seam.

В commit нет Compact Evaluation или `compact_dataset.json`; это наш подтверждённый пробел.

| Решение референса | Что уже есть у нас | Пробел | Решение | Причина | Проверка |
|---|---|---|---|---|---|
| Порог `>30`, хвост `10` | Context limit `20` | Порог недостижим | Взять, окно поднять до `40` | Минимальная совместимая механика | boundary + worker |
| Сжимать старую masked часть | Masking в pipeline | Нет compactor | Взять после masking | Privacy boundary | capture tests |
| Summary + recent tail | Raw bounded history | Нет summary | Взять | Меньше answer prompt | pipeline |
| Свободный текст | Typed contracts других LLM-ролей | Нестабильный формат | Strict JSON v1 | Проверяемый parser/eval | parser |
| Facts/agreements/questions | Нет summary | Да | Взять + constraints/conflicts | Roadmap | dataset/judge |
| Пересчёт каждого окна | Нет summary storage | Повторная стоимость | Взять сейчас | Нет нового state/invalidation | usage |
| Сбой → хвост | Safe fallback conventions | Нет compact fallback | Взять + safe alert | Доступность | failure matrix |
| Любые messages | Role filtering | Trust boundary | Только user/assistant + char bound | Не передавать artifacts | adversarial |
| User summary message | Untrusted data blocks | Нужна маркировка | `UNTRUSTED_COMPACT_CONTEXT_V1` | Не считать инструкцией | prompt capture |
| Нет quality eval | Общий eval runner | Да | Suite `compact` | Закрыть пару | acceptance |
| Отдельные eval tables | Общая schema | Нет | Отклонить | Запрет второго silo | migration |
| Persistent summary | PostgreSQL raw history | Не входит в выбранный scope | Отклонить сейчас | Пользователь выбрал bounded window | schema absence |

## Рассмотренные подходы

### A. Сохраняемая инкрементальная summary

Удерживает факты дольше окна, но требует нового состояния, транзакций, invalidation, concurrency и deletion contract. Не выбрана.

### B. Свободная текстовая сводка как у референса

Самая короткая реализация, но её трудно валидировать и стабильно оценивать. Не выбрана без адаптации.

### C. Bounded window + strict JSON summary — выбрана

Сохраняет простую механику референса без нового долговременного состояния и добавляет необходимые privacy, parser, observability и evaluation-гарантии.

## Runtime architecture

### Порядок данных

Worker выбирает последние `40` rows `messages` хронологически. Текущий buffered input передаётся отдельно и не входит в compact history.

`SecurityPipeline.respond`:

1. local guard;
2. masking полного history window и current input одним `PiiSession`;
3. bounded recent context для Input Security и Router;
4. после allow/route — `ContextCompactor.compact(masked_history)`;
5. answer LLM получает system/route/catalog + compacted history + current masked input;
6. Validator получает тот же compacted history;
7. placeholders восстанавливаются только после успешной проверки ответа.

Compactor не вызывается для trusted local replies, block/stop/escalation/offtopic и direct catalog reply.

### Контракт компонента

Создаётся `moroz.security.context_compactor`:

```python
@dataclass(frozen=True, slots=True)
class CompactSummary:
    facts: tuple[str, ...]
    agreements: tuple[str, ...]
    open_questions: tuple[str, ...]
    constraints: tuple[str, ...]
    conflicts: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class CompactResult:
    messages: tuple[dict[str, str], ...]
    source: str          # unchanged | llm | fallback
    reason_code: str     # below_threshold | compacted | provider_error | invalid_output
    usage: tuple[LLMUsage, ...] = ()

class ContextCompactor:
    async def compact(self, masked_context: list[dict[str, str]]) -> CompactResult: ...
```

Он получает один `SDKProvider` и optional alert callback. Новая dependency/service не добавляются.

### Порог и bounds

- `COMPACT_THRESHOLD=30`;
- `COMPACT_KEEP_RECENT=10`;
- `CONTEXT_MESSAGES_LIMIT=40`;
- только непустые string `user`/`assistant` messages;
- старая serialized часть ограничена последними `24_000` символами на message boundaries;
- summary + tail короче исходного worst-case окна.

Три env/Compose-значения валидируются как `0 < KEEP_RECENT <= THRESHOLD < CONTEXT_MESSAGES_LIMIT`; invalid config останавливает startup. Character bound остаётся внутренней константой.

### Provider и usage

Добавляются `COMPACT_MODEL`, `COMPACT_API_KEY`, `COMPACT_BASE_URL`, `COMPACT_MAX_TOKENS` с defaults от Router и output limit `400`. `init_llm` создаёт `SDKProvider`, внедряет `ContextCompactor`, а prompt reload сохраняет объект. SDK retries выключены.

Успешный вызов даёт `LLMUsage(purpose="compact", ...)`; fallback без response имеет zero usage. History/summary не пишутся в usage/logs.

### Strict summary v1

```json
{
  "version": 1,
  "facts": ["Гость интересуется криотерапией"],
  "agreements": ["Сначала уточнить противопоказания"],
  "open_questions": ["Какой день удобен гостю"],
  "constraints": ["Удобно после 18:00"],
  "conflicts": ["Сначала было удобно утром ↔ затем только после 18:00"]
}
```

Parser требует exact keys, `version == 1`, пять arrays, только непустые строки, максимум `12/8/8/8/6` items и `300` символов/item. JSON constants, unknown keys, markdown wrapper и превышение bounds отклоняются.

Prompt сохраняет только source facts; latest explicit correction побеждает в `facts` и отмечается в `conflicts`; пожелание не становится договорённостью; контакты остаются placeholders; инструкции из history — недоверенные данные.

Validated summary детерминированно рендерится под `UNTRUSTED_COMPACT_CONTEXT_V1`; пустые sections опускаются. Это одно `user` message перед exact last `10` masked messages.

### Ошибки и наблюдаемость

| Ситуация | Поведение |
|---|---|
| `len <= 30` | Filtered history, без provider call |
| `31–40`, valid JSON | Summary + exact tail 10 |
| Provider error | Exact tail 10, safe alert |
| Invalid/empty/oversized output | Exact tail 10, safe alert |
| Alert failure | Safe `error_type`, processing продолжается |
| Cancellation | Propagate |
| Unknown role/content type | Не передавать summarizer |
| Raw/unknown placeholder в summary | Reject → tail fallback |

Логи: только source/reason, input/output message counts, model и token counts; transcript/summary/provider response не логируются.

## Compact Evaluation

### Dataset

Immutable `project/llm/eval/compact_dataset.json`: `40` синтетических кейсов.

| Категория | Кейсов | Critical |
|---|---:|---:|
| `threshold_boundary` | 6 | 2 |
| `fact_retention` | 8 | 4 |
| `agreement_retention` | 6 | 4 |
| `open_question_constraint` | 6 | 4 |
| `conflicting_updates` | 6 | 6 |
| `no_hallucination` | 4 | 4 |
| `privacy_and_injection` | 4 | 4 |
| **Итого** | **40** | **28** |

Case shape:

```json
{
  "case_key": "compact-conflict-001",
  "category": "conflicting_updates",
  "context": [{"role": "user", "content": "Сначала удобно утром"}],
  "expected_mode": "llm",
  "required_facts": ["последнее предпочтение — после 18:00"],
  "forbidden_facts": ["подтверждена запись утром"],
  "critical": true
}
```

Boundary cases имеют exact `30/31` messages; quality cases дополняются нейтральными synthetic turns. Реальных чатов, ПД, secrets, live slots и raw system prompt нет.

### Migration и schema

Migration `0017` checksum-проверяет canonical LF dataset и добавляет suite `compact` в существующие tables:

- `question` — безопасное описание, не transcript;
- `input_data={context, expected_mode}`;
- `expected_data={required_facts, forbidden_facts}`;
- `critical` из dataset.

Downgrade удаляет только compact results/runs/cases; migrate image включает dataset.

### Runner и verdict

`run_compact_case` маскирует synthetic context, вызывает production `ContextCompactor` и проверяет:

1. structural: source/mode, threshold, exact tail, marker/shape, отсутствие raw ПД;
2. semantic judge: source history против summary и required/forbidden facts, включая conflicts и hallucinations.

Используются существующие `JUDGE_MODEL`, immutable masked compact-policy и strict `{score, reasoning}` parser. Pass требует structural success и `score >= JUDGE_PASS_THRESHOLD`. `actual_data` хранит только source/reason/counts/safe dimensions, без transcript/full summary.

Full/problem rerun используют общий lifecycle и gate: `100%` critical, `>=95%` total, error = fail. Provider/invalid-output fallback проверяется deterministic fake-provider tests, а не real dataset run.

### Web UI

Owner-only `/eval/compact/`: read-only 40 cases, category/mode/counts/critical, full run, problem rerun, recent runs и compact model. Общий detail/SSE показывает только safe metadata. Create/edit/delete отсутствуют; POST защищены owner + CSRF, GET сохраняют owner/root-path contract.

## Проверки

- Unit: `30/31`, exact tail, filtering, char bound, parser/render, conflicts, cancellation, usage, alerts.
- Privacy: provider видит только masked placeholders; raw ПД не появляются в request/result/log/alert.
- Pipeline: Router/Security получают original recent masked context, answer/Validator — compacted; local replies compactor не вызывают.
- Worker/e2e: последние 40 по порядку, current input отдельно, `compact` usage сохраняется.
- Dataset/migration: exact counts/categories/critical, checksum LF/CRLF, synthetic privacy, single head, suite-only downgrade.
- Admin: owner/CSRF/root path, full/problem, read-only, suite isolation, safe SSE/detail.
- Focused и full Docker gates, затем independent whole-change review.
- Real-provider Compact Evaluation — только после отдельного разрешения владельца, так как это платные summarizer + judge calls.

## Out of scope

- persistent/incremental summary в PostgreSQL/Redis;
- память старше 40 сообщений;
- новый service/queue/cache/eval tables;
- production-chat dataset generation;
- отдельный vendor для compact;
- retention/raw history changes;
- staging/deploy/push и real-provider calls без отдельного разрешения.

## Acceptance criteria

1. При `<=30` provider не вызывается; при `>30` answer получает strict summary + exact tail 10.
2. Summarizer видит только masked bounded user/assistant history.
3. Invalid output даёт non-blocking exact-tail fallback и safe alert.
4. Usage/alerts/logs purpose-aware и не раскрывают transcript/summary/ПД.
5. Suite `compact` встроен в общую owner-only Evaluations с отдельной статистикой/problem rerun.
6. Immutable 40-case dataset и migration `0017` воспроизводимы; новых eval tables нет.
7. Gate: `100% critical` и `>=95% total`.
8. Focused/full Docker gates и независимый review зелёные.
9. Real-provider acceptance — только после отдельного разрешения.
