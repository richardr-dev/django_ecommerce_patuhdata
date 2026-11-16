"""Price lists per customer group and payment terms on the customer (tax/pricing.py)."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.catalog.models import PriceList, PriceListItem, Product, ProductVariant
from apps.shops.models import Customer, Store, StoreStaff
from apps.tax.models import Invoice
from apps.tax.pricing import customer_price, due_date_for, list_price, price_map


class PricingRulesTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("pemilik", password="x")
        self.store = Store.objects.create(owner=self.owner, name="Maju Jaya", slug="maju")
        self.kabel = Product.objects.create(store=self.store, name="Kabel", slug="kabel", sku="KBL", price=100_000)
        self.lampu = Product.objects.create(store=self.store, name="Lampu", slug="lampu", sku="LMP", price=50_000)
        self.grosir = PriceList.objects.create(store=self.store, name="Grosir", discount_percent=Decimal(10))
        PriceListItem.objects.create(price_list=self.grosir, product=self.kabel, price=80_000)

    def test_own_row_then_discount_then_nothing(self):
        self.assertEqual(list_price(self.grosir, self.kabel), Decimal(80_000))      # its own row
        self.assertEqual(list_price(self.grosir, self.lampu), Decimal(45_000))      # retail less 10%
        plain = PriceList.objects.create(store=self.store, name="Proyek")
        self.assertIsNone(list_price(plain, self.lampu))                            # nothing to say
        self.assertIsNone(list_price(None, self.lampu))

    def test_a_variant_row_wins_over_the_product_row(self):
        merah = ProductVariant.objects.create(product=self.kabel, name="Merah", sku="KBL-M", price=120_000)
        self.assertEqual(list_price(self.grosir, self.kabel, merah), Decimal(80_000))
        PriceListItem.objects.create(price_list=self.grosir, product=self.kabel, variant=merah, price=95_000)
        self.assertEqual(list_price(self.grosir, self.kabel, merah), Decimal(95_000))

    def test_customer_price_and_terms(self):
        toko = Customer.objects.create(store=self.store, name="Toko Ani", price_list=self.grosir, payment_terms_days=30)
        self.assertEqual(customer_price(toko, self.kabel), Decimal(80_000))
        today = timezone.localdate()
        self.assertEqual(due_date_for(toko, today), today + timedelta(days=30))
        self.assertIsNone(due_date_for(Customer.objects.create(store=self.store, name="Budi"), today))
        self.assertEqual(price_map(self.store), {self.kabel.pk: {str(self.grosir.pk): "80000"}})


class PriceListOnTheInvoiceFormTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("pemilik", password="x")
        self.store = Store.objects.create(owner=self.owner, name="Maju Jaya", slug="maju")
        self.kabel = Product.objects.create(store=self.store, name="Kabel", slug="kabel", sku="KBL", price=100_000)
        self.grosir = PriceList.objects.create(store=self.store, name="Grosir", discount_percent=Decimal(10))
        PriceListItem.objects.create(price_list=self.grosir, product=self.kabel, price=80_000)
        self.toko = Customer.objects.create(store=self.store, name="Toko Ani", phone="0811",
            price_list=self.grosir, payment_terms_days=30)
        self.client.force_login(self.owner)
        self.today = timezone.localdate()

    def test_the_form_carries_the_lists_and_the_customer_search_names_them(self):
        page = self.client.get(reverse("merchant-invoice-new")).content.decode()
        self.assertIn(f'"lists": {{"{self.grosir.pk}": "80000"}}', page)
        self.assertIn('"discount": "10.00"', page)
        found = self.client.get(reverse("merchant-customer-search") + "?q=ani").json()["results"][0]
        self.assertEqual((found["price_list_id"], found["payment_terms_days"]), (str(self.grosir.pk), 30))
        prefilled = self.client.get(reverse("merchant-invoice-new") + f"?customer={self.toko.id}").content.decode()
        self.assertIn(f'data-list="{self.grosir.pk}" data-terms="30"', prefilled)

    def test_a_credit_invoice_without_a_due_date_takes_the_customer_terms(self):
        reply = self.client.post(reverse("merchant-invoice-new"), {"channel": "manual",
            "issued_on": self.today.isoformat(), "buyer_name": "Toko Ani", "customer_id": str(self.toko.id),
            "product[]": [self.kabel.pk], "description[]": [""], "qty[]": ["2"], "unit_price[]": ["80000"],
            "line_discount[]": [""], "discount": "0", "shipping": "0", "payment_status": "credit", "due_date": ""})
        self.assertEqual(reply.status_code, 302)
        invoice = Invoice.objects.get(order__customer=self.toko)
        self.assertEqual(invoice.due_date, self.today + timedelta(days=30))
        self.assertEqual(invoice.subtotal, Decimal(160_000))


class PriceListPagesTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("pemilik", password="x")
        self.store = Store.objects.create(owner=self.owner, name="Maju Jaya", slug="maju")
        self.kabel = Product.objects.create(store=self.store, name="Kabel", slug="kabel", sku="KBL", price=100_000)
        self.client.force_login(self.owner)

    def test_create_add_a_row_remove_it_and_update(self):
        reply = self.client.post(reverse("merchant-price-lists"), {"name": "Reseller", "discount_percent": "5"})
        row = PriceList.objects.get(store=self.store, name="Reseller")
        self.assertRedirects(reply, reverse("merchant-price-list", args=[row.pk]), fetch_redirect_response=False)
        self.client.post(reverse("merchant-price-list", args=[row.pk]), {"action": "add", "product": self.kabel.pk, "price": "85.000"})
        item = row.items.get()
        self.assertEqual(item.price, Decimal(85_000))
        self.assertContains(self.client.get(reverse("merchant-price-list", args=[row.pk])), "85.000")
        self.client.post(reverse("merchant-price-list", args=[row.pk]), {"action": "remove", "item": item.pk})
        self.assertFalse(row.items.exists())
        self.client.post(reverse("merchant-price-list", args=[row.pk]), {"action": "update", "name": "Reseller B",
            "discount_percent": "7.5", "notes": "khusus luar kota"})
        row.refresh_from_db()
        self.assertEqual((row.name, row.discount_percent, row.is_active), ("Reseller B", Decimal("7.5"), False))
        self.assertContains(self.client.get(reverse("merchant-price-lists")), "Reseller B")

    def test_a_duplicate_name_and_a_bad_percent_are_refused(self):
        PriceList.objects.create(store=self.store, name="Grosir")
        self.client.post(reverse("merchant-price-lists"), {"name": "Grosir", "discount_percent": "0"})
        self.assertEqual(PriceList.objects.filter(store=self.store).count(), 1)
        self.client.post(reverse("merchant-price-lists"), {"name": "Proyek", "discount_percent": "120"})
        self.assertFalse(PriceList.objects.filter(name="Proyek").exists())

    def test_a_viewer_reads_but_cannot_change(self):
        row = PriceList.objects.create(store=self.store, name="Grosir")
        viewer = User.objects.create_user("pelihat", password="x")
        StoreStaff.objects.create(store=self.store, user=viewer, role="viewer")
        other = Client()
        other.force_login(viewer)
        self.assertEqual(other.get(reverse("merchant-price-list", args=[row.pk])).status_code, 200)
        self.assertEqual(other.post(reverse("merchant-price-lists"), {"name": "X"}).status_code, 404)
        self.assertEqual(other.post(reverse("merchant-price-list", args=[row.pk]), {"action": "add",
            "product": self.kabel.pk, "price": "1"}).status_code, 404)


class CustomerTermsTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("pemilik", password="x")
        self.store = Store.objects.create(owner=self.owner, name="Maju Jaya", slug="maju")
        self.grosir = PriceList.objects.create(store=self.store, name="Grosir")
        self.client.force_login(self.owner)

    def test_terms_are_saved_from_the_customer_page_and_the_add_dialog(self):
        ani = Customer.objects.create(store=self.store, name="Toko Ani")
        self.client.post(reverse("merchant-customer-terms", args=[ani.id]), {"payment_terms_days": "45", "price_list": self.grosir.pk})
        ani.refresh_from_db()
        self.assertEqual((ani.payment_terms_days, ani.price_list), (45, self.grosir))
        self.assertContains(self.client.get(reverse("merchant-customer-detail", args=[ani.id])), "Termin 45 hari")
        self.client.post(reverse("merchant-customer-terms", args=[ani.id]), {"payment_terms_days": "", "price_list": ""})
        ani.refresh_from_db()
        self.assertEqual((ani.payment_terms_days, ani.price_list), (None, None))
        self.client.post(reverse("merchant-customer-new"), {"name": "CV Budi", "payment_terms_days": "14", "price_list": self.grosir.pk})
        budi = Customer.objects.get(store=self.store, name="CV Budi")
        self.assertEqual((budi.payment_terms_days, budi.price_list), (14, self.grosir))
