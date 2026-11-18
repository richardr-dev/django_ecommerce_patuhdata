# Patuhdata back office (formerly Lakuin)

Accounting backbone for Indonesian commerce and SMEs: sales from every channel (own store,
Shopee, Tokopedia/TikTok Shop, Lazada, mitra konsinyasi), inventory, invoicing, purchasing, a
double-entry ledger, and Indonesian tax. Django 5.2, server-rendered templates, htmx, Tailwind
play CDN plus `static/css/merchant-shell.css`.


Make sure this is SAP Grade, MYOB Accounting with Modern UI/UX with the **Glacier** (ice blue and white) theme.
Glacier tokens (from `logo/glacier-disc/colors.css`): primary `#0b6fb8`, hover `#095f9e`, deep ink `#012441`, ice `#bfe8fb`, frost `#eaf7fe`, muted `#5b7186`, line `#d6e6f0`. Logo and icons: `logo/glacier-disc/` (copied to `static/img/brand/`). The hex values below written as sapphire `#0066FF` etc. are now these glacier values (tokens live in `merchant-shell.css` `--ds-*`).

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
  The one exception is stok minus, below.
- **Stok minus (negative stock) is a per-store switch, off by default** (`Store.allow_negative_stock`,
  page "Stok minus", service `orders/deficits.py`). Decided rules: the switch is per store only (not per
  product or warehouse); only admin and manager may sell short (`stock.oversell`), automatic jobs never
  do; open minus is a warning at month-end (control `negative_stock`, a Tutup buku step), not a block.
  - The shelf figure never goes below zero. A sale beyond it takes what is there and records the rest as
    a `StockDeficit` at the cost known then (provisional), with a `deficit` movement, so the full
    quantity is traceable. HPP for the full quantity is booked with the invoice as always, so Persediaan
    runs below the shelf value by the open deficits (`inventory.reconciliation` subtracts them).
  - The next goods receipt (`receive_goods`: bill "Terima barang", Penerimaan barang, intercompany)
    covers open deficits first, oldest first: a `deficit_cover` movement takes those units off again and
    `("stock_revalue", cover id)` posts units x (receipt cost - provisional cost) to HPP against
    Persediaan. Only the rest of the receipt joins the moving average.
  - Worked example (pinned by `tests/test_negative_stock.py`): 100 units at Rp60.000, sell 120 → shelf 0,
    minus 20, HPP Rp7.200.000. Receive 50 at Rp65.000 → 20 cover the minus, HPP +Rp100.000 (final
    Rp7.300.000), shelf 30 at Rp65.000 = Rp1.950.000.
  - Refused, never guessed: a product with no cost price, and products with variants. The online
    storefront never sells short. A marketplace import does (the sale already happened) when the switch
    is on. Voiding or editing the sale releases its deficit; units a receipt already gave it return to
    the shelf and the correction is reversed. A manual stock edit on Kelola stok does not cover a deficit.
- **A sale can be made from one chosen location** (`create_sales_invoice(warehouse=)`, `stock_out(warehouse=)`,
  `stock_shortages(store, wants, warehouse)`; tests `tests/test_sale_from_warehouse.py`): the units come from that
  warehouse's shelf only, what another branch holds does not count, a tracked product with no row there is short
  (0 available, not untracked), and stok minus, when allowed, is recorded on that location's level. Without
  `warehouse` the store's locations are drawn in priority order as before. Variants still have one stock figure
  for the whole store, so the parameter does not apply to them yet. This is POS phase 0 (`docs/pos-architecture.md`),
  together with: `create_sales_invoice(number_series="POS")` (own gapless `POS/<year>/000001` series through
  `numbering.next_number`, never the INV series), `tax/services.py:exclusive_price` (PPN backed out of a shelf price
  that includes it), the register accounts 1210 / 4950 / 6155 / 6980 / 7250 in every chart, role `cashier`
  (screen role `viewer`; `pos.sell`, `pos.shift`, `pos.void`; refunds, variance approval, discount override and
  settings need manager or above) pinned to one outlet by `StoreStaff.outlet`, and `Warehouse.cost_center` so POS
  postings land in the outlet's cost centre.
