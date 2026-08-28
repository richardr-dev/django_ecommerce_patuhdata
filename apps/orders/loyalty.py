from decimal import Decimal

from django.db import transaction

from .models import LoyaltyAccount, LoyaltyTransaction, Order


def loyalty_account(store, user, *, lock=False):
    queryset = LoyaltyAccount.objects
    if lock:
        queryset = queryset.select_for_update()
    account, _ = queryset.get_or_create(store=store, user=user)
    return account


def redemption_quote(store, user, requested_points, eligible_amount, *, account=None):
    if not store.loyalty_enabled or not user.is_authenticated:
        return 0, Decimal(0)
    try:
        requested = max(0, int(requested_points or 0))
    except (TypeError, ValueError):
        requested = 0
    account = account or loyalty_account(store, user)
    maximum_by_amount = int(Decimal(eligible_amount) // store.loyalty_point_value)
    points = min(requested, account.balance, maximum_by_amount)
    if points < store.loyalty_min_redeem:
        return 0, Decimal(0)
    return points, Decimal(points * store.loyalty_point_value)


def _entry(account, order, kind, points, note):
    if LoyaltyTransaction.objects.filter(order=order, kind=kind).exists():
        return False
    account.balance += points
    if kind == "earn":
        account.lifetime_earned += points
    account.save(update_fields=["balance", "lifetime_earned", "updated_at"])
    LoyaltyTransaction.objects.create(account=account, order=order, kind=kind, points=points,
        balance_after=account.balance, note=note)
    return True


@transaction.atomic
def redeem_for_order(order, points):
    if not points or not order.customer_user_id:
        return False
    account = loyalty_account(order.store, order.customer_user, lock=True)
    if points > account.balance:
        raise ValueError("Saldo poin tidak mencukupi.")
    return _entry(account, order, "redeem", -points, "Redeemed at checkout")


def apply_order_loyalty(order, new_status):
    """Apply idempotent loyalty effects while the order transition is locked."""
    if not order.store.loyalty_enabled or not order.customer_user_id:
        return
    account = loyalty_account(order.store, order.customer_user, lock=True)
    if new_status == "completed" and not order.loyalty_points_earned:
        eligible = max(Decimal(0), order.subtotal - order.discount)
        earned = int(eligible // order.store.loyalty_spend_step) * order.store.loyalty_points_per_step
        if earned and _entry(account, order, "earn", earned, "Completed order reward"):
            Order.objects.filter(pk=order.pk).update(loyalty_points_earned=earned)
            order.loyalty_points_earned = earned
    if new_status in {"cancelled", "refunded"} and order.loyalty_points_redeemed:
        _entry(account, order, "restore", order.loyalty_points_redeemed,
            f"Points restored after order {new_status}")
    if new_status == "refunded" and order.loyalty_points_earned:
        _entry(account, order, "reversal", -order.loyalty_points_earned, "Reward reversed after refund")
