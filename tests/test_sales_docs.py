"""Credit notes, customer receipts and credit, quotes, and recurring invoices."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounting import bank
from apps.accounting.ledger import (
    CASH,
    COGS,
    CUSTOMER_CREDIT,
    RECEIVABLE,
    REFUNDS,
    VAT,
    account_balances,
)
from apps.catalog.models import InventoryLevel, Product, Warehouse
from apps.shops.models import Customer, Store
from apps.tax import credit_notes, quotes, receipts, recurring
from apps.tax.models import CreditNote, Invoice, Quote, RecurringInvoice, TaxProfile
from apps.tax.posting import void_invoice
from apps.tax.sales import create_sales_invoice
from apps.tax.services import record_payment


def balance(store, code):
    return {a.code: a.balance for a in account_balances(store)}.get(code, Decimal(0))


class Base(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("sd-owner", password="x")
        self.store = Store.objects.create(owner=self.owner, name="Toko Sales", slug="toko-sales",
            tax_rate=Decimal(11))
        TaxProfile.objects.create(store=self.store, is_pkp=True, legal_name="PT Sales",
            npwp="0123456789012345")
        self.today = timezone.localdate()
        warehouse = Warehouse.objects.create(store=self.store, code="G1", name="Gudang")
        self.kabel = Product.objects.create(store=self.store, sku="KBL", name="Kabel", slug="kabel",
            price=100_000, cost_price=60_000)
        self.lampu = Product.objects.create(store=self.store, sku="LMP", name="Lampu", slug="lampu",
            price=50_000, cost_price=30_000)
        for product in (self.kabel, self.lampu):
            InventoryLevel.objects.create(warehouse=warehouse, product=product, on_hand=100)
        self.customer = Customer.objects.create(store=self.store, name="Budi")
        self.main = bank.bank_accounts(self.store)[0]

    def on_hand(self, product):
        return InventoryLevel.objects.get(product=product).on_hand

    def invoice(self, *, kabel=2, lampu=0, credit=True, crm=True, days_ago=0, due_days=30):
        lines = [{"product": self.kabel, "qty": kabel, "unit_price": Decimal(100_000)}] if kabel else []
        if lampu:
            lines.append({"product": self.lampu, "qty": lampu, "unit_price": Decimal(50_000)})
        issued = self.today - timedelta(days=days_ago)
        return create_sales_invoice(self.store, lines=lines, buyer_name="Budi", issued_on=issued,
            customer=self.customer if crm else None, on_credit=credit,
            due_date=issued + timedelta(days=due_days))

    def lines(self, invoice, qty=1, restock=True):
        item = invoice.items.first()
        return [{"item": item, "quantity": qty, "unit_price": item.unit_price, "restock": restock}]


class CreditNoteTests(Base):
    def test_note_on_an_unpaid_invoice_reduces_what_is_owed(self):
        invoice = self.invoice()  # 2 × 100.000 + PPN 22.000 = 222.000
        self.assertEqual(invoice.total, Decimal(222_000))
        note = credit_notes.create_credit_note(invoice, lines=self.lines(invoice), issued_on=self.today)
        self.assertEqual((note.subtotal, note.tax, note.total), (Decimal(100_000), Decimal(11_000),
            Decimal(111_000)))
        self.assertEqual((note.applied, note.paid_out), (Decimal(111_000), Decimal(0)))
        invoice = Invoice.objects.get(pk=invoice.pk)
        self.assertEqual(invoice.amount_due, Decimal(111_000))
        self.assertEqual(balance(self.store, RECEIVABLE), Decimal(111_000))
        self.assertEqual(balance(self.store, REFUNDS), Decimal(-100_000))  # contra revenue
        self.assertEqual(balance(self.store, VAT), Decimal(11_000))      # 22.000 owed − 11.000
        # goods came back: shelf, inventory and cost of goods follow
        self.assertEqual(self.on_hand(self.kabel), 99)
        self.assertEqual(balance(self.store, COGS), Decimal(60_000))

    def test_without_restock_the_stock_and_cost_stay(self):
        invoice = self.invoice()
        credit_notes.create_credit_note(invoice, lines=self.lines(invoice, restock=False),
            issued_on=self.today)
        self.assertEqual(self.on_hand(self.kabel), 98)
        self.assertEqual(balance(self.store, COGS), Decimal(120_000))

    def test_full_credit_settles_the_invoice(self):
        invoice = self.invoice()
        credit_notes.create_credit_note(invoice, lines=self.lines(invoice, qty=2), issued_on=self.today)
        invoice = Invoice.objects.get(pk=invoice.pk)
        self.assertEqual((invoice.status, invoice.amount_due), ("paid", Decimal(0)))
        self.assertEqual(balance(self.store, RECEIVABLE), Decimal(0))

    def test_cannot_credit_more_than_was_invoiced(self):
        invoice = self.invoice()
        credit_notes.create_credit_note(invoice, lines=self.lines(invoice, qty=2), issued_on=self.today)
        with self.assertRaises(credit_notes.CreditNoteError):
            credit_notes.create_credit_note(invoice, lines=self.lines(invoice, qty=1),
                issued_on=self.today)

    def test_paid_invoice_excess_becomes_customer_credit(self):
        invoice = self.invoice()
        record_payment(invoice, Decimal(222_000), self.today, "transfer")
        note = credit_notes.create_credit_note(invoice, lines=self.lines(invoice),
            issued_on=self.today)
        self.assertEqual((note.applied, note.paid_out), (Decimal(0), Decimal(111_000)))
        self.assertEqual(balance(self.store, RECEIVABLE), Decimal(0))
        self.assertEqual(balance(self.store, CUSTOMER_CREDIT), Decimal(111_000))
        self.assertEqual(receipts.credit_balance(self.store, self.customer, "Budi"), Decimal(111_000))

    def test_paid_invoice_excess_can_be_refunded_from_a_bank_account(self):
        petty = bank.add_bank_account(self.store, name="BCA")
        invoice = self.invoice()
        record_payment(invoice, Decimal(222_000), self.today, "transfer")
        credit_notes.create_credit_note(invoice, lines=self.lines(invoice), issued_on=self.today,
            excess_action="refund", bank_account=petty)
        self.assertEqual(balance(self.store, petty.account.code), Decimal(-111_000))
        self.assertEqual(balance(self.store, CUSTOMER_CREDIT), Decimal(0))

    def test_part_paid_invoice_splits_between_balance_and_credit(self):
        invoice = self.invoice()  # 222.000, paid 200.000, owes 22.000; note of 111.000
        record_payment(invoice, Decimal(200_000), self.today, "transfer")
        note = credit_notes.create_credit_note(invoice, lines=self.lines(invoice), issued_on=self.today)
        self.assertEqual((note.applied, note.paid_out), (Decimal(22_000), Decimal(89_000)))
        self.assertEqual(balance(self.store, RECEIVABLE), Decimal(0))
        self.assertEqual(balance(self.store, CUSTOMER_CREDIT), Decimal(89_000))

    def test_void_note_restores_everything(self):
        invoice = self.invoice()
        note = credit_notes.create_credit_note(invoice, lines=self.lines(invoice), issued_on=self.today)
        credit_notes.void_credit_note(note)
        self.assertEqual(balance(self.store, RECEIVABLE), Decimal(222_000))
        self.assertEqual(balance(self.store, REFUNDS), Decimal(0))
        self.assertEqual(self.on_hand(self.kabel), 98)
        self.assertEqual(Invoice.objects.get(pk=invoice.pk).amount_due, Decimal(222_000))

    def test_invoice_with_a_note_cannot_be_voided_until_the_note_is(self):
        from apps.accounting.ledger import LedgerError
        invoice = self.invoice()
        note = credit_notes.create_credit_note(invoice, lines=self.lines(invoice), issued_on=self.today)
        with self.assertRaises(LedgerError):
            void_invoice(invoice)
        credit_notes.void_credit_note(note)
        void_invoice(Invoice.objects.get(pk=invoice.pk))
        self.assertEqual(balance(self.store, RECEIVABLE), Decimal(0))

    def test_voiding_a_paid_off_by_credit_note_invoice_reopens_it(self):
        invoice = self.invoice()
        note = credit_notes.create_credit_note(invoice, lines=self.lines(invoice, qty=2),
            issued_on=self.today)
        credit_notes.void_credit_note(note)
        self.assertEqual(Invoice.objects.get(pk=invoice.pk).status, "issued")

    def test_note_feeds_the_tax_report(self):
        from apps.tax.services import monthly_tax
        invoice = self.invoice()
        credit_notes.create_credit_note(invoice, lines=self.lines(invoice), issued_on=self.today)
        report = monthly_tax(self.store, self.today.year, self.today.month)
        self.assertEqual(report.turnover, Decimal(100_000))  # 200.000 sold − 100.000 returned
        self.assertEqual(report.vat, Decimal(11_000))


class ReceiptTests(Base):
    def test_one_payment_pays_several_invoices_oldest_first_and_keeps_the_rest(self):
        first, second = self.invoice(days_ago=10), self.invoice(kabel=1)  # 222.000 + 111.000
        invoices = receipts.open_invoices_for(self.store, self.customer, "Budi")
        allocations = receipts.auto_allocate(invoices, Decimal(400_000))
        receipt = receipts.receive_payment(self.store, customer=self.customer, buyer_name="Budi",
            date=self.today, amount=400_000, method="transfer", allocations=allocations)
        self.assertEqual(receipt.deposit, Decimal(67_000))
        self.assertEqual(Invoice.objects.get(pk=first.pk).status, "paid")
        self.assertEqual(Invoice.objects.get(pk=second.pk).status, "paid")
        self.assertEqual(balance(self.store, CASH), Decimal(400_000))
        self.assertEqual(balance(self.store, RECEIVABLE), Decimal(0))
        self.assertEqual(receipts.credit_balance(self.store, self.customer, "Budi"), Decimal(67_000))

    def test_partial_allocation_and_over_allocation(self):
        invoice = self.invoice()
        with self.assertRaises(receipts.ReceiptError):  # more allocated than received
            receipts.receive_payment(self.store, customer=self.customer, buyer_name="Budi",
                date=self.today, amount=100_000, method="transfer", allocations=[(invoice, 150_000)])
        with self.assertRaises(receipts.ReceiptError):  # more than the invoice owes
            receipts.receive_payment(self.store, customer=self.customer, buyer_name="Budi",
                date=self.today, amount=500_000, method="transfer", allocations=[(invoice, 500_000)])
        self.assertEqual(balance(self.store, CASH), Decimal(0))

    def test_rest_must_be_kept_explicitly(self):
        invoice = self.invoice()
        with self.assertRaises(receipts.ReceiptError):
            receipts.receive_payment(self.store, customer=self.customer, buyer_name="Budi",
                date=self.today, amount=300_000, method="transfer", allocations=[(invoice, 222_000)],
                keep_rest=False)

    def test_receipt_goes_to_the_chosen_bank_account(self):
        petty = bank.add_bank_account(self.store, name="Mandiri")
        invoice = self.invoice()
        receipts.receive_payment(self.store, customer=self.customer, buyer_name="Budi",
            date=self.today, amount=222_000, method="transfer", allocations=[(invoice, 222_000)],
            bank_account=petty)
        self.assertEqual(balance(self.store, petty.account.code), Decimal(222_000))
        self.assertEqual(balance(self.store, CASH), Decimal(0))

    def test_credit_pays_a_later_invoice_and_can_be_refunded(self):
        receipts.receive_payment(self.store, customer=self.customer, buyer_name="Budi",
            date=self.today, amount=500_000, method="transfer", allocations=[])
        self.assertEqual(balance(self.store, CUSTOMER_CREDIT), Decimal(500_000))
        invoice = self.invoice()
        receipts.apply_credit(invoice, Decimal(222_000), self.today)
        self.assertEqual(Invoice.objects.get(pk=invoice.pk).status, "paid")
        self.assertEqual(balance(self.store, CUSTOMER_CREDIT), Decimal(278_000))
        self.assertEqual(balance(self.store, RECEIVABLE), Decimal(0))
        with self.assertRaises(receipts.ReceiptError):
            receipts.apply_credit(self.invoice(), Decimal(300_000), self.today)  # only 278.000 left
        receipts.refund_credit(self.store, self.customer, "Budi", Decimal(278_000), self.today)
        self.assertEqual(balance(self.store, CUSTOMER_CREDIT), Decimal(0))
        self.assertEqual(balance(self.store, CASH), Decimal(222_000))  # 500.000 in, 278.000 back

    def test_voiding_an_invoice_paid_by_credit_returns_the_credit(self):
        receipts.receive_payment(self.store, customer=self.customer, buyer_name="Budi",
            date=self.today, amount=300_000, method="transfer", allocations=[])
        invoice = self.invoice()
        receipts.apply_credit(invoice, Decimal(222_000), self.today)
        void_invoice(Invoice.objects.get(pk=invoice.pk))
        self.assertEqual(receipts.credit_balance(self.store, self.customer, "Budi"), Decimal(300_000))

    def test_void_receipt_reopens_invoices(self):
        invoice = self.invoice()
        receipt = receipts.receive_payment(self.store, customer=self.customer, buyer_name="Budi",
            date=self.today, amount=250_000, method="transfer", allocations=[(invoice, 222_000)])
        receipts.void_receipt(receipt)
        self.assertEqual(Invoice.objects.get(pk=invoice.pk).status, "issued")
        self.assertEqual(balance(self.store, CASH), Decimal(0))
        self.assertEqual(receipts.credit_balance(self.store, self.customer, "Budi"), Decimal(0))
        self.assertEqual(balance(self.store, RECEIVABLE), Decimal(222_000))

    def test_buyer_without_crm_record_is_matched_by_name(self):
        invoice = self.invoice(crm=False)
        self.assertIsNone(invoice.order.customer)
        found = receipts.open_invoices_for(self.store, None, "budi")
        self.assertEqual([i.pk for i in found], [invoice.pk])


class QuoteTests(Base):
    def make(self, **kw):
        return quotes.save_quote(self.store, buyer_name="Budi", customer=self.customer,
            lines=[{"product": self.kabel, "qty": 3, "unit_price": Decimal(100_000)},
                {"product": self.lampu, "qty": 2, "unit_price": Decimal(50_000)}], **kw)

    def test_quote_books_nothing_and_computes_totals(self):
        quote = self.make(discount=Decimal(20_000), shipping=Decimal(10_000))
        self.assertEqual(quote.subtotal, Decimal(400_000))
        self.assertEqual(quote.tax, Decimal(41_800))  # 11% of 380.000
        self.assertEqual(quote.total, Decimal(431_800))
        self.assertRegex(quote.number, r"^QT/\d{4}/000001$")
        self.assertEqual(balance(self.store, RECEIVABLE), Decimal(0))
        self.assertEqual(self.on_hand(self.kabel), 100)

    def test_converting_makes_a_credit_invoice_once(self):
        quote = self.make()
        invoice = quotes.convert_to_invoice(quote, due_days=21)
        quote.refresh_from_db()
        self.assertEqual((quote.status, quote.invoice_id), ("invoiced", invoice.pk))
        self.assertEqual(invoice.total, quote.total)
        self.assertEqual(invoice.due_date, self.today + timedelta(days=21))
        self.assertEqual(balance(self.store, RECEIVABLE), quote.total)
        self.assertEqual(self.on_hand(self.kabel), 97)
        with self.assertRaises(quotes.QuoteError):
            quotes.convert_to_invoice(quote)

    def test_declined_quote_cannot_be_invoiced_and_editing_replaces_lines(self):
        quote = self.make()
        quotes.set_status(quote, "declined")
        with self.assertRaises(quotes.QuoteError):
            quotes.convert_to_invoice(quote)
        quote = quotes.save_quote(self.store, quote=quote, buyer_name="Budi",
            lines=[{"product": self.lampu, "qty": 1, "unit_price": Decimal(50_000)}])
        self.assertEqual(quote.items.count(), 1)
        self.assertEqual(quote.subtotal, Decimal(50_000))

    def test_expiry(self):
        quote = self.make(issued_on=self.today - timedelta(days=30), valid_days=14)
        self.assertTrue(quotes.is_expired(quote))
        quotes.set_status(quote, "accepted")
        self.assertFalse(quotes.is_expired(quote))


class RecurringTests(Base):
    def make(self, **kw):
        defaults = {"name": "Pasokan bulanan", "buyer_name": "Budi", "customer": self.customer,
            "interval": "month", "next_run": self.today, "due_days": 14,
            "lines": [{"product": self.kabel, "qty": 1, "unit_price": Decimal(100_000)}]}
        defaults.update(kw)
        return recurring.save_recurring(self.store, **defaults)

    def test_runs_once_per_due_date_and_advances(self):
        template = self.make()
        issued = recurring.run_due(self.today)
        self.assertEqual(len(issued), 1)
        self.assertEqual(issued[0].due_date, self.today + timedelta(days=14))
        self.assertEqual(recurring.run_due(self.today), [])  # idempotent
        template.refresh_from_db()
        self.assertEqual((template.runs, template.next_run),
            (1, recurring.advance(self.today, "month")))
        self.assertEqual(balance(self.store, RECEIVABLE), Decimal(111_000))

    def test_catches_up_missed_periods_in_order(self):
        start = self.today - timedelta(days=70)
        self.make(next_run=start)
        issued = recurring.run_due(self.today)
        self.assertEqual([i.issued_on for i in issued],
            [start, recurring.advance(start, "month"),
             recurring.advance(recurring.advance(start, "month"), "month")])

    def test_stops_at_the_end_date_and_when_paused(self):
        template = self.make(next_run=self.today - timedelta(days=60),
            end_date=self.today - timedelta(days=25))
        issued = recurring.run_due(self.today)
        self.assertEqual(len(issued), 2)
        template.refresh_from_db()
        self.assertFalse(template.is_active)
        paused = self.make(name="Jeda", next_run=self.today)
        RecurringInvoice.objects.filter(pk=paused.pk).update(is_active=False)
        self.assertEqual(recurring.run_due(self.today), [])

    def test_month_end_dates_are_clamped(self):
        from datetime import date
        self.assertEqual(recurring.advance(date(2026, 1, 31), "month"), date(2026, 2, 28))
        self.assertEqual(recurring.advance(date(2026, 11, 30), "quarter"), date(2027, 2, 28))
        self.assertEqual(recurring.advance(date(2026, 3, 1), "week"), date(2026, 3, 8))


class PageTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)

    def test_every_page_renders(self):
        invoice = self.invoice()
        note = credit_notes.create_credit_note(invoice, lines=self.lines(invoice), issued_on=self.today)
        receipt = receipts.receive_payment(self.store, customer=self.customer, buyer_name="Budi",
            date=self.today, amount=300_000, method="transfer", allocations=[(invoice, 100_000)])
        quote = QuoteTests.make(self)
        template = RecurringTests.make(self)
        urls = [reverse("merchant-invoice-detail", args=[invoice.id]),
            reverse("merchant-credit-notes"), reverse("merchant-credit-note-detail", args=[note.id]),
            reverse("merchant-credit-note-new") + f"?invoice={invoice.id}",
            reverse("merchant-receive-payment"),
            reverse("merchant-receive-payment") + f"?customer={self.customer.id}",
            reverse("merchant-receive-payment") + "?buyer=Tanpa+CRM",
            reverse("merchant-receipts"), reverse("merchant-receipt-detail", args=[receipt.id]),
            reverse("merchant-customer-credit"), reverse("merchant-quotes"),
            reverse("merchant-quote-new"), reverse("merchant-quote-detail", args=[quote.id]),
            reverse("merchant-quote-edit", args=[quote.id]), reverse("merchant-recurring"),
            reverse("merchant-recurring-new"), reverse("merchant-recurring-edit", args=[template.id])]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_credit_note_through_the_form(self):
        invoice = self.invoice()
        item = invoice.items.first()
        response = self.client.post(reverse("merchant-credit-note-new"), {"invoice": invoice.id,
            "issued_on": self.today.isoformat(), "reason": "return", f"qty_{item.id}": "1",
            f"price_{item.id}": "100000", f"restock_{item.id}": "1", "excess_action": "credit"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(CreditNote.objects.get().total, Decimal(111_000))
        page = self.client.get(reverse("merchant-invoice-detail", args=[invoice.id]))
        self.assertContains(page, "Nota kredit")
        self.assertContains(page, "CN/")

    def test_receipt_through_the_form(self):
        invoice = self.invoice()
        response = self.client.post(reverse("merchant-receive-payment"), {
            "customer": self.customer.id, "amount": "222.000", "date": self.today.isoformat(),
            "method": "transfer", f"alloc_{invoice.id}": "222.000", "keep_rest": "1"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Invoice.objects.get(pk=invoice.pk).status, "paid")

    def test_quote_through_the_form_and_convert(self):
        response = self.client.post(reverse("merchant-quote-new"), {"buyer_name": "Budi",
            "customer_id": self.customer.id, "product[]": [self.kabel.id, ""], "qty[]": ["2", ""],
            "unit_price[]": ["100.000", ""], "discount": "", "shipping": ""})
        self.assertEqual(response.status_code, 302)
        quote = Quote.objects.get()
        convert = self.client.post(reverse("merchant-quote-detail", args=[quote.id]),
            {"action": "convert", "due_days": "30"})
        self.assertEqual(convert.status_code, 302)
        self.assertTrue(Invoice.objects.filter(quote=quote).exists())

    def test_viewer_cannot_post_money_pages(self):
        viewer = User.objects.create_user("sd-viewer", password="x")
        from apps.shops.models import StoreStaff
        StoreStaff.objects.create(store=self.store, user=viewer, role="viewer")
        self.client.force_login(viewer)
        self.assertEqual(self.client.get(reverse("merchant-receive-payment")).status_code, 404)
        self.assertEqual(self.client.get(reverse("merchant-credit-notes")).status_code, 200)


class InvoiceViewTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)

    def test_a_credit_note_alone_is_not_a_payment(self):
        invoice = self.invoice()
        credit_notes.create_credit_note(invoice, lines=self.lines(invoice), issued_on=self.today)
        self.assertEqual(Invoice.objects.get(pk=invoice.pk).status, "issued")
        record_payment(invoice, Decimal(50_000), self.today, "transfer")
        self.assertEqual(Invoice.objects.get(pk=invoice.pk).status, "partial")

    def test_invoice_list_balance_follows_credit_notes(self):
        invoice = self.invoice()
        credit_notes.create_credit_note(invoice, lines=self.lines(invoice), issued_on=self.today)
        page = self.client.get(reverse("merchant-sales-invoices"))
        self.assertContains(page, "111.000")
        self.assertNotContains(page, "222.000")

    def test_manual_invoice_form_includes_ppn_and_company_is_not_repeated(self):
        page = self.client.get(reverse("merchant-invoice-new"))
        self.assertContains(page, "PPN 11%")
        response = self.client.post(reverse("merchant-invoice-new"), {"channel": "manual",
            "issued_on": self.today.isoformat(), "buyer_name": "PT Maju", "company_name": "PT Maju",
            "product[]": [self.kabel.pk], "description[]": [""], "qty[]": ["1"],
            "unit_price[]": ["100000"], "line_discount[]": [""], "discount": "0", "shipping": "0",
            "payment_status": "credit", "due_date": (self.today + timedelta(days=14)).isoformat()})
        self.assertEqual(response.status_code, 302)
        invoice = Invoice.objects.get(buyer_name="PT Maju")
        self.assertEqual((invoice.tax, invoice.total, invoice.status), (Decimal(11_000),
            Decimal(111_000), "issued"))
        self.assertEqual(balance(self.store, RECEIVABLE), Decimal(111_000))

    def test_paying_from_the_invoice_page_into_a_bank_account(self):
        petty = bank.add_bank_account(self.store, name="BCA")
        invoice = self.invoice()
        response = self.client.post(reverse("merchant-invoice-detail", args=[invoice.id]),
            {"action": "pay", "amount": "222000", "paid_on": self.today.isoformat(),
             "method": "transfer", "bank_account": petty.id})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(balance(self.store, petty.account.code), Decimal(222_000))
        self.assertEqual(Invoice.objects.get(pk=invoice.pk).status, "paid")

    def test_voiding_an_invoice_with_a_note_shows_a_message_not_an_error(self):
        invoice = self.invoice()
        credit_notes.create_credit_note(invoice, lines=self.lines(invoice), issued_on=self.today)
        response = self.client.post(reverse("merchant-invoice-detail", args=[invoice.id]),
            {"action": "void"}, follow=True)
        self.assertContains(response, "nota kredit")
        self.assertEqual(Invoice.objects.get(pk=invoice.pk).status, "issued")

    def test_pdf_shows_the_credit(self):
        invoice = self.invoice()
        credit_notes.create_credit_note(invoice, lines=self.lines(invoice), issued_on=self.today)
        response = self.client.get(reverse("merchant-invoice-pdf", args=[invoice.id]))
        self.assertEqual(response.status_code, 200)

    def test_menu_cancel_with_a_note_shows_a_message_not_an_error(self):
        invoice = self.invoice()
        credit_notes.create_credit_note(invoice, lines=self.lines(invoice), issued_on=self.today)
        response = self.client.post(reverse("merchant-invoice-delete", args=[invoice.id]), follow=True)
        self.assertContains(response, "nota kredit")

    def test_taking_back_a_sale_voids_its_credit_notes_first(self):
        from apps.channels.lifecycle import take_back_sale
        invoice = self.invoice()
        credit_notes.create_credit_note(invoice, lines=self.lines(invoice), issued_on=self.today)
        take_back_sale(invoice.order, reason="dibatalkan marketplace")
        self.assertEqual(Invoice.objects.get(pk=invoice.pk).status, "void")
        self.assertEqual(CreditNote.objects.get().status, "void")
        self.assertEqual(balance(self.store, RECEIVABLE), Decimal(0))
        self.assertEqual(self.on_hand(self.kabel), 100)


class ChannelRecapTests(Base):
    def make(self, channel, kabel=1, days_ago=0, credit=False):
        issued = self.today - timedelta(days=days_ago)
        return create_sales_invoice(self.store, lines=[{"product": self.kabel, "qty": kabel,
            "unit_price": Decimal(100_000)}], buyer_name="Pembeli", issued_on=issued,
            channel=channel, on_credit=credit, due_date=issued + timedelta(days=14))

    def test_recap_separates_shopee_and_tokopedia(self):
        from apps.channels.invoice_recap import invoice_recap
        self.make("shopee", kabel=2)
        self.make("shopee")
        self.make("tokopedia", kabel=3, credit=True)
        recap = invoice_recap(self.store, self.today.replace(day=1), self.today, today=self.today)
        rows = {r["channel"]: r for r in recap["channels"]}
        self.assertEqual((rows["shopee"]["count"], rows["shopee"]["invoiced"]),
            (2, Decimal(333_000)))  # 222.000 + 111.000
        self.assertEqual((rows["tokopedia"]["count"], rows["tokopedia"]["invoiced"]),
            (1, Decimal(333_000)))
        self.assertEqual(recap["totals"]["count"], 3)
        self.assertEqual(recap["totals"]["invoiced"], Decimal(666_000))
        self.assertEqual(rows["shopee"]["share"] + rows["tokopedia"]["share"], Decimal("100.0"))

    def test_outstanding_overdue_credit_notes_and_voids(self):
        from apps.channels.invoice_recap import invoice_recap
        paid = self.make("shopee")                                    # paid at once
        late = self.make("tokopedia", kabel=2, days_ago=40, credit=True)  # due 26 days ago
        credit_notes.create_credit_note(late, lines=self.lines(late), issued_on=self.today)
        gone = self.make("tokopedia")
        void_invoice(gone)
        recap = invoice_recap(self.store, self.today - timedelta(days=60), self.today,
            today=self.today)
        rows = {r["channel"]: r for r in recap["channels"]}
        self.assertEqual(rows["shopee"]["outstanding"], Decimal(0))
        self.assertEqual(rows["shopee"]["paid"], paid.total)
        toko = rows["tokopedia"]
        self.assertEqual((toko["count"], toko["void_count"]), (1, 1))
        self.assertEqual(toko["credited"], Decimal(111_000))          # half of 222.000
        self.assertEqual(toko["net"], Decimal(111_000))
        self.assertEqual(toko["outstanding"], Decimal(111_000))
        self.assertEqual((toko["overdue"], toko["overdue_count"]), (Decimal(111_000), 1))
        # the recap ties to the receivable account
        self.assertEqual(balance(self.store, RECEIVABLE), recap["totals"]["outstanding"])

    def test_months_matrix_and_other_period_excluded(self):
        from apps.channels.invoice_recap import invoice_recap
        self.make("shopee", days_ago=45)
        self.make("tokopedia")
        recap = invoice_recap(self.store, self.today - timedelta(days=90), self.today,
            today=self.today)
        self.assertGreaterEqual(len(recap["month_rows"]), 2)
        self.assertEqual(sum(recap["month_totals"]), recap["totals"]["net"])
        narrow = invoice_recap(self.store, self.today, self.today, today=self.today)
        self.assertEqual([r["channel"] for r in narrow["channels"]], ["tokopedia"])

    def test_pages(self):
        self.client.force_login(self.owner)
        self.make("shopee")
        self.make("tokopedia")
        page = self.client.get(reverse("merchant-channel-recap"))
        self.assertContains(page, "Shopee")
        self.assertContains(page, "Tokopedia")
        self.assertContains(page, "channel=shopee")
        csv_page = self.client.get(reverse("merchant-channel-recap") + "?format=csv")
        self.assertEqual(csv_page["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("Shopee", csv_page.content.decode())
        invoices = self.client.get(reverse("merchant-sales-invoices"))
        self.assertContains(invoices, "Rekap per channel")
        only = self.client.get(reverse("merchant-sales-invoices") + "?channel=tokopedia")
        self.assertEqual(only.status_code, 200)
        self.assertEqual(len(only.context["orders"]), 1)


class OneWayToCreateTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)

    def test_invoice_creation_is_one_button_not_several_menu_items(self):
        page = self.client.get(reverse("merchant-sales-invoices"))
        html = page.content.decode()
        nav = html.split('id="pd-nav"')[1].split("</nav>")[0]
        self.assertNotIn("Buat invoice", nav)
        self.assertNotIn("Buat pesanan", nav)
        self.assertNotIn("Invoice massal", html)
        header = html.split('<section class="relative overflow-hidden')[1].split("</section>")[0]
        self.assertEqual(header.count(f'href="{reverse("merchant-invoice-new")}"'), 1)
        self.assertContains(page, reverse("merchant-invoice-new"))

    def test_create_menu_lists_invoice_once_and_no_order(self):
        html = self.client.get(reverse("merchant-dashboard")).content.decode()
        create = html.split("create-menu")[1].split("</details>")[0]
        self.assertEqual(create.count(reverse("merchant-invoice-new")), 1)
        self.assertNotIn(reverse("merchant-order-new"), create)

    def test_the_old_separate_pages_are_gone_and_one_form_serves_new_edit_and_duplicate(self):
        invoice = self.invoice()
        for url in (reverse("merchant-invoice-new"),
                reverse("merchant-invoice-edit", args=[invoice.id]),
                reverse("merchant-invoice-duplicate", args=[invoice.id])):
            page = self.client.get(url)
            self.assertEqual(page.status_code, 200)
            self.assertTemplateUsed(page, "merchant/invoice_form.html")
        from django.urls import NoReverseMatch
        with self.assertRaises(NoReverseMatch):
            reverse("merchant-invoice-batch")

    def test_orders_without_an_invoice_get_a_banner_only_when_there_are_some(self):
        from apps.orders.models import Order
        page = self.client.get(reverse("merchant-sales-invoices"))
        self.assertNotContains(page, "belum punya invoice")
        Order.objects.create(store=self.store, customer_name="Tanpa invoice", phone="-",
            shipping_address="-", subtotal=Decimal(10_000), total=Decimal(10_000), channel="manual",
            status="paid", idempotency_key="no-inv-1")
        page = self.client.get(reverse("merchant-sales-invoices"))
        self.assertContains(page, "belum punya invoice")
        self.assertContains(page, "Terbitkan semua")


class StockAndBooksTests(Base):
    def test_invoice_beyond_the_stock_is_refused_and_leaves_no_trace(self):
        from apps.accounting.ledger import INVENTORY
        from apps.tax.sales import StockShortage
        with self.assertRaises(StockShortage) as caught:
            self.invoice(kabel=101)
        self.assertIn("butuh 101, tersedia 100", str(caught.exception))
        self.assertEqual(self.on_hand(self.kabel), 100)
        self.assertEqual(Invoice.objects.count(), 0)
        self.assertEqual(balance(self.store, INVENTORY), Decimal(0))
        self.assertEqual(balance(self.store, COGS), Decimal(0))

    def test_issuing_takes_stock_and_books_its_cost_together(self):
        from apps.accounting.ledger import INVENTORY
        self.invoice(kabel=3)
        self.assertEqual(self.on_hand(self.kabel), 97)
        self.assertEqual(balance(self.store, COGS), Decimal(180_000))   # 3 × 60.000
        self.assertEqual(balance(self.store, INVENTORY), Decimal(-180_000))

    def test_product_without_stock_tracking_is_not_blocked(self):
        from apps.catalog.models import InventoryLevel
        InventoryLevel.objects.filter(product=self.lampu).delete()
        self.invoice(kabel=0, lampu=500)  # a service-like item: no inventory level
        self.assertEqual(Invoice.objects.count(), 1)

    def test_same_product_on_two_lines_counts_together(self):
        from apps.tax.sales import StockShortage
        with self.assertRaises(StockShortage):
            create_sales_invoice(self.store, buyer_name="X", lines=[
                {"product": self.kabel, "qty": 60, "unit_price": Decimal(1)},
                {"product": self.kabel, "qty": 60, "unit_price": Decimal(1)}])

    def test_valuation_reconciles_stock_with_the_books_and_adjusts(self):
        from apps.accounting import inventory
        from apps.accounting.ledger import INVENTORY
        data = inventory.reconciliation(self.store)
        # 100 × 60.000 + 100 × 30.000 on the shelves, nothing in the books yet
        self.assertEqual(data["shelf_value"], Decimal(9_000_000))
        self.assertEqual((data["book"], data["difference"]), (Decimal(0), Decimal(-9_000_000)))
        inventory.adjust_books(self.store, date=self.today)
        self.assertEqual(balance(self.store, INVENTORY), Decimal(9_000_000))
        self.assertEqual(inventory.reconciliation(self.store)["difference"], Decimal(0))
        self.invoice(kabel=2)  # a sale moves both sides together
        self.assertEqual(inventory.reconciliation(self.store)["difference"], Decimal(0))
        self.assertEqual(inventory.adjust_books(self.store, date=self.today), [])

    def test_bill_not_yet_received_is_explained_not_flagged(self):
        from apps.accounting import inventory
        from apps.accounting.models import Supplier
        from apps.accounting.payables import create_bill, receive_bill
        inventory.adjust_books(self.store, date=self.today)
        supplier = Supplier.objects.create(store=self.store, name="CV Stok")
        bill = create_bill(self.store, supplier=supplier, bill_date=self.today, category="1400",
            lines=[{"description": "Kabel", "qty": 10, "unit_price": Decimal(55_000), "sku": "KBL"}])
        data = inventory.reconciliation(self.store)
        self.assertEqual(data["in_transit"], Decimal(550_000))
        self.assertEqual(data["difference"], Decimal(0))      # in the books, on the way
        receive_bill(bill)
        data = inventory.reconciliation(self.store)
        self.assertEqual(data["in_transit"], Decimal(0))
        self.assertEqual(self.on_hand(self.kabel), 110)
        self.assertEqual(data["difference"], Decimal(0))      # now on the shelf at its new cost

    def test_page_and_adjustment(self):
        self.client.force_login(self.owner)
        page = self.client.get(reverse("merchant-inventory-value"))
        self.assertContains(page, "Nilai persediaan")
        self.assertContains(page, "Sesuaikan buku dengan stok")
        self.client.post(reverse("merchant-inventory-value"), {"date": self.today.isoformat(),
            "contra": "opening"})
        self.assertContains(self.client.get(reverse("merchant-inventory-value")),
            "sama dengan nilai stok")
        self.assertEqual(self.client.get(reverse("merchant-inventory-value") + "?format=csv")
            ["Content-Type"], "text/csv")


class OpeningBalanceTests(Base):
    def test_posts_one_balanced_entry_with_modal_as_the_rest(self):
        from apps.accounting import opening
        from apps.accounting.ledger import EQUITY, INVENTORY, LOANS, PAYABLE
        entry = opening.post_opening(self.store, date=self.today, banks={self.main: 5_000_000},
            receivable=2_000_000, payable=1_500_000, loans=1_000_000, stock=(9_000_000, 0))
        self.assertEqual(entry.source_type, "opening")
        self.assertEqual(balance(self.store, CASH), Decimal(5_000_000))
        self.assertEqual(balance(self.store, RECEIVABLE), Decimal(2_000_000))
        self.assertEqual(balance(self.store, INVENTORY), Decimal(9_000_000))
        self.assertEqual(balance(self.store, PAYABLE), Decimal(1_500_000))
        self.assertEqual(balance(self.store, LOANS), Decimal(1_000_000))
        # assets 16.000.000 − debts 2.500.000
        self.assertEqual(balance(self.store, EQUITY), Decimal(13_500_000))

    def test_only_once_until_undone_and_needs_an_amount(self):
        from apps.accounting import opening
        from apps.accounting.ledger import LedgerError
        with self.assertRaises(LedgerError):
            opening.post_opening(self.store, date=self.today)
        opening.post_opening(self.store, date=self.today, banks={self.main: 100_000})
        with self.assertRaises(LedgerError):
            opening.post_opening(self.store, date=self.today, banks={self.main: 1})
        opening.undo_opening(self.store)
        self.assertEqual(balance(self.store, CASH), Decimal(0))
        opening.post_opening(self.store, date=self.today, banks={self.main: 250_000})
        self.assertEqual(balance(self.store, CASH), Decimal(250_000))

    def test_negative_amounts_and_a_closed_period_are_refused(self):
        from apps.accounting import opening
        from apps.accounting.ledger import LedgerError
        from apps.accounting.models import LedgerLock
        with self.assertRaises(LedgerError):
            opening.post_opening(self.store, date=self.today, receivable=-5)
        LedgerLock.objects.create(store=self.store, locked_through=self.today)
        with self.assertRaises(LedgerError):
            opening.post_opening(self.store, date=self.today, banks={self.main: 1_000})

    def test_stock_in_the_opening_entry_ties_to_the_stock_value(self):
        from apps.accounting import inventory, opening
        valuation = inventory.stock_valuation(self.store)
        opening.post_opening(self.store, date=self.today,
            stock=(valuation["shelf_value"], valuation["mitra_value"]))
        self.assertEqual(inventory.reconciliation(self.store)["difference"], Decimal(0))

    def test_opening_receivable_is_flagged_as_not_itemised_and_comes_down_when_collected(self):
        from apps.accounting import opening
        from apps.tax.receivables import aging
        opening.post_opening(self.store, date=self.today, receivable=2_000_000)
        invoice = self.invoice()  # 222.000 itemised on top of the opening 2.000.000
        _, totals = aging(self.store, self.today)
        self.assertEqual(totals["total"], invoice.total)
        self.client.force_login(self.owner)
        page = self.client.get(reverse("merchant-receivables"))
        self.assertContains(page, "belum dirinci per invoice")
        self.assertEqual(page.context["unitemised"], Decimal(2_000_000))
        bank.record_cash_transaction(self.store, kind="receive", bank_account=self.main,
            date=self.today, lines=[(RECEIVABLE, "Pelunasan piutang awal Budi", 800_000)])
        self.assertEqual(balance(self.store, RECEIVABLE), Decimal(2_000_000 + 222_000 - 800_000))
        self.assertEqual(self.client.get(reverse("merchant-receivables")).context["unitemised"],
            Decimal(1_200_000))

    def test_page_posts_and_undoes(self):
        from apps.accounting.ledger import PAYABLE
        self.client.force_login(self.owner)
        page = self.client.get(reverse("merchant-opening-balances"))
        self.assertContains(page, "Saldo awal pembukuan")
        self.assertContains(page, f"bank_{self.main.pk}")
        response = self.client.post(reverse("merchant-opening-balances"), {
            "date": self.today.isoformat(), f"bank_{self.main.pk}": "5.000.000",
            "receivable": "1.250.000", "payable": "750.000", "loans": "", "include_stock": "1"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(balance(self.store, CASH), Decimal(5_000_000))
        self.assertEqual(balance(self.store, PAYABLE), Decimal(750_000))
        self.assertContains(self.client.get(reverse("merchant-opening-balances")),
            "Saldo awal sudah dicatat")
        self.client.post(reverse("merchant-opening-balances"), {"action": "undo"})
        self.assertEqual(balance(self.store, CASH), Decimal(0))

    def test_payables_page_flags_the_not_itemised_part(self):
        from apps.accounting import opening
        opening.post_opening(self.store, date=self.today, payable=900_000)
        self.client.force_login(self.owner)
        page = self.client.get(reverse("merchant-payables"))
        self.assertContains(page, "belum dirinci per tagihan")
        self.assertEqual(page.context["unitemised"], Decimal(900_000))

    def test_viewer_cannot_post(self):
        from apps.shops.models import StoreStaff
        viewer = User.objects.create_user("op-viewer", password="x")
        StoreStaff.objects.create(store=self.store, user=viewer, role="viewer")
        self.client.force_login(viewer)
        self.assertEqual(self.client.get(reverse("merchant-opening-balances")).status_code, 200)
        self.assertEqual(self.client.post(reverse("merchant-opening-balances"),
            {"date": self.today.isoformat(), "receivable": "1000"}).status_code, 404)


class AddBrandTests(Base):
    def setUp(self):
        super().setUp()
        from apps.billing.models import Plan
        from apps.shops.models import Organization
        self.org = Organization.objects.create(name="Grup", slug="grup", owner=self.owner)
        self.store.organization = self.org
        self.store.save()
        Plan.objects.get_or_create(code="free", defaults={"name": "Gratis", "is_default": True})
        self.client.force_login(self.owner)

    def test_owner_adds_a_brand_in_the_same_organization_and_switches_to_it(self):
        page = self.client.get(reverse("merchant-brand-new"))
        self.assertContains(page, "Tambah brand")
        response = self.client.post(reverse("merchant-brand-new"), {"store_name": "Brand Dua",
            "slug": "brand-dua", "phone": "081234567890", "instagram": "", "accept_terms": "on"})
        self.assertEqual(response.status_code, 302)
        brand = Store.objects.get(slug="brand-dua")
        self.assertEqual((brand.owner, brand.organization), (self.owner, self.org))
        self.assertEqual(self.client.session["merchant_store_id"], brand.id)
        self.assertEqual(self.client.get(reverse("merchant-dashboard")).status_code, 200)
        # the switcher now offers both, and each has its own books
        switcher = self.client.get(reverse("merchant-dashboard")).content.decode()
        self.assertIn("Toko Sales", switcher)
        self.assertIn("Brand Dua", switcher)
        main = bank.bank_accounts(brand)[0]
        bank.record_cash_transaction(brand, kind="receive", bank_account=main, date=self.today,
            lines=[("3100", "Modal", 500_000)])
        self.assertEqual(balance(brand, CASH), Decimal(500_000))
        self.assertEqual(balance(self.store, CASH), Decimal(0))  # each brand keeps its own books

    def test_taken_or_reserved_address_and_staff_are_refused(self):
        taken = self.client.post(reverse("merchant-brand-new"), {"store_name": "X", "slug": "toko-sales",
            "phone": "081234567890", "accept_terms": "on"})
        self.assertEqual(taken.status_code, 200)
        self.assertContains(taken, "sudah dipakai")
        staff = User.objects.create_user("brand-staff", password="x")
        from apps.shops.models import StoreStaff
        StoreStaff.objects.create(store=self.store, user=staff, role="admin")
        self.client.force_login(staff)
        self.assertEqual(self.client.get(reverse("merchant-brand-new")).status_code, 404)

    def test_menu_shows_the_link_to_owners_only(self):
        self.assertContains(self.client.get(reverse("merchant-dashboard")), "Tambah brand")


class BooksChecksTests(Base):
    def run_checks(self):
        from apps.accounting import controls
        return {c.key: c for c in controls.run_checks(self.store, self.today)}

    def test_a_clean_store_passes_everything_that_can_be_checked(self):
        checks = self.run_checks()
        self.assertEqual(checks["trial_balance"].status, "ok")
        self.assertEqual(checks["unposted"].status, "ok")
        self.assertEqual(checks["receivable"].status, "ok")
        self.assertEqual(checks["payable"].status, "ok")
        self.assertEqual(checks["customer_credit"].status, "ok")
        self.assertEqual(checks["negative_inventory"].status, "ok")
        self.assertNotIn("error", {c.status for c in checks.values()})

    def test_normal_activity_keeps_the_books_tied_to_the_documents(self):
        from apps.accounting import inventory
        inventory.adjust_books(self.store, date=self.today)         # opening stock
        invoice = self.invoice(kabel=3)
        record_payment(invoice, Decimal(100_000), self.today, "transfer")
        credit_notes.create_credit_note(invoice, lines=self.lines(invoice), issued_on=self.today)
        receipts.receive_payment(self.store, customer=self.customer, buyer_name="Budi",
            date=self.today, amount=400_000, method="transfer", allocations=[])
        checks = self.run_checks()
        for key in ("trial_balance", "unposted", "invoice_journal", "receivable", "inventory",
                "customer_credit", "negative_inventory"):
            self.assertEqual(checks[key].status, "ok", f"{key}: {checks[key].summary}")

    def test_it_catches_stock_that_left_without_the_books(self):
        from apps.accounting.ledger import INVENTORY
        self.invoice(kabel=2)  # HPP booked, but no opening stock in the books: Persediaan is minus
        checks = self.run_checks()
        self.assertEqual(checks["negative_inventory"].status, "fail")
        self.assertEqual(checks["inventory"].status, "warn")
        self.assertEqual(balance(self.store, INVENTORY), Decimal(-120_000))

    def test_it_catches_a_document_that_never_reached_the_ledger(self):
        from apps.accounting.models import JournalEntry
        invoice = self.invoice()
        JournalEntry.objects.filter(source_type="invoice", source_id=str(invoice.pk)).delete()
        self.assertEqual(self.run_checks()["unposted"].status, "warn")

    def test_it_catches_an_invoice_changed_after_it_was_posted(self):
        invoice = self.invoice()
        Invoice.objects.filter(pk=invoice.pk).update(total=Decimal(999_999))
        self.assertEqual(self.run_checks()["invoice_journal"].status, "fail")

    def test_it_catches_a_drifting_receivable_and_explains_an_opening_one(self):
        from apps.accounting import opening
        from apps.accounting.ledger import post_journal
        post_journal(self.store, date=self.today, memo="Koreksi", lines=[(RECEIVABLE, 50_000, 0),
            ("3100", 0, 50_000)])
        self.assertEqual(self.run_checks()["receivable"].status, "warn")
        other = Store.objects.create(owner=self.owner, name="Brand B", slug="brand-b")
        opening.post_opening(other, date=self.today, receivable=700_000)
        from apps.accounting import controls
        found = {c.key: c for c in controls.run_checks(other, self.today)}
        self.assertEqual(found["receivable"].status, "info")

    def test_bank_account_ties_to_the_statement(self):
        from apps.accounting import bank as bank_service
        bank_service.record_cash_transaction(self.store, kind="receive", bank_account=self.main,
            date=self.today, lines=[("3100", "Modal", 300_000)])
        key = f"bank_{self.main.pk}"
        self.assertEqual(self.run_checks()[key].status, "info")        # no statement yet
        rows, _ = bank_service.parse_statement(
            f"Tanggal,Keterangan,Debit,Kredit,Saldo\n{self.today:%d/%m/%Y},Setoran,,300.000,300.000\n")
        bank_service.import_statement(self.main, rows)
        checks = self.run_checks()
        self.assertEqual(checks[key].status, "warn")                    # one line to match
        line = self.main.statement_lines.get()
        bank_service.match_line(line, bank_service.suggestions(line)[0])
        self.assertEqual(self.run_checks()[key].status, "ok")

    def test_a_broken_check_reports_error_instead_of_failing_the_page(self):
        from unittest import mock

        from apps.accounting import controls
        with mock.patch.object(controls, "CHECKS", [controls.trial_balance,
                lambda store, today, ledger: 1 / 0]):
            results = controls.run_checks(self.store, self.today)
        self.assertEqual([r.status for r in results], ["ok", "error"])

    def test_page_lists_the_checks_and_posts_the_backlog(self):
        self.client.force_login(self.owner)
        page = self.client.get(reverse("merchant-books-checks"))
        self.assertContains(page, "Pemeriksaan pembukuan")
        self.assertContains(page, "Debit sama dengan kredit")
        self.assertContains(page, "Piutang usaha sama dengan invoice terbuka")
        response = self.client.post(reverse("merchant-books-checks"))
        self.assertRedirects(response, reverse("merchant-books-checks"))

    def test_other_stores_are_not_mixed_in(self):
        other = Store.objects.create(owner=self.owner, name="Brand C", slug="brand-c")
        self.invoice()
        from apps.accounting import controls
        found = {c.key: c for c in controls.run_checks(other, self.today)}
        self.assertEqual(found["receivable"].book, Decimal(0))


class SalesExportTests(Base):
    def make(self, channel, kabel=1, lampu=0, days_ago=0, credit=False):
        issued = self.today - timedelta(days=days_ago)
        lines = [{"product": self.kabel, "qty": kabel, "unit_price": Decimal(100_000)}]
        if lampu:
            lines.append({"product": self.lampu, "qty": lampu, "unit_price": Decimal(50_000)})
        return create_sales_invoice(self.store, lines=lines, buyer_name=f"Pembeli {channel}",
            issued_on=issued, channel=channel, on_credit=credit,
            due_date=issued + timedelta(days=14))

    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)
        self.shopee = self.make("shopee", kabel=2, lampu=1)
        self.toko = self.make("tokopedia", kabel=1, credit=True)
        record_payment(self.toko, Decimal(50_000), self.today, "transfer", reference="TRF-9")
        credit_notes.create_credit_note(self.toko, lines=self.lines(self.toko), issued_on=self.today)

    def export(self, **params):
        query = {"period": "this_month", **params}
        return self.client.get(reverse("merchant-sales-export"), query)

    def rows(self, dataset, **params):
        import csv
        import io
        response = self.export(dataset=dataset, format="csv", **params)
        self.assertEqual(response.status_code, 200)
        text = b"".join(response.streaming_content).decode()
        self.assertTrue(text.startswith("﻿"))
        return list(csv.reader(io.StringIO(text.lstrip("﻿"))))

    def test_lines_have_every_item_with_cost_and_margin(self):
        rows = self.rows("lines")
        header, body = rows[0], rows[1:]
        self.assertEqual(len(body), 3)  # kabel + lampu on shopee, kabel on tokopedia
        sku = header.index("SKU")
        qty, total = header.index("Qty"), header.index("Jumlah baris")
        hpp, margin = header.index("HPP baris"), header.index("Laba kotor baris")
        kabel = next(r for r in body if r[sku] == "KBL" and r[header.index("Channel")] == "Shopee")
        self.assertEqual((kabel[qty], Decimal(kabel[total]), Decimal(kabel[hpp]),
            Decimal(kabel[margin])), ("2", Decimal(200_000), Decimal(120_000), Decimal(80_000)))
        self.assertTrue(all(len(r) == len(header) for r in body))

    def test_orders_carry_invoice_payment_credit_and_what_is_still_owed(self):
        header, *body = self.rows("orders")
        by_channel = {r[header.index("Channel")]: r for r in body}
        toko = by_channel["Tokopedia"]
        value = lambda name: toko[header.index(name)]
        self.assertEqual(value("No. invoice"), self.toko.number)
        self.assertEqual(Decimal(value("Total invoice")), Decimal(111_000))
        self.assertEqual(Decimal(value("Nota kredit")), Decimal(111_000))
        self.assertEqual(Decimal(value("Dibayar")), Decimal(50_000))
        self.assertEqual(Decimal(value("Sisa tagihan")), Decimal(0))
        self.assertEqual(Decimal(value("PPN")), Decimal(11_000))
        shopee = by_channel["Shopee"]
        self.assertEqual(Decimal(shopee[header.index("HPP")]), Decimal(150_000))  # 2×60k + 30k
        self.assertTrue(all(len(r) == len(header) for r in body))

    def test_the_datasets_add_up_to_each_other(self):
        lines_header, *lines = self.rows("lines")
        orders_header, *orders = self.rows("orders")
        from_lines = sum(Decimal(r[lines_header.index("Jumlah baris")]) for r in lines)
        from_orders = sum(Decimal(r[orders_header.index("Subtotal")]) for r in orders)
        self.assertEqual(from_lines, from_orders)
        self.assertEqual(sum(int(r[lines_header.index("Qty")]) for r in lines),
            sum(int(r[orders_header.index("Jumlah barang")]) for r in orders))

    def test_payments_list_invoice_payments_with_their_reference(self):
        header, *body = self.rows("payments")
        refs = [r[header.index("Referensi")] for r in body]
        self.assertIn("TRF-9", refs)

    def test_channel_filter_and_only_sold(self):
        header, *body = self.rows("orders", channel="shopee")
        self.assertEqual([r[header.index("Channel")] for r in body], ["Shopee"])
        from apps.orders.models import Order
        Order.objects.filter(pk=self.shopee.order_id).update(status="cancelled")
        everything = self.rows("orders")[1:]
        sold = self.rows("orders", sold="1")[1:]
        self.assertEqual((len(everything), len(sold)), (2, 1))
        status = self.rows("orders")[0].index("Status pesanan")
        self.assertIn("Dibatalkan", {r[status] for r in everything})

    def test_excel_has_three_sheets_with_the_same_rows(self):
        import io

        from openpyxl import load_workbook
        response = self.export(format="xlsx")
        self.assertEqual(response.status_code, 200)
        book = load_workbook(io.BytesIO(response.content))
        self.assertEqual(book.sheetnames, ["Baris barang", "Per pesanan", "Pembayaran"])
        self.assertEqual(book["Baris barang"].max_row, 4)   # header + 3 lines
        self.assertEqual(book["Per pesanan"].max_row, 3)    # header + 2 orders
        self.assertEqual(book["Per pesanan"]["A1"].value, "Tanggal")

    def test_page_shows_counts_and_other_periods_are_excluded(self):
        page = self.export()
        self.assertContains(page, "Ekspor data penjualan lengkap")
        self.assertEqual(page.context["counts"]["orders"], 2)
        old = self.make("lazada", days_ago=400)
        self.assertEqual(self.export().context["counts"]["orders"], 2)
        self.assertEqual(self.export(period="all").context["counts"]["orders"], 3)
        self.assertIsNotNone(old)

    def test_other_stores_data_never_appears(self):
        other = Store.objects.create(owner=self.owner, name="Lain", slug="lain-x")
        self.client.get(reverse("merchant-sales-export"), {"store": other.id})
        _header, *body = self.rows("orders", store=other.id)
        self.assertEqual(body, [])

    def test_viewer_may_read_but_staff_without_access_may_not(self):
        from apps.shops.models import StoreStaff
        warehouse = User.objects.create_user("wh-user", password="x")
        StoreStaff.objects.create(store=self.store, user=warehouse, role="warehouse")
        self.client.force_login(warehouse)
        self.assertEqual(self.export().status_code, 404)


class PostEverythingTests(Base):
    def test_one_call_posts_payments_refunds_and_registered_documents(self):
        from apps.accounting.ledger import post_everything
        from apps.accounting.models import JournalEntry
        from apps.orders.models import Order, PaymentTransaction
        invoice = self.invoice()  # a registered document
        JournalEntry.objects.filter(source_type="invoice", source_id=str(invoice.pk)).delete()
        order = Order.objects.create(store=self.store, customer_name="Web", phone="-",
            shipping_address="-", subtotal=Decimal(50_000), total=Decimal(50_000), channel="web",
            status="paid", idempotency_key="pe-1")
        PaymentTransaction.objects.create(order=order, provider="doku", external_id="px-1",
            amount=Decimal(50_000), status="succeeded", reconciled=True)
        before = self.run_checks()["unposted"]
        self.assertEqual(before.status, "warn")
        # the invoice entry, the payment entry, and the invoice the paid web order never had
        self.assertEqual(post_everything(), 3)
        self.assertTrue(Invoice.objects.filter(order=order).exists())
        self.assertEqual(self.run_checks()["unposted"].status, "ok")
        self.assertEqual(post_everything(), 0)  # idempotent

    def run_checks(self):
        from apps.accounting import controls
        return {c.key: c for c in controls.run_checks(self.store, self.today)}

    def test_the_page_button_posts_payments_too(self):
        from apps.orders.models import Order, PaymentTransaction
        order = Order.objects.create(store=self.store, customer_name="Web", phone="-",
            shipping_address="-", subtotal=Decimal(70_000), total=Decimal(70_000), channel="web",
            status="paid", idempotency_key="pe-2")
        PaymentTransaction.objects.create(order=order, provider="doku", external_id="px-2",
            amount=Decimal(70_000), status="succeeded", reconciled=True)
        self.client.force_login(self.owner)
        self.client.post(reverse("merchant-books-checks"))
        self.assertEqual(self.run_checks()["unposted"].status, "ok")


class PaidElsewhereTests(Base):
    def test_invoice_of_a_gateway_paid_order_is_settled_and_leaves_the_receivable_list(self):
        from apps.accounting.ledger import post_everything
        from apps.orders.models import Order, PaymentTransaction
        from apps.tax.receivables import aging
        from apps.tax.services import issue_invoice_for_order
        order = Order.objects.create(store=self.store, customer_name="Web", phone="-",
            shipping_address="-", subtotal=Decimal(80_000), total=Decimal(80_000), channel="web",
            status="pending", idempotency_key="pd-1")
        invoice, _ = issue_invoice_for_order(order)  # issued while the order was still unpaid
        self.assertEqual(invoice.status, "issued")
        PaymentTransaction.objects.create(order=order, provider="doku", external_id="pd-x",
            amount=Decimal(80_000), status="succeeded", reconciled=True)
        self.assertEqual(aging(self.store, self.today)[1]["total"], Decimal(80_000))  # wrongly open
        from apps.accounting import controls
        found = {c.key: c for c in controls.run_checks(self.store, self.today)}
        self.assertEqual(found["invoice_status"].status, "warn")
        post_everything()
        self.assertEqual(Invoice.objects.get(pk=invoice.pk).status, "paid")
        self.assertEqual(aging(self.store, self.today)[1]["total"], Decimal(0))
        found = {c.key: c for c in controls.run_checks(self.store, self.today)}
        self.assertEqual(found["invoice_status"].status, "ok")
        self.assertEqual(found["receivable"].status, "ok")

    def test_a_manually_paid_invoice_is_left_alone(self):
        from apps.tax.posting import paid_elsewhere
        invoice = self.invoice()  # credit invoice, partly paid by hand
        record_payment(invoice, Decimal(10_000), self.today, "transfer")
        self.assertEqual(paid_elsewhere(self.store).count(), 0)


class BrandPerTabTests(Base):
    def setUp(self):
        super().setUp()
        self.brand = Store.objects.create(owner=self.owner, name="Brand Kedua", slug="brand-kedua")
        self.client.force_login(self.owner)

    def test_page_carries_its_brand_only_when_there_are_several(self):
        html = self.client.get(reverse("merchant-dashboard"), {"store": self.store.id}).content.decode()
        self.assertIn(f'data-store="{self.store.id}"', html)
        single = User.objects.create_user("solo", password="x")
        Store.objects.create(owner=single, name="Solo", slug="solo-s")
        self.client.force_login(single)
        self.assertNotIn("data-store=", self.client.get(reverse("merchant-dashboard")).content.decode())

    def test_each_tab_stays_on_its_brand_whatever_the_session_says(self):
        self.client.get(reverse("merchant-dashboard"), {"store": self.brand.id})  # tab B switches
        in_a = self.client.get(reverse("merchant-dashboard"), {"store": self.store.id})
        self.assertContains(in_a, f'data-store="{self.store.id}"')
        in_b = self.client.get(reverse("merchant-dashboard"), {"store": self.brand.id})
        self.assertContains(in_b, f'data-store="{self.brand.id}"')

    def test_a_form_posted_with_its_brand_books_into_that_brand_not_the_session_one(self):
        from apps.accounting.models import CashTransaction
        self.client.get(reverse("merchant-dashboard"), {"store": self.brand.id})  # session: brand
        response = self.client.post(reverse("merchant-cash-new"), {"store": self.store.id,
            "kind": "receive", "bank_account": self.main.id, "date": self.today.isoformat(),
            "account[]": ["3100", ""], "line_description[]": ["Modal", ""],
            "amount[]": ["250000", ""], "tax": ""})
        self.assertEqual(response.status_code, 302)
        txn = CashTransaction.objects.get()
        self.assertEqual(txn.store_id, self.store.id)
        self.assertEqual(balance(self.brand, CASH), Decimal(0))

    def test_a_brand_the_user_does_not_belong_to_is_never_selected(self):
        stranger = Store.objects.create(owner=User.objects.create_user("zz"), name="Asing", slug="asing")
        page = self.client.get(reverse("merchant-dashboard"), {"store": stranger.id})
        self.assertNotContains(page, f'data-store="{stranger.id}"')


class InvoiceFormTests(Base):
    """The one invoice form: new, edit and duplicate, free-text lines, line discounts, and adding
    a customer to the CRM from inside it."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)

    def post(self, url=None, **extra):
        data = {"channel": "manual", "issued_on": self.today.isoformat(), "buyer_name": "Budi",
            "phone": "0812", "address": "Bandung", "discount": "", "shipping": "",
            "payment_status": "credit", "due_date": (self.today + timedelta(days=30)).isoformat(),
            "product[]": [self.kabel.pk], "description[]": [""], "qty[]": ["2"],
            "unit_price[]": ["100000"], "line_discount[]": [""], **extra}
        return self.client.post(url or reverse("merchant-invoice-new"), data)

    # ── create ──
    def test_a_service_line_is_revenue_without_stock_or_cost(self):
        from apps.accounting.ledger import SALES
        response = self.post(**{"product[]": ["", ""], "description[]": ["Ongkos pasang", "Servis"],
            "qty[]": ["1", "2"], "unit_price[]": ["150000", "50000"], "line_discount[]": ["", ""]})
        self.assertEqual(response.status_code, 302)
        invoice = Invoice.objects.get()
        self.assertEqual([(i.description, i.product_id) for i in invoice.items.all()],
            [("Ongkos pasang", None), ("Servis", None)])
        self.assertEqual(invoice.subtotal, Decimal(250_000))
        self.assertEqual(invoice.total, Decimal(277_500))  # + 11% PPN
        self.assertEqual(balance(self.store, SALES), Decimal(250_000))
        self.assertEqual(balance(self.store, RECEIVABLE), Decimal(277_500))
        self.assertEqual(balance(self.store, COGS), Decimal(0))
        self.assertEqual(self.on_hand(self.kabel), 100)
        self.assertEqual(invoice.order.items.count(), 0)

    def test_goods_and_a_service_together_with_a_line_discount(self):
        self.post(**{"product[]": [self.kabel.pk, ""], "description[]": ["", "Antar"],
            "qty[]": ["2", "1"], "unit_price[]": ["100000", "20000"],
            "line_discount[]": ["10000", ""]})
        invoice = Invoice.objects.get()
        goods, service = invoice.items.all()
        self.assertEqual((goods.line_total, goods.discount), (Decimal(190_000), Decimal(10_000)))
        self.assertEqual(service.line_total, Decimal(20_000))
        self.assertEqual(invoice.subtotal, Decimal(210_000))
        self.assertEqual(self.on_hand(self.kabel), 98)
        self.assertEqual(balance(self.store, COGS), Decimal(120_000))
        self.assertEqual(invoice.order.subtotal, Decimal(210_000))

    def test_an_invalid_row_is_refused_and_the_form_keeps_what_was_typed(self):
        response = self.post(**{"qty[]": ["0"]})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "harus lebih dari nol")
        self.assertContains(response, "Budi")
        self.assertEqual(Invoice.objects.count(), 0)
        short = self.post(**{"qty[]": ["500"]})
        self.assertContains(short, "Stok tidak cukup")
        self.assertContains(short, "Kabel")  # the row is still on the page
        self.assertEqual(Invoice.objects.count(), 0)

    def test_discount_larger_than_the_line_and_empty_invoice_are_refused(self):
        self.assertEqual(self.post(**{"line_discount[]": ["999999"]}).status_code, 200)
        self.assertEqual(self.post(**{"product[]": [""], "description[]": [""]}).status_code, 200)
        self.assertEqual(Invoice.objects.count(), 0)

    def test_save_and_new_returns_to_an_empty_form(self):
        response = self.post(next="new")
        self.assertRedirects(response, reverse("merchant-invoice-new"))
        self.assertEqual(Invoice.objects.count(), 1)

    def test_prefilled_from_a_customer(self):
        self.customer.phone, self.customer.company_name = "0899", "PT Budi"
        self.customer.save()
        page = self.client.get(reverse("merchant-invoice-new"), {"customer": self.customer.id})
        self.assertContains(page, "PT Budi")
        self.assertContains(page, f'value="{self.customer.id}"')

    # ── customers ──
    def test_a_new_customer_is_added_to_the_crm_and_returned(self):
        response = self.client.post(reverse("merchant-customer-quick-add"), {"name": "Sari Dewi",
            "phone": "0856", "company_name": "CV Sari", "npwp": "0123456789012345",
            "address": "Jl. Mawar 1"})
        data = response.json()
        self.assertTrue(data["created"])
        customer = Customer.objects.get(pk=data["customer"]["id"])
        self.assertEqual((customer.store, customer.name, customer.company_name, customer.npwp),
            (self.store, "Sari Dewi", "CV Sari", "0123456789012345"))
        # and an invoice made with it is linked to the CRM record
        self.post(customer_id=customer.pk, buyer_name="Sari Dewi")
        self.assertEqual(Invoice.objects.get().order.customer, customer)

    def test_adding_the_same_phone_twice_reuses_the_customer(self):
        first = self.client.post(reverse("merchant-customer-quick-add"),
            {"name": "A", "phone": "0877"}).json()
        again = self.client.post(reverse("merchant-customer-quick-add"),
            {"name": "A lagi", "phone": "0877"}).json()
        self.assertFalse(again["created"])
        self.assertEqual(again["customer"]["id"], first["customer"]["id"])
        self.assertEqual(Customer.objects.filter(store=self.store, phone="0877").count(), 1)

    def test_customer_quick_add_validates_and_stays_in_its_store(self):
        self.assertEqual(self.client.post(reverse("merchant-customer-quick-add"), {"name": ""})
            .status_code, 400)
        bad = self.client.post(reverse("merchant-customer-quick-add"), {"name": "X", "npwp": "123"})
        self.assertEqual(bad.status_code, 400)
        self.assertIn("NPWP", bad.json()["error"])
        self.assertEqual(self.client.get(reverse("merchant-customer-quick-add")).status_code, 405)
        other = Store.objects.create(owner=self.owner, name="Lain", slug="lain-z")
        self.client.post(reverse("merchant-customer-quick-add"), {"name": "Di toko lain",
            "store": other.id})
        self.assertTrue(Customer.objects.filter(store=other, name="Di toko lain").exists())
        self.assertFalse(Customer.objects.filter(store=self.store, name="Di toko lain").exists())

    # ── edit ──
    def test_editing_the_header_changes_no_money_or_stock(self):
        invoice = self.invoice(kabel=2)
        before = (balance(self.store, RECEIVABLE), self.on_hand(self.kabel))
        self.post(reverse("merchant-invoice-edit", args=[invoice.id]), buyer_name="Budi Baru",
            notes="Catatan baru", reference="PO-9", **{"qty[]": ["2"]})
        invoice.refresh_from_db()
        self.assertEqual((invoice.buyer_name, invoice.notes, invoice.order.external_order_id),
            ("Budi Baru", "Catatan baru", "PO-9"))
        self.assertEqual((balance(self.store, RECEIVABLE), self.on_hand(self.kabel)), before)
        self.assertEqual(Invoice.objects.count(), 1)

    def test_changing_the_quantity_returns_old_stock_takes_new_and_rebooks_the_cost(self):
        from apps.accounting.ledger import INVENTORY
        invoice = self.invoice(kabel=2)  # 98 on the shelf, HPP 120.000
        response = self.post(reverse("merchant-invoice-edit", args=[invoice.id]), **{"qty[]": ["5"]})
        self.assertEqual(response.status_code, 302)
        invoice.refresh_from_db()
        self.assertEqual((invoice.subtotal, invoice.tax, invoice.total),
            (Decimal(500_000), Decimal(55_000), Decimal(555_000)))
        self.assertEqual(self.on_hand(self.kabel), 95)
        self.assertEqual(balance(self.store, COGS), Decimal(300_000))
        self.assertEqual(balance(self.store, INVENTORY), Decimal(-300_000))
        self.assertEqual(balance(self.store, RECEIVABLE), Decimal(555_000))
        self.assertEqual(invoice.number, Invoice.objects.get().number)  # same invoice and number
        # the order the invoice used to point at is kept, cancelled, and its stock came back
        from apps.orders.models import Order
        self.assertEqual(Order.objects.filter(store=self.store, status="cancelled").count(), 1)

    def test_an_edit_can_use_the_stock_the_old_version_held(self):
        invoice = self.invoice(kabel=60)
        # 40 left; editing to 100 only works because the 60 come back first
        self.assertEqual(self.post(reverse("merchant-invoice-edit", args=[invoice.id]),
            **{"qty[]": ["100"]}).status_code, 302)
        self.assertEqual(self.on_hand(self.kabel), 0)
        # and one more than exists is refused, leaving the invoice as it was
        again = self.post(reverse("merchant-invoice-edit", args=[invoice.id]), **{"qty[]": ["101"]})
        self.assertContains(again, "Stok tidak cukup")
        invoice.refresh_from_db()
        self.assertEqual((invoice.subtotal, self.on_hand(self.kabel)), (Decimal(10_000_000), 0))

    def test_editing_twice_and_voiding_afterwards_leaves_clean_books(self):
        from apps.accounting.ledger import INVENTORY
        invoice = self.invoice(kabel=2)
        url = reverse("merchant-invoice-edit", args=[invoice.id])
        self.post(url, **{"qty[]": ["4"]})
        self.post(url, **{"qty[]": ["1"]})
        self.assertEqual(self.on_hand(self.kabel), 99)
        void_invoice(Invoice.objects.get(pk=invoice.pk))
        self.assertEqual(self.on_hand(self.kabel), 100)
        for code in (RECEIVABLE, COGS, INVENTORY):
            self.assertEqual(balance(self.store, code), Decimal(0), code)

    def test_a_paid_in_full_cash_invoice_can_still_be_edited_and_stays_paid(self):
        invoice = self.invoice(kabel=2, credit=False)  # paid at once: an automatic payment exists
        self.assertEqual(invoice.status, "paid")
        self.post(reverse("merchant-invoice-edit", args=[invoice.id]), payment_status="paid",
            **{"qty[]": ["3"]})
        invoice.refresh_from_db()
        self.assertEqual((invoice.status, invoice.total, invoice.amount_due),
            ("paid", Decimal(333_000), Decimal(0)))
        self.assertEqual(balance(self.store, CASH), Decimal(333_000))
        self.assertEqual(balance(self.store, RECEIVABLE), Decimal(0))
        self.assertEqual(invoice.payments.count(), 1)

    def test_amounts_lock_once_paid_or_credited_but_the_header_stays_editable(self):
        invoice = self.invoice(kabel=2)
        record_payment(invoice, Decimal(50_000), self.today, "transfer")
        page = self.client.get(reverse("merchant-invoice-edit", args=[invoice.id]))
        self.assertContains(page, "Isi invoice terkunci")
        self.assertContains(page, "sudah ada pembayaran")
        self.post(reverse("merchant-invoice-edit", args=[invoice.id]), buyer_name="Ganti Nama",
            **{"qty[]": ["9"]})  # the quantity is ignored while locked
        invoice.refresh_from_db()
        self.assertEqual((invoice.buyer_name, invoice.total), ("Ganti Nama", Decimal(222_000)))
        self.assertEqual(self.on_hand(self.kabel), 98)

    def test_void_invoices_cannot_be_edited(self):
        invoice = self.invoice()
        void_invoice(invoice)
        response = self.client.get(reverse("merchant-invoice-edit", args=[invoice.id]))
        self.assertRedirects(response, reverse("merchant-invoice-detail", args=[invoice.id]))

    # ── duplicate ──
    def test_duplicate_opens_with_the_lines_copied_and_makes_a_new_invoice(self):
        invoice = self.invoice(kabel=2)
        page = self.client.get(reverse("merchant-invoice-duplicate", args=[invoice.id]))
        self.assertContains(page, "Duplikat invoice")
        self.assertContains(page, "Kabel")
        self.post(reverse("merchant-invoice-duplicate", args=[invoice.id]), **{"qty[]": ["2"]})
        self.assertEqual(Invoice.objects.count(), 2)
        self.assertEqual(self.on_hand(self.kabel), 96)

    # ── access ──
    def test_viewer_cannot_use_the_form(self):
        from apps.shops.models import StoreStaff
        viewer = User.objects.create_user("inv-viewer", password="x")
        StoreStaff.objects.create(store=self.store, user=viewer, role="viewer")
        self.client.force_login(viewer)
        self.assertEqual(self.client.get(reverse("merchant-invoice-new")).status_code, 404)
        self.assertEqual(self.client.post(reverse("merchant-customer-quick-add"),
            {"name": "X"}).status_code, 404)

    def test_finance_may_create_invoices(self):
        from apps.shops.models import StoreStaff
        finance = User.objects.create_user("inv-finance", password="x")
        StoreStaff.objects.create(store=self.store, user=finance, role="finance")
        self.client.force_login(finance)
        self.assertEqual(self.post().status_code, 302)

    def test_invoice_pages_show_service_lines(self):
        self.post(**{"product[]": [""], "description[]": ["Jasa instalasi"], "qty[]": ["1"],
            "unit_price[]": ["300000"], "line_discount[]": [""]})
        invoice = Invoice.objects.get()
        page = self.client.get(reverse("merchant-invoice-detail", args=[invoice.id]))
        self.assertContains(page, "Jasa instalasi")
        self.assertEqual(self.client.get(reverse("merchant-invoice-pdf", args=[invoice.id])).status_code, 200)
        credit_page = self.client.get(reverse("merchant-credit-note-new") + f"?invoice={invoice.id}")
        self.assertContains(credit_page, "Jasa instalasi")


