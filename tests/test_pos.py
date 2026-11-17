"""Kasir (POS): a receipt is an ordinary invoice sold from the outlet's shelf, each tender is a
payment, and the drawer's expected cash is the ledger balance of the register's own account."""
from decimal import Decimal

from django.contrib.auth.models import User
from test_sales_docs import Base

from apps.accounting.ledger import (
    CASH_OVER,
    CASH_SHORT,
    COGS,
    EDC_CLEARING,
    ROUNDING,
    SALES,
    VAT,
    account_balances,
    post_everything,
)
from apps.accounting.models import CostCenter, JournalEntry
from apps.catalog.models import InventoryLevel, Warehouse
from apps.orders.stock import transfer_stock
from apps.pos import services as pos
from apps.pos.models import PosSale, PosShift
from apps.shops.models import StoreStaff
from apps.tax.models import Invoice


def balance(store, code):
    return {a.code: a.balance for a in account_balances(store)}.get(code, Decimal(0))


class PosBase(Base):
    """Base: PKP store at 11%, Kabel (price Rp100.000, cost Rp60.000) and Lampu (Rp50.000 / Rp30.000),
    100 each in Gudang. The outlet gets 20 Kabel and 10 Lampu, one register and a cashier."""

    def setUp(self):
        super().setUp()
        self.gudang = Warehouse.objects.get(store=self.store)
        self.centre = CostCenter.objects.create(store=self.store, code="KMG", name="Outlet Kemang")
        self.outlet = Warehouse.objects.create(store=self.store, code="KMG", name="Outlet Kemang",
            is_outlet=True, priority=200, cost_center=self.centre)
        transfer_stock(store=self.store, product=self.kabel, quantity=20, source=self.gudang, target=self.outlet)
        transfer_stock(store=self.store, product=self.lampu, quantity=10, source=self.gudang, target=self.outlet)
        self.register = pos.create_register(self.store, outlet=self.outlet, code="k1", actor=self.owner)
        self.kasir = User.objects.create_user("kasir", password="x")
        StoreStaff.objects.create(store=self.store, user=self.kasir, role="cashier", outlet=self.outlet)
        self.drawer = self.register.cash_account.account.code

    def open(self, float_=500_000):
        return pos.open_shift(self.register, opening_float=float_, actor=self.kasir, fund_from=self.main)

    def sell(self, lines=None, tenders=None, **kwargs):
        lines = lines or [{"product": self.kabel, "qty": 1}]
        tenders = tenders if tenders is not None else [{"method": "cash", "amount": 100_000}]
        return pos.sell(self.register, cashier=kwargs.pop("cashier", self.kasir), lines=lines, tenders=tenders, **kwargs)

    def shelf(self, warehouse, product):
        return InventoryLevel.objects.get(warehouse=warehouse, product=product).on_hand


class RegisterTests(PosBase):
    def test_a_register_gets_its_own_cash_account(self):
        self.assertEqual((self.register.code, self.register.cash_account.kind), ("K1", "cash"))
        self.assertEqual(self.register.cash_account.name, "Kas kasir Outlet Kemang K1")
        self.assertNotEqual(self.drawer, "1100")
        with self.assertRaises(pos.PosError):                                  # same code twice
            pos.create_register(self.store, outlet=self.outlet, code="K1")
        with self.assertRaises(pos.PosError):                                  # a plain warehouse is not an outlet
            pos.create_register(self.store, outlet=self.gudang, code="K9")

    def test_a_cashier_sees_only_the_registers_of_their_outlet(self):
        other = Warehouse.objects.create(store=self.store, code="BDG", name="Outlet Bandung", is_outlet=True)
        pos.create_register(self.store, outlet=other, code="B1")
        self.assertEqual([r.code for r in pos.registers_for(self.store, self.kasir)], ["K1"])
        self.assertEqual(sorted(r.code for r in pos.registers_for(self.store, self.owner)), ["B1", "K1"])


