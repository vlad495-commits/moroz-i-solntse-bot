from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import json
import os
import re
from typing import Mapping
from urllib.parse import urlsplit

from aiogram import Bot


WEBHOOK_SECRET = re.compile(r"[A-Za-z0-9_-]{32,64}\Z")


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
    return parser


async def run(args: argparse.Namespace) -> dict[str, object]:
    if args.group == "webhook":
        return await manage_webhook(
            args.action,
            WebhookConfig.from_env(os.environ),
            drop_pending_updates=args.drop_pending_updates,
        )
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
