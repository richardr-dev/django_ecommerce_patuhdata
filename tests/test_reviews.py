"""Penilaian produk: any buyer who received the product reviews it from the order page (guests
included), with up to 3 photos; the product page shows a marketplace-style review section."""
import shutil
import tempfile
import uuid

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings

from apps.catalog.models import Product, ProductReview
from apps.orders.models import Order, OrderItem
from apps.shops.models import Store

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
MEDIA = tempfile.mkdtemp()


def photo(name="foto.png", content=PNG, content_type="image/png"):
    return SimpleUploadedFile(name, content, content_type=content_type)


@override_settings(MEDIA_ROOT=MEDIA)
class OrderReviewTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def setUp(self):
        self.store = Store.objects.create(owner=User.objects.create_user("seller"), name="Ruuma",
            slug="ruuma")
        self.product = Product.objects.create(store=self.store, name="Kue Lapis", slug="kue",
            sku="KUE", price=50000)

    def _order(self, status="completed", name="Ayu Saraswati", user=None):
        order = Order.objects.create(store=self.store, customer_name=name, phone="0812",
            shipping_address="x", subtotal=1, total=1, status=status, customer_user=user,
            idempotency_key=uuid.uuid4().hex)
        OrderItem.objects.create(order=order, product=self.product, name="Kue Lapis", sku="KUE",
            variant_name="20 cm", unit_price=1, quantity=1, line_total=1)
        return order

    def _review(self, order, client=None, **data):
        payload = {"token": str(order.access_token), "product": self.product.id, "rating": 5,
            "title": "Lembut", "body": "Lembut sekali dan wangi pandan."} | data
        return (client or Client()).post(f"/orders/{order.id}/review/", payload)

    def test_a_guest_reviews_from_the_order_page_with_photos(self):
        order = self._order()
        page = Client().get(f"/orders/{order.id}/?token={order.access_token}")
        self.assertContains(page, "Nilai produk")
        self.assertContains(page, "Belum dinilai")

        self._review(order, photos=[photo(), photo("b.png")])
        review = ProductReview.objects.get()
        self.assertIsNone(review.user)
        self.assertEqual((review.order, review.display_name, review.variant_name),
            (order, "Ayu S.", "20 cm"))
        self.assertEqual(review.photos.count(), 2)
        self.assertIn("stores/ruuma/reviews/", review.photos.first().file.name)

        detail = Client().get("/products/kue/")
        self.assertContains(detail, "Lembut sekali dan wangi pandan.")
        self.assertContains(detail, "Varian: 20 cm")
        self.assertContains(detail, "Pembelian terverifikasi")
        self.assertContains(detail, 'data-review-photo="', count=2, html=False)
        self.assertNotContains(detail, "Saraswati")

    def test_resubmitting_updates_the_one_review_and_keeps_photos_unless_replaced(self):
        order = self._order()
        self._review(order, photos=[photo()])
        self._review(order, rating=3, body="Ternyata agak manis buat saya.")
        review = ProductReview.objects.get()
        self.assertEqual((review.rating, review.photos.count()), (3, 1))
        self._review(order, photos=[photo(), photo("2.png"), photo("3.png")])
        self.assertEqual(ProductReview.objects.get().photos.count(), 3)

    def test_only_received_orders_and_only_their_buyer_can_review(self):
        paid = self._order(status="shipped")
        self._review(paid)
        self.assertFalse(ProductReview.objects.exists())

        done = self._order()
        stranger = Client().post(f"/orders/{done.id}/review/", {"product": self.product.id,
            "rating": 5, "body": "Saya bukan pembelinya."})
        self.assertEqual(stranger.status_code, 404)
        self.assertFalse(ProductReview.objects.exists())

    def test_fake_images_too_many_photos_and_short_reviews_are_refused(self):
        order = self._order()
        self._review(order, photos=[photo("x.png", b"<?php echo 1; ?>")])
        self._review(order, photos=[photo(f"{n}.png") for n in range(4)])
        reply = self._review(order, body="ok")
        self.assertFalse(ProductReview.objects.exists())
        self.assertRedirects(reply, f"/orders/{order.id}/#ulasan-{self.product.id}",
            fetch_redirect_response=False)

    def test_an_account_holder_keeps_one_review_per_product_across_orders(self):
        ayu = User.objects.create_user("ayu")
        client = Client()
        client.force_login(ayu)
        self._review(self._order(user=ayu), client=client)
        self._review(self._order(user=ayu), client=client, rating=4)
        review = ProductReview.objects.get()
        self.assertEqual((review.user, review.rating), (ayu, 4))

        page = client.get("/products/kue/")
        self.assertContains(page, "Ubah ulasan Anda")

    def test_finished_orders_ask_for_a_review_until_given(self):
        ayu = User.objects.create_user("ayu")
        client = Client()
        client.force_login(ayu)
        order = self._order(user=ayu)
        self.assertContains(client.get("/account/orders/"), "Beri penilaian")
        self._review(order, client=client)
        self.assertNotContains(client.get("/account/orders/"), "Beri penilaian")


class ProductReviewSectionTests(TestCase):
    def setUp(self):
        self.store = Store.objects.create(owner=User.objects.create_user("seller"), name="Ruuma",
            slug="ruuma")
        self.product = Product.objects.create(store=self.store, name="Kue Lapis", slug="kue",
            sku="KUE", price=50000)
        for number in range(8):
            order = Order.objects.create(store=self.store, customer_name=f"Pembeli {number}",
                phone="0812", shipping_address="x", subtotal=1, total=1, status="completed",
                idempotency_key=uuid.uuid4().hex)
            ProductReview.objects.create(store=self.store, product=self.product, order=order,
                rating=5 if number < 6 else 2, body=f"Ulasan nomor {number}",
                display_name=f"P{number}.", is_published=number != 7)

    def test_summary_filters_sorting_and_load_more(self):
        page = Client().get("/products/kue/")
        self.assertContains(page, "7 penilaian dari pembeli terverifikasi")
        self.assertContains(page, "5 Bintang (6)")
        self.assertContains(page, "2 Bintang (1)")
        self.assertContains(page, 'class="review pro-review"', count=6, html=False)
        self.assertContains(page, "Muat lebih banyak ulasan")
        self.assertNotContains(page, "Ulasan nomor 7")  # hidden by the seller

        low = Client().get("/products/kue/?bintang=2")
        self.assertContains(low, 'class="review pro-review"', count=1, html=False)
        self.assertContains(low, "Ulasan nomor 6")
        lowest_first = Client().get("/products/kue/?urut=terendah")
        self.assertLess(lowest_first.content.index(b"Ulasan nomor 6"),
            lowest_first.content.index(b"Ulasan nomor 5"))
        second = Client().get("/products/kue/?ulasan=2")
        self.assertContains(second, 'class="review pro-review"', count=1, html=False)
        self.assertContains(Client().get("/products/kue/?foto=1"), "Belum ada ulasan dengan filter ini")

    def test_no_reviews_yet(self):
        ProductReview.objects.all().delete()
        page = Client().get("/products/kue/")
        self.assertContains(page, "Belum ada penilaian")
        self.assertNotContains(page, "review-filters")
