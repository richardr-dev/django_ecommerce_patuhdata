"""Orders booked through Biteship print the courier's own label (JNE, J&T, …); others keep the
Lakuin label."""
from io import BytesIO
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from pypdf import PdfReader, PdfWriter

from apps.integrations.http import ProviderError
from apps.orders.models import Order
from apps.shops.models import Store

LABEL = {"success": True, "shipping_label": {"paper_size": "10x15cm",
    "file_url": "https://biteship.s3.ap-southeast-1.amazonaws.com/public/shipping_labels/x.pdf"}}


@override_settings(BITESHIP_API_KEY="biteship_test.key")
class CourierLabelTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user("seller")
        self.store = Store.objects.create(owner=self.owner, name="Ruuma", slug="ruuma")
        self.seller = Client()
        self.seller.force_login(self.owner)

    def _order(self, booked=True, key="k"):
        return Order.objects.create(store=self.store, customer_name="Ayu", phone="0812",
            shipping_address="x", subtotal=1, total=1, status="packed", idempotency_key=key,
            shipping_provider="biteship", courier_code="jne", courier_service_code="reg",
            external_shipping_id=f"BS-{key}" if booked else "", tracking_number="WYB-1" if booked else "")

    @patch("apps.integrations.shipping.request_json", return_value=LABEL)
    def test_a_booked_order_opens_the_couriers_official_label(self, request_json):
        order = self._order()
        page = self.seller.get("/merchant/orders/?tab=all")
        self.assertContains(page, "Cetak label resmi JNE")
        reply = self.seller.get(f"/merchant/orders/{order.id}/courier-label/")
        self.assertRedirects(reply, LABEL["shipping_label"]["file_url"], fetch_redirect_response=False)
        self.assertTrue(request_json.call_args.args[0].endswith("/v1/orders/BS-k/label"))
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
        self.assertContains(page, "Total: <b>Rp1</b>")
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


def _pdf(pages=1):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=283, height=425)  # 10x15 cm
    output = BytesIO()
    writer.write(output)
    return MagicMock(ok=True, status_code=200, content=output.getvalue())


@override_settings(BITESHIP_API_KEY="biteship_test.key")
class BulkCourierLabelTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user("seller")
        self.store = Store.objects.create(owner=self.owner, name="Ruuma", slug="ruuma")
        self.seller = Client()
        self.seller.force_login(self.owner)
        self.orders = [CourierLabelTests._order(self, key=key) for key in "ab"]

    _order = CourierLabelTests._order

    @patch("requests.get", return_value=_pdf())
    @patch("apps.integrations.shipping.request_json", return_value=LABEL)
    def test_several_booked_orders_print_as_one_pdf(self, _request_json, _get):
        ids = "&".join(f"id={order.id}" for order in self.orders)
        reply = self.seller.get(f"/merchant/orders/labels/?{ids}&print=1")
        self.assertRedirects(reply, f"/merchant/orders/courier-labels/?{ids}",
            fetch_redirect_response=False)
        pdf = self.seller.get(f"/merchant/orders/courier-labels/?{ids}")
        self.assertEqual(pdf["Content-Type"], "application/pdf")
        self.assertEqual(len(PdfReader(BytesIO(pdf.content)).pages), 2)
        self.assertFalse(Order.objects.filter(label_printed_at__isnull=True).exists())

    @patch("requests.get", side_effect=[_pdf(), MagicMock(ok=False, status_code=404, content=b"")])
    @patch("apps.integrations.shipping.request_json", return_value=LABEL)
    def test_a_label_not_ready_yet_is_left_out(self, _request_json, _get):
        ids = "&".join(f"id={order.id}" for order in self.orders)
        pdf = self.seller.get(f"/merchant/orders/courier-labels/?{ids}")
        self.assertEqual(len(PdfReader(BytesIO(pdf.content)).pages), 1)
        self.assertEqual(pdf["X-Labels-Missing"], str(self.orders[1].id))
        self.assertIsNone(Order.objects.get(id=self.orders[1].id).label_printed_at)

    @patch("apps.integrations.shipping.request_json", return_value=LABEL)
    def test_a_mixed_batch_offers_the_merged_pdf(self, _request_json):
        manual = self._order(booked=False, key="c")
        ids = "&".join(f"id={order.id}" for order in [*self.orders, manual])
        page = self.seller.get(f"/merchant/orders/labels/?{ids}")
        self.assertContains(page, "Cetak 2 label resmi sekaligus")

    def test_another_stores_orders_are_not_printed(self):
        other = Client()
        other.force_login(User.objects.create_user("other"))
        Store.objects.create(owner=User.objects.get(username="other"), name="Lain", slug="lain")
        reply = other.get(f"/merchant/orders/courier-labels/?id={self.orders[0].id}")
        self.assertRedirects(reply, "/merchant/orders/", fetch_redirect_response=False)
