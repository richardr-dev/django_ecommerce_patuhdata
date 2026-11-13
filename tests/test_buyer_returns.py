"""The buyer asks to return or exchange: item, type, reason, a photo, and a size that is in stock."""
import io
import shutil
import tempfile
import uuid
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from PIL import Image

from apps.catalog.models import Product, ProductVariant
from apps.orders.models import Order, OrderItem, ReturnRequest
from apps.shops.models import Store

MEDIA = tempfile.mkdtemp()


def photo(name="foto.png", size=(40, 40)):
    buffer = io.BytesIO()
    Image.new("RGB", size, (120, 60, 20)).save(buffer, "PNG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")


@override_settings(MEDIA_ROOT=MEDIA, PAYMENT_PROVIDER="demo", SHIPPING_RATE_PROVIDER="biteship",
    BITESHIP_API_KEY="")
class BuyerReturnTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def setUp(self):
        self.seller = User.objects.create_user("seller", password="x")
        self.store = Store.objects.create(owner=self.seller, name="Batik", slug="batik")
        self.shirt = Product.objects.create(store=self.store, sku="KMJ", name="Kemeja", slug="kemeja",
            price=150_000, cost_price=80_000)
        self.m = ProductVariant.objects.create(product=self.shirt, name="M", sku="KMJ-M", stock=3)
        self.l = ProductVariant.objects.create(product=self.shirt, name="L", sku="KMJ-L", stock=4)
        self.xl = ProductVariant.objects.create(product=self.shirt, name="XL", sku="KMJ-XL", stock=0)
        self.buyer = User.objects.create_user("buyer", password="x")
        self.order = Order.objects.create(store=self.store, customer_user=self.buyer, customer_name="Ani",
            phone="0812", shipping_address="Jakarta", subtotal=Decimal(300_000), total=Decimal(300_000),
            status="completed", idempotency_key=uuid.uuid4().hex)
        self.line = OrderItem.objects.create(order=self.order, product=self.shirt, variant=self.m,
            name="Kemeja", variant_name="M", sku="KMJ-M", unit_price=150_000, quantity=2,
            line_total=300_000, unit_cost=80_000)
        self.client = Client()
        self.client.force_login(self.buyer)
        self.url = reverse("request-return", args=[self.order.id])

    def ask(self, **extra):
        data = {"order_item": self.line.pk, "request_type": "return_refund", "reason": "defective",
            "reason_detail": "Jahitan lepas", "quantity": 1, "photo": photo(), **extra}
        return self.client.post(self.url, data, follow=True)

    def test_return_with_photo_is_recorded_with_item_and_quantity(self):
        self.ask()
        ret = ReturnRequest.objects.get()
        self.assertEqual((ret.status, ret.request_type, ret.order_item_id, ret.quantity),
            ("requested", "return_refund", self.line.pk, 1))
        self.assertTrue(ret.photo)
        self.assertEqual(ret.original_variant_id, self.m.pk)

    def test_photo_is_required(self):
        response = self.ask(photo="")
        self.assertContains(response, "Lampirkan foto")
        self.assertEqual(ReturnRequest.objects.count(), 0)

    def test_a_file_that_is_not_a_picture_is_refused(self):
        fake = SimpleUploadedFile("x.png", b"not an image", content_type="image/png")
        self.assertContains(self.ask(photo=fake), "bukan foto yang valid")
        self.assertEqual(ReturnRequest.objects.count(), 0)

    def test_exchange_needs_an_available_size(self):
        self.assertContains(self.ask(request_type="exchange"), "Pilih ukuran pengganti")
        self.assertContains(self.ask(request_type="exchange", replacement_variant=self.xl.pk),
            "Pilih ukuran pengganti")  # XL is out of stock
        self.assertContains(self.ask(request_type="exchange", replacement_variant=self.m.pk),
            "Pilih ukuran pengganti")  # the same size
        self.assertEqual(ReturnRequest.objects.count(), 0)
        self.ask(request_type="exchange", replacement_variant=self.l.pk)
        ret = ReturnRequest.objects.get()
        self.assertEqual((ret.request_type, ret.replacement_variant_id), ("exchange", self.l.pk))

    def test_exchange_quantity_cannot_exceed_the_stock_of_that_size(self):
        self.l.stock = 1
        self.l.save()
        self.assertContains(self.ask(request_type="exchange", replacement_variant=self.l.pk, quantity=2),
            "hanya tersisa 1")

    def test_quantity_item_and_double_requests_are_checked(self):
        self.assertContains(self.ask(quantity=5), "Jumlah harus antara 1 dan 2")
        self.ask()
        self.assertContains(self.ask(), "sudah punya pengajuan")
        self.assertEqual(ReturnRequest.objects.count(), 1)

    def test_not_before_the_parcel_is_sent(self):
        Order.objects.filter(pk=self.order.pk).update(status="paid")
        self.assertContains(self.ask(), "setelah pesanan dikirim")

    def test_order_page_shows_the_form_the_sizes_and_the_status(self):
        page = self.client.get(reverse("order-detail", args=[self.order.id]))
        self.assertContains(page, 'name="photo"')
        self.assertContains(page, "Menukar ukuran")
        sizes = page.context["return_sizes"][str(self.line.pk)]
        self.assertEqual([s["name"] for s in sizes], ["L"])  # M is the same, XL has no stock
        self.ask(request_type="exchange", replacement_variant=self.l.pk)
        page = self.client.get(reverse("order-detail", args=[self.order.id]))
        self.assertContains(page, "Diminta")
        self.assertContains(page, "ukuran L")

    def test_another_buyer_cannot_open_it(self):
        other = Client()
        other.force_login(User.objects.create_user("other", password="x"))
        self.assertEqual(other.post(self.url, {"order_item": self.line.pk}).status_code, 404)

    def test_seller_cannot_approve_without_a_photo_but_can_upload_one(self):
        ret = ReturnRequest.objects.create(order=self.order, order_item=self.line, status="requested")
        seller = Client()
        seller.force_login(self.seller)
        url = reverse("merchant-return-detail", args=[ret.pk])
        seller.post(url, {"action": "approve"})
        ret.refresh_from_db()
        self.assertEqual(ret.status, "requested")
        seller.post(url, {"action": "upload_photo", "photo": photo()})
        ret.refresh_from_db()
        self.assertTrue(ret.photo)
        seller.post(url, {"action": "approve"})
        ret.refresh_from_db()
        self.assertEqual(ret.status, "approved")

    def test_the_quantity_carries_into_the_settlement(self):
        from apps.tax import returns
        ret = self.ask() and ReturnRequest.objects.get()
        rows = {r["line"].pk: r for r in returns.returnable_lines(ret)}
        self.assertEqual(rows[self.line.pk]["suggested"], 1)


