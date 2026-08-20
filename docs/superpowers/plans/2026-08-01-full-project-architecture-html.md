# Full Project Architecture HTML Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Создать новый автономный `docs/moroz-i-solntse-full-architecture.html` в визуальном стиле референса Lucky Hair Studio, честно разделив реализованный runtime, код без полного live-подтверждения и согласованный backlog.

**Architecture:** Один статичный HTML содержит сравнительную таблицу и четырнадцать последовательных архитектурных секций. CSS повторяет визуальный язык референса: компактные карточки, вертикальные потоки, ромбы решений, контейнерные зоны, веера маршрутов и серый пунктирный backlog; внешний JavaScript и сетевые ресурсы отсутствуют.

**Tech Stack:** семантический HTML5, inline CSS, Python 3.12 stdlib `html.parser`, pytest, Docker Compose.

## Global Constraints

- Источник дизайна: `docs/superpowers/specs/2026-08-01-full-project-architecture-reference-style-design.md`.
- Референс доступен только для чтения: `D:\Downloads\Telegram Desktop\архитектура-проекта-полная.html`.
- Создать новый `docs/moroz-i-solntse-full-architecture.html`; существующий `docs/production-v1-architecture.html` не изменять.
- Цветной блок означает реализацию, подтверждённую кодом и тестом/Compose/миграцией/staging evidence.
- Жёлтая плашка означает существующий код без полного live или production evidence.
- Серый пунктир означает функцию из утверждённых документов, которая пока не реализована.
- Не показывать секреты, `.env`, реальные персональные данные, адрес сервера и connection strings.
- Не использовать внешние шрифты, картинки, библиотеки, `fetch`, XHR, WebSocket, iframe или сетевые URL.
- Страница должна оставаться читаемой на ширине от 320 px без горизонтального переполнения.
- Новые проверки запускать только через Docker Compose.
- Временные файлы допустимы только в корневом `tmp/`.
- После каждого логического шага обновлять `Дорожная карта.md` и `changelog.md`.
- Push и внешние публикации не выполнять.

---

## File Structure

- Create: `docs/moroz-i-solntse-full-architecture.html` — полная пользовательская карта проекта.
- Create: `project/tests/unit/test_full_project_architecture_visual.py` — структурный и safety-контракт нового файла.
- Modify: `Дорожная карта.md` — завершённый статус артефакта и проверки.
- Modify: `changelog.md` — RED/GREEN, проверка и ограничения визуального preview.

---

### Task 1: Статический контракт нового HTML

**Files:**
- Create: `project/tests/unit/test_full_project_architecture_visual.py`
- Modify: `changelog.md`

**Interfaces:**
- Consumes: путь репозитория, вычисляемый от файла теста.
- Produces: обязательные ID секций, три статуса, ключевые узлы, сравнительную таблицу, автономность и responsive-контракт.

- [ ] **Step 1: Write the failing test**

Create `project/tests/unit/test_full_project_architecture_visual.py`:

```python
from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
import re


REPO_ROOT = Path(__file__).resolve().parents[3]
HTML_PATH = REPO_ROOT / "docs" / "moroz-i-solntse-full-architecture.html"

REQUIRED_SECTIONS = {
    "comparison",
    "channels",
    "ingress",
    "queue-worker",
    "routing",
    "llm-security",
    "booking",
    "delivery",
    "background",
    "admin-evals",
    "data",
    "infrastructure",
    "models-integrations",
    "privacy-security",
    "future-boundary",
}

REQUIRED_IMPLEMENTED = {
    "telegram-channel",
    "privacy-gate",
    "durable-inbox",
    "message-buffer",
    "rabbitmq-tasks",
    "worker-process",
    "scripts-first-router",
    "pii-masking",
    "guardrails",
    "primary-reserve-gateway",
    "output-validator",
    "durable-outbox",
    "admin-panel",
    "eval-system",
    "postgres-store",
    "redis-store",
    "rabbitmq-store",
}

REQUIRED_EVIDENCE_PENDING = {
    "yclients-live",
    "production-backup",
    "external-uptime",
}

REQUIRED_PLANNED = {
    "whatsapp-channel",
    "instagram-channel",
    "vk-channel",
    "site-channel",
    "voice-speechkit",
    "yookassa-payments",
    "mass-mailings",
    "reactivation",
    "knowledge-base-editor",
    "extended-business-analytics",
}


class ArchitectureParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = dict(attrs)
        self.tags.append((tag, attributes))
        if element_id := attributes.get("id"):
            self.ids.add(element_id)


def load_visual() -> tuple[str, ArchitectureParser]:
    html = HTML_PATH.read_text(encoding="utf-8")
    parser = ArchitectureParser()
    parser.feed(html)
    return html, parser


def test_full_architecture_contains_required_sections_and_statuses() -> None:
    html, parser = load_visual()
    assert REQUIRED_SECTIONS <= parser.ids
    assert REQUIRED_IMPLEMENTED <= parser.ids
    assert REQUIRED_EVIDENCE_PENDING <= parser.ids
    assert REQUIRED_PLANNED <= parser.ids
    assert 'data-status="implemented"' in html
    assert 'data-status="evidence-pending"' in html
    assert 'data-status="planned"' in html
    assert "РАБОТАЕТ" in html
    assert "КОД ЕСТЬ · НУЖНА ПРОВЕРКА" in html
    assert "ПЛАН" in html


def test_comparison_and_fact_snapshot_are_explicit() -> None:
    html, _ = load_visual()
    for token in (
        "Lucky Hair Studio",
        "Что расходится",
        "Что можно перенять",
        "0009_production_admin",
        "worker",
        "scheduler",
        "RabbitMQ",
        "PostgreSQL",
        "Redis",
        "YCLIENTS",
        "152-ФЗ",
    ):
        assert token in html
    assert re.search(r"git\s+<code>[0-9a-f]{7,40}</code>", html)


def test_page_is_standalone_and_does_not_expose_secrets() -> None:
    html, parser = load_visual()
    without_svg_namespace = html.replace("http://www.w3.org/2000/svg", "")
    for forbidden in (
        "fetch(",
        "XMLHttpRequest",
        "WebSocket(",
        "http://",
        "https://",
        "<iframe",
        "<object",
        "<embed",
        "TELEGRAM_BOT_TOKEN=",
        "POSTGRES_PASSWORD=",
        "RABBITMQ_PASSWORD=",
        "API_KEY=",
    ):
        assert forbidden not in without_svg_namespace

    for tag, attrs in parser.tags:
        if tag in {"script", "link", "img"}:
            assert not (attrs.get("src") or attrs.get("href"))


def test_reference_style_and_responsive_contract_are_present() -> None:
    html, _ = load_visual()
    for token in (
        ".node",
        ".decision",
        ".lane",
        ".branches",
        ".future",
        ".flagoff",
        "border-style: dashed",
        "@media (max-width: 760px)",
        "overflow-x: hidden",
    ):
        assert token in html
```

- [ ] **Step 2: Run the test to verify RED**

Run from `project/`:

```bash
docker compose --env-file ../.env run --rm test \
  pytest tests/unit/test_full_project_architecture_visual.py -q
```

Expected: FAIL with `FileNotFoundError` for
`docs/moroz-i-solntse-full-architecture.html`.

- [ ] **Step 3: Record RED evidence**

Prepend one UTC+3 entry to `changelog.md` with the exact failing command and
failure reason. Do not modify production code.

- [ ] **Step 4: Commit the RED contract**

```bash
git add project/tests/unit/test_full_project_architecture_visual.py changelog.md
git commit -m "test: задан контракт полной HTML-архитектуры"
```

---

### Task 2: Полная архитектурная страница в стиле референса

**Files:**
- Create: `docs/moroz-i-solntse-full-architecture.html`
- Modify: `changelog.md`

**Interfaces:**
- Consumes: IDs and content tokens from Task 1; verified facts from `project/`, `docs/production-v1-architecture.html`, `ТЗ и архитектура.md`, `План реализации.md`, `Дорожная карта.md`, `changelog.md`.
- Produces: one standalone UTF-8 HTML document satisfying the test contract.

- [ ] **Step 1: Recheck the fact snapshot before writing**

Run:

```bash
git rev-parse --short HEAD
docker compose --env-file ../.env config --services
```

Inspect the migration head:

```bash
Get-ChildItem migrations/versions -Filter '*.py' |
  Sort-Object Name |
  Select-Object -Last 1 -ExpandProperty BaseName
```

Expected migration head: `0009_production_admin`. Record only evidence visible
in command output; do not copy secret environment values into HTML or logs.

- [ ] **Step 2: Build the page shell and exact status legend**

Create `docs/moroz-i-solntse-full-architecture.html` with:

```html
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Мороз и Солнце — Полная архитектура проекта</title>
  <style>
    * { box-sizing: border-box; }
    body {
      margin: 0;
      padding: 32px 16px 64px;
      overflow-x: hidden;
      font-family: -apple-system, "Segoe UI", Roboto, Arial, sans-serif;
      background: #fbfbfb;
      color: #1c2733;
    }
    .node { border: 1.5px solid; border-radius: 10px; }
    .decision { clip-path: polygon(50% 0,100% 50%,50% 100%,0 50%); }
    .lane { border: 2px dashed #b7c2cd; border-radius: 14px; }
    .branches { display: grid; gap: 16px; }
    .future { border-style: dashed; background: #f4f5f7; color: #8b959e; }
    .flagoff { background: #fff3cd; color: #8a6d1a; }
    @media (max-width: 760px) {
      .branches { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <h1>МОРОЗ И СОЛНЦЕ — ПОЛНАЯ АРХИТЕКТУРА ПРОЕКТА</h1>
  <p class="sub"><b>Цветное:</b> РАБОТАЕТ · <b>Жёлтое:</b> КОД ЕСТЬ · НУЖНА ПРОВЕРКА · <b>Серое:</b> ПЛАН</p>
</body>
</html>
```

