# POS module: architecture (decided 2026-11-17; phases 0 and 1 built the same day, see §8)

Point of sale for physical retail: one company (`Store`) runs several outlets, each outlet runs one or
more registers (kasir), and every sale lands in the same books as the online, marketplace and B2B sales.
The outlet is controlled on its own (stock, cash drawer, cashiers, prices, reports); the accounting is
shared (one ledger, one chart of accounts, one SPT Masa PPN).

This document follows the rules in `CLAUDE.md` and `architecture.md`: the ledger is the only money record,
stock moves only through `orders/stock.py`, invoices are made only through `tax/sales.py`, roles come from
one matrix, every money form carries an idempotency key.

## 1. What already exists and is reused

| Need | Already in the code | Reuse |
|---|---|---|
| Outlet | `catalog.Warehouse.is_outlet` (address, phone, opening hours, pickup) | The outlet **is** the warehouse. No new outlet entity. |
| Stock per outlet | `catalog.InventoryLevel` (warehouse × product), append-only `InventoryMovement` | POS sells from the outlet's level only. |
| Moving stock to an outlet | `orders/stock.py:transfer_stock`, stock requests (`orders/stock_requests.py`), stok opname | Outlet replenishment and counts need nothing new. |
| Sales channel | `Order.CHANNELS` has `("pos", "POS Retail")` | Every POS ticket is an order with `channel="pos"`. |
| Sale + stock + HPP in one transaction | `tax/sales.py:create_sales_invoice` | One call per ticket; add a `warehouse=` parameter (see §4). |
| Money accounts | `accounting.BankAccount` kind `cash` with its own 11xx account | One cash account per register (the drawer). |
| Payments | `tax.InvoicePayment` (cash / transfer / qris / other, `bank_account`) | One payment row per tender; split tender is several rows. |
| Returns | `tax/credit_notes.py:create_credit_note` (refund to a `bank_account`, restock) | Refund at the register is a credit note paid from the drawer. |
| Petty expenses from the drawer | `Expense` with `bank_account` | "Kas keluar" at the register. |
| Deposit to the bank | `accounting/bank.py` transfer between accounts | Shift close "setor ke bank". |
| Per-outlet P&L | `accounting.CostCenter`, `JournalEntry.cost_center`, budgets | One cost centre per outlet; POS postings pass it. |
| Approvals, audit, fraud checks | `accounting/approvals.py`, `shops/audit.py`, `accounting/controls.py` | Shift variance above threshold, voids, discounts. |
| Idempotency | `accounting/idempotency.py` | Ticket submits carry a client-generated key. |
| Customer, loyalty, gift cards, price lists | `shops.Customer`, `orders/loyalty.py`, `orders/gift_cards.py`, `tax/pricing.py` | Optional on a ticket; walk-in is the default. |
| Barcodes | `ProductVariant.barcode`, `orders/barcodes.py` | Scanner is a keyboard wedge: scan → lookup by barcode, then SKU. |
| Numbering | `tax/numbering.py:next_number(store, kind, on_date)` | A `pos` series so tickets do not use the INV numbers. |
| Staff and roles | `shops.StoreStaff`, `shops/permissions.py:MATRIX` | New role `cashier` and `pos.*` permission keys. |

## 2. Entities to add (app `apps/pos/`)

```
Warehouse (is_outlet)  1 ── n  PosRegister ── n  PosShift ── n  PosSale ── 1  Invoice (channel=pos)
                                   │                 │             └── n  PosTender ── 1  InvoicePayment
                                   └── 1 BankAccount │
                                       (kas kasir)   └── n  PosCashMovement (paid-in / paid-out / deposit)
StoreStaff.outlet (nullable)  → the outlet a cashier is pinned to
CostCenter ← Warehouse.cost_center (nullable)
```

- **PosRegister** (`store`, `outlet` FK Warehouse, `code` "K1", `name`, `cash_account` OneToOne BankAccount
  kind cash, `receipt_prefix`, `receipt_header`, `receipt_footer`, `is_active`). Creating a register
  creates its cash account ("Kas kasir Outlet Bandung K1", next 11xx code). A register is a drawer, not a
  device: two tablets may share one register, but one register has one open shift at a time.
