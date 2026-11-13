"""Transfers between two brands of one Organization: two books, one transaction."""
from decimal import Decimal

from django.contrib.auth.models import User
from django.urls import reverse
from test_sales_docs import Base

from apps.accounting import controls, intercompany
from apps.accounting.ledger import (
    COGS,
    INTERCO_PAYABLE,
    INTERCO_RECEIVABLE,
    INTERCO_SALES,
    INVENTORY,
    account_balances,
    post_journal,
)
from apps.catalog.models import InventoryLevel, Product, Warehouse
from apps.shops.models import Organization, Store


def balances(store):
    return {a.code: a.balance for a in account_balances(store)}


class IntercompanyTests(Base):
    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(name="Grup", slug="grup", owner=self.owner)
        Store.objects.filter(pk=self.store.pk).update(organization=self.org)
        self.store.refresh_from_db()
        self.other = Store.objects.create(owner=self.owner, organization=self.org, name="Brand B", slug="brand-b")
        warehouse = Warehouse.objects.create(store=self.other, code="B1", name="Gudang B")
        self.other_kabel = Product.objects.create(store=self.other, sku="KBL", name="Kabel", slug="kabel-b",
            price=120_000, cost_price=0)
        InventoryLevel.objects.create(warehouse=warehouse, product=self.other_kabel, on_hand=0)
        self.transfer = intercompany.create_transfer(self.store, self.other, transfer_date=self.today,
            number="IC-1", lines=[{"sku": "KBL", "qty": Decimal(10), "unit_price": Decimal(80_000)}], actor=self.owner)

    def test_stock_moves_between_the_two_stores(self):
        self.assertEqual(self.on_hand(self.kabel), 90)
        self.assertEqual(InventoryLevel.objects.get(product=self.other_kabel).on_hand, 10)
        self.other_kabel.refresh_from_db()
        self.assertEqual(self.other_kabel.cost_price, Decimal("80000.00"))  # buyer's cost is the transfer price

    def test_each_book_gets_its_own_entries(self):
        seller, buyer = balances(self.store), balances(self.other)
        self.assertEqual(seller[INTERCO_RECEIVABLE], Decimal(800_000))
        self.assertEqual(seller[INTERCO_SALES], Decimal(800_000))
        self.assertEqual(seller[COGS], Decimal(600_000))            # 10 x its average cost of 60.000
        self.assertEqual(buyer[INVENTORY], Decimal(800_000))
        self.assertEqual(buyer[INTERCO_PAYABLE], Decimal(800_000))
        self.assertEqual(seller[INTERCO_PAYABLE], 0)
        self.assertEqual(buyer[INTERCO_RECEIVABLE], 0)

    def test_group_receivable_and_payable_offset(self):
        receivable, payable = intercompany.group_balances(self.org)
        self.assertEqual(receivable, payable)
        self.assertEqual(controls.intercompany_tie(self.store, self.today, {}).status, controls.OK)
        post_journal(self.other, date=self.today, memo="bayar sebagian", lines=[(INTERCO_PAYABLE, 100_000, 0), (INVENTORY, 0, 100_000)])
        self.assertEqual(controls.intercompany_tie(self.store, self.today, {}).status, controls.INFO)

    def test_refusals_leave_nothing_behind(self):
        outsider = Store.objects.create(owner=self.owner, name="Luar", slug="luar")
        with self.assertRaises(intercompany.TransferError):
            intercompany.create_transfer(self.store, outsider, transfer_date=self.today,
                lines=[{"sku": "KBL", "qty": Decimal(1), "unit_price": Decimal(1)}])
        with self.assertRaises(intercompany.TransferError):
            intercompany.create_transfer(self.store, self.store, transfer_date=self.today,
                lines=[{"sku": "KBL", "qty": Decimal(1), "unit_price": Decimal(1)}])
        with self.assertRaises(intercompany.TransferError):   # SKU the buyer does not carry
            intercompany.create_transfer(self.store, self.other, transfer_date=self.today,
                lines=[{"sku": "LMP", "qty": Decimal(1), "unit_price": Decimal(1)}])
        with self.assertRaises(intercompany.TransferError):   # more than the shelf holds
            intercompany.create_transfer(self.store, self.other, transfer_date=self.today,
                lines=[{"sku": "KBL", "qty": Decimal(500), "unit_price": Decimal(1)}])
        self.assertEqual(self.on_hand(self.kabel), 90)
        self.assertEqual(InventoryLevel.objects.get(product=self.other_kabel).on_hand, 10)

    def test_pages_and_the_admin_of_both_rule(self):
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse("merchant-intercompany"), {"store": self.store.pk}), "IC-1")
        self.assertContains(self.client.get(reverse("merchant-intercompany-detail", args=[self.transfer.pk]), {"store": self.store.pk}), "Anda penjual")
        form = {"to_store": self.other.pk, "transfer_date": self.today.isoformat(), "number": "IC-2",
            "sku[]": ["KBL"], "qty[]": ["2"], "unit_price[]": ["80.000"], "idem": "ic-1"}
        self.client.post(reverse("merchant-intercompany-new") + f"?store={self.store.pk}", form)
        self.client.post(reverse("merchant-intercompany-new") + f"?store={self.store.pk}", form)
        from apps.accounting.models import IntercompanyTransfer
        self.assertEqual(IntercompanyTransfer.objects.count(), 2)   # IC-1 and one IC-2, not two
        staff = User.objects.create_user("fin", password="x")
        from apps.shops.models import StoreStaff
        StoreStaff.objects.create(store=self.store, user=staff, role="finance")
        self.client.force_login(staff)
        self.assertEqual(self.client.get(reverse("merchant-intercompany-new"), {"store": self.store.pk}).status_code, 404)

    def test_backlog_worker_completes_a_missing_entry(self):
        from apps.accounting.models import JournalEntry
        JournalEntry.objects.filter(store=self.other, source_type="ic_in").delete()
        self.assertEqual(intercompany.post_unposted_transfers(), 1)
        self.assertEqual(balances(self.other)[INTERCO_PAYABLE], Decimal(800_000))
