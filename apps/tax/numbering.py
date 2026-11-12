"""Gapless document numbers per store, kind and year: CN/2026/000001. Call inside the
transaction that creates the document, so a failed creation does not burn a number."""
from .models import DocumentSequence


def next_number(store, kind, on_date):
    sequence, _ = DocumentSequence.objects.get_or_create(store=store, kind=kind, year=on_date.year)
    sequence = DocumentSequence.objects.select_for_update().get(pk=sequence.pk)
    sequence.last_number += 1
    sequence.save(update_fields=["last_number"])
    return f"{kind}/{on_date.year}/{sequence.last_number:06d}"
