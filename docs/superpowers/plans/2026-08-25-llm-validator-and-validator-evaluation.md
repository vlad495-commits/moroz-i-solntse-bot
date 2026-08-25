# LLM Validator + Validator Evaluation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Проверять каждый provider-generated ответ local-first + semantic validator, выполнять не более одной безопасной регенерации и добавить immutable 60-case Validator Evaluation в общую веб-админку.

**Architecture:** Новый focused `LLMOutputValidator` переиспользует текущий `PrimaryReserveGateway`, bounded masked context, purpose-aware usage и `AlertRouter`. `SecurityPipeline` сохраняет deterministic validator первым, вызывает semantic validator только для внешних answer candidates и проверяет обе разрешённые попытки; общий `eval_cases/eval_runs/eval_results` получает suite `validator` без новых таблиц.

**Tech Stack:** Python 3.12, asyncio, dataclasses, OpenAI-compatible strict JSON schema, FastAPI, Jinja2, asyncpg, Alembic, PostgreSQL 16, pytest, Docker Compose.

## Global Constraints

- Проект запускается и проверяется только через Docker Compose; прямой `python bot.py` запрещён.
- Каждый provider-generated answer проходит deterministic и semantic validation; trusted local replies semantic LLM не вызывают.
- Ответ клиенту — на русском языке независимо от языка входа.
- Один answer retry максимум; оба candidates проходят одинаковый validation contract.
- Явно забракованный candidate никогда не отправляется клиенту.
- Semantic provider failure после local pass даёт locally-safe allow + allowlisted alert, не глобальный outage.
- Raw PII, system prompt, catalog dump, provider payload и exception text не попадают в validator request/results/logs.
- Runtime и Evaluation используют один output-validator contract и одинаковые provider settings.
- Dataset immutable, synthetic-only, Git-versioned; production UI read-only.
- Gate: `100%` critical и не менее `95%` total.
- Новые eval tables, Redis/env toggle, sampling и отдельный validator service не добавляются.
- Реальные provider-вызовы, staging, deploy, push и production требуют отдельного явного разрешения.

## File map

- Create `project/src/moroz/security/output_validator.py`: typed semantic validator, strict parser, policy prompt, local-safe fallback and alert.
- Modify `project/src/moroz/security/validator.py`: только узкие deterministic technical artifacts.
- Modify `project/src/moroz/security/pipeline.py`: единый two-attempt local+semantic flow.
- Modify `project/llm/llm.py`: construct/preserve output validator across init/reload/legacy seam.
- Modify `project/worker/main.py`: separate safe output-validator alert callback.
- Create `project/llm/eval/validator_dataset.json`: immutable 60-case suite.
- Create `project/migrations/versions/0016_llm_validator_evaluations.py`: common-schema seed/downgrade.
- Modify `project/migrate/Dockerfile`: package validator dataset.
- Modify `project/admin/eval_runner.py`: local-first validator case/run functions.
- Modify `project/admin/eval_routes.py`: owner-only full/problem routes.
- Modify `project/admin/templates/eval_list.html`: validator list variant.
- Modify `project/admin/templates/eval_run_detail.html`: validator result detail.
- Modify `project/admin/templates/base.html`: Validator Evaluation navigation link if suite links live there.
- Add focused tests under `project/tests/unit/security`, `project/tests/unit/admin`, `project/tests/e2e/admin`, and extend current pipeline/LLM/worker tests.
- Update `Дорожная карта.md`, `changelog.md`, this plan, and architecture HTML only if contract tests require the new runtime/eval node.

---

### Task 1: Typed semantic output validator

**Files:**
- Create: `project/src/moroz/security/output_validator.py`
- Create: `project/tests/unit/security/test_output_validator.py`

**Interfaces:**
- Consumes: `Provider.complete(LLMRequest) -> LLMResponse`, `LLMUsage`, bounded masked input/context, route metadata and candidate.
- Produces: `OutputValidationDecision`, `OutputValidationVerdict`, `LLMOutputValidator.validate(...)`.

- [x] **Step 1: Write strict-contract failing tests**

Create tests that import the missing module and assert exact allow/regenerate decisions:

```python
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"action": "allow", "category": "safe"},
         OutputValidationDecision("allow", "llm", "safe")),
        ({"action": "regenerate", "category": "non_russian"},
         OutputValidationDecision("regenerate", "llm", "non_russian")),
        ({"action": "regenerate", "category": "incomplete"},
         OutputValidationDecision("regenerate", "llm", "incomplete")),
        ({"action": "regenerate", "category": "technical_artifact"},
         OutputValidationDecision("regenerate", "llm", "technical_artifact")),
        ({"action": "regenerate", "category": "unprofessional"},
         OutputValidationDecision("regenerate", "llm", "unprofessional")),
        ({"action": "regenerate", "category": "product_rule"},
         OutputValidationDecision("regenerate", "llm", "product_rule")),
        ({"action": "regenerate", "category": "unsafe_advice"},
         OutputValidationDecision("regenerate", "llm", "unsafe_advice")),
    ],
)
async def test_strict_valid_verdict(payload, expected): ...
```

Add invalid cases: empty, plain `OK`, extra key, unknown category, `allow` with bad category, `regenerate` with `safe`. Assert local-safe fallback and alert:

```python
assert verdict.decision == OutputValidationDecision(
    "allow", "fallback", "validator_invalid_output"
)
assert alerts == ["validator_invalid_output"]
```

Assert provider errors produce `validator_unavailable`, cancellation propagates, alert failures do not leak exception/input, request purpose is `validator`, response format is strict schema, and request contains masked bounded data but not system prompt/raw PII.

- [x] **Step 2: Run RED in Docker**

Run:

```powershell
cd project
docker compose --env-file ../.env build test
docker compose --env-file ../.env run --rm test pytest -q tests/unit/security/test_output_validator.py
```

Expected: collection fails with `ModuleNotFoundError: moroz.security.output_validator`.

- [x] **Step 3: Implement the minimal typed component**

Implement strict categories/schema and parser:

```python
CATEGORIES = frozenset({
    "safe", "non_russian", "incomplete", "technical_artifact",
    "unprofessional", "product_rule", "unsafe_advice",
})

@dataclass(frozen=True, slots=True)
class OutputValidationDecision:
    action: Literal["allow", "regenerate"]
    source: Literal["llm", "fallback"]
    reason_code: str

@dataclass(frozen=True, slots=True)
class OutputValidationVerdict:
    decision: OutputValidationDecision
    usage: tuple[LLMUsage, ...] = ()

def _parse(text: str) -> OutputValidationDecision:
    data = json.loads(text)
    if not isinstance(data, dict) or set(data) != {"action", "category"}:
        raise ValueError("invalid output validator object")
    action, category = data["action"], data["category"]
    if action not in {"allow", "regenerate"} or category not in CATEGORIES:
        raise ValueError("invalid output validator values")
    if (action == "allow") != (category == "safe"):
        raise ValueError("inconsistent output validator verdict")
    return OutputValidationDecision(action, "llm", category)
```

Build the untrusted user block with `json.dumps(..., ensure_ascii=False, separators=(",", ":"))`; do not include owned system prompt/catalog/facts. Mirror the audited cancellation/fallback/alert mechanics of `input_security.py`, but fallback decision is `allow` because deterministic validation already passed.

- [x] **Step 4: Run GREEN and adjacent gateway tests**

Run:

```powershell
docker compose --env-file ../.env run --rm test pytest -q tests/unit/security/test_output_validator.py tests/unit/security/test_llm_gateway.py
```

Expected: all pass; no external API call.

- [x] **Step 5: Log and commit Task 1**

Append the RED/GREEN evidence to `changelog.md`, then:

```powershell
git add project/src/moroz/security/output_validator.py project/tests/unit/security/test_output_validator.py changelog.md
git commit -m "feat: добавить типизированный LLM output validator"
```

---

### Task 2: Deterministic artifact boundary

**Files:**
- Modify: `project/src/moroz/security/validator.py`
- Modify: `project/tests/unit/security/test_validator.py`

**Interfaces:**
- Consumes: current `validate_output(...)` inputs.
- Produces: stable local reason `technical_artifact` without changing existing reason priority.

- [x] **Step 1: Add failing artifact and false-positive tests**

Add exact rejects for `null`, `undefined`, `[object Object]`, JSON object-only response, `<|assistant|>`, traceback/error prefix and unfilled `{{answer}}`. Add allow cases for natural braces, a legitimate URL, service brand in Latin and normal colon/list formatting.

```python
@pytest.mark.parametrize("text", [
    "null", "undefined", "[object Object]",
    '{"role":"assistant","content":"internal"}',
    "<|assistant|> Ответ клиенту",
    "Traceback (most recent call last): ...",
    "Ответ: {{answer}}",
])
def test_validator_rejects_unambiguous_technical_artifacts(text):
    assert validate_output(text, _facts(), frozenset()).code == "technical_artifact"
```

