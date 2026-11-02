"""Self-serve multi-tenant platform: host routing, seller signup, plans and subscription billing."""
import io
import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from apps.billing.models import Plan, Subscription, SubscriptionInvoice
from apps.catalog.models import Product, Warehouse
from apps.integrations.payments import doku_signature
from apps.orders.models import Order, ShippingMethod
from apps.shops.models import Organization, Store, StoreStaff
from apps.shops.tenancy import store_origin

PLATFORM = "niaga.test"


def _store(slug, owner=None, plan=None, **fields):
    owner = owner or User.objects.create_user(f"{slug}-owner", email=f"{slug}@example.com")
    store = Store.objects.create(owner=owner, name=slug.title(), slug=slug, **fields)
    if plan:
        Subscription.objects.create(store=store, plan=plan, current_period_end=(
            timezone.now() + timedelta(days=10) if plan.is_paid else None))
    return store


def _host(name):
    return Client(HTTP_HOST=name)


@override_settings(PLATFORM_DOMAIN=PLATFORM, ALLOWED_HOSTS=[PLATFORM, f".{PLATFORM}", "kopi.example"],
    PUBLIC_BASE_URL=f"https://{PLATFORM}")
class HostRoutingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.free, cls.pro = Plan.objects.get(code="free"), Plan.objects.get(code="pro")
        cls.kopi = _store("kopi", plan=cls.pro, domain="kopi.example")
        cls.teh = _store("teh", plan=cls.free)
        Product.objects.create(store=cls.kopi, name="Gayo", slug="gayo", sku="GAYO", price=50000)

    def test_platform_host_shows_platform_home_not_a_store(self):
        response = _host(PLATFORM).get("/")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "platform/home.html")
        self.assertContains(response, "Coba gratis 14 hari")
        self.assertContains(response, "https://kopi.example/")

    def test_subdomain_serves_that_store_only(self):
        response = _host(f"teh.{PLATFORM}").get("/")
        self.assertEqual(response.context["store"], self.teh)
        # A query parameter cannot switch a subdomain to another seller's store.
        self.assertEqual(_host(f"teh.{PLATFORM}").get("/?store=kopi").context["store"], self.teh)

    def test_unknown_or_inactive_store_is_not_found(self):
        self.assertEqual(_host(f"nope.{PLATFORM}").get("/").status_code, 404)
        Store.objects.filter(id=self.teh.id).update(is_active=False)
        self.assertEqual(_host(f"teh.{PLATFORM}").get("/").status_code, 404)

    def test_custom_domain_requires_an_entitled_plan(self):
        self.assertEqual(_host("kopi.example").get("/").context["store"], self.kopi)
        Subscription.objects.filter(store=self.kopi).update(
            current_period_end=timezone.now() - timedelta(days=1))
        response = _host("kopi.example").get("/products/gayo/?ref=ig")
        self.assertRedirects(response, f"https://kopi.{PLATFORM}/products/gayo/?ref=ig",
            fetch_redirect_response=False)

    @override_settings(ALLOWED_HOSTS=["*"])
    def test_unknown_host_never_reaches_views(self):
        # With ALLOWED_HOSTS=* a forged Host header must not produce, e.g., a password reset
        # email linking to the attacker's domain.
        User.objects.create_user("victim", email="victim@example.com")
        response = _host("evil.example").post("/account/password-reset/", {"email": "victim@example.com"})
        self.assertEqual(response.status_code, 404)
        self.assertTemplateUsed(response, "platform/store_not_found.html")

    def test_mobile_clients_name_the_store_on_the_platform_host(self):
        client = Client(HTTP_HOST=PLATFORM, HTTP_X_STORE_SLUG="teh")
        self.assertEqual(client.get("/cart/").context["store"], self.teh)

    def test_payment_return_url_uses_the_store_host(self):
        self.assertEqual(store_origin(self.teh), f"https://teh.{PLATFORM}")
        self.assertEqual(store_origin(self.kopi), "https://kopi.example")

    def test_tls_ask_only_approves_hosts_the_platform_serves(self):
        client = Client(HTTP_HOST="web:8000")  # Caddy's internal call; not in ALLOWED_HOSTS
        def ask(domain):
            return client.get("/internal/tls-ask/", {"domain": domain}).status_code
        self.assertEqual(ask(PLATFORM), 200)
        self.assertEqual(ask(f"www.{PLATFORM}"), 200)
        self.assertEqual(ask(f"teh.{PLATFORM}"), 200)
        self.assertEqual(ask(f"ghost.{PLATFORM}"), 404)
        self.assertEqual(ask("kopi.example"), 200)
        self.assertEqual(ask("random.example"), 404)
        self.assertEqual(ask(""), 404)
        Store.objects.filter(id=self.teh.id).update(domain="teh.example")
        self.assertEqual(ask("teh.example"), 200)  # Starter includes a custom domain
        Subscription.objects.filter(store=self.teh).update(
            current_period_end=timezone.now() - timedelta(days=1))
        self.assertEqual(ask("teh.example"), 404)  # lapsed: no custom domain

    def test_directory_lists_shoppable_opted_in_stores(self):
        response = _host(PLATFORM).get("/stores/")
        names = [item["store"].name for item in response.context["stores"]]
        self.assertEqual(names, ["Kopi"])  # Teh has no active product
        Store.objects.filter(id=self.kopi.id).update(listed_in_directory=False)
        self.assertEqual(list(_host(PLATFORM).get("/stores/").context["stores"]), [])


