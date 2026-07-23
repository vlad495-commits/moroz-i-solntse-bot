import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from zoneinfo import ZoneInfoNotFoundError

import pytest

from moroz.booking.yclients_http import (
    YclientsConfig,
    YclientsHttpClient,
    YclientsRateLimiter,
    YclientsTransportError,
)


@dataclass(frozen=True)
class RecordedRequest:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes


class FakeYclientsServer(ThreadingHTTPServer):
    requests: list[RecordedRequest]
    redirect_url: str | None

    def __init__(self) -> None:
        self.requests = []
        self.redirect_url = None
        super().__init__(("127.0.0.1", 0), FakeYclientsHandler)


class FakeYclientsHandler(BaseHTTPRequestHandler):
    server: FakeYclientsServer

    def do_GET(self) -> None:
        self._respond()

    def do_POST(self) -> None:
        self._respond()

    def _respond(self) -> None:
        self.server.requests.append(RecordedRequest(
            method=self.command,
            path=self.path,
            headers=dict(self.headers),
            body=self.rfile.read(int(self.headers.get("Content-Length", "0"))),
        ))
        if self.path == "/redirect" and self.server.redirect_url is not None:
            self.send_response(302)
            self.send_header("Location", self.server.redirect_url)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        status = 503 if self.path == "/error" else 200
        body = b"upstream-failure" if status == 503 else b'{"success":true}'
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        pass


@pytest.fixture
def fake_server() -> Iterator[FakeYclientsServer]:
    server = FakeYclientsServer()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.fixture
def client(fake_server: FakeYclientsServer) -> YclientsHttpClient:
    host, port = fake_server.server_address
    config = YclientsConfig.from_env({
        "YCLIENTS_BASE_URL": f"http://{host}:{port}",
        "YCLIENTS_PARTNER_TOKEN": "partner-value",
        "YCLIENTS_USER_TOKEN": "user-value",
        "YCLIENTS_COMPANY_ID": "123",
    })
    return YclientsHttpClient(config)


def test_default_http_clients_share_one_process_limiter() -> None:
    config = YclientsConfig.from_env({
        "YCLIENTS_PARTNER_TOKEN": "partner-value",
        "YCLIENTS_USER_TOKEN": "user-value",
        "YCLIENTS_COMPANY_ID": "123",
    })

    first = YclientsHttpClient(config)
    second = YclientsHttpClient(config)

    assert first._limiter is second._limiter


def test_config_requires_tokens_without_leaking_them() -> None:
    with pytest.raises(ValueError, match="YCLIENTS_USER_TOKEN is required"):
        YclientsConfig.from_env({
            "YCLIENTS_PARTNER_TOKEN": "partner-value",
            "YCLIENTS_COMPANY_ID": "123",
        })
    config = YclientsConfig.from_env({
        "YCLIENTS_PARTNER_TOKEN": "partner-value",
        "YCLIENTS_USER_TOKEN": "user-value",
        "YCLIENTS_COMPANY_ID": "123",
    })

    assert "partner-value" not in repr(config)
    assert "user-value" not in repr(config)


def test_config_validates_positive_numbers_and_timezone() -> None:
    env = {
        "YCLIENTS_PARTNER_TOKEN": "partner-value",
        "YCLIENTS_USER_TOKEN": "user-value",
        "YCLIENTS_COMPANY_ID": "0",
    }

    with pytest.raises(ValueError, match="YCLIENTS numeric settings must be positive"):
        YclientsConfig.from_env(env)
    with pytest.raises(ValueError, match="YCLIENTS numeric settings must be positive"):
        YclientsConfig.from_env({**env, "YCLIENTS_COMPANY_ID": "123", "YCLIENTS_TIMEOUT_SECONDS": "0"})
    with pytest.raises(ZoneInfoNotFoundError):
        YclientsConfig.from_env({**env, "YCLIENTS_COMPANY_ID": "123", "YCLIENTS_TIMEZONE": "not/a-timezone"})


def test_config_uses_defaults_for_empty_optional_values() -> None:
    config = YclientsConfig.from_env({
        "YCLIENTS_PARTNER_TOKEN": "partner-value",
        "YCLIENTS_USER_TOKEN": "user-value",
        "YCLIENTS_COMPANY_ID": "123",
        "YCLIENTS_BASE_URL": "",
        "YCLIENTS_TIMEZONE": "",
        "YCLIENTS_TIMEOUT_SECONDS": "",
    })

    assert config.base_url == "https://api.yclients.com"
    assert config.timezone_name == "Europe/Moscow"
    assert config.timeout_seconds == 10.0


@pytest.mark.parametrize("timeout", ["nan", "inf"])
def test_config_rejects_non_finite_timeout(timeout: str) -> None:
    with pytest.raises(ValueError, match="YCLIENTS numeric settings must be positive"):
        YclientsConfig.from_env({
            "YCLIENTS_PARTNER_TOKEN": "partner-value",
            "YCLIENTS_USER_TOKEN": "user-value",
            "YCLIENTS_COMPANY_ID": "123",
            "YCLIENTS_TIMEOUT_SECONDS": timeout,
        })


@pytest.mark.asyncio
async def test_http_uses_exact_partner_and_partner_user_headers(
    fake_server: FakeYclientsServer, client: YclientsHttpClient,
) -> None:
    await client.request("GET", "/partner")
    await client.request("GET", "/protected", user_auth=True)

    assert fake_server.requests[0].headers["Authorization"] == "Bearer partner-value"
    assert fake_server.requests[1].headers["Authorization"] == (
        "Bearer partner-value, User user-value"
    )
    assert all(
        request.headers["Accept"] == "application/vnd.yclients.v2+json"
        for request in fake_server.requests
    )
    assert all(
        request.headers["Content-Type"] == "application/json"
        for request in fake_server.requests
    )