class ExportWithServicesTests(Base):
    def test_service_lines_are_in_the_export_so_lines_equal_orders(self):
        import csv
        import io
        self.client.force_login(self.owner)
        self.client.post(reverse("merchant-invoice-new"), {"channel": "shopee",
            "issued_on": self.today.isoformat(), "buyer_name": "Budi", "phone": "08", "address": "x",
            "payment_status": "credit", "product[]": [self.kabel.pk, ""],
            "description[]": ["", "Ongkos pasang"], "qty[]": ["1", "1"],
            "unit_price[]": ["100000", "40000"], "line_discount[]": ["", ""]})

        def rows(dataset):
            response = self.client.get(reverse("merchant-sales-export"),
                {"period": "this_month", "dataset": dataset, "format": "csv"})
            return list(csv.reader(io.StringIO(b"".join(response.streaming_content).decode().lstrip("﻿"))))
        lines_header, *lines = rows("lines")
        orders_header, *orders = rows("orders")
        names = {r[lines_header.index("Nama barang")] for r in lines}
        self.assertEqual(names, {"Kabel", "Ongkos pasang"})
        self.assertEqual(sum(Decimal(r[lines_header.index("Jumlah baris")]) for r in lines),
            sum(Decimal(r[orders_header.index("Subtotal")]) for r in orders))


