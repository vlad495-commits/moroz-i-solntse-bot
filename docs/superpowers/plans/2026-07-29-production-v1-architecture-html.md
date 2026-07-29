# Production V1 Architecture HTML Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Создать самостоятельную статичную HTML-схему всей фактически реализованной Production Telegram V1, которую технический владелец читает сверху вниз и понимает назначение каждого узла и связи.

**Architecture:** Один автономный `docs/production-v1-architecture.html` содержит четыре последовательных workflow-полотна и общий фундамент. Узлы — обычные статичные блоки; минимальный vanilla JavaScript только строит адаптивные SVG-соединители по декларативным `data-from` / `data-to`, без кликов, фильтров и изменяемого состояния.

**Tech Stack:** семантический HTML, CSS Grid, inline SVG, vanilla JavaScript, Python stdlib `html.parser`, Docker Compose pytest, локальная browser-проверка.

## Global Constraints

- Итоговый артефакт: `docs/production-v1-architecture.html`.
- Источник требований: `docs/superpowers/specs/2026-07-29-production-v1-architecture-visual-design.md`.
- Визуальный образец: последняя workflow-компоновка `docs/foundation-client-message-flow.html` и `docs/foundation-architecture.html` из ветки `codex/foundation-architecture-html`.
- Схема показывает только фактически реализованный Telegram V1 runtime; post-launch backlog визуально отделён и не включён в рабочие потоки.
- Никаких кликабельных узлов, кнопок, вкладок, фильтров, zoom/pan, hover-details, скрытых карточек или пошаговой навигации.
- HTML работает локально без сервера, сборщика, `fetch`, XHR, WebSocket, внешних библиотек и сетевых ресурсов.
- Не показывать `.env`, credentials, токены, серверные адреса, строки подключений, персональные данные и тексты сообщений.
- Схема читается сверху вниз, поддерживает светлую/тёмную тему и ширину от 320 px без горизонтального переполнения.
- На узкой ширине полотна перестраиваются в линейные вертикальные цепочки.
- Все изменения и проверки выполняются через Docker Compose, кроме browser visual QA и статических git-проверок.
- Временные screenshots и расходники складываются только в корневой `tmp/`.
- После каждого логического шага обновляются `Дорожная карта.md` и `changelog.md`.

---

## File Structure

- Create: `docs/production-v1-architecture.html` — единственная пользовательская визуальная схема.
- Create: `project/tests/unit/test_architecture_visual.py` — постоянный структурный контракт схемы.
- Modify: `Дорожная карта.md` — статус создания и проверки схемы.
- Modify: `changelog.md` — фактические шаги, RED/GREEN и визуальная проверка.

---

### Task 1: Семантический каркас и полный набор узлов

**Files:**
- Create: `project/tests/unit/test_architecture_visual.py`
- Create: `docs/production-v1-architecture.html`
- Modify: `changelog.md`

**Interfaces:**
- Consumes: approved visual spec and the current `project/` module tree.
- Produces: four sections with stable IDs `message-flow`, `booking-flow`, `background-flow`, `operations-flow`; every runtime node has a unique `id`.

- [ ] **Step 1: Write the failing structural contract**

Create `project/tests/unit/test_architecture_visual.py`:

```python
from __future__ import annotations

from html.parser import HTMLParser
import os
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
HTML_PATH = Path(
    os.environ.get(
        "ARCHITECTURE_HTML_PATH",
        REPO_ROOT / "docs" / "production-v1-architecture.html",
    )
)

REQUIRED_SECTIONS = {
    "message-flow",
    "booking-flow",
    "background-flow",
    "operations-flow",
    "platform-foundation",
    "post-launch-boundary",
}

REQUIRED_NODES = {
    "telegram-client",
    "telegram-api",
    "caddy-ingress",
    "bot-process",
    "webhook-validation",
    "privacy-gate",
    "durable-inbox",
    "message-buffer",
    "rabbit-tasks",
    "worker-process",
    "chat-serialization",
    "scripts-router",
    "scenario-engine",
    "pii-masking",
    "guardrails",
    "llm-gateway",
    "output-validator",
    "durable-outbound",
    "telegram-sender",
    "postgres-storage",
    "redis-storage",
    "rabbit-storage",
    "booking-state-machine",
    "booking-ownership",
    "yclients-read",
    "booking-confirmation",
    "yclients-mutation",
    "booking-reconciliation",
    "notification-planner",
    "scheduler-jobs",
    "scheduler-process",
    "notification-handler",
    "lifecycle-handler",
    "dead-letter-queue",
    "human-escalation",
    "admin-process",
    "admin-auth",
    "admin-session",
    "admin-rbac-csrf",
    "admin-features",
    "admin-audit",
    "health-endpoint",
    "system-metrics",
    "alert-routing",
    "backup-verify-restore",
    "deploy-gates",
    "rollback-images",
    "alembic-migrations",
    "docker-compose",
    "shared-package",
    "test-gate",
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
        element_id = attributes.get("id")
        if element_id:
            self.ids.add(element_id)


def load_visual() -> tuple[str, ArchitectureParser]:
    html = HTML_PATH.read_text(encoding="utf-8")
    parser = ArchitectureParser()
    parser.feed(html)
    return html, parser


def test_visual_contains_every_implemented_runtime_node() -> None:
    html, parser = load_visual()
    assert REQUIRED_SECTIONS <= parser.ids
    assert REQUIRED_NODES <= parser.ids
    assert "919 passed" in html
    assert "READY FOR MANUAL ACCEPTANCE" in html
    assert "0/14" in html
    assert "0009_production_admin" in html


def test_visual_is_static_and_self_contained() -> None:
    html, parser = load_visual()
    assert all(tag != "button" for tag, _ in parser.tags)
    assert 'data-component="' not in html
    assert "aria-pressed" not in html
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
    ):
        assert forbidden not in without_svg_namespace

    for tag, attrs in parser.tags:
        if tag in {"script", "link", "img"}:
            source = attrs.get("src") or attrs.get("href")
            assert not source, (tag, source)
```

- [ ] **Step 2: Run RED in Docker**

Run from `project/`:

```powershell
docker compose --env-file ../.env -p moroz_architecture_visual --profile test run --build --no-deps --rm `
  -e ARCHITECTURE_HTML_PATH=/repo/docs/production-v1-architecture.html `
  -v "${PWD}\..\docs:/repo/docs:ro" `
  test pytest tests/unit/test_architecture_visual.py -q
```

Expected: FAIL because `docs/production-v1-architecture.html` does not exist.

- [ ] **Step 3: Create the standalone document shell**

Create a complete standalone HTML document with this exact top-level structure:

```html
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="referrer" content="no-referrer">
  <meta http-equiv="Content-Security-Policy"
        content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'">
  <title>Moroz i Solntse — Production Telegram V1</title>
</head>
<body>
  <header class="hero">
    <p class="eyebrow">MOROZ I SOLNTSE · PRODUCTION TELEGRAM V1</p>
    <h1>Как устроена реализованная система</h1>
    <p>Читайте сверху вниз: от сообщения клиента до ответа, записи, фоновых задач и эксплуатации.</p>
    <div class="status-strip" aria-label="Фактическая готовность">
      <span>8/8 фаз реализовано</span>
      <span>919 passed</span>
      <span>staging 6/6</span>
      <span>READY FOR MANUAL ACCEPTANCE</span>
      <span>production gates 0/14</span>
    </div>
  </header>
  <main>
    <section id="message-flow" class="workflow" aria-labelledby="message-title">
      <h2 id="message-title">1. Путь сообщения клиента</h2>
    </section>
    <section id="booking-flow" class="workflow" aria-labelledby="booking-title">
      <h2 id="booking-title">2. Запись через YCLIENTS</h2>
    </section>
    <section id="background-flow" class="workflow" aria-labelledby="background-title">
      <h2 id="background-title">3. Scheduler и фоновые задачи</h2>
    </section>
    <section id="operations-flow" class="workflow" aria-labelledby="operations-title">
      <h2 id="operations-title">4. Админка и эксплуатация</h2>
    </section>
    <section id="platform-foundation" aria-labelledby="foundation-title">
      <h2 id="foundation-title">Общий фундамент</h2>
    </section>
    <section id="post-launch-boundary" aria-labelledby="backlog-title">
      <h2 id="backlog-title">После первого запуска</h2>
    </section>
  </main>
