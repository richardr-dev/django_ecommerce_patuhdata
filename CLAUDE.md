# Lakuin / Patuhdata back office

Accounting backbone for Indonesian commerce and SMEs: sales from every channel (own store,
Shopee, Tokopedia/TikTok Shop, Lazada, mitra konsinyasi), inventory, invoicing, purchasing, a
double-entry ledger, and Indonesian tax. Django 5.2, server-rendered templates, htmx, Tailwind
play CDN plus `static/css/merchant-shell.css`.


Make sure this is SAP Grade, MYOB Accounting with Modern UI/UX with BLUE and WHITE Theme

All user-facing text is Indonesian. Code, comments and tests are English.

## Commands

```
.venv/bin/pytest -q                      # tests use db.sqlite3 settings from .env
.venv/bin/ruff check <files>             # run --fix only on files you wrote or changed
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/python manage.py run_books     # daily worker: ledger backlog + recurring invoices + depreciation
.venv/bin/python manage.py post_ledger   # only the ledger backlog
.venv/bin/python manage.py run_recurring # only due recurring invoices
.venv/bin/python manage.py stock_audit   # stock vs movements; --fix
```

Four test files failed before this work and still do: `test_digital_products`,
`test_segments`, `test_smoke_urls` (KeyError `return_id`), `test_stock_editing`.

## Architecture rules

- **The ledger is the only place money is recorded.** Modules never write journal lines. They
  call `post_entry(store, date=, memo=, amounts={code: signed}, source=(type, id))` in
  `apps/accounting/ledger.py`. Positive is a debit, negative a credit.
- **One live entry per source document** (`unique_live_entry_per_source`). Posting twice returns
  the existing entry, so every poster is idempotent.
- **Entries are never edited or deleted.** Corrections use `reverse_entry` / `reverse_source`.
- **Closed periods** (`LedgerLock`): a posting with `strict=True` raises `LedgerLocked`; an
  automatic one moves to the first open day. Use `strict=True` whenever a person chose the date.
- **Every module registers a backlog poster** with `register_backlog`, so a document whose
  posting failed is picked up. Always call `post_everything()` (payments and refunds first, then
  the registered backlog), never `post_backlog()` alone.
- **New check = new function in `accounting/controls.py`** (ok / info / warn / fail with the two
  figures). When you add a ledger tie, add its check.
- **Stock changes go through `apps/orders/stock.py` or `inventory.py`** and always leave an
  `InventoryMovement`. Cost is moving average.
- **Services hold the logic, views stay thin.** A view parses the form, calls one service
  function, and turns its domain error (`BankError`, `CreditNoteError`, `ReceiptError`…) into a
  message. Services raise; they never swallow.
- **Invoices are created and changed only through `apps/tax/sales.py`** (`create_sales_invoice`,
  `update_sales_invoice`). One form (`tax/invoice_form_views.py`) serves new, edit and duplicate;
  never add a second invoice page. Lines may be goods (stock + HPP) or services (revenue only).
  Editing goods returns the old stock, takes the new and swaps the order (the old order is kept,
  cancelled); amounts lock once paid, credited or imported, and are corrected by credit note.
- **An invoice that asks for more than the shelf holds is refused** (`StockShortage` in
  `tax/sales.py`): stock leaves and HPP is booked in the same transaction, so the shelf and
  Persediaan cannot disagree. Products with no inventory level are untracked and not blocked.
- Opening receivables/payables are totals in the books (not itemised by invoice); the aging
  pages show the difference. Itemised opening invoices need `Invoice.order` to be optional.
- **Marketplace orders are dated by the marketplace's order time** (`MarketplaceOrder.placed_at` /
  `paid_at`, read by each parser; applied in `channels/services.py:apply_marketplace_dates`), never
  by the import day: the order, its payment, its journal entry (`ledger.redate_payment`) and its
  invoice follow it, and re-importing the same file corrects older imports. A new parser must set
  `placed_at`. Marketplace fees are split by charge into accounts 6110–6150 (`ledger.FEE_ACCOUNTS`,
  `Payout.fee_breakdown`); a missing invoice is issued later by the books worker.