class FormDataTests(Base):
    def test_the_catalog_and_lines_reach_the_page_as_real_json_lists(self):
        import json
        import re
        self.client.force_login(self.owner)
        invoice = self.invoice(kabel=2)
        page = self.client.get(reverse("merchant-invoice-edit", args=[invoice.id])).content.decode()
        catalog = json.loads(re.search(r'id="catalog-data" type="application/json">(.*?)</script>',
            page, re.DOTALL).group(1))
        lines = json.loads(re.search(r'id="initial-lines" type="application/json">(.*?)</script>',
            page, re.DOTALL).group(1))
        self.assertIsInstance(catalog, list)   # not a string holding JSON
        self.assertEqual({p["sku"] for p in catalog}, {"KBL", "LMP"})
        self.assertEqual(catalog[0]["stock"], 98 if catalog[0]["sku"] == "KBL" else 100)
        self.assertEqual(lines[0]["qty"], "2")
        self.assertEqual(lines[0]["net"], "200000")


class InvoiceColumnTests(Base):
    def test_unit_note_code_round_trip_and_render(self):
        self.client.force_login(self.owner)
        r = self.client.post(reverse("merchant-invoice-new"), {
            "buyer_name": "Budi", "issued_on": "2026-11-10", "payment_status": "paid", "channel": "offline",
            "product[]": ["", ""], "description[]": ["Jasa pasang", ""], "qty[]": ["2", ""],
            "unit_price[]": ["50000", ""], "line_discount[]": ["", ""],
            "unit[]": ["jam", ""], "note[]": ["Lantai 2", ""], "code[]": ["JSP", ""],
        })
        self.assertEqual(r.status_code, 302, getattr(r, "content", b"")[:300])
        from apps.tax.models import Invoice
        inv = Invoice.objects.latest("id")
        item = inv.items.get()
        self.assertEqual((item.unit, item.note, item.sku), ("jam", "Lantai 2", "JSP"))
        html = self.client.get(reverse("merchant-invoice-detail", args=[inv.id])).content.decode()
        self.assertIn("Lantai 2", html)
        self.assertIn("2 jam", html)


class SidebarStaysOnTheRightItemTests(Base):
    """Sub pages keep their sidebar item highlighted and its group open."""

    def active(self, path):
        from django.test import RequestFactory

        from apps.orders.templatetags.merchant_nav import build_nav
        sections = build_nav({"request": RequestFactory().get(path), "merchant_role": "admin"})
        return [(g["label"], link["label"]) for s in sections for g in s["groups"]
                for link in g["links"] if link["active"] and g["open"]]

    def test_detail_and_form_pages_keep_their_menu_item(self):
        cases = {
            "/merchant/sales/invoices/abc/": "Invoice penjualan",
            "/merchant/sales/invoices/abc/duplicate/": "Invoice penjualan",
            "/merchant/orders/abc/": "Pesanan",
            "/merchant/sales/quotes/abc/": "Penawaran",
            "/merchant/products/3/partner-prices/": "Semua produk",
            "/merchant/shipping-areas/": "Pengiriman",
        }
        for path, label in cases.items():
            self.assertEqual([item[1] for item in self.active(path)], [label], path)
