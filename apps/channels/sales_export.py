"""The full sales data of a store, for an accountant, auditor or analyst who wants the raw rows.

Three datasets over the same orders (a period, optionally one channel), each row carrying the keys
that tie it to the others (order number, marketplace order number, invoice number):

* ``lines``    one row per item sold: SKU, quantity, price, cost and gross profit.
* ``orders``   one row per order: amounts, tax, payment, invoice and what is still owed, credit
               notes, refunds, marketplace fees and the payout that paid it.
* ``payments`` money movements: gateway and marketplace payments, refunds, invoice payments.

Nothing is filtered by status unless asked, so the export is complete, and every row says its
status so it can be filtered in a spreadsheet."""
from decimal import Decimal

from django.utils import timezone

from apps.orders.models import Order, OrderItem, PaymentTransaction

from .models import ChannelOrderIncome

ZERO = Decimal(0)
NOT_SOLD = {"pending", "payment_failed", "cancelled", "expired"}

LINE_COLUMNS = ["Tanggal", "Jam", "Channel", "Tipe pesanan", "No. pesanan", "No. pesanan marketplace",
    "Status pesanan", "Status marketplace", "No. invoice", "Tanggal invoice", "Status invoice",
    "Pelanggan", "No. HP", "SKU", "Nama barang", "Varian", "SKU bundle", "Qty", "Harga satuan",
    "Jumlah baris", "HPP satuan", "HPP baris", "Laba kotor baris", "Harga mitra", "Pengiriman",
    "No. resi"]
ORDER_COLUMNS = ["Tanggal", "Jam", "Channel", "Tipe pesanan", "No. pesanan", "No. pesanan marketplace",
    "Status pesanan", "Status marketplace", "Pelanggan", "No. HP", "Alamat", "Jumlah barang",
    "Subtotal", "Diskon", "Ongkir", "PPN", "Total", "Penjualan bersih", "Metode bayar",
    "Penyedia pembayaran", "Refund", "No. invoice", "Tanggal invoice", "Jatuh tempo",
    "Status invoice", "Total invoice", "Nota kredit", "Dibayar", "Sisa tagihan", "HPP",
    "Komisi", "Biaya layanan", "Biaya admin", "Ongkir marketplace", "Voucher", "Afiliasi",
    "Biaya lain", "Total biaya marketplace", "Dana cair bersih", "Tanggal cair", "Ref. pencairan",
    "Margin setelah biaya", "Pengiriman", "No. resi"]
PAYMENT_COLUMNS = ["Tanggal", "Jenis", "Penyedia / metode", "No. pesanan", "No. pesanan marketplace",
    "Channel", "No. invoice", "Referensi", "Status", "Jumlah", "Cocok dengan pesanan",
    "Ref. pencairan"]
DATASETS = {"lines": ("Baris barang", LINE_COLUMNS), "orders": ("Per pesanan", ORDER_COLUMNS),
    "payments": ("Pembayaran", PAYMENT_COLUMNS)}


def orders_in(store, start, end, *, channel=None, only_sold=False):
    orders = Order.objects.filter(store=store, created_at__date__range=(start, end)).exclude(order_type="opening")
    if channel:
        orders = orders.filter(channel=channel)
    if only_sold:
        orders = orders.exclude(status__in=NOT_SOLD)
    return orders


def _day(order):
    local = timezone.localtime(order.created_at)
    return local.date(), local.strftime("%H:%M")


def _invoice(order):
    return getattr(order, "invoice", None)


def _unit_cost(item):
    if item.unit_cost is not None:
        return item.unit_cost
    for holder in (item.variant, item.product):
        if holder is not None and holder.cost_price is not None:
            return holder.cost_price
    return ZERO


def _kind(order):
    return dict(Order.ORDER_TYPES).get(order.order_type, order.order_type)


def _shipping(order):
    return " ".join(part for part in (order.shipping_provider, order.shipping_service) if part)


def lines(orders):
    labels, status = dict(Order.CHANNELS), dict(Order._meta.get_field("status").choices)
    items = (OrderItem.objects.filter(order__in=orders).select_related("order", "order__invoice",
        "order__customer", "product", "variant").order_by("order__created_at", "order_id", "id"))
    for item in items.iterator(chunk_size=2000):
        order, invoice = item.order, _invoice(item.order)
        day, hour = _day(order)
        cost = _unit_cost(item)
        mitra = item.effective_ab_shop_price if order.order_type == "reseller_sell_through" else None
        yield [day, hour, labels.get(order.channel, order.channel), _kind(order),
            order.invoice_number, order.external_order_id, status.get(order.status, order.status),
            order.marketplace_status, invoice.number if invoice else "",
            invoice.issued_on if invoice else "",
            invoice.get_status_display() if invoice else "", order.customer_name, order.phone,
            item.sku, item.name, item.variant_name, item.bundle_sku, item.quantity, item.unit_price,
            item.line_total, cost, cost * item.quantity, item.line_total - cost * item.quantity,
            mitra if mitra is not None else "", _shipping(order), order.tracking_number]