- **A return is settled in one step** (`tax/returns.py`): inspection records the condition; settling
  creates the credit note (refund or customer credit) and moves stock together. Good goods go back
  on the shelf and to Persediaan at their cost; damaged goods stay out and their cost stays in HPP.
  An exchange takes the replacement variant off the shelf, returns the original if good, and books
  HPP against Persediaan when the original is damaged. Never flip a return to `refunded` by hand.
- **Refunds on marketplace orders** are `CreditNote.EXCESS_PLATFORM` ("dipotong dari pencairan"): the
  note credits Piutang marketplace (1300), not a bank, because the marketplace pays the buyer and
  takes it off the next payout. Only offered for shopee/tokopedia/tiktok_shop/lazada orders.
- **Buyer links** (`/i/<token>/`) are public and must stay in `ALWAYS_OPEN` (`shops/availability.py`):
  they work while the shop is in setup or closed. The token is the only key; never put a store
  admin action behind that URL.
- **Pesanan is the website's queue** (online shop, manual, POS, TV, reseller). Orders imported from a
  marketplace file or sold on shopee/tokopedia/tiktok_shop/lazada are hidden there
  (`orders/scope.py`) because they already live in Invoice penjualan, the channel recap and the
  books; a switch shows them. New lists of "orders to fulfil" must use `website_orders(store)`.
  `manage.py clean_orders` reports empty import batches, duplicate imports and look-alike orders
  (`--apply` removes only the empty batches, never orders).
- **Every document a buyer sees takes its logo, accent colour and footer from `tax/branding.py`**
  (invoice logo from Tampilan invoice, else the shop logo, else the initial): invoice screen, print,
  PDF, buyer link, surat tagihan, surat jalan, quote, credit note. A new buyer-facing document must
  load `branding` and use `{% brand store obj as b %}`, never `merchant_store.logo` directly. PDFs
  embed the logo (`b.data_uri`); web pages use `b.logo_url`. Logos are PNG/JPG/WebP, verified with
  Pillow, max 2 MB and 4000 px (no SVG).
- **A return needs a photo before it is approved** (buyer uploads it with the request; a seller can add
  it on the return page). An exchange can only ask for a size that is in stock now. Photos are
  verified with Pillow (PNG/JPG/WebP, max 5 MB). Seller-recorded returns start as `requested`.
- **AB Shop is a mitra** (a `ConsignmentPartner`), not a sales channel. Its prices live in
  `PartnerProductPrice` ("Harga mitra" on a product) and are read through
  `settlements/pricing.py:partner_price` (own list, then the general `ab_shop_price`, then retail);
  `OrderItem.effective_ab_shop_price`, settlements and the invoice form all use it. On the one invoice
  form, "Jual ke mitra" prices the lines from that list, fills the mitra's contact, and records an
  order with `consignment_partner`, type `reseller_sell_in`, billed on this invoice (so a settlement
  never bills it again). A settlement only takes that mitra's unbilled orders. Courier and resi are
  stored on the order and print on every invoice document.
- **Customer segments filter by where they buy and where they live** (`channel`, `location` in the
  segment, so broadcasts can use them too). A place is the order's city/province, else the marketplace
  buyer's (Tokopedia hides addresses but gives city and province), else the address text; every typed
  word must match. Marketplace buyers waiting for approval are included whenever those filters are on.
- **Charts are drawn in HTML/CSS with one hue** (blue = magnitude; identity by label, not colour) and
  a hover tooltip (`data-tip`). Never put a Django float in `style="width:{{ x }}%"`: the Indonesian
  locale prints a decimal comma and the bar loses its width; use `|stringformat:"s"`.
- **Analytics margin comes from `channels/reporting.py:sales_report`** (the one margin source). It warns
  when marketplace fees were not imported and when sold products have no cost price: never show a
  margin without those warnings. Exports (`merchant-customers-export`, `merchant-sales-analytics-xlsx`)
  take the same query string as the screen they come from; their buttons read the live form.
- **Internal control** (see `accounting/approvals.py`, `accounting/attachments.py`, `shops/audit.py`):
  `accountant` is a read-only role (`_require_roles` lets it through on GET for pages that allow
  viewer or finance, never on POST; screens treat it as `viewer`). Above a per-store threshold, bill
  payments, expenses, manual journals and invoice or bill voids become an `ApprovalRequest` that a
  different admin, manager or finance user must approve; it then runs as the requester, and a store
  with no other approver is never blocked. A new money action must call `approvals.required(...)`
  before it posts. Every detail page shows its history (`shops/audit.trail`, so log new actions with
  `shops.audit.log`) and bills, journals, cash transactions and invoices take attachments.
  Fraud checks (duplicate bills, unusual expenses, big manual journals, back-dated entries, voided
  paid invoices) live in `accounting/controls.py`.
