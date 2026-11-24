"""The company's book currency: one setting (``Store.currency``) that every amount on screen, in PDFs and in Excel
follows. Rupiah is the only currency switched on; USD and EUR are defined so the setting, the formatting and the
tests already handle them, and are turned on by adding them to ``ENABLED`` once the rest of the system (exchange
rates, multi-currency documents) is ready.

The currency is the *book* currency, not a transaction currency: it can be chosen only while the company's books
have no entry, because changing it afterwards would relabel every posted amount without converting it."""
from contextvars import ContextVar
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation


@dataclass(frozen=True)
class Currency:
    code: str
    label: str
    symbol: str
    decimals: int          # digits shown after the decimal mark
    thousands: str         # thousands separator
    decimal_mark: str
    excel_format: str      # number format for Excel cells

    def format(self, value, *, signed=False):
        """``Rp1.250.000`` / ``$1,250.00``; a real minus sign; ``signed`` adds ``+`` to positive amounts."""
        number = to_decimal(value)
        if number is None:
            return ""
        step = Decimal(1).scaleb(-self.decimals)
        rounded = abs(number).quantize(step, ROUND_HALF_UP)
        whole, _, fraction = f"{rounded:.{self.decimals}f}".partition(".")
        groups = []
        while len(whole) > 3:
            groups.insert(0, whole[-3:])
            whole = whole[:-3]
        groups.insert(0, whole)
        text = self.thousands.join(groups) + (self.decimal_mark + fraction if self.decimals else "")
        sign = "−" if number < 0 and rounded else ("+" if signed and number > 0 else "")
        return f"{sign}{self.symbol}{text}"


CURRENCIES = {
    "IDR": Currency("IDR", "Rupiah (IDR)", "Rp", 0, ".", ",", '"Rp"#,##0;-"Rp"#,##0'),
    "USD": Currency("USD", "Dolar AS (USD)", "$", 2, ",", ".", '"$"#,##0.00;-"$"#,##0.00'),
    "EUR": Currency("EUR", "Euro (EUR)", "€", 2, ".", ",", '"€"#,##0.00;-"€"#,##0.00'),
}
DEFAULT = "IDR"
# Currencies a company may choose today. USD and EUR follow once rates and foreign documents exist.
ENABLED = frozenset({"IDR"})
CHOICES = [(code, currency.label) for code, currency in CURRENCIES.items()]

_active = ContextVar("book_currency", default=DEFAULT)


def to_decimal(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def get(code):
    return CURRENCIES.get((code or "").upper(), CURRENCIES[DEFAULT])


def for_store(store):
    return get(getattr(store, "currency", DEFAULT))


def activate(code):
    """Make ``code`` the currency amounts are printed in for the rest of this request."""
    _active.set(get(code).code)


def reset():
    _active.set(DEFAULT)


def current():
    return CURRENCIES[_active.get()]


def can_change(store):
    """``(allowed, reason)``: the book currency is fixed once any journal entry exists."""
    from apps.accounting.models import JournalEntry
    if JournalEntry.objects.filter(store=store).exists():
        return False, ("Pembukuan sudah berisi jurnal. Mata uang pembukuan tidak bisa diganti, karena semua "
            "angka yang sudah tercatat akan berganti label tanpa dikonversi.")
    return True, ""


class CurrencyError(ValueError):
    pass


def set_currency(store, code, *, actor=None):
    code = (code or "").upper()
    if code not in CURRENCIES:
        raise CurrencyError("Mata uang tidak dikenal.")
    if code not in ENABLED:
        raise CurrencyError(f"{CURRENCIES[code].label} belum tersedia. Saat ini pembukuan memakai Rupiah.")
    if code == store.currency:
        return store
    allowed, reason = can_change(store)
    if not allowed:
        raise CurrencyError(reason)
    old = store.currency
    store.currency = code
    store.save(update_fields=["currency"])
    from apps.shops.audit import log
    log(store, actor, "store.currency_changed", store, before=old, after=code)
    return store