class ShiftTests(PosBase):
    def test_the_float_comes_from_a_named_account(self):
        with self.assertRaises(pos.PosError):                                  # nobody can explain Rp500.000
            pos.open_shift(self.register, opening_float=500_000, actor=self.kasir)
        main_before = balance(self.store, self.main.account.code)
        shift = self.open()
        self.assertEqual(pos.drawer_balance(self.register), Decimal(500_000))
        self.assertEqual(balance(self.store, self.main.account.code), main_before - 500_000)
        self.assertTrue(shift.is_open)
        with self.assertRaises(pos.PosError):                                  # one open shift per register
            self.open()

    def test_selling_needs_an_open_shift(self):
        with self.assertRaises(pos.PosError) as caught:
            self.sell()
        self.assertIn("Buka shift", str(caught.exception))

    def test_closing_short_books_the_shortage_and_deposits_the_rest(self):
        shift = self.open()
        self.sell()                                                            # Rp100.000 cash
        main_before = balance(self.store, self.main.account.code)
        shift = pos.close_shift(shift, counted_cash=595_000, actor=self.kasir, deposit_to=self.main,
            keep_float=500_000)
        self.assertEqual((shift.expected_cash, shift.variance, shift.deposited),
            (Decimal(600_000), Decimal(-5_000), Decimal(95_000)))
        self.assertEqual(balance(self.store, CASH_SHORT), Decimal(5_000))
        self.assertEqual(pos.drawer_balance(self.register), Decimal(500_000))  # tomorrow's float
        self.assertEqual(balance(self.store, self.main.account.code), main_before + 95_000)
        entry = JournalEntry.objects.get(source_type="pos_shift", source_id=str(shift.pk))
        self.assertEqual(entry.cost_center, self.centre)
        with self.assertRaises(pos.PosError):
            pos.close_shift(shift, counted_cash=1, actor=self.kasir)           # already closed
        self.open()                                                            # the float is there: no funding needed

    def test_closing_over_books_the_surplus(self):
        shift = self.open()
        shift = pos.close_shift(shift, counted_cash=502_000, actor=self.kasir)
        self.assertEqual(shift.variance, Decimal(2_000))
        self.assertEqual(balance(self.store, CASH_OVER), Decimal(2_000))
        self.assertEqual(pos.drawer_balance(self.register), Decimal(502_000))  # nothing deposited
        entries = JournalEntry.objects.count()
        post_everything()                                                      # nothing left for the backlog
        self.assertEqual(JournalEntry.objects.count(), entries)