- **Kasir (POS)** (`apps/pos/`: `services.py`, `views.py`; pages Penjualan › Kasir (POS) at `/books/pos/`; design and
  phases in `docs/pos-architecture.md`; tests `tests/test_pos.py`). Phase 0 and phase 1 are built.
  - An outlet is a `Warehouse` with `is_outlet`. A **register** (`PosRegister`) is a cash drawer, not a device, and
    owns a cash account ("Kas kasir <outlet> <code>", created by `create_register`). One open **shift** per register.
  - A **receipt** (`PosSale`) is one ordinary invoice: `pos.sell` calls `create_sales_invoice(channel="pos",
    warehouse=outlet, number_series="POS", rounding=, cost_center=outlet.cost_center, on_credit=True)` and then
    `record_payment` once per tender. POS never writes journal lines and has no lines of its own: the invoice's
    lines are the lines; `PosSale.cart` is only the shelf-price snapshot the receipt prints.
  - Tenders: cash → the register's cash account; `edc` and `qris_pos` (`InvoicePayment.CLEARING_METHODS`) → 1210
    Piutang EDC / QRIS whatever bank account is named, settled later in bank reconciliation; transfer → the bank
    account named. Change comes off the cash tender; non-cash can never exceed the total.
  - **The drawer's expected cash is the ledger balance of the register's account** (`drawer_balance`), never a
    separate tally. Opening a shift with a count that differs from the books needs `fund_from` (the difference is a
    transfer) or is refused. Closing posts counted − expected as `("pos_shift", shift.id)`: short to 6980, over to
    7250, with the outlet's cost centre, then transfers everything above `keep_float` to `deposit_to`.
  - Shelf prices include PPN when `PosOutletSettings.prices_include_tax` and the store is PKP: `pos.quote` backs
    PPN out per line (`exclusive_price`) and the cents left over, plus any Rp100/Rp500 rounding, go to
    `Invoice.rounding` → 4950 (`post_invoice` takes it out of Penjualan). `Invoice.cost_center` is passed to the
    invoice and payment entries.
  - A cashier sells at the shelf price and discounts up to `max_discount_pct`; more needs `pos.discount_override`.
    A cashier (`StoreStaff.outlet`) reaches only that outlet's registers (`registers_for`), lands on `/books/pos/`
    instead of Beranda, and sees only the Kasir link in the sidebar (`merchant_nav.CASHIER_LINKS`).
  - The selling screen (`templates/merchant/pos_sell.html`, `static/js/pos.js`, `static/css/pos.css`) is standalone
    (no sidebar, no Tailwind, no htmx). It posts JSON with a client `idem` key; the same key returns the first
    receipt. The server is the authority on price, stock and total; the script only mirrors the arithmetic.
  - The buyer's e-receipt is `/struk/<token>/` (in `ALWAYS_OPEN`; `/s/` is the campaign short link).
  - Controls `pos_receipts` (every receipt is a paid, posted invoice; 1210 older than 3 days is an info row),
    `pos_shifts` (a shift left open overnight), `pos_voids` (3+ struk voided by one cashier in a day) and
    `pos_variance_trend` (3+ shortage shifts by one cashier in 30 days). `apps/orders/nav_access.py` is generated:
    after adding a role or a sidebar link run `WRITE_NAV_ACCESS=1 pytest tests/test_nav_access.py`.
  - **`/kasir/` is the outlet's own app, separate from the back office** (decided 2026-11-17): its own sign-in
    (`PosLoginView`, `pos/login.html`, the same email-or-WhatsApp account), its own register picker
    (`kasir_home`, `pos/home.html`), and the shift report (`merchant_pos_shift`, now a standalone page, not
    `merchant/base.html`) — no sidebar or topbar at any step, so a tablet bookmarked to `/kasir/` never shows
    the books. It posts to the same endpoints as the admin panel (`merchant_pos_open`, `merchant_pos_sell`,
    `merchant_pos_shift`), so there is one set of business logic, not two apps that could drift. Signing in at
    `/login/` still sends a cashier to `/kasir/` too; `pos-login` ignores `?next=` on purpose. `/books/pos/`
    (the register list) is now admin-only (`pos.settings` or `pos.shift_approve`: admin, manager, finance) for
    creating registers and outlet settings; a plain cashier or `ops` hand gets 404 there and uses `/kasir/`.
  - **Phase 2 built 2026-11-18**: void in the open shift (`pos.void_sale`, reverses the invoice via
    `tax/posting.void_invoice`; a cashier only their own ticket, `pos.refund` any), refund at the register
    (`pos.refund_sale`, a credit note paid from `register.cash_account`, restocking to the outlet via
    `create_credit_note(warehouse=)`), paid-out (`pos.record_paid_out`, an `Expense` off the register's cash
    account) and paid-in (`pos.record_paid_in`, a named-account transfer), approval of a shift variance above
    the store's threshold (`accounting.approvals`, kind `pos_shift_variance`: `PosShift.variance_pending_approval`
    / `variance_request`; the backlog never auto-posts a pending or rejected one), and SPT digunggung with POS
    invoices left out of the Coretax XML (`tax/faktur.is_pos_digunggung`, used by both `tax/spt.py` and
    `tax/faktur.xml_document`).
  - Not built (phases 3 to 4): exchange at the register (waits for variant stock per warehouse), variants at the
    register, outlet price lists, customer picker and loyalty, offline queue, printer agent, device pairing
    (today it is account login, not a paired tablet), kiosk mode (disabling the browser chrome itself).