- **PosShift** (`register`, `opened_by`, `opened_at`, `opening_float`, `closed_by`, `closed_at`,
  `expected_cash` (float + cash sales − cash refunds − paid-out + paid-in − deposits), `counted_cash`,
  `variance`, `variance_entry` FK JournalEntry, `status` open/closed/approved, `note`).
  One open shift per register (`UniqueConstraint(register, status=open)`).
- **PosSale** (`shift`, `register`, `number` "BDG-K1-20261117-0042", `invoice` OneToOne Invoice,
  `cashier`, `customer` nullable, `sold_at`, `subtotal`, `discount`, `rounding`, `tax`, `total`,
  `paid`, `change`, `status` paid/void, `voided_by`, `voided_at`, `void_reason`, `idem_key` unique).
  Lines are **not** duplicated: they are the invoice's `InvoiceItem`s and the order's `OrderItem`s.
- **PosTender** (`sale`, `method` cash/qris/edc_debit/edc_credit/transfer/ewallet/gift_card/customer_credit,
  `amount`, `reference` (approval code, QRIS RRN), `payment` OneToOne InvoicePayment, `bank_account`).
- **PosCashMovement** (`shift`, `kind` paid_in/paid_out/deposit, `amount`, `reason`, `expense` FK Expense
  nullable, `transfer_entry` FK JournalEntry nullable, `actor`).
- **Settings on the outlet** (fields on Warehouse or a `PosOutletSettings` OneToOne): `cost_center`,
  `price_list` (outlet prices), `prices_include_tax` (retail shows PPN-inclusive prices), `rounding`
  (none / Rp100 / Rp500), `max_line_discount_pct`, `allow_price_override`, `receipt_logo` (falls back to
  `tax/branding.py`).
- **StoreStaff.outlet** nullable FK Warehouse: a cashier sees only that outlet's registers; back-office
  roles (admin, manager, finance…) see all outlets.

New accounts (in `ledger.CHART` since 2026-11-17, so every store gets them on its next posting; constants in `ledger.py`):

| Code | Name | Use |
|---|---|---|
| 1210 | Piutang EDC / QRIS | Card and QRIS tenders until the acquirer settles to the bank |
| 11xx | Kas kasir \<outlet\> \<register\> | One per register (`BankAccount` kind cash) |
| 4950 | Pembulatan penjualan | Rounding given or taken at the register |
| 6980 | Selisih kas kasir | Shift variance (short); a surplus credits 7250 Lebih kas kasir |
| 6155 | Biaya MDR EDC / QRIS | Acquirer fee found at settlement |

## 3. Accounting integration (the whole point)

POS never writes journal lines. Each ticket becomes documents the books already understand:

1. **Sale**: `create_sales_invoice(store, lines=…, channel="pos", warehouse=outlet, customer=…,
   discount=…, actor=cashier, number_series="pos")` → order (`channel="pos"`), stock out of the outlet's
   `InventoryLevel` with HPP at moving average, invoice posted by `tax/posting.py` as today:
   `1250 Piutang / 4100 Penjualan, 2100 PPN keluaran, 4900 Potongan, 5100 HPP / 1400 Persediaan`.
   Rounding is a line of its own on the entry (4950), so the entry still balances to `total`.
2. **Tenders**: one `InvoicePayment` per tender, `paid_on = sold_at`:
   cash → the register's cash account; QRIS / EDC → 1210; transfer → the chosen bank account;
   gift card / customer credit → the existing redemption paths. The invoice is `paid` immediately
   (a POS sale is never on credit; B2B credit sales go through the invoice form).
3. **Refund / return at the register**: `create_credit_note(invoice, lines, excess_action=REFUND,
   bank_account=register.cash_account, restock=…)`. Good goods go back on the outlet's shelf; damaged
   stay out (same rules as `tax/returns.py`). A refund in cash reduces the drawer's expected cash.
4. **Void**: allowed only while the ticket's shift is open and by the cashier's own ticket or a manager:
   it reverses the invoice (`reverse_source`), returns the stock, and reverses the tender(s). After the
   shift closes a mistake is a credit note, never a void.
5. **Paid-out** (buy ice, pay the parking man): an `Expense` with `bank_account=register.cash_account`.
   **Paid-in** (manager tops up the float): a bank transfer from Kas utama to the register cash.
