"""The courier gets the whole address (building, kelurahan, kecamatan, city, province,
postcode), and checkout asks for the full street/building address first."""
from django.contrib.auth.models import User
from django.test import Client, TestCase

from apps.catalog.models import InventoryLevel, Product, Warehouse
from apps.integrations.shipping import _biteship_booking_payload
from apps.orders.models import Order, OrderItem
from apps.shops.models import Store


class DeliveryAddressTests(TestCase):
    def setUp(self):
        self.store = Store.objects.create(owner=User.objects.create_user("seller"), name="Ruuma",
            slug="ruuma")
        self.product = Product.objects.create(store=self.store, name="Bola", slug="bola",
            sku="BOLA", price=10000)

    def _order(self, street, **extra):
        values = {"store": self.store, "customer_name": "Ayu", "phone": "081234567890",
            "shipping_address": street, "village": "Pesanggrahan", "district": "Pesanggrahan",
            "city": "Jakarta Selatan", "province": "DKI Jakarta", "postal_code": "12320",
            "subtotal": 10000, "total": 10000, "idempotency_key": street[:60], **extra}
        return Order.objects.create(**values)

    def test_full_address_adds_the_area_once(self):
        order = self._order("Apartemen Metro Park Residence Tower B Lt. 12 Unit 05")
        self.assertEqual(order.full_address, "Apartemen Metro Park Residence Tower B Lt. 12 Unit 05, "
            "Pesanggrahan, Jakarta Selatan, DKI Jakarta 12320")
        typed = self._order("Jl. Melati 3, Jakarta Selatan 12320")
        self.assertEqual(typed.full_address, "Jl. Melati 3, Jakarta Selatan 12320, Pesanggrahan, DKI Jakarta")

    def test_the_courier_booking_carries_the_full_address(self):
        order = self._order("Apartemen Metro Park Residence Tower B Unit 05", destination_area_id="IDNP6")
        OrderItem.objects.create(order=order, product=self.product, name="Bola", sku="BOLA",
            unit_price=10000, quantity=1, line_total=10000)
        payload = _biteship_booking_payload(order, {"name": "Gudang", "phone": "0811", "address": "Jl. A",
            "postal_code": "12190"})
        self.assertIn("Metro Park Residence", payload["destination_address"])
        self.assertIn("Jakarta Selatan, DKI Jakarta 12320", payload["destination_address"])
        self.assertEqual(payload["destination_area_id"], "IDNP6")

    def test_checkout_asks_for_the_full_address_first(self):
        warehouse = Warehouse.objects.create(store=self.store, name="G", code="G")
        InventoryLevel.objects.create(warehouse=warehouse, product=self.product, on_hand=3)
        shopper = Client()
        shopper.post(f"/cart/add/{self.product.id}/")
        page = shopper.get("/checkout/").content.decode()
        self.assertIn("Alamat lengkap", page)
        self.assertIn("Metro Park Residence", page)  # the example
        self.assertLess(page.index('name="shipping_address"'), page.index('name="province"'))
