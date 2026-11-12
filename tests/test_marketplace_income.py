"""Income reports give each marketplace order its real fees and book the payout."""
import io
from datetime import date
from decimal import Decimal

import openpyxl
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from apps.accounting.ledger import CASH, FEES, MARKETPLACE, account_balances, post_pending_entries
from apps.accounting.models import Payout
from apps.catalog.models import InventoryLevel, Product, Warehouse
from apps.channels.adapters import MarketplaceOrder, OrderLine
from apps.channels.income import import_income
from apps.channels.models import ChannelOrderIncome, SalesChannel
from apps.channels.parsers.income import parse_income
from apps.channels.services import ChannelImportError, import_order
from apps.shops.models import Store

HEADER = ["No. Pesanan", "Tanggal Dana Dilepaskan", "Total Penghasilan", "Biaya Komisi",
    "Biaya Layanan", "Biaya Administrasi", "Voucher Ditanggung Penjual", "Biaya Afiliasi",
    "Harga Produk"]


def _xlsx(rows, header=HEADER):
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(["Laporan Penghasilan"])
    sheet.append(header)
    for row in rows:
        sheet.append(row)
    stream = io.BytesIO()
    book.save(stream)
    stream.seek(0)
    return stream


def _balance(store, code):
    return {a.code: a.balance for a in account_balances(store)}.get(code, Decimal(0))


class ParserTests(TestCase):
    def test_reads_fees_by_header_keywords(self):
        rows = parse_income(_xlsx([["S1", "2026-11-10", 88000, -6000, 2000, 1000, 3000, 0, 100000],
            ["Total", None, 88000, None, None, None, None, None, None]]))

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row.order_id, row.net, row.released_on), ("S1", Decimal(88000),
            date(2026, 11, 10)))
        self.assertEqual(row.fees, {"commission": Decimal(6000), "service_fee": Decimal(2000),
            "admin_fee": Decimal(1000), "voucher": Decimal(3000)})

    def test_indonesian_number_text_and_csv(self):
        csv = ("No. Pesanan,Total Penghasilan,Biaya Komisi\nS1,\"Rp 88.000\",\"(6.000)\"\n")
        rows = parse_income(csv, "laporan.csv")

        self.assertEqual((rows[0].net, rows[0].fees["commission"]), (Decimal(88000), Decimal(6000)))

    def test_a_file_without_the_needed_columns_is_refused(self):
        with self.assertRaises(ChannelImportError):
            parse_income(_xlsx([["a", "b"]], header=["Nama", "Alamat"]))


class ImportTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user("income-owner")
        self.store = Store.objects.create(owner=owner, name="Toko", slug="toko-income")
        gudang = Warehouse.objects.create(store=self.store, name="G", code="G1")
        kopi = Product.objects.create(store=self.store, name="Kopi", slug="kopi", sku="KOPI",
            price=50000)
        InventoryLevel.objects.create(warehouse=gudang, product=kopi, on_hand=50)
        self.channel = SalesChannel.objects.create(store=self.store, platform="shopee", name="S")
        for order_id in ("S1", "S2"):
            import_order(self.channel, MarketplaceOrder(external_id=order_id, buyer_name="B",
                phone="0812", address="x", lines=[OrderLine(sku="KOPI", quantity=2,
                    unit_price=Decimal(50000))]))
        post_pending_entries()

    def test_income_moves_the_marketplace_receivable_to_cash_less_fees(self):
        rows = parse_income(_xlsx([["S1", "2026-11-10", 88000, -6000, 2000, 1000, 3000, 0, 100000],
            ["S2", "2026-11-10", 90000, -6000, 2000, 0, 0, 0, 100000]]))

        summary = import_income(self.channel, rows)

        self.assertEqual((summary["orders"], summary["payouts"], summary["errors"]), (2, 1, []))
        self.assertEqual(summary["net"], Decimal(178000))
        income = ChannelOrderIncome.objects.get(channel_order__external_order_id="S1")
        # 100.000 charged, 88.000 released: 12.000 of fees, itemised 6.000 + 2.000 + 1.000 +
        # 3.000 voucher, so nothing is left unexplained.
        self.assertEqual((income.commission, income.voucher, income.other_fee, income.total_fees),
            (Decimal(6000), Decimal(3000), Decimal(0), Decimal(12000)))
        self.assertEqual(_balance(self.store, MARKETPLACE), 0)
        self.assertEqual(_balance(self.store, CASH), Decimal(178000))
        # The 22.000 kept by Shopee is split over the fee accounts, by what each charge was.
        from django.db.models import Sum
        by = lambda *names: sum((ChannelOrderIncome.objects.aggregate(t=Sum(n))["t"] or 0
            for n in names), Decimal(0))
        self.assertEqual(_balance(self.store, "6110"), by("commission"))
        self.assertEqual(_balance(self.store, "6120"), by("service_fee", "admin_fee"))
        self.assertEqual(_balance(self.store, "6140"), by("voucher"))
        self.assertEqual(_balance(self.store, FEES), by("other_fee"))
        total = sum((_balance(self.store, code) for code in ("6100", "6110", "6120", "6130",
            "6140", "6150")), Decimal(0))
        self.assertEqual(total, Decimal(22000))
        self.assertGreater(_balance(self.store, "6110"), 0)
        self.assertGreater(_balance(self.store, "6140"), 0)

    def test_unitemised_deductions_become_other_fee(self):
        rows = parse_income(_xlsx([["S1", "2026-11-10", 90000, -4000, 0, 0, 0, 0, 100000]]))

        import_income(self.channel, rows)

        income = ChannelOrderIncome.objects.get()
        self.assertEqual((income.commission, income.other_fee), (Decimal(4000), Decimal(6000)))

    def test_reuploading_is_safe_and_new_orders_still_pay_out(self):
        first = parse_income(_xlsx([["S1", "2026-11-10", 90000, -4000, 0, 0, 0, 0, 100000]]))
        import_income(self.channel, first)

        again = import_income(self.channel, first)
        self.assertEqual((again["orders"], again["already"], again["payouts"]), (0, 1, 0))

        both = parse_income(_xlsx([["S1", "2026-11-10", 90000, -4000, 0, 0, 0, 0, 100000],
            ["S2", "2026-11-10", 90000, -4000, 0, 0, 0, 0, 100000]]))
        later = import_income(self.channel, both)
        self.assertEqual((later["orders"], later["already"], later["payouts"]), (1, 1, 1))
        self.assertEqual(Payout.objects.count(), 2)

    def test_unknown_orders_and_missing_dates_are_reported(self):
        rows = parse_income(_xlsx([["NOPE", "2026-11-10", 90000, 0, 0, 0, 0, 0, 0]]))
        self.assertIn("belum diimpor", import_income(self.channel, rows)["errors"][0])

        rows = parse_income(_xlsx([["S1", None, 90000, 0, 0, 0, 0, 0, 0]]))
        self.assertIn("tanggal pencairan", import_income(self.channel, rows)["errors"][0])
        summary = import_income(self.channel, rows, default_release=date(2026, 11, 11))
        self.assertEqual(summary["orders"], 1)

    def test_cancelled_orders_are_skipped(self):
        cancelled = MarketplaceOrder(external_id="S1", buyer_name="B", phone="0812", address="x",
            status_text="Batal", lines=[OrderLine(sku="KOPI", quantity=2,
                unit_price=Decimal(50000))])
        import_order(self.channel, cancelled)
        rows = parse_income(_xlsx([["S1", "2026-11-10", 0, 0, 0, 0, 0, 0, 0]]))

        summary = import_income(self.channel, rows)

        self.assertEqual((summary["orders"], len(summary["warnings"])), (0, 1))

    def test_page_imports_a_file(self):
        self.client.force_login(self.store.owner)
        upload = SimpleUploadedFile("income.xlsx", _xlsx(
            [["S1", "2026-11-10", 90000, -4000, 0, 0, 0, 0, 100000]]).read())

        response = self.client.post(f"/merchant/marketplace/penghasilan/?store={self.store.id}",
            {"channel": self.channel.id, "file": upload})

        self.assertContains(response, "Hasil impor")
        self.assertEqual(ChannelOrderIncome.objects.count(), 1)


