from __future__ import annotations

from html.parser import HTMLParser
import os
from pathlib import Path
import re


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
HTML_PATH = Path(
    os.environ.get(
        "FULL_ARCHITECTURE_HTML_PATH",
        REPOSITORY_ROOT / "docs" / "moroz-i-solntse-full-architecture.html",
    )
)

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

REQUIRED_IMPLEMENTED_NODES = {
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

REQUIRED_EVIDENCE_PENDING_NODES = {
    "yclients-live",
    "production-backup",
    "external-uptime",
}

REQUIRED_PLANNED_NODES = {
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


class FullArchitectureParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.statuses_by_id: dict[str, str] = {}
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.visible_text: list[str] = []
        self._non_visible_tag_depth = 0

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = dict(attrs)
        self.tags.append((tag, attributes))
        element_id = attributes.get("id")
        if element_id:
            self.ids.add(element_id)
            status = attributes.get("data-status")
            if status:
                self.statuses_by_id[element_id] = status
        if tag in {"script", "style"}:
            self._non_visible_tag_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self._non_visible_tag_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._non_visible_tag_depth:
            self.visible_text.append(data)


def load_visual() -> tuple[str, FullArchitectureParser]:
    html = HTML_PATH.read_text(encoding="utf-8")
    parser = FullArchitectureParser()
    parser.feed(html)
    return html, parser


def test_visual_contains_required_sections_and_status_nodes() -> None:
    _, parser = load_visual()
    assert REQUIRED_SECTIONS <= parser.ids
    assert REQUIRED_IMPLEMENTED_NODES <= parser.ids
    assert REQUIRED_EVIDENCE_PENDING_NODES <= parser.ids
    assert REQUIRED_PLANNED_NODES <= parser.ids
    assert {
        parser.statuses_by_id[node] for node in REQUIRED_IMPLEMENTED_NODES
    } == {"implemented"}
    assert {
        parser.statuses_by_id[node] for node in REQUIRED_EVIDENCE_PENDING_NODES
    } == {"evidence-pending"}
    assert {
        parser.statuses_by_id[node] for node in REQUIRED_PLANNED_NODES
    } == {"planned"}


def test_visual_contains_status_labels_and_comparison_facts() -> None:
    html, parser = load_visual()
    visible_text = "".join(parser.visible_text)
    for label in ("РАБОТАЕТ", "КОД ЕСТЬ · НУЖНА ПРОВЕРКА", "ПЛАН"):
        assert label in visible_text
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
    assert re.search(r"git <code>[0-9a-f]{7,40}</code>", html)


def test_visual_is_static_and_does_not_expose_secrets() -> None:
    html, _ = load_visual()
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
        assert forbidden not in html
    for secret in (
        "TELEGRAM_BOT_TOKEN",
        "POSTGRES_PASSWORD",
        "RABBITMQ_PASSWORD",
        "API_KEY",
    ):
        assert not re.search(rf"\b{secret}\b\s*=", html)


def test_visual_has_no_external_assets() -> None:
    _, parser = load_visual()
    for tag, attrs in parser.tags:
        if tag in {"script", "link", "img"}:
            source = attrs.get("src") or attrs.get("href")
            assert not source or not source.startswith(("http://", "https://"))


def test_visual_has_required_css_contract() -> None:
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
