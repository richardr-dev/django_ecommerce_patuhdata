"""Bulan marketplace: one month of a marketplace shop as five steps a seller can follow without knowing the accounts.

1. Pesanan     the month's order file is imported (orders exist for the month)
2. Invoice     every order that should be a sale has its invoice (the books worker issues missing ones)
3. Penghasilan the income/settlement report is imported, so fees, PPh 22 and the money released are booked
4. Cek selisih SKUs mapped to products, products with a cost price, no order flagged for review
5. Tutup bulan the books are locked through the month's last day

Figures come from ``marketplace_tax.build`` (the same invoices, credit notes and income rows as the books), so
this page, Pajak marketplace and the ledger always agree. Reads only."""
from decimal import Decimal

from django.urls import reverse

from apps.accounting.ledger import locked_through

from . import marketplace_tax
from .models import ChannelOrder, ImportBatch, SalesChannel, UnmappedSku

ZERO = Decimal(0)
NOT_SALES = ["pending", "payment_failed", "cancelled", "expired"]


def _step(key, title, state, text, url="", action=""):
    return {"key": key, "title": title, "state": state, "text": text, "url": url, "action": action}


def shop_month(store, channel, start, end, row, locked):
    """The five steps for one shop. ``row`` is the shop's row from ``marketplace_tax.build`` (or None)."""
    orders = ChannelOrder.objects.filter(channel=channel, order__created_at__date__range=(start, end))
    order_count = orders.count()
    cancelled = orders.filter(order__status="cancelled").count()
    missing = orders.filter(order__invoice__isnull=True).exclude(order__status__in=NOT_SALES).count()
    flagged = orders.exclude(attention="").count()
    unmapped = UnmappedSku.objects.filter(channel=channel, resolved=False).count()
    last = ImportBatch.objects.filter(channel=channel).order_by("-imported_at").first()
    row = row or marketplace_tax._blank(channel)
    import_url = reverse("merchant-import") + f"?platform={channel.platform}"
    steps = [
        _step("orders", "Impor pesanan",
            "ok" if order_count else "todo",
            (f"{order_count} pesanan bulan ini" + (f" ({cancelled} dibatalkan)" if cancelled else "") +
             (f". Impor terakhir {last.imported_at:%d %b %Y}." if last else ".")) if order_count else
            "Belum ada pesanan bulan ini. Unduh laporan pesanan dari Seller Center, lalu impor.",
            import_url, "Impor pesanan"),
        _step("invoices", "Invoice penjualan",
            "todo" if not order_count else ("warn" if missing else "ok"),
            (f"{missing} pesanan belum punya invoice. Invoice terbit otomatis oleh pekerja buku; periksa pesanan "
             "yang ditandai bila tetap kosong.") if missing else
            (f"{row['invoices']} invoice, penjualan bersih Rp{row['net_sales']:,.0f}.".replace(",", ".")
             if order_count else "Menunggu pesanan."),
            reverse("merchant-sales-invoices") + "?inv=none" if missing else
            reverse("merchant-sales-invoices") + f"?channel={channel.platform}", "Lihat invoice"),
        _step("income", "Impor penghasilan",
            "todo" if not row["invoices"] else ("warn" if row["awaiting_count"] else "ok"),
            (f"{row['released_count']} pesanan sudah cair: Rp{row['net']:,.0f} masuk rekening, biaya "
             f"Rp{row['fees_total']:,.0f}, PPh 22 Rp{row['pph22']:,.0f}.").replace(",", ".") +
            (f" {row['awaiting_count']} invoice (Rp{row['awaiting_value']:,.0f}) belum ada laporan penghasilannya: "
             "masih piutang marketplace.".replace(",", ".") if row["awaiting_count"] else "")
            if row["invoices"] else "Setelah ada invoice, impor laporan penghasilan / settlement dari marketplace.",
            reverse("merchant-income-import"), "Impor penghasilan"),
        _step("check", "Cek selisih",
            "todo" if not order_count else ("warn" if unmapped or flagged else "ok"),
            " ".join(part for part in (
                f"{unmapped} SKU belum dipetakan ke produk (stok dan HPP tidak tercatat)." if unmapped else "",
                f"{flagged} pesanan ditandai untuk dicek." if flagged else "",
            ) if part) or ("Semua SKU cocok dan tidak ada pesanan yang ditandai." if order_count else "Menunggu pesanan."),
            reverse("merchant-sku-map") if unmapped else reverse("merchant-orders") + "?sumber=semua",
            "Petakan SKU" if unmapped else "Lihat pesanan"),
        _step("close", "Tutup bulan",
            "ok" if locked else ("todo" if not order_count else "info"),
            "Pembukuan bulan ini sudah dikunci." if locked else
            "Setelah semua langkah di atas beres, tutup buku agar angka bulan ini tidak berubah lagi.",
            reverse("merchant-year-close"), "Tutup buku"),
    ]
    done = sum(step["state"] == "ok" for step in steps)
    return {"channel": channel, "steps": steps, "done": done, "row": row,
        "complete": done == len(steps)}


def build(store, start, end):
    """``{"shops": [...], "pack": marketplace_tax.build(...)}`` for every active marketplace shop."""
    pack = marketplace_tax.build(store, start, end)
    rows = {row["channel"].pk: row for row in pack["shops"]}
    lock = locked_through(store)
    locked = bool(lock and lock >= end)
    shops = [shop_month(store, channel, start, end, rows.get(channel.pk), locked) for channel in
        SalesChannel.objects.filter(store=store, is_active=True).order_by("platform", "name")]
    return {"shops": shops, "pack": pack, "locked": locked}
