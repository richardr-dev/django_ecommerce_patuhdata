"""Double-submit guards on money forms."""
from django.urls import reverse
from test_sales_docs import Base


class ManualJournalFormTests(Base):
    """Jurnal umum: a refused entry keeps what was typed, and a double submit books once."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)
        self.url = reverse("merchant-journal-new")

    def post(self, **extra):
        data = {"date": "2026-01-05", "memo": "Setoran modal", "account[]": ["1100", "3100"],
            "debit[]": ["500000", ""], "credit[]": ["", "500000"], **extra}
        return self.client.post(self.url, data)

    def test_error_keeps_typed_lines(self):
        page = self.post(**{"credit[]": ["", "400000"]})
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "tidak seimbang")
        self.assertContains(page, 'value="500000"')
        self.assertContains(page, "Setoran modal")
        self.assertContains(page, 'value="400000"')

    def test_bad_date_is_a_plain_message(self):
        self.assertContains(self.post(date=""), "Isi tanggal jurnal")

    def test_double_submit_books_one_entry(self):
        from apps.accounting.models import JournalEntry
        first = self.post(idem="k1")
        self.assertEqual(first.status_code, 302)
        count = JournalEntry.objects.filter(store=self.store, source_type="journal").count()
        self.post(idem="k1")
        self.assertEqual(JournalEntry.objects.filter(store=self.store, source_type="journal").count(), count)

    def test_attachment_is_saved_with_the_journal_and_a_bad_file_books_nothing(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from apps.accounting.models import Attachment, JournalEntry
        data = {"date": "2026-01-05", "memo": "Setoran modal", "account[]": ["1100", "3100"],
            "debit[]": ["500000", ""], "credit[]": ["", "500000"]}
        bad = self.client.post(self.url, {**data, "attachment": SimpleUploadedFile("x.pdf", b"not a pdf")})
        self.assertContains(bad, "bukan PDF")
        self.assertFalse(JournalEntry.objects.filter(store=self.store, source_type="journal").exists())
        good = self.client.post(self.url, {**data, "attachment": SimpleUploadedFile("bukti.pdf", b"%PDF-1.4 ok")})
        self.assertEqual(good.status_code, 302)
        self.assertEqual(Attachment.objects.filter(store=self.store, kind="journal").count(), 1)

    def test_copy_fills_the_form_with_the_same_lines(self):
        from apps.accounting.models import JournalEntry
        self.post(idem="c1")
        entry = JournalEntry.objects.get(store=self.store, source_type="journal")
        page = self.client.get(self.url, {"copy": entry.pk})
        self.assertContains(page, 'value="Setoran modal"')
        self.assertContains(page, 'value="500000"')
        detail = self.client.get(reverse("merchant-journal-detail", args=[entry.pk]))
        self.assertContains(detail, "Salin jurnal")
        self.assertNotContains(self.client.get(self.url, {"copy": 99999}), 'value="Setoran modal"')
