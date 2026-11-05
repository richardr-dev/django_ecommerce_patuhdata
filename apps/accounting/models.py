from django.db import models
from django.utils.deconstruct import deconstructible

from apps.catalog.private_files import private_storage_callable
from apps.orders.models import PaymentTransaction, Refund
from apps.shops.models import Store


class Account(models.Model):
    TYPES = [("asset", "Aset"), ("liability", "Liabilitas"), ("equity", "Ekuitas"),
        ("revenue", "Pendapatan"), ("expense", "Beban")]
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="ledger_accounts")
    code = models.CharField(max_length=10)
    name = models.CharField(max_length=120)
    type = models.CharField(max_length=20, choices=TYPES)

    class Meta:
        ordering = ["code"]
        constraints = [models.UniqueConstraint(fields=["store", "code"],
            name="unique_store_account_code")]

    def __str__(self):
        return f"{self.code} {self.name}"


class Payout(models.Model):
    """Money a payment gateway or marketplace transferred to the seller's bank, settling the
    orders it lists. Each payout reference is recorded once per provider."""
    store = models.ForeignKey(Store, on_delete=models.PROTECT, related_name="payouts")
    provider = models.CharField(max_length=40)
    reference = models.CharField(max_length=120)
    paid_on = models.DateField()
    gross = models.DecimalField(max_digits=14, decimal_places=2)
    fees = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    net = models.DecimalField(max_digits=14, decimal_places=2)
    created_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-paid_on", "-id"]
        constraints = [models.UniqueConstraint(fields=["store", "provider", "reference"],
            name="unique_store_payout_reference")]

    def __str__(self):
        return f"{self.provider} {self.reference}"


class PayoutLine(models.Model):
    payout = models.ForeignKey(Payout, on_delete=models.CASCADE, related_name="lines")
    # A payment is settled by exactly one payout.
    payment = models.OneToOneField(PaymentTransaction, on_delete=models.PROTECT,
        related_name="payout_line")
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    fee = models.DecimalField(max_digits=14, decimal_places=2, default=0)


# Operating expenses the seller records by hand (Biaya operasional), one ledger account each.
EXPENSE_CATEGORIES = [
    ("6200", "Gaji & upah"),
    ("6300", "Sewa tempat"),
    ("6400", "Iklan & promosi"),
    ("6500", "Kemasan & perlengkapan"),
    ("6600", "Listrik, air & internet"),
    ("6700", "Transportasi & ongkos kirim"),
    ("6800", "Langganan aplikasi & software"),
    ("6900", "Biaya lain-lain"),
]


@deconstructible
class ExpenseReceiptPath:
    def __call__(self, instance, filename):
        import uuid

        safe = "".join(c for c in filename if c.isalnum() or c in "._- ")[:120] or "nota"
        return f"expenses/{instance.store.slug}/{uuid.uuid4().hex}/{safe}"


class Expense(models.Model):
    """An operating expense paid from the seller's bank (rent, salaries, ads...). Posted to the
    ledger when saved, so the profit and loss shows real net profit."""
    store = models.ForeignKey(Store, on_delete=models.PROTECT, related_name="expenses")
    date = models.DateField()
    category = models.CharField(max_length=10, choices=EXPENSE_CATEGORIES)
    description = models.CharField(max_length=200)
    vendor = models.CharField(max_length=120, blank=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2)  # what left the bank
    input_vat = models.DecimalField(max_digits=14, decimal_places=2, default=0)  # PPN masukan
    receipt = models.FileField(upload_to=ExpenseReceiptPath(), storage=private_storage_callable,
        blank=True)
    created_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-id"]
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="expense_positive"),
            models.CheckConstraint(condition=models.Q(input_vat__gte=0)
                & models.Q(input_vat__lt=models.F("amount")), name="expense_vat_within_amount"),
        ]

    def __str__(self):
        return f"{self.date} {self.description}"


class JournalEntry(models.Model):
    """A balanced double-entry posting. Each payment, refund or payout posts at most once, so
    the worker can scan for unposted ones on every cycle without double counting."""
    store = models.ForeignKey(Store, on_delete=models.PROTECT, related_name="journal_entries")
    date = models.DateField()
    memo = models.CharField(max_length=300)
    payment = models.OneToOneField(PaymentTransaction, on_delete=models.PROTECT, null=True,
        blank=True, related_name="journal_entry")
    refund = models.OneToOneField(Refund, on_delete=models.PROTECT, null=True, blank=True,
        related_name="journal_entry")
    payout = models.OneToOneField(Payout, on_delete=models.PROTECT, null=True, blank=True,
        related_name="journal_entry")
    expense = models.OneToOneField(Expense, on_delete=models.PROTECT, null=True, blank=True,
        related_name="journal_entry")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-id"]
        verbose_name_plural = "journal entries"


class JournalLine(models.Model):
    entry = models.ForeignKey(JournalEntry, on_delete=models.CASCADE, related_name="lines")
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="lines")
    debit = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    credit = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    class Meta:
        constraints = [models.CheckConstraint(name="journal_line_one_sided",
            condition=models.Q(debit__gt=0, credit=0) | models.Q(credit__gt=0, debit=0))]