@override_settings(PLATFORM_DOMAIN=PLATFORM, ALLOWED_HOSTS=[PLATFORM, f".{PLATFORM}"],
    PUBLIC_BASE_URL=f"https://{PLATFORM}")
class SignupTests(TestCase):
    form = {"store_name": "Batik Sari", "slug": "batik-sari", "phone": "+62 812-3456-7890",
        "full_name": "Sari Wulandari", "email": "Sari@Example.com",
        "password1": "Batik-Sari-2026!", "password2": "Batik-Sari-2026!", "accept_terms": "on"}

    def test_signup_creates_a_ready_store_on_the_free_plan(self):
        client = _host(PLATFORM)
        response = client.post("/start/", self.form)
        self.assertRedirects(response, "/merchant/settings/", fetch_redirect_response=False)
        store = Store.objects.get(slug="batik-sari")
        user = User.objects.get(username="sari@example.com")
        self.assertEqual((store.owner, store.organization.owner), (user, user))
        self.assertEqual(store.support_phone, "081234567890")
        self.assertEqual(store.subscription.plan.code, "free")
        self.assertTrue(Warehouse.objects.filter(store=store, code="MAIN").exists())
        self.assertTrue(ShippingMethod.objects.filter(store=store, code="reguler").exists())
        dashboard = client.get("/merchant/billing/")
        self.assertContains(dashboard, f"https://batik-sari.{PLATFORM}/")
        self.assertEqual(_host(f"batik-sari.{PLATFORM}").get("/").context["store"], store)

    def test_reserved_and_taken_addresses_are_rejected(self):
        _store("kopi")
        for slug in ("www", "admin", "kopi", "-bad", "a", "Has Space"):
            response = _host(PLATFORM).post("/start/", {**self.form, "slug": slug})
            self.assertEqual(response.status_code, 200, slug)
            self.assertIn("slug", response.context["form"].errors, slug)
        self.assertFalse(User.objects.filter(username="sari@example.com").exists())

    def test_signed_in_owner_opens_another_brand_without_a_new_account(self):
        owner = User.objects.create_user("owner@example.com", email="owner@example.com")
        _store("pertama", owner=owner, plan=Plan.objects.get(code="free"))
        Organization.objects.create(name="Taken", slug="kedua", owner=owner)
        client = _host(PLATFORM)
        client.force_login(owner)
        response = client.post("/start/", {"store_name": "Kedua", "slug": "kedua",
            "phone": "081200000001", "accept_terms": "on"})
        self.assertRedirects(response, "/merchant/settings/", fetch_redirect_response=False)
        store = Store.objects.get(slug="kedua")
        self.assertEqual(store.owner, owner)
        self.assertEqual(store.organization.slug, "kedua-2")
        self.assertEqual(client.session["merchant_store_id"], store.id)

    def test_signup_on_a_store_host_moves_to_the_platform(self):
        _store("kopi")
        response = _host(f"kopi.{PLATFORM}").get("/start/")
        self.assertRedirects(response, f"https://{PLATFORM}/start/", fetch_redirect_response=False)

    def test_signup_closed_without_a_default_plan(self):
        Plan.objects.update(is_default=False)
        response = _host(PLATFORM).post("/start/", self.form)
        self.assertContains(response, "belum dibuka")
        self.assertFalse(Store.objects.filter(slug="batik-sari").exists())


