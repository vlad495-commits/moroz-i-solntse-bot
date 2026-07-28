import pytest

from moroz.common.alerts import AlertRouter, redact_pii
from moroz.common.metrics import MetricsRegistry


class FakeRedis:
    def __init__(self):
        self.keys = {}

    async def set(self, key, value, *, ex=None, nx=False):
        if nx and key in self.keys:
            return False
        self.keys[key] = {"value": value, "ex": ex}
        return True

    def expire(self, key):
        self.keys.pop(key, None)


class FakeSender:
    def __init__(self):
        self.messages = []

    async def __call__(self, chat_id, text):
        self.messages.append((chat_id, text))


def test_metrics_registry_exports_prometheus_text_without_pii_labels():
    metrics = MetricsRegistry()
    metrics.increment("bot_requests_total", labels={"channel": "telegram"})
    metrics.increment("bot_requests_total", value=2, labels={"channel": "telegram"})
    metrics.set_gauge("queue_dlq_messages", 4)

    text = metrics.to_prometheus()

    assert 'bot_requests_total{channel="telegram"} 3.0' in text
    assert "queue_dlq_messages 4.0" in text

    with pytest.raises(ValueError, match="label is not allowlisted"):
        metrics.increment("bot_requests_total", labels={"phone": "+79990000000"})


@pytest.mark.asyncio
async def test_alert_router_deduplicates_by_code_subject_and_routes_recipients():
    redis = FakeRedis()
    sender = FakeSender()
    router = AlertRouter(
        redis,
        sender,
        technical_chat_id="tech-chat",
        business_chat_id="owner-chat",
        cooldown_seconds=300,
    )

    delivered = await router.emit(
        code="queue_dlq",
        subject="tasks",
        severity="critical",
        text="DLQ has 8 messages for +7 999 000-00-00 and test@example.ru",
        business_critical=True,
    )
    duplicate = await router.emit(
        code="queue_dlq",
        subject="tasks",
        severity="critical",
        text="DLQ still has messages",
        business_critical=True,
    )

    assert delivered is True
    assert duplicate is False
    assert redis.keys["alert:queue_dlq:tasks"]["ex"] == 300
    assert sender.messages == [
        (
            "tech-chat",
            "[critical] queue_dlq/tasks: DLQ has 8 messages for [phone] and [email]",
        ),
        (
            "owner-chat",
            "[critical] queue_dlq/tasks: DLQ has 8 messages for [phone] and [email]",
        ),
    ]


@pytest.mark.asyncio
async def test_alert_router_sends_again_after_cooldown_key_expires():
    redis = FakeRedis()
    sender = FakeSender()
    router = AlertRouter(redis, sender, technical_chat_id="tech-chat")

    assert await router.emit(
        code="llm_errors",
        subject="primary",
        severity="warning",
        text="primary errors above threshold",
    )
    redis.expire("alert:llm_errors:primary")

    assert await router.emit(
        code="llm_errors",
        subject="primary",
        severity="warning",
        text="primary errors above threshold",
    )
    assert len(sender.messages) == 2


def test_redact_pii_masks_email_and_phone_like_values():
    assert redact_pii("mail a@b.ru phone +7 (999) 000-00-00") == (
        "mail [email] phone [phone]"
    )
