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

URL_ATTRIBUTES = {
    "action",
    "archive",
    "background",
    "cite",
    "codebase",
    "data",
    "dynsrc",
    "formaction",
    "href",
    "longdesc",
    "lowsrc",
    "manifest",
    "poster",
    "profile",
    "src",
    "usemap",
    "xlink:href",
}
MULTI_URL_ATTRIBUTES = {"imagesrcset", "ping", "srcset"}
VOID_ELEMENTS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
CSS_URL_PATTERN = re.compile(
    r"url\(\s*(?P<quote>['\"]?)(?P<target>.*?)(?P=quote)\s*\)",
    re.IGNORECASE | re.DOTALL,
)
CONNECTION_STRING_PATTERN = re.compile(
    r"\b(?:postgres(?:ql)?|rediss?|amqps?)://",
    re.IGNORECASE,
)


@dataclass
class ParsedElement:
    tag: str
    attrs: dict[str, str | None]
    ancestor_ids: tuple[str, ...]
    hides_content: bool
    visible_text_parts: list[str] = field(default_factory=list)

    @property
    def visible_text(self) -> str:
        return "".join(self.visible_text_parts)


class FullArchitectureParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.elements_by_id: dict[str, ParsedElement] = {}
        self.id_counts: dict[str, int] = {}
        self.duplicate_ids: set[str] = set()
        self.tags: list[ParsedElement] = []
        self.visible_text_parts: list[str] = []
        self._open_elements: list[ParsedElement] = []
        self._non_visible_tag_depth = 0

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        parsed_attrs = dict(attrs)
        ancestor_ids = tuple(
            element_id
            for element in self._open_elements
            if (element_id := element.attrs.get("id"))
        )
        normalized_style = re.sub(
            r"\s+", "", parsed_attrs.get("style") or ""
        ).casefold()
        hides_content = (
            tag in {"script", "style", "template"}
            or "hidden" in parsed_attrs
            or (parsed_attrs.get("aria-hidden") or "").casefold() == "true"
            or "display:none" in normalized_style
            or "visibility:hidden" in normalized_style
        )
        element = ParsedElement(tag, parsed_attrs, ancestor_ids, hides_content)
        self.tags.append(element)
        element_id = element.attrs.get("id")
        if element_id:
            self.id_counts[element_id] = self.id_counts.get(element_id, 0) + 1
            if element_id in self.elements_by_id:
                self.duplicate_ids.add(element_id)
            else:
                self.elements_by_id[element_id] = element
        if tag not in VOID_ELEMENTS:
            self._open_elements.append(element)
            if hides_content:
                self._non_visible_tag_depth += 1

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._open_elements) - 1, -1, -1):
            if self._open_elements[index].tag == tag:
                self._non_visible_tag_depth -= sum(
                    element.hides_content for element in self._open_elements[index:]
                )
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


def is_external_target(target: str) -> bool:
    target = target.strip()
    if not target or target.startswith("#"):
        return False
    if target.startswith(("//", "\\\\")):
        return True
    parsed = urlsplit(target)
    return bool(parsed.scheme or parsed.netloc)


def iter_attribute_targets(attribute: str, value: str) -> list[str]:
    if attribute in {"imagesrcset", "srcset"}:
        return [
            candidate.strip().split()[0]
            for candidate in value.split(",")
            if candidate.strip()
        ]
    if attribute == "ping":
        return value.split()
    return [value]


def test_visual_contains_required_sections_and_status_markers() -> None:
    _, parser = load_visual()
    assert not parser.duplicate_ids, sorted(parser.duplicate_ids)
    assert REQUIRED_SECTIONS <= parser.elements_by_id.keys()
    for status, nodes in STATUS_NODES.items():
        marker = STATUS_MARKERS[status]
        for node in nodes:
            assert parser.id_counts.get(node) == 1, node
            element = parser.elements_by_id[node]
            assert element.attrs.get("data-status") == status
            assert marker in element.visible_text
            classes = set((element.attrs.get("class") or "").split())
            assert "node" in classes
            if status == "planned":
                assert "future" in classes
                assert "future-boundary" in element.ancestor_ids
            else:
                assert "future" not in classes
                assert "future-boundary" not in element.ancestor_ids


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
    ):
        assert forbidden not in normalized_html
    assert not re.search(r"@import\b", html, re.IGNORECASE)
    for match in CSS_URL_PATTERN.finditer(html):
        target = match.group("target").strip()
        assert not is_external_target(target), target
    for element in parser.tags:
        inline_style = element.attrs.get("style") or ""
        assert not re.search(r"@import\b", inline_style, re.IGNORECASE)
        for match in CSS_URL_PATTERN.finditer(inline_style):
            target = match.group("target").strip()
            assert not is_external_target(target), target
    decoded_content = "\n".join(
        [
            html,
            parser.visible_text,
            *(
                value
                for element in parser.tags
                for value in element.attrs.values()
                if value
            ),
        ]
    )
    assert not CONNECTION_STRING_PATTERN.search(decoded_content)
    for secret in (
        "telegram_bot_token",
        "postgres_password",
        "rabbitmq_password",
        "api_key",
    ):
        assert not re.search(rf"\b{secret}\b\s*=", normalized_html)


def test_visual_has_only_local_assets() -> None:
    _, parser = load_visual()
    forbidden_elements = {"embed", "iframe", "object"}
    present_forbidden_elements = sorted(
        {element.tag for element in parser.tags} & forbidden_elements
    )
    assert not present_forbidden_elements, present_forbidden_elements
    for element in parser.tags:
        for attribute in URL_ATTRIBUTES | MULTI_URL_ATTRIBUTES:
            source = element.attrs.get(attribute)
            if not source:
                continue
            for target in iter_attribute_targets(attribute, source):
                assert not is_external_target(target), (element.tag, attribute, target)
        if element.attrs.get("srcdoc"):
            raise AssertionError((element.tag, "srcdoc"))
        if (
            element.tag == "meta"
            and (element.attrs.get("http-equiv") or "").strip().casefold()
            == "refresh"
        ):
            raise AssertionError((element.tag, "refresh"))


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
        "repeat(auto-fit,minmax(205px,1fr))",
        "max-width: 1085px",
        "@media (max-width: 760px)",
    ):
        assert token in html