class FeeAccountTests(ImportTests):
    def test_the_profit_and_loss_shows_each_marketplace_charge_on_its_own_line(self):
        import datetime

        from apps.accounting import reports
        rows = parse_income(_xlsx([["S1", "2026-11-10", 88000, -6000, 2000, 1000, 3000, 0, 100000]]))
        import_income(self.channel, rows)
        names = {r["name"] for r in reports.profit_and_loss(self.store, datetime.date(2026, 11, 1),
            datetime.date(2026, 11, 30))["sections"]["operating"]}
        self.assertIn("Komisi marketplace", names)
        self.assertIn("Voucher & promo ditanggung penjual", names)

    def test_a_plain_payout_file_still_books_all_fees_in_the_general_account(self):
        from datetime import date

        from apps.accounting.models import Payout
        from apps.accounting.payouts import import_payout
        from apps.orders.models import PaymentTransaction
        payment = PaymentTransaction.objects.filter(order__store=self.store).first()
        payout, _ = import_payout(self.store, "shopee", "CSV-1", {"paid_on": date(2026, 11, 2),
            "lines": [{"number": 2, "order": payment.external_id, "amount": payment.amount,
                "fee": Decimal(7000)}]})
        self.assertEqual(Payout.objects.get(pk=payout.pk).fee_breakdown, {})
        self.assertEqual(_balance(self.store, FEES), Decimal(7000))
        self.assertEqual(_balance(self.store, "6110"), 0)

    def test_the_split_always_adds_up_to_the_payout_fees_even_with_a_rounding_remainder(self):
        from apps.accounting.ledger import _fee_amounts
        from apps.accounting.models import Payout
        payout = Payout(fees=Decimal(1000), fee_breakdown={"6110": "600.00", "6140": "300.00"})
        split = _fee_amounts(payout)
        self.assertEqual(sum(split.values()), Decimal(1000))
        self.assertEqual(split[FEES], Decimal(100))
        over = Payout(fees=Decimal(500), fee_breakdown={"6110": "600.00"})
        self.assertEqual(sum(_fee_amounts(over).values()), Decimal(500))


class InvoiceMarketplacePanelTests(ImportTests):
    def test_the_invoice_shows_the_shop_the_fees_and_the_payout(self):
        from django.urls import reverse

        from apps.tax.models import Invoice
        client = self.client
        client.force_login(self.store.owner)
        invoice = Invoice.objects.filter(order__external_order_id="S1").get()
        before = client.get(reverse("merchant-invoice-detail", args=[invoice.id]))
        self.assertContains(before, "Shopee")
        self.assertContains(before, "Impor laporan penghasilan")   # no fees known yet
        rows = parse_income(_xlsx([["S1", "2026-11-10", 88000, -6000, 2000, 1000, 3000, 0, 100000]]))
        import_income(self.channel, rows)
        page = client.get(reverse("merchant-invoice-detail", args=[invoice.id]))
        self.assertContains(page, "Dana diterima")
        self.assertContains(page, "88.000")
        self.assertContains(page, "Komisi")
        self.assertContains(page, "−Rp6.000")
        self.assertContains(page, "Pencairan")

    def test_a_manual_invoice_has_no_marketplace_panel(self):
        from django.urls import reverse
        self.client.force_login(self.store.owner)
        from apps.catalog.models import Product
        from apps.tax.sales import create_sales_invoice
        invoice = create_sales_invoice(self.store, buyer_name="X", lines=[{
            "product": Product.objects.get(sku="KOPI"), "qty": 1, "unit_price": Decimal(50000)}])
        self.assertNotContains(self.client.get(reverse("merchant-invoice-detail", args=[invoice.id])),
            "Status di marketplace")


class ChannelRecapMarketplaceTests(ImportTests):
    def test_the_recap_shows_fees_money_received_and_orders_not_yet_paid_out(self):
        import datetime

        from django.urls import reverse
        from django.utils import timezone

        from apps.channels.invoice_recap import invoice_recap
        rows = parse_income(_xlsx([["S1", "2026-11-10", 88000, -6000, 2000, 1000, 3000, 0, 100000]]))
        import_income(self.channel, rows)   # S1 has its income report, S2 does not
        today = timezone.localdate()
        recap = invoice_recap(self.store, today - datetime.timedelta(days=5), today, today=today)
        shopee = next(r for r in recap["channels"] if r["channel"] == "shopee")
        self.assertEqual((shopee["fees"], shopee["released"], shopee["income_count"]),
            (Decimal(12000), Decimal(88000), 1))
        self.assertEqual((shopee["awaiting_count"], shopee["awaiting_value"]), (1, Decimal(100000)))
        self.assertTrue(recap["has_marketplace"])
        self.client.force_login(self.store.owner)
        page = self.client.get(reverse("merchant-channel-recap"))
        self.assertContains(page, "Dana diterima")
        self.assertContains(page, "Belum cair")

    def test_stores_without_marketplace_orders_do_not_get_the_extra_columns(self):
        from django.urls import reverse
        other = Store.objects.create(owner=self.store.owner, name="Polos", slug="polos-x")
        self.client.force_login(self.store.owner)
        page = self.client.get(reverse("merchant-channel-recap"), {"store": other.id})
        self.assertNotContains(page, "Biaya marketplace")