class PlanLimitTests(TestCase):
    def setUp(self):
        self.plan = Plan.objects.get(code="free")
        Plan.objects.filter(id=self.plan.id).update(max_products=2)
        self.store = _store("limit", plan=Plan.objects.get(code="free"))
        self.client.force_login(self.store.owner)
        for index in range(2):
            Product.objects.create(store=self.store, name=f"P{index}", slug=f"p{index}",
                sku=f"P{index}", price=1000)

    def _new_product(self, **extra):
        return self.client.post("/merchant/setup/products/new/", {"name": "Baru", "slug": "baru",
            "sku": "BARU", "price": "1000", "weight_grams": 0, "length_cm": 0, "width_cm": 0,
            "height_cm": 0, **extra})

    def test_plan_caps_active_products(self):
        response = self._new_product(is_active="on")
        self.assertContains(response, "maksimal 2 produk aktif")
        self.assertFalse(Product.objects.filter(sku="BARU").exists())
        # An inactive draft does not count, so merchants can prepare products before upgrading.
        self.assertEqual(self._new_product().status_code, 302)

    def test_csv_import_respects_the_cap(self):
        csv = "name,sku,price,stock\nP0,P0,1000,1\nBaru,BARU,1000,1\n"
        upload = io.BytesIO(csv.encode())
        upload.name = "products.csv"
        response = self.client.post("/merchant/products/import/", {"file": upload}, follow=True)
        self.assertContains(response, "maksimal 2 produk aktif")
        self.assertFalse(Product.objects.filter(sku="BARU").exists())

    def test_operator_managed_store_without_subscription_is_unlimited(self):
        self.store.subscription.delete()
        self.assertEqual(self._new_product(is_active="on").status_code, 302)


@override_settings(PAYMENT_PROVIDER="demo")
class BillingTests(TestCase):
    def setUp(self):
        self.pro = Plan.objects.get(code="pro")
        self.store = _store("langganan", plan=Plan.objects.get(code="free"))
        self.client.force_login(self.store.owner)

    def _checkout(self, months=1):
        response = self.client.post("/merchant/billing/checkout/", {"plan": "pro", "months": months})
        return response, SubscriptionInvoice.objects.filter(store=self.store).first()

    def test_demo_upgrade_activates_and_renewal_extends(self):
        response, invoice = self._checkout()
        self.assertRedirects(response, f"/merchant/billing/{invoice.id}/demo-pay/")
        self.assertEqual(invoice.amount, Decimal(399000))
        self.client.post(response["Location"])
        subscription = Subscription.objects.get(store=self.store)
        self.assertEqual(subscription.plan, self.pro)
        first_end = subscription.current_period_end
        self.assertAlmostEqual(first_end, timezone.now() + timedelta(days=30), delta=timedelta(minutes=1))

        response, renewal = self._checkout(months=12)
        self.assertEqual(renewal.amount, Decimal(3990000))
        self.client.post(response["Location"])
        self.client.post(response["Location"])  # paying twice must not extend twice
        subscription.refresh_from_db()
        self.assertEqual(subscription.current_period_end, first_end + timedelta(days=365))

    def test_demo_payment_page_is_closed_once_a_real_gateway_is_configured(self):
        response, _invoice = self._checkout()
        with override_settings(PAYMENT_PROVIDER="doku"):
            self.assertEqual(self.client.post(response["Location"]).status_code, 404)
        self.assertEqual(Subscription.objects.get(store=self.store).plan.code, "free")

    def test_only_owner_admin_or_finance_manage_billing(self):
        viewer = User.objects.create_user("viewer")
        StoreStaff.objects.create(store=self.store, user=viewer, role="viewer")
        client = Client()
        client.force_login(viewer)
        self.assertEqual(client.get("/merchant/billing/").status_code, 404)
        self.assertEqual(client.post("/merchant/billing/checkout/", {"plan": "pro"}).status_code, 404)

    def test_custom_plan_cannot_be_checked_out(self):
        response = self.client.post("/merchant/billing/checkout/", {"plan": "custom"}, follow=True)
        self.assertContains(response, "tidak tersedia")
        self.assertFalse(SubscriptionInvoice.objects.exists())


@override_settings(PAYMENT_PROVIDER="doku", DOKU_CLIENT_ID="MCH-TEST", DOKU_SECRET_KEY="doku-secret",
    DOKU_API_URL="https://api-sandbox.doku.com", PUBLIC_BASE_URL="https://niaga.test")