@pytest.mark.asyncio
async def test_http_encodes_query_json_and_http_error_response(
    fake_server: FakeYclientsServer, client: YclientsHttpClient,
) -> None:
    await client.request(
        "POST",
        "/api/v1/book_check",
        query=(("service_ids", [1, 2]), ("staff_id", 3)),
        json_body={
            "appointments": [{"staff_id": 3}],
            "custom_fields": {
                "moroz_booking_key": "3b53e155-7fd7-4dd0-9ff3-871e0db59577",
            },
        },
    )
    error = await client.request("GET", "/error")

    request = fake_server.requests[0]
    assert request.method == "POST"
    assert request.path == "/api/v1/book_check?service_ids=1&service_ids=2&staff_id=3"
    assert request.headers["Content-Type"] == "application/json"
    assert request.body == (
        b'{"appointments":[{"staff_id":3}],"custom_fields":'
        b'{"moroz_booking_key":"3b53e155-7fd7-4dd0-9ff3-871e0db59577"}}'
    )
    assert b'"api_id"' not in request.body
    assert (error.status, error.body) == (503, b"upstream-failure")
    assert sum(request.path == "/error" for request in fake_server.requests) == 1


@pytest.mark.asyncio
async def test_http_never_follows_redirect_or_leaks_protected_auth(
    fake_server: FakeYclientsServer, client: YclientsHttpClient,
) -> None:
    target = FakeYclientsServer()
    thread = Thread(target=target.serve_forever, daemon=True)
    thread.start()
    host, port = target.server_address
    fake_server.redirect_url = f"http://{host}:{port}/target"
    try:
        response = await client.request(
            "POST", "/redirect", json_body={"safe": True}, user_auth=True
        )
    finally:
        target.shutdown()
        thread.join()
        target.server_close()

    assert response.status == 302
    assert len(fake_server.requests) == 1
    assert fake_server.requests[0].headers["Authorization"] == (
        "Bearer partner-value, User user-value"
    )
    assert target.requests == []


@pytest.mark.asyncio
async def test_transport_error_is_message_safe() -> None:
    config = YclientsConfig.from_env({
        "YCLIENTS_BASE_URL": "http://127.0.0.1:1",
        "YCLIENTS_PARTNER_TOKEN": "partner-value",
        "YCLIENTS_USER_TOKEN": "user-value",
        "YCLIENTS_COMPANY_ID": "123",
        "YCLIENTS_TIMEOUT_SECONDS": "0.1",
    })

    with pytest.raises(YclientsTransportError) as raised:
        await YclientsHttpClient(config).request("GET", "/sensitive-path")

    assert str(raised.value) == ""
    assert "partner-value" not in repr(raised.value)
    assert "user-value" not in repr(raised.value)
    assert "sensitive-path" not in repr(raised.value)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay


class CoordinatedClock(FakeClock):
    def __init__(self, required_waiters: tuple[int, ...]) -> None:
        super().__init__()
        self._required_waiters = required_waiters
        self._round = 0
        self._waiters = [0] * len(required_waiters)
        self.waiters_ready = [asyncio.Event() for _ in required_waiters]
        self.releases = [asyncio.Event() for _ in required_waiters]

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        round_number = self._round
        self._waiters[round_number] += 1
        if self._waiters[round_number] == self._required_waiters[round_number]:
            self.waiters_ready[round_number].set()
        await self.releases[round_number].wait()

    def release(self, now: float) -> None:
        self.now = now
        self.releases[self._round].set()
        self._round += 1


@pytest.mark.asyncio
async def test_rate_limiter_waits_one_second_before_sixth_request() -> None:
    clock = FakeClock()
    limiter = YclientsRateLimiter(monotonic=clock.monotonic, sleep=clock.sleep)

    for _ in range(6):
        await limiter.acquire()

    assert clock.sleeps == [1.0]


@pytest.mark.asyncio
async def test_rate_limiter_admits_only_five_concurrent_waiters_per_window() -> None:
    clock = CoordinatedClock(required_waiters=(6, 1))
    limiter = YclientsRateLimiter(monotonic=clock.monotonic, sleep=clock.sleep)

    for _ in range(5):
        await limiter.acquire()
    contenders = [asyncio.create_task(limiter.acquire()) for _ in range(6)]

    await asyncio.wait_for(clock.waiters_ready[0].wait(), timeout=1.0)
    assert not any(contender.done() for contender in contenders)
    assert clock.sleeps == [1.0] * 6

    clock.release(1.0)
    await asyncio.wait_for(clock.waiters_ready[1].wait(), timeout=1.0)
    assert sum(contender.done() for contender in contenders) == 5
    assert clock.sleeps == [1.0] * 7

    clock.release(2.0)
    await asyncio.wait_for(asyncio.gather(*contenders), timeout=1.0)


@pytest.mark.asyncio
async def test_rate_limiter_waits_for_minute_window_after_200_requests() -> None:
    clock = FakeClock()
    limiter = YclientsRateLimiter(monotonic=clock.monotonic, sleep=clock.sleep)

    for _ in range(200):
        await limiter.acquire()
        clock.now += 0.25
    await limiter.acquire()

    assert clock.sleeps == [pytest.approx(10.0)]
