"""The invoice list and receivables run a fixed number of queries, however many invoices are on the page."""
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from test_sales_docs import Base


class InvoiceListQueryTests(Base):
    def queries(self, name):
        with CaptureQueriesContext(connection) as captured:
            self.assertEqual(self.client.get(reverse(name)).status_code, 200)
        return len(captured.captured_queries)

    def test_no_query_per_row(self):
        self.client.force_login(self.owner)
        for _ in range(3):
            self.invoice(kabel=1)
        few = {name: self.queries(name) for name in ("merchant-sales-invoices", "merchant-receivables")}
        for _ in range(12):
            self.invoice(kabel=1)
        for name, count in few.items():
            self.assertLessEqual(self.queries(name), count + 2, name)
