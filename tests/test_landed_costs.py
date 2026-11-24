"""Biaya perolehan (landed cost).

Worked example: 100 Kabel at 60.000 and 100 Lampu at 30.000 on the shelf. A receipt brings 50 Kabel (3.000.000)
and 50 Lampu (1.500.000). 120 Kabel are sold, leaving 30. The forwarder bills 450.000 freight (category 1460).
By value Kabel carries 300.000 and Lampu 150.000. Of the 50 Kabel only 30 are still here: 180.000 goes into
Persediaan (cost 60.000 → 66.000) and 120.000 to HPP. All 50 Lampu are here: 150.000 into Persediaan (cost 30.000
→ 31.000 over the 150 held). Entry: Persediaan 330.000 + HPP 120.000 against 1460 450.000."""
from decimal import Decimal

from django.urls import reverse
from test_sales_docs import Base, balance

from apps.accounting import landed_costs
from apps.accounting.controls import _balances, inventory_value, landed_cost_tie
from apps.accounting.models import LandedCost, Supplier
from apps.accounting.payables import create_bill
from apps.accounting.receipts import create_receipt
from apps.orders.stock import record_opening_balances
from apps.tax.sales import create_sales_invoice

D = Decimal


class LandedCostTests(Base):
    def setUp(self):
        super().setUp()
        record_opening_balances(self.store)
        self.sumber = Supplier.objects.create(store=self.store, name="CV Sumber")
        self.cargo = Supplier.objects.create(store=self.store, name="JNE Cargo")
        self.receipt = create_receipt(self.store, supplier=self.sumber, received_on=self.today, lines=[
            {"sku": "KBL", "qty": 50, "unit_cost": D(60_000)}, {"sku": "LMP", "qty": 50, "unit_cost": D(30_000)}])
        create_sales_invoice(self.store, lines=[{"product": self.kabel, "qty": 120, "unit_price": D(100_000)}],
            buyer_name="Budi", customer=self.customer, on_credit=True)
        self.freight = create_bill(self.store, supplier=self.cargo, bill_date=self.today, category="1460",
            lines=[{"description": "Ongkos angkut kontainer", "qty": 1, "unit_price": D(450_000)}])

    def costs(self):
        self.kabel.refresh_from_db()
        self.lampu.refresh_from_db()
        return self.kabel.cost_price, self.lampu.cost_price

    def ties(self):
        """1460 against the bills waiting, and whether Persediaan and the shelf value moved together (the Base
        shelf was never booked, so the stock check starts with a gap; the allocation must not change it)."""
        balances = _balances(self.store)
        stock = inventory_value(self.store, self.today, balances)
        gap = stock.book - stock.other
        if not hasattr(self, "gap"):
            self.gap = gap
        return landed_cost_tie(self.store, self.today, balances).status, "ok" if abs(gap - self.gap) <= 1 else gap

    def test_the_worked_example(self):
        self.ties()                                            # the gap before anything is allocated
        self.assertEqual(balance(self.store, "1460"), D(450_000))
        self.assertEqual(landed_costs.open_bills(self.store), [self.freight])
        self.assertEqual(self.ties()[0], "ok")
        hpp = balance(self.store, "5100")
        cost = landed_costs.allocate(self.store, bill=self.freight, receipts=[self.receipt], date=self.today)
        self.assertEqual((cost.number[:3], cost.amount, cost.to_stock, cost.to_cogs),
            ("LC/", D(450_000), D(330_000), D(120_000)))
        kabel = cost.lines.get(product=self.kabel)
        self.assertEqual((kabel.held, kabel.share, kabel.to_stock, kabel.to_cogs),
            (30, D(300_000), D(180_000), D(120_000)))
        self.assertEqual(self.costs(), (D(66_000), D(31_000)))
        self.assertEqual(balance(self.store, "1460"), D(0))
        self.assertEqual(balance(self.store, "5100") - hpp, D(120_000))
        self.assertEqual(self.ties(), ("ok", "ok"))
        self.assertEqual(landed_costs.open_bills(self.store), [])
        with self.assertRaisesMessage(landed_costs.LandedCostError, "seluruhnya"):
            landed_costs.allocate(self.store, bill=self.freight, receipts=[self.receipt], date=self.today)

        landed_costs.void(cost)
        self.assertEqual(self.costs(), (D(60_000), D(30_000)))
        self.assertEqual(balance(self.store, "1460"), D(450_000))
        self.assertEqual(self.ties(), ("ok", "ok"))

    def test_by_quantity_and_in_parts(self):
        self.ties()
        cost = landed_costs.allocate(self.store, bill=self.freight, receipts=[self.receipt], date=self.today,
            method=LandedCost.BY_QUANTITY, amount=D(200_000))
        self.assertEqual([line.share for line in cost.lines.order_by("product__name")], [D(100_000), D(100_000)])
        self.assertEqual(landed_costs.unallocated(self.freight), D(250_000))
        self.assertEqual(self.ties(), ("ok", "ok"))
        with self.assertRaisesMessage(landed_costs.LandedCostError, "Sisa"):
            landed_costs.allocate(self.store, bill=self.freight, receipts=[self.receipt], date=self.today,
                amount=D(300_000))

    def test_void_is_refused_once_the_goods_left(self):
        cost = landed_costs.allocate(self.store, bill=self.freight, receipts=[self.receipt], date=self.today)
        create_sales_invoice(self.store, lines=[{"product": self.kabel, "qty": 1, "unit_price": D(100_000)}],
            buyer_name="Budi", customer=self.customer, on_credit=True)
        with self.assertRaisesMessage(landed_costs.LandedCostError, "sudah keluar"):
            landed_costs.void(cost)

    def test_only_a_freight_bill_can_be_allocated(self):
        goods = create_bill(self.store, supplier=self.sumber, bill_date=self.today, category="6200",
            lines=[{"description": "Gaji", "qty": 1, "unit_price": D(1_000)}])
        with self.assertRaisesMessage(landed_costs.LandedCostError, "1460"):
            landed_costs.allocate(self.store, bill=goods, receipts=[self.receipt], date=self.today)

    def test_the_pages(self):
        self.client.force_login(self.owner)
        bill_page = self.client.get(reverse("merchant-bill-detail", args=[self.freight.pk]))
        self.assertContains(bill_page, "Alokasikan ke penerimaan")
        page = self.client.get(reverse("merchant-landed-costs") + f"?bill={self.freight.pk}")
        self.assertContains(page, "JNE Cargo")
        data = {"idem": "lc1", "bill": str(self.freight.pk), "receipt": [str(self.receipt.pk)], "method": "value",
            "date": self.today.isoformat()}
        reply = self.client.post(reverse("merchant-landed-costs"), data)
        cost = LandedCost.objects.get()
        self.assertRedirects(reply, reverse("merchant-landed-cost", args=[cost.pk]), fetch_redirect_response=False)
        self.client.post(reverse("merchant-landed-costs"), data)       # pressed twice
        self.assertEqual(LandedCost.objects.count(), 1)
        detail = self.client.get(reverse("merchant-landed-cost", args=[cost.pk]))
        self.assertContains(detail, "Kabel")
        self.assertEqual(len(detail.context["journal"]), 1)
        self.client.post(reverse("merchant-landed-cost", args=[cost.pk]), {"action": "void"})
        cost.refresh_from_db()
        self.assertEqual(cost.status, LandedCost.VOID)
