"""The system's address opens on the back-office login; stores keep their own storefront."""
import pytest
from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from apps.shops.models import Store


@pytest.fixture(autouse=True)
def _login_first(settings):
    settings.HOME_IS_LOGIN = True


class LoginFirstTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner", password="pw")
        self.store = Store.objects.create(owner=self.owner, name="Kopi", slug="kopi", is_active=True)

    def test_root_goes_to_login_when_signed_out(self):
        response = self.client.get("/")
        self.assertRedirects(response, "/merchant/login/", fetch_redirect_response=False)

    def test_root_goes_to_the_dashboard_when_signed_in(self):
        self.client.force_login(self.owner)
        self.assertRedirects(self.client.get("/"), "/merchant/", fetch_redirect_response=False)

    def test_an_explicit_store_link_still_shows_the_storefront(self):
        response = self.client.get("/?store=kopi")
        self.assertNotEqual(response.status_code, 302)

    def test_a_stores_own_subdomain_keeps_its_storefront(self):
        with override_settings(PLATFORM_DOMAIN="lakuin.test", ALLOWED_HOSTS=["*"]):
            response = self.client.get("/", HTTP_HOST="kopi.lakuin.test")
            self.assertNotEqual(response.get("Location"), "/merchant/login/")

    @override_settings(HOME_IS_LOGIN=False)
    def test_the_switch_turns_it_off(self):
        self.assertNotEqual(self.client.get("/").get("Location"), "/merchant/login/")

    def test_the_login_page_itself_is_reachable_and_inner_paths_are_not_redirected(self):
        self.assertEqual(self.client.get("/merchant/login/").status_code, 200)
        self.assertNotEqual(self.client.get("/cart/").get("Location"), "/merchant/login/")