- **An outlet asking the warehouse for goods is a transfer, not a sale** (`orders/stock.py:transfer_stock`):
  one `transfer_out` movement at the warehouse and one `transfer_in` at the outlet, same quantity. No
  journal entry: the goods stay the same company's Persediaan at the same average cost, so total stock,
  stock value and the books are unchanged; each location's own count changes. A transfer is refused when
  the source lacks free stock (it never creates stok minus). Outlets are `Warehouse` rows with
  `is_outlet`. Not built yet: a request → approve → send → receive screen (only the service exists) and
  variant stock per location. Goods moving to a *different brand* are an intercompany transfer instead.
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
- **Coretax XML** (`tax/faktur.py:xml_document`, button "XML Coretax (uji dulu)" on SPT Masa PPN): `TaxInvoiceBulk` with the
  elements of DJP's Sample Faktur PK Template v1.4 as described by a third-party guide; **not import-tested** and the DJP
  sample/XSD were not read. 11% is written as TrxCode 04 (12% on 11/12 of the base), buyer NPWP/NIK 16 digits, NITKU = id +
  `000000`, line unit must be a Coretax `UM.xxxx` code; invoices that fail are listed, never dropped silently. Retur
  (nota retur) XML is not built.
- **Coretax** (older note): the official import is a DJP Excel template converted to XML by DJP's converter, and its
  field layout is not confirmed in this repo. `tax/faktur.py` therefore produces a checked worksheet (per
  line, buyer NPWP/NIK flagged) and says so; do not claim it is an importable XML until the current DJP
  template (pajak.go.id, Coretax, Template XML dan Converter) has been read and the format tested.
- **Stock movements are append-only** (`InventoryMovement`): `save` on an existing row, `delete`, and the
  queryset `update`/`delete` raise `MovementImmutable`, and on PostgreSQL a trigger refuses raw UPDATE and
  DELETE too (migration `orders/0041`; only deleting a user may blank `actor`). A mistake is a new movement.
  `tests/test_movement_ledger.py` replays every level from its movements.
- **GRNI** (`accounting/receipts.py`): goods that arrive before the supplier's bill are a `GoodsReceipt`:
  stock up, Persediaan / GRNI (2350). The bill is made *from* the receipt (`bill_receipt`): it clears GRNI
  into Utang usaha and books only the price difference to Selisih harga pembelian (5150); voiding that bill
  reopens the receipt. The warehouse role may receive goods but not bill them. `controls.grni_tie` checks it.
- **Double submits**: forms that create money documents carry `<input name="idem" value="{{ idem_key }}">`
  and call `idempotency.claim / finish / release` (a claim older than two minutes with no result is stale).
  A new create form for an invoice, bill, receipt, payment or transfer must do the same.
- **Intercompany** (`accounting/intercompany.py`): brands of one Organization are separate legal entities.
  A transfer moves stock out of one store and into the other at the transfer price and posts one entry in
  each book (seller: 1260 Piutang antar perusahaan / 4150 Penjualan antar perusahaan and HPP / Persediaan;
  buyer: Persediaan / 2360 Utang antar perusahaan). The SKU must exist in both catalogs; the person must be
  admin of both. PPN and consolidation eliminations are not applied. `controls.intercompany_tie` checks the
  group's receivable against payable.
- **PostgreSQL is the production database**; develop and test on it too. `select_for_update` with
  `select_related` over a nullable relation needs `of=("self",)` (SQLite hides this). The customer key
  migration (`shops/0024`) rebuilds the key in SQL on PostgreSQL.
- **Roles come from one table** (`shops/permissions.py`: `MATRIX`, `roles("books.edit")`). A view never
  declares its own role set (`tests/test_permissions.py` fails if one does, and pins the matrix); the
  "Hak akses" page prints the same table. The accountant reads and never writes. **Segregation of duties:**
  `maker_may_not_check` stops the person who created a bill from paying it when someone else could; a
  one-person store is never locked out. Give a new action a permission key, not a new role set.
