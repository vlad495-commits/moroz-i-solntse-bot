import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from uuid import uuid4

import pytest

from moroz.booking.models import BookingScenario
from moroz.booking.service import BookingService
from moroz.booking.yclients import YclientsAdapter
from moroz.booking.yclients_http import YclientsConfig


pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


class DropAfterCreateServer(ThreadingHTTPServer):
    def __init__(self) -> None:
        self.create_count = 0
        super().__init__(("127.0.0.1", 0), DropAfterCreateHandler)


class DropAfterCreateHandler(BaseHTTPRequestHandler):
    server: DropAfterCreateServer

    def do_GET(self) -> None:
        if "/book_dates/" in self.path:
            self._json(200, {"success": True, "data": {"booking_dates": ["2026-07-29"]}})
        elif "/book_staff/" in self.path:
            self._json(200, {"success": True, "data": [{"id": 6544, "bookable": True}]})
        elif "/book_times/" in self.path:
            self._json(200, {"success": True, "data": [
                {"datetime": 1785315600, "seance_length": 3600}
            ]})
        else:
            self._json(404, {"success": False})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        if "/book_check/" in self.path:
            self._json(201, {"success": True, "data": {}})
            return
        if "/records/" in self.path:
            self.server.create_count += 1
            self.close_connection = True
            return
        self._json(404, {"success": False})

    def _json(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        pass


def _config(server: DropAfterCreateServer) -> YclientsConfig:
    host, port = server.server_address
    return YclientsConfig(
        base_url=f"http://{host}:{port}",
        partner_token="partner-value",
        user_token="user-value",
        company_id=123,
    )


def _slot_id(config: YclientsConfig) -> str:
    raw = json.dumps(
        {"duration": 3600, "services": [331], "staff": 6544, "start": 1785315600},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    message = b"yclients-slot:v1\0" + str(config.company_id).encode() + b"\0" + raw
    tag = hmac.new(config.user_token.encode(), message, hashlib.sha256).digest()[:16]
    encode = lambda value: base64.urlsafe_b64encode(value).decode().rstrip("=")
    return f"yclients:v1:{encode(raw)}.{encode(tag)}"


async def test_real_adapter_unknown_create_is_durable_and_never_retried_after_restart(repo) -> None:
    server = DropAfterCreateServer()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = _config(server)
        scenario = BookingScenario(
            id=uuid4(),
            kind="create",
            phase="awaiting_confirmation",
            idempotency_key=f"create:{uuid4()}",
            customer_id="customer-7",
            state={
                "slot_query": {
                    "service_ids": ["331"],
                    "starts_after": "2026-07-29T00:00:00+03:00",
                    "starts_before": "2026-07-30T00:00:00+03:00",
                    "staff_id": "6544",
                },
                "selected_slot_id": _slot_id(config),
                "customer_name": "Sandbox Customer",
                "customer_phone": "+70000000000",
                "personal_data_processing_allowed": True,
                "comment": "test booking",
            },
            error_code=None,
            created_at=NOW,
            updated_at=NOW,
        )
        await repo.create_scenario(scenario)

        result = await BookingService(
            YclientsAdapter(config), repo, now=lambda: NOW
        ).handle(scenario.id, confirmed=True)
        repeat = await BookingService(
            YclientsAdapter(config), repo, now=lambda: NOW
        ).handle(scenario.id, confirmed=True)

        assert repeat == result
        assert (result.status, result.error_code) == (
            "escalated",
            "booking_outcome_unknown",
        )
        assert server.create_count == 1
        stored = await repo.get_scenario(scenario.id)
        assert (stored.phase, stored.error_code) == (
            "escalated",
            "booking_outcome_unknown",
        )
        events = await repo.list_events(scenario.id)
        assert [event.event_type for event in events].count("booking_execution_started") == 1
        assert [event.event_type for event in events].count("admin_attention_required") == 1
        assert await repo.get_local_booking(scenario.id) is None
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