6. **Shift close**: `expected_cash` computed from the ledger (balance of the register's account since the
   shift opened, which equals float + cash tenders − cash refunds − paid-out + paid-in) and compared with
   `counted_cash`. The variance posts one entry `("pos_shift", shift.id)`: 6980 / register cash (short) or
   register cash / 7250 (over). Above the store's threshold the close becomes an `ApprovalRequest`
   (kind `pos_shift_variance`) for a manager. "Setor ke bank" moves the counted cash (minus the float
   kept for tomorrow) to the chosen bank account with a bank transfer, so the next morning the drawer
   holds the float only.
7. **Acquirer settlement**: the bank statement line "QRIS settlement 17/11" is matched in the existing
   bank reconciliation against 1210 with the MDR fee to 6155 (same pattern as marketplace payouts into
   1300 with fees to 61xx). `controls.edc_clearing_aging` warns when 1210 holds lines older than 3 days.
8. **Cost centre**: every POS posting passes the outlet's `cost_center`, so Budgets and the cost-centre
   P&L show each outlet; the consolidated P&L is the normal one (one Store = one ledger).
9. **PPN for retail PKP**: POS invoices to walk-in buyers are *Faktur Pajak digunggung* (reported as one
   total in SPT Masa PPN, not one e-Faktur each). `tax/spt.py` must sum `channel="pos"` invoices without
   NPWP into the digunggung line, and `tax/faktur.py:xml_document` must leave them out of
   `TaxInvoiceBulk`. A POS sale **with** a buyer NPWP is a normal faktur and stays in the XML.
10. **Backlog**: `register_backlog("pos_shift", …)` for variance entries; sales and payments are already
    covered by the invoice and payment backlog. `post_everything()` picks up anything that failed.
11. **Closed periods**: a ticket is dated now, so locks never bite; a shift close after month-end locks is
    refused with `strict=True` (the manager reopens or dates the deposit on the first open day).

### Ticket sequence (one database transaction, idempotent)

```
POST /pos/<register>/sale/  {idem, lines[], tenders[], customer?, discount, note}
  claim(idem) ─ exists → return the earlier PosSale (double tap, flaky network)
  shift = open shift of register, else refuse "Buka kasir dulu"
  invoice = create_sales_invoice(channel="pos", warehouse=register.outlet, …)   # stock + HPP + entry
  for tender in tenders: record_receipt(invoice, method, amount, bank_account=…)  # 11xx or 1210
  sale = PosSale(…, invoice=invoice, paid=Σ, change=Σ − total)
  OutboxEvent("pos.sale", sale.id)  # e-receipt by WhatsApp, Shopify/marketplace stock push later
  finish(idem, sale)
  → JSON {number, total, change, receipt_url}; the browser prints the receipt
```

## 4. Changes to existing code (prerequisites)

1. **`stock_out(warehouse=…)`** ✅ done 2026-11-17: `stock_out`, `stock_shortages`, `deduct_invoice_stock` and
   `create_sales_invoice` take `warehouse=`; the sale comes from that outlet only, refuses when the outlet
   lacks the units even if another branch has them, and records stok minus at the outlet
   (`tests/test_sale_from_warehouse.py`). Variants are still excluded (one stock figure per store).
2. **Variant stock per warehouse** (architecture item 4): `ProductVariant.stock` is one number per
   variant. A fashion outlet selling size M cannot be counted per outlet until variants have levels per
   warehouse. Build this before POS, or limit POS v1 to products without variants (a real limitation for
   apparel, acceptable for F&B, minimarket, pharmacy, spare parts).
3. **Stok minus at the register**: POS refuses a sale beyond the outlet's shelf by default (the goods are
   physically handed over, so a shortage means the count is wrong, not the sale). With the store switch on,
   a user holding `stock.oversell` may confirm it; the deficit is recorded on the outlet's level.
4. **Number series** ✅ done 2026-11-17: `create_sales_invoice(number_series="POS")` →
   `issue_invoice_for_order(number_series=)` → `numbering.next_number(store, "POS", date)` gives
   `POS/2026/000001`, apart from `INV`. The per-register receipt number (`BDG-K1-20261117-0042`) lives on
   `PosSale` in phase 1.
