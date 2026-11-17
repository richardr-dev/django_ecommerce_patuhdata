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
        self.assertRedirects(response, "/login/", fetch_redirect_response=False)

    def test_root_goes_to_the_dashboard_when_signed_in(self):
        self.client.force_login(self.owner)
        self.assertRedirects(self.client.get("/"), "/books/", fetch_redirect_response=False)

    def test_an_explicit_store_link_still_shows_the_storefront(self):
        response = self.client.get("/?store=kopi")
        self.assertNotEqual(response.status_code, 302)

    def test_a_stores_own_subdomain_keeps_its_storefront(self):
        with override_settings(PLATFORM_DOMAIN="lakuin.test", ALLOWED_HOSTS=["*"]):
            response = self.client.get("/", HTTP_HOST="kopi.lakuin.test")
            self.assertNotEqual(response.get("Location"), "/login/")

    @override_settings(HOME_IS_LOGIN=False)
    def test_the_switch_turns_it_off(self):
        self.assertNotEqual(self.client.get("/").get("Location"), "/login/")

    def test_the_login_page_itself_is_reachable_and_inner_paths_are_not_redirected(self):
        self.assertEqual(self.client.get("/login/").status_code, 200)
        self.assertNotEqual(self.client.get("/cart/").get("Location"), "/login/")

    def test_a_signed_in_owner_never_sees_the_login_form_again(self):
        self.client.force_login(self.owner)
        self.assertRedirects(self.client.get("/login/"), "/books/", fetch_redirect_response=False)

    def test_a_signed_in_owner_follows_a_safe_next_link(self):
        self.client.force_login(self.owner)
        response = self.client.get("/login/?next=/books/sales/invoices/")
        self.assertRedirects(response, "/books/sales/invoices/", fetch_redirect_response=False)

    def test_a_signed_in_shopper_goes_to_their_account_not_the_books(self):
        shopper = User.objects.create_user("buyer", password="pw")
        self.client.force_login(shopper)
        self.assertRedirects(self.client.get("/login/"), "/account/orders/", fetch_redirect_response=False)


class LoginPageTests(TestCase):
    def test_the_sign_in_page_is_a_plain_card_named_for_the_system(self):
        from django.conf import settings
        page = self.client.get("/login/")
        self.assertContains(page, "<title>Masuk · " + settings.PLATFORM_NAME)
        self.assertContains(page, 'name="username"')
        self.assertContains(page, 'name="password"')
        self.assertContains(page, "Lupa kata sandi?")
        self.assertNotContains(page, "Coba gratis")

    def test_a_wrong_password_shows_one_clear_message(self):
        User.objects.create_user("owner", password="pw")
        page = self.client.post("/login/", {"username": "owner", "password": "salah"})
        self.assertContains(page, "kata sandi salah")
        self.assertContains(page, 'role="alert"')

    def test_signing_in_opens_the_dashboard(self):
        User.objects.create_user("owner2", password="pw")
        response = self.client.post("/login/", {"username": "owner2", "password": "pw", "next": "/books/"})
        self.assertRedirects(response, "/books/", fetch_redirect_response=False)
