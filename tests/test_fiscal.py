"""Annual badan worksheet (Pasal 31E) and the tax calendar."""
from datetime import date
from decimal import Decimal

from django.urls import reverse
from test_sales_docs import Base

from apps.accounting.ledger import CASH, SALES, post_entry
from apps.tax import fiscal, tax_calendar
from apps.tax.models import FiscalAdjustment, TaxFiling, TaxProfile


class TaxDueTests(Base):
    def test_pasal_31e_reduction_follows_the_turnover(self):
        # turnover within 4,8 miliar: every rupiah of PKP at 11%
        self.assertEqual(fiscal.tax_due(Decimal(1_000_000_000), Decimal(4_000_000_000))[0], Decimal(110_000_000))
        # no facility above 50 miliar: 22% flat
        self.assertEqual(fiscal.tax_due(Decimal(1_000_000_000), Decimal(60_000_000_000))[0], Decimal(220_000_000))
        # turnover 9,6 miliar: half of PKP (4,8/9,6) at 11%, the rest at 22%
        tax, detail = fiscal.tax_due(Decimal(1_000_000_000), Decimal(9_600_000_000))
        self.assertEqual(tax, Decimal(500_000_000 * Decimal("0.11") + 500_000_000 * Decimal("0.22")))
        self.assertEqual([part[1] for part in detail], [Decimal(500_000_000), Decimal(500_000_000)])
        self.assertEqual(fiscal.tax_due(Decimal(0), Decimal(1))[0], 0)
        self.assertEqual(fiscal.tax_due(Decimal(-5), Decimal(1))[0], 0)


class WorksheetTests(Base):
    def setUp(self):
        super().setUp()
        TaxProfile.objects.filter(store=self.store).update(taxpayer_type="company", pph_regime="general", is_pkp=True)
        post_entry(self.store, date=date(2026, 6, 1), memo="s", amounts={CASH: 1_000_000_000, SALES: -1_000_000_000}, source=("t", "s"))
        post_entry(self.store, date=date(2026, 7, 1), memo="e", amounts={"6900": 400_000_000, CASH: -400_000_000}, source=("t", "e"))

    def add(self, kind, amount, text="x"):
        return FiscalAdjustment.objects.create(store=self.store, year=2026, kind=kind, description=text, amount=Decimal(amount))

    def test_commercial_profit_through_corrections_to_pph_29(self):
        w = fiscal.worksheet(self.store, 2026)
        self.assertEqual((w["commercial"], w["turnover"]), (600_000_000, 1_000_000_000))
        self.add("positive", 50_000_000, "Biaya representasi")
        self.add("negative", 20_000_000, "Penghasilan final")
        self.add("credit", 30_000_000, "PPh 25")
        w = fiscal.worksheet(self.store, 2026)
        self.assertEqual((w["fiscal"], w["pkp"]), (630_000_000, 630_000_000))
        self.assertEqual(w["tax"], Decimal(69_300_000))                                  # 11% of 630.000.000 (turnover under 4,8 miliar)
        self.assertEqual(w["pph29"], Decimal(39_300_000))
        self.assertEqual(w["pph25_monthly"], Decimal(5_775_000))
        self.assertEqual(w["effective"], Decimal("11.6"))

    def test_pkp_rounds_down_to_thousands_and_a_loss_is_flagged(self):
        self.add("negative", 1_234_567, "x")
        w = fiscal.worksheet(self.store, 2026)
        self.assertEqual(w["fiscal"], Decimal(598_765_433))
        self.assertEqual(w["pkp"], Decimal(598_765_000))                                   # rounded down to thousands
        self.add("negative", 900_000_000, "kompensasi")
        loss = fiscal.worksheet(self.store, 2026)
        self.assertEqual((loss["pkp"], loss["tax"]), (0, 0))
        self.assertGreater(loss["loss"], 0)

    def test_not_applicable_for_individuals_and_umkm_final(self):
        TaxProfile.objects.filter(store=self.store).update(pph_regime="umkm_final")
        self.assertIn("PPh Final UMKM", fiscal.worksheet(self.store, 2026)["not_applicable"])
        TaxProfile.objects.filter(store=self.store).update(taxpayer_type="individual", pph_regime="general")
        self.assertIn("orang pribadi", fiscal.worksheet(self.store, 2026)["not_applicable"].lower())

    def test_page_adds_and_deletes_rows(self):
        self.client.force_login(self.owner)
        self.client.post(reverse("merchant-spt-tahunan"), {"year": 2026, "kind": "credit", "description": "PPh 25", "amount": "5.000.000"})
        row = FiscalAdjustment.objects.get()
        self.assertEqual((row.kind, row.amount), ("credit", Decimal(5_000_000)))
        self.assertContains(self.client.get(reverse("merchant-spt-tahunan"), {"year": 2026}), "PPh 25")
        self.client.post(reverse("merchant-spt-tahunan"), {"year": 2026, "kind": "credit", "description": "", "amount": "1"})
        self.assertEqual(FiscalAdjustment.objects.count(), 1)                              # a blank description is refused
        self.client.post(reverse("merchant-spt-tahunan"), {"year": 2026, "action": "delete", "id": row.pk})
        self.assertEqual(FiscalAdjustment.objects.count(), 0)


