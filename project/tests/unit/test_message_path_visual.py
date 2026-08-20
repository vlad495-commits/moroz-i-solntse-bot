import os
from pathlib import Path
import re


HTML_PATH = Path(
    os.environ.get(
        "MESSAGE_PATH_HTML",
        Path(__file__).resolve().parents[3]
        / "docs"
        / "architecture"
        / "message-processing-path.html",
    )
)

REQUIRED_SECTIONS = {
    "ingress-zone",
    "buffer-zone",
    "rabbit-zone",
    "worker-zone",
    "delivery-zone",
    "failure-zone",
}

REQUIRED_NODES = {
    "client-message",
    "telegram-update",
    "caddy-webhook",
    "webhook-security",
    "ingress-gates",
    "postgres-dedup",
    "duplicate-drop",
    "message-inbox-accepted",
    "redis-buffer",
    "redis-deadline",
    "redis-fallback",
    "pipeline-pump",
    "task-outbox-process",
    "outbox-relay",
    "rabbit-exchange",
    "rabbit-queue",
    "rabbit-consumer",
    "worker-router",
    "worker-task-validation",
    "worker-chat-lock",
    "worker-inbox-lock",
    "worker-idempotency-stop",
    "worker-context",
    "worker-llm",
    "worker-atomic-commit",
    "task-outbox-send",
    "worker-send-outbound",
    "outbound-claim",
    "telegram-send",
    "delivery-status",
    "rabbit-retry",
    "rabbit-dlq",
}


def load_document():
    return HTML_PATH.read_text(encoding="utf-8")


def test_message_path_is_static_standalone_html():
    text = load_document()
    assert "<html" in text
    assert re.search(r"<title>[^<]+</title>", text)
    assert "<script" not in text
    assert not re.search(r"<(?:button|input|select|textarea)\b", text)
    assert "http://" not in text
    assert "https://" not in text


def test_message_path_contains_all_runtime_zones_and_nodes():
    text = load_document()
    ids = set(re.findall(r'\bid="([^"]+)"', text))
    assert REQUIRED_SECTIONS <= ids
    assert REQUIRED_NODES <= ids


def test_message_path_documents_real_dedup_and_queue_contracts():
    text = load_document()
    required = (
        "UNIQUE (channel, external_message_id)",
        "ON CONFLICT DO NOTHING",
        "process_message:{update_ids}",
        "reply:process_message:{update_ids}",
        "FOR UPDATE SKIP LOCKED",
        "publisher confirms",
        "persistent message",
        "tasks exchange",
        "tasks routing key",
        "tasks queue",
        "prefetch 4",
        "manual ACK",
        "x-retry-count",
        "1 → 5 → 30 секунд",
        "tasks.dlx",
        "tasks.dlq",
        "TTL 30 дней",
    )
    for token in required:
        assert token in text


def test_worker_internals_and_delivery_states_are_explicit():
    text = load_document()
    required = (
        "pg_advisory_xact_lock",
        "ingress_sequence",
        "accepted → processed",
        "messages + token_usage + outbound_messages + task_outbox",
        "process_message",
        "send_outbound",
        "scheduler_job",
        "pending → sending → sent",
        "delivery_unknown",
    )
    for token in required:
        assert token in text


def test_mobile_layout_stacks_the_whiteboard():
    text = load_document()
    assert "@media (max-width: 760px)" in text
    assert "grid-template-columns: 1fr" in text
    assert "overflow-x: hidden" in text