class DokuSubscriptionTests(TestCase):
    def setUp(self):
        self.store = _store("doku", plan=Plan.objects.get(code="free"))
        self.client.force_login(self.store.owner)

    @patch("apps.integrations.payments.request_json",
        return_value={"response": {"payment": {"url": "https://checkout.doku.test/pay"}}})
    def _start(self, request_json):
        response = self.client.post("/merchant/billing/checkout/", {"plan": "pro", "months": 1})
        self.assertRedirects(response, "https://checkout.doku.test/pay", fetch_redirect_response=False)
        payload = json.loads(request_json.call_args.kwargs["encoded_payload"])
        self.assertEqual(payload["order"]["amount"], 399000)
        self.assertEqual(payload["callbacks"]["url"], "https://niaga.test/merchant/billing/")
        return SubscriptionInvoice.objects.get(store=self.store)

    def _notify(self, invoice, amount, request_id="sub-notification-1"):
        body = json.dumps({"order": {"invoice_number": invoice.payment_reference, "amount": amount,
            "currency": "IDR"}, "transaction": {"status": "SUCCESS"}}, separators=(",", ":")).encode()
        timestamp = "2026-10-30T12:00:00Z"
        signature = doku_signature("MCH-TEST", request_id, timestamp, "/integrations/webhooks/doku/",
            body, "doku-secret")
        return Client().post("/integrations/webhooks/doku/", data=body, content_type="application/json",
            HTTP_CLIENT_ID="MCH-TEST", HTTP_REQUEST_ID=request_id, HTTP_REQUEST_TIMESTAMP=timestamp,
            HTTP_SIGNATURE=signature)

    def test_signed_notification_activates_the_plan_once(self):
        invoice = self._start()
        self.assertTrue(invoice.payment_reference.startswith("SB"))
        self.assertEqual(self._notify(invoice, 399000).status_code, 200)
        self.assertEqual(self._notify(invoice, 399000).json(), {"received": True, "duplicate": True})
        subscription = Subscription.objects.get(store=self.store)
        self.assertEqual(subscription.plan.code, "pro")
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, "paid")
        self.assertFalse(Order.objects.exists())

    def test_wrong_amount_does_not_activate(self):
        invoice = self._start()
        self.assertEqual(self._notify(invoice, 1000).status_code, 400)
        self.assertEqual(Subscription.objects.get(store=self.store).plan.code, "free")


@override_settings(PLATFORM_DOMAIN="localhost", PUBLIC_BASE_URL="https://tunnel.ngrok.test",
    PLATFORM_BASE_URL="http://localhost:8000", ALLOWED_HOSTS=["*"],
    PLATFORM_COMPANY_NAME="PT Patuhdata Solusi Nusantara")
class LocalPlatformTests(TestCase):
    """The local Docker setup: browse localhost, receive payment webhooks through a tunnel."""
    @classmethod
    def setUpTestData(cls):
        cls.store = _store("kopi-kita")
        Product.objects.create(store=cls.store, name="Gayo", slug="gayo", sku="GAYO", price=50000)

    def test_localhost_is_the_landing_page_and_the_store_lives_on_its_subdomain(self):
        home = _host("localhost:8000").get("/")
        self.assertTemplateUsed(home, "platform/home.html")
        self.assertIsNone(home.context["store"])
        self.assertContains(home, "http://kopi-kita.localhost:8000/")
        self.assertContains(home, "© ")
        self.assertContains(home, "PT Patuhdata Solusi Nusantara. Hak cipta dilindungi")
        store = _host("kopi-kita.localhost:8000").get("/")
        self.assertEqual(store.context["store"], self.store)
        self.assertContains(store, "PT Patuhdata Solusi Nusantara")

    @override_settings(DOKU_CLIENT_ID="MCH-TEST", DOKU_SECRET_KEY="doku-secret")
    def test_webhook_tunnel_host_counts_as_the_platform(self):
        response = _host("tunnel.ngrok.test").post("/integrations/webhooks/doku/", data="{}",
            content_type="application/json")
        self.assertEqual(response.status_code, 401)  # reached the webhook, which rejects unsigned calls

    def test_platform_legal_pages_name_the_operator(self):
        for path in ("/terms/", "/privacy/"):
            response = _host("localhost:8000").get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertContains(response, "PT Patuhdata Solusi Nusantara")
        self.assertContains(_host("localhost:8000").get("/start/"), 'href="/terms/"')


