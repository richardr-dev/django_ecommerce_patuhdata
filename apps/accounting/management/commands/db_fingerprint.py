"""Row counts of every table and the trial balance of every store, as JSON.

Run it against the old and the new database (``DATABASE_URL=... manage.py db_fingerprint``) and
compare: a database move is correct when the two outputs are identical."""
import json
from decimal import Decimal

from django.apps import apps
from django.core.management.base import BaseCommand

from apps.accounting.ledger import account_balances
from apps.shops.models import Store


class Command(BaseCommand):
    help = "Print table row counts and per-store ledger balances as JSON (to verify a database copy)."

    def handle(self, *args, **options):
        counts = {model._meta.label: model._default_manager.count() for model in apps.get_models()
            if not model._meta.proxy and model._meta.app_label not in {"contenttypes", "sessions", "admin"}
            and model._meta.label != "auth.Permission"}
        books = {}
        for store in Store.objects.order_by("pk"):
            rows = {a.code: [str(a.total_debit), str(a.total_credit)] for a in account_balances(store)
                if a.total_debit or a.total_credit}
            books[store.slug] = rows
        total = sum((Decimal(d) - Decimal(c) for rows in books.values() for d, c in rows.values()), Decimal(0))
        self.stdout.write(json.dumps({"counts": counts, "books": books, "ledger_net": str(total)},
            sort_keys=True, indent=1))
