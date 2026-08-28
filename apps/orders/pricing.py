from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from .services import calculate_discount

MONEY = Decimal("0.01")


@dataclass(frozen=True)
class PriceBreakdown:
    subtotal: Decimal
    discount: Decimal
    shipping: Decimal
    tax: Decimal
    fees: Decimal
    total: Decimal


def money(value):
    return Decimal(value).quantize(MONEY, rounding=ROUND_HALF_UP)


def calculate_price(*, store, lines, shipping_cost=0, coupon=None, fees=0, additional_discount=0):
    subtotal = money(sum((line["unit_price"] * line["quantity"] for line in lines), Decimal(0)))
    shipping = money(shipping_cost)
    discount, shipping = calculate_discount(coupon, subtotal, shipping)
    discount = Decimal(discount) + max(Decimal(0), Decimal(additional_discount))
    discount, shipping = money(min(discount, subtotal)), money(shipping)
    taxable = max(Decimal(0), subtotal - discount)
    tax = money(taxable * store.tax_rate / Decimal(100))
    fees = money(max(Decimal(0), Decimal(fees)))
    total = money(max(Decimal(0), taxable + shipping + tax + fees))
    return PriceBreakdown(subtotal, discount, shipping, tax, fees, total)
