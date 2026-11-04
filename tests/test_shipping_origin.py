"""Each seller ships from their own warehouse: Biteship rates and courier bookings start from the
store's warehouse postal code, not one platform-wide origin."""
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import Client, TestCase, override_settings

from apps.catalog.models import InventoryLevel, Product, Warehouse
from apps.integrations.shipping import shipping_origin
from apps.orders.models import ShippingMethod
from apps.shops.models import Store

RATES = {"pricing": [{"courier_code": "jne", "courier_service_code": "reg",
    "courier_name": "JNE", "courier_service_name": "Reguler", "price": 12000,
    "duration": "1 - 2 days"}]}


@override_settings(SHIPPING_RATE_PROVIDER="biteship", BITESHIP_API_KEY="biteship_test.key",
    BITESHIP_ORIGIN_POSTAL_CODE="", PAYMENT_PROVIDER="demo", ALLOW_DEMO_PAYMENTS=True)
class ShippingOriginTests(TestCase):
    def setUp(self):
        self.store = Store.objects.create(owner=User.objects.create_user("seller"), name="Ruuma",
            slug="ruuma", support_phone="0811111111")
        self.product = Product.objects.create(store=self.store, name="Kopi", slug="kopi",
            sku="KOPI", price=50000, weight_grams=500)
        self.warehouse = Warehouse.objects.create(store=self.store, name="Gudang Bandung",
            code="BDG", address="Jl. Braga 1, Bandung", phone="0822222222", priority=1)
        InventoryLevel.objects.create(warehouse=self.warehouse, product=self.product, on_hand=5)
        ShippingMethod.objects.create(store=self.store, code="kurir-toko", name="Kurir toko",
            flat_rate=15000, is_active=True)
        self.buyer = Client()
        self.buyer.post(f"/cart/add/{self.product.id}/")

    def test_without_a_warehouse_postal_code_checkout_uses_the_stores_own_rates(self):
        self.assertIsNone(shipping_origin(self.store))
        page = self.buyer.get("/checkout/")
        self.assertContains(page, "Kurir toko")
        reply = self.buyer.post("/checkout/shipping-rates/", {"postal_code": "60271"})
        self.assertEqual(reply.json()["code"], "no_origin")

    @patch("apps.integrations.shipping.request_json", return_value=RATES)
    def test_rates_start_from_the_sellers_warehouse(self, request_json):
        self.warehouse.postal_code = "40115"
        self.warehouse.save()
        Warehouse.objects.create(store=self.store, name="Cadangan", code="X", postal_code="99999",
            priority=9)
        reply = self.buyer.post("/checkout/shipping-rates/", {"postal_code": "60271"})
        self.assertEqual(reply.json()["quotes"][0]["price"], 12000)
        self.assertEqual(request_json.call_args.kwargs["payload"]["origin_postal_code"], 40115)

    def test_bookings_use_the_warehouse_contact(self):
        self.warehouse.postal_code = "40115"
        self.warehouse.save()
        origin = shipping_origin(self.store)
        self.assertEqual(origin, {"name": "Ruuma", "phone": "0822222222",
            "address": "Jl. Braga 1, Bandung", "postal_code": "40115"})

    def test_postal_code_must_be_five_digits(self):
        self.warehouse.postal_code = "4011"
        with self.assertRaisesMessage(ValidationError, "5 angka"):
            self.warehouse.full_clean()