Expand the CSS to match the reference's actual node colors, rows, arrows,
lanes, cards, tables and mobile stacking without copying Lucky Hair business
facts.

- [ ] **Step 3: Add comparison and implemented runtime sections**

Add semantic `<section>` elements with the exact IDs from `REQUIRED_SECTIONS`.
Every implemented card must use:

```html
<div id="telegram-channel" class="node chan" data-status="implemented">
  <b>Telegram</b>
  <small>webhook/polling через aiogram · текущий пользовательский канал</small>
  <span class="status done">РАБОТАЕТ</span>
</div>
```

Populate the flows from verified code contracts:

- webhook → privacy gate → durable inbox → Redis buffer;
- RabbitMQ tasks → worker → scripts-first/scenario path;
- PII mask → guardrails → primary/reserve gateway → output validator;
- durable outbound → Telegram sender;
- admin, evals, PostgreSQL, Redis, RabbitMQ;
- scheduler, notifications, lifecycle, feedback, retry and DLQ.

The comparison table must separate `Что расходится` and `Что можно перенять`.

- [ ] **Step 4: Add evidence-pending integrations**

Use a colored node with a yellow label:

```html
<div id="yclients-live" class="node proc" data-status="evidence-pending">
  <b>YCLIENTS API</b>
  <small>адаптер, ownership и lifecycle реализованы; live-доступ и production smoke не подтверждены</small>
  <span class="flagoff">КОД ЕСТЬ · НУЖНА ПРОВЕРКА</span>
</div>
```

Apply the same conservative status to production backup evidence and external
uptime monitoring. Do not imply that local/e2e tests equal a live provider run.

- [ ] **Step 5: Add the planned boundary**

Use gray dashed nodes such as:

```html
<div id="yookassa-payments" class="node future" data-status="planned">
  <b>ЮKassa</b>
  <small>платёжная ссылка, подписанный webhook, статусы и возвраты</small>
  <span class="ph">ПЛАН</span>
</div>
```

Include all IDs from `REQUIRED_PLANNED`. Planned nodes must not appear inside
the main working path as though they already execute.

- [ ] **Step 6: Run the focused GREEN test**

```bash
docker compose --env-file ../.env run --rm test \
  pytest tests/unit/test_full_project_architecture_visual.py -q
```

Expected: `4 passed`.

- [ ] **Step 7: Record GREEN evidence and commit**

Prepend the exact result to `changelog.md`, then:

```bash
git add docs/moroz-i-solntse-full-architecture.html changelog.md
git commit -m "docs: добавлена полная архитектура проекта"
```

---

### Task 3: Regression, visual QA and project handoff

**Files:**
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`

**Interfaces:**
- Consumes: completed HTML and both architecture test modules.
- Produces: regression evidence, completed roadmap item and final local commit.

- [ ] **Step 1: Run both architecture contracts in Docker**

```bash
docker compose --env-file ../.env run --rm test \
  pytest tests/unit/test_full_project_architecture_visual.py \
         tests/unit/test_architecture_visual.py -q
```

Expected: all tests PASS; the exact count is recorded from output rather than
predicted in project documentation.

- [ ] **Step 2: Run static repository checks**

```bash
git diff --check
git status --short
```

Expected: no whitespace errors; only intended roadmap/changelog changes remain.

- [ ] **Step 3: Perform visual QA**

Open `docs/moroz-i-solntse-full-architecture.html` using the available safe
local preview surface and inspect desktop and mobile widths. Verify:

- no overlapping cards or clipped text;
- gray planned nodes remain visually distinct;
- yellow evidence labels are readable;
- tables scroll or stack safely on mobile;
- the main message path reads from top to bottom.

If the browser security policy still blocks local HTML, do not circumvent it.
Record that limitation and validate syntax, DOM structure, responsive CSS tokens
and absence of external resources through the Docker tests.

- [ ] **Step 4: Update project records**

In `Дорожная карта.md`, mark the full standalone architecture item complete and
record the final Docker result. In `changelog.md`, prepend an entry containing:

- created file path;
- status model;
- focused/regression test result;
- visual QA result or the explicit browser-preview limitation;
- confirmation that the existing Production V1 HTML was not modified.

- [ ] **Step 5: Commit the verified handoff**

```bash
git add Дорожная\ карта.md changelog.md
git commit -m "docs: подтверждена полная архитектурная схема"
```

- [ ] **Step 6: Final cleanliness check**

```bash
git status --short
git log -4 --oneline
```

Expected: clean working tree and three new logical commits for RED contract,
HTML implementation and verified handoff. Do not push.
