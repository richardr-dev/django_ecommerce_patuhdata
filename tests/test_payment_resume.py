"""A shopper who closed the payment page can go back to it, with the time left on screen, and
the checkout summary shows the Total, not only Subtotal and Ongkir."""
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.utils import timezone

from apps.catalog.models import InventoryLevel, Product, Warehouse
from apps.orders.models import Order, ShippingMethod
from apps.shops.models import Store

PAY_URL = "https://checkout.example.com/pay/abc"


class PaymentResumeTests(TestCase):
    def setUp(self):
        self.store = Store.objects.create(owner=User.objects.create_user("seller"), name="Ruuma",
            slug="ruuma")
        self.product = Product.objects.create(store=self.store, name="bolabola", slug="bola",
            sku="BOLA", price=10000)
        warehouse = Warehouse.objects.create(store=self.store, name="Gudang", code="G")
        InventoryLevel.objects.create(warehouse=warehouse, product=self.product, on_hand=5)
        self.shopper = Client()

    def _order(self, **extra):
        values = {"store": self.store, "customer_name": "Ayu", "phone": "081234567890",
            "shipping_address": "Jakarta", "subtotal": 10000, "shipping_cost": 6700,
            "total": 16700, "status": "pending", "payment_provider": "doku",
            "payment_checkout_url": PAY_URL,
            "payment_expires_at": timezone.now() + timedelta(minutes=15),
            "idempotency_key": f"k{Order.objects.count()}", **extra}
        order = Order.objects.create(**values)
        session = self.shopper.session
        session["customer_order_ids"] = [*session.get("customer_order_ids", []), str(order.id)]
        session.save()
        return order

    def test_every_store_page_offers_to_resume_the_payment(self):
        order = self._order()
        page = self.shopper.get("/")
        self.assertContains(page, "Menunggu pembayaran")
        self.assertContains(page, "Rp16.700")
        self.assertContains(page, f'href="{PAY_URL}"')
        self.assertContains(page, f'data-countdown="{timezone.localtime(order.payment_expires_at).isoformat()}"')
        self.assertNotContains(Client().get("/"), "Menunggu pembayaran")

    def test_the_order_page_has_a_resume_button_and_timer(self):
        order = self._order()
        page = self.shopper.get(f"/orders/{order.id}/")
        self.assertContains(page, "Lanjutkan pembayaran")
        self.assertContains(page, "Sisa waktu")
        self.assertNotContains(page, "pending-payment-bar")  # no second reminder on its own page

    def test_bank_transfer_reminder_opens_the_order_page(self):
        order = self._order(payment_provider="bank_transfer", payment_checkout_url="")
        self.assertContains(self.shopper.get("/"), f'href="/orders/{order.id}/"')

    def test_no_reminder_once_paid_or_expired(self):
        self._order(status="paid")
        self._order(payment_expires_at=timezone.now() - timedelta(minutes=1))
        self.assertNotContains(self.shopper.get("/"), "Menunggu pembayaran")

    def test_checkout_summary_shows_the_total(self):
        ShippingMethod.objects.create(store=self.store, name="Kurir toko", code="kurir",
            flat_rate=6700, is_active=True)
        ShippingMethod.objects.create(store=self.store, name="Promo", code="promo",
            flat_rate=9000, free_above=5000, is_active=True)
        self.shopper.post(f"/cart/add/{self.product.id}/")
        page = self.shopper.get("/checkout/")
        self.assertContains(page, 'id="checkout-total"')
        self.assertContains(page, 'data-subtotal="10000')
        self.assertContains(page, 'value="kurir" data-price="6700')
        self.assertContains(page, 'value="promo" data-price="0"')
        self.assertContains(page, "checkout-summary.js")
