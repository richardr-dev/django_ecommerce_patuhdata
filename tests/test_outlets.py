"""Multi-outlet: Rp399.000 per extra outlet on Pro, single outlet on Starter, unlimited on
Custom; outlets bought mid-period, billed at renewal; pickup at an outlet from its own stock."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from apps.billing.models import Plan, Subscription, SubscriptionInvoice
from apps.billing.plans import allowed_outlets, plan_amount
from apps.billing.renewals import run_renewals
from apps.billing.services import (
    BillingError,
    mark_invoice_paid,
    outlet_purchase_quote,
    reduce_outlets,
    start_outlet_purchase,
)
from apps.catalog.models import InventoryLevel, Product, Warehouse
from apps.orders.models import Order
from apps.shops.models import Store


def _store(slug, plan_code, days_left=30, extra=0):
    owner = User.objects.create_user(f"{slug}-owner", email=f"{slug}@toko.id")
    store = Store.objects.create(owner=owner, name=slug.title(), slug=slug)
    Subscription.objects.create(store=store, plan=Plan.objects.get(code=plan_code),
        current_period_end=timezone.now() + timedelta(days=days_left, minutes=5),
        extra_outlets=extra)
    return store


def _outlet_form(name, **extra):
    return {"name": name, "code": name.upper(), "priority": 10, "is_active": "on",
        "is_outlet": "on", "address": f"Jl. {name}", **extra}


@override_settings(PAYMENT_PROVIDER="demo", ALLOW_DEMO_PAYMENTS=True)
class OutletBillingTests(TestCase):
    def _client(self, store):
        client = Client()
        client.force_login(store.owner)
        return client

    def test_starter_runs_one_outlet_and_is_pointed_to_pro(self):
        store = _store("starter", "free")
        client = self._client(store)
        client.post("/merchant/setup/warehouses/new/", _outlet_form("Kemang"))
        page = client.post("/merchant/setup/warehouses/new/", _outlet_form("Depok"))

        self.assertEqual(store.warehouses.filter(is_outlet=True).count(), 1)
        self.assertContains(page, "Multi-outlet tersedia di paket Pro")

    def test_pro_includes_one_outlet_and_charges_399k_for_each_extra(self):
        store = _store("pro", "pro")
        self.assertEqual(allowed_outlets(store), 1)
        pro = Plan.objects.get(code="pro")
        self.assertEqual(plan_amount(pro, 1, 2), Decimal(399000 * 3))
        self.assertEqual(plan_amount(pro, 12, 2), Decimal(3990000 * 3))

        page = self._client(store).post("/merchant/setup/warehouses/new/", _outlet_form("A"))
        self.assertEqual(page.status_code, 302)
        page = self._client(store).post("/merchant/setup/warehouses/new/", _outlet_form("B"))
        self.assertContains(page, "Tambah outlet di menu Langganan")

    def test_a_mid_period_outlet_is_charged_for_the_days_left(self):
        full = Subscription.objects.get(store=_store("penuh", "pro", days_left=30))
        half = Subscription.objects.get(store=_store("separuh", "pro", days_left=15))

        self.assertEqual(outlet_purchase_quote(full, 1)[0], Decimal(399000))
        self.assertEqual(outlet_purchase_quote(half, 2)[0], Decimal(399000))  # 2 × half a month
        self.assertEqual(outlet_purchase_quote(half, 1)[0], Decimal(200000))  # 199.500 rounded up
        with self.assertRaises(BillingError):
            outlet_purchase_quote(Subscription.objects.get(store=_store("s", "free")), 1)

    def test_paying_for_outlets_raises_the_limit_and_the_next_renewal(self):
        store = _store("tumbuh", "pro", days_left=5)
        run_renewals()
        renewal = SubscriptionInvoice.objects.get(kind="renewal")
        self.assertEqual(renewal.amount, Decimal(399000))

        invoice = start_outlet_purchase(store, 2, store.owner)
        mark_invoice_paid(invoice.id, amount=invoice.amount)
        store.refresh_from_db()

        self.assertEqual(store.subscription.extra_outlets, 2)
        self.assertEqual(allowed_outlets(store), 3)
        renewal.refresh_from_db()
        self.assertEqual((renewal.extra_outlets, renewal.amount), (2, Decimal(399000 * 3)))
        self.assertEqual(store.subscription.current_period_end.date(),
            (timezone.now() + timedelta(days=5)).date())  # the period itself is unchanged

    def test_buying_outlets_from_the_billing_page_opens_payment(self):
        store = _store("halaman", "pro")
        response = self._client(store).post("/merchant/billing/outlets/", {"action": "add",
            "count": "1"})
        invoice = SubscriptionInvoice.objects.get(kind="outlets")
        self.assertRedirects(response, f"/merchant/billing/{invoice.id}/demo-pay/",
            fetch_redirect_response=False)
        self.assertContains(self._client(store).get("/merchant/billing/"), "0 outlet aktif dari 1")

    def test_outlets_can_be_reduced_once_the_extra_ones_are_closed(self):
        store = _store("hemat", "pro", extra=2)
        for name in ("A", "B", "C"):
            Warehouse.objects.create(store=store, name=name, code=name, is_outlet=True)
        with self.assertRaisesMessage(BillingError, "Nonaktifkan dulu"):
            reduce_outlets(store, 1, 3)
        reduce_outlets(store, 1, 2)
        store.subscription.refresh_from_db()
        self.assertEqual(store.subscription.extra_outlets, 1)

    def test_moving_to_a_plan_without_outlets_drops_the_extras(self):
        store = _store("turun", "pro", extra=3)
        invoice = SubscriptionInvoice.objects.create(store=store, plan=Plan.objects.get(code="free"),
            amount=99000, provider="demo")
        mark_invoice_paid(invoice.id, amount=99000)
        store.subscription.refresh_from_db()
        self.assertEqual(store.subscription.extra_outlets, 0)

    def test_custom_has_no_outlet_limit(self):
        self.assertIsNone(allowed_outlets(_store("besar", "custom")))


@override_settings(PAYMENT_PROVIDER="demo", ALLOW_DEMO_PAYMENTS=True,
    SHIPPING_RATE_PROVIDER="biteship", BITESHIP_API_KEY="")
class OutletPickupTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("seller")
        self.store = Store.objects.create(owner=self.owner, name="Ruuma", slug="ruuma")
        self.product = Product.objects.create(store=self.store, name="Eclair", slug="eclair",
            sku="ECL", price=25000)
        self.main = Warehouse.objects.create(store=self.store, name="Gudang", code="MAIN",
            priority=1)
        self.kemang = Warehouse.objects.create(store=self.store, name="Outlet Kemang",
            code="KMG", priority=5, is_outlet=True, pickup_enabled=True,
            address="Jl. Kemang Raya 1", opening_hours="08.00–20.00")
        self.main_level = InventoryLevel.objects.create(warehouse=self.main, product=self.product,
            on_hand=10)
        self.kemang_level = InventoryLevel.objects.create(warehouse=self.kemang,
            product=self.product, on_hand=2)
        self.buyer = Client()
        self.buyer.force_login(User.objects.create_user("buyer"))

    def _checkout(self, quantity=1, **extra):
        self.buyer.post(f"/cart/add/{self.product.id}/", {"quantity": quantity})
        return self.buyer.post("/checkout/", {"customer_name": "Ayu", "phone": "081234567890",
            "shipping_method": f"outlet-{self.kemang.id}", **extra})

    def test_pickup_needs_no_address_and_takes_stock_from_that_outlet(self):
        self._checkout()
        order = Order.objects.get()
        self.assertEqual(order.pickup_outlet, self.kemang)
        self.assertEqual(order.shipping_cost, 0)
        self.assertIn("Outlet Kemang", order.shipping_address)
        self.kemang_level.refresh_from_db()
        self.main_level.refresh_from_db()
        self.assertEqual((self.kemang_level.reserved, self.main_level.reserved), (1, 0))

    def test_pickup_is_refused_when_that_outlet_lacks_stock(self):
        page = self._checkout(quantity=3)
        self.assertFalse(Order.objects.exists())
        self.assertRedirects(page, "/checkout/", fetch_redirect_response=False)

    def test_checkout_and_home_show_the_outlets(self):
        self.buyer.post(f"/cart/add/{self.product.id}/")
        self.assertContains(self.buyer.get("/checkout/"), "🏪 Outlet Kemang")
        home = self.buyer.get("/")
        self.assertContains(home, "Outlet kami")
        self.assertContains(home, "Petunjuk arah")
