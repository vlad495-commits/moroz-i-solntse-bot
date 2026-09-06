import inspect

from moroz.booking import catalog
from worker.main import MessageTaskHandler


def test_catalog_has_only_technical_booking_interface():
    assert not hasattr(catalog.CatalogRepository, "ground")
    assert not hasattr(catalog, "match_catalog")
    assert not hasattr(catalog, "CatalogGrounding")
    assert callable(catalog.CatalogRepository.list_services)
    assert callable(catalog.CatalogSyncCoordinator.run)
    parameters = inspect.signature(MessageTaskHandler).parameters
    assert "catalog_grounding_enabled" not in parameters
    assert "catalog_repository" not in parameters
