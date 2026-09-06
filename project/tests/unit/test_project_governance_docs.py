from pathlib import Path


ROOT = Path("/repo")


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


ARCHIVE = "docs/archive/2026-09-06-current-version-cleanup"


def test_manual_plan_is_the_only_current_user_document() -> None:
    manual = read("План ручного тестирования.md")
    agents = read("AGENTS.md")
    assert "# Базовый план ручного тестирования" in manual
    assert "единственный актуальный пользовательский план" in agents
    assert not (ROOT / "Дорожная карта.md").exists()


def test_previous_root_documents_are_archived() -> None:
    for relative in (
        "ТЗ и архитектура.md",
        "План реализации.md",
        "checklist.md",
        "Вопросы Свете.md",
        "Уточнить.md",
    ):
        assert (ROOT / ARCHIVE / relative).is_file(), relative


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
