import asyncio
import http.client
import json
import math
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


@dataclass(frozen=True, slots=True)
class YclientsConfig:
    base_url: str
    partner_token: str = field(repr=False)
    user_token: str = field(repr=False)
    company_id: int
    timezone_name: str = "Europe/Moscow"
    timeout_seconds: float = 10.0

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "YclientsConfig":
        def required(name: str) -> str:
            value = env.get(name, "").strip()
            if not value:
                raise ValueError(f"{name} is required")
            return value

        company_id = int(required("YCLIENTS_COMPANY_ID"))
        base_url = env.get("YCLIENTS_BASE_URL", "").strip() or "https://api.yclients.com"
        timeout_seconds = float(env.get("YCLIENTS_TIMEOUT_SECONDS", "").strip() or "10")
        timezone_name = env.get("YCLIENTS_TIMEZONE", "").strip() or "Europe/Moscow"
        if company_id <= 0 or timeout_seconds <= 0 or not math.isfinite(timeout_seconds):
            raise ValueError("YCLIENTS numeric settings must be positive")
        ZoneInfo(timezone_name)
        return cls(
            base_url=base_url.rstrip("/"),
            partner_token=required("YCLIENTS_PARTNER_TOKEN"),
            user_token=required("YCLIENTS_USER_TOKEN"),
            company_id=company_id,
            timezone_name=timezone_name,
            timeout_seconds=timeout_seconds,
        )


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: bytes


class YclientsTransportError(Exception):
    pass


class YclientsRateLimiter:
    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._monotonic = monotonic
        self._sleep = sleep
        self._second: deque[float] = deque()
        self._minute: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = self._monotonic()
                self._evict(now)
                second_delay = self._delay(self._second, 5, 1.0, now)
                minute_delay = self._delay(self._minute, 200, 60.0, now)
                delay = max(second_delay, minute_delay)
                if not delay:
                    self._second.append(now)
                    self._minute.append(now)
                    return
            await self._sleep(delay)

    def _evict(self, now: float) -> None:
        for timestamps, window in ((self._second, 1.0), (self._minute, 60.0)):
            while timestamps and timestamps[0] <= now - window:
                timestamps.popleft()

    @staticmethod
    def _delay(timestamps: deque[float], limit: int, window: float, now: float) -> float:
        return timestamps[0] + window - now if len(timestamps) >= limit else 0.0


_DEFAULT_LIMITER = YclientsRateLimiter()


class YclientsHttpClient:
    def __init__(self, config: YclientsConfig, *, limiter: YclientsRateLimiter | None = None) -> None:
        self._config = config
        self._limiter = limiter or _DEFAULT_LIMITER

    async def request(
        self,
        method: str,
        path: str,
        *,
        query: Sequence[tuple[str, object]] = (),
        json_body: Mapping[str, object] | None = None,
        user_auth: bool = False,
    ) -> HttpResponse:
        await self._limiter.acquire()
        return await asyncio.to_thread(
            self._request_sync, method, path, query, json_body, user_auth
        )

    def _request_sync(
        self,
        method: str,
        path: str,
        query: Sequence[tuple[str, object]],
        json_body: Mapping[str, object] | None,
        user_auth: bool,
    ) -> HttpResponse:
        encoded_query = urlencode(query, doseq=True)
        url = f"{self._config.base_url}{path}"
        if encoded_query:
            url = f"{url}?{encoded_query}"
        body = None
        headers = {
            "Accept": "application/vnd.yclients.v2+json",
            "Authorization": self._authorization(user_auth),
            "Content-Type": "application/json",
        }
        if json_body is not None:
            body = json.dumps(json_body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self._config.timeout_seconds) as response:
                return HttpResponse(response.status, response.read())
        except HTTPError as error:
            return HttpResponse(error.code, error.read())
        except (URLError, TimeoutError, OSError, http.client.HTTPException) as error:
            raise YclientsTransportError() from error

    def _authorization(self, user_auth: bool) -> str:
        partner = f"Bearer {self._config.partner_token}"
        return f"{partner}, User {self._config.user_token}" if user_auth else partner
