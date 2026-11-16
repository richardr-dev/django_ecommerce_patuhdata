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


@pytest.fixture(autouse=True)
def _storefront_on(settings):
    """The built-in online shop is off by default; its tests run with it on (off: test_no_storefront.py)."""
    settings.STOREFRONT_ENABLED = True


@pytest.fixture(autouse=True)
def _home_is_storefront(settings):
    """Storefront tests open "/"; the login-first home has its own tests (test_login_first.py)."""
    settings.HOME_IS_LOGIN = False
