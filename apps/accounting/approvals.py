"""Maker and checker: above an amount, one person asks and a different person approves.

A store sets, per kind of action (paying a bill, an expense, a manual journal, voiding an invoice or
a bill), the amount from which a second person must approve. Below it nothing changes. Above it the
action is parked as a request; it runs only when someone else, with approval rights, accepts it, and
it runs as the person who asked, so the books and the audit log show the real maker. Nobody approves
their own request. A store with nobody else who could approve is never blocked."""
import datetime
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.shops.audit import log as audit_log
from apps.shops.permissions import roles

from .models import ApprovalPolicy, ApprovalRequest

APPROVER_ROLES = roles("approval.decide")


class ApprovalError(ValueError):
    pass


def approvers(store, *, exclude=None):
    """Users who could approve: the owner and staff with admin, manager or finance roles."""
    users = {store.owner}
    users |= {m.user for m in store.staff_members.filter(role__in=APPROVER_ROLES).select_related("user")}
    users.discard(exclude)
    return users


def required(store, kind, amount, requester):
    policy = ApprovalPolicy.objects.filter(store=store, kind=kind, enabled=True).first()
    if policy is None or Decimal(amount) < policy.threshold:
        return False
    return bool(approvers(store, exclude=requester))  # a one-person store is never locked out


def submit(store, kind, amount, summary, payload, requester, subject=None):
    request = ApprovalRequest.objects.create(store=store, kind=kind, amount=amount, summary=summary[:300],
        payload=payload, requester=requester)
    if subject is not None:
        audit_log(store, requester, "approval.requested", subject, kind=kind, amount=amount)
    return request


def can_decide(store, user, request):
    return request.status == ApprovalRequest.PENDING and user != request.requester and user in approvers(store)


# ── What each kind does once approved ─────────────────────────────────────────

def _bill_payment(store, p, actor):
    from .models import BankAccount, Bill
    from .payables import pay_bill
    bill = Bill.objects.get(pk=p["bill_id"], store=store)
    bank = BankAccount.objects.filter(pk=p.get("bank_account_id"), store=store).first() if p.get("bank_account_id") else None
    pay_bill(bill, amount=Decimal(p["amount"]), paid_on=datetime.date.fromisoformat(p["paid_on"]),
        method=p.get("method", "transfer"), reference=p.get("reference", ""), actor=actor, bank_account=bank,
        withholding=(p["pph_kind"], p.get("pph_rate")) if p.get("pph_kind") else None)
    audit_log(store, actor, "bill.paid", bill, amount=p["amount"], approved=True)


def _bill_void(store, p, actor):
    from .models import Bill
    from .payables import void_bill
    bill = Bill.objects.get(pk=p["bill_id"], store=store)
    void_bill(bill, actor=actor)
    audit_log(store, actor, "bill.voided", bill, total=bill.total, approved=True)


def _invoice_void(store, p, actor):
    from apps.tax.models import Invoice
    from apps.tax.posting import void_invoice
    invoice = Invoice.objects.get(pk=p["invoice_id"], store=store)
    void_invoice(invoice, actor=actor)
    audit_log(store, actor, "invoice.voided", invoice, total=invoice.total, approved=True)


def _journal(store, p, actor):
    from .ledger import post_journal
    from .models import CostCenter
    center = CostCenter.objects.filter(pk=p.get("cost_center_id"), store=store).first() if p.get("cost_center_id") else None
    entry = post_journal(store, date=datetime.date.fromisoformat(p["date"]), memo=p["memo"],
        lines=[(code, Decimal(d), Decimal(c)) for code, d, c in p["lines"]], actor=actor, cost_center=center)
    audit_log(store, actor, "journal.created", entry, memo=p["memo"], approved=True)


def _expense(store, p, actor):
    from .ledger import post_expense
    from .models import Expense
    expense = Expense.objects.select_for_update().get(pk=p["expense_id"], store=store)
    expense.approval_pending = False
    expense.save(update_fields=["approval_pending"])
    post_expense(expense)
    if p.get("pph_kind"):
        from . import withholding as pph
        pph.record(store, kind=p["pph_kind"], date=expense.date, base=expense.amount + expense.withheld
            - expense.input_vat, rate=p.get("pph_rate") or None, payee_name=expense.vendor or expense.description,
            source_type="expense", source_id=expense.pk, actor=actor)
    audit_log(store, actor, "expense.created", expense, amount=expense.amount, approved=True)


EXECUTORS = {"bill_payment": _bill_payment, "bill_void": _bill_void, "invoice_void": _invoice_void,
    "journal": _journal, "expense": _expense}


@transaction.atomic
def approve(request, user):
    request = ApprovalRequest.objects.select_for_update().get(pk=request.pk)
    if request.status != ApprovalRequest.PENDING:
        raise ApprovalError("Permintaan ini sudah diputuskan.")
    if user == request.requester:
        raise ApprovalError("Anda tidak bisa menyetujui permintaan Anda sendiri.")
    if user not in approvers(request.store):
        raise ApprovalError("Anda tidak punya hak menyetujui.")
    from .ledger import LedgerError
    from .payables import PayableError
    try:
        EXECUTORS[request.kind](request.store, request.payload, request.requester)
    except (LedgerError, PayableError, ValueError, KeyError) as error:
        raise ApprovalError(f"Tidak bisa dijalankan: {error}") from error
    request.status, request.decided_by, request.decided_at = ApprovalRequest.APPROVED, user, timezone.now()
    request.save(update_fields=["status", "decided_by", "decided_at"])
    audit_log(request.store, user, "approval.approved", request, kind=request.kind, amount=request.amount,
        requester=request.requester)
    return request


@transaction.atomic
def reject(request, user, note=""):
    request = ApprovalRequest.objects.select_for_update().get(pk=request.pk)
    if request.status != ApprovalRequest.PENDING:
        raise ApprovalError("Permintaan ini sudah diputuskan.")
    if user == request.requester:
        raise ApprovalError("Anda tidak bisa memutuskan permintaan Anda sendiri.")
    if user not in approvers(request.store):
        raise ApprovalError("Anda tidak punya hak memutuskan.")
    request.status, request.decided_by, request.decided_at = ApprovalRequest.REJECTED, user, timezone.now()
    request.decision_note = note[:300]
    request.save(update_fields=["status", "decided_by", "decided_at", "decision_note"])
    if request.kind == "expense":  # the parked expense never reaches the books
        from .models import Expense
        Expense.objects.filter(pk=request.payload.get("expense_id"), store=request.store,
            approval_pending=True).delete()
    audit_log(request.store, user, "approval.rejected", request, kind=request.kind, note=note)
    return request


def pending_count(store, user):
    if user not in approvers(store):
        return 0
    return ApprovalRequest.objects.filter(store=store, status=ApprovalRequest.PENDING).exclude(requester=user).count()
