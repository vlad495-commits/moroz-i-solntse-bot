"""Owner-only metrics collected from authoritative runtime state."""

import asyncio
import logging
import os
import re
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
RABBIT_QUEUE_METRIC = re.compile(
    r'^rabbitmq_detailed_queue_messages_ready'
    r'\{vhost="/",queue="(tasks(?:\.dlq)?)"\} ([0-9]+)$'
)


async def collect_system_metrics(
    *,
    postgres_loader: Callable[[], Awaitable[dict]] | None = None,
    redis_client=None,
    rabbit_client=None,
    rabbitmq_metrics_url: str | None = None,
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
            await _close_safely("redis", redis_client)

    metrics_url = (
        rabbitmq_metrics_url
        or os.getenv("RABBITMQ_METRICS_URL", "http://rabbitmq:15692")
    ).rstrip("/")
    try:
        if rabbit_client is None:
            rabbit_client = httpx.AsyncClient(
                timeout=PROBE_TIMEOUT_SECONDS,
            )
        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
            response = await rabbit_client.get(
                f"{metrics_url}/metrics/detailed"
                "?family=queue_coarse_metrics&vhost=%2F"
            )
            response.raise_for_status()
            queue_depths = _parse_queue_depths(response.text)
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
            await _close_safely("rabbitmq", rabbit_client)

    return metrics


def _add_postgres_metrics(metrics: MetricsRegistry, snapshot: dict) -> None:
    scalar_names = (
        "bot_inbound_messages_total",
        "worker_processed_messages_total",
        "inbox_accepted_messages",
        "task_outbox_pending_messages",
        "task_outbox_published_total",
        "retained_llm_calls",
        "retained_llm_tokens",
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


def _parse_queue_depths(payload: str) -> dict[str, int]:
    queue_depths = {}
    for line in payload.splitlines():
        match = RABBIT_QUEUE_METRIC.fullmatch(line)
        if match:
            queue_depths[match.group(1)] = int(match.group(2))
    if set(queue_depths) != set(RABBIT_QUEUES):
        raise ValueError("RabbitMQ queue metrics are incomplete")
    return queue_depths


async def _close_safely(source: str, client) -> None:
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
            await client.aclose()
    except Exception as error:
        _log_probe_failure(f"{source}_cleanup", error)
