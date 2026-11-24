"""Paket pajak marketplace: one month of marketplace selling, ready to hand to the accountant.

Per shop (a Shopee shop, a Tokopedia shop…) it shows two things, on the bases the books use:
- **Sales**, by invoice date: invoices, DPP, PPN keluaran, and the credit notes (retur) issued on them. This is
  what the SPT Masa PPN and the PPh Final turnover read.
- **Payouts**, by release date: what the marketplace released in the month, what it kept by kind of fee, and the
  PPh 22 it collected (PMK 37/2025). This is what the bank and the fee accounts show.

It reads the same invoices, credit notes and income rows as the invoice recap and the books, so its figures tie
to the ledger, and it lists what must be fixed before the month is handed over (``checks``)."""
from collections import defaultdict
from decimal import Decimal

from django.urls import reverse

from apps.tax.models import CreditNote, Invoice

from .invoice_recap import FEE_LABELS, LIVE
from .models import ChannelOrder, ChannelOrderIncome, SalesChannel, UnmappedSku

ZERO = Decimal(0)
MARKETPLACES = ("shopee", "tokopedia", "tiktok_shop", "lazada")
FEE_NAMES = [name for name, _label in FEE_LABELS]


def _blank(channel):
    return {"channel": channel, "invoices": 0, "dpp": ZERO, "vat": ZERO, "total": ZERO, "returns": ZERO,
        "returns_vat": ZERO, "return_count": 0, "released_count": 0, "gross_released": ZERO,
        "fees": {name: ZERO for name in FEE_NAMES}, "fees_total": ZERO, "pph22": ZERO, "net": ZERO,
        "awaiting_count": 0, "awaiting_value": ZERO}


def _invoices(store, start, end):
    return (Invoice.objects.filter(store=store, issued_on__range=(start, end), status__in=LIVE,
        order__channel_order__isnull=False).exclude(order__order_type="opening")
        .select_related("order__channel_order__channel", "order__channel_order__income"))


def _incomes(store, start, end):
    return (ChannelOrderIncome.objects.filter(channel_order__channel__store=store, released_on__range=(start, end))
        .exclude(payout_reference="").select_related("channel_order__channel", "channel_order__order"))


def build(store, start, end):
    """``{"shops": [...], "totals": {...}, "orders": [...], "checks": [...], "fee_labels": [...]}``."""
    shops = {}

    def shop(channel):
        if channel.pk not in shops:
            shops[channel.pk] = _blank(channel)
        return shops[channel.pk]

    invoices = list(_invoices(store, start, end))
    by_invoice = {}
    for invoice in invoices:
        row = shop(invoice.order.channel_order.channel)
        row["invoices"] += 1
        row["dpp"] += invoice.tax_base
        row["vat"] += invoice.tax
        row["total"] += invoice.total
        by_invoice[invoice.pk] = invoice
    notes = CreditNote.objects.filter(invoice__in=invoices).exclude(status=CreditNote.STATUS_VOID)
    returns_by_invoice = defaultdict(lambda: ZERO)
    for note in notes:
        row = shop(by_invoice[note.invoice_id].order.channel_order.channel)
        row["returns"] += note.total
        row["returns_vat"] += note.tax
        row["return_count"] += 1
        returns_by_invoice[note.invoice_id] += note.total

    for income in _incomes(store, start, end):
        row = shop(income.channel_order.channel)
        row["released_count"] += 1
        for name in FEE_NAMES:
            row["fees"][name] += getattr(income, name)
        row["fees_total"] += income.total_fees
        row["pph22"] += income.pph22
        row["net"] += income.net_released
        row["gross_released"] += income.net_released + income.total_fees + income.pph22

    # Sold in the month and not yet released by its last day: still Piutang marketplace (1300).
    for invoice in invoices:
        income = getattr(invoice.order.channel_order, "income", None)
        if income is None or not income.payout_reference or (income.released_on and income.released_on > end):
            row = shop(invoice.order.channel_order.channel)
            row["awaiting_count"] += 1
            row["awaiting_value"] += invoice.total

    table = sorted(shops.values(), key=lambda r: (r["channel"].platform, r["channel"].name))
    numbers = ("invoices", "dpp", "vat", "total", "returns", "returns_vat", "return_count", "released_count",
        "gross_released", "fees_total", "pph22", "net", "awaiting_count", "awaiting_value")
    totals = {key: sum((r[key] for r in table), ZERO) for key in numbers}
    totals["fees"] = {name: sum((r["fees"][name] for r in table), ZERO) for name in FEE_NAMES}
    for r in table + [totals]:
        r["net_sales"] = r["total"] - r["returns"]
        r["fee_rows"] = [(label, r["fees"][name]) for name, label in FEE_LABELS if r["fees"][name]]
    orders = [_order_row(invoice, returns_by_invoice[invoice.pk]) for invoice in
        sorted(invoices, key=lambda i: (i.issued_on, i.number))]
    return {"start": start, "end": end, "shops": table, "totals": totals, "orders": orders,
        "fee_labels": FEE_LABELS, "checks": checks(store, start, end, invoices)}


