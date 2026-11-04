"""Orders booked through Biteship print the courier's own label (JNE, J&T, …); others keep the
Lakuin label."""
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings

from apps.integrations.http import ProviderError
from apps.orders.models import Order
from apps.shops.models import Store

LABEL = {"success": True, "shipping_label": {"paper_size": "10x15cm",
    "file_url": "https://biteship.s3.ap-southeast-1.amazonaws.com/public/shipping_labels/x.pdf"}}


@override_settings(BITESHIP_API_KEY="biteship_test.key")
class CourierLabelTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("seller")
        self.store = Store.objects.create(owner=self.owner, name="Ruuma", slug="ruuma")
        self.seller = Client()
        self.seller.force_login(self.owner)

    def _order(self, booked=True, key="k"):
        return Order.objects.create(store=self.store, customer_name="Ayu", phone="0812",
            shipping_address="x", subtotal=1, total=1, status="packed", idempotency_key=key,
            shipping_provider="biteship", courier_code="jne", courier_service_code="reg",
            external_shipping_id="BS-123" if booked else "", tracking_number="WYB-1" if booked else "")

    @patch("apps.integrations.shipping.request_json", return_value=LABEL)
    def test_a_booked_order_opens_the_couriers_official_label(self, request_json):
        order = self._order()
        page = self.seller.get("/merchant/orders/?tab=all")
        self.assertContains(page, "Cetak label resmi JNE")
        reply = self.seller.get(f"/merchant/orders/{order.id}/courier-label/")
        self.assertRedirects(reply, LABEL["shipping_label"]["file_url"], fetch_redirect_response=False)
        self.assertTrue(request_json.call_args.args[0].endswith("/v1/orders/BS-123/label"))
        order.refresh_from_db()
        self.assertIsNotNone(order.label_printed_at)

        self.seller.get(f"/merchant/orders/{order.id}/courier-label/")
        self.assertEqual(request_json.call_count, 1)  # cached

    @patch("apps.integrations.shipping.request_json", return_value=LABEL)
    def test_printing_one_booked_order_goes_to_the_official_label(self, _request_json):
        order = self._order()
        reply = self.seller.get(f"/merchant/orders/labels/?id={order.id}&print=1")
        self.assertRedirects(reply, f"/merchant/orders/{order.id}/courier-label/",
            fetch_redirect_response=False)

    def test_orders_without_a_booking_keep_the_lakuin_label(self):
        order = self._order(booked=False)
        self.assertNotContains(self.seller.get("/merchant/orders/?tab=all"), "Cetak label resmi")
        page = self.seller.get(f"/merchant/orders/labels/?id={order.id}")
        self.assertEqual(page.status_code, 200)
        self.assertRedirects(self.seller.get(f"/merchant/orders/{order.id}/courier-label/"),
            f"/merchant/orders/labels/?id={order.id}&print=1", fetch_redirect_response=False)

    @patch("apps.integrations.shipping.request_json", return_value=LABEL)
    def test_a_mixed_batch_lists_the_official_labels(self, _request_json):
        booked, manual = self._order(key="a"), self._order(booked=False, key="b")
        page = self.seller.get(f"/merchant/orders/labels/?id={booked.id}&id={manual.id}")
        self.assertContains(page, "Label resmi kurir tersedia")
        self.assertContains(page, f"/merchant/orders/{booked.id}/courier-label/")

    @patch("apps.integrations.shipping.request_json", side_effect=ProviderError("HTTP 500"))
    def test_a_missing_label_falls_back_politely(self, _request_json):
        order = self._order()
        reply = self.seller.get(f"/merchant/orders/{order.id}/courier-label/", follow=True)
        self.assertContains(reply, "Label resmi kurir belum tersedia")