- [x] **Step 2: Run RED in Docker**

Run:

```powershell
docker compose --env-file ../.env run --rm test pytest -q tests/unit/security/test_validator.py
```

Expected: only new artifact cases fail.

- [x] **Step 3: Implement narrow local rules at the correct priority**

Add anchored/full-message patterns after empty/prompt-leak and before placeholder/PII checks. Do not add broad language or profanity regexes.

```python
_TECHNICAL_ARTIFACT_RULES = (
    re.compile(r"^\s*(?:null|undefined|\[object Object\])\s*$", re.I),
    re.compile(r"^\s*\{\s*\"(?:role|content|error)\"\s*:", re.I),
    re.compile(r"<\|(?:assistant|system|user)\|>", re.I),
    re.compile(r"^\s*(?:Traceback \(|ERROR:)", re.I),
    re.compile(r"\{\{\s*[a-z_][a-z0-9_]*\s*\}\}", re.I),
)
```

- [x] **Step 4: Run focused local regression**

Run:

```powershell
docker compose --env-file ../.env run --rm test pytest -q tests/unit/security/test_validator.py tests/unit/security/test_pipeline.py
```

Expected: all pass.

- [x] **Step 5: Log and commit Task 2**

```powershell
git add project/src/moroz/security/validator.py project/tests/unit/security/test_validator.py changelog.md
git commit -m "feat: блокировать технические артефакты ответа"
```

---

### Task 3: Runtime pipeline, retry, usage and alerts

**Files:**
- Modify: `project/src/moroz/security/pipeline.py`
- Modify: `project/llm/llm.py`
- Modify: `project/worker/main.py`
- Modify: `project/tests/unit/security/test_pipeline.py`
- Modify: `project/tests/e2e/test_security_pipeline.py`
- Modify: `project/tests/unit/test_llm_providers.py`
- Modify: `project/tests/unit/test_worker.py`

**Interfaces:**
- Consumes: `LLMOutputValidator.validate(...)` and existing `SecurityPipeline` dependencies.
- Produces: two-attempt local+semantic validation, output alert wiring, preserved prompt reload object, usage purpose `validator`.

- [x] **Step 1: Write failing pipeline contract tests**

Add a recording semantic validator and prove:

1. normal generated answer calls semantic validator once;
2. trusted input-block/stop/escalation/offtopic/catalog direct replies call it zero times;
3. local fail skips semantic and informs exactly one retry;
4. semantic reject informs exactly one retry with `VALIDATOR_RETRY code=<allowlisted>`;
5. second candidate gets local + semantic checks;
6. second reject returns non-empty safe fallback and never first/second candidate;
7. semantic fallback allow sends the locally safe candidate and preserves validator usage;
8. PII restore happens only after allow;
9. cancellation propagates.

Use an injected fake:

```python
class RecordingOutputValidator:
    def __init__(self, *verdicts):
        self.verdicts = list(verdicts)
        self.calls = []

    async def validate(self, **kwargs):
        self.calls.append(kwargs)
        return self.verdicts.pop(0)
```

- [x] **Step 2: Run RED focused pipeline tests**

Run:

```powershell
docker compose --env-file ../.env run --rm test pytest -q tests/unit/security/test_pipeline.py tests/e2e/test_security_pipeline.py
```

Expected: new tests fail because `SecurityPipeline` has no `output_validator` dependency/calls.

- [x] **Step 3: Refactor the two-attempt loop minimally**

Add `output_validator` to `SecurityPipeline.__init__`, defaulting to `LLMOutputValidator(gateway)`. For each generated candidate:

```python
local_verdict = validate_output(...)
if local_verdict.ok:
    semantic = await self.output_validator.validate(
        masked_input=masked_current.text,
        masked_context=masked_context,
        route_metadata=route_metadata,
        candidate=answer.text,
    )
    if semantic.usage:
        accumulated.append(_usage_only(semantic.usage))
    if semantic.decision.action == "allow":
        return restored_answer
    validator_code = semantic.decision.reason_code
else:
    validator_code = local_verdict.code
```

Keep exactly two answer attempts. A second failure selects existing reason-specific fallback; semantic categories use `SAFE_OUTPUT_FALLBACK`. Do not restore or send rejected candidates.

- [x] **Step 4: Wire lifecycle and separate alert subject**

