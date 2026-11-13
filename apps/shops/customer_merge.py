"""Duplicate customers, found and merged into one.

The same buyer often exists twice: typed by hand once, imported from Shopee or Tokopedia once. A
merge keeps one record (the *primary*) and moves everything that pointed at the others onto it:
orders, receipts, customer credit, quotes, recurring invoices and broadcast history. Contact
fields the primary lacks are filled from the duplicates, tags are joined, and a marketplace
buyer link moves over. Invoices and the ledger follow through their order. The duplicates are
deleted; the merge is written to the audit log with what moved."""
import re
from collections import defaultdict

from django.db import transaction

from .models import AuditLog, Customer


class MergeError(ValueError):
    pass


def _digits(phone):
    digits = re.sub(r"\D", "", phone or "")
    for prefix in ("62", "0"):
        if digits.startswith(prefix):
            return digits[len(prefix):]
    return digits


def _norm(text):
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def duplicate_groups(store):
    """Groups of customers that look like one person: same phone (any format), same email, or
    same name and company. Each group is ``{"reason", "customers"}``, biggest buyer first."""
    customers = list(Customer.objects.filter(store=store).order_by("created_at"))
    keys = defaultdict(set)
    for customer in customers:
        phone = _digits(customer.phone)
        if len(phone) >= 7:
            keys[("Nomor HP sama", phone)].add(customer.pk)
        if customer.email:
            keys[("Email sama", _norm(customer.email))].add(customer.pk)
        if customer.name:
            keys[("Nama dan perusahaan sama", (_norm(customer.name), _norm(customer.company_name)))].add(customer.pk)
    by_id = {c.pk: c for c in customers}
    parent = {c.pk: c.pk for c in customers}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    reasons = defaultdict(set)
    for (reason, _value), ids in keys.items():
        if len(ids) < 2:
            continue
        ids = list(ids)
        for other in ids[1:]:
            parent[find(other)] = find(ids[0])
        for pk in ids:
            reasons[pk].add(reason)
    groups = defaultdict(list)
    for pk in parent:
        if pk in reasons:
            groups[find(pk)].append(by_id[pk])
    result = []
    for members in groups.values():
        if len(members) < 2:
            continue
        why = sorted({r for m in members for r in reasons.get(m.pk, ())})
        result.append({"reason": ", ".join(why), "customers": sorted(members,
            key=lambda c: (-c.orders.count(), c.created_at))})
    return sorted(result, key=lambda g: g["customers"][0].name.lower())



@transaction.atomic
def merge_customers(store, primary_id, duplicate_ids, *, actor=None):
    """Move the duplicates into the primary. Returns ``{"moved": {label: count}, "removed": n}``."""
    primary = Customer.objects.select_for_update().filter(store=store, pk=primary_id).first()
    if primary is None:
        raise MergeError("Pelanggan utama tidak ditemukan.")
    duplicates = list(Customer.objects.select_for_update().filter(store=store,
        pk__in=[d for d in duplicate_ids if str(d) != str(primary.pk)]))
    if not duplicates:
        raise MergeError("Pilih minimal satu pelanggan lain untuk digabung.")
    moved = defaultdict(int)
    for duplicate in duplicates:
        for relation in Customer._meta.get_fields(include_hidden=True):
            if not (relation.auto_created and not relation.concrete and relation.one_to_many):
                continue
            model, field = relation.related_model, relation.field.name
            count = model.objects.filter(**{field: duplicate}).update(**{field: primary})
            if count:
                moved[model._meta.verbose_name_plural.title()] += count
        for name in ("email", "phone", "company_name", "npwp", "address", "customer_code"):
            if not getattr(primary, name) and getattr(duplicate, name):
                # the duplicate is deleted below, so its unique phone / code is free to move
                setattr(primary, name, getattr(duplicate, name))
                setattr(duplicate, name, "")
                duplicate.save(update_fields=[name])
        if not primary.channel_buyer_id and duplicate.channel_buyer_id:
            buyer = duplicate.channel_buyer
            duplicate.channel_buyer = None
            duplicate.save(update_fields=["channel_buyer"])
            primary.channel_buyer = buyer
        tags = [t.strip() for t in f"{primary.tags},{duplicate.tags}".split(",") if t.strip()]
        primary.tags = ", ".join(dict.fromkeys(tags))[:300]
        primary.accepts_marketing = primary.accepts_marketing or duplicate.accepts_marketing
        primary.save()
    names = ", ".join(d.name for d in duplicates)
    for duplicate in duplicates:
        duplicate.delete()
    AuditLog.objects.create(store=store, actor=actor, action="customer.merged", object_type="Customer",
        object_id=str(primary.pk), details={"into": primary.name, "removed": names, "moved": dict(moved)})
    return {"moved": dict(moved), "removed": len(duplicates)}
