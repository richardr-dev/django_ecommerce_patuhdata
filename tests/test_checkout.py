import tempfile
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings

from apps.catalog.models import InventoryLevel, Product, ProductImage, ProductVariant, Warehouse
from apps.orders.models import (
    Coupon,
    InventoryMovement,
    Order,
    ShipmentTrackingEvent,
    ShippingMethod,
)
from apps.orders.pricing import calculate_price
from apps.orders.services import InvalidOrderTransition, transition_order
from apps.orders.tracking import sync_order_tracking
from apps.shops.models import Store


@override_settings(PAYMENT_PROVIDER="demo", SHIPPING_RATE_PROVIDER="biteship",
    BITESHIP_API_KEY="")
class CheckoutTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner")
        self.store = Store.objects.create(owner=self.owner, name="Test Shop", slug="test-shop")
        self.product = Product.objects.create(store=self.store, name="Item", slug="item", sku="SKU-1", price=10000)
        self.warehouse = Warehouse.objects.create(store=self.store, name="Jakarta", code="JKT")
        self.level = InventoryLevel.objects.create(warehouse=self.warehouse, product=self.product, on_hand=2)
        self.client = Client()
        self.buyer = User.objects.create_user("buyer", password="BuyerPass-2026!")
        self.client.force_login(self.buyer)

    def test_checkout_requires_login_and_preserves_cart(self):
        self.client.logout()
        self.client.post(f"/cart/add/{self.product.id}/")
        response = self.client.get("/checkout/")
        self.assertRedirects(response, "/account/login/?next=/checkout/")
        self.assertEqual(self.client.session["cart"][f"p:{self.product.id}"], 1)
        login_page = self.client.get(response.url)
        self.assertContains(login_page, "/account/register/?next=/checkout/")

    def test_home_privacy_and_cookie_experience_are_available(self):
        home = self.client.get("/")
        self.assertContains(home, "home-hero")
        self.assertContains(home, "cookie-banner")
        self.assertEqual(self.client.get("/privacy/").status_code, 200)

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

    def test_cart_displays_product_photo(self):
        self.product.image_url = "https://cdn.example.com/item.jpg"
        self.product.save(update_fields=["image_url"])
        self.client.post(f"/cart/add/{self.product.id}/")
        response = self.client.get("/cart/")
        self.assertContains(response, "https://cdn.example.com/item.jpg")
        self.assertContains(response, f'alt="{self.product.name}"')

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
            "warehouse": self.warehouse.id, "initial_stock": 7,
        })
        self.assertRedirects(response, "/merchant/setup/products/")
        self.assertTrue(Product.objects.filter(store=self.store, sku="DASH-1").exists())
        self.assertFalse(Product.objects.filter(store=other, sku="DASH-1").exists())
        created = Product.objects.get(store=self.store, sku="DASH-1")
        self.assertEqual(created.inventory_levels.get(warehouse=self.warehouse).on_hand, 7)

    def test_customer_registration_checkout_and_order_history(self):
        self.client.logout()
        response = self.client.post("/account/register/", {"first_name": "Ayu Putri",
            "email": "ayu@example.com", "password1": "StrongPass-2026!",
            "password2": "StrongPass-2026!", "next": "/checkout/"})
        self.assertRedirects(response, "/checkout/", target_status_code=302)
        user = User.objects.get(username="ayu@example.com")
        self.client.post(f"/cart/add/{self.product.id}/")
        self.client.post("/checkout/", {"customer_name": "Ayu Putri",
            "email": "ayu@example.com", "phone": "081234567890",
            "shipping_address": "Jakarta"})
        order = Order.objects.get()
        self.assertEqual(order.customer_user, user)
        history = self.client.get("/account/orders/")
        self.assertContains(history, str(order.id)[:8])
        self.assertContains(history, self.product.name)

    def test_password_reset_sends_single_use_link(self):
        User.objects.create_user("buyer@example.com", email="buyer@example.com",
            password="StrongPass-2026!")
        response = self.client.post("/account/password-reset/", {"email": "buyer@example.com"})
        self.assertRedirects(response, "/account/password-reset/done/")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("/account/reset/", mail.outbox[0].body)

    def test_merchant_can_upload_product_gallery_photo(self):
        self.client.force_login(self.owner)
        image = SimpleUploadedFile("product.gif",
            b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;",
            content_type="image/gif")
        with tempfile.TemporaryDirectory() as media_root, self.settings(MEDIA_ROOT=media_root):
            response = self.client.post("/merchant/setup/images/new/", {"product": self.product.sku,
                "file": image, "url": "", "alt_text": "Foto Item", "position": 1})
        self.assertRedirects(response, "/merchant/setup/images/")
        self.assertTrue(ProductImage.objects.filter(product=self.product,
            alt_text="Foto Item").exists())

    def test_merchant_can_upload_store_logo_and_storefront_uses_it(self):
        self.client.force_login(self.owner)
        logo = SimpleUploadedFile("brand.png",
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89",
            content_type="image/png")
        with tempfile.TemporaryDirectory() as media_root, self.settings(MEDIA_ROOT=media_root):
            response = self.client.post("/merchant/settings/", {
                "name": self.store.name, "logo": logo, "domain": "",
                "description": "Toko uji", "theme": "modern", "primary_color": "#155eef",
                "currency": "IDR", "tax_rate": "0", "support_phone": "",
                "support_email": "", "terms_url": "", "privacy_url": "",
                "return_policy": "",
            })
            self.assertRedirects(response, "/merchant/settings/")
            self.store.refresh_from_db()
            self.assertTrue(self.store.logo.name.startswith("stores/logos/"))
            storefront = self.client.get("/")
            self.assertContains(storefront, self.store.logo.url, count=3)
            settings_page = self.client.get("/merchant/settings/")
            self.assertContains(settings_page, "Identitas toko")
            self.assertContains(settings_page, self.store.logo.url)

    def test_merchant_can_adjust_stock_without_editing_reservations(self):
        self.level.reserved = 1
        self.level.save(update_fields=["reserved"])
        self.client.force_login(self.owner)
        response = self.client.post("/merchant/stock/", {"target_type": "level",
            "target_id": self.level.id, "stock": 8, "safety_stock": 2,
            "reason": "Barang masuk"})
        self.assertRedirects(response, "/merchant/stock/")
        self.level.refresh_from_db()
        movement = InventoryMovement.objects.get(kind="adjust")
        self.assertEqual(self.level.on_hand, 8)
        self.assertEqual(self.level.reserved, 1)
        self.assertEqual(self.level.available, 5)
        self.assertEqual(movement.previous_on_hand, 2)
        self.assertEqual(movement.new_on_hand, 8)

    def test_stock_adjustment_cannot_drop_below_reserved_units(self):
        self.level.reserved = 2
        self.level.save(update_fields=["reserved"])
        self.client.force_login(self.owner)
        self.client.post("/merchant/stock/", {"target_type": "level",
            "target_id": self.level.id, "stock": 1, "safety_stock": 0})
        self.level.refresh_from_db()
        self.assertEqual(self.level.on_hand, 2)
        self.assertFalse(InventoryMovement.objects.filter(kind="adjust").exists())

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

    def test_full_happy_path_checkout_payment_fulfilment_and_completion(self):
        self.client.post(f"/cart/add/{self.product.id}/", {"quantity": 2})
        checkout = self.client.post("/checkout/", {"customer_name": "Ayu",
            "email": "ayu@example.com", "phone": "081234567890",
            "shipping_address": "Jakarta"})
        order = Order.objects.get()
        self.assertRedirects(checkout, f"/integrations/demo-pay/{order.id}/")

        self.client.post(f"/integrations/demo-pay/{order.id}/")
        order.refresh_from_db()
        self.assertEqual(order.status, "paid")

        self.client.force_login(self.owner)
        for status in ("confirmed", "allocated", "picking", "packed"):
            response = self.client.post(f"/merchant/orders/{order.id}/update/", {"status": status})
            self.assertRedirects(response, "/merchant/orders/")
        self.client.post(f"/merchant/orders/{order.id}/update/",
            {"status": "shipped", "tracking_number": "JNE-123"})
        for status in ("delivered", "completed"):
            self.client.post(f"/merchant/orders/{order.id}/update/", {"status": status,
                "tracking_number": "JNE-123"})

        order.refresh_from_db()
        self.level.refresh_from_db()
        self.assertEqual(order.status, "completed")
        self.assertEqual(order.tracking_number, "JNE-123")
        self.assertEqual(self.level.on_hand, 0)
        self.assertEqual(self.level.reserved, 0)
        self.assertEqual(order.status_events.values_list("status", flat=True).last(), "completed")

    def test_merchant_can_add_resi_and_fulfillment_method_self_service(self):
        order = Order.objects.create(store=self.store, customer_user=self.buyer, status="paid",
            customer_name="Ayu", phone="081234567890", shipping_address="Jakarta",
            subtotal=10000, total=10000, idempotency_key="manual-resi")
        self.client.force_login(self.owner)
        response = self.client.post(f"/merchant/orders/{order.id}/tracking/", {
            "fulfillment_method": "merchant_dropoff", "courier_code": "jne",
            "tracking_number": "JNE-SELF-123"})
        self.assertRedirects(response, "/merchant/orders/")
        order.refresh_from_db()
        self.assertEqual(order.fulfillment_method, "merchant_dropoff")
        self.assertEqual(order.courier_code, "jne")
        self.assertEqual(order.tracking_number, "JNE-SELF-123")
        self.assertEqual(order.tracking_status, "ready_to_track")
        page = self.client.get("/merchant/orders/")
        self.assertContains(page, "JNE-SELF-123")
        self.assertContains(page, "Toko antar ke drop point")

    @patch("apps.orders.tracking.RajaOngkirProvider.track")
    def test_courier_tracking_sync_updates_timeline_and_order_status(self, track):
        order = Order.objects.create(store=self.store, customer_user=self.buyer, status="packed",
            customer_name="Ayu", phone="081234567890", shipping_address="Jakarta",
            subtotal=10000, total=10000, idempotency_key="tracking-sync",
            courier_code="jne", tracking_number="JNE123", tracking_status="ready_to_track")
        track.return_value = {"status": "ON PROCESS", "detail": "Paket di gateway Jakarta",
            "events": [{"status": "TRANSIT", "description": "Paket di gateway Jakarta",
                "location": "Jakarta", "occurred_at": "2026-08-27 08:00:00",
                "payload": {"manifest_code": "TRANSIT", "manifest_date": "2026-08-27"}}],
            "payload": {}}
        sync_order_tracking(order)
        order.refresh_from_db()
        self.assertEqual(order.status, "shipped")
        self.assertEqual(order.tracking_status, "in_transit")
        self.assertEqual(ShipmentTrackingEvent.objects.get(order=order).location, "Jakarta")
