import importlib

import pytest


def test_customer_data_deletion_contract_exists():
    module = importlib.import_module("customer_data_deletion")

    assert module.DELETION_CHANNEL == "telegram"
    assert module.CustomerDataDeletionError
    assert module.DeletionResult


@pytest.mark.asyncio
async def test_marker_failure_happens_before_database_access():
    module = importlib.import_module("customer_data_deletion")

    class FailingRedis:
        async def set(self, *_args, **_kwargs):
            raise ConnectionError("secret redis detail")

    class ForbiddenPool:
        def acquire(self):
            raise AssertionError("database must not be touched")

    with pytest.raises(module.CustomerDataDeletionError) as error:
        await module.delete_customer_data(
            pool=ForbiddenPool(),
            redis_client=FailingRedis(),
            chat_id=42,
            actor_id=7,
            ip_address=None,
            user_agent=None,
        )

    assert str(error.value) == "customer data deletion failed"