class WholeShopeeChainTests(TestCase):
    """Order file -> invoice -> income report with fees -> payout into a bank account -> bank
    statement line -> every reconciliation check. If these tie, the books are usable."""

    def test_from_the_order_file_to_a_reconciled_bank_account(self):
        import datetime

        from django.utils import timezone

        from apps.accounting import bank, controls
        from apps.accounting.ledger import post_everything
        from apps.accounting.models import BankStatementLine
        from apps.tax.models import Invoice
        owner = User.objects.create_user("chain-owner")
        store = Store.objects.create(owner=owner, name="Toko Rantai", slug="toko-rantai")
        gudang = Warehouse.objects.create(store=store, name="G", code="G1")
        kopi = Product.objects.create(store=store, name="Kopi", slug="kopi", sku="KOPI", price=50000,
            cost_price=Decimal(30000))
        InventoryLevel.objects.create(warehouse=gudang, product=kopi, on_hand=50)
        channel = SalesChannel.objects.create(store=store, platform="shopee", name="Toko Shopee")
        main = bank.bank_accounts(store)[0]
        bca = bank.add_bank_account(store, name="BCA")
        from apps.accounting import inventory
        inventory.adjust_books(store, date=datetime.date(2026, 10, 1))   # opening stock, as a client does
        placed = timezone.make_aware(datetime.datetime(2026, 10, 20, 10, 0))
        import_order(channel, MarketplaceOrder(external_id="S100", buyer_name="Rina", phone="0812",
            address="Bandung", status_text="Selesai", placed_at=placed, paid_at=placed,
            lines=[OrderLine(sku="KOPI", quantity=2, unit_price=Decimal(50000))]))
        post_everything()

        invoice = Invoice.objects.get(order__external_order_id="S100")
        self.assertEqual((invoice.issued_on, invoice.status, invoice.total),
            (datetime.date(2026, 10, 20), "paid", Decimal(100000)))
        self.assertEqual(_balance(store, MARKETPLACE), Decimal(100000))   # owed by Shopee, in October
        self.assertEqual(_balance(store, "5100"), Decimal(60000))          # cost of the 2 units

        rows = parse_income(_xlsx([["S100", "2026-10-27", 88000, -6000, 2000, 1000, 3000, 0, 100000]]))
        summary = import_income(channel, rows, bank_account=bca)
        self.assertEqual((summary["orders"], summary["payouts"], summary["errors"]), (1, 1, []))
        post_everything()

        self.assertEqual(_balance(store, MARKETPLACE), 0)
        self.assertEqual(_balance(store, bca.account.code), Decimal(88000))
        self.assertEqual(_balance(store, CASH), 0)
        self.assertEqual(_balance(store, "6110"), Decimal(6000))     # commission
        self.assertEqual(_balance(store, "6140"), Decimal(3000))     # seller voucher

        statement, _ = bank.parse_statement("Tanggal,Keterangan,Debit,Kredit,Saldo\n"
            "27/10/2026,CR SHOPEE PENCAIRAN,,88.000,88.000\n")
        bank.import_statement(bca, statement)
        line = BankStatementLine.objects.get()
        suggested = bank.suggestions(line)
        self.assertEqual(len(suggested), 1)
        bank.match_line(line, suggested[0])

        results = {c.key: c for c in controls.run_checks(store, datetime.date(2026, 11, 1))}
        for key in ("trial_balance", "unposted", "invoice_journal", "invoice_status", "receivable",
                f"bank_{bca.pk}", "providers", "income", "negative_inventory", "inventory"):
            self.assertEqual(results[key].status, "ok", f"{key}: {results[key].summary}")