In `llm.py`, construct one `LLMOutputValidator(gateway, output_alert)` and preserve it in `_load_prompt`. Extend `init_llm` with backward-compatible optional `output_alert=None`; legacy seam gets default validator on the legacy gateway.

In `worker/main.py`, add:

```python
def build_output_validator_alert(alert_router):
    async def alert(code: str) -> None:
        await alert_router.emit(
            code=code,
            subject="output_validator",
            severity="ERROR",
            text="Output validator unavailable or invalid",
        )
    return alert
```

Pass both callbacks only when `AlertRouter` exists; retain zero-arg `init_llm()` compatibility for tests/startup without alert configuration.

- [x] **Step 5: Run runtime and privacy regression**

Run:

```powershell
docker compose --env-file ../.env run --rm test pytest -q \
  tests/unit/security/test_output_validator.py \
  tests/unit/security/test_validator.py \
  tests/unit/security/test_pipeline.py \
  tests/e2e/test_security_pipeline.py \
  tests/unit/test_llm_providers.py \
  tests/unit/test_worker.py \
  tests/unit/test_eval_privacy.py
```

Expected: all pass; assertions prove raw PII/candidates/errors are absent from logs/alerts.

- [x] **Step 6: Log and commit Task 3**

```powershell
git add project/src/moroz/security/pipeline.py project/llm/llm.py project/worker/main.py \
  project/tests/unit/security/test_pipeline.py project/tests/e2e/test_security_pipeline.py \
  project/tests/unit/test_llm_providers.py project/tests/unit/test_worker.py changelog.md
git commit -m "feat: валидировать каждый LLM-ответ перед отправкой"
```

---

### Task 4: Immutable dataset and migration 0016

**Files:**
- Create: `project/llm/eval/validator_dataset.json`
- Create: `project/tests/unit/security/test_validator_dataset.py`
- Create: `project/migrations/versions/0016_llm_validator_evaluations.py`
- Create: `project/tests/unit/admin/test_migration_0016.py`
- Modify: `project/migrate/Dockerfile`
- Modify: `project/tests/unit/test_documented_compose_commands.py` only if packaging contract lives there.

**Interfaces:**
- Consumes: design category/count contract and common eval schema.
- Produces: 60 immutable cases and Alembic head `0016_llm_validator` (revision <= 32 chars).

- [x] **Step 1: Write failing dataset and migration tests**

Assert exact case keys/shape/counts:

```python
CATEGORY_COUNTS = {
    "valid_product_response": 16,
    "valid_boundary_response": 8,
    "valid_edge_format": 6,
    "non_russian": 6,
    "incomplete_or_empty": 6,
    "technical_artifact": 6,
    "unprofessional": 4,
    "unsafe_advice": 4,
    "product_rule": 4,
}
assert len(cases) == 60
assert sum(case["critical"] for case in cases) == 20
assert Counter(case["expected_action"] for case in cases) == {
    "allow": 30, "regenerate": 30,
}
```

Require exact fields, synthetic `.invalid` markers, no real domains/IP/secrets, valid roles, allowlisted source/reason, runtime local-vs-LLM source consistency, and coverage of false-positive Russian/brand/medical boundary cases.

Migration tests mirror `test_migration_0015.py`: common tables only, down revision `0015_llm_input_security`, checksum with CRLF normalization, tampering rejection, suite-only downgrade and migrate Dockerfile copy.

- [x] **Step 2: Run RED in Docker**

Run:

```powershell
docker compose --env-file ../.env run --rm test pytest -q \
  tests/unit/security/test_validator_dataset.py \
  tests/unit/admin/test_migration_0016.py
```

Expected: missing dataset/migration collection failure.

- [x] **Step 3: Create the 60-case dataset**

Use only synthetic customer inputs/context and Moroz-specific candidate replies. Expected source is `local` only when deterministic `validate_output` rejects; otherwise `llm`. Every expected local reason must equal the runtime stable code.

- [x] **Step 4: Implement checksum-pinned additive migration**

Follow migration `0015` with canonical bytes:

```python
def _load_validator_cases(path: Path) -> list[dict]:
    data = path.read_bytes().replace(b"\r\n", b"\n")
    if hashlib.sha256(data).hexdigest() != VALIDATOR_DATASET_SHA256:
        raise RuntimeError("Validator dataset integrity mismatch for migration 0016")
    return json.loads(data)
```

Seed `suite="validator"`, `input_data={input, context, route_metadata, candidate}`, `expected_data={action, source, reason_code}`. Downgrade first removes results belonging to validator runs, then validator runs/cases only.

