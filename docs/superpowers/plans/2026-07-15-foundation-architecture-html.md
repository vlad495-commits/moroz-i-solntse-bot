# Foundation Architecture HTML Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Создать самостоятельную интерактивную HTML-карту завершённой фазы Foundation для технического владельца проекта.

**Architecture:** Один редактируемый HTML-фрагмент хранится в thread-scoped visualization directory и рендерится штатным `visualize/scripts/render.py` в `docs/foundation-architecture.html`. Вся схема, данные компонентов, CSS и локальная интерактивность находятся внутри фрагмента; сетевых запросов, сборщика и новых зависимостей нет.

**Tech Stack:** семантический HTML, CSS Grid/Flexbox, inline SVG, vanilla JavaScript, штатный renderer визуализаций.

## Global Constraints

- Единственный итоговый проектный артефакт: `docs/foundation-architecture.html`.
- Редактируемый fragment: `C:/Users/dauto/.codex/visualizations/2026/07/14/019f6238-513d-7323-b273-b47b67759bf4/foundation-architecture.html`.
- Standalone HTML работает локально без сервера, сборщика, `fetch`, XHR, WebSocket и внешних данных.
- Не показывать значения `.env`, credentials, реальные строки подключений или содержимое пользовательских сообщений.
- Не обозначать Reliable Telegram Pipeline, YCLIENTS, production guardrails и уведомления как реализованные.
- На первом экране явно показать Foundation, независимый review и `122/122` Docker-теста.
- Интерактивные узлы доступны с клавиатуры через native `<button>`; выбранный узел обновляет одну общую область деталей.
- Поддержать ширину от 320 px без горизонтального скролла и светлую/тёмную тему через системные theme variables.
- Ponytail full: без фреймворка, диаграммной библиотеки, конфигурационного слоя, фильтров, zoom/pan и декоративной анимации потока.

---

### Task 1: Интерактивная карта и standalone HTML

**Files:**
- Create: `C:/Users/dauto/.codex/visualizations/2026/07/14/019f6238-513d-7323-b273-b47b67759bf4/foundation-architecture.html`
- Create: `docs/foundation-architecture.html`
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`

**Interfaces:**
- Consumes: утверждённая spec `docs/superpowers/specs/2026-07-15-foundation-architecture-visual-design.md` и фактические пути компонентов в `project/`.
- Produces: локально открываемый `docs/foundation-architecture.html`; выбор любого `[data-component]` обновляет `#foundation-detail` по данным `componentDetails`.

- [ ] **Step 1: Зафиксировать проверку структуры до реализации**

Создать `tmp/verify-foundation-architecture.ps1` со следующими проверками итогового файла:

```powershell
$path = Join-Path $PSScriptRoot '..\docs\foundation-architecture.html'
if (-not (Test-Path -LiteralPath $path)) { throw 'standalone HTML is missing' }
$html = Get-Content -Raw -Encoding utf8 $path
$required = @(
  '122/122',
  'data-component="bot"',
  'data-component="worker"',
  'data-component="scheduler"',
  'data-component="postgres"',
  'data-component="redis"',
  'data-component="rabbitmq"',
  'id="foundation-detail"',
  'Reliable Telegram Pipeline',
  'Следующий этап'
)
foreach ($token in $required) {
  if (-not $html.Contains($token)) { throw "missing token: $token" }
}
foreach ($forbidden in @('fetch(', 'XMLHttpRequest', 'WebSocket(', ('TO' + 'DO'), ('T' + 'BD'))) {
  if ($html.Contains($forbidden)) { throw "forbidden token: $forbidden" }
}
```

- [ ] **Step 2: Запустить проверку и подтвердить RED**

Run:

```powershell
& tmp/verify-foundation-architecture.ps1
```

Expected: команда завершается ошибкой `standalone HTML is missing`, потому что итоговый файл ещё не создан.

- [ ] **Step 3: Создать минимальный интерактивный fragment**

Создать fragment с корнем `#foundation-architecture-map`, четырьмя зонами процессов/хранилищ/гарантий/следующей фазы, одной SVG-сеткой связей и общей областью деталей. Данные хранить непосредственно в JavaScript:

```html
<div id="foundation-architecture-map">
  <section aria-label="Статус Foundation" class="viz-grid">
    <div class="card viz-stat"><span class="text-muted">Фаза</span><span class="viz-stat-value">Foundation</span><span>завершена</span></div>
    <div class="card viz-stat"><span class="text-muted">Проверка</span><span class="viz-stat-value">122/122</span><span>Docker-теста</span></div>
    <div class="card viz-stat"><span class="text-muted">Review</span><span class="viz-stat-value">Approved</span><span>без замечаний</span></div>
  </section>
  <div class="foundation-map" aria-label="Связи компонентов Foundation">
    <button type="button" class="btn" data-component="bot">bot</button>
    <button type="button" class="btn" data-component="admin">admin</button>
    <button type="button" class="btn" data-component="worker">worker</button>
    <button type="button" class="btn" data-component="scheduler">scheduler</button>
    <button type="button" class="btn" data-component="postgres">PostgreSQL</button>
    <button type="button" class="btn" data-component="redis">Redis</button>
    <button type="button" class="btn" data-component="rabbitmq">RabbitMQ + DLQ</button>
  </div>
  <section id="foundation-detail" class="card" aria-live="polite"></section>
  <section class="foundation-next" aria-label="Следующий этап">
    <span class="viz-badge">Следующий этап</span>
    <strong>Reliable Telegram Pipeline</strong>
    <span class="text-muted">webhook → privacy gate → buffer → inbox/outbox → очередь → доставка</span>
  </section>
</div>
```