@override_settings(PLATFORM_DOMAIN="lakuin.store", PUBLIC_BASE_URL="https://lakuin.store",
    ALLOWED_HOSTS=["*"])
class ProductionDomainTests(TestCase):
    def test_www_redirects_permanently_to_the_apex(self):
        response = _host("www.lakuin.store").get("/stores/?q=kopi")
        self.assertEqual(response.status_code, 301)
        self.assertEqual(response["Location"], "https://lakuin.store/stores/?q=kopi")

    def test_brand_names_cannot_be_claimed_as_store_addresses(self):
        form = {"store_name": "Palsu", "slug": "lakuin", "phone": "081200000000",
            "full_name": "X", "email": "x@example.com", "password1": "Lakuin-Palsu-2026!",
            "password2": "Lakuin-Palsu-2026!", "accept_terms": "on"}
        response = _host("lakuin.store").post("/start/", form)
        self.assertIn("slug", response.context["form"].errors)


@override_settings(PAYMENT_PROVIDER="demo", ALLOW_DEMO_PAYMENTS=False)
class DemoPaymentsDisabledInProductionTests(TestCase):
    def setUp(self):
        self.store = _store("prod", plan=Plan.objects.get(code="free"))
        self.client.force_login(self.store.owner)

    def test_subscription_checkout_is_refused(self):
        response = self.client.post("/merchant/billing/checkout/", {"plan": "pro", "months": 1},
            follow=True)
        self.assertContains(response, "belum dikonfigurasi")
        self.assertFalse(SubscriptionInvoice.objects.exists())

    def test_demo_payment_pages_are_closed(self):
        invoice = SubscriptionInvoice.objects.create(store=self.store, plan=Plan.objects.get(code="pro"),
            amount=Decimal(99000), provider="demo")
        self.assertEqual(self.client.post(f"/merchant/billing/{invoice.id}/demo-pay/").status_code, 404)
        self.assertEqual(Subscription.objects.get(store=self.store).plan.code, "free")

    def test_demo_provider_refuses_to_create_a_payment(self):
        from apps.integrations.http import ProviderError
        from apps.integrations.payments import DemoPaymentProvider
        with self.assertRaises(ProviderError):
            DemoPaymentProvider().create_payment(Order(store=self.store))


class StoreSocialLinksTests(TestCase):
    def test_whatsapp_and_instagram_links(self):
        store = Store(support_phone="0812-3456-7890", instagram="batiksari.id")
        self.assertEqual(store.whatsapp_url, "https://wa.me/6281234567890")
        self.assertEqual(store.instagram_url, "https://www.instagram.com/batiksari.id/")
        self.assertEqual(Store(support_phone="").whatsapp_url, "")

    def test_instagram_handle_is_normalised(self):
        from django import forms

        from apps.shops.forms import clean_instagram_handle
        for raw in ("@batiksari.id", "https://www.instagram.com/batiksari.id/?hl=id", "batiksari.id"):
            self.assertEqual(clean_instagram_handle(raw), "batiksari.id", raw)
        with self.assertRaises(forms.ValidationError):
            clean_instagram_handle("not a handle!")

    @override_settings(PLATFORM_DOMAIN=PLATFORM, ALLOWED_HOSTS=[f".{PLATFORM}", PLATFORM])
    def test_signup_instagram_shows_as_icons_on_the_storefront(self):
        _host(PLATFORM).post("/start/", {**SignupTests.form, "instagram": "@batik.sari"})
        store = Store.objects.get(slug="batik-sari")
        self.assertEqual(store.instagram, "batik.sari")
        page = _host(f"batik-sari.{PLATFORM}").get("/")
        self.assertContains(page, 'href="https://wa.me/6281234567890"', count=2)  # footer + chat button
        self.assertContains(page, 'href="https://www.instagram.com/batik.sari/"')
        self.assertContains(page, "@batik.sari")


@override_settings(PLATFORM_DOMAIN="localhost", PLATFORM_DISPLAY_DOMAIN="lakuin.store",
    ALLOWED_HOSTS=["*"])
class SignupAddressDisplayTests(TestCase):
    def test_signup_shows_the_public_store_address(self):
        page = _host("localhost:8000").get("/start/")
        self.assertContains(page, "<span>.lakuin.store</span>", html=False)
        self.assertContains(page, "https://<span data-slug-preview>nama-toko</span>.lakuin.store")
        self.assertNotContains(page, "&lt;alamat&gt;")
        self.assertNotContains(page, ".localhost</span>")
