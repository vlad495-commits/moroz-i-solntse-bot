from pathlib import Path
import inspect

import reactivation_database
from moroz.reactivation.policy import ProgramPolicy


MODULE = Path("/workspace/admin/reactivation_database.py")


def test_reactivation_database_has_minimal_public_api():
    assert MODULE.exists(), "reactivation repository must exist"
    text = MODULE.read_text(encoding="utf-8")
    for function in (
        "get_settings",
        "save_settings",
        "get_marketing_consent",
        "set_marketing_consent",
        "create_campaign",
        "queue_campaign",
        "get_page_data",
        "create_draft",
        "preview_version",
        "queue_test_send",
        "record_test_sent",
        "approve_legal",
        "activate_version",
        "set_mode",
        "get_dashboard",
    ):
        assert f"async def {function}(" in text


def test_v2_admin_backend_uses_one_repository_and_existing_environment_names():
    text = MODULE.read_text(encoding="utf-8")

    assert "ReactivationRepository" in text
    assert "ADMIN_SESSION_SECRET" in text
    assert "BUSINESS_ALERT_CHAT_ID" in text
    assert "REACTIVATION_TEST_CHAT_ID" not in text


def test_create_draft_wrapper_has_one_policy_input():
    signature = inspect.signature(reactivation_database.create_draft)

    assert signature.parameters["policy"].annotation in {
        ProgramPolicy,
        "ProgramPolicy",
    }
    assert signature.parameters["policy"].default is inspect.Parameter.empty
    assert {
        "inactivity_days",
        "reminder_after_days",
        "cooldown_days",
        "main_text",
        "reminder_text",
    }.isdisjoint(signature.parameters)
