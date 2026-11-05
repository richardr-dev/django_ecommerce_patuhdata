"""Gift card: store credit with a code. Redeemed at checkout as a discount on the products
(never below MIN_PAYABLE, so a payment page is always needed), and restored when the order is
cancelled or refunded."""
import secrets
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .models import GiftCard, GiftCardEntry

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
MIN_PAYABLE = Decimal(1000)


def normalize(code):
    return "".join(character for character in (code or "").upper() if character.isalnum())


def format_code(raw):
    return "-".join(raw[i:i + 4] for i in range(0, len(raw), 4))


def new_code(store):
    for _ in range(20):
        code = format_code("".join(secrets.choice(ALPHABET) for _ in range(16)))
        if not GiftCard.objects.filter(store=store, code=code).exists():
            return code
    raise RuntimeError("could not generate a unique gift card code")


def find(store, code, *, lock=False):
    """The usable gift card for this code, or None."""
    raw = normalize(code)
    if len(raw) != 16:
        return None
    cards = GiftCard.objects.select_for_update() if lock else GiftCard.objects
    card = cards.filter(store=store, code=format_code(raw), is_active=True, balance__gt=0).first()
    if card and card.expires_on and card.expires_on < timezone.localdate():
        return None
    return card


def usable_amount(card, products_after_discounts, shipping):
    """How much of the card an order can take: up to the products' price after other
    discounts, leaving at least MIN_PAYABLE to pay."""
    if card is None:
        return Decimal(0)
    room = min(products_after_discounts, products_after_discounts + shipping - MIN_PAYABLE)
    return max(Decimal(0), min(card.balance, room))


@transaction.atomic
def issue(store, value, *, actor=None, recipient_name="", recipient_email="", note="",
        expires_on=None):
    card = GiftCard.objects.create(store=store, code=new_code(store), initial_value=value,
        balance=value, recipient_name=recipient_name, recipient_email=recipient_email, note=note,
        expires_on=expires_on, created_by=actor)
    GiftCardEntry.objects.create(gift_card=card, kind="issue", amount=value, actor=actor)
    return card


def redeem(card, order, amount):
    """Inside the checkout transaction, with `card` locked."""
    if amount <= 0:
        return
    if amount > card.balance:
        raise ValueError("Saldo gift card tidak mencukupi.")
    card.balance -= amount
    card.save(update_fields=["balance"])
    GiftCardEntry.objects.create(gift_card=card, order=order, kind="redeem", amount=-amount)


def apply_order_gift_card(order, new_status):
    """Called by transition_order with the order locked: give the credit back once."""
    if new_status not in {"cancelled", "refunded"} or not order.gift_card_id or not order.gift_card_amount:
        return
    if GiftCardEntry.objects.filter(order=order, kind="restore").exists():
        return
    card = GiftCard.objects.select_for_update().get(pk=order.gift_card_id)
    card.balance += order.gift_card_amount
    card.save(update_fields=["balance"])
    GiftCardEntry.objects.create(gift_card=card, order=order, kind="restore",
        amount=order.gift_card_amount)