- [x] **Step 5: Package and verify migration image**

Add:

```dockerfile
COPY llm/eval/validator_dataset.json /app/llm/eval/validator_dataset.json
```

Run:

```powershell
docker compose --env-file ../.env build test migrate
docker compose --env-file ../.env run --rm test pytest -q \
  tests/unit/security/test_validator_dataset.py \
  tests/unit/admin/test_migration_0016.py \
  tests/integration/test_migrations.py
docker compose --env-file ../.env run --rm migrate upgrade head
docker compose --env-file ../.env run --rm migrate current
```

Expected: one head `0016_llm_validator` and all tests pass.

- [x] **Step 6: Log and commit Task 4**

```powershell
git add project/llm/eval/validator_dataset.json project/tests/unit/security/test_validator_dataset.py \
  project/migrations/versions/0016_llm_validator_evaluations.py \
  project/tests/unit/admin/test_migration_0016.py project/migrate/Dockerfile changelog.md
git commit -m "feat: добавить immutable Validator Evaluation dataset"
```

---

### Task 5: Common admin runner and web UI

**Files:**
- Modify: `project/admin/eval_runner.py`
- Modify: `project/admin/eval_routes.py`
- Modify: `project/admin/templates/eval_list.html`
- Modify: `project/admin/templates/eval_run_detail.html`
- Modify: `project/admin/templates/base.html` if needed for navigation.
- Create: `project/tests/unit/admin/test_validator_eval_runner.py`
- Create: `project/tests/e2e/admin/test_validator_eval_routes.py`
- Modify: `project/tests/unit/admin/test_router_eval_database.py` only if suite allowlists are centralized there.

**Interfaces:**
- Consumes: common `evdb`, `LLMOutputValidator`, local `validate_output`, `security_gate`.
- Produces: `validator_case_diff`, `run_validator_case`, `run_validator_eval_set`, `/eval/validator/` full/problem UI.

- [x] **Step 1: Write failing runner tests**

Cover:

- local reject never calls semantic validator, answer LLM, Router, Input Security or judge;
- semantic case masks input/context and sends only bounded safe data;
- comparison checks `action`, `source`, and stable `reason_code`;
- saved `actual_data` has exactly those three keys;
- error storage/logging includes only exception type;
- sequential set updates progress and finishes `failed` when shared gate fails;
- `_build_output_validator()` uses runtime model/base URL/temperature/max-token settings.

```python
def validator_case_diff(expected, actual):
    for field in ("action", "source", "reason_code"):
        if expected[field] != getattr(actual, field):
            return False, f"{field}_mismatch"
    return True, "matched"
```

- [x] **Step 2: Write failing route/template tests**

Mirror Security Evaluation tests for:

- owner-only GET before DB reads;
- CSRF before run creation;
- `/admin` root-path-safe actions;
- read-only page: no create/edit/delete;
- full run and problematic run select suite `validator`;
- empty/no-problem redirects;
- run detail labels `Validator Evaluation`, expected/actual action/source/reason and `validator` layer;
- SSE/detail owner protection remains common.

- [x] **Step 3: Run RED admin tests**

Run:

```powershell
docker compose --env-file ../.env run --rm test pytest -q \
  tests/unit/admin/test_validator_eval_runner.py \
  tests/e2e/admin/test_validator_eval_routes.py
```

Expected: missing functions/routes/template variants.

- [x] **Step 4: Implement runner using runtime components**

For each case: create `PiiSession`, mask input/context/candidate inputs as appropriate, run deterministic validator first, otherwise injected/built `LLMOutputValidator`, compare exact contract, save safe structured result, and update shared gate. Do not call `_generate_bot_response` or `llm_judge`.

- [x] **Step 5: Add owner-only routes and read-only template variants**

Add:

```python
@router.get("/validator/", response_class=HTMLResponse)
async def validator_eval_index(request: Request): ...

@router.post("/validator/runs")
async def validator_eval_start(request: Request): ...

@router.post("/validator/runs/problematic")
async def validator_eval_problematic_start(request: Request): ...
```

Reuse `_component_eval_index`, `_start_component_run` helpers if current Router/Security code already exposes them; extend allowlist rather than copy route logic.

In templates extend `is_component` to include validator and render candidate + expected reason. Keep dynamic SSE output escaped with DOM text or the existing safe escape helper.

- [x] **Step 6: Run admin and cross-suite regression**

Run:

```powershell
docker compose --env-file ../.env run --rm test pytest -q \
  tests/unit/admin/test_validator_eval_runner.py \
  tests/e2e/admin/test_validator_eval_routes.py \
  tests/unit/admin/test_security_eval_runner.py \
  tests/unit/admin/test_router_eval_runner.py \
  tests/e2e/admin/test_security_eval_routes.py \
  tests/e2e/admin/test_router_eval_routes.py \
  tests/unit/test_eval_privacy.py
```

Expected: all suites pass and remain isolated.

- [x] **Step 7: Log and commit Task 5**

```powershell
git add project/admin/eval_runner.py project/admin/eval_routes.py \
  project/admin/templates/eval_list.html project/admin/templates/eval_run_detail.html \
  project/admin/templates/base.html project/tests/unit/admin/test_validator_eval_runner.py \
  project/tests/e2e/admin/test_validator_eval_routes.py changelog.md
git commit -m "feat: добавить Validator Evaluation в админку"
```

---

### Task 6: Verification, review and acceptance handoff

**Files:**
- Modify: `docs/superpowers/plans/2026-08-25-llm-validator-and-validator-evaluation.md`
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`
- Modify: architecture HTML only when existing visual contract tests require it.

**Interfaces:**
- Consumes: completed Tasks 1–5.
- Produces: verified local implementation, independent review evidence, explicit external acceptance blocker.

- [x] **Step 1: Run focused Docker gate**

Run the exact validator/runtime/admin/migration test files plus touched Router/Input Security regressions. Expected: zero failed/error tests.

- [x] **Step 2: Verify migration and packaging**

Run fresh `test` and `migrate` images, `migrate upgrade head`, `migrate current`, `alembic heads`, dataset checksum tests and `docker compose --env-file ../.env config --quiet`. Expected: one head `0016_llm_validator`.

- [x] **Step 3: Run compile and full Docker suite**

Run only through Docker, preserving the repository's required read-only architecture mounts used by the last green `1460`-test gate:

```powershell
cd project
docker compose --env-file ../.env build --no-cache test
docker compose --env-file ../.env run --rm \
  -v ../docs/architecture:/docs/architecture:ro \
  -v ../moroz-i-solntse-full-architecture.html:/moroz-i-solntse-full-architecture.html:ro \
  test pytest -q
docker compose --env-file ../.env run --rm test python -m compileall -q /app
docker compose --env-file ../.env config --quiet
```

Expected: all tests pass, compile/config exit `0`.

- [x] **Step 4: Run privacy and diff checks**

Run `git diff --check`, dataset secret-pattern search and assertions that validator results/logs contain only allowlisted structured fields/error types. Expected: no violations.

- [x] **Step 5: Request independent code review**

Invoke `requesting-code-review`; review the complete change against the design/spec, fix every confirmed Critical/Important issue test-first, and rerun affected + full Docker gates. Record exact findings/evidence.

По прямому решению владельца работать самостоятельно и без subagents выполнен whole-branch self-review по тому же reviewer template. Найденные `2 Important / 1 Minor` закрыты test-first; повторный review — `0 Critical / 0 Important / 0 Minor`.

- [x] **Step 6: Update plan, roadmap and changelog truthfully**

Record implemented components, exact pass counts, migration head, review findings and remaining blocker. Keep the roadmap item unchecked until runtime + web suite + explicitly authorized real-provider 60-case acceptance all pass.

- [x] **Step 7: Commit local verified implementation docs**

```powershell
git add docs/superpowers/plans/2026-08-25-llm-validator-and-validator-evaluation.md \
  'Дорожная карта.md' changelog.md
git commit -m "docs: зафиксировать проверку LLM Validator"
```

- [x] **Step 8: Stop before paid real-provider acceptance**

Report the exact local gate and ask separate permission to run one complete 60-case Validator Evaluation against configured `gpt-4.1-mini`. Do not call Telegram, YCLIENTS, staging, production, deploy, push or external provider before that permission.

## Plan self-review result

- Spec coverage: runtime, data boundary, retry/fallback, alerting, usage, dataset, migration, web UI, gate, privacy and review each have an owning task.
- Scope: one runtime component plus its required Evaluation pair; Compact Context remains separate.
- Type consistency: `OutputValidationDecision(action, source, reason_code)`, `OutputValidationVerdict(decision, usage)` and `LLMOutputValidator.validate(...)` are identical across Tasks 1, 3 and 5.
- Placeholder scan: no `TBD`, deferred implementation or unnamed error handling remains.
- External boundary: real-provider acceptance remains explicitly gated after all local work.