- **Budgets and cost centres** (`accounting/budgets.py`): actuals are read from the ledger, cost-centre
  actuals from `JournalEntry.cost_center`, so a posting that should count for a centre must pass it.
- **Year-end closing** (`accounting/closing.py`): `close_year` posts one `("year_close", year)` entry on
  31 Dec that zeroes revenue and expense into Laba ditahan 3300 and locks the books through that day;
  years close in order, never the current year, and only an admin reopens the newest one (the entry is
  reversed, not deleted). Reports pass `exclude_closing=True` to `account_balances`, so a closed year
  still shows its profit; only the trial balance shows 3300. Any new report that sums revenue or expense
  over a range must do the same. `controls.year_close_tie` fails when a closed year's profit changes.
- **Excel exports** (`accounting/exports.py`): a workbook uses the same service as its screen (never a
  second query) and writes real numbers with a rupiah format. A new report gets an entry in `REPORTS`.
- **Coretax**: the official import is a DJP Excel template converted to XML by DJP's converter, and its
  field layout is not confirmed in this repo. `tax/faktur.py` therefore produces a checked worksheet (per
  line, buyer NPWP/NIK flagged) and says so; do not claim it is an importable XML until the current DJP
  template (pajak.go.id, Coretax, Template XML dan Converter) has been read and the format tested.
- Mitra stock is on consignment: it stays the store's property (account 1450) until sold.

### Where things live

| Area | Service | Views |
|---|---|---|
| Ledger, chart of accounts | `accounting/ledger.py` | `accounting/views.py` |
| Kas & bank, reconciliation | `accounting/bank.py` | `accounting/views_books.py` |
| Fixed assets | `accounting/assets.py` | `accounting/views_books.py` |
| Opening balances (saldo awal) | `accounting/opening.py` | `accounting/views_books.py` |
| Reconciliation checks (does everything tie?) | `accounting/controls.py` | `accounting/views_books.py` |
| Full sales data export (lines, orders, payments) | `channels/sales_export.py` | `channels/report_views.py` |
| Stock value vs Persediaan | `accounting/inventory.py` | `accounting/views_books.py` |
| Add a brand (store in same Organization) | `shops/views.py:_create_store` | `shops/views.py` |
| Reports (P&L, cash flow, neraca, account ledger) | `accounting/reports.py` | both |
| Excel for each report, auditor yearly pack (`/merchant/accounting/paket-audit/`) | `accounting/exports.py` | `accounting/views_books.py` |
| SAK EMKM statements (screen, print, PDF) | `accounting/emkm.py` | `views_books.py:merchant_emkm_pack`, `templates/merchant/emkm_pack.html` |
| Year-end closing, reopen, monthly close checklist | `accounting/closing.py` | `views_books.py:merchant_year_close`, `templates/merchant/year_close.html` |
| PPh 21/23/4(2) withholding, bukti potong, deposit | `accounting/withholding.py` | `views_books.py:merchant_withholdings` |
| SPT Masa PPN worksheet with kompensasi | `tax/spt.py` | `tax/views.py:merchant_spt_ppn` |
| Faktur Pajak Keluaran worksheet for the Coretax template | `tax/faktur.py` | `tax/views.py:merchant_faktur_export` |
| Supplier bills, payables aging | `accounting/payables.py` | `accounting/views.py` |
| Invoice posting and status | `tax/posting.py`, `tax/services.py` | `channels/views.py` |
| **The one invoice form** (new, edit, duplicate; add customer inline) | `tax/sales.py` | `tax/invoice_form_views.py`, `templates/merchant/invoice_form.html` |
| Credit notes | `tax/credit_notes.py` | `tax/sales_views.py` |
| Store logo, accent colour and footer on every buyer document (Tampilan invoice) | `tax/branding.py` (`{% brand store invoice as b %}`) | `tax/sales_views.py:merchant_invoice_style`, `templates/merchant/invoice_style.html` |
| Send an invoice (link, WhatsApp, email, reminder) and the buyer's page | `tax/sharing.py`, `tax/public_views.py` | `channels/views.py:merchant_invoice_detail/_email/_sent`, `templates/tax/public_invoice.html` |
| Duplicate customers: find and merge | `shops/customer_merge.py` | `shops/segment_views.py:merchant_customer_duplicates` |
| Buyer's return / exchange request (item, type, reason, photo, size in stock) | `orders/return_requests.py` | `orders/views.py:request_return`, `templates/storefront/order.html` |
| Customer returns (inspect → settle: credit note + stock, or exchange) | `tax/returns.py` | `orders/views.py:merchant_return_detail`, `templates/merchant/return_detail.html` |
| Receipts, customer credit | `tax/receipts.py` | `tax/sales_views.py` |
| Quotes, recurring invoices | `tax/quotes.py`, `tax/recurring.py` | `tax/sales_views.py` |
| Sales analytics dashboard (revenue, channels, buyers new/returning, places, products) | `channels/analytics.py` | `channels/report_views.py:merchant_sales_analytics`, `_sales_analytics_body.html` |
| Find customers by channel bought on and by place (city, province, address words) | `shops/segments.py` (`by_channel`, `by_location`, `places`) | `shops/segment_views.py:merchant_customers` |
| Invoice, Pesanan and customer-history filters (one service; exports share it) | `channels/invoice_filters.py` | `channels/views.py:merchant_sales_invoices`, `_sales_invoices_list.html` |
| Invoice recap per channel (Shopee, Tokopedia…) | `channels/invoice_recap.py` | `channels/report_views.py` |
| Order-based margin per channel | `channels/reporting.py` | `channels/report_views.py` |
| Marketplace import, SKU map, income | `channels/services.py`, `mapping.py`, `income.py` | `channels/` |
| Consignment (mitra) | `settlements/consignment.py` | `settlements/views.py` |

