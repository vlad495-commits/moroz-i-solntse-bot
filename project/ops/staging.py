from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import sys
from typing import Mapping
from urllib import error, request
from urllib.parse import urlsplit

import asyncpg
from aiogram import Bot

from moroz.common.config import database_url_from_env


WEBHOOK_SECRET = re.compile(r"[A-Za-z0-9_-]{32,64}\Z")
CANARY_TEXT = "staging canary: проверка ответа"
SYNTHETIC = {
    "worker-restart": (-1000000001, "staging synthetic: worker restart"),
    "redis-loss": (-1000000002, "staging synthetic: redis loss"),
}
LABELS = {"live", *SYNTHETIC}


def validated_public_url(value: str) -> str:
    if (
        any(char.isspace() or not char.isprintable() for char in value)
        or "\\" in value
        or "?" in value
        or "#" in value
    ):
        raise ValueError("staging_public_url_invalid")
    public_url = value.rstrip("/")
    try:
        parsed = urlsplit(public_url)
        _ = parsed.port
    except ValueError:
        raise ValueError("staging_public_url_invalid") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.netloc.endswith(":")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("staging_public_url_invalid")
    return public_url


def validated_secret(value: str) -> str:
    if WEBHOOK_SECRET.fullmatch(value) is None:
        raise ValueError("webhook_secret_invalid")
    return value


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message):
        raise ValueError("cli_arguments_invalid")


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("staging_webhook_rejected")


@dataclass(frozen=True, slots=True)
class WebhookConfig:
    token: str
    expected_bot_id: int
    webhook_url: str
    secret: str

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "WebhookConfig":
        public_url = validated_public_url(env.get("STAGING_PUBLIC_URL", ""))
        secret = validated_secret(env.get("TELEGRAM_WEBHOOK_SECRET", ""))
        expected = env.get("STAGING_TELEGRAM_BOT_ID", "")
        if not expected.isdecimal():
            raise ValueError("staging_bot_id_invalid")
        token = env.get("TELEGRAM_BOT_TOKEN", "")
        if not token:
            raise ValueError("telegram_token_missing")
        return cls(
            token=token,
            expected_bot_id=int(expected),
            webhook_url=f"{public_url}/telegram/webhook",
            secret=secret,
        )


@dataclass(frozen=True, slots=True)
class Counts:
    inbox: int
    llm: int
    sent: int


@dataclass(frozen=True, slots=True)
class Snapshot:
    counts: Counts
    started_at: datetime


