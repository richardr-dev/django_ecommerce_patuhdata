"""Brand storefront features: reviews, wishlist, content pages, newsletter, related products, SEO."""
import json
import re
import uuid
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import Client, TestCase

from apps.catalog.models import Category, Product, ProductReview, WishlistItem
from apps.orders.models import Order, OrderItem
from apps.shops.models import NewsletterSubscriber, Store, StorePage


class BrandStorefrontTestCase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("brand-owner")
        self.store = Store.objects.create(owner=self.owner, name="Kopi Brand", slug="kopi-brand",
            description="Kopi pilihan dari Gayo.")
        self.other_store = Store.objects.create(owner=self.owner, name="Lain", slug="lain")
        self.category = Category.objects.create(store=self.store, name="Kopi", slug="kopi")
        self.product = Product.objects.create(store=self.store, category=self.category,
            name="Gayo Wine", slug="gayo", sku="GAYO", price=85000)
        self.buyer = User.objects.create_user("ayu", first_name="Ayu", last_name="Saraswati")
        self.client = Client(HTTP_X_STORE_SLUG=self.store.slug)

    def order(self, user=None, status="delivered", product=None):
        order = Order.objects.create(store=self.store, customer_user=user or self.buyer,
            status=status, customer_name="Ayu", phone="081234567890", shipping_address="Jakarta",
            subtotal=Decimal(85000), total=Decimal(85000), idempotency_key=str(uuid.uuid4()))
        product = product or self.product
        OrderItem.objects.create(order=order, product=product, name=product.name, sku=product.sku,
            unit_price=product.price, quantity=1, line_total=product.price)
        return order

    def owner_client(self):
        client = Client()
        client.force_login(self.owner)
        client.get("/merchant/", {"store": self.store.id})
        return client


class ProductReviewTests(BrandStorefrontTestCase):
    def review(self, client=None, **data):
        payload = {"rating": 5, "title": "Mantap", "body": "Aromanya wangi sekali."} | data
        return (client or self.client).post("/products/gayo/review/", payload)

    def test_anonymous_visitor_is_sent_to_login(self):
        response = self.review()
        self.assertRedirects(response, "/account/login/?next=/products/gayo/",
            fetch_redirect_response=False)
        self.assertFalse(ProductReview.objects.exists())

    def test_customer_must_have_received_the_product(self):
        self.client.force_login(self.buyer)
        self.order(status="paid")
        self.review()
        self.assertFalse(ProductReview.objects.exists())
        self.assertNotContains(self.client.get("/products/gayo/"), "Tulis ulasan")

    def test_buyer_review_is_published_with_abbreviated_name_and_rating(self):
        self.client.force_login(self.buyer)
        order = self.order()
        self.assertContains(self.client.get("/products/gayo/"), "Tulis ulasan")
        self.review()
        review = ProductReview.objects.get()
        self.assertEqual((review.order, review.display_name, review.store), (order, "Ayu S.", self.store))
        response = Client(HTTP_X_STORE_SLUG=self.store.slug).get("/products/gayo/")
        self.assertContains(response, "Aromanya wangi sekali.")
        self.assertContains(response, "Pembelian terverifikasi")
        self.assertContains(response, "(1 ulasan)")
        self.assertNotContains(response, "Saraswati")

    def test_resubmitting_updates_the_single_review(self):
        self.client.force_login(self.buyer)
        self.order()
        self.review()
        self.review(rating=3, body="Ternyata biasa saja.")
        review = ProductReview.objects.get()
        self.assertEqual((review.rating, review.body), (3, "Ternyata biasa saja."))

    def test_invalid_rating_is_rejected(self):
        self.client.force_login(self.buyer)
        self.order()
        for rating in (0, 6, "x"):
            self.review(rating=rating)
        self.assertFalse(ProductReview.objects.exists())

    def test_hidden_review_is_not_shown_or_counted(self):
        ProductReview.objects.create(store=self.store, product=self.product, user=self.buyer,
            rating=1, body="Disembunyikan", display_name="Ayu S.", is_published=False)
        response = self.client.get("/products/gayo/")
        self.assertNotContains(response, "Disembunyikan")
        self.assertNotContains(response, "aggregateRating")

    def test_structured_data_includes_rating_and_cannot_break_out_of_script(self):
        self.product.name = "Gayo</script><script>alert(1)</script>"
        self.product.save()
        ProductReview.objects.create(store=self.store, product=self.product, user=self.buyer,
            rating=4, body="Enak", display_name="Ayu S.")
        html = self.client.get("/products/gayo/").content.decode()
        self.assertNotIn("<script>alert(1)", html)
        raw = re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL).group(1)
        data = json.loads(raw)
        self.assertEqual(data["name"], self.product.name)
        self.assertEqual(data["aggregateRating"], {"@type": "AggregateRating", "ratingValue": 4.0,
            "reviewCount": 1})
        self.assertEqual(data["offers"]["priceCurrency"], "IDR")

    def test_merchant_moderates_but_cannot_author_reviews(self):
        review = ProductReview.objects.create(store=self.store, product=self.product,
            user=self.buyer, rating=2, body="Kurang segar", display_name="Ayu S.")
        client = self.owner_client()
        self.assertEqual(client.get("/merchant/setup/reviews/new/").status_code, 404)
        self.assertNotContains(client.get("/merchant/setup/reviews/"), "+ Tambah")
        response = client.post(f"/merchant/setup/reviews/{review.id}/",
            {"merchant_reply": "Maaf, kami kirim pengganti.", "rating": 5, "body": "Diubah"})
        self.assertEqual(response.status_code, 302)
        review.refresh_from_db()
        self.assertEqual((review.is_published, review.rating, review.body), (False, 2, "Kurang segar"))
        self.assertEqual(review.merchant_reply, "Maaf, kami kirim pengganti.")

    def test_reviews_are_store_scoped_in_back_office(self):
        product = Product.objects.create(store=self.other_store, name="Teh", slug="teh", sku="TEH",
            price=1000)
        review = ProductReview.objects.create(store=self.other_store, product=product,
            user=self.buyer, rating=5, body="Teh lain", display_name="Ayu S.")
        client = self.owner_client()
        self.assertNotContains(client.get("/merchant/setup/reviews/"), "Teh")
        self.assertEqual(client.get(f"/merchant/setup/reviews/{review.id}/").status_code, 404)