def _order_row(invoice, returned):
    channel_order = invoice.order.channel_order
    income = getattr(channel_order, "income", None)
    released = income if income and income.payout_reference else None
    return {"date": invoice.issued_on, "shop": channel_order.channel.name,
        "platform": channel_order.channel.get_platform_display(), "order": channel_order.external_order_id,
        "invoice": invoice, "buyer": invoice.buyer_name, "dpp": invoice.tax_base, "vat": invoice.tax,
        "total": invoice.total, "returned": returned,
        "fees": {name: getattr(released, name) if released else None for name in FEE_NAMES},
        "fees_total": released.total_fees if released else None, "pph22": released.pph22 if released else None,
        "net": released.net_released if released else None, "released_on": released.released_on if released else None,
        "payout": released.payout_reference if released else ""}


def checks(store, start, end, invoices):
    """What to fix before the month goes to the accountant: ``[(level, text, url)]``, worst first."""
    from apps.accounting.ledger import locked_through
    found = []
    missing = ChannelOrder.objects.filter(channel__store=store, order__invoice__isnull=True,
        order__created_at__date__range=(start, end)).exclude(
        order__status__in=["pending", "payment_failed", "cancelled", "expired"]).count()
    if missing:
        found.append(("bad", (f"{missing} pesanan marketplace bulan ini belum punya invoice. Jalankan pekerja buku "
            "(invoice terbit otomatis) atau periksa pesanan yang ditandai."), reverse("merchant-sales-invoices") + "?inv=none"))
    unreleased = [i for i in invoices if not getattr(getattr(i.order.channel_order, "income", None),
        "payout_reference", "")]
    if unreleased:
        found.append(("warn", (f"{len(unreleased)} invoice marketplace belum ada laporan penghasilannya: biaya, "
            "PPh 22, dan dana cair belum tercatat. Impor laporan penghasilan / settlement dari marketplace."),
            reverse("merchant-income-import")))
    unmapped = UnmappedSku.objects.filter(channel__store=store, resolved=False).count()
    if unmapped:
        found.append(("warn", (f"{unmapped} SKU marketplace belum dipetakan ke produk: stok dan HPP-nya tidak "
            "tercatat."), reverse("merchant-sku-map")))
    no_cost = {line.sku for invoice in invoices for line in invoice.order.items.all()
        if line.unit_cost is None and line.product.cost_price is None}
    if no_cost:
        found.append(("warn", (f"{len(no_cost)} produk terjual tanpa harga pokok: laba kotor bulan ini terlalu "
            "tinggi."), reverse("merchant-entity-list", args=["products"])))
    locked = locked_through(store)
    if not locked or locked < end:
        found.append(("info", ("Bulan ini belum ditutup. Setelah dicek, tutup buku agar angkanya tidak berubah "
            "lagi."), reverse("merchant-year-close")))
    return found


def has_marketplace(store):
    return SalesChannel.objects.filter(store=store).exists()
