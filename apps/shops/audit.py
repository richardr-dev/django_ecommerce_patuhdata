"""The history of one document: who did what, and when. Read from the audit log, so the page
shows exactly what was recorded and nothing is added by hand."""
from .models import AuditLog

LABELS = {
    "stock_adjustment.posted": "Penyesuaian stok dibukukan", "stock_adjustment.voided": "Penyesuaian stok dibatalkan",
    "payroll.created": "Penggajian disusun", "payroll.updated": "Penggajian dihitung ulang",
    "payroll.posted": "Penggajian diposting", "payroll.paid": "Gaji dibayar", "payroll.bpjs_paid": "BPJS disetor",
    "payroll.voided": "Penggajian dibatalkan", "employee.created": "Karyawan ditambahkan",
    "employee.updated": "Data karyawan diubah", "bill.opening_created": "Tagihan saldo awal dicatat",
    "credit_hold.policy_updated": "Aturan tahan kredit diubah",
    "invoice.credit_hold_override": "Disetujui melewati tahan kredit",
    "invoice.created": "Invoice dibuat", "invoice.updated": "Invoice diubah",
    "invoice.paid": "Pembayaran dicatat", "invoice.voided": "Invoice dibatalkan",
    "invoice.sent": "Invoice dikirim", "bill.created": "Tagihan dibuat", "bill.paid": "Pembayaran tagihan",
    "bill.voided": "Tagihan dibatalkan", "bill.attachment": "Lampiran ditambahkan",
    "journal.created": "Jurnal dicatat", "journal.reversed": "Jurnal dibalik",
    "credit_note.created": "Nota kredit dibuat", "credit_note.voided": "Nota kredit dibatalkan",
    "quote.saved": "Penawaran disimpan", "quote.invoiced": "Penawaran dijadikan invoice",
    "expense.created": "Biaya dicatat", "expense.removed": "Biaya dihapus",
    "return.created": "Retur dicatat", "return.status_updated": "Status retur berubah",
    "return.settle_refund": "Retur diselesaikan (refund)", "return.settle_exchange": "Retur diselesaikan (tukar)",
    "cash.created": "Transaksi kas dicatat", "cash.voided": "Transaksi kas dibatalkan",
    "approval.requested": "Diajukan untuk persetujuan", "approval.approved": "Disetujui",
    "approval.rejected": "Ditolak", "attachment.added": "Lampiran ditambahkan",
    "attachment.removed": "Lampiran dihapus",
    "pos.shift_opened": "Shift kasir dibuka", "pos.shift_closed": "Shift kasir ditutup",
    "pos.sale_voided": "Struk kasir dibatalkan", "pos.refund": "Retur/refund kasir",
    "pos.paid_out": "Kas keluar kasir", "pos.paid_in": "Kas masuk kasir",
    "pos.shift_variance_approved": "Selisih kas shift disetujui",
}


def trail(store, obj, limit=40):
    """Audit entries for ``obj`` (a model instance), newest first."""
    rows = AuditLog.objects.filter(store=store, object_type=type(obj).__name__, object_id=str(obj.pk)
        ).select_related("actor").order_by("-created_at")[:limit]
    return [{"when": row.created_at, "who": row.actor.get_username() if row.actor else "Sistem",
        "what": LABELS.get(row.action, row.action), "details": row.details} for row in rows]


def log(store, actor, action, obj, **details):
    AuditLog.objects.create(store=store, actor=actor, action=action, object_type=type(obj).__name__,
        object_id=str(obj.pk), details={k: str(v) for k, v in details.items()})