class WishlistTests(BrandStorefrontTestCase):
    def test_anonymous_toggle_goes_to_login(self):
        response = self.client.post(f"/wishlist/{self.product.id}/")
        self.assertRedirects(response, "/account/login/?next=/products/gayo/",
            fetch_redirect_response=False)
        self.assertEqual(self.client.get("/account/wishlist/").status_code, 302)

    def test_toggle_adds_lists_and_removes(self):
        self.client.force_login(self.buyer)
        self.client.post(f"/wishlist/{self.product.id}/")
        self.assertTrue(WishlistItem.objects.filter(user=self.buyer, product=self.product).exists())
        self.assertContains(self.client.get("/products/gayo/"), "Tersimpan di wishlist")
        self.assertContains(self.client.get("/account/wishlist/"), "Gayo Wine")
        self.client.post(f"/wishlist/{self.product.id}/")
        self.assertFalse(WishlistItem.objects.exists())
        self.assertContains(self.client.get("/account/wishlist/"), "Wishlist masih kosong")

    def test_get_does_not_change_wishlist_and_other_store_products_404(self):
        self.client.force_login(self.buyer)
        self.client.get(f"/wishlist/{self.product.id}/")
        self.assertFalse(WishlistItem.objects.exists())
        foreign = Product.objects.create(store=self.other_store, name="Teh", slug="teh",
            sku="TEH", price=1000)
        self.assertEqual(self.client.post(f"/wishlist/{foreign.id}/").status_code, 404)

    def test_redirect_back_ignores_foreign_hosts(self):
        self.client.force_login(self.buyer)
        response = self.client.post(f"/wishlist/{self.product.id}/",
            {"next": "https://evil.example.com/"})
        self.assertRedirects(response, "/products/gayo/", fetch_redirect_response=False)


class StorePageTests(BrandStorefrontTestCase):
    def test_published_page_renders_escaped_and_is_linked(self):
        StorePage.objects.create(store=self.store, title="Tentang Kami", slug="tentang-kami",
            body="Kami petani kopi.\n\n<script>alert(1)</script>", show_in_header=True)
        response = self.client.get("/pages/tentang-kami/")
        self.assertContains(response, "<p>Kami petani kopi.</p>", html=True)
        self.assertNotContains(response, "<script>alert(1)")
        home = self.client.get("/")
        self.assertContains(home, 'href="/pages/tentang-kami/"', count=2)

    def test_unpublished_and_other_store_pages_404(self):
        StorePage.objects.create(store=self.store, title="Draft", slug="draft", body="x",
            is_published=False)
        StorePage.objects.create(store=self.other_store, title="FAQ", slug="faq", body="x")
        self.assertEqual(self.client.get("/pages/draft/").status_code, 404)
        self.assertEqual(self.client.get("/pages/faq/").status_code, 404)

    def test_merchant_slug_must_be_unique_per_store(self):
        StorePage.objects.create(store=self.store, title="FAQ", slug="faq", body="x")
        StorePage.objects.create(store=self.other_store, title="Size", slug="ukuran", body="x")
        client = self.owner_client()
        payload = {"title": "FAQ 2", "body": "y", "position": 0, "is_published": "on",
            "show_in_footer": "on"}
        response = client.post("/merchant/setup/pages/new/", payload | {"slug": "faq"})
        self.assertContains(response, "sudah dipakai")
        client.post("/merchant/setup/pages/new/", payload | {"slug": "ukuran"})
        self.assertTrue(StorePage.objects.filter(store=self.store, slug="ukuran").exists())


