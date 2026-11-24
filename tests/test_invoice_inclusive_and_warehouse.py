"""The invoice form: prices typed with PPN in them, and selling from one chosen warehouse."""
from decimal import Decimal

from django.urls import reverse
from test_sales_docs import Base

from apps.catalog.models import InventoryLevel, Warehouse
from apps.tax.models import Invoice
from apps.tax.sales import create_sales_invoice, from_inclusive


class InclusivePriceTests(Base):
    def test_round_price_splits_exactly(self):
        lines, discount, rounding = from_inclusive(self.store, [{"product": self.kabel, "qty": 2,
            "unit_price": Decimal(111_000)}])
        self.assertEqual(lines[0]["unit_price"], 100_000)
        self.assertEqual((discount, rounding), (0, 0))

    def test_awkward_price_still_totals_what_was_typed(self):
        typed = [{"product": self.kabel, "qty": 3, "unit_price": Decimal(10_000), "discount": Decimal(1_500)}]
        lines, discount, rounding = from_inclusive(self.store, typed, Decimal(2_000))
        invoice = create_sales_invoice(self.store, lines=lines, buyer_name="Budi", discount=discount,
            rounding=rounding, on_credit=True)
        self.assertEqual(invoice.total, Decimal(30_000 - 1_500 - 2_000))
        self.assertLess(abs(rounding), 1)

    def test_non_pkp_seller_is_unchanged(self):
        profile = self.store.tax_profile
        profile.is_pkp = False
        profile.save()
        typed = [{"product": self.kabel, "qty": 1, "unit_price": Decimal(111_000)}]
        self.assertEqual(from_inclusive(self.store, typed)[0], typed)

    def test_form_switch(self):
        self.client.force_login(self.owner)
        page = self.client.get(reverse("merchant-invoice-new"))
        self.assertContains(page, 'name="prices_include_tax"')
        self.client.post(reverse("merchant-invoice-new"), {"idem": "incl-1", "buyer_name": "Budi",
            "issued_on": self.today.isoformat(), "payment_status": "credit", "channel": "manual",
            "prices_include_tax": "1", "product[]": [str(self.kabel.pk)], "description[]": [""],
            "qty[]": ["2"], "unit_price[]": ["111000"], "line_discount[]": [""], "unit[]": [""],
            "note[]": [""], "code[]": [""]})
        invoice = Invoice.objects.get(store=self.store)
        self.assertEqual((invoice.total, invoice.tax), (Decimal(222_000), Decimal(22_000)))


class SellFromWarehouseTests(Base):
    def test_chosen_warehouse_gives_the_stock(self):
        branch = Warehouse.objects.create(store=self.store, code="G2", name="Cabang")
        InventoryLevel.objects.create(warehouse=branch, product=self.kabel, on_hand=10)
        self.client.force_login(self.owner)
        page = self.client.get(reverse("merchant-invoice-new"))
        self.assertContains(page, "Jual dari gudang")
        self.client.post(reverse("merchant-invoice-new"), {"idem": "wh-1", "buyer_name": "Budi",
            "issued_on": self.today.isoformat(), "payment_status": "credit", "channel": "manual",
            "warehouse": str(branch.pk), "product[]": [str(self.kabel.pk)], "description[]": [""],
            "qty[]": ["3"], "unit_price[]": ["100000"], "line_discount[]": [""], "unit[]": [""],
            "note[]": [""], "code[]": [""]})
        self.assertTrue(Invoice.objects.filter(store=self.store).exists())
        self.assertEqual(InventoryLevel.objects.get(warehouse=branch, product=self.kabel).on_hand, 7)
        self.assertEqual(InventoryLevel.objects.get(warehouse__code="G1", product=self.kabel).on_hand, 100)

    def test_single_warehouse_shows_no_choice(self):
        self.client.force_login(self.owner)
        self.assertNotContains(self.client.get(reverse("merchant-invoice-new")), "Jual dari gudang")