- **Stores are installed by a partner; there is no registration.** `shops/provisioning.py:install` is the
  only way a store comes to exist, called by `manage.py install_store` and by `/registration/` (platform
  superusers only; 404 for everyone else). One call creates the owner account (no password: a one-time
  invite link sets it), the Organization, the store, main warehouse and shipping method, chart of
  accounts with Kas utama, and the tax profile; `organization=` adds a brand to an installed client.
  Removed and not to be brought back: `/start/` sign-up, "Tambah brand", the marketing home page, the
  store directory `/stores/`, and the Ajak teman pages and `/r/` links (the referral models and bill
  discounts in `billing/referrals.py` remain only so existing bills still compute). Buyer accounts on a
  storefront (`/account/register/`) are part of the online shop and stay.
- **The system's name is one setting**: `PLATFORM_NAME` (default `Patuhdata`). Templates print `{{ platform_name }}`;
  never type the name into a template. The sign-in page (`registration/merchant_login.html`, `static/css/login.css`)
  is a standalone centered card: name, two labelled fields, show-password, one blue Masuk button, forgot-password.
  Domains (`lakuin.store`) and the `lakuin-*.svg` brand files are infrastructure and have not been renamed.
- **Pesanan shows every order by default** (website, manual, cash register and marketplace). The Channel row starts
  with a "Semua" chip; `?sumber=web` narrows to the store's own orders and says so in a banner. The stage tabs live in
  `_orders_list.html` so they refresh with the list, and their counts follow the filters. The three work-queue tabs
  (Perlu dicek, Perlu dikemas, Siap kirim) count only orders the store packs and ships itself
  (`scope.marketplace_orders` excluded): a marketplace moves its own parcels, so a marketplace card has no pack or
  ship buttons, only courier, resi (with copy), the marketplace status and the review flag (`ChannelOrder.attention`).
  "Unggah label resi" (`orders/resi_labels.py`, `label_views.py`) reads PDF labels with pypdf, no OCR engine.
- **The back office lives under `/books/`** (it was `/merchant/`, a word that suits a shop, not an accounting
  product). Old `/merchant/...` addresses redirect (`config/views.py:legacy_merchant`: 301 for GET, 308 for
  anything else, `/merchant/login/` goes to `/login/`), so bookmarks, invite emails and forms in flight keep
  working. Route names (`merchant-*`), template folders and view functions keep the old word on purpose: nobody
  sees them. A new back-office route is `path("books/...")`. Dropping the prefix entirely was ruled out: it
  collides with `orders/<id>/`, the site root and storefront paths (`products`, `feeds`, `integrations`).
- **The first page is the login** (`HOME_IS_LOGIN`, default 1; `tenancy.opens_on_login`): `/` on the system's
  own address redirects to `/login/` (or the dashboard when signed in), and the storefront gate lets
  it through. A store's own subdomain or custom domain, and `?store=slug` links ("Lihat toko"), keep the
  storefront; `/start/` and `/stores/` stay reachable. Tests run with it off (`tests/conftest.py`).
- **Purchasing documents** (`accounting/purchasing.py`, `supplier_notes.py`, `stock_counts.py`,
  `orders/stock_requests.py`; permissions `purchase.order`, `stock.count`, `stock.count_approve`,
  `stock.request`, `stock.request_approve`, `stock.send`): a purchase order books nothing; each goods receipt
  against it adds to `received_quantity` (never above the order) and posts Persediaan / GRNI; the bill is
  made from the receipt. A debit note takes Utang usaha down and returns stock at the bill's price, never
  above what is unpaid. A stok opname applies its difference on top of today's stock (the system quantity is
  frozen when counting starts) and posts 6960 / Persediaan on approval by a different person. A stock
  request moves stock out at *send* (in transit, counted by `inventory.reconciliation`) and in at *receive*;
  missing units are a loss in transit at cost. None of these edit or delete a posted document: correct with
  a new document.
- **The product is the books; the online shop is one optional sales channel** (decided 2026-11-16). What a
  partner installs is a *Perusahaan* (PT, CV, UD, distributor, personal trading business): say "perusahaan" on
  screen wherever the business is meant, and "toko online" only for the storefront. Code keeps `Store`,
  `merchant-*` and `store` on purpose. The sidebar follows an accounting product: Penjualan, Pembelian,
  Kas & bank, Persediaan, Akuntansi (Buku besar, Tutup buku & kontrol, Laporan, Pajak), then Saluran penjualan
  (Marketplace, Mitra, Toko online, Promosi), Pelanggan, Pengaturan. Beranda opens with the books
  (`_books_summary`: kas & bank, piutang, utang, laba bulan ini) for roles holding `books.view`.
- **Toko online is its own module** (`/books/toko-online/`, `orders/views.py:merchant_online_store`,
  `templates/merchant/online_store.html`): status, address, the setup checklist and the open/close switch
  live there, never on Beranda or in the sidebar foot. `Store.is_open` is enforced: off means
  `store_state` is "closed" (shoppers browse, no new orders), same as a lapsed subscription. The switch is
  admin/manager only and is logged (`store.opened` / `store.closed`).
