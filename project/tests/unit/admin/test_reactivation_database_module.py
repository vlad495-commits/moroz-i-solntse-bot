from pathlib import Path


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
    ):
        assert f"async def {function}(" in text
