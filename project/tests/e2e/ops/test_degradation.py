from pathlib import Path


PROJECT_ROOT = Path("/workspace")
if not PROJECT_ROOT.exists():
    PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_smoke_script_covers_health_privacy_faq_booking_and_admin_login():
    smoke = (PROJECT_ROOT / "ops" / "smoke.ps1").read_text(encoding="utf-8")

    assert "PUBLIC_BASE_URL" in smoke
    assert "TELEGRAM_WEBHOOK_SECRET" in smoke
    assert "X-Telegram-Bot-Api-Secret-Token" in smoke
    assert "/telegram/webhook" in smoke
    assert "privacy" in smoke
    assert "faq" in smoke
    assert "booking" in smoke
    assert "/admin/login" in smoke
    assert "throw" in smoke


def test_load_script_limits_production_v1_target_rate():
    load = (PROJECT_ROOT / "ops" / "load" / "k6.js").read_text(encoding="utf-8")

    assert "constant-arrival-rate" in load
    assert "rate: 30" in load
    assert "preAllocatedVUs: 20" in load
    assert "TELEGRAM_WEBHOOK_SECRET" in load
    assert "X-Telegram-Bot-Api-Secret-Token" in load
    assert "/telegram/webhook" in load
    assert "http_req_failed" in load


def test_failure_gate_documents_redis_rabbitmq_yclients_and_llm_outages():
    checklist = (PROJECT_ROOT / "ops" / "failure-gates.md").read_text(encoding="utf-8")

    for component in ("Redis", "RabbitMQ", "YCLIENTS", "primary LLM"):
        assert component in checklist
    assert "no lost confirmed state" in checklist
    assert "visible delay status" in checklist
    assert "recovery after component restart" in checklist