- **The built-in online shop is switched off by default** (`STOREFRONT_ENABLED`, default 0; decided 2026-11-16:
  the system does not carry shopper traffic, online selling goes through connected channels such as
  marketplaces and, planned, Shopify with stock sync from Patuhdata). Off means: shopping paths
  (`availability.SHOPPING`: catalogue, cart, checkout, shopper account pages, sitemap, feed, `/api/`) answer
  410 with `storefront/unavailable.html`; `/` and `/account/login/` go to `/login/`; shopper sign-up shows the
  sales page; the sidebar hides the Toko online and Promosi groups (`Group.when="storefront"`). Still served:
  `/books/`, buyers' existing orders (`/orders/<id>/`), invoice links (`/i/`), payment webhooks, password reset.
  The code is kept, not deleted; tests run with it on (`tests/conftest.py`), `tests/test_no_storefront.py`
  covers off. A new shopper-facing route must be added to `SHOPPING`.
- **Price lists and payment terms** (`catalog.PriceList`/`PriceListItem`, `Customer.price_list`,
  `Customer.payment_terms_days`; service `tax/pricing.py`; pages Penjualan › Daftar harga): a customer's list prices
  the invoice form's lines in the browser (`_catalog` sends `lists` per product, `price_lists` the discounts; the
  mitra price list still wins when "Jual ke mitra" is chosen) and a credit invoice posted without a due date takes
  the customer's terms on the server (`due_date_for`). A product without a row sells at retail less the list's
  discount. Quotes do not apply price lists yet.
- **Sending documents** (`tax/sharing.py`, `tax/share_views.py`): invoices and quotes have a buyer link
  (`/i/<token>/`, `/q/<token>/`, both in `ALWAYS_OPEN`), a WhatsApp link, email, and, when the store connected its
  own WhatsApp account (`StoreWhatsAppSettings.ready`), a direct send (kinds `invoice`, `invoice_reminder`,
  `quote` in `integrations/whatsapp.KINDS`). The buyer answers a quote on its link (accept/decline → status,
  `responded_at`, `response_note`) and presses "Saya sudah transfer" on an invoice (`transfer_claimed_at`,
  Beranda row "konfirmasi transfer dari pembeli"). **Automatic reminders** (`tax/reminders.py`, `ReminderPolicy`
  per store from Piutang pelanggan › Pengingat otomatis, `ReminderLog` one row per invoice/step/channel): run by
  `run_books`; email to the customer's address, WhatsApp through the store's account; never twice.
- **Proforma** (`merchant-quote-proforma`, `tax/proforma.html`): printed from a quote, numbered `PRO-<quote>`,
  says it is neither a faktur pajak nor a receipt, shows the bank accounts. Books nothing. Surat jalan prints from
  an invoice (`merchant-delivery-note`); a goods-issue document of its own is still missing (architecture item 2).
