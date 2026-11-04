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
            "address": "Jl. Braga 1, Bandung", "postal_code": "40115", "area_id": "",
            "latitude": None, "longitude": None})

    def test_postal_code_must_be_five_digits(self):
        self.warehouse.postal_code = "4011"
        with self.assertRaisesMessage(ValidationError, "5 angka"):
            self.warehouse.full_clean()


@override_settings(SHIPPING_RATE_PROVIDER="biteship", BITESHIP_API_KEY="biteship_test.key",
    BITESHIP_ORIGIN_POSTAL_CODE="")
class WarehouseLocationTests(TestCase):
    """The seller picks the warehouse's area from the courier provider's list and pins it on a
    map; rates and pickups then start from exactly there."""

    def setUp(self):
        self.owner = User.objects.create_user("seller")
        self.store = Store.objects.create(owner=self.owner, name="Ruuma", slug="ruuma")
        self.warehouse = Warehouse.objects.create(store=self.store, name="Gudang", code="MAIN",
            priority=10)
        self.seller = Client()
        self.seller.force_login(self.owner)

    def test_the_form_shows_the_map_and_saves_the_picked_place(self):
        url = f"/merchant/setup/warehouses/{self.warehouse.id}/"
        page = self.seller.get(url)
        self.assertContains(page, "Lokasi pengiriman")
        self.assertContains(page, 'id="location-map"')
        self.assertContains(page, "Telepon (dihubungi kurir)")
        self.seller.post(url, {"name": "Gudang", "code": "MAIN", "priority": 10, "is_active": "on",
            "area_id": "IDNP6IDNC148IDND836IDZ12730", "area_name": "Kemang, Jakarta Selatan. 12730",
            "postal_code": "12730", "latitude": "-6.260500", "longitude": "106.813400",
            "address": "Jl. Kemang Raya 10", "phone": "0812"})
        self.warehouse.refresh_from_db()
        self.assertEqual((self.warehouse.area_id, self.warehouse.postal_code),
            ("IDNP6IDNC148IDND836IDZ12730", "12730"))
        self.assertEqual(str(self.warehouse.latitude), "-6.260500")

    @patch("apps.integrations.shipping.request_json", return_value=RATES)
    def test_rates_use_the_picked_area_and_bookings_carry_the_pin(self, request_json):
        from apps.integrations.shipping import BiteshipProvider, _biteship_booking_payload
        from apps.orders.models import Order

        Warehouse.objects.filter(id=self.warehouse.id).update(postal_code="12730",
            area_id="AREA-KEMANG", latitude="-6.2605", longitude="106.8134", address="Jl. Kemang")
        origin = shipping_origin(self.store)
        product = Product(name="Kopi", description="", weight_grams=500)
        BiteshipProvider().rates("40115", [{"product": product, "variant": None,
            "unit_price": 1, "quantity": 1}], origin_postal_code=origin["postal_code"],
            origin_area_id=origin["area_id"])
        payload = request_json.call_args.kwargs["payload"]
        self.assertEqual(payload["origin_area_id"], "AREA-KEMANG")
        self.assertNotIn("origin_postal_code", payload)

        order = Order.objects.create(store=self.store, customer_name="Ayu", phone="0812",
            shipping_address="x", postal_code="40115", subtotal=1, total=1, courier_code="jne",
            courier_service_code="reg", idempotency_key="k")
        booking = _biteship_booking_payload(order, origin)
        self.assertEqual(booking["origin_area_id"], "AREA-KEMANG")
        self.assertEqual(booking["origin_coordinate"], {"latitude": -6.2605, "longitude": 106.8134})

    @patch("apps.integrations.shipping.request_json", return_value={"areas": [{"id": "A1",
        "name": "Kemang, Jakarta Selatan. 12730", "postal_code": 12730}]})
    def test_the_seller_searches_the_couriers_area_list(self, _request_json):
        reply = self.seller.get("/merchant/shipping-areas/?q=kemang")
        self.assertEqual(reply.json()["areas"][0]["postal_code"], "12730")
        self.assertEqual(Client().get("/merchant/shipping-areas/?q=kemang").status_code, 302)
