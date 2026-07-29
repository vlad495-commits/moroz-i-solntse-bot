from __future__ import annotations

from html.parser import HTMLParser
import os
from pathlib import Path
import re


REPO_ROOT = Path(__file__).resolve().parents[3]
HTML_PATH = Path(
    os.environ.get(
        "ARCHITECTURE_HTML_PATH",
        REPO_ROOT / "docs" / "production-v1-architecture.html",
    )
)

REQUIRED_SECTIONS = {
    "internal-platform",
    "message-flow",
    "booking-flow",
    "background-flow",
    "operations-flow",
    "platform-foundation",
    "post-launch-boundary",
}

REQUIRED_NODES = {
    "system-ingress-layer",
    "system-process-layer",
    "system-domain-layer",
    "system-data-layer",
    "system-external-layer",
    "rabbitmq-subsystem",
    "rabbit-tasks-exchange",
    "rabbit-tasks-queue",
    "rabbit-consumer-contract",
    "rabbit-retry-loop",
    "rabbit-dead-letter-exchange",
    "rabbit-dead-letter-queue",
    "postgres-subsystem",
    "postgres-messaging-tables",
    "postgres-booking-tables",
    "postgres-notification-tables",
    "postgres-admin-eval-tables",
    "redis-subsystem",
    "redis-buffer-keys",
    "redis-context-keys",
    "redis-control-keys",
    "redis-alert-keys",
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
                "primary",
                "data",
                "booking",
                "background",
                "operations",
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


def test_internal_platform_explains_real_infrastructure_contracts() -> None:
    html, _ = load_visual()
    for token in (
        "tasks exchange",
        "tasks queue",
        "prefetch 4",
        "manual ack",
        "x-retry-count",
        "retry 1 / 5 / 30",
        "tasks.dlx",
        "tasks.dlq",
        "TTL 30 дней",
        "message_inbox",
        "outbound_messages",
        "task_outbox",
        "booking_scenarios",
        "scheduler_jobs",
        "admin_sessions",
        "buffer:{chat_id}",
        "buffer:deadlines",
        "chat:{chat_id}:messages",
        "bot:paused",
        "alert:{code}:{subject}",
    ):
        assert token in html


def test_desktop_grid_areas_are_rectangular() -> None:
    html, _ = load_visual()
    for grid_class in (
        "main-grid",
        "booking-grid",
        "background-grid",
        "operations-grid",
    ):
        match = re.search(
            rf"\.{grid_class}\s*\{{.*?grid-template-areas:\s*(.*?);",
            html,
            re.DOTALL,
        )
        assert match, grid_class
        rows = [row.split() for row in re.findall(r'"([^"]+)"', match.group(1))]
        assert rows and len({len(row) for row in rows}) == 1, grid_class

        positions: dict[str, set[tuple[int, int]]] = {}
        for row_index, row in enumerate(rows):
            for column_index, area in enumerate(row):
                if area != ".":
                    positions.setdefault(area, set()).add(
                        (row_index, column_index)
                    )

        for area, cells in positions.items():
            row_indexes = {row for row, _ in cells}
            column_indexes = {column for _, column in cells}
            rectangle = {
                (row, column)
                for row in range(min(row_indexes), max(row_indexes) + 1)
                for column in range(
                    min(column_indexes), max(column_indexes) + 1
                )
            }
            assert cells == rectangle, (grid_class, area, cells)
