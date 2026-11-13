"""Lembar kerja SPT Tahunan PPh Badan: from the commercial profit in the books to PPh 29.

    laba komersial (ledger) + koreksi positif − koreksi negatif = laba fiskal → PKP (rounded down to
    thousands, never below zero) → PPh terutang at 22%, or with the Pasal 31E reduction (half the rate on the
    part of PKP that matches Rp4,8 miliar of turnover) when turnover is within Rp50 miliar → less credits
    (PPh 22/23/25 already paid) = PPh 29 kurang/lebih bayar.

The corrections and credits are entered by the accountant (``FiscalAdjustment``): the worksheet never
guesses what is deductible. It is a preparation aid, not a replacement for the tax advisor; individuals
(progressive rates, PTKP) and PPh Final UMKM stores do not use it."""
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from .models import FiscalAdjustment
from .services import tax_profile

FULL_RATE = Decimal("0.22")
DISCOUNT_RATE = Decimal("0.11")          # 50% of 22%
DISCOUNT_BASE = Decimal(4_800_000_000)   # turnover whose share of PKP gets the reduced rate
DISCOUNT_CEILING = Decimal(50_000_000_000)
ZERO = Decimal(0)


def _round(value):
    return Decimal(value).quantize(Decimal(1), ROUND_HALF_UP)


def tax_due(pkp, turnover):
    """``(tax, detail)`` for ``pkp`` at ``turnover``. ``detail`` lists the parts with their rates."""
    if pkp <= 0:
        return ZERO, []
    if turnover > DISCOUNT_CEILING:
        return _round(pkp * FULL_RATE), [("PKP", pkp, FULL_RATE)]
    if turnover <= DISCOUNT_BASE:
        return _round(pkp * DISCOUNT_RATE), [("PKP seluruhnya (Pasal 31E)", pkp, DISCOUNT_RATE)]
    reduced = (pkp * DISCOUNT_BASE / turnover).quantize(Decimal(1), ROUND_DOWN)
    rest = pkp - reduced
    return _round(reduced * DISCOUNT_RATE + rest * FULL_RATE), [("Bagian berfasilitas (Pasal 31E)", reduced, DISCOUNT_RATE),
        ("Sisanya", rest, FULL_RATE)]


def applicable(profile):
    """Why the worksheet does not apply, or ``""``."""
    if profile.taxpayer_type != "company":
        return "Lembar ini untuk badan usaha. Orang pribadi memakai tarif progresif dan PTKP."
    if profile.pph_regime == "umkm_final":
        return "Toko ini memakai PPh Final UMKM 0,5% dari omzet, sehingga tidak ada perhitungan PPh badan tahunan. Ubah rezim pajak di Profil pajak bila sudah tidak memenuhi syarat."
    return ""


def worksheet(store, year):
    from datetime import date

    from apps.accounting import reports
    profile = tax_profile(store)
    pl = reports.profit_and_loss(store, date(year, 1, 1), date(year, 12, 31), compare=False)
    commercial, turnover = pl["totals"]["net"]["amount"], pl["totals"]["revenue"]["amount"]
    rows = list(FiscalAdjustment.objects.filter(store=store, year=year))
    positive = sum((r.amount for r in rows if r.kind == FiscalAdjustment.POSITIVE), ZERO)
    negative = sum((r.amount for r in rows if r.kind == FiscalAdjustment.NEGATIVE), ZERO)
    credits = sum((r.amount for r in rows if r.kind == FiscalAdjustment.CREDIT), ZERO)
    fiscal = commercial + positive - negative
    pkp = max((fiscal / 1000).quantize(Decimal(1), ROUND_DOWN) * 1000, ZERO)
    tax, detail = tax_due(pkp, turnover)
    return {"year": year, "profile": profile, "not_applicable": applicable(profile), "commercial": commercial,
        "turnover": turnover, "rows": rows, "positive": positive, "negative": negative, "credits": credits,
        "fiscal": fiscal, "pkp": pkp, "tax": tax, "detail": detail, "pph29": tax - credits,
        "pph25_monthly": _round(tax / 12), "loss": -fiscal if fiscal < 0 else ZERO,
        "effective": round(tax / commercial * 100, 1) if commercial > 0 and tax else None,
        "facility": ZERO < turnover <= DISCOUNT_CEILING}