### Account codes

1100–1199 cash and bank (one per `BankAccount`), 1200 gateway, 1250 piutang usaha,
1300 marketplace, 1400 persediaan, 1450 persediaan di mitra, 1500 PPN masukan, 1600/1690 aset
tetap and akumulasi, 2100 PPN keluaran, 2200 belum teridentifikasi, 2300 utang usaha,
2400 kredit pelanggan, 2500 pinjaman, 3100 modal, 3200 prive, 4100 penjualan, 4200 ongkir,
4800 retur, 4900 potongan, 5100 HPP, 6100 biaya gateway, 62xx–69xx biaya operasional,
6950 penyusutan, 7100/7200 pendapatan lain, 8100 biaya bank.

## UI conventions

- Shell: `templates/merchant/base.html`; sidebar is defined once in
  `apps/orders/templatetags/merchant_nav.py` (also the "Buat baru" menu). Every sub page must
  light up its sidebar item: when you add a URL outside an existing link's prefix, add it to
  that link's `match=` (`SidebarStaysOnTheRightItemTests` pins this). The sidebar keeps its
  scroll position and opened groups across page loads (`pd-nav-state` in sessionStorage).
  Sidebar, topbar-menu and in-page links navigate softly (`merchant-ui.js`, "Soft navigation"):
  the response's `<main>` replaces the current one, so the sidebar never re-renders or folds a
  group. Keep `#pd-nav` and `<main class="pd-main">` in `base.html`; scripts inside `<main>` re-run
  on every visit, so never add `document`/`window` listeners there (bind to elements). Opt a
  link out with `data-no-soft`. Links with `hx-*`, `data-confirm`, `onclick`, pdf/csv/print stay
  normal.
- Invoice lines carry optional enterprise columns: Kode (`code[]` → `InvoiceItem.sku` for
  services), Satuan (`unit[]`), Keterangan (`note[]`), Diskon Rp and a Diskon % helper. The form's
  "Kolom" menu shows them (remembered per device); detail and PDF show them when filled.
  Presentation-only fields stay editable after the amounts lock.
- New pages use the component classes in `merchant-shell.css`, not ad-hoc Tailwind:
  `pg-head`, `toolbar`, `tiles`, `panel`, `tbl`, `stmt` (statements), `editor` (line items),
  `dp-*` (detail/document pages), `pd-dialog`, `pill pill-{status}`, `btn`, `btn-primary`.