class CalendarTests(Base):
    def setUp(self):
        super().setUp()
        TaxProfile.objects.filter(store=self.store).update(taxpayer_type="company", pph_regime="general", is_pkp=True)

    def test_a_pkp_gets_the_ppn_duty_for_last_month_due_at_month_end(self):
        rows = tax_calendar.obligations(self.store, 2026, 11, date(2026, 11, 20))
        ppn = next(r for r in rows if r["code"] == "ppn:2026-10")
        self.assertEqual(ppn["due"], date(2026, 11, 30))
        self.assertEqual(ppn["status"], "upcoming")
        self.assertEqual(tax_calendar.obligations(self.store, 2026, 11, date(2026, 12, 3))[0]["status"], "overdue")
        self.assertEqual(next(r for r in tax_calendar.obligations(self.store, 2026, 11, date(2026, 11, 26)) if r["code"] == "ppn:2026-10")["status"], "soon")

    def test_marking_done_and_the_annual_returns(self):
        TaxFiling.objects.create(store=self.store, code="ppn:2026-10", done_on=date(2026, 11, 25), reference="NTPN123")
        ppn = next(r for r in tax_calendar.obligations(self.store, 2026, 11, date(2026, 12, 3)) if r["code"] == "ppn:2026-10")
        self.assertEqual(ppn["status"], "done")
        self.assertTrue(any(r["code"] == "spt_tahunan:badan:2025" for r in tax_calendar.obligations(self.store, 2026, 4, date(2026, 4, 1))))
        self.assertTrue(any(r["code"] == "spt_tahunan:op:2025" for r in tax_calendar.obligations(self.store, 2026, 3, date(2026, 3, 1))))

    def test_withholding_deposits_appear_and_the_page_marks_things_done(self):
        from apps.accounting.models import Supplier
        from apps.accounting.payables import create_bill, pay_bill
        supplier = Supplier.objects.create(store=self.store, name="K", npwp="0123456789012345")
        bill = create_bill(self.store, supplier=supplier, bill_date=date(2026, 10, 5), lines=[{"description": "Jasa", "qty": 1, "unit_price": Decimal(1_000_000)}],
            category="6900", actor=self.owner)
        pay_bill(bill, amount=Decimal(1_000_000), paid_on=date(2026, 10, 6), withholding=("pph23_jasa", None), actor=self.owner)
        rows = tax_calendar.obligations(self.store, 2026, 11, date(2026, 11, 12))
        deposit = next(r for r in rows if r["code"] == "pph_deposit:2026-10")
        self.assertEqual((deposit["amount"], deposit["status"]), (Decimal(20_000), "overdue"))        # 10th passed
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse("merchant-tax-calendar"), {"month": "2026-11"}), "Setor PPh 21/23/4(2)")
        self.client.post(reverse("merchant-tax-calendar"), {"code": "pph_report:2026-10", "done_on": "2026-11-18", "reference": "BPE1"})
        self.assertTrue(TaxFiling.objects.filter(code="pph_report:2026-10", reference="BPE1").exists())
        self.client.post(reverse("merchant-tax-calendar"), {"action": "undo", "code": "pph_report:2026-10"})
        self.assertFalse(TaxFiling.objects.filter(code="pph_report:2026-10").exists())
