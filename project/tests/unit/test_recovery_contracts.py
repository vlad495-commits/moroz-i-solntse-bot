from pathlib import Path


ROOT = Path("/workspace")
EXPECTED_START_REPLY = (
    "Здравствуйте! Я онлайн-ассистент центра загара и криотерапии "
    "«Мороз и Солнце» ❄️☀️"
)


def test_start_reply_represents_the_center_in_config_and_compose() -> None:
    config = (ROOT / "llm" / "config.py").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert EXPECTED_START_REPLY in config
    assert EXPECTED_START_REPLY in compose
    assert "Напишите вопрос своими словами, например:" in config
    assert "Напишите вопрос своими словами, например:" in compose
    assert "Выберите действие в меню" not in config
    assert "Выберите действие в меню" not in compose


def test_current_system_prompt_has_no_removed_runtime_claims() -> None:
    prompt = (ROOT / "llm" / "prompts" / "system.md").read_text(
        encoding="utf-8"
    )

    assert "catalog_browse" not in prompt
    assert "на staging не развёрнут" not in prompt

    router = (ROOT / "src" / "moroz" / "messaging" / "router.py").read_text(
        encoding="utf-8"
    )
    assert "mode=catalog_browse" not in router