- Amounts: `{% load books %}` then `{{ value|rp }}`, `|signed_rp`, `|abs`.
- Detail pages show the journal behind the document with `{% include "merchant/_journal_card.html" %}`.
- Dialogs use `<dialog class="pd-dialog">` with `data-open` / `data-close` and
  `{% include "merchant/_dialog_js.html" %}`.
- A template variable used as a filter *argument* (`|default:post.name`) raises if it is
  missing. Pass a `defaultdict(str)` for such values (see `_posted` in `tax/sales_views.py`).
- Tailwind config must come after the CDN script in `base.html`; preflight stays on. The
  Tailwind CDN and htmx stay as they are (decision: no build step); new pages use the shell
  classes, not utilities.
- **Shared behaviour is in `static/js/merchant-ui.js`; do not copy it into templates:**
  - `<input data-rupiah>` formats 1.250.000 while typing and posts plain digits.
  - `data-confirm="text"` on a `<form>` or `<button>` opens the styled confirm dialog
    (`data-confirm-ok="Label"`, `data-confirm-tone="neutral"` for a non-destructive action).
    Never use `confirm()`.
  - Django messages are shown as toasts (success disappears, errors stay); `pdToast(text, kind)`
    for client-side ones.
  - Submit buttons show a spinner and cannot be double-clicked (opt out with `data-no-loading`).
  - A thin progress bar runs on htmx requests and navigation.
  - `<form data-unsaved>` warns before leaving with unsaved edits (soft navigation and tab
    close). Put it on every data-entry form (invoice, quote, bill, journal, cash, credit note).
  - Rows of `table.tbl` and `table[data-rows-link]` open their first link when clicked anywhere
    (⌘/Ctrl-click opens a new tab); mark a cell `data-no-row` to opt out.
  - Keyboard: `/` or ⌘K search (empty box shows "Terakhir dibuka" and "Buat baru"), `n` Buat baru,
    `g`+`d/i/o/p/b/a/c/s` jump (map in `#pd-go` in `base.html`), `?` shortcut sheet.
  - Density "rapat/nyaman" lives in the user menu (`html[data-density=compact]`); size new
    table paddings so the compact rules in `merchant-shell.css` apply.
  - htmx filters push clean URLs (empty parameters dropped).
- **htmx pattern for filters and search:** the list page includes a partial
  (`_bills_table.html`); the form has `hx-get`, `hx-target="#list"`, `hx-push-url="true"`; the
  view returns the partial through `render_partial()` in `accounting/views.py` (full page on a
  normal or history-restore request). Copy `merchant_bills` for new lists.
- Legacy pages (`.bo-card`, `.kpi-grid`, `.data-table`, entity forms) are restyled by the
  "Legacy bridge" block of `merchant-shell.css`. Rebuild them with the components above rather
  than adding to the bridge.
- **Brand per tab:** with several brands `<body data-store>` is set and `merchant-ui.js` adds
  `?store=` to back-office links, forms and htmx requests, so a tab keeps its brand whatever
  another tab switched to. `_staff_store` already validates it. Don't build URLs without it.
- Global search (`orders/search.py`, topbar) covers invoices, credit notes, quotes, orders,
  customers, products, bills, suppliers and journals; add a record type there.
- Beranda's "Perlu tindakan" gets finance rows from `accounting/todo.py`; add one there.

### Bubble style (the look every back-office page must share)

Reference pages: `templates/merchant/sales_invoices.html` (list) and `invoice_form.html` (form).
**Modern Blue & White Bubble Enterprise Dashboard.** The tokens live once, in the "Design system"
block at the end of `static/css/merchant-shell.css` (`--ds-*`); change the look there, never per page.

**Philosophy.** Structured enterprise data (KPIs, tables, filters) in friendly, tactile, rounded
geometry: generous radii, pill controls, soft elevation. A bright, high-trust canvas: pure-white
surfaces on an ice-blue canvas, sapphire and sky accents. Depth comes from tonal contrast
(`#FFFFFF` on `#F4F8FF`) and soft blue-tinted shadows, not from heavy grey borders.