</body>
</html>
```

Populate every section with static `<article class="node" id="...">` elements
matching `REQUIRED_NODES`. Each node contains:

```html
<article class="node node-process" id="worker-process">
  <span class="node-kind">Процесс</span>
  <h3>worker</h3>
  <p>Последовательно обрабатывает задачи одного диалога.</p>
  <small>manual ack · retry · DLQ</small>
</article>
```

Use the following exact meaning groups:

- `node-external`: Telegram API, LLM providers, YCLIENTS.
- `node-process`: bot, worker, scheduler, admin.
- `node-step`: validation, privacy, inbox, buffer, router, scenarios, sender.
- `node-security`: PII, guardrails, gateway, output validator.
- `node-storage`: PostgreSQL, Redis, RabbitMQ, DLQ.
- `node-ops`: health, metrics, alerts, backup, deploy, rollback, Alembic.

The backlog section contains only muted static labels: WhatsApp, Instagram,
ВКонтакте, сайт, ЮKassa, SpeechKit, full booking UI, knowledge management UI,
escalation UI, broadcasts/reactivation, extended observability.

- [ ] **Step 4: Run structural GREEN**

Run:

```powershell
docker compose --env-file ../.env -p moroz_architecture_visual --profile test run --build --no-deps --rm `
  -e ARCHITECTURE_HTML_PATH=/repo/docs/production-v1-architecture.html `
  -v "${PWD}\..\docs:/repo/docs:ro" `
  test pytest tests/unit/test_architecture_visual.py -q
```

Expected: `2 passed`; no external resources or interactive controls.

- [ ] **Step 5: Record and commit Task 1**

Append a changelog entry with RED/GREEN evidence, then run:

```powershell
git diff --check
git add docs/production-v1-architecture.html project/tests/unit/test_architecture_visual.py changelog.md
git commit -m "docs: добавлен каркас архитектуры Production V1"
```

Expected: one commit containing the standalone document, permanent contract test
and changelog entry; no `tmp/`, `.env` or secrets.

---

### Task 2: Workflow layout, connections and readable technical content

**Files:**
- Modify: `docs/production-v1-architecture.html`
- Modify: `project/tests/unit/test_architecture_visual.py`
- Modify: `changelog.md`

**Interfaces:**
- Consumes: stable node IDs and section IDs from Task 1.
- Produces: declarative `.edge[data-from][data-to][data-kind]` records and responsive SVG paths; no user interaction.

- [ ] **Step 1: Extend the contract with connection validation**

Append to `project/tests/unit/test_architecture_visual.py`:

```python
REQUIRED_EDGES = {
    ("telegram-client", "telegram-api"),
    ("telegram-api", "caddy-ingress"),
    ("caddy-ingress", "bot-process"),
    ("bot-process", "webhook-validation"),
    ("webhook-validation", "privacy-gate"),
    ("privacy-gate", "durable-inbox"),
    ("durable-inbox", "message-buffer"),
    ("message-buffer", "rabbit-tasks"),
    ("rabbit-tasks", "worker-process"),
    ("worker-process", "chat-serialization"),
    ("chat-serialization", "scripts-router"),
    ("scripts-router", "scenario-engine"),
    ("scenario-engine", "pii-masking"),
    ("pii-masking", "guardrails"),
    ("guardrails", "llm-gateway"),
    ("llm-gateway", "output-validator"),
    ("output-validator", "durable-outbound"),
    ("durable-outbound", "telegram-sender"),
    ("telegram-sender", "telegram-api"),
    ("scenario-engine", "booking-state-machine"),
    ("booking-state-machine", "booking-ownership"),
    ("booking-ownership", "yclients-read"),
    ("yclients-read", "booking-confirmation"),
    ("booking-confirmation", "yclients-mutation"),
    ("yclients-mutation", "booking-reconciliation"),
    ("booking-reconciliation", "notification-planner"),
    ("scheduler-jobs", "scheduler-process"),
    ("scheduler-process", "rabbit-tasks"),
    ("worker-process", "notification-handler"),
    ("worker-process", "lifecycle-handler"),
    ("notification-handler", "telegram-sender"),
    ("lifecycle-handler", "yclients-read"),
    ("rabbit-tasks", "dead-letter-queue"),
    ("dead-letter-queue", "human-escalation"),
    ("caddy-ingress", "admin-process"),
    ("admin-process", "admin-auth"),
    ("admin-auth", "admin-session"),
    ("admin-session", "admin-rbac-csrf"),
    ("admin-rbac-csrf", "admin-features"),
    ("admin-features", "admin-audit"),
    ("admin-process", "system-metrics"),
    ("caddy-ingress", "health-endpoint"),
    ("system-metrics", "alert-routing"),
    ("backup-verify-restore", "postgres-storage"),
    ("deploy-gates", "rollback-images"),
    ("alembic-migrations", "postgres-storage"),
}


def test_every_required_connection_has_valid_nodes() -> None:
    _, parser = load_visual()
    edges: set[tuple[str, str]] = set()
    for tag, attrs in parser.tags:
        if tag == "span" and "edge" in (attrs.get("class") or "").split():
            source = attrs.get("data-from")
            target = attrs.get("data-to")
            assert source in parser.ids
            assert target in parser.ids
            assert attrs.get("data-kind") in {
                "primary", "data", "booking", "background", "operations"
            }
            edges.add((str(source), str(target)))
    assert REQUIRED_EDGES <= edges


def test_responsive_contract_is_present() -> None:
    html, _ = load_visual()
    assert "@media (max-width: 720px)" in html
    assert "prefers-color-scheme: dark" in html
    assert "overflow-x: hidden" in html
    assert "drawConnections" in html
    assert "addEventListener('click'" not in html
    assert 'addEventListener("click"' not in html
```

- [ ] **Step 2: Run connection RED**

Run:

```powershell
docker compose --env-file ../.env -p moroz_architecture_visual --profile test run --build --no-deps --rm `
  -e ARCHITECTURE_HTML_PATH=/repo/docs/production-v1-architecture.html `
  -v "${PWD}\..\docs:/repo/docs:ro" `
  test pytest tests/unit/test_architecture_visual.py -q
```

Expected: the two new tests fail because edge records, responsive CSS and
`drawConnections` do not exist.

- [ ] **Step 3: Implement the static Foundation-style workflow**

For each section add this unframed structure:

```html
<div class="workflow-stage">
  <svg class="connectors" aria-hidden="true">
    <defs>
      <marker id="arrow-primary" viewBox="0 0 10 10" refX="9" refY="5"
              markerWidth="6" markerHeight="6" orient="auto-start-reverse">
        <path d="M 0 0 L 10 5 L 0 10 z"></path>
      </marker>
    </defs>
    <g class="connector-paths"></g>
  </svg>
  <div class="workflow-grid">…existing static nodes…</div>
  <div class="edge-definitions" hidden>
    <span class="edge" data-from="telegram-client" data-to="telegram-api"
          data-kind="primary"></span>
  </div>
</div>
```

Add every pair from `REQUIRED_EDGES` as a static `.edge` definition. Add short
visible labels next to cross-system arrows:

- `HTTPS update`
- `согласие подтверждено`
- `durable task`
- `masked request`
- `validated result`
- `protected mutation`
- `durable scheduler job`
- `retry exhausted`
- `read-only counters`

Use CSS Grid with a central main column, side columns for stores/providers and
explicit named classes for placement. Use only theme-aware variables:

```css
:root {
  color-scheme: light dark;
  --background: #ffffff;
  --foreground: #171717;
  --surface: #f5f5f5;
  --surface-strong: #e9eef5;
  --muted-foreground: #666666;
  --border: #cfcfcf;
  --primary: #1769aa;
  --primary-foreground: #ffffff;
  --series-data: #4b7f52;
  --series-booking: #8a5a16;
  --series-background: #6c5aa0;
  --series-operations: #59636e;
}

