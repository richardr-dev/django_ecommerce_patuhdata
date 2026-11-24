"""Back-office pages opened with real documents, not random ids: the URL smoke test only proves a page answers
404 for a document that does not exist, so a template that breaks on real data slips through. Each test here
names a page that once crashed."""
from django.urls import reverse
from test_sales_docs import Base


class StoreParameterTests(Base):
    def test_a_junk_store_parameter_is_ignored(self):
        """``?store=`` carries the store id (merchant-ui.js adds it); a slug or text must not be a server error."""
        self.client.force_login(self.owner)
        invoice = self.invoice()
        for junk in ("toko-sales", "abc", "1; DROP", " ", "99999999"):
            page = self.client.get(reverse("merchant-invoice-detail", args=[invoice.pk]), {"store": junk})
            self.assertEqual(page.status_code, 200, junk)
