import pytest
from django.core.cache import cache


@pytest.fixture(autouse=True)
def _fresh_cache():
    """Rate-limit counters live in the cache; start every test with a clean budget."""
    cache.clear()
    yield
    cache.clear()