5. **Roles** ✅ done 2026-11-17: role `cashier` (screen role `viewer`, `StoreStaff.outlet` pins it to one
   outlet) and keys `pos.sell` (cashier, ops, manager, admin), `pos.shift` (cashier, manager, admin),
   `pos.void` (cashier for own open-shift receipt, manager, admin), `pos.refund`, `pos.discount_override`,
   `pos.settings` (manager, admin), `pos.shift_approve` (manager, admin, finance); pinned in
   `tests/test_permissions.py`. Still to apply in phase 1: the cashier who closed a shift cannot approve its
   variance (`maker_may_not_check`).
6. **Price list on the outlet** and `prices_include_tax`: retail prices are PPN-inclusive on the shelf.
   ✅ `tax/services.py:exclusive_price(store, inclusive)` (2026-11-17) backs PPN out of a shelf price; the
   register sends exclusive unit prices to `create_sales_invoice` and books the leftover cents to 4950.
   The outlet's own price list and the `prices_include_tax` switch come with the outlet settings in phase 1.
7. **Controls** (`accounting/controls.py`): `pos_cash_tie` (each register account vs its open shift's
   expected cash), `pos_unposted` (PosSale without a live invoice entry or with tenders ≠ total),
   `edc_clearing_aging` (1210 older than 3 days), `shift_variance_trend` (fraud: repeated shortages per
   cashier), `pos_voids` (fraud: voids per cashier per day above a threshold).
8. **Todo rows** (`accounting/todo.py`): shifts open longer than 24 h, shift variances awaiting approval,
   1210 settlements not matched.
9. **Sidebar**: group Penjualan › Kasir (POS) with pages Register, Shift hari ini, Laporan per outlet;
   the selling screen itself is full-screen (no sidebar).

## 5. The register screen

- Route `/pos/<register_code>/` (under `/books/` for auth; add to `ALWAYS_OPEN` nothing: it is staff-only).
  Full-screen page, no sidebar, designed for a 10" tablet and a PC with a scanner; big product grid with
  categories and search, cart on the right, pay sheet with tender buttons and the change figure, numeric
  keypad for cash tendered. Keyboard: scan (Enter), `F2` pay, `F4` discount, `Esc` clear.
- Receipt: 58/80 mm thermal through the browser print dialog (`@page` CSS), QR with the e-receipt link
  (`/struk/<token>/`, public, in `ALWAYS_OPEN`, branded through `tax/branding.py`; `/s/` is the campaign short link and `/r/` the retired referral link), WhatsApp send through the
  store's account (`integrations/whatsapp.KINDS` + `receipt`). ESC/POS drivers, cash-drawer kick and
  customer displays are phase 4 (a small local print agent, not WebUSB).
- Connectivity: v1 is online-only with the idempotency key, so a retried submit never duplicates a sale.
  v2 keeps a queue in IndexedDB and replays it; the server then dates the sale by the device time
  (`sold_at`) exactly as marketplace imports keep the marketplace's time.
- The POS screen is a server-rendered page with a small JS cart (no React; the catalog is loaded once per
  shift as JSON and refreshed on demand), in the Glacier theme but with larger targets.

## 6. Per-outlet control vs shared books

| Controlled per outlet | Shared by the company |
|---|---|
| Stock (level, opname, requests, transfers) | Chart of accounts, journal, period locks, year close |
| Registers, drawers, shifts, cash deposits | Bank accounts the deposits go to, reconciliation |
| Cashiers (`StoreStaff.outlet`), who may discount or void | Roles matrix, approval thresholds |
| Prices (outlet price list, inclusive tax display) | Products, costs (one moving average per product per store) |
| Reports: sales per outlet / register / cashier / hour, Z report, variance, top items | P&L, neraca, SPT Masa PPN, SPT Tahunan, audit pack |
| Cost centre actuals and budget | Consolidated figures (sum of cost centres = company) |

Two companies (PT A and PT B) are two `Store`s with separate books, as today; an outlet of PT B is never
a cost centre of PT A. Group consolidation stays unbuilt (architecture item 6).

## 7. What else is needed (outside the code)

