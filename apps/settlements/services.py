"""
Settlement service layer — all business logic for creating, transitioning,
and reconciling consignment settlements.

Rules enforced here (not in views):
- Lines are locked once status is confirmed.
- Status transitions follow Settlement.TRANSITIONS.
- Invoice number is gap-free per store-year, assigned at invoiced status.
- Payment cannot exceed total_due.
- Recalculate totals whenever lines change (before confirmed).
"""
from datetime import date as date_type
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.orders.models import Order
from apps.shops.models import AuditLog, Store

from .models import ConsignmentPartner, Settlement, SettlementLine, SettlementPayment


class SettlementError(ValueError):
    pass


# ── Partner ───────────────────────────────────────────────────────────────────

def get_or_create_partner(store: Store, name: str, **kwargs) -> ConsignmentPartner:
    partner, _ = ConsignmentPartner.objects.get_or_create(
        store=store, name=name, defaults=kwargs
    )
    return partner


# ── Settlement creation ───────────────────────────────────────────────────────

@transaction.atomic
def create_settlement(store: Store, partner: ConsignmentPartner,
                      period_start: date_type, period_end: date_type,
                      created_by=None, notes: str = "") -> Settlement:
    """Create a draft settlement for a partner/period.

    Does NOT auto-add lines — call add_sell_through_orders() after creation.
    """
    if period_end < period_start:
        raise SettlementError("Tanggal akhir periode tidak boleh sebelum tanggal mulai.")

    # Prevent overlapping non-closed settlements for same partner
    overlap = Settlement.objects.filter(
        store=store, partner=partner,
        period_start__lte=period_end, period_end__gte=period_start,
    ).exclude(status=Settlement.STATUS_CLOSED).exists()
    if overlap:
        raise SettlementError(
            f"Sudah ada settlement aktif untuk {partner.name} yang periodenya tumpang tindih."
        )

    settlement = Settlement.objects.create(
        store=store, partner=partner,
        period_start=period_start, period_end=period_end,
        notes=notes, created_by=created_by,
    )
    return settlement


@transaction.atomic
def add_sell_through_orders(settlement: Settlement, order_ids: list) -> int:
    """Add sell-through orders to a draft/revised settlement. Returns count added."""
    if settlement.is_locked:
        raise SettlementError("Settlement sudah dikonfirmasi. Lines tidak bisa diubah.")

    added = 0
    for order in Order.objects.filter(
        pk__in=order_ids,
        store=settlement.store,
        order_type__in=["reseller_sell_in", "reseller_sell_through"],
    ).prefetch_related("items"):
        # Skip orders already in another confirmed settlement
        if hasattr(order, "settlement_line") and order.settlement_line.settlement_id != settlement.id:
            existing = order.settlement_line.settlement
            if existing.is_locked:
                continue

        # Compute AB Shop total for this order
        ab_total = sum(
            (item.effective_ab_shop_price or item.unit_price) * item.quantity
            for item in order.items.all()
        )
        SettlementLine.objects.update_or_create(
            settlement=settlement, order=order,
            defaults={
                "description": f"{order.invoice_number or str(order.id)[:8]} · {order.customer_name}",
                "sale_date": order.created_at.date(),
                "ab_shop_source": order.ab_shop_source,
                "line_total": ab_total,
                "shipping_contribution": order.shipping_cost,
            },
        )
        added += 1

    _refresh_totals(settlement)
    return added


def _refresh_totals(settlement: Settlement):
    settlement.recalculate_totals()
    settlement.save(update_fields=["subtotal", "shipping_total", "total_due", "updated_at"])


# ── Status transitions ────────────────────────────────────────────────────────

@transaction.atomic
def transition(settlement: Settlement, new_status: str, actor=None,
               notes: str = "", dispute_reason: str = "") -> Settlement:
    """Move settlement to new_status, enforcing the transition rules."""
    if not settlement.can_transition_to(new_status):
        raise SettlementError(
            f"Tidak bisa mengubah status dari '{settlement.get_status_display()}' "
            f"ke '{dict(Settlement.STATUS_CHOICES).get(new_status, new_status)}'."
        )

    if new_status == Settlement.STATUS_DISPUTED and not dispute_reason:
        raise SettlementError("Alasan dispute wajib diisi.")

    now = timezone.now()
    update_fields = ["status", "updated_at"]

    settlement.status = new_status

    if new_status == Settlement.STATUS_DISPUTED:
        settlement.dispute_reason = dispute_reason
        update_fields.append("dispute_reason")

    if new_status == Settlement.STATUS_CONFIRMED:
        settlement.confirmed_at = now
        update_fields.append("confirmed_at")

    if new_status == Settlement.STATUS_INVOICED:
        settlement.invoice_number = _next_invoice_number(settlement.store)
        settlement.invoiced_at = now
        from datetime import timedelta
        settlement.due_date = now.date() + timedelta(days=settlement.partner.payment_term_days)
        update_fields += ["invoice_number", "invoiced_at", "due_date"]

    if new_status == Settlement.STATUS_CLOSED:
        settlement.closed_at = now
        update_fields.append("closed_at")

    if notes:
        settlement.internal_notes = (settlement.internal_notes + "\n" + notes).strip()
        update_fields.append("internal_notes")

    settlement.save(update_fields=update_fields)

    if actor:
        store = settlement.store
        AuditLog.objects.create(
            store=store, actor=actor,
            action="settlement.status_changed",
            object_type="Settlement", object_id=str(settlement.id),
            details={"to": new_status, "invoice": settlement.invoice_number},
        )

    return settlement


def _next_invoice_number(store) -> str:
    """Gap-free invoice number: SINV-YYYY-NNNNN per store."""
    from datetime import date
    year = date.today().year
    prefix = f"SINV-{year}-"
    last = (Settlement.objects.filter(store=store, invoice_number__startswith=prefix)
            .order_by("-invoice_number")
            .values_list("invoice_number", flat=True).first())
    seq = 1
    if last:
        try:
            seq = int(last.split("-")[-1]) + 1
        except (ValueError, IndexError):
            seq = 1
    return f"{prefix}{seq:05d}"


# ── Payments ──────────────────────────────────────────────────────────────────

@transaction.atomic
def record_payment(settlement: Settlement, amount: Decimal, payment_date: date_type,
                   method: str = "bank_transfer", reference: str = "",
                   notes: str = "", recorded_by=None) -> SettlementPayment:
    """Record a payment from the partner. Updates settlement.amount_paid and status."""
    if settlement.status not in (Settlement.STATUS_INVOICED, Settlement.STATUS_PARTIAL,
                                  Settlement.STATUS_PAID):
        raise SettlementError("Payment hanya bisa dicatat setelah settlement ditagih (invoiced).")

    if amount <= Decimal("0"):
        raise SettlementError("Jumlah payment harus lebih dari 0.")

    new_total_paid = settlement.amount_paid + amount
    if new_total_paid > settlement.total_due:
        raise SettlementError(
            f"Total payment (Rp{new_total_paid:,.0f}) melebihi total tagihan "
            f"(Rp{settlement.total_due:,.0f})."
        )

    payment = SettlementPayment.objects.create(
        settlement=settlement, amount=amount, payment_date=payment_date,
        method=method, reference=reference, notes=notes, recorded_by=recorded_by,
    )

    settlement.amount_paid = new_total_paid
    new_status = (Settlement.STATUS_PAID if new_total_paid >= settlement.total_due
                  else Settlement.STATUS_PARTIAL)
    settlement.status = new_status
    settlement.save(update_fields=["amount_paid", "status", "updated_at"])

    return payment
