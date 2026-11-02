"""A new store is live on its own subdomain at once, and its address leads to its dashboard."""
import test_platform
from django.test import TestCase, override_settings
from test_platform import PLATFORM, _host, _store

from apps.shops.models import Store


@override_settings(PLATFORM_DOMAIN=PLATFORM, ALLOWED_HOSTS=[PLATFORM, f".{PLATFORM}"],
    PUBLIC_BASE_URL=f"https://{PLATFORM}")
class StoreAddressTests(TestCase):
    def test_signup_makes_the_subdomain_eligible_for_https_immediately(self):
        tls = _host("web:8000")
        self.assertEqual(tls.get("/internal/tls-ask/",
            {"domain": f"batik-sari.{PLATFORM}"}).status_code, 404)

        _host(PLATFORM).post("/start/", test_platform.SignupTests.form)

        self.assertEqual(tls.get("/internal/tls-ask/",
            {"domain": f"batik-sari.{PLATFORM}"}).status_code, 200)
        self.assertEqual(_host(f"batik-sari.{PLATFORM}").get("/").status_code, 200)

    def test_back_office_paths_on_a_store_address_open_that_stores_dashboard(self):
        store = _store("kopi")
        client = _host(f"kopi.{PLATFORM}")

        response = client.get("/merchant/orders/", {"q": "Budi"})
        self.assertRedirects(response, f"https://{PLATFORM}/merchant/orders/?q=Budi&store={store.id}",
            fetch_redirect_response=False)
        self.assertRedirects(client.get("/dashboard"),
            f"https://{PLATFORM}/merchant/?store={store.id}", fetch_redirect_response=False)

    def test_platform_host_back_office_is_not_redirected(self):
        _store("kopi")
        response = _host(PLATFORM).get("/merchant/", HTTP_X_STORE_SLUG="kopi")

        self.assertNotIn("store=", response.headers.get("Location", ""))

    def test_dashboard_link_switches_the_owner_to_that_store(self):
        first = _store("pertama")
        second = Store.objects.create(owner=first.owner, name="Kedua", slug="kedua")
        client = _host(PLATFORM)
        client.force_login(first.owner)

        response = client.get(f"/merchant/?store={second.id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(client.session["merchant_store_id"], second.id)
