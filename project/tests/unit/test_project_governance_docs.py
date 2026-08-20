from pathlib import Path


ROOT = Path("/repo")


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_roadmap_is_the_only_current_status_source() -> None:
    roadmap = read("Дорожная карта.md")
    agents = read("AGENTS.md")
    assert "## Где мы сейчас" in roadmap
    assert "## Активная работа" in roadmap
    assert "## Блокеры" in roadmap
    assert "## Что нужно от владельца" in roadmap
    assert "## Дальше: Now / Next / Later" in roadmap
    assert "Текущая ступень" not in agents
    assert "единственный источник текущего статуса" in agents


def test_static_documents_have_one_role() -> None:
    for relative in (
        "ТЗ и архитектура.md",
        "План реализации.md",
        "changelog.md",
        "checklist.md",
    ):
        body = read(relative)
        assert "Роль документа" in body, relative
        assert "Дорожная карта.md" in body, relative


def test_history_and_governance_manual_exist() -> None:
    assert (ROOT / "docs/archive/roadmap-history-through-2026-08-20.md").is_file()
    manual = read("docs/project/Система управления проектом.md")
    assert "Один вопрос — один источник правды" in manual
    assert "Происхождение документов" in manual
    assert "Идея / референс" in manual


def test_volodya_audit_has_current_disposition() -> None:
    audit = read("docs/audits/Аудит решений бота Володи 2026-08-13.md")
    assert "Актуализация статусов" in audit
    assert "Реализовано или закрыто нашей реализацией" in audit
    assert "Остаётся в продуктовой очереди" in audit
    assert "Исключено или не переносится" in audit