**Tokens**
- Canvas `#F4F8FF`; card/bubble `#FFFFFF`; nested surface `#EBF3FF` (sub-cards `#F7FAFF`); hover/active pill `#DCEAFF`.
- Primary `#0066FF` (sapphire; `#1A73E8` allowed), hover `#0052CC`. Hero gradient (hero KPI card and
  insight cards only): `linear-gradient(135deg,#0066FF,#3898FF)`.
- Text: ink `#0F1E36` (never `#000`), muted `#526685`, placeholder `#8A9BB5`.
- Hairline `1px solid rgba(0,102,255,.08)`; shadow `0 8px 24px -4px rgba(15,30,54,.05), 0 2px 6px -1px rgba(0,102,255,.04)`.
- Status pills (soft pastel): positive `#0D7A3E` on `#E6F7EE`; warning `#B85D00` on `#FFF4E5`;
  critical `#C92A2A` on `#FFEBEB`; info `#0066FF` on `#EBF3FF`. Status colours only for meaning.
- Chart palette: `#0066FF`, `#4DA1FF`, `#8CC4FF`, `#0F1E36`.

**Shape and spacing**
- Main cards (bento containers) `rounded-3xl` (24px), padding 20-24px. Nested widgets `rounded-2xl`
  (16px) on `#F7FAFF`. Buttons, filter chips, search, tabs, badges and deltas are `rounded-full`
  (badges padding `4px 10px`). Grid gutter 20px (`gap-5`) so each card floats on the canvas.
- Inputs `h-11 rounded-xl`, soft fill, blue focus ring. Never a 0-4px corner on a card, button or input.

**Typography.** `'Plus Jakarta Sans','Inter',-apple-system,sans-serif`. `font-variant-numeric:
tabular-nums` on KPIs, money, dates and table columns. Page title 24px bold (`-0.02em`), card header
16px semibold, KPI hero number 32px bold, body/table 14px, micro labels and pills 12px medium.

**Layout**
1. Top bar: floating white pill (56px) or translucent header (`blur(12px)`, `rgba(255,255,255,.85)`);
   active navigation tab is a solid `#0066FF` pill with white text.
2. Filter and action strip: pill dropdowns (white, subtle blue border) + pill search + one solid
   `#0066FF` pill primary action.
3. KPI row: 3-4 `rounded-3xl` cards, at most one hero card in the gradient, each with a 40px circular
   `#EBF3FF` icon badge in the corner.
4. Tables inside a `rounded-3xl` white card: no vertical lines; header is a floating `#F4F8FF`
   `rounded-2xl` strip (12px semibold, `#526685`); rows 48-52px with a soft rounded hover; numbers
   right-aligned; statuses as pastel pills.
5. Charts: smooth curves (tension .4) with a soft blue gradient fill; bars with rounded tops (8px).

**Forbidden.** Dark mode, black or muddy grey surfaces (`#CCC`, `#888`), heavy dark shadows
(`rgba(0,0,0,.25)`), dense outer borders on cards, and any accent outside blue.

- **Page skeleton:** breadcrumb (`text-xs text-blue-700`) → `h1 text-2xl font-bold` + one-line
  help → content. Lists: hero with the single create button, KPI cards with icon bubbles, pill
  tabs, pill filters, avatar rows, status pills. Forms: left column of numbered sections, right
  sticky summary rail with the total and the save buttons.
- **Documents:** details show the journal card; columns that are optional (Kode, Satuan,
  Diskon, Keterangan) appear only when they hold data.
- **Optional columns pattern:** a "Kolom" menu with checkboxes, choice kept in `localStorage`
  (try/catch), a column holding data is never hidden.
- **Rules:** Indonesian text; every input has a label; one create button per page; no
  `confirm()`; amounts through `|rp`; rupiah inputs use `data-rupiah`; test at 390px width.
- **When touching a page, restyle it to this.** Do not mix the bubble style with
  `.bo-card` / `.kpi-grid` on one page. New pages never use the legacy bridge.
- **The "Bubble layer" at the end of `merchant-shell.css` is the single source of this look.**
  It restyles the shared components (`panel`, `dp-card`, `dp-paper`, `tile`, `tbl`, `stmt`,
  `btn`, `btn-primary`, `pill`, `field`, `tabs`, `pd-dialog`, `notice`), so any page built from
  them is already in the style: use those first. For new markup it also defines `bb-hero`,
  `bb-card`, `bb-bubble` (+ `step`/`ok`/`warn`/`bad`), `bb-avatar`, `bb-input`, `bb-btn`,
  `bb-btn-soft`, `bb-pill`, `bb-summary`. Change the look there, never per page. The Tailwind
  CDN has no `@apply`, so write plain CSS. Page-level Tailwind is for layout (grid, gap,
  spacing) only; when a utility string for colour/shape repeats a third time, add a `bb-*` class.

