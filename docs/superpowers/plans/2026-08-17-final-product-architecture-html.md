# Final Product Architecture HTML Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Обновить основной автономный HTML так, чтобы он честно показывал работающий staging, code/evidence gaps и полную согласованную архитектуру без голосовых.

**Architecture:** Сохранить существующий статичный HTML/CSS и его три статуса. Встроить единый профиль, кампании, каналы и traceability в основной поток, а не складывать их одной строкой в общий future backlog.

**Tech Stack:** HTML5, CSS, Python stdlib `html.parser`, pytest внутри Docker, in-app browser.

## Global Constraints

- Все проектные проверки выполняются только через Docker.
- HTML остаётся автономным: без JavaScript, сетевых запросов и внешних assets.
- Голосовые, SpeechKit и чат сайта не входят в целевую схему.
- Секреты, raw prompt и ПД не попадают в HTML, тесты, логи или Git.

---

### Task 1: Зафиксировать целевой контракт тестом

**Files:**
- Modify: `project/tests/unit/test_full_project_architecture_visual.py`
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`

**Interfaces:**
- Consumes: существующие `REQUIRED_SECTIONS`, `STATUS_NODES` и HTML parser.
- Produces: проверяемые IDs `customer-profile`, `channel-identity-map`, `technical-trace-journal`, `campaign-builder`, `marketing-consent-audience`, `reactivation-selector`, `campaign-scheduler`, `campaign-outbox-delivery`, `campaign-stop-feedback`, `campaign-analytics`.

- [x] **Step 1: Добавить ожидания новых секций и planned-узлов**

Добавить `customer-identity`, `campaigns-reactivation`, `traceability` в `REQUIRED_SECTIONS`; добавить перечисленные IDs в `STATUS_NODES["planned"]`; убрать `site-channel` и `voice-speechkit`.

- [x] **Step 2: Добавить отдельный negative contract**

```python
def test_visual_excludes_voice_and_site_channels() -> None:
    html, parser = load_visual()
    assert "voice-speechkit" not in parser.elements_by_id
    assert "site-channel" not in parser.elements_by_id
    assert "SpeechKit" not in html
    assert "Голосовые" not in html
```

- [x] **Step 3: Проверить RED через Docker**

Run: `docker compose -p moroz-architecture-html-red --env-file ../.env --profile test run --rm -e FULL_ARCHITECTURE_HTML_PATH=/workspace/docs/moroz-i-solntse-full-architecture.html -v ../docs:/workspace/docs:ro test pytest project/tests/unit/test_full_project_architecture_visual.py -q`

Expected: FAIL на отсутствующих новых секциях/узлах и ещё присутствующих voice/site nodes.

### Task 2: Обновить HTML минимальным статичным diff

**Files:**
- Modify: `docs/moroz-i-solntse-full-architecture.html`
- Modify: `project/tests/unit/test_full_project_architecture_visual.py`
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`

**Interfaces:**
- Consumes: статусный контракт Task 1 и факты staging/roadmap на 17.08.2026.
- Produces: автономный целевой HTML с актуальной легендой и встроенными planned-потоками.

- [x] **Step 1: Актуализировать audit header и evidence**

Отразить staging runtime `833db15`, Alembic `0011_yclients_service_catalog`, технический eval `31/31 PASS`, full Docker `1232 passed`, `7/7 healthy` и fail-closed booking projection без раскрытия внешних данных.

- [x] **Step 2: Встроить новые целевые секции**

Добавить customer identity после ingress, campaigns/reactivation рядом с scheduler/outbox и traceability рядом с данными/эксплуатацией. Planned-карточки получают серый пунктир, `data-status="planned"` и маркер `ПЛАН`.

- [x] **Step 3: Удалить voice/site и обновить comparison/future boundary**

Оставить четыре канала: Telegram работает; VK, Instagram и WhatsApp запланированы. Убрать SpeechKit и сайт из текста, IDs и backlog-карточек.

- [x] **Step 4: Проверить GREEN и регрессию через Docker**

Run focused contract с Compose project `moroz-architecture-html-green`, затем весь `project/tests/unit/test_full_project_architecture_visual.py`; Expected: все tests PASS и cleanup `0`.

- [x] **Step 5: Выполнить browser QA и завершить документы**

Через отдельный точный Docker-preview проверить wide и 390px viewports: `scrollWidth == clientWidth`, новые секции видимы, статусная легенда сохранена. Отметить roadmap, записать результат в changelog и сделать логический локальный commit без push.
