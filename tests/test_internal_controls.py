"""Internal control: a read-only accountant, a history on each document, attachments, fraud checks."""
import io

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from PIL import Image
from test_sales_docs import Base

from apps.accounting import attachments
from apps.accounting.models import Attachment, Bill, Supplier
from apps.accounting.payables import create_bill
from apps.shops.audit import trail
from apps.shops.models import AuditLog, StoreStaff


def pdf(name="faktur.pdf", body=b"%PDF-1.4 test"):
    return SimpleUploadedFile(name, body, content_type="application/pdf")


def png(name="bukti.png"):
    buffer = io.BytesIO()
    Image.new("RGB", (20, 20), (10, 90, 200)).save(buffer, "PNG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")


class AccountantRoleTests(Base):
    def setUp(self):
        super().setUp()
        self.accountant = User.objects.create_user("akuntan", password="x")
        StoreStaff.objects.create(store=self.store, user=self.accountant, role="accountant")
        self.client.force_login(self.accountant)

    def test_reads_the_books_and_sales_pages(self):
        for name in ("merchant-profit-loss", "merchant-balance-sheet", "merchant-trial-balance", "merchant-journal-list",
                "merchant-bills", "merchant-receivables", "merchant-sales-invoices", "merchant-expenses",
                "merchant-books-checks", "merchant-budget", "merchant-tax"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)

    def test_changes_nothing(self):
        invoice = self.invoice()
        before = Bill.objects.count()
        for name, args, data in (
                ("merchant-invoice-delete", [invoice.pk], {}),
                ("merchant-bill-new", [], {"supplier": "1"}),
                ("merchant-journal-new", [], {"memo": "x"}),
                ("merchant-budget-edit", [], {"year": 2026}),
                ("merchant-cost-centers", [], {"action": "add", "code": "X", "name": "X"})):
            self.assertEqual(self.client.post(reverse(name, args=args), data).status_code, 404, name)
        self.assertEqual(Bill.objects.count(), before)
        invoice.refresh_from_db()
        self.assertNotEqual(invoice.status, "void")

    def test_cannot_open_staff_or_settings_and_is_shown_as_an_accountant(self):
        self.assertEqual(self.client.get(reverse("merchant-staff")).status_code, 404)
        page = self.client.get(reverse("merchant-profit-loss"))
        self.assertContains(page, "Auditor")
        self.assertTrue(page.context["merchant_is_accountant"])
        self.assertEqual(page.context["merchant_role"], "viewer")

    def test_role_can_be_given_from_the_staff_page(self):
        self.client.force_login(self.owner)
        page = self.client.get(reverse("merchant-staff"))
        self.assertContains(page, "Auditor (hanya lihat)")


class AuditTrailTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)

    def test_invoice_history_shows_who_paid_and_voided(self):
        invoice = self.invoice(credit=True)
        self.client.post(reverse("merchant-invoice-detail", args=[invoice.pk]), {"action": "pay",
            "amount": "50000", "paid_on": self.today.isoformat(), "method": "transfer"})
        self.client.post(reverse("merchant-invoice-detail", args=[invoice.pk]), {"action": "void"})
        what = [row["what"] for row in trail(self.store, invoice)]
        self.assertIn("Pembayaran dicatat", what)
        self.assertIn("Invoice dibatalkan", what)
        page = self.client.get(reverse("merchant-invoice-detail", args=[invoice.pk]))
        self.assertContains(page, "Riwayat dokumen")
        self.assertContains(page, self.owner.username)

    def test_bill_history_and_its_page(self):
        supplier = Supplier.objects.create(store=self.store, name="Pemasok")
        self.client.post(reverse("merchant-bill-new"), {"supplier_id": supplier.pk, "bill_date": self.today.isoformat(),
            "description[]": ["Kemasan"], "qty[]": ["2"], "unit_price[]": ["50000"], "sku[]": [""],
            "category": "1400"})
        bill = Bill.objects.get()
        self.client.post(reverse("merchant-bill-detail", args=[bill.pk]), {"action": "pay", "amount": "40000",
            "paid_on": self.today.isoformat()})
        what = [row["what"] for row in trail(self.store, bill)]
        self.assertEqual(what[:2], ["Pembayaran tagihan", "Tagihan dibuat"])
        self.assertContains(self.client.get(reverse("merchant-bill-detail", args=[bill.pk])), "Riwayat dokumen")


class AttachmentTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)
        supplier = Supplier.objects.create(store=self.store, name="Pemasok")
        self.bill = create_bill(self.store, supplier=supplier, lines=[{"description": "x", "qty": 1,
            "unit_price": 100_000}], bill_date=self.today)
        self.add = reverse("merchant-attachment-add")

    def upload(self, file, kind="bill", object_id=None):
        return self.client.post(self.add, {"kind": kind, "object_id": object_id or self.bill.pk, "file": file,
            "next": reverse("merchant-bill-detail", args=[self.bill.pk])})

    def test_pdf_and_picture_are_kept_and_shown_and_served_privately(self):
        self.upload(pdf())
        self.upload(png())
        self.assertEqual(Attachment.objects.count(), 2)
        page = self.client.get(reverse("merchant-bill-detail", args=[self.bill.pk]))
        self.assertContains(page, "faktur.pdf")
        item = Attachment.objects.get(name="faktur.pdf")
        response = self.client.get(reverse("merchant-attachment-file", args=[item.pk]))
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertIn("Lampiran ditambahkan", [r["what"] for r in trail(self.store, self.bill)])

    def test_fakes_wrong_types_and_big_files_are_refused(self):
        self.upload(pdf(body=b"<script>alert(1)</script>"))
        self.upload(SimpleUploadedFile("x.png", b"not a picture", content_type="image/png"))
        self.upload(SimpleUploadedFile("x.exe", b"MZ", content_type="application/octet-stream"))
        big = SimpleUploadedFile("big.pdf", b"%PDF" + b"0" * (attachments.MAX_BYTES + 1), content_type="application/pdf")
        self.upload(big)
        self.assertEqual(Attachment.objects.count(), 0)

    def test_another_stores_record_or_unknown_kind_is_not_attachable(self):
        self.upload(pdf(), kind="bill", object_id="00000000-0000-0000-0000-000000000000")
        self.upload(pdf(), kind="nonsense")
        self.assertEqual(Attachment.objects.count(), 0)

    def test_viewer_reads_but_cannot_add_or_delete(self):
        self.upload(pdf())
        item = Attachment.objects.get()
        viewer = User.objects.create_user("vw3", password="x")
        StoreStaff.objects.create(store=self.store, user=viewer, role="viewer")
        self.client.force_login(viewer)
        self.assertEqual(self.client.get(reverse("merchant-attachment-file", args=[item.pk])).status_code, 200)
        self.assertEqual(self.upload(pdf("lain.pdf")).status_code, 404)
        self.assertEqual(self.client.post(reverse("merchant-attachment-delete", args=[item.pk])).status_code, 404)
        self.assertEqual(Attachment.objects.count(), 1)

    def test_delete_removes_it_and_is_logged(self):
        self.upload(pdf())
        item = Attachment.objects.get()
        self.client.post(reverse("merchant-attachment-delete", args=[item.pk]))
        self.assertEqual(Attachment.objects.count(), 0)
        self.assertTrue(AuditLog.objects.filter(action="attachment.removed").exists())


class FraudCheckTests(Base):
    def check(self, key):
        from apps.accounting import controls
        return next(c for c in controls.run_checks(self.store, self.today) if c.key == key)

    def bill(self, supplier, number="", amount=100_000, days_ago=0):
        import datetime
        return create_bill(self.store, supplier=supplier, lines=[{"description": "x", "qty": 1,
            "unit_price": amount}], bill_date=self.today - datetime.timedelta(days=days_ago), number=number)

    def test_the_same_supplier_invoice_number_twice_is_flagged(self):
        from apps.accounting import controls
        supplier = Supplier.objects.create(store=self.store, name="Pemasok")
        self.bill(supplier, "INV-7", 100_000, 3)
        self.assertEqual(self.check("duplicate_bills").status, controls.OK)
        self.bill(supplier, "INV-7", 120_000, 1)
        check = self.check("duplicate_bills")
        self.assertEqual(check.status, controls.WARN)
        self.assertIn("INV-7", check.items[0])

    def test_same_amount_on_the_same_day_is_flagged_too(self):
        from apps.accounting import controls
        supplier = Supplier.objects.create(store=self.store, name="Pemasok")
        self.bill(supplier, "A", 250_000)
        self.bill(supplier, "B", 250_000)
        self.assertEqual(self.check("duplicate_bills").status, controls.WARN)

    def test_a_cost_far_above_the_usual_is_listed(self):
        import datetime

        from apps.accounting import controls
        from apps.accounting.models import Expense
        for i in range(7):
            Expense.objects.create(store=self.store, date=self.today - datetime.timedelta(days=i), category="6210",
                description=f"Rutin {i}", amount=200_000)
        self.assertEqual(self.check("unusual_expenses").status, controls.OK)
        Expense.objects.create(store=self.store, date=self.today, category="6210", description="Besar", amount=5_000_000)
        check = self.check("unusual_expenses")
        self.assertEqual(check.status, controls.INFO)
        self.assertIn("Besar", check.items[0])

    def test_big_manual_journals_and_voided_paid_invoices(self):
        from apps.accounting import controls
        from apps.accounting.ledger import post_journal
        from apps.accounting.ledger import chart_of_accounts
        cost = next(c for c, a in chart_of_accounts(self.store).items() if a.type == "expense" and c not in {"5100", "6100"})
        post_journal(self.store, date=self.today, memo="Koreksi besar", lines=[(cost, 20_000_000, 0), ("1100", 0, 20_000_000)])
        check = self.check("manual_journals")
        self.assertEqual(check.status, controls.INFO)
        self.assertIn("Koreksi besar", check.items[0])
        invoice = self.invoice(credit=False)             # paid on creation
        from apps.tax.posting import void_invoice
        void_invoice(invoice)
        self.assertEqual(self.check("voids_after_payment").status, controls.WARN)