Использовать один объект данных и один обработчик выбора:

```javascript
const componentDetails = {
  bot: {
    title: 'bot',
    purpose: 'Принимает Telegram-сообщения и обращается к LLM в текущем прототипе.',
    guarantee: 'В regression gate проверяется импортом без запуска production polling.',
    files: ['project/llm/bot.py', 'project/llm/handlers.py', 'project/llm/llm.py']
  },
  admin: {
    title: 'admin',
    purpose: 'Показывает существующую серверную админку и управляет настройками.',
    guarantee: 'Использует общий безопасный доступ к PostgreSQL и Redis.',
    files: ['project/admin/app.py', 'project/admin/database.py']
  },
  worker: {
    title: 'worker',
    purpose: 'Выполняет фоновые задачи из RabbitMQ.',
    guarantee: 'Manual ack, retry 1/5/30, подтверждение публикации и DLQ.',
    files: ['project/worker/main.py', 'project/src/moroz/common/queue.py']
  },
  scheduler: {
    title: 'scheduler',
    purpose: 'Даёт долгоживущий процесс для задач по расписанию.',
    guarantee: 'Heartbeat показывает реальное продвижение цикла.',
    files: ['project/scheduler/main.py']
  },
  postgres: {
    title: 'PostgreSQL',
    purpose: 'Хранит постоянную историю и служебное состояние.',
    guarantee: 'Структура изменяется только безопасными Alembic-миграциями.',
    files: ['project/migrations/', 'project/src/moroz/common/db.py']
  },
  redis: {
    title: 'Redis',
    purpose: 'Хранит короткий контекст и быстрое временное состояние.',
    guarantee: 'Клиенты закрываются в finally, ошибки не раскрывают URL и пароли.',
    files: ['project/llm/cache.py']
  },
  rabbitmq: {
    title: 'RabbitMQ + DLQ',
    purpose: 'Надёжно передаёт фоновые задачи worker-процессу.',
    guarantee: 'Три повторные доставки, bounded shutdown и отдельная очередь проблемных задач.',
    files: ['project/src/moroz/common/queue.py', 'project/docker-compose.yml']
  }
};

const root = document.getElementById('foundation-architecture-map');
const detail = root.querySelector('#foundation-detail');
const buttons = [...root.querySelectorAll('[data-component]')];

function renderDetail(item) {
  const wrapper = document.createElement('div');
  const title = document.createElement('h3');
  const purpose = document.createElement('p');
  const guarantee = document.createElement('p');
  const files = document.createElement('code');
  title.textContent = item.title;
  purpose.textContent = item.purpose;
  guarantee.textContent = `Гарантия: ${item.guarantee}`;
  files.textContent = item.files.join(' · ');
  wrapper.append(title, purpose, guarantee, files);
  return wrapper;
}

function selectComponent(key) {
  const item = componentDetails[key];
  detail.replaceChildren(renderDetail(item));
  buttons.forEach((button) => button.setAttribute('aria-pressed', String(button.dataset.component === key)));
}

buttons.forEach((button) => button.addEventListener('click', () => selectComponent(button.dataset.component)));
selectComponent('bot');
```

Формировать DOM через `document.createElement`/`textContent`, не через пользовательский HTML. CSS должен перестраивать карту в одну колонку на узком экране, не задавать fixed/viewport height и учитывать `prefers-reduced-motion`.

- [ ] **Step 4: Отрендерить standalone HTML штатным renderer**

Run:

```powershell
python C:/Users/dauto/.codex/plugins/cache/openai-bundled/visualize/1.0.11/skills/visualize/scripts/render.py `
  C:/Users/dauto/.codex/visualizations/2026/07/14/019f6238-513d-7323-b273-b47b67759bf4/foundation-architecture.html `
  D:/AI_Projects/moroz_i_solntse/moroz-i-solntse-bot/docs/foundation-architecture.html
```

Expected: renderer завершается с exit `0`, итоговый standalone-файл существует в `docs/`.

- [ ] **Step 5: Запустить структурную проверку и подтвердить GREEN**

Run:

```powershell
& tmp/verify-foundation-architecture.ps1
```

Expected: exit `0`, все обязательные узлы/статусы присутствуют, сетевые API и незаполненные метки отсутствуют.

- [ ] **Step 6: Выполнить визуальную и интерактивную проверку**

Открыть standalone HTML в локальном браузере. Проверить широкую ширину около 1200 px и узкую около 390 px, отсутствие горизонтального переполнения, читаемость светлой/тёмной темы, клавиатурный focus и обновление `#foundation-detail` после выбора `bot`, `worker`, `rabbitmq` и `scheduler`.

Expected: все узлы помещаются без наложений; выбранный узел получает `aria-pressed="true"`; область деталей показывает назначение, гарантию и реальные пути файлов; в консоли нет ошибок.

- [ ] **Step 7: Обновить проектные документы**

В `Дорожная карта.md` отметить HTML-карту завершённой. В `changelog.md` записать создание fragment/standalone, результаты structural/browser checks и факт отсутствия внешних запросов/секретов. Временный `tmp/verify-foundation-architecture.ps1` оставить игнорируемым расходником либо удалить после успешной проверки.

- [ ] **Step 8: Проверить diff и закоммитить**

Run:

```powershell
git diff --check
git status --short
git add docs/foundation-architecture.html 'Дорожная карта.md' changelog.md
git commit -m 'docs: добавлена интерактивная схема foundation'
```

Expected: `git diff --check` не выводит ошибок; коммит содержит standalone HTML и две обязательные записи документации, но не содержит `.env`, временный verifier или секреты.
