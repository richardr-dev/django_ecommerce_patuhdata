"""Duplicate customers are found and merged into one, with everything that pointed at them."""
from decimal import Decimal

from django.urls import reverse
from test_sales_docs import Base

from apps.orders.models import Order
from apps.shops import customer_merge
from apps.shops.models import AuditLog, Customer
from apps.tax.models import CustomerCredit


class MergeTests(Base):
    def setUp(self):
        super().setUp()
        self.dup = Customer.objects.create(store=self.store, name="Budi Santoso",
            phone="0812-3456-7890", email="budi@example.com", company_name="CV Budi",
            tags="reseller", accepts_marketing=True)
        self.customer.phone = "+62 812 3456 7890"
        self.customer.tags = "vip"
        self.customer.save()
        self.client.force_login(self.owner)

    def test_same_phone_in_another_format_is_found(self):
        groups = customer_merge.duplicate_groups(self.store)
        self.assertEqual(len(groups), 1)
        self.assertEqual({c.pk for c in groups[0]["customers"]}, {self.customer.pk, self.dup.pk})
        self.assertIn("Nomor HP", groups[0]["reason"])

    def test_merge_moves_orders_credit_and_fills_blanks(self):
        inv = self.invoice(crm=False)
        Order.objects.filter(pk=inv.order_id).update(customer=self.dup)
        CustomerCredit.objects.create(store=self.store, customer=self.dup, buyer_name="Budi",
            kind="credit_note", date=self.today, amount=Decimal(5_000))
        result = customer_merge.merge_customers(self.store, self.customer.pk, [self.dup.pk],
            actor=self.owner)
        self.assertEqual(result["removed"], 1)
        self.assertFalse(Customer.objects.filter(pk=self.dup.pk).exists())
        self.assertEqual(Order.objects.get(pk=inv.order_id).customer_id, self.customer.pk)
        self.assertEqual(CustomerCredit.objects.get().customer_id, self.customer.pk)
        kept = Customer.objects.get(pk=self.customer.pk)
        self.assertEqual((kept.email, kept.company_name), ("budi@example.com", "CV Budi"))
        self.assertEqual(set(t.strip() for t in kept.tags.split(",")), {"vip", "reseller"})
        self.assertTrue(kept.accepts_marketing)
        log = AuditLog.objects.get(action="customer.merged")
        self.assertIn("Budi Santoso", log.details["removed"])

    def test_cannot_merge_into_nothing_or_across_stores(self):
        with self.assertRaises(customer_merge.MergeError):
            customer_merge.merge_customers(self.store, self.customer.pk, [self.customer.pk])
        other = Customer.objects.create(store=self.store, name="Siti")
        with self.assertRaises(customer_merge.MergeError):
            customer_merge.merge_customers(self.store, "00000000-0000-0000-0000-000000000000", [other.pk])

    def test_page_lists_and_merges(self):
        url = reverse("merchant-customer-duplicates")
        self.assertContains(self.client.get(url), "Nomor HP sama")
        self.client.post(url, {"primary": str(self.customer.pk), "merge": [str(self.dup.pk)]})
        self.assertEqual(Customer.objects.filter(store=self.store).count(), 1)
        self.assertContains(self.client.get(url), "Tidak ada pelanggan ganda")

    def test_customers_page_links_to_duplicates(self):
        self.assertContains(self.client.get(reverse("merchant-customers")),
            reverse("merchant-customer-duplicates"))
