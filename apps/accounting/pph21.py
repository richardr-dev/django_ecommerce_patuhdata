"""PPh Pasal 21 for permanent employees under PP 58/2023 and PMK 168/2023.

January to November: gross monthly income × the monthly TER rate (tarif efektif rata-rata) of the
employee's category. The category follows the PTKP status: A for TK/0, TK/1, K/0; B for TK/2, TK/3, K/1,
K/2; C for K/3. "Gross" includes the BPJS premiums the employer pays that count as the employee's income
(Kesehatan, JKK, JKM).

December (the last tax period): the year's tax is computed with the Pasal 17 ayat (1) huruf a rates on
the year's PKP, and what January to November withheld is subtracted. The rest is withheld in December;
a negative rest is returned to the employee (lebih potong).

The three tables below are the Lampiran of PP 58/2023 (huruf A, B and C), each row checked against the
official scan: ``(upper bound inclusive, rate %)``, the last row without an upper bound."""
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

D = Decimal

TER = {
    "A": [(5_400_000, "0"), (5_650_000, "0.25"), (5_950_000, "0.5"), (6_300_000, "0.75"), (6_750_000, "1"),
        (7_500_000, "1.25"), (8_550_000, "1.5"), (9_650_000, "1.75"), (10_050_000, "2"), (10_350_000, "2.25"),
        (10_700_000, "2.5"), (11_050_000, "3"), (11_600_000, "3.5"), (12_500_000, "4"), (13_750_000, "5"),
        (15_100_000, "6"), (16_950_000, "7"), (19_750_000, "8"), (24_150_000, "9"), (26_450_000, "10"),
        (28_000_000, "11"), (30_050_000, "12"), (32_400_000, "13"), (35_400_000, "14"), (39_100_000, "15"),
        (43_850_000, "16"), (47_800_000, "17"), (51_400_000, "18"), (56_300_000, "19"), (62_200_000, "20"),
        (68_600_000, "21"), (77_500_000, "22"), (89_000_000, "23"), (103_000_000, "24"), (125_000_000, "25"),
        (157_000_000, "26"), (206_000_000, "27"), (337_000_000, "28"), (454_000_000, "29"), (550_000_000, "30"),
        (695_000_000, "31"), (910_000_000, "32"), (1_400_000_000, "33"), (None, "34")],
    "B": [(6_200_000, "0"), (6_500_000, "0.25"), (6_850_000, "0.5"), (7_300_000, "0.75"), (9_200_000, "1"),
        (10_750_000, "1.5"), (11_250_000, "2"), (11_600_000, "2.5"), (12_600_000, "3"), (13_600_000, "4"),
        (14_950_000, "5"), (16_400_000, "6"), (18_450_000, "7"), (21_850_000, "8"), (26_000_000, "9"),
        (27_700_000, "10"), (29_350_000, "11"), (31_450_000, "12"), (33_950_000, "13"), (37_100_000, "14"),
        (41_100_000, "15"), (45_800_000, "16"), (49_500_000, "17"), (53_800_000, "18"), (58_500_000, "19"),
        (64_000_000, "20"), (71_000_000, "21"), (80_000_000, "22"), (93_000_000, "23"), (109_000_000, "24"),
        (129_000_000, "25"), (163_000_000, "26"), (211_000_000, "27"), (374_000_000, "28"), (459_000_000, "29"),
        (555_000_000, "30"), (704_000_000, "31"), (957_000_000, "32"), (1_405_000_000, "33"), (None, "34")],
    "C": [(6_600_000, "0"), (6_950_000, "0.25"), (7_350_000, "0.5"), (7_800_000, "0.75"), (8_850_000, "1"),
        (9_800_000, "1.25"), (10_950_000, "1.5"), (11_200_000, "1.75"), (12_050_000, "2"), (12_950_000, "3"),
        (14_150_000, "4"), (15_550_000, "5"), (17_050_000, "6"), (19_500_000, "7"), (22_700_000, "8"),
        (26_600_000, "9"), (28_100_000, "10"), (30_100_000, "11"), (32_600_000, "12"), (35_400_000, "13"),
        (38_900_000, "14"), (43_000_000, "15"), (47_400_000, "16"), (51_200_000, "17"), (55_800_000, "18"),
        (60_400_000, "19"), (66_700_000, "20"), (74_500_000, "21"), (83_200_000, "22"), (95_600_000, "23"),
        (110_000_000, "24"), (134_000_000, "25"), (169_000_000, "26"), (221_000_000, "27"), (390_000_000, "28"),
        (463_000_000, "29"), (561_000_000, "30"), (709_000_000, "31"), (965_000_000, "32"),
        (1_419_000_000, "33"), (None, "34")],
}

