import redis
from redis.asyncio import Redis


def test_shared_image_uses_redis_7_4_async_api():
    assert redis.__version__ == "7.4.0"
    for method in ("get", "set", "delete", "publish", "aclose"):
        assert callable(getattr(Redis, method))
