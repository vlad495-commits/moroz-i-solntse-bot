import importlib

import pytest


@pytest.mark.asyncio
async def test_deletion_failure_does_not_log_or_expose_recipient_data(caplog):
    module = importlib.import_module("customer_data_deletion")
    private = ("+79991234567", "telegram-user-424242", "proof text", "provider detail")

    class FailingRedis:
        def lock(self, *_args, **_kwargs):
            return self

        async def acquire(self):
            raise RuntimeError(" ".join(private))

    with pytest.raises(module.CustomerDataDeletionError) as raised:
        await module.delete_customer_data(
            pool=None,
            redis_client=FailingRedis(),
            chat_id=424242,
            actor_id=1,
            ip_address=None,
            user_agent=None,
        )

    assert str(raised.value) == "customer data deletion failed"
    for value in private:
        assert value not in caplog.text
        assert value not in str(raised.value)
