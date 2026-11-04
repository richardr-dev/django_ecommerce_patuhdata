import pytest
from django.core.cache import cache


@pytest.fixture(autouse=True)
def _fresh_cache():
    """Rate-limit counters live in the cache; start every test with a clean budget."""
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def _storefront_open(settings):
    """Most tests build a bare store; the go-live gate has its own tests (test_store_gate.py)."""
    settings.STOREFRONT_GATE = False