def write_snapshot(
    label: str,
    counts: Counts,
    *,
    started_at: str | None = None,
    state_dir=Path("/state"),
) -> Path:
    if label not in LABELS:
        raise ValueError("evidence_label_invalid")
    path = state_dir / f"staging-{label}.json"
    started = started_at or datetime.now(UTC).isoformat()
    path.write_text(
        json.dumps(
            {
                "counts": asdict(counts),
                "label": label,
                "started_at": started,
                "version": 1,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    return path


def read_snapshot(label: str, *, state_dir=Path("/state")) -> Snapshot:
    if label not in LABELS:
        raise ValueError("evidence_label_invalid")
    data = json.loads((state_dir / f"staging-{label}.json").read_text("utf-8"))
    if data.get("version") != 1 or data.get("label") != label:
        raise ValueError("evidence_state_invalid")
    return Snapshot(
        counts=Counts(**data["counts"]),
        started_at=datetime.fromisoformat(data["started_at"]),
    )


def evidence_delta(before: Counts, after: Counts) -> dict[str, object]:
    result = {
        "inbox_delta": after.inbox - before.inbox,
        "llm_delta": after.llm - before.llm,
        "sent_delta": after.sent - before.sent,
    }
    return {"ok": set(result.values()) == {1}, **result}


async def collect_counts(connection) -> Counts:
    row = await connection.fetchrow(
        """
        SELECT
          (SELECT count(*) FROM message_inbox) AS inbox,
          (SELECT count(*) FROM token_usage) AS llm,
          (SELECT count(*) FROM outbound_messages WHERE status = 'sent') AS sent
        """
    )
    return Counts(row["inbox"], row["llm"], row["sent"])


def build_update(payload: dict[str, str], *, update_id=None, text=None) -> dict:
    received = datetime.fromisoformat(payload["received_at"])
    return {
        "update_id": int(payload["update_id"] if update_id is None else update_id),
        "message": {
            "message_id": int(payload["message_id"]),
            "date": int(received.timestamp()),
            "chat": {"id": int(payload["chat_id"]), "type": "private"},
            "from": {
                "id": int(payload["user_id"]),
                "is_bot": False,
                "first_name": "Staging",
            },
            "text": payload["text"] if text is None else text,
        },
    }


def scan_log_lines(lines) -> dict[str, object]:
    secret = re.compile(
        r"bot\d+:[A-Za-z0-9_-]+|"
        r"(?:postgresql|redis|amqp)s?://[^\s@]+:[^\s@]+@|"
        r"(?:Authorization|X-Telegram-Bot-Api-Secret-Token)\s*[:=]"
    )
    pii = re.compile(
        r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|"
        r"(?<!\w)(?:\+7|8)[\s-]*\(?\d{3}\)?(?:[\s-]*\d){7}(?!\d)"
    )
    raw = re.compile(r'"text"|\b(?:chat_id|user_id|update_id|message_id)\b')
    private_texts = (CANARY_TEXT, *(text for _update_id, text in SYNTHETIC.values()))
    secret_count = 0
    traceback_count = 0
    pii_count = 0
    raw_text_count = 0
    for line in lines:
        secret_count += bool(secret.search(line))
        traceback_count += "Traceback (most recent call last)" in line
        pii_count += bool(pii.search(line))
        raw_text_count += bool(
            raw.search(line) or any(text in line for text in private_texts)
        )
    return {
        "ok": not any(
            (secret_count, traceback_count, pii_count, raw_text_count)
        ),
        "secret_shaped_lines": secret_count,
        "traceback_lines": traceback_count,
        "pii_shaped_lines": pii_count,
        "raw_text_lines": raw_text_count,
    }


async def post_update(public_url: str, secret: str, update: dict) -> None:
    body = json.dumps(update, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        f"{public_url.rstrip('/')}/telegram/webhook",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Telegram-Bot-Api-Secret-Token": secret,
        },
    )

    opener = request.build_opener(NoRedirect())

    def send() -> int:
        try:
            with opener.open(req, timeout=10) as response:
                return response.status
        except error.HTTPError as exc:
            if 300 <= exc.code < 400:
                raise RuntimeError("staging_webhook_rejected") from None
            raise

    if await asyncio.to_thread(send) != 200:
        raise RuntimeError("staging_webhook_rejected")


def smoke_config(env: Mapping[str, str]) -> tuple[str, str, str]:
    return (
        database_url_from_env(env),
        validated_public_url(env.get("STAGING_PUBLIC_URL", "")),
        validated_secret(env.get("TELEGRAM_WEBHOOK_SECRET", "")),
    )


async def find_payload(connection, label: str, snapshot: Snapshot) -> dict | None:
    if label == "live":
        row = await connection.fetchrow(
            """
            SELECT payload
            FROM message_inbox
            WHERE payload->>'text' = $1
              AND created_at >= $2
            ORDER BY created_at DESC
            LIMIT 1
            """,
            CANARY_TEXT,
            snapshot.started_at,
        )
    else:
        row = await connection.fetchrow(
            """
            SELECT payload
            FROM message_inbox
            WHERE channel = 'telegram' AND external_message_id = $1
            """,
            str(SYNTHETIC[label][0]),
        )
    if row is None:
        return None
    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    required = {"update_id", "message_id", "chat_id", "user_id", "text", "received_at"}
    if not isinstance(payload, dict) or not required <= payload.keys():
        raise ValueError("staging_evidence_payload_invalid")
    return payload


async def snapshot_command(label: str, database_url: str) -> dict[str, object]:
    connection = await asyncpg.connect(database_url)
    try:
        write_snapshot(label, await collect_counts(connection))
    finally:
        await connection.close()
    return {"ok": True, "action": "snapshot", "label": label}


async def verify_command(
    label: str,
    database_url: str,
    *,
    timeout_seconds: float = 120,
) -> dict[str, object]:
    snapshot = read_snapshot(label)
    connection = None
    try:
        try:
            async with asyncio.timeout(timeout_seconds):
                connection = await asyncpg.connect(database_url)
                while True:
                    result = evidence_delta(
                        snapshot.counts, await collect_counts(connection)
                    )
                    target = await find_payload(connection, label, snapshot)
                    if result["ok"] and target is not None:
                        return {"action": "verify", "label": label, **result}
                    await asyncio.sleep(1)
        except TimeoutError:
            return {
                "ok": False,
                "action": "verify",
                "label": label,
                "timed_out": True,
            }
    finally:
        if connection is not None:
            await connection.close()


async def replay_live(
    database_url: str, public_url: str, secret: str
) -> dict[str, object]:
    snapshot = read_snapshot("live")
    connection = await asyncpg.connect(database_url)
    try:
        payload = await find_payload(connection, "live", snapshot)
    finally:
        await connection.close()
    if payload is None:
        raise RuntimeError("staging_evidence_update_missing")
    await post_update(public_url, secret, build_update(payload))
    return await verify_command("live", database_url)


async def inject_synthetic(
    label: str,
    database_url: str,
    public_url: str,
    secret: str,
) -> dict[str, object]:
    if label not in SYNTHETIC:
        raise ValueError("synthetic_label_invalid")
    await snapshot_command(label, database_url)
    live_snapshot = read_snapshot("live")
    connection = await asyncpg.connect(database_url)
    try:
        payload = await find_payload(connection, "live", live_snapshot)
    finally:
        await connection.close()
    if payload is None:
        raise RuntimeError("staging_evidence_update_missing")
    payload = {
        **payload,
        "received_at": datetime.now(UTC).isoformat(),
    }
    update_id, text = SYNTHETIC[label]
    await post_update(
        public_url,
        secret,
        build_update(payload, update_id=update_id, text=text),
    )
    return {"ok": True, "action": "inject", "label": label}


async def run_smoke(args: argparse.Namespace) -> dict[str, object]:
    if args.action == "scan-logs":
        return scan_log_lines(sys.stdin)
    database_url, public_url, secret = smoke_config(os.environ)
    if args.action == "snapshot":
        return await snapshot_command(args.label, database_url)
    if args.action == "verify":
        return await verify_command(args.label, database_url)
    if args.action == "replay-live":
        return await replay_live(database_url, public_url, secret)
    if args.action == "inject":
        return await inject_synthetic(args.label, database_url, public_url, secret)
    raise ValueError("smoke_action_invalid")


async def manage_webhook(
    action: str,
    config: WebhookConfig,
    *,
    drop_pending_updates: bool = False,
    bot_factory=Bot,
) -> dict[str, object]:
    bot = bot_factory(config.token)
    try:
        identity = await bot.get_me()
        if identity.id != config.expected_bot_id:
            raise RuntimeError("staging_bot_identity_mismatch")
        if action == "set":
            accepted = await bot.set_webhook(
                url=config.webhook_url,
                secret_token=config.secret,
                allowed_updates=["message", "callback_query"],
                max_connections=5,
                drop_pending_updates=drop_pending_updates,
            )
            if not accepted:
                raise RuntimeError("telegram_set_webhook_rejected")
            return {"ok": True, "action": "set"}
        if action == "status":
            info = await bot.get_webhook_info()
            allowed = sorted(info.allowed_updates or [])
            return {
                "ok": (
                    info.url == config.webhook_url
                    and allowed == ["callback_query", "message"]
                    and info.max_connections == 5
                ),
                "action": "status",
                "pending_update_count": info.pending_update_count,
                "has_last_error": info.last_error_date is not None,
            }
        if action == "delete":
            deleted = await bot.delete_webhook(drop_pending_updates=False)
            if not deleted:
                raise RuntimeError("telegram_delete_webhook_rejected")
            return {"ok": True, "action": "delete"}
        raise ValueError("webhook_action_invalid")
    finally:
        await bot.session.close()


def build_parser() -> argparse.ArgumentParser:
    parser = SafeArgumentParser()
    groups = parser.add_subparsers(dest="group", required=True)
    webhook = groups.add_parser("webhook")
    webhook.add_argument("action", choices=("set", "status", "delete"))
    webhook.add_argument("--drop-pending-updates", action="store_true")
    smoke = groups.add_parser("smoke")
    smoke_actions = smoke.add_subparsers(dest="action", required=True)
    snapshot = smoke_actions.add_parser("snapshot")
    snapshot.add_argument("--label", choices=("live",), required=True)
    verify = smoke_actions.add_parser("verify")
    verify.add_argument(
        "--label",
        choices=("live", "worker-restart", "redis-loss"),
        required=True,
    )
    smoke_actions.add_parser("replay-live")
    inject = smoke_actions.add_parser("inject")
    inject.add_argument(
        "--label", choices=("worker-restart", "redis-loss"), required=True
    )
    smoke_actions.add_parser("scan-logs")
    return parser


async def run(args: argparse.Namespace) -> dict[str, object]:
    if args.group == "webhook":
        if args.drop_pending_updates and args.action != "set":
            raise ValueError("drop_pending_only_valid_for_set")
        return await manage_webhook(
            args.action,
            WebhookConfig.from_env(os.environ),
            drop_pending_updates=args.drop_pending_updates,
        )
    if args.group == "smoke":
        return await run_smoke(args)
    raise ValueError("command_group_invalid")


def main() -> int:
    try:
        result = asyncio.run(run(build_parser().parse_args()))
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get("ok") else 1
    except Exception as error:
        print(json.dumps({"ok": False, "error_type": type(error).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
