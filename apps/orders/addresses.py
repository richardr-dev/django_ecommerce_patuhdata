from .models import CustomerAddress


def save_checkout_address(store, user, values, phone):
    """Remember the address used at checkout; reuse the entry when the same address recurs."""
    fields = {name: values.get(name, "").strip()[:200] for name in CustomerAddress.CHECKOUT_FIELDS}
    fields["phone"] = phone
    fields["shipping_address"] = values.get("shipping_address", "").strip()
    existing = CustomerAddress.objects.filter(store=store, user=user,
        postal_code=fields["postal_code"], shipping_address__iexact=fields["shipping_address"]).first()
    if existing:
        for name, value in fields.items():
            setattr(existing, name, value)
        existing.save()
        return existing
    first = not CustomerAddress.objects.filter(store=store, user=user).exists()
    return CustomerAddress.objects.create(store=store, user=user, is_default=first, **fields)


def saved_addresses(store, user):
    if not user.is_authenticated:
        return []
    return list(CustomerAddress.objects.filter(store=store, user=user))


def checkout_prefill(addresses, *, posted):
    """Values for the checkout form: the default address on first view, nothing after a POST.

    Every key is always present, because template filter arguments must resolve."""
    default = next((address for address in addresses if address.is_default), None)
    values = {name: "" for name in CustomerAddress.CHECKOUT_FIELDS} | {"id": None}
    if default and not posted:
        values |= {name: getattr(default, name) for name in CustomerAddress.CHECKOUT_FIELDS}
        values["id"] = default.id
    return values