class NewsletterTests(BrandStorefrontTestCase):
    def test_subscribing_requires_consent_and_deduplicates(self):
        self.client.post("/newsletter/subscribe/", {"email": "ayu@example.com"})
        self.assertFalse(NewsletterSubscriber.objects.exists())
        for email in ("Ayu@Example.com", "ayu@example.com"):
            self.client.post("/newsletter/subscribe/", {"email": email, "consent": "on"})
        subscriber = NewsletterSubscriber.objects.get()
        self.assertEqual((subscriber.store, subscriber.email), (self.store, "ayu@example.com"))

    def test_unsubscribe_needs_post_and_resubscribe_restores(self):
        subscriber = NewsletterSubscriber.objects.create(store=self.store, email="ayu@example.com")
        url = f"/newsletter/unsubscribe/{subscriber.unsubscribe_token}/"
        self.assertContains(self.client.get(url), "Berhenti berlangganan?")
        subscriber.refresh_from_db()
        self.assertTrue(subscriber.is_subscribed)
        self.client.post(url)
        subscriber.refresh_from_db()
        self.assertFalse(subscriber.is_subscribed)
        self.client.post("/newsletter/subscribe/", {"email": "ayu@example.com", "consent": "on"})
        subscriber.refresh_from_db()
        self.assertTrue(subscriber.is_subscribed)

    def test_other_store_token_404s(self):
        subscriber = NewsletterSubscriber.objects.create(store=self.other_store, email="a@b.co")
        url = f"/newsletter/unsubscribe/{subscriber.unsubscribe_token}/"
        self.assertEqual(self.client.post(url).status_code, 404)

    def test_csv_export_is_scoped_to_active_subscribers_of_the_brand(self):
        NewsletterSubscriber.objects.create(store=self.store, email="aktif@example.com")
        NewsletterSubscriber.objects.create(store=self.store, email="berhenti@example.com",
            unsubscribed_at="2026-01-01T00:00:00Z")
        NewsletterSubscriber.objects.create(store=self.other_store, email="lain@example.com")
        client = self.owner_client()
        body = client.get("/merchant/subscribers.csv").content.decode()
        self.assertIn("aktif@example.com", body)
        self.assertNotIn("berhenti@example.com", body)
        self.assertNotIn("lain@example.com", body)
        self.assertEqual(client.get("/merchant/setup/subscribers/new/").status_code, 404)

    def test_customer_without_staff_role_cannot_export(self):
        self.client.force_login(self.buyer)
        self.assertEqual(self.client.get("/merchant/subscribers.csv").status_code, 404)


class DiscoveryTests(BrandStorefrontTestCase):
    def test_related_products_share_category_and_exclude_self_and_inactive(self):
        Product.objects.create(store=self.store, category=self.category, name="Toraja", slug="toraja",
            sku="TOR", price=90000)
        Product.objects.create(store=self.store, category=self.category, name="Arsip", slug="arsip",
            sku="ARS", price=90000, is_active=False)
        Product.objects.create(store=self.store, name="Gelas", slug="gelas", sku="GLS", price=1000)
        response = self.client.get("/products/gayo/")
        related = [product.slug for product in response.context["related_products"]]
        self.assertEqual(related, ["toraja"])

    def test_robots_and_sitemap_list_public_urls_only(self):
        Product.objects.create(store=self.store, name="Arsip", slug="arsip", sku="ARS", price=1,
            is_active=False)
        StorePage.objects.create(store=self.store, title="FAQ", slug="faq", body="x")
        StorePage.objects.create(store=self.store, title="Draft", slug="draft", body="x",
            is_published=False)
        robots = self.client.get("/robots.txt").content.decode()
        self.assertIn("Disallow: /checkout/", robots)
        self.assertIn("Sitemap: http://testserver/sitemap.xml", robots)
        response = self.client.get("/sitemap.xml")
        self.assertEqual(response["Content-Type"], "application/xml")
        sitemap = response.content.decode()
        for url in ("http://testserver/", "/products/gayo/", "/pages/faq/"):
            self.assertIn(url, sitemap)
        self.assertNotIn("arsip", sitemap)
        self.assertNotIn("draft", sitemap)

    def test_product_seo_fields_drive_meta_tags(self):
        self.product.seo_title = "Kopi Gayo Wine 250g"
        self.product.seo_description = "Biji kopi fermentasi."
        self.product.save()
        response = self.client.get("/products/gayo/")
        self.assertContains(response, "<title>Kopi Gayo Wine 250g · Kopi Brand</title>", html=False)
        self.assertContains(response, '<meta name="description" content="Biji kopi fermentasi.">')
        self.assertContains(response, '<link rel="canonical" href="http://testserver/products/gayo/">')