class SaleTests(PosBase):
    def setUp(self):
        super().setUp()
        self.shift = self.open()

    def test_a_cash_sale_is_an_invoice_from_the_outlets_shelf(self):
        sales_before = balance(self.store, SALES)
        sale = self.sell(lines=[{"product": self.kabel, "qty": 2}], tenders=[{"method": "cash", "amount": 250_000}])
        invoice = sale.invoice
        self.assertEqual((sale.total, sale.tendered, sale.change), (Decimal(200_000), Decimal(250_000), Decimal(50_000)))
        self.assertEqual(sale.number, f"KMG-K1-{self.today:%Y%m%d}-0001")
        self.assertTrue(invoice.number.startswith("POS/"))
        self.assertEqual((invoice.status, invoice.order.channel, invoice.order.status), (Invoice.STATUS_PAID, "pos", "completed"))   # handed over: nothing to pack
        # Shelf price includes PPN: Rp200.000 = Rp180.180,18 + PPN Rp19.819,82
        self.assertEqual((invoice.total, invoice.tax), (Decimal(200_000), Decimal("19819.82")))
        self.assertEqual(balance(self.store, SALES) - sales_before, Decimal("180180.18"))
        self.assertEqual(self.shelf(self.outlet, self.kabel), 18)
        self.assertEqual(self.shelf(self.gudang, self.kabel), 80)             # the warehouse is untouched
        self.assertEqual(pos.drawer_balance(self.register), Decimal(700_000))  # float + 200.000, change went back
        self.assertEqual(invoice.cost_center, self.centre)
        entry = JournalEntry.objects.get(source_type="invoice", source_id=str(invoice.pk))
        self.assertEqual(entry.cost_center, self.centre)
        tender = sale.tenders.get()
        self.assertEqual((tender.amount, tender.tendered, tender.payment.bank_account), (Decimal(200_000),
            Decimal(250_000), self.register.cash_account))

    def test_split_tender_card_waits_in_the_clearing_account(self):
        sale = self.sell(lines=[{"product": self.kabel, "qty": 3}], tenders=[
            {"method": "edc", "amount": 200_000, "reference": "APPR 123"}, {"method": "qris", "amount": 50_000},
            {"method": "cash", "amount": 100_000}])
        self.assertEqual((sale.total, sale.change), (Decimal(300_000), Decimal(50_000)))
        self.assertEqual(balance(self.store, EDC_CLEARING), Decimal(250_000))
        self.assertEqual(pos.drawer_balance(self.register), Decimal(550_000))  # only Rp50.000 of the cash stayed
        self.assertEqual(sale.invoice.amount_due, 0)
        summary = pos.shift_summary(self.shift)
        self.assertEqual((summary["count"], summary["total"], summary["cash_sales"], summary["expected_cash"]),
            (1, Decimal(300_000), Decimal(50_000), Decimal(550_000)))

    def test_too_little_money_or_too_much_card_is_refused(self):
        with self.assertRaises(pos.PosError) as caught:
            self.sell(tenders=[{"method": "cash", "amount": 90_000}])
        self.assertIn("kurang Rp10.000", str(caught.exception))
        with self.assertRaises(pos.PosError):                                  # no change from a card
            self.sell(tenders=[{"method": "edc", "amount": 150_000}])
        with self.assertRaises(pos.PosError):                                  # transfer needs its account
            self.sell(tenders=[{"method": "transfer", "amount": 100_000}])
        self.assertEqual(PosSale.objects.count(), 0)
        self.assertEqual(self.shelf(self.outlet, self.kabel), 20)

    def test_the_outlet_cannot_sell_what_only_the_warehouse_holds(self):
        with self.assertRaises(pos.PosError) as caught:
            self.sell(lines=[{"product": self.kabel, "qty": 21}], tenders=[{"method": "cash", "amount": 2_100_000}])
        self.assertIn("Outlet Kemang", str(caught.exception))
        self.assertEqual(PosSale.objects.count(), 0)

    def test_a_retried_request_returns_the_first_receipt(self):
        first = self.sell(idem_key="abc")
        again = self.sell(idem_key="abc")
        self.assertEqual(first.pk, again.pk)
        self.assertEqual((PosSale.objects.count(), self.shelf(self.outlet, self.kabel)), (1, 19))
        self.assertEqual(self.sell(idem_key="def").number[-4:], "0002")

    def test_a_cashier_discounts_within_the_limit_and_never_changes_a_price(self):
        sale = self.sell(lines=[{"product": self.kabel, "qty": 1, "discount": 10_000}],
            tenders=[{"method": "cash", "amount": 90_000}])                    # 10% is the default limit
        self.assertEqual(sale.total, Decimal(90_000))
        with self.assertRaises(pos.PosError):
            self.sell(lines=[{"product": self.kabel, "qty": 1, "discount": 20_000}],
                tenders=[{"method": "cash", "amount": 80_000}])
        with self.assertRaises(pos.PosError):
            self.sell(lines=[{"product": self.kabel, "qty": 1, "unit_price": 70_000}],
                tenders=[{"method": "cash", "amount": 70_000}])
        manager = self.sell(cashier=self.owner, lines=[{"product": self.kabel, "qty": 1, "unit_price": 70_000}],
            tenders=[{"method": "cash", "amount": 70_000}])
        self.assertEqual(manager.total, Decimal(70_000))

    def test_rounding_down_to_rp500_goes_to_pembulatan(self):
        config = pos.settings_for(self.outlet)
        config.rounding_step = 500
        config.save()
        self.lampu.price = 50_250
        self.lampu.save(update_fields=["price"])
        sale = self.sell(lines=[{"product": self.lampu, "qty": 1}], tenders=[{"method": "cash", "amount": 50_000}])
        self.assertEqual((sale.total, sale.invoice.total, sale.invoice.rounding), (Decimal(50_000), Decimal(50_000),
            Decimal(-250)))
        self.assertEqual(balance(self.store, ROUNDING), Decimal(-250))       # revenue account, debit side
        self.assertEqual(sale.invoice.tax, Decimal("4979.73"))                # PPN on the full shelf price

    def test_the_books_still_tie_after_a_day_at_the_register(self):
        from apps.accounting import controls
        vat_before, cogs_before = balance(self.store, VAT), balance(self.store, COGS)
        sale = self.sell(lines=[{"product": self.kabel, "qty": 2}, {"product": self.lampu, "qty": 1}],
            tenders=[{"method": "qris", "amount": 250_000}])
        self.assertEqual(balance(self.store, COGS) - cogs_before, Decimal(150_000))    # 2 × 60.000 + 30.000
        # Backing PPN out of three shelf prices leaves one cent; the buyer still pays Rp250.000.
        self.assertEqual(balance(self.store, VAT) - vat_before, Decimal("24774.78"))
        self.assertEqual((sale.invoice.total, sale.invoice.rounding), (Decimal(250_000), Decimal("-0.01")))
        pos.close_shift(self.shift, counted_cash=500_000, actor=self.kasir)
        results = {check.key: check.status for check in controls.run_checks(self.store, self.today)}
        for key in ("trial_balance", "unposted", "invoice_journal", "invoice_status", "receivable"):
            self.assertEqual(results[key], "ok", key)
        self.assertEqual(PosShift.objects.get().variance, 0)
        self.assertEqual((results["pos_receipts"], results["pos_shifts"]), ("ok", "ok"))

    def test_the_checks_flag_a_shift_left_open_and_old_card_money(self):
        from datetime import timedelta

        from apps.accounting import controls
        self.sell(tenders=[{"method": "edc", "amount": 100_000}])
        later = self.today + timedelta(days=5)                                 # nobody closed, the bank never settled
        results = {check.key: check for check in controls.run_checks(self.store, later)}
        self.assertEqual(results["pos_shifts"].status, "warn")
        self.assertIn("Outlet Kemang K1", results["pos_shifts"].items[0])
        self.assertEqual((results["pos_receipts"].status, results["pos_receipts"].book), ("info", Decimal(100_000)))