## Backlog

### 1. Architecture debt

- [ ] `Invoice.order` is required. Make it optional so an invoice (service fee, deposit) needs
      no order, then drop the order created only to carry an invoice.
- [ ] Move invoice views out of `channels/views.py` (1,700 lines, many inline imports and
      `except Exception: pass`) into `tax/`. Keep URLs.
- [ ] Remove the legacy `JournalEntry.payment/refund/payout/expense` links; `source_type` +
      `source_id` replace them.
- [x] Payments: `InvoicePayment`, `BillPayment` and `Expense` all carry `bank_account` now.
      Marketplace/gateway `Payout`s and mitra `SettlementPayment`s carry it too (import forms and
      the settlement payment form ask for the receiving account); a bank statement line quoting a
      payout reference ranks that payout first. All money can now be reconciled per account.
- [ ] Supplier side has no multi-bill payment, supplier credit, or debit note. Mirror
      `tax/receipts.py` and `tax/credit_notes.py` in `accounting/`.
- [ ] Voiding a receipt reverses the cash. Offer "keep as customer credit" instead.
- [x] Marketplace payouts land in a chosen bank account and are matched to the statement line.
- [x] Year-end closing to Laba ditahan (3300) with lock, reopen and a monthly checklist (`accounting/closing.py`).
- [ ] Product variants per warehouse; variants keep a single stock number today.
- [x] One scheduled worker: `manage.py run_books` (ledger backlog, recurring invoices,
      monthly depreciation). It still has to be scheduled (cron) in deployment.
- [ ] Tests run on SQLite. Add a PostgreSQL CI job for the `select_for_update` paths.
- [ ] Fix the four failing test files listed above.

### 2. Features still missing

Tax (first thing Indonesian clients ask for):
- [~] e-Faktur / Coretax export (partial: checked worksheet only, `tax/faktur.py`; XML needs the DJP template confirmed) for Faktur Pajak keluaran and nota retur (only a CSV register
      exists). Confirm the current Coretax import format before building.
- [x] PPh 21 (gaji), PPh 23 (jasa), PPh 4(2) (sewa): withholding on bills and expenses, bukti
      potong, and a utang PPh account.
- [x] SPT Masa PPN worksheet: masukan vs keluaran, kompensasi lebih bayar carried forward.

Sales and purchasing:
- [ ] Surat jalan (delivery order) and proforma from a quote or invoice.
- [ ] Purchase order before the bill; partial receiving against the PO.
- [ ] Send invoice, quote and payment reminder by email and WhatsApp, with a public pay link.
- [ ] Price lists per customer group; payment terms stored on the customer.

Inventory:
- [ ] Stok opname and transfer screens (services exist in `orders/stock.py`, no UI).
- [ ] Push bundle stock to marketplaces; live marketplace APIs (import is file-based).

Accounting:
- [x] Budgets vs actuals; cost centres or tags per brand/outlet.
- [x] Attachments on bills, cash transactions and journals; maker-checker approval.
- [ ] Simple payroll posting to gaji and PPh 21.
- [ ] Multi-currency for sellers who buy in USD/CNY.
- [x] Excel export for every financial report, auditor pack and SAK EMKM PDF. (Other lists still CSV only.)
- [ ] POS module (planned; `channel="pos"` is reserved).

Access:
- [x] Accountant role (read-only plus export), an audit-log viewer per document.

### 3. UI/UX: towards a premium, professional product

Bubble-style rollout (see "Bubble style"): the Bubble layer in `merchant-shell.css` gives every
sidebar page and sub page the blue hero (`.pg-head`, `.bo-heading`, `.bb-hero` are one header),
bubble cards, KPI tiles, pill buttons and pill tabs; checked by screenshot on all 66 sidebar pages
and 16 detail/form pages. Rebuilt in full: `dashboard`, `sales_overview`, `sales_invoices`,
`invoice_form`, `quote_form` (quote + recurring: same two-column layout with summary rail).
Remaining: rebuild the layout (not just the look) of legacy-markup pages such as `settings`,
`whatsapp`, `tax`, `payouts`, `expenses`, `stock`, `orders`, `customers`; check `payout_detail`,
`settlement_detail`, `return_detail`, `channel_detail` (no demo data yet); move `bill_new` and
`credit_note_new` onto the two-column form layout.