- **Sales order → surat jalan → invoice** (`tax/sales_orders.py`, `SalesOrder`/`Delivery`; pages Penjualan › Pesanan
  penjualan and Surat jalan): an order books nothing and holds no stock (goods only; services are invoiced
  directly). A delivery takes the units off the shelf (`stock_out`, movement kind `issue`) and posts
  1410 Barang terkirim belum ditagih / Persediaan at the product's cost. An invoice made from deliveries
  (`invoice_deliveries` → `create_sales_invoice(delivered_costs=…)`, `Invoice.from_deliveries`) books the sale and HPP
  against **1410**, takes no stock, locks its amounts, and when voided only releases its deliveries (the goods
  stay with the buyer). Cancelling a delivery needs it uninvoiced; it returns the stock and reverses the entry.
  Partial deliveries leave a backorder (`backorder()`), "Tutup pesanan" drops it. `controls.delivered_tie`
  checks 1410 against uninvoiced deliveries. A quote becomes an order with "Jadikan pesanan penjualan".
  Not built: reserving stock on an order, delivering from a chosen warehouse, variants on an order.
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
| Install a client's store or add a brand (partner only) | `shops/provisioning.py`, `manage.py install_store` | `shops/install_views.py`, `templates/platform/install.html` |
| Reports (P&L, cash flow, neraca, account ledger) | `accounting/reports.py` | both |
| Excel for each report, auditor yearly pack (`/books/accounting/paket-audit/`) | `accounting/exports.py` | `accounting/views_books.py` |
| SAK EMKM statements (screen, print, PDF) | `accounting/emkm.py` | `views_books.py:merchant_emkm_pack`, `templates/merchant/emkm_pack.html` |
| Year-end closing, reopen, monthly close checklist | `accounting/closing.py` | `views_books.py:merchant_year_close`, `templates/merchant/year_close.html` |
| PPh 21/23/4(2) withholding, bukti potong, deposit | `accounting/withholding.py` | `views_books.py:merchant_withholdings` |
| SPT Masa PPN worksheet with kompensasi | `tax/spt.py` | `tax/views.py:merchant_spt_ppn` |
| Faktur Pajak Keluaran worksheet for the Coretax template | `tax/faktur.py` | `tax/views.py:merchant_faktur_export` |
| Goods received before the bill (GRNI 2350), bill made from a receipt | `accounting/receipts.py` | `accounting/views_receipts.py` |
| Sell stock between two brands of one Organization (two books) | `accounting/intercompany.py` | `accounting/views_intercompany.py` |
| Who may do what (matrix, Hak akses page, segregation of duties) | `shops/permissions.py` | `shops/staff_views.py:merchant_access` |
| Stok minus: switch, open list, cover on receipt, HPP correction | `orders/deficits.py` | `orders/deficit_views.py`, `templates/merchant/stock_minus.html` |
| Purchase orders, debit notes, multi-bill payment | `accounting/purchasing.py`, `supplier_notes.py` | `views_purchasing.py`, `views_supplier.py` |
| Stok opname document, outlet stock requests | `accounting/stock_counts.py`, `orders/stock_requests.py` | `views_counts.py`, `orders/stock_request_views.py` |
| Stock card, slow-moving stock, inventory close picture | `accounting/inventory_reports.py` | `views_inventory.py` |
| Adjusting entries, quarter report, quarter close, loans | `accounting/adjustments.py`, `reports.py`, `closing.py`, `loans.py` | `views_adjust.py`, `views_quarter.py`, `views_loans.py` |
| Annual badan worksheet, tax calendar | `tax/fiscal.py`, `tax/tax_calendar.py` | `tax/views_annual.py` |
| Duplicate-submit guard for money forms | `accounting/idempotency.py` | invoice form, bill form, receipts, transfers |
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
| Kasir (POS): registers, shifts, receipts, Z report, e-receipt | `pos/services.py` | `pos/views.py`, `templates/merchant/pos*.html`, `templates/pos/receipt.html` |
| Consignment (mitra) | `settlements/consignment.py` | `settlements/views.py` |

### Account codes

1100–1199 cash and bank (one per `BankAccount`), 1200 gateway, 1250 piutang usaha,
1300 marketplace, 1400 persediaan, 1450 persediaan di mitra, 1500 PPN masukan, 1600/1690 aset
tetap and akumulasi, 2100 PPN keluaran, 2200 belum teridentifikasi, 2300 utang usaha,
2400 kredit pelanggan, 2500 pinjaman, 3100 modal, 3200 prive, 4100 penjualan, 4200 ongkir,
4800 retur, 4900 potongan, 4950 pembulatan kasir, 5100 HPP, 6100 biaya gateway, 6155 MDR EDC/QRIS,
62xx–69xx biaya operasional, 6950 penyusutan, 6980 selisih kas kasir, 7100/7200 pendapatan lain,
7250 lebih kas kasir, 8100 biaya bank, 1210 piutang EDC/QRIS (POS, `docs/pos-architecture.md`).

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

**One blue header per page (decided).** The page's blue header (`pg-head` / `bb-hero`) is the only blue bubble. The top bar is a
flat strip on the canvas (search, Buat baru, account), not a second floating card; the first KPI tile (`tile accent`) and
the form summary rail (`bb-summary`) are white with a blue figure and blue ring; a page never stacks a second `bb-hero`
under its `pg-head`. Blue gradient fills stay for the header and primary buttons only.

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

## Architecture check against `architecture.md` (checked 2026-11-13, updated after purchasing work)

Verdict: sufficient for a company (or a few brands) trading goods through its own store and marketplaces,
with real books and warehouse control. Not yet the full ERP baseline: sales-side documents, units of
measure/lots and database-level isolation remain. Deliberately not adopted: React/Next.js, DRF as the main
interface, Celery/Redis, microservices, the `core/ master_data/ ...` folder layout.

**Met**
- Modular monolith on Django + PostgreSQL (dev, CI, production); suite passes on PostgreSQL and SQLite.
- Double-entry ledger as the only money record, reversal instead of edit, one live entry per source
  document, a backlog poster per module.
- Perpetual inventory, moving average cost, cost booked with the sale; append-only movement ledger
  (app + PostgreSQL trigger); balances replayable from movements; reservations; row locks.
