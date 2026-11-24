"""Buat banyak invoice: a paste from Excel grouped by reference, and the same lines billed to many customers.
Every invoice goes through ``create_sales_invoice`` (stock, HPP, PPN, journal), and a batch is made whole or
not at all.

Worked example: PO-1 for CV Maju (2 Kabel at the list price, 1 Jasa pasang 250.000) and PO-2 for a new buyer
(3 Lampu at 45.000 less 5.000). PPN 11%: PO-1 = (200.000 + 250.000) × 1,11 = 499.500; PO-2 = 130.000 × 1,11 =
144.300."""
from decimal import Decimal

from django.contrib.auth.models import User
from django.urls import reverse
from test_sales_docs import Base, balance

from apps.accounting.controls import _balances, invoice_open_balance
from apps.accounting.ledger import RECEIVABLE
from apps.catalog.models import PriceList, PriceListItem
from apps.shops.models import Customer, StoreStaff
from apps.tax import invoice_batch as batch
from apps.tax.models import Invoice

D = Decimal
PASTE = ("No. ref\tPelanggan\tTanggal\tJatuh tempo\tBarang\tJumlah\tHarga\tDiskon\n"
    "PO-1\tCV Maju\t\t\tKBL\t2\t\t\n"
    "PO-1\tCV Maju\t\t\tJasa pasang\t1\t250.000\t\n"
    "PO-2;Toko Baru;;;lmp;3;45000;5000\n")


class BatchTests(Base):
    def setUp(self):
        super().setUp()
        self.maju = Customer.objects.create(store=self.store, name="CV Maju", payment_terms_days=14)

    def make(self, text=PASTE, **extra):
        return batch.create_all(self.store, batch.parse_paste(self.store, text), **extra)

    def test_paste_groups_rows_by_reference(self):
        plans = batch.parse_paste(self.store, PASTE)
        self.assertEqual([(p.reference, p.buyer_name, p.ok, p.total) for p in plans],
            [("PO-1", "CV Maju", True, D(499_500)), ("PO-2", "Toko Baru", True, D(144_300))])
        self.assertTrue(plans[1].new_customer)
        self.assertEqual(Invoice.objects.count(), 0)              # planning writes nothing
        one, two = self.make()
        self.assertEqual((one.total, two.total), (D(499_500), D(144_300)))
        self.assertEqual(one.due_date, self.today + __import__("datetime").timedelta(days=14))   # the terms
        self.assertEqual(one.order.external_order_id, "PO-1")
        self.assertEqual(one.items.count(), 2)
        self.assertEqual((self.on_hand(self.kabel), self.on_hand(self.lampu)), (98, 97))
        self.assertTrue(Customer.objects.filter(store=self.store, name="Toko Baru").exists())
        self.assertEqual(balance(self.store, RECEIVABLE), D(643_800))
        self.assertEqual(invoice_open_balance(self.store, self.today, _balances(self.store)).status, "ok")

    def test_mistakes_are_named_per_invoice_and_nothing_is_made(self):
        plans = batch.parse_paste(self.store, "A;CV Maju;31/13/2026;;KBL;1;;\nB;CV Maju;;;Konsultasi;1;;\n"
            "C;;;;KBL;1;;\nD;CV Maju;;;KBL;-1;;\n")
        self.assertEqual([p.ok for p in plans], [False, False, False, False])
        self.assertIn("tanggal", plans[0].errors[0])
        self.assertIn("isi harganya", plans[1].errors[0])
        self.assertIn("nama pelanggan", plans[2].errors[0])
        self.assertIn("jumlah", plans[3].errors[0])
        with self.assertRaises(batch.BatchError):
            batch.create_all(self.store, plans)
        self.assertEqual(Invoice.objects.count(), 0)

    def test_one_invoice_failing_rolls_back_the_others_and_their_numbers(self):
        with self.assertRaisesMessage(batch.BatchError, "ref PO-2"):
            self.make("PO-1;CV Maju;;;KBL;2;;\nPO-2;CV Maju;;;LMP;500;;\n")   # only 100 Lampu
        self.assertEqual(Invoice.objects.count(), 0)
        self.assertEqual(self.on_hand(self.kabel), 100)
        self.assertEqual(self.invoice().number.rsplit("/", 1)[1], "000001")  # no gap in the series

    def test_the_same_lines_to_many_customers_at_each_ones_price(self):
        grosir = PriceList.objects.create(store=self.store, name="Grosir")
        PriceListItem.objects.create(price_list=grosir, product=self.kabel, price=D(90_000))
        self.maju.price_list = grosir
        self.maju.save()
        plans = batch.for_customers(self.store, [self.customer, self.maju],
            items=[("KBL", "1", "", ""), ("Biaya kirim", "1", "10000", "")], issued_on=self.today,
            reference="Nov")
        made = batch.create_all(self.store, plans)
        self.assertEqual([i.subtotal for i in made], [D(110_000), D(100_000)])
        self.assertEqual({i.buyer_name for i in made}, {"Budi", "CV Maju"})

    def test_credit_limit_holds_unless_approved(self):
        self.maju.credit_limit = D(0)
        self.maju.save()
        with self.assertRaisesMessage(batch.BatchError, "CV Maju"):
            self.make("PO-1;CV Maju;;;KBL;1;;\n")
        self.assertEqual(len(self.make("PO-1;CV Maju;;;KBL;1;;\n", credit_check=False)), 1)


class BatchPageTests(Base):
    url = reverse("merchant-invoice-batch")

    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)

    def test_preview_then_create_once(self):
        self.assertContains(self.client.get(self.url), "Tempel baris dari Excel")
        self.assertContains(self.client.get(self.url + "?cara=pelanggan"), "Budi")
        page = self.client.post(self.url, {"cara": "tempel", "rows": PASTE, "action": "preview", "idem": "k1"})
        self.assertContains(page, "Pratinjau")
        self.assertContains(page, "Buat 2 invoice")
        self.assertEqual(Invoice.objects.count(), 0)
        data = {"cara": "tempel", "rows": PASTE, "action": "create", "idem": "k1"}
        self.assertRedirects(self.client.post(self.url, data), reverse("merchant-sales-invoices"),
            fetch_redirect_response=False)
        self.client.post(self.url, data)                       # pressed twice
        self.assertEqual(Invoice.objects.count(), 2)

    def test_create_is_refused_while_a_row_is_wrong(self):
        page = self.client.post(self.url, {"cara": "tempel", "rows": "A;Budi;;;Konsultasi;1;;", "action": "create",
            "idem": "k2"})
        self.assertContains(page, "isi harganya")
        self.assertEqual(Invoice.objects.count(), 0)

    def test_customers_mode(self):
        other = Customer.objects.create(store=self.store, name="Sari")
        page = self.client.post(self.url, {"cara": "pelanggan", "customer[]": [self.customer.pk, other.pk],
            "item[]": ["Iuran bulanan", ""], "qty[]": ["1", "1"], "price[]": ["100000", ""],
            "line_discount[]": ["", ""], "issued_on": self.today.isoformat(), "action": "create", "idem": "k3"})
        self.assertEqual(page.status_code, 302)
        self.assertEqual(sorted(Invoice.objects.values_list("buyer_name", flat=True)), ["Budi", "Sari"])

    def test_viewer_may_not_open_it(self):
        viewer = User.objects.create_user("sd-viewer", password="x")
        StoreStaff.objects.create(store=self.store, user=viewer, role="viewer")
        self.client.force_login(viewer)
        self.assertIn(self.client.get(self.url).status_code, {403, 404})