@media (prefers-color-scheme: dark) {
  :root {
    --background: #181818;
    --foreground: #f4f4f4;
    --surface: #242424;
    --surface-strong: #29323c;
    --muted-foreground: #b6b6b6;
    --border: #525252;
    --primary: #7dc4ff;
    --primary-foreground: #111111;
    --series-data: #86bd8d;
    --series-booking: #d9a55f;
    --series-background: #b3a0e6;
    --series-operations: #aeb7c0;
  }
}

html, body { margin: 0; min-width: 0; overflow-x: hidden; }
.workflow-stage { position: relative; min-width: 0; }
.connectors { position: absolute; inset: 0; width: 100%; height: 100%; pointer-events: none; }
.workflow-grid { position: relative; display: grid; min-width: 0; }
.node { min-width: 0; overflow-wrap: anywhere; }

@media (max-width: 720px) {
  .workflow-grid { display: flex; flex-direction: column; }
  .node { width: 100%; }
  .connector-label { display: none; }
}
```

Implement only layout-driven connector drawing:

```javascript
function drawConnections() {
  document.querySelectorAll('.workflow-stage').forEach((stage) => {
    const svg = stage.querySelector('.connectors');
    const group = stage.querySelector('.connector-paths');
    const stageRect = stage.getBoundingClientRect();
    group.replaceChildren();

    stage.querySelectorAll('.edge[data-from][data-to]').forEach((edge) => {
      const source = document.getElementById(edge.dataset.from);
      const target = document.getElementById(edge.dataset.to);
      if (!source || !target) return;

      const a = source.getBoundingClientRect();
      const b = target.getBoundingClientRect();
      const x1 = a.left + a.width / 2 - stageRect.left;
      const y1 = a.bottom - stageRect.top;
      const x2 = b.left + b.width / 2 - stageRect.left;
      const y2 = b.top - stageRect.top;
      const middle = y1 + (y2 - y1) / 2;
      const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', `M ${x1} ${y1} V ${middle} H ${x2} V ${y2}`);
      path.setAttribute('class', `connector connector-${edge.dataset.kind}`);
      path.setAttribute('marker-end', 'url(#arrow-primary)');
      group.append(path);
    });

    svg.setAttribute('viewBox', `0 0 ${stageRect.width} ${stageRect.height}`);
  });
}

const redraw = () => requestAnimationFrame(drawConnections);
window.addEventListener('resize', redraw);
if ('ResizeObserver' in window) {
  document.querySelectorAll('.workflow-stage').forEach(
    (stage) => new ResizeObserver(redraw).observe(stage)
  );
}
drawConnections();
```

Do not add event listeners for click, pointer, keyboard or selection.

- [ ] **Step 4: Add the technical reference strips**

Under the corresponding workflow sections add compact static file references:

```html
<p class="file-strip">
  <code>project/llm/webhook.py</code>
  <code>project/src/moroz/messaging/</code>
  <code>project/src/moroz/security/</code>
  <code>project/worker/main.py</code>
</p>
```

Use these exact groups:

- message: `project/llm/webhook.py`, `project/src/moroz/messaging/`,
  `project/src/moroz/security/`, `project/worker/main.py`;
- booking: `project/src/moroz/booking/`, migrations `0005` and `0006`;
- background: `project/src/moroz/notifications/`, `project/scheduler/main.py`,
  migrations `0007` and `0008`;
- operations: `project/admin/`, `project/ops/`, migration `0009`,
  `project/docker-compose.prod.yml`;
- foundation: `project/src/moroz/common/`, `project/migrations/`,
  `project/docker-compose.yml`.

- [ ] **Step 5: Run full visual contract GREEN**

Run:

```powershell
docker compose --env-file ../.env -p moroz_architecture_visual --profile test run --build --no-deps --rm `
  -e ARCHITECTURE_HTML_PATH=/repo/docs/production-v1-architecture.html `
  -v "${PWD}\..\docs:/repo/docs:ro" `
  test pytest tests/unit/test_architecture_visual.py -q
```

Expected: `4 passed`; every required connection references existing node IDs,
the document stays static and self-contained.

- [ ] **Step 6: Record and commit Task 2**