- **Hardware per register**: Android/iPad tablet or PC, 58/80 mm thermal printer (network or USB with a
  print agent), USB/Bluetooth barcode scanner, cash drawer (kicked by the printer), an EDC terminal from
  the bank, a QRIS merchant ID (bank or payment provider). Price indication: Rp 3–6 juta per register.
- **Master data before go-live**: every sellable item has a SKU and a barcode; opening stock per outlet
  (transfer from the warehouse or stok opname); outlet price list; tax profile (PKP or not) decides whether
  PPN is on the receipt; receipt header/footer text; float amount per register.
- **Procedures**: open shift every morning with a counted float, close every evening with a count and a
  deposit; manager approves variances; month-end checklist gets a "all shifts closed" step.
- **Tax**: retail PKP reports POS sales digunggung in SPT Masa PPN; confirm with the client's accountant
  whether their KPP expects a faktur per receipt for B2B walk-ins (NPWP typed at the register).
- **Billing**: outlets are already "billed per outlet" on the plan; registers are free within an outlet.
- **Infrastructure**: the register page must stay fast on 4G; keep the catalog payload small and cache
  product images; one more worker job (`run_books`) step for the 1210 ageing.

## 8. Build order

| Phase | Scope | Depends on |
|---|---|---|
| 0 ✅ | `stock_out(warehouse=)`, `create_sales_invoice(warehouse=, number_series=)`, accounts 1210/4950/6980/7250/6155, `Warehouse.cost_center`, role `cashier` + `StoreStaff.outlet`, `pos.*` keys, `exclusive_price` | done 2026-11-17 |
| 1 ✅ | Register + cash account, open/close shift with variance entry and deposit, selling screen (cash/QRIS/EDC/transfer, split tender, line and ticket discount, rounding), receipt print, e-receipt link, shift (Z) report, controls `pos_receipts` / `pos_shifts`, cashier home and sidebar | done 2026-11-17; a per-outlet sales report beyond the shift report is still open |
| 2 | Refund and exchange at the register, void in open shift, paid-in/paid-out, deposit to bank, approvals on variance, fraud checks, todo rows, month-end step, SPT digunggung and XML exclusion | phase 1 |
| 3 | Variant stock per warehouse (then variants at the register), customer on ticket with loyalty and customer credit, gift cards, promotions, outlet price lists, WhatsApp e-receipt, outbox stock push | phase 2 |
| 4 | Offline queue (IndexedDB replay), local print agent (ESC/POS, drawer kick), EDC integration, multi-register per outlet dashboard | phase 3 |

Estimate: phase 0+1 about 3 weeks, phase 2 about 2 weeks, phase 3 depends mostly on variant stock per warehouse.

## 9. Decisions to confirm

1. **One invoice per receipt** (recommended) rather than one daily summary invoice per register. Per receipt
   keeps returns, stock card, HPP and fraud checks at line level; PostgreSQL handles the volume
   (a busy minimarket is ~1,000 rows a day).
2. **Register = drawer, not device** (recommended): shifts and cash tie to the drawer.
3. **Cashier is a new role**, pinned to one outlet, with no back-office pages except their own shift.
4. **Retail prices are PPN-inclusive** on the receipt for PKP stores (what Indonesian retail prints);
   the books still split 4100 and 2100.
5. **Online-only in v1**; the offline queue is phase 4.
6. **Void only in the open shift**; afterwards a credit note.

## 10. What phase 1 changed from this plan

- The drawer's expected cash is **the ledger balance of the register's cash account**, not a formula over the
  shift's sales. Paid-in, paid-out and deposits (phase 2) will therefore need no change to the close.
- `PosCashMovement` was not needed for phase 1: the float and the deposit are ordinary bank transfers
  (`CashTransaction`), visible in Kas & bank.
- Rounding and the outlet's cost centre live on the invoice (`Invoice.rounding`, `Invoice.cost_center`), so a
  repost from the backlog books the same entry.
- Card and QRIS use two payment methods of their own (`edc`, `qris_pos`); the existing `qris` method on office
  invoices still goes to the bank account named.
- The e-receipt is `/struk/<token>/`: `/s/` was already the campaign short link.
- Outlet settings (`PosOutletSettings`: PPN-inclusive prices, rounding step, cashier discount limit, receipt header
  and footer) have a model and defaults but no screen yet.