- Hybrid negative stock with provisional cost and revaluation on receipt (store switch, admin/manager).
- Purchasing chain: purchase order → partial goods receipts (GRNI) → bill from the receipt, matched three
  ways (ordered / received / billed) with price differences to 5150; overdue orders in the checks.
- Supplier side: debit note (retur ke supplier: stock out, Utang usaha down, PPN masukan reversed),
  one payment over several bills, goods receipt before bill.
- Warehouse control: stok opname as a document (frozen system quantity, second-person approval, shrinkage
  journal to 6960); outlet/warehouse stock requests (request → approve → send → receive) with goods in
  transit and a loss in transit booked; transfers between brands with an entry in each book.
- Inventory reports: stock card per item (+Excel), slow-moving stock, "Penutupan persediaan" picture and a
  month-checklist step; reconciliation counts goods in transit, deficits and unreceived bills.
- Period control: `LedgerLock`, year-end closing to 3300, month-end checklist, reopen by admin only.
- Idempotency keys on every create form; audit log per document; maker-checker approvals; one role matrix
  with segregation of duties on bill payment, stock counts and stock requests.
- Reports from the ledger: P&L, neraca, arus kas, neraca saldo, buku besar, Excel, SAK EMKM, audit pack.
- Transactional outbox for integrations (`integrations.OutboxEvent`); ledger posting is in-transaction
  with the backlog as the safety net.

**Partly met**
- Negative stock policy: store level and role only; no warehouse/product rule, no "critical" approval tier,
  variants excluded, a manual stock edit does not cover a deficit.
- Valuation: one cost per product per store. No per-warehouse pool, no cost layers, no landed cost
  (freight, duty). A returned item leaves at the bill's price without recomputing the average.
- Debit note: only up to what is unpaid on the bill (money already paid is a cash entry, no supplier
  credit balance); goods lines without variants only; no void (correct with a manual journal).
- Stock count and request flows: product (not variant) level, one warehouse level per product.
- Idempotency: forms only; the JSON API and stock services have no key of their own.
- Isolation between stores: enforced in views and services; no composite keys or Row-Level Security.
- Fiscal periods: one lock date per store, not a period table; account codes are constants, not a
  configurable posting-rule table.
- Tests: example-based, including the architecture's worked examples; no property-based tests and no
  two-connection concurrency test for stock.

**Missing**
1. Sales order with partial fulfilment and backorder (an invoice takes all its stock at once; "incoming"
   and "backordered" are not tracked).
2. Goods issue / surat jalan as its own stock-moving document.
3. Units of measure and conversions (box/pcs), decimal quantities, lots/batches, serial numbers, bins.
4. Variant stock per warehouse.
5. Database-level tenant isolation (RLS or composite keys); branches as an entity under a company.
6. Consolidation: group reports with intercompany elimination; PPN on intercompany sales.
7. Monitoring (Sentry needs a DSN), S3 media in production, a backup restore drill.

**Suggested order**: 1 and 2 (sales side) → 4 → 3 only when a client needs it → 5 before hosting unrelated
clients in one database → 6.

## Accountant's quarter (built 2026-11-13)

What an accountant does at a quarter, and where it lives:
- **Adjusting entries** (`accounting/adjustments.py`, page "Jurnal penyesuaian"): prepaid expenses (1550)
  amortised monthly, accruals (2310) with automatic reversal on day one of the next period, recurring
  journals, and the receivable provision (1255 contra-asset, 6970 expense) trued up from the aging buckets with
  editable rates. All idempotent, run by `run_books`.
