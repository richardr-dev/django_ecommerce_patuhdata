from django.contrib.auth.models import User
from django.test import Client, TestCase

from apps.catalog.models import InventoryLevel, Product, ProductVariant, Warehouse
from apps.orders.models import Coupon, InventoryMovement, Order, ShippingMethod
from apps.orders.pricing import calculate_price
from apps.orders.services import InvalidOrderTransition, transition_order
from apps.shops.models import Store


class CheckoutTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner")
        self.store = Store.objects.create(owner=self.owner, name="Test Shop", slug="test-shop")
        self.product = Product.objects.create(store=self.store, name="Item", slug="item", sku="SKU-1", price=10000)
        self.warehouse = Warehouse.objects.create(store=self.store, name="Jakarta", code="JKT")
        self.level = InventoryLevel.objects.create(warehouse=self.warehouse, product=self.product, on_hand=2)
        self.client = Client()

    def test_checkout_reserves_stock_and_creates_order(self):
        self.client.post(f"/cart/add/{self.product.id}/")
        response = self.client.post("/checkout/", {"customer_name":"Ayu", "email":"ayu@example.com", "phone":"0812", "shipping_address":"Jakarta"})
        order = Order.objects.get()
        self.level.refresh_from_db()
        self.assertRedirects(response, f"/integrations/demo-pay/{order.id}/")
        self.assertEqual(order.total, 10000)
        self.assertEqual(self.level.reserved, 1)

    def test_products_are_tenant_scoped(self):
        other = Store.objects.create(owner=self.store.owner, name="Other", slug="other")
        hidden = Product.objects.create(store=other, name="Secret", slug="secret", sku="S", price=1)
        response = self.client.get(f"/products/{hidden.slug}/")
        self.assertEqual(response.status_code, 404)

    def test_checkout_applies_shipping_and_coupon(self):
        ShippingMethod.objects.create(store=self.store, code="jne", name="JNE Reguler", flat_rate=15000)
        Coupon.objects.create(store=self.store, code="HEMAT10", kind="percent", value=10)
        self.client.post(f"/cart/add/{self.product.id}/")
        self.client.post("/checkout/", {"customer_name": "Ayu", "phone": "+62 812-3456-7890",
            "shipping_address": "Jl. Merdeka 1", "province": "DKI Jakarta", "city": "Jakarta",
            "district": "Menteng", "postal_code": "10310", "shipping_method": "jne",
            "payment_method": "qris", "coupon_code": "hemat10"})
        order = Order.objects.get()
        self.assertEqual(order.phone, "081234567890")
        self.assertEqual(order.discount, 1000)
        self.assertEqual(order.shipping_cost, 15000)
        self.assertEqual(order.total, 24000)

    def test_unknown_shipping_method_is_rejected(self):
        ShippingMethod.objects.create(store=self.store, code="jne", name="JNE", flat_rate=15000)
        self.client.post(f"/cart/add/{self.product.id}/")
        response = self.client.post("/checkout/", {"customer_name": "Ayu", "phone": "081234567890",
            "shipping_address": "Jakarta", "shipping_method": "forged"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Order.objects.exists())

    def test_variant_price_stock_and_identity_are_preserved(self):
        variant = ProductVariant.objects.create(product=self.product, name="Merah / L",
            sku="SKU-RED-L", price=12500, weight_grams=750, stock=2)
        self.client.post(f"/cart/add/{self.product.id}/", {"variant_id": variant.id, "quantity": 2})
        response = self.client.post("/checkout/", {"customer_name": "Ayu", "phone": "081234567890",
            "shipping_address": "Jakarta", "province": "DKI Jakarta", "city": "Jakarta",
            "district": "Menteng", "postal_code": "10310"})
        order = Order.objects.get()
        item = order.items.get()
        variant.refresh_from_db()
        self.assertRedirects(response, f"/integrations/demo-pay/{order.id}/")
        self.assertEqual(order.subtotal, 25000)
        self.assertEqual(item.variant, variant)
        self.assertEqual(item.sku, "SKU-RED-L")
        self.assertEqual(item.variant_name, "Merah / L")
        self.assertEqual(item.unit_price, 12500)
        self.assertEqual(variant.stock, 2)
        self.assertEqual(variant.reserved, 2)
        self.assertEqual(variant.available_stock, 0)
        self.assertEqual(self.level.reserved, 0)

    def test_variant_stock_is_restored_when_order_is_cancelled(self):
        variant = ProductVariant.objects.create(product=self.product, name="Biru", sku="SKU-BLUE", stock=1)
        self.client.post(f"/cart/add/{self.product.id}/", {"variant_id": variant.id})
        self.client.post("/checkout/", {"customer_name": "Ayu", "phone": "081234567890",
            "shipping_address": "Jakarta", "province": "DKI Jakarta", "city": "Jakarta",
            "district": "Menteng", "postal_code": "10310"})
        self.client.force_login(self.store.owner)
        order = Order.objects.get()
        self.client.post(f"/merchant/orders/{order.id}/update/", {"status": "cancelled"})
        variant.refresh_from_db()
        self.assertEqual(variant.stock, 1)
        self.assertEqual(variant.reserved, 0)

    def test_merchant_dashboard_and_product_setup_are_store_scoped(self):
        other = Store.objects.create(owner=self.owner, name="Other", slug="other-dashboard")
        self.client.force_login(self.owner)
        response = self.client.get("/merchant/", {"store": self.store.id})
        self.assertContains(response, "Ringkasan penjualan")
        response = self.client.post("/merchant/setup/products/new/", {
            "name": "Dashboard Product", "slug": "dashboard-product", "sku": "DASH-1",
            "description": "", "price": 45000, "compare_at_price": "", "image_url": "",
            "weight_grams": 100, "length_cm": 0, "width_cm": 0, "height_cm": 0,
            "seo_title": "", "seo_description": "", "is_active": "on",
        })
        self.assertRedirects(response, "/merchant/setup/products/")
        self.assertTrue(Product.objects.filter(store=self.store, sku="DASH-1").exists())
        self.assertFalse(Product.objects.filter(store=other, sku="DASH-1").exists())

    def test_order_state_machine_rejects_skipping_fulfilment_steps(self):
        order = Order.objects.create(store=self.store, customer_name="Ayu", phone="0812",
            shipping_address="Jakarta", subtotal=10000, total=10000, idempotency_key="state-machine")
        with self.assertRaises(InvalidOrderTransition):
            transition_order(order, "shipped")
        transition_order(order, "paid")
        transition_order(order, "confirmed")
        transition_order(order, "allocated")
        transition_order(order, "picking")
        transition_order(order, "packed")
        transition_order(order, "shipped")
        order.refresh_from_db()
        self.assertEqual(order.status, "shipped")

    def test_checkout_submission_is_idempotent(self):
        self.client.post(f"/cart/add/{self.product.id}/")
        self.client.get("/checkout/")
        key = self.client.session["checkout_key"]
        payload = {"idempotency_key": key, "customer_name": "Ayu", "phone": "081234567890",
            "shipping_address": "Jakarta", "province": "DKI Jakarta", "city": "Jakarta",
            "district": "Menteng", "postal_code": "10310"}
        first = self.client.post("/checkout/", payload)
        second = self.client.post("/checkout/", payload)
        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(Order.objects.count(), 1)

    def test_inventory_is_committed_once_when_order_is_packed(self):
        self.client.post(f"/cart/add/{self.product.id}/")
        self.client.post("/checkout/", {"customer_name": "Ayu", "phone": "081234567890",
            "shipping_address": "Jakarta", "province": "DKI Jakarta", "city": "Jakarta",
            "district": "Menteng", "postal_code": "10310"})
        order = Order.objects.get()
        for status in ("paid", "confirmed", "allocated", "picking", "packed"):
            transition_order(order, status)
            order.refresh_from_db()
        self.level.refresh_from_db()
        self.assertEqual(self.level.on_hand, 1)
        self.assertEqual(self.level.reserved, 0)
        self.assertEqual(list(InventoryMovement.objects.values_list("kind", flat=True)),
            ["reserve", "commit"])

    def test_pricing_is_server_authoritative_and_discount_reduces_tax_base(self):
        self.store.tax_rate = 11
        coupon = Coupon.objects.create(store=self.store, code="FIXED", kind="fixed", value=1000)
        breakdown = calculate_price(store=self.store,
            lines=[{"unit_price": self.product.price, "quantity": 1}], shipping_cost=500, coupon=coupon)
        self.assertEqual(breakdown.subtotal, 10000)
        self.assertEqual(breakdown.discount, 1000)
        self.assertEqual(breakdown.tax, 990)
        self.assertEqual(breakdown.total, 10490)
