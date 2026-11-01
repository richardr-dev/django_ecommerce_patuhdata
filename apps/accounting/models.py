from django.db import models

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


class JournalEntry(models.Model):
    """A balanced double-entry posting. Each payment or refund posts at most once, so the worker
    can scan for unposted ones on every cycle without double counting."""
    store = models.ForeignKey(Store, on_delete=models.PROTECT, related_name="journal_entries")
    date = models.DateField()
    memo = models.CharField(max_length=300)
    payment = models.OneToOneField(PaymentTransaction, on_delete=models.PROTECT, null=True,
        blank=True, related_name="journal_entry")
    refund = models.OneToOneField(Refund, on_delete=models.PROTECT, null=True, blank=True,
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