CATEGORY = {"TK/0": "A", "TK/1": "A", "K/0": "A", "TK/2": "B", "TK/3": "B", "K/1": "B", "K/2": "B", "K/3": "C"}

# PTKP setahun (PMK 101/PMK.010/2016): Rp54.000.000 for the taxpayer, Rp4.500.000 for being married and
# Rp4.500.000 per dependant (at most three).
PTKP_BASE, PTKP_STEP = D(54_000_000), D(4_500_000)

# Pasal 17 ayat (1) huruf a UU PPh as amended by UU HPP: (upper bound of the layer, rate %).
PASAL_17 = [(D(60_000_000), D(5)), (D(250_000_000), D(15)), (D(500_000_000), D(25)), (D(5_000_000_000), D(30)),
    (None, D(35))]

# Biaya jabatan: 5% of gross, at most Rp500.000 a month (Rp6.000.000 a year).
JOB_COST_RATE, JOB_COST_MONTHLY_CAP = D(5), D(500_000)


def category(ptkp_status):
    return CATEGORY[ptkp_status]


def ter_rate(ptkp_status, gross):
    """The monthly TER rate (percent) for a month's ``gross`` income."""
    gross = D(gross)
    for upper, rate in TER[category(ptkp_status)]:
        if upper is None or gross <= upper:
            return D(rate)
    raise AssertionError("unreachable: the last row has no upper bound")


def ptkp(ptkp_status):
    married, dependants = ptkp_status.split("/")
    return PTKP_BASE + (PTKP_STEP if married == "K" else 0) + PTKP_STEP * int(dependants)


def pasal_17(pkp):
    """Tax on a year's PKP with the progressive Pasal 17 layers."""
    tax, lower = D(0), D(0)
    for upper, rate in PASAL_17:
        if pkp <= lower:
            break
        top = pkp if upper is None else min(pkp, upper)
        tax += (top - lower) * rate / 100
        lower = upper if upper is not None else pkp
    return tax.quantize(D(1), rounding=ROUND_HALF_UP)


def annual(ptkp_status, *, gross, pension, months, withheld):
    """The December recalculation. ``gross`` is the year's gross (with the taxable premiums), ``pension``
    the year's JHT + JP paid by the employee, ``months`` how many months were paid, ``withheld`` the PPh 21
    of January to November. Returns a dict of every step; ``december`` may be negative (lebih potong)."""
    gross, pension, withheld = D(gross), D(pension), D(withheld)
    job_cost = min((gross * JOB_COST_RATE / 100).quantize(D(1), rounding=ROUND_HALF_UP),
        JOB_COST_MONTHLY_CAP * months)
    net = gross - job_cost - pension
    allowance = ptkp(ptkp_status)
    pkp = (max(net - allowance, D(0)) / 1000).to_integral_value(rounding=ROUND_DOWN) * 1000  # whole thousands
    tax = pasal_17(pkp)
    return {"gross": gross, "job_cost": job_cost, "pension": pension, "net": net, "ptkp": allowance, "pkp": pkp,
        "tax": tax, "withheld": withheld, "december": tax - withheld}
