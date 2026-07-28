"""Owner-only metrics collected from authoritative runtime state."""

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable

import httpx
import redis.asyncio as redis

import database
from moroz.common.metrics import MetricsRegistry


logger = logging.getLogger(__name__)
PROBE_TIMEOUT_SECONDS = 2.0
OUTBOUND_STATUSES = frozenset(
    {"pending", "sending", "sent", "delivery_unknown"}
)
SCHEDULER_STATUSES = frozenset(
    {"pending", "claimed", "finished", "skipped", "failed"}
)
RABBIT_QUEUES = ("tasks", "tasks.dlq")


async def collect_system_metrics(
    *,
    postgres_loader: Callable[[], Awaitable[dict]] | None = None,
    redis_client=None,
    rabbit_client=None,
    rabbitmq_management_url: str | None = None,
) -> MetricsRegistry:
    metrics = MetricsRegistry()
    loader = postgres_loader or database.get_system_metrics_snapshot

    try:
        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
            snapshot = await loader()
    except Exception as error:
        _log_probe_failure("postgres", error)
        metrics.set_gauge("moroz_postgres_available", 0)
    else:
        metrics.set_gauge("moroz_postgres_available", 1)
        _add_postgres_metrics(metrics, snapshot)

    try:
        if redis_client is None:
            redis_client = redis.from_url(
                os.environ["REDIS_URL"],
                decode_responses=True,
            )
        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
            await redis_client.ping()
    except Exception as error:
        _log_probe_failure("redis", error)
        metrics.set_gauge("moroz_redis_available", 0)
    else:
        metrics.set_gauge("moroz_redis_available", 1)
    finally:
        if redis_client is not None:
            await redis_client.aclose()

    management_url = (
        rabbitmq_management_url
        or os.getenv("RABBITMQ_MANAGEMENT_URL", "http://rabbitmq:15672")
    ).rstrip("/")
    try:
        if rabbit_client is None:
            rabbit_client = httpx.AsyncClient(
                auth=(
                    os.environ["RABBITMQ_USER"],
                    os.environ["RABBITMQ_PASSWORD"],
                ),
                timeout=PROBE_TIMEOUT_SECONDS,
            )
        queue_depths = {}
        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
            for queue in RABBIT_QUEUES:
                response = await rabbit_client.get(
                    f"{management_url}/api/queues/%2F/{queue}"
                )
                response.raise_for_status()
                payload = response.json()
                count = payload.get("messages_ready")
                if type(count) is not int or count < 0:
                    raise ValueError("RabbitMQ queue depth is invalid")
                queue_depths[queue] = count
    except Exception as error:
        _log_probe_failure("rabbitmq", error)
        metrics.set_gauge("moroz_rabbitmq_available", 0)
    else:
        metrics.set_gauge("moroz_rabbitmq_available", 1)
        for queue, count in queue_depths.items():
            metrics.set_gauge(
                "moroz_queue_ready_messages",
                count,
                labels={"queue": queue},
            )
    finally:
        if rabbit_client is not None:
            await rabbit_client.aclose()

    return metrics


def _add_postgres_metrics(metrics: MetricsRegistry, snapshot: dict) -> None:
    scalar_names = (
        "bot_inbound_messages_total",
        "worker_processed_messages_total",
        "inbox_accepted_messages",
        "task_outbox_pending_messages",
        "task_outbox_published_total",
        "llm_calls_total",
        "llm_tokens_total",
        "open_escalations",
    )
    for name in scalar_names:
        metrics.set_gauge(f"moroz_{name}", snapshot[name])

    oldest_age = snapshot["inbox_oldest_age_seconds"]
    if oldest_age is not None:
        metrics.set_gauge("moroz_inbox_oldest_age_seconds", oldest_age)

    for status, count in snapshot["outbound_messages"].items():
        if status in OUTBOUND_STATUSES:
            metrics.set_gauge(
                "moroz_outbound_messages",
                count,
                labels={"status": status},
            )
    for status, count in snapshot["scheduler_jobs"].items():
        if status in SCHEDULER_STATUSES:
            metrics.set_gauge(
                "moroz_scheduler_jobs",
                count,
                labels={"status": status},
            )


def _log_probe_failure(source: str, error: Exception) -> None:
    logger.warning(
        "metrics_probe_failed source=%s error_type=%s",
        source,
        type(error).__name__,
    )
