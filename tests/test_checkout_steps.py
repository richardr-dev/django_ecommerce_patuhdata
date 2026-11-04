"""Phone-friendly checkout: courier choices read like a marketplace's, and the stepped form is
saved in the session between steps and visits."""
from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings

from apps.catalog.models import InventoryLevel, Product, Warehouse
from apps.orders.couriers import present_quotes
from apps.orders.models import ShippingMethod
from apps.shops.models import Store


def quote(name, price, eta, code, service=""):
    return {"token": name, "name": name, "price": price, "eta": eta, "courier_code": code,
        "service_code": service}


class CourierPresentationTests(TestCase):
    def test_raw_courier_names_become_short_and_sorted(self):
        rows = present_quotes([
            quote("Jalur Nugraha Ekakurir (JNE) REG", 51000, "2 day", "jne", "REG"),
            quote("Jalur Nugraha Ekakurir (JNE) JTR<130", 2600000, "7 day", "jne", "JTR<130"),
            quote("J&T Express EZ", 41000, "", "J&T", "ez"),
            quote("POS Indonesia (POS) Pos Reguler", 45000, "5 day", "pos"),
            quote("SiCepat Reguler", 11000, "2 - 3 days", "sicepat", "reg"),
        ])
        self.assertEqual([(row["courier_name"], row["service"], row["eta_label"]) for row in rows], [
            ("SiCepat", "Reguler", "2–3 hari"), ("J&T", "EZ", ""), ("POS", "Reguler", "5 hari"),
            ("JNE", "Reguler", "2 hari"), ("JNE", "JTR<130", "7 hari")])
        self.assertEqual(rows[0]["tags"], ["Termurah"])
        self.assertEqual(rows[3]["tags"], ["Tercepat"])
        self.assertEqual(rows[1]["colour"], "#d6001c")


@override_settings(PAYMENT_PROVIDER="demo", ALLOW_DEMO_PAYMENTS=True,
    SHIPPING_RATE_PROVIDER="biteship", BITESHIP_API_KEY="")
class CheckoutDraftTests(TestCase):
    def setUp(self):
        self.store = Store.objects.create(owner=User.objects.create_user("seller"), name="Ruuma",
            slug="ruuma")
        self.product = Product.objects.create(store=self.store, name="Kopi", slug="kopi",
            sku="KOPI", price=50000)
        warehouse = Warehouse.objects.create(store=self.store, name="Gudang", code="G")
        InventoryLevel.objects.create(warehouse=warehouse, product=self.product, on_hand=5)
        ShippingMethod.objects.create(store=self.store, code="reg", name="Reguler", flat_rate=15000)
        self.buyer = Client()
        self.buyer.post(f"/cart/add/{self.product.id}/")

    def test_checkout_is_split_into_three_steps(self):
        page = self.buyer.get("/checkout/")
        self.assertContains(page, 'class="checkout-step" data-step="1"')
        self.assertContains(page, 'class="checkout-step" data-step="3"')
        self.assertContains(page, "Lanjut ke pengiriman")
        self.assertContains(page, 'data-initial-step="1"')

    def test_the_draft_is_kept_in_the_session_and_refills_the_form(self):
        self.buyer.post("/checkout/draft/", {"customer_name": "Ayu", "phone": "0812345678",
            "shipping_address": "Jl. Melati 1", "postal_code": "40115", "email": "ayu@example.com",
            "coupon_code": "HEMAT", "step": "2", "payment_method": "x", "evil": "<script>"})
        draft = self.buyer.session["checkout_draft"]
        self.assertEqual(draft["step"], 2)
        self.assertNotIn("payment_method", draft)
        self.assertNotIn("evil", draft)

        page = self.buyer.get("/checkout/")
        for value in ("Ayu", "0812345678", "Jl. Melati 1", "40115", "ayu@example.com", "HEMAT"):
            self.assertContains(page, value)
        self.assertContains(page, 'data-initial-step="2"')

    def test_placing_the_order_clears_the_draft(self):
        self.buyer.post("/checkout/draft/", {"customer_name": "Ayu", "step": "3"})
        self.buyer.post("/checkout/", {"customer_name": "Ayu", "phone": "081234567890",
            "shipping_address": "Jakarta", "shipping_method": "reg"})
        self.assertNotIn("checkout_draft", self.buyer.session)

    def test_the_draft_only_accepts_posts_for_a_store(self):
        self.assertEqual(self.buyer.get("/checkout/draft/").status_code, 405)