def service_lines(orders):
    """Lines that are not goods (services, fees): they are on the invoice and in the order total,
    but have no order item, so they are listed here to keep the lines equal to the orders."""
    from apps.tax.models import InvoiceItem
    labels, status = dict(Order.CHANNELS), dict(Order._meta.get_field("status").choices)
    items = (InvoiceItem.objects.filter(invoice__order__in=orders, product__isnull=True)
        .select_related("invoice__order").order_by("invoice__order__created_at", "invoice_id", "id"))
    for item in items.iterator(chunk_size=2000):
        invoice = item.invoice
        order = invoice.order
        day, hour = _day(order)
        yield [day, hour, labels.get(order.channel, order.channel), _kind(order),
            order.invoice_number, order.external_order_id, status.get(order.status, order.status),
            order.marketplace_status, invoice.number, invoice.issued_on,
            invoice.get_status_display(), order.customer_name, order.phone, "", item.description,
            "Jasa / biaya", "", item.quantity, item.unit_price, item.line_total, ZERO, ZERO,
            item.line_total, "", _shipping(order), order.tracking_number]


def goods_and_services(orders):
    yield from lines(orders)
    yield from service_lines(orders)


def orders_rows(orders):
    labels, status = dict(Order.CHANNELS), dict(Order._meta.get_field("status").choices)
    queryset = (orders.select_related("invoice", "channel_order__income")
        .prefetch_related("items__product", "items__variant", "refunds", "invoice__payments",
            "invoice__credit_notes", "payment_transactions__payout_line__payout")
        .order_by("created_at", "id"))
    for order in queryset.iterator(chunk_size=500):
        invoice = _invoice(order)
        day, hour = _day(order)
        items = list(order.items.all())
        cost = sum((_unit_cost(i) * i.quantity for i in items), ZERO)
        refunds = sum((r.amount for r in order.refunds.all() if r.status == "succeeded"), ZERO)
        income = getattr(getattr(order, "channel_order", None), "income", None)
        fees = [getattr(income, name, ZERO) if income else ZERO
            for name in ChannelOrderIncome.FEE_FIELDS]
        total_fees = sum(fees, ZERO)
        payment = next((p for p in order.payment_transactions.all()
            if p.kind == "payment" and p.status == "succeeded"), None)
        net_sales = order.subtotal - order.discount
        yield [day, hour, labels.get(order.channel, order.channel), _kind(order),
            order.invoice_number, order.external_order_id, status.get(order.status, order.status),
            order.marketplace_status, order.customer_name, order.phone, order.shipping_address,
            sum(i.quantity for i in items), order.subtotal, order.discount, order.shipping_cost,
            order.tax, order.total, net_sales, order.payment_method,
            payment.provider if payment else "", refunds,
            invoice.number if invoice else "", invoice.issued_on if invoice else "",
            invoice.due_date or "" if invoice else "",
            invoice.get_status_display() if invoice else "", invoice.total if invoice else "",
            sum((n.total for n in invoice.credit_notes.all() if n.status != "void"), ZERO)
            if invoice else "", invoice.amount_paid if invoice else "",
            invoice.amount_due if invoice else "", cost, *fees, total_fees,
            income.net_released if income else "", income.released_on or "" if income else "",
            income.payout_reference if income else "",
            net_sales - cost - total_fees if order.status not in NOT_SOLD else "",
            _shipping(order), order.tracking_number]


def payment_rows(orders):
    labels = dict(Order.CHANNELS)
    transactions = (PaymentTransaction.objects.filter(order__in=orders).select_related("order",
        "order__invoice", "payout_line__payout").order_by("created_at", "id"))
    for tx in transactions.iterator(chunk_size=2000):
        order, invoice = tx.order, _invoice(tx.order)
        payout = getattr(tx, "payout_line", None)
        yield [timezone.localtime(tx.updated_at).date(),
            "Refund" if tx.kind == "refund" else "Pembayaran", tx.provider, order.invoice_number,
            order.external_order_id, labels.get(order.channel, order.channel),
            invoice.number if invoice else "", tx.external_id, tx.get_status_display(), tx.amount,
            "Ya" if tx.reconciled else "Tidak", payout.payout.reference if payout else ""]
    from apps.tax.models import InvoicePayment
    manual = (InvoicePayment.objects.filter(invoice__order__in=orders)
        .select_related("invoice__order").order_by("paid_on", "id"))
    for pay in manual.iterator(chunk_size=2000):
        order = pay.invoice.order
        yield [pay.paid_on, "Pembayaran invoice", pay.get_method_display(), order.invoice_number,
            order.external_order_id, labels.get(order.channel, order.channel), pay.invoice.number,
            pay.reference, "Berhasil", pay.amount, "Ya", ""]


ROWS = {"lines": goods_and_services, "orders": orders_rows, "payments": payment_rows}


def rows(dataset, orders):
    return ROWS[dataset](orders)


def counts(orders):
    from apps.tax.models import InvoicePayment
    return {"orders": orders.count(), "lines": OrderItem.objects.filter(order__in=orders).count(),
        "payments": PaymentTransaction.objects.filter(order__in=orders).count()
            + InvoicePayment.objects.filter(invoice__order__in=orders).count()}