Consistency (largest visible gain):
- [x] Rebuilt with the shell components: `bills`, `suppliers`, `trial_balance`,
      `chart_of_accounts`, `journal_new`, `receivables`, and the header and filters of the
      invoice list.
- [x] Every other legacy page now takes the shell palette, buttons, tables and bordered
      controls through the "Legacy bridge" CSS (`expenses`, `payouts`, `finance`, `tax`,
      `stock`, `orders`, `customers`, `dashboard`, returns, settlements…).
- [ ] Still to rebuild properly (the old invoice_new/edit/batch pages are gone): `bill_new`,
      `expenses`, `payouts`, `finance`, `tax`, `stock`, `orders`, `customers`, `dashboard`, and
      the detail pages `settlement_detail`, `payout_detail`, `return_detail`,
      `settlement_partner_detail`, `channel_detail`. Then delete the legacy bridge and the
      `.pd-main .bg-gray-900 { … !important }` overrides and unused `backoffice.css` / `admin.css`.
- [ ] (Deferred by decision) Replace the Tailwind play CDN with a compiled stylesheet.

Data entry:
- [ ] One line-item editor for invoice, quote, bill and credit note: searchable product
      picker (name or SKU, shows stock and price), keyboard add-row, live PPN and total.
- [x] Rupiah inputs that format while typing: `data-rupiah` (shared helper). Applied on the
      new money forms; add it to `invoice_new`, `bill_new`, `expenses`, the pay dialogs.
      Each template still has its own small `num()` for live totals; move it into
      `merchant-ui.js`.
- [ ] Searchable customer and supplier pickers with "tambah baru" inline.
- [ ] Inline validation and keeping the entered values after an error on every form.
- [ ] Autosave drafts for invoices and bills.

Lists and reports:
- [ ] Shared list pattern: sticky header, saved filters, column chooser, bulk actions bar,
      consistent pagination, row click opens the detail.
- [x] Invoice list filter bar cleaned (labelled dates, one "Lainnya" menu).
- [x] The invoice list filters live with htmx (search by invoice, name, phone, product/SKU; period
      presets and custom range; channel; status tabs with counts; amount; removable chips;
      per-device saved views). Any new export or report on invoices must call
      `invoice_filters.apply_filters` / `apply_tab`, never re-implement the filter.
- [ ] Real charts on Ringkasan keuangan and Laba rugi (follow the `dataviz` skill), with
      period comparison and drill-down.
- [ ] Empty states with one clear next action on every list.

Documents:
- [ ] Invoice templates per store: logo, accent colour, bank details, terms, signature;
      the same layout for screen, print and PDF.
- [ ] A public invoice page for the buyer: view, download, pay, confirm transfer.
- [ ] Activity timeline on every document (created, edited, sent, viewed, paid, by whom).

Navigation and feedback:
- [x] Global search across records (topbar, `/` or ⌘K), with recent records and quick actions.
- [x] Keyboard shortcuts, clickable rows, compact density, unsaved-changes guard.
- [x] Toasts, styled confirm dialogs, loading states, progress bar.
- [ ] Onboarding checklist: tax profile, bank accounts, opening balances, first import
      (Beranda has a store-setup checklist but nothing for the books).
- [x] Finance rows in Beranda's "Perlu tindakan": overdue invoices, bills due, bank lines to
      match, recurring invoices ready, unmapped SKUs, depreciation not posted.

Quality:
- [x] Phone layout: under 640px nothing widens the page; wide tables scroll inside their
      card, tiles go two per row. Headless Chrome cannot go below 500px, so test 390px inside
      an iframe. [ ] Turn long tables into cards on phones.
- [x] Focus rings on all controls. [ ] Labels on every input, contrast AA, dialog focus trap.
- [x] Dark mode: ruled out by the design system (light blue and white only).
- [ ] Page-size and query checks on the long lists (N+1 on invoice and bill lists).