class PageTests(PosBase):
    """The screens: register list, open shift, ring up through the JSON endpoint, receipt, close."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.kasir)

    def ring(self, **body):
        import json
        body = {"idem": "k-1", "lines": [{"id": self.kabel.id, "qty": 2}],
            "tenders": [{"method": "cash", "amount": 200_000}], **body}
        return self.client.post(f"/books/pos/{self.register.pk}/sell/", data=json.dumps(body),
            content_type="application/json")

    def test_a_cashier_opens_a_shift_sells_prints_and_closes(self):
        page = self.client.get("/kasir/")
        self.assertContains(page, "Buka shift")
        self.assertNotContains(page, "Register baru")                         # settings are the manager's
        opened = self.client.post(f"/books/pos/{self.register.pk}/open/", {"opening_float": "500.000",
            "fund_from": self.main.id})
        self.assertRedirects(opened, f"/books/pos/{self.register.pk}/sell/", fetch_redirect_response=False)
        screen = self.client.get(f"/books/pos/{self.register.pk}/sell/")
        self.assertContains(screen, "pos-setup")
        self.assertEqual([p["stock"] for p in screen.context["setup"]["products"] if p["sku"] == "KBL"], [20])
        sold = self.ring()
        self.assertEqual(sold.status_code, 200, sold.content)
        data = sold.json()
        self.assertEqual((data["total"], data["change"]), (200_000, 0))
        self.assertEqual([p["stock"] for p in data["products"] if p["sku"] == "KBL"], [18])
        self.assertEqual(self.ring().json()["number"], data["number"])        # a retry is the same receipt
        receipt = self.client.get(data["receipt"])
        self.assertContains(receipt, data["number"])
        self.assertContains(receipt, "Rp200.000")
        sale = PosSale.objects.get()
        self.client.logout()
        self.assertContains(self.client.get(f"/struk/{sale.token}/"), sale.number)     # the buyer's link needs no login
        self.client.force_login(self.kasir)
        shift = PosShift.objects.get()
        self.assertContains(self.client.get(f"/books/pos/shift/{shift.pk}/"), "Tutup shift")
        self.client.post(f"/books/pos/shift/{shift.pk}/", {"counted_cash": "700.000", "keep_float": "500.000",
            "deposit_to": self.main.id})
        shift.refresh_from_db()
        self.assertEqual((shift.status, shift.variance, shift.deposited), ("closed", Decimal(0), Decimal(200_000)))

    def test_a_refused_sale_answers_with_the_reason(self):
        self.open()
        short = self.ring(tenders=[{"method": "cash", "amount": 150_000}])
        self.assertEqual(short.status_code, 400)
        self.assertIn("kurang Rp50.000", short.json()["error"])
        self.assertEqual(self.ring(lines=[{"id": 999999, "qty": 1}]).status_code, 400)
        self.assertEqual(PosSale.objects.count(), 0)

    def test_a_cashier_is_kept_to_their_outlet_and_out_of_the_books(self):
        other = Warehouse.objects.create(store=self.store, code="BDG", name="Outlet Bandung", is_outlet=True)
        elsewhere = pos.create_register(self.store, outlet=other, code="B1")
        self.assertEqual(self.client.post(f"/books/pos/{elsewhere.pk}/open/", {"opening_float": "0"}).status_code, 404)
        self.assertEqual(self.client.get(f"/books/pos/{elsewhere.pk}/sell/").status_code, 404)
        self.assertEqual(self.client.post("/books/pos/register/new/", {"outlet": other.id, "code": "B2"}).status_code, 404)
        self.assertEqual(self.client.get("/books/pos/").status_code, 404)     # the admin panel is not theirs
        self.assertEqual(self.client.get("/books/accounting/").status_code, 404)
        self.assertRedirects(self.client.get("/books/"), "/kasir/", fetch_redirect_response=False)
        page = self.client.get("/kasir/")
        self.assertContains(page, "Kasir (POS)")
        self.assertNotContains(page, "Bagan akun")                            # no back-office sidebar at all
        self.assertNotContains(page, "Semua produk")

    def test_a_manager_sets_how_the_outlet_sells(self):
        self.assertEqual(self.client.post(f"/books/pos/outlet/{self.outlet.id}/settings/", {}).status_code, 404)  # cashier
        self.client.force_login(self.owner)
        self.assertContains(self.client.get("/books/pos/"), "Pengaturan Outlet Kemang")
        self.client.post(f"/books/pos/outlet/{self.outlet.id}/settings/", {"rounding_step": "500",
            "max_discount_pct": "5", "receipt_footer": "Barang yang sudah dibeli tidak dapat ditukar."})
        config = pos.settings_for(self.outlet)
        self.assertEqual((config.prices_include_tax, config.rounding_step, config.max_discount_pct),
            (False, 500, Decimal(5)))
        # Prices now exclude PPN: Rp100.000 + 11% = Rp111.000
        self.open()
        sale = self.sell(tenders=[{"method": "cash", "amount": 111_000}])
        self.assertEqual((sale.total, sale.invoice.tax), (Decimal(111_000), Decimal(11_000)))
        self.assertContains(self.client.get(f"/books/pos/receipt/{sale.pk}/"), "tidak dapat ditukar")
        with self.assertRaises(pos.PosError):
            pos.update_settings(self.outlet, prices_include_tax=True, rounding_step=250, max_discount_pct=5)

    def test_a_manager_creates_a_register(self):
        self.client.force_login(self.owner)
        self.assertContains(self.client.get("/books/pos/"), "Register baru")
        self.client.post("/books/pos/register/new/", {"outlet": self.outlet.id, "code": "k2", "name": "Kasir belakang"})
        self.assertTrue(self.store.pos_registers.filter(code="K2", name="Kasir belakang").exists())


class KioskTests(PosBase):
    """The outlet's own app: /kasir/ with its own sign-in, never the back office."""

    def test_the_kiosk_has_its_own_login_separate_from_the_back_office(self):
        page = self.client.get("/kasir/")
        self.assertRedirects(page, "/kasir/login/?next=/kasir/", fetch_redirect_response=False)
        login = self.client.get("/kasir/login/")
        self.assertContains(login, "Masuk kasir")
        self.assertNotContains(login, "pd-nav")                               # no back-office chrome at all
        posted = self.client.post("/kasir/login/", {"username": "kasir", "password": "x"})
        self.assertRedirects(posted, "/kasir/", fetch_redirect_response=False)

    def test_a_signed_in_cashier_on_the_kiosk_login_goes_straight_in_ignoring_next(self):
        self.client.force_login(self.kasir)
        page = self.client.get("/kasir/login/?next=/books/accounting/")
        self.assertRedirects(page, "/kasir/", fetch_redirect_response=False)

    def test_the_back_office_login_also_sends_a_cashier_to_the_kiosk(self):
        page = self.client.post("/login/", {"username": "kasir", "password": "x"})
        self.assertRedirects(page, "/books/", fetch_redirect_response=False)
        self.assertRedirects(self.client.get("/books/"), "/kasir/", fetch_redirect_response=False)

    def test_the_kiosk_home_lists_only_this_cashiers_outlet_and_has_no_sidebar(self):
        self.client.force_login(self.kasir)
        page = self.client.get("/kasir/")
        self.assertContains(page, self.register.name)
        self.assertContains(page, "Keluar")
        self.assertNotContains(page, "pd-nav")
        self.assertNotContains(page, "Register baru")                        # no admin actions in the kiosk

    def test_a_cashier_cannot_open_the_admin_panel_but_can_still_use_shift_and_sell(self):
        self.client.force_login(self.kasir)
        self.assertEqual(self.client.get("/books/pos/").status_code, 404)
        self.open()
        shift = self.register.shifts.get()
        page = self.client.get(f"/books/pos/shift/{shift.pk}/")
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "pd-nav")                                # chrome-free wherever it is opened
        self.assertContains(page, "← Kasir")

    def test_opening_a_shift_from_the_kiosk_lands_on_the_selling_screen_and_back_returns_to_the_kiosk(self):
        self.client.force_login(self.kasir)
        opened = self.client.post(f"/books/pos/{self.register.pk}/open/", {"opening_float": "500.000",
            "fund_from": self.main.id})
        self.assertRedirects(opened, f"/books/pos/{self.register.pk}/sell/", fetch_redirect_response=False)
        sell = self.client.get(f"/books/pos/{self.register.pk}/sell/")
        self.assertContains(sell, "/kasir/")                                  # the back arrow and Keluar both lead home

    def test_keluar_signs_the_cashier_out_back_to_the_kiosk_login(self):
        self.client.force_login(self.kasir)
        self.assertEqual(self.client.get("/kasir/keluar/").status_code, 405)  # a link or prefetch must not sign anyone out
        out = self.client.post("/kasir/keluar/")
        self.assertRedirects(out, "/kasir/login/", fetch_redirect_response=False)
        self.assertRedirects(self.client.get("/kasir/"), "/kasir/login/?next=/kasir/", fetch_redirect_response=False)

    def test_a_manager_still_uses_the_admin_panel_and_can_reach_the_kiosk_too(self):
        self.client.force_login(self.owner)
        self.assertContains(self.client.get("/books/pos/"), "Register baru")
        self.assertEqual(self.client.get("/kasir/").status_code, 200)