Append exact GREEN evidence to `changelog.md`, then run:

```powershell
git diff --check
git add docs/production-v1-architecture.html project/tests/unit/test_architecture_visual.py changelog.md
git commit -m "docs: показаны все потоки архитектуры Production V1"
```

Expected: clean commit with workflow layout and contracts, no external files.

---

### Task 3: Browser QA, documentation closure and cleanup

**Files:**
- Modify: `docs/production-v1-architecture.html` only for confirmed layout defects
- Modify: `project/tests/unit/test_architecture_visual.py` only for a reproduced regression
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`
- Temporary: `tmp/production-v1-architecture-wide.png`
- Temporary: `tmp/production-v1-architecture-narrow.png`

**Interfaces:**
- Consumes: complete static scheme and passing four-test contract.
- Produces: visually verified standalone architecture at wide and narrow widths, with project status recorded.

- [ ] **Step 1: Start a local static preview**

Use a temporary Docker container rather than a host Python server:

```powershell
docker run --rm -d --name moroz-architecture-preview `
  -p 127.0.0.1:8765:80 `
  -v "${PWD}\..\docs:/usr/share/nginx/html:ro" `
  nginx:alpine
```

Expected: container is running and
`http://127.0.0.1:8765/production-v1-architecture.html` returns HTTP 200.

- [ ] **Step 2: Perform wide browser QA**

Open the page at approximately 1440 px width and verify:

- four workflow sections and the foundation are visible in top-to-bottom order;
- all nodes and labels fit without overlap;
- connector endpoints touch their source/target nodes;
- main, booking, background, data and operations connections are distinguishable
  by both legend label and line style/color;
- backlog is muted and never connected to runtime;
- no button, click affordance or hidden details panel exists;
- browser console contains no errors;
- network panel contains no request except the HTML document itself.

Save one screenshot to `tmp/production-v1-architecture-wide.png`.

- [ ] **Step 3: Perform narrow browser QA**

Set width near 390 px and verify:

- every workflow becomes a readable vertical chain;
- no horizontal scrolling or clipped text;
- connector labels do not overlap nodes;
- status strip wraps;
- the page remains understandable without zooming.

Save one screenshot to `tmp/production-v1-architecture-narrow.png`.

- [ ] **Step 4: Fix only reproduced visual defects**

For each observed defect, first add a narrow regression assertion to
`project/tests/unit/test_architecture_visual.py` when it can be expressed
statically. Make the smallest HTML/CSS/connector correction, rerun the focused
Docker test and repeat the affected viewport inspection. Do not add new nodes,
interactions, libraries or product scope.

- [ ] **Step 5: Run final verification**

Run:

```powershell
docker compose --env-file ../.env -p moroz_architecture_visual --profile test run --build --no-deps --rm `
  -e ARCHITECTURE_HTML_PATH=/repo/docs/production-v1-architecture.html `
  -v "${PWD}\..\docs:/repo/docs:ro" `
  test pytest tests/unit/test_architecture_visual.py -q
git diff --check
```

Expected: all architecture tests pass, `git diff --check` is silent, browser
console is clean at both widths.

- [ ] **Step 6: Stop and clean only task resources**

Run:

```powershell
docker rm -f moroz-architecture-preview
docker compose --env-file ../.env -p moroz_architecture_visual down --volumes --remove-orphans
```

Expected: preview and `moroz_architecture_visual` resources are gone. Do not
remove shared images, other Compose projects or the ignored screenshots unless
the user asks.

- [ ] **Step 7: Close roadmap and changelog**

In `Дорожная карта.md` mark the Production V1 architecture HTML task complete
and record final contract/browser evidence. In `changelog.md` append the wide
and narrow QA result, final tests, cleanup and absence of external requests or
secrets.

- [ ] **Step 8: Final commit**

Run:

```powershell
git diff --check
git status --short
git add docs/production-v1-architecture.html project/tests/unit/test_architecture_visual.py 'Дорожная карта.md' changelog.md
git commit -m "docs: завершена визуальная архитектура Production V1"
```

Expected: clean logical commit; `main` may be ahead of `origin/main`, but no push
is performed without a separate explicit user request.