- **Quarter statements** (`reports.quarterly_profit / equity_statement / cash_flow_indirect`, page "Laporan
  kuartal", `exports.quarter_workbook`): Q1..Q4 side by side, laba rugi vs previous quarter, neraca, indirect
  cash flow (must equal the movement of the cash accounts: `difference` is shown and tested), changes in equity
  (closing taken from the neraca), Excel and print. Closing entries are excluded as everywhere else.
- **Quarter close** (`closing.quarter_checklist`, "Tutup kuartal" tab): the month checklist for the last month
  plus adjusting entries run, provision reviewed, statements tie out, three SPT Masa PPN checks, and a button
  that locks the books to the quarter end.
- **Loans** (`accounting/loans.py`, page "Pinjaman"): annuity or flat schedule, drawdown entry, installments
  paid in order and split into principal (2500) and interest (8200). Interest is booked when paid; accrue to
  period end with a recurring accrual journal.
- **Tax** (`tax/fiscal.py`, `tax/tax_calendar.py`): SPT Tahunan badan worksheet (commercial profit → manual
  koreksi fiskal and credits → PKP rounded down to thousands → PPh at 22% or the Pasal 31E reduction → PPh 29,
  PPh 25 estimate) for companies not on PPh Final; a tax calendar per month with amounts from the books and a
  filing tracker (NTPN/BPE); due and overdue duties appear on Beranda.

Still not built: statement of cash flows indirect is in; **bank loan interest accrual is manual**; no
foreign currency; no fiscal loss carry-forward ledger (it is a manual negative correction); no individual
(orang pribadi) annual computation; adjusting journals are not subject to maker-checker approval above a
threshold (only the one-off manual journal is).

Legacy pages still on the old markup (scan of `templates/merchant` for `bo-card`, `kpi-grid`, `data-table`,
`bo-heading`, `bo-button`, `entity-form`, Tailwind `bg-gray-*`): _customer_history, _customers_list, _orders_list, abshop, billing_demo_pay, customer_detail, customers, entity_form, import, mitra_invoice_list, mitra_invoice_new, order_detail, order_new, order_payment_link, orders, product_partner_prices_partial, sales, sales_stats_partial.
The whole Pengaturan menu (settings, payments, delivery, WhatsApp, integrations, tracking, staff, access, activity, billing)
is rebuilt. The rest work and take the blue palette; large ones need care: `entity_form` (product form), `sales` (htmx
replaces the whole page).

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
- [ ] Supplier side has no multi-bill payment, supplier credit, debit note or purchase return. Mirror
      `tax/receipts.py` and `tax/credit_notes.py` in `accounting/`.
- [ ] Voiding a receipt reverses the cash. Offer "keep as customer credit" instead.
- [x] Marketplace payouts land in a chosen bank account and are matched to the statement line.
- [x] Year-end closing to Laba ditahan (3300) with lock, reopen and a monthly checklist (`accounting/closing.py`).
- [ ] Product variants per warehouse; variants keep a single stock number today.
- [x] One scheduled worker: `manage.py run_books` (ledger backlog, recurring invoices,
      monthly depreciation). It still has to be scheduled (cron) in deployment.
- [x] The suite passes on SQLite and PostgreSQL (CI has a `postgres` job). Local dev now runs on PostgreSQL too (`.env` `DATABASE_URL`); `db.sqlite3` is kept untouched as the fallback.
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
- [x] Purchase order before the bill; partial receiving against the PO.
- [ ] Send invoice, quote and payment reminder by email and WhatsApp, with a public pay link.
- [ ] Price lists per customer group; payment terms stored on the customer.

Inventory:
- [x] Stok opname and warehouse/outlet transfer screens (stock count document, stock requests).
- [ ] Push bundle stock to marketplaces; live marketplace APIs (import is file-based).

Accounting:
- [x] Budgets vs actuals; cost centres or tags per brand/outlet.
- [x] Attachments on bills, cash transactions and journals; maker-checker approval.
- [ ] Simple payroll posting to gaji and PPh 21.
- [ ] Multi-currency for sellers who buy in USD/CNY.
- [x] Excel export for every financial report, auditor pack and SAK EMKM PDF. (Other lists still CSV only.)
- [~] POS module: phases 0, 1 and 2 built 2026-11-17/18 (`apps/pos/`); phases 3 to 4 open, see the Kasir (POS) rule above. Architecture decided in `docs/pos-architecture.md`: outlet = `Warehouse.is_outlet`, register = drawer with its own cash account, shift with variance to 6980 (now with approval above threshold, §12), one invoice per receipt through `create_sales_invoice(warehouse=)`, tenders as `InvoicePayment` (EDC/QRIS to 1210), per-outlet cost centre; prerequisites in its §4.

Access:
- [x] Accountant role (read-only plus export), an audit-log viewer per document.

### 3. UI/UX: towards a premium, professional product

Bubble-style rollout (see "Bubble style"): the Bubble layer in `merchant-shell.css` gives every
sidebar page and sub page the blue hero (`.pg-head`, `.bo-heading`, `.bb-hero` are one header),
bubble cards, KPI tiles, pill buttons and pill tabs; checked by screenshot on all 66 sidebar pages
and 16 detail/form pages. Rebuilt in full: `dashboard`, `sales_overview`, `sales_invoices`,
`invoice_form`, `quote_form` (quote + recurring: same two-column layout with summary rail).
Rebuilt since: `tax` (PPN & PPh), `payouts`, `bill_new` (numbered sections + summary rail, keeps typed values after an error), `expenses`
(month pills, tiles, table, entry in a dialog), `stock` (one form per row). Remaining: rebuild the layout (not just the look) of
legacy-markup pages such as `settings`, `whatsapp`, `finance`, `orders`, `customers`; check `payout_detail`,
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