class ReturnsListTests(BuyerReturnTests):
    def test_list_shows_channel_photo_and_filters_by_channel_and_search(self):
        shopee = Order.objects.create(store=self.store, customer_name="Rina", phone="0813", channel="shopee",
            shipping_address="-", subtotal=1, total=1, status="completed", idempotency_key="rt-shp",
            external_order_id="SHP-77")
        sline = OrderItem.objects.create(order=shopee, product=self.shirt, variant=self.m, name="Kemeja",
            variant_name="M", sku="KMJ-M", unit_price=1, quantity=1, line_total=1)
        ReturnRequest.objects.create(order=shopee, order_item=sline, status="requested")
        self.ask()
        seller = Client()
        seller.force_login(self.seller)
        url = reverse("merchant-returns")
        page = seller.get(url)
        self.assertContains(page, "Shopee")
        self.assertEqual(len(page.context["returns"]), 2)
        only = seller.get(url, {"channel": "shopee"})
        self.assertEqual([r.order.channel for r in only.context["returns"]], ["shopee"])
        self.assertEqual(len(seller.get(url, {"q": "SHP-77"}).context["returns"]), 1)
        self.assertEqual(len(seller.get(url, {"q": "KMJ-M"}).context["returns"]), 2)
        part = seller.get(url, {"channel": "shopee"}, HTTP_HX_REQUEST="true")
        self.assertNotContains(part, "<html")
        self.assertContains(part, "Belum ada foto")
