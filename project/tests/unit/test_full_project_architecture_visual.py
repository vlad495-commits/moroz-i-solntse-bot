from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
import os
from pathlib import Path
import re
from urllib.parse import urlsplit


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

STATUS_NODES = {
    "implemented": {
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
    },
    "evidence-pending": {
        "yclients-live",
        "production-backup",
        "external-uptime",
    },
    "planned": {
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
    },
}

STATUS_MARKERS = {
    "implemented": "РАБОТАЕТ",
    "evidence-pending": "КОД ЕСТЬ · НУЖНА ПРОВЕРКА",
    "planned": "ПЛАН",
}


@dataclass
class ParsedElement:
    tag: str
    attrs: dict[str, str | None]
    visible_text_parts: list[str] = field(default_factory=list)

    @property
    def visible_text(self) -> str:
        return "".join(self.visible_text_parts)


class FullArchitectureParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.elements_by_id: dict[str, ParsedElement] = {}
        self.tags: list[ParsedElement] = []
        self.visible_text_parts: list[str] = []
        self._open_elements: list[ParsedElement] = []
        self._non_visible_tag_depth = 0

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        element = ParsedElement(tag, dict(attrs))
        self.tags.append(element)
        self._open_elements.append(element)
        element_id = element.attrs.get("id")
        if element_id:
            self.elements_by_id[element_id] = element
        if tag in {"script", "style"}:
            self._non_visible_tag_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self._non_visible_tag_depth -= 1
        for index in range(len(self._open_elements) - 1, -1, -1):
            if self._open_elements[index].tag == tag:
                del self._open_elements[index:]
                break

    def handle_data(self, data: str) -> None:
        if not self._non_visible_tag_depth:
            self.visible_text_parts.append(data)
            for element in self._open_elements:
                element.visible_text_parts.append(data)

    @property
    def visible_text(self) -> str:
        return "".join(self.visible_text_parts)


def load_visual() -> tuple[str, FullArchitectureParser]:
    html = HTML_PATH.read_text(encoding="utf-8")
    parser = FullArchitectureParser()
    parser.feed(html)
    return html, parser


def test_visual_contains_required_sections_and_status_markers() -> None:
    _, parser = load_visual()
    assert REQUIRED_SECTIONS <= parser.elements_by_id.keys()
    for status, nodes in STATUS_NODES.items():
        marker = STATUS_MARKERS[status]
        for node in nodes:
            element = parser.elements_by_id[node]
            assert element.attrs.get("data-status") == status
            assert marker in element.visible_text


def test_visual_contains_status_labels_and_comparison_facts() -> None:
    html, parser = load_visual()
    for marker in STATUS_MARKERS.values():
        assert marker in parser.visible_text
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
    html, parser = load_visual()
    normalized_html = html.casefold()
    for forbidden in (
        "fetch(",
        "xmlhttprequest",
        "websocket(",
        "http://",
        "https://",
    ):
        assert forbidden not in normalized_html
    assert not any(element.tag in {"iframe", "object", "embed"} for element in parser.tags)
    for secret in (
        "telegram_bot_token",
        "postgres_password",
        "rabbitmq_password",
        "api_key",
    ):
        assert not re.search(rf"\b{secret}\b\s*=", normalized_html)


def test_visual_has_only_local_assets() -> None:
    _, parser = load_visual()
    for element in parser.tags:
        if element.tag not in {"script", "link", "img"}:
            continue
        for attribute in ("src", "href"):
            source = element.attrs.get(attribute)
            if not source:
                continue
            source = source.strip()
            if not source or source.startswith("#"):
                continue
            parsed = urlsplit(source)
            assert not source.startswith("//"), (element.tag, attribute, source)
            assert not parsed.scheme, (element.tag, attribute, source)
            assert not parsed.netloc, (element.tag, attribute, source)


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
