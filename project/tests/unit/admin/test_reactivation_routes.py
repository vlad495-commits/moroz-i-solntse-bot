from pathlib import Path

import reactivation_routes


ADMIN = Path("/workspace/admin")

EXPECTED_POSTS = {
    "/marketing/versions",
    "/marketing/versions/{version_id}/preview",
    "/marketing/versions/{version_id}/test",
    "/marketing/versions/{version_id}/activate",
    "/marketing/legal",
    "/marketing/mode",
    "/marketing/consents/{consent_id}/revoke",
}


def test_marketing_route_map_is_exact():
    routes = {
        route.path
        for route in reactivation_routes.router.routes
        if "POST" in route.methods
    }

    assert routes == EXPECTED_POSTS
    assert {
        route.path for route in reactivation_routes.legacy_router.routes
    } == {"/reactivation/"}


def test_marketing_navigation_and_focused_page_contract():
    base = (ADMIN / "templates" / "base.html").read_text(encoding="utf-8")
    html = (ADMIN / "templates" / "reactivation.html").read_text(encoding="utf-8")

    assert "/marketing/" in base
    assert "Маркетинговые коммуникации" in base
    assert "/reactivation/" not in base
    for label in (
        "Реактивация клиентов",
        "Готовность к запуску",
        "Правила и тексты",
        "Предпросмотр",
        "Исключены по причинам",
        "Замаскированные примеры",
        "Пути реактивации",
        "Воронка результата",
        "История согласий",
        "Найти по ID согласия",
        "Страница",
        "Отозвать согласие",
        "Архив старой версии",
        "Черновая версия, реальные сообщения не отправлялись",
        "АКТИВИРОВАТЬ",
    ):
        assert label in html
    for forbidden in (
        "Рассылки",
        "Новая кампания",
        "Скидка, %",
        "Инструкция для LLM",
        "Записать согласие",
    ):
        assert forbidden not in html
