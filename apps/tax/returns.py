"""Settling a customer return: money and stock in one step.

A return request walks Diminta → Disetujui → Barang diterima → Diperiksa. The inspection records
the condition. Settling it then does everything that follows, in one transaction:

* Retur & refund: a credit note on the order's invoice (issued first when the order has none).
  The note books the sale reversal (Retur penjualan, PPN, Piutang usaha / kas / kredit
  pelanggan). Goods in good condition go back on the shelf and to Persediaan at the cost they
  left at; damaged goods stay out, so their cost remains in HPP as the loss.
* Tukar ukuran/barang: the replacement variant leaves the shelf and, when the returned unit is
  fine, the original comes back. A damaged original is a loss: HPP is booked against Persediaan so
  the books still equal the shelf.

Every stock change is an InventoryMovement with ``source=("credit_note"|"return", id)``."""
import logging
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.accounting.ledger import COGS, INVENTORY, LedgerError, money, post_entry
from apps.orders.stock import StockError, stock_in, stock_out

from .credit_notes import CreditNoteError, create_credit_note, credited_quantity
from .models import CreditNote, Invoice

logger = logging.getLogger(__name__)


class ReturnError(ValueError):
    pass


def _lock(ret):
    from apps.orders.models import ReturnRequest
    return ReturnRequest.objects.select_for_update().select_related("order", "order_item",
        "order_item__product", "original_variant", "replacement_variant").get(pk=ret.pk)


def return_invoice(ret):
    """The invoice the return is credited against: issued now when the order has none yet."""
    from .services import issue_invoice_for_order
    invoice = Invoice.objects.filter(order=ret.order).exclude(
        status__in=[Invoice.STATUS_VOID, Invoice.STATUS_DRAFT]).first()
    if invoice is None:
        invoice, _ = issue_invoice_for_order(ret.order)
    return invoice


def returnable_lines(ret):
    """Order lines with the units that can still come back, for the settlement form."""
    rows = []
    invoice = Invoice.objects.filter(order=ret.order).exclude(status=Invoice.STATUS_VOID).first()
    for line in ret.order.items.select_related("product", "variant"):
        credited = 0
        if invoice:
            for item in invoice.items.filter(product_id=line.product_id):
                credited += credited_quantity(item)
        left = max(line.quantity - int(credited), 0)
        suggested = min(left, ret.quantity or left) if (ret.order_item_id == line.pk) else 0
        rows.append({"line": line, "left": left, "suggested": suggested})
    return rows


@transaction.atomic
def settle_refund(ret, *, quantities, issued_on, excess_action="credit", bank_account=None,
        actor=None):
    """``quantities`` maps OrderItem id → units returned. Raises ReturnError."""
    ret = _lock(ret)
    if ret.request_type != "return_refund" or ret.status != "inspected":
        raise ReturnError("Retur ini belum bisa diselesaikan: periksa barangnya dulu.")
    if ret.credit_note_id:
        raise ReturnError("Retur ini sudah diselesaikan.")
    if not ret.condition:
        raise ReturnError("Catat kondisi barang saat pemeriksaan dulu.")
    if excess_action == "refund" and bank_account is None:
        raise ReturnError("Pilih rekening asal uang refund.")
    good = ret.condition == "good"
    invoice = return_invoice(ret)
    lines = []
    for order_line in ret.order.items.select_related("product"):
        quantity = int(quantities.get(order_line.pk, 0) or 0)
        if quantity <= 0:
            continue
        if quantity > order_line.quantity:
            raise ReturnError(f"{order_line.name}: hanya {order_line.quantity} yang dibeli.")
        item = None
        for candidate in invoice.items.filter(product_id=order_line.product_id):
            if candidate.quantity - credited_quantity(candidate) >= quantity:
                item = candidate
                break
        if item is None:
            raise ReturnError(f"{order_line.name}: tidak ada sisa jumlah di invoice untuk "
                f"dikembalikan {quantity} unit.")
        lines.append({"item": item, "quantity": quantity, "restock": good,
            "unit_price": money(item.line_total / item.quantity)})
    if not lines:
        raise ReturnError("Isi jumlah barang yang dikembalikan.")
    try:
        note = create_credit_note(invoice, lines=lines, issued_on=issued_on, reason="return",
            notes=f"Retur #{ret.pk}", excess_action=excess_action, bank_account=bank_account,
            actor=actor)
    except CreditNoteError as error:
        raise ReturnError(str(error)) from error
    ret.credit_note, ret.status, ret.settled_at = note, "refunded", timezone.now()
    ret.save(update_fields=["credit_note", "status", "settled_at", "updated_at"])
    return note


@transaction.atomic
def settle_exchange(ret, *, replacement_variant, quantity=None, actor=None):
    """Send ``replacement_variant`` in exchange for the returned unit(s)."""
    ret = _lock(ret)
    if ret.request_type != "exchange" or ret.status != "inspected":
        raise ReturnError("Penukaran ini belum bisa dikirim: periksa barangnya dulu.")
    line = ret.order_item
    if line is None:
        raise ReturnError("Penukaran harus menunjuk satu barang di pesanan.")
    if not ret.condition:
        raise ReturnError("Catat kondisi barang saat pemeriksaan dulu.")
    original = ret.original_variant or line.variant
    if replacement_variant is None or replacement_variant.product_id != line.product_id \
            or not replacement_variant.is_active:
        raise ReturnError("Pilih varian pengganti dari produk yang sama.")
    if original and original.pk == replacement_variant.pk:
        raise ReturnError("Varian pengganti harus berbeda dari yang dikembalikan.")
    if original is None:
        raise ReturnError("Barang yang dikembalikan tidak punya varian; penukaran ukuran butuh "
            "varian asal.")
    quantity = int(quantity or line.quantity)
    if not 0 < quantity <= line.quantity:
        raise ReturnError(f"Jumlah penukaran 1 sampai {line.quantity}.")
    cost = line.unit_cost if line.unit_cost is not None else (line.product.cost_price or Decimal(0))
    source = ("return", ret.pk)
    try:
        stock_out(store=ret.order.store, product=line.product, variant=replacement_variant,
            quantity=quantity, kind="return", actor=actor, source=source, unit_cost=cost,
            reason=f"Barang pengganti retur #{ret.pk}")
        if ret.condition == "good":
            stock_in(store=ret.order.store, product=line.product, variant=original,
                quantity=quantity, kind="return", actor=actor, source=source, unit_cost=cost,
                reason=f"Barang tukar kembali, retur #{ret.pk}")
        elif cost:
            post_entry(ret.order.store, date=timezone.localdate(), actor=actor,
                source=("return_exchange", ret.pk),
                memo=f"Retur #{ret.pk}: barang rusak, pengganti dikirim",
                amounts={COGS: cost * quantity, INVENTORY: -cost * quantity})
    except (StockError, LedgerError) as error:
        raise ReturnError(str(error)) from error
    ret.replacement_variant, ret.original_variant = replacement_variant, original
    ret.status, ret.settled_at = "replacement_sent", timezone.now()
    ret.save(update_fields=["replacement_variant", "original_variant", "status", "settled_at",
        "updated_at"])
    return ret


def movements(ret):
    """The stock movements this return caused, for the detail page."""
    from django.db.models import Q

    from apps.orders.models import InventoryMovement
    query = Q(source_type="return", source_id=str(ret.pk))
    if ret.credit_note_id:
        query |= Q(source_type="credit_note", source_id=str(ret.credit_note_id))
    return InventoryMovement.objects.filter(query).select_related("variant", "inventory_level__product",
        "inventory_level__warehouse").order_by("created_at")
