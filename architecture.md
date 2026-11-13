Final Inventory Architecture — Multi-Company Accounting System
Architecture decision finalized
 Django + PostgreSQL


My recommendation is to build a perpetual inventory system with Moving Weighted Average Cost (Moving Average), controlled negative stock, immutable inventory movements, and automatic double-entry accounting integration.

This architecture is designed for Indonesian trading companies, wholesalers, distributors, retailers, and multi-company business groups.

The most important design principle is:
Commercial transactions should be flexible. Physical inventory must be traceable. Financial accounting must remain balanced and auditable.

We should not build inventory as a simple feature inside the product or invoice module. It deserves its own transaction engine and subledger.
1. Final architecture decisions
These are the decisions I would lock before development.
Component	Final decision
Architecture	Modular monolith
Backend	Django + Django REST Framework
Database	PostgreSQL
Accounting model	Double-entry accounting
Inventory model	Perpetual inventory
Costing	Moving Weighted Average
Negative inventory	Hybrid
Stock reservation	Supported
Partial fulfillment	Supported
Backorders	Supported
Multiple warehouses	Supported
Multiple companies	Supported
Intercompany transfers	Supported
Inventory valuation	Real-time subledger
Audit trail	Immutable transaction ledger
Financial posting	Event-driven within transactional boundaries
Reporting	Inventory subledger + General Ledger

I would not introduce microservices yet. A modular monolith gives you transactional consistency without making solo development unnecessarily difficult.
2. Overall system architecture
The inventory system must communicate with Sales, Purchasing, Warehouse, and Accounting.
Enterprise accounting architecture
CORE BUSINESS APPLICATION

Company / Branch / User / Permissions

Sales Module

Orders, invoices, returns


Purchasing Module

PO, receipts, bills


Warehouse Module

Picking, packing, delivery


Inventory Module

Stock, reservations, valuation



Business events and posting services

INVENTORY TRANSACTION ENGINE

Movement ledger · Cost engine · Reservation engine · Reconciliation


ACCOUNTING ENGINE

Journal Entries · General Ledger · Trial Balance


FINANCIAL REPORTING

Balance Sheet · Profit & Loss · Cash Flow · Inventory Valuation

All modules share one controlled database, but each module owns its business logic and records.
One rule: Sales must not directly update inventory balances, and Inventory must not directly manipulate the General Ledger.

They communicate through controlled application services.

3. Database architecture
I would organize the database into eight functional groups.
A. Multi-company foundation
Every business transaction must belong to a legal entity.
Table	Important fields
companies	id, name, tax_id, currency_id, status
branches	id, company_id, name
warehouses	id, company_id, branch_id, name
users	id, email, status
company_users	user_id, company_id, role
fiscal_periods	company_id, start_date, end_date, status

Important design decision:

A group can own multiple legal entities, but each entity maintains its own accounting books.

For example:
Example: Business Group
Holding Company

PT Trading A

Company ID: A

Warehouse Jakarta

Warehouse Surabaya
Own ledger


PT Distribution B

Company ID: B

Warehouse Bandung

Warehouse Semarang
Own ledger



Company A cannot accidentally post inventory movements into Company B's inventory or books.

Use company_id on every company-owned business table, including movement records, journals, products, warehouses and reservations.

Use composite foreign keys or equivalent database-level constraints to prevent cross-company references. PostgreSQL Row-Level Security can provide an additional isolation layer.
B. Product master
Table	Purpose
products	Product identity
product_variants	Size, color, model
product_categories	Product classification
units_of_measure	PCS, BOX, KG, LTR
product_uom_conversions	Box-to-piece conversion
product_barcodes	Barcode mapping
product_company_settings	Company-specific valuation and stock rules

A product can be shared in a group catalog, but its stock and valuation remain company-specific.

For example, Product A might be purchased at Rp50,000 by PT Trading A and Rp55,000 by PT Distribution B.

These must never be merged into one inventory valuation.
C. Inventory configuration
Table	Purpose
inventory_policies	Negative stock policy
inventory_locations	Warehouse bin/rack
inventory_lots	Batch tracking
inventory_serial_numbers	Serialized products
inventory_ownership	Company ownership of stock
inventory_valuation_pools	Moving average valuation scope

My default valuation scope:

Company + Product + Inventory Ownership.

This means all warehouses belonging to one company share the same moving-average cost for a product.

Physical quantities remain warehouse-specific.

A warehouse transfer therefore does not change the company's average inventory cost.

For products that require warehouse-specific costing, the valuation pool can be configured separately.
D. Inventory transaction tables
These are the most important tables in the entire inventory system.
Table	Purpose
inventory_documents	Document header
inventory_document_lines	Document detail
inventory_movements	Immutable stock movement ledger
inventory_balances	Current quantity snapshot
inventory_reservations	Allocated stock
inventory_reservation_lines	Allocation details
inventory_cost_layers	Cost allocation and historical traceability
inventory_cost_adjustments	Cost revaluation
inventory_stock_counts	Physical stock count
inventory_adjustments	Approved discrepancies

The most important distinction:
- inventory_movements is the historical source of truth.
- inventory_balances is a performance-oriented current-state projection.
- inventory_cost_layers supports cost traceability and revaluation.
Never treat the balance table as the only record of stock.
E. Accounting tables
Table	Purpose
chart_of_accounts	Account definitions
journal_entries	Journal header
journal_entry_lines	Debit and credit lines
accounting_periods	Period control
posting_rules	Event-to-account mapping
subledger_reconciliations	Inventory vs GL reconciliation

Every posted journal entry must balance.
\[
\sum Debit = \sum Credit
\]

The inventory system must not independently create arbitrary journal entries. It must use the accounting posting service.
F. Sales and purchasing integration
Table	Purpose
sales_orders	Customer orders
sales_order_lines	Ordered quantities
invoices	Customer billing
invoice_lines	Billed quantities
goods_issues	Actual stock dispatch
purchase_orders	Supplier orders
purchase_receipts	Actual received quantities
supplier_bills	Supplier invoices
sales_returns	Customer returns
purchase_returns	Supplier returns

This separation is essential.

An invoice is not a goods issue.

A purchase order is not a goods receipt.
G. Integration and reliability
Table	Purpose
business_events	Event history
outbox_events	Events awaiting processing
idempotency_keys	Duplicate request prevention
audit_logs	User activity
posting_errors	Failed accounting postings

These tables help prevent duplicate deductions, lost accounting events, and silent synchronization failures.
H. Reporting
Build reports from the subledgers and accounting records rather than independently maintaining financial numbers.
- Stock on hand.
- Available stock.
- Reserved stock.
- Negative inventory report.
- Inventory valuation.
- Stock movement history.
- Slow-moving inventory.
- Stock adjustment report.
- Inventory vs GL reconciliation.

4. Inventory movement engine
This is the heart of the system.

Every operation that changes inventory must generate a movement record.

I recommend five movement categories:
Movement	On-hand effect
Purchase receipt	+
Sales goods issue	-
Warehouse transfer	- at source, + at destination
Inventory adjustment	+ or -
Customer/supplier return	+ or - depending on disposition

A movement should never be silently deleted or edited after posting. Corrections should be made through reversal and replacement transactions.
Movement data structure
A movement record should contain:
class InventoryMovement:    id    company_id    product_id    warehouse_id    location_id    movement_type    quantity    base_uom_id    unit_cost    total_cost    source_document_type    source_document_id    source_document_line_id    movement_date    posted_at    status    created_by



Additional fields I would include:
- valuation_pool_id
- lot_id
- serial_number_id
- reversal_of_id
- idempotency_key
- currency_id where applicable
- cost_status (final or provisional)
The actual implementation should use Django models with DecimalField, not floating-point numbers.
Example: Sales of 120 units with only 100 available
Inventory engine simulation
Initial stock
100

Sales quantity
120


On-hand after delivery-20

Negative inventory exception
The transaction is permitted under the hybrid policy, subject to permission and configured cost handling.

The engine must record the entire 120-unit movement. It must not silently reduce the movement to 100 just to avoid negative stock.

This is important because the actual physical dispatch must remain traceable.

5. Hybrid negative inventory policy
I recommend implementing three levels of control.
Level	Action	Permission
Normal stock	Allow issue	Warehouse staff
Negative stock	Allow with warning	Authorized warehouse user
Critical negative stock	Require approval	Warehouse manager

You should also configure the policy at multiple levels:
1. Company level: default policy.
2. Warehouse level: operational override.
3. Product level: stricter or more permissive exception.
4. User level: authorization.
The most restrictive applicable rule should win.
What happens when stock becomes negative?
The system must handle two separate problems:
- Quantity is negative.
- Inventory valuation may be temporarily uncertain.
Do not confuse them.

A negative quantity can be operationally valid in the system, while its valuation needs provisional treatment.
6. Moving Weighted Average valuation engine
This is where the accounting logic becomes interesting.

Your system will use a perpetual moving average method.

The formula is:
\[
WAC = \frac{\text{Inventory Value}}{\text{Inventory Quantity}}
\]

When new goods are received:
\[
New\ WAC =
\frac{Old\ Value + Received\ Value}
{Old\ Quantity + Received\ Quantity}
\]

Example A: Normal purchase receipt
Opening stock:
- 100 units.
- Rp50,000/unit.
- Total inventory value: Rp5,000,000.
Purchase:
- 100 units.
- Rp60,000/unit.
- Total purchase value: Rp6,000,000.
Moving average calculation
Opening value

Rp5,000,000

Purchase value

Rp6,000,000


Total units
200

Total value
Rp11,000,000

NEW MOVING AVERAGE
Rp55,000
Per unit

Now if you sell 50 units:
- COGS = 50 × Rp55,000.
- COGS = Rp2,750,000.
- Remaining inventory = 150 × Rp55,000.
- Remaining inventory value = Rp8,250,000.
Example B: Negative stock and subsequent purchase
This is the scenario your system must handle correctly.

Assume:
1. Opening stock: 100 units at Rp60,000.
2. Sales delivery: 120 units.
3. Negative stock: 20 units.
4. Subsequent purchase: 50 units at Rp65,000.
For the initial 120-unit issue, the system uses the last known moving average of Rp60,000.

Initial COGS:
\[
120 \times 60,000 = Rp7,200,000
\]

After the goods issue:
Account	Quantity/value
Inventory quantity	-20
Inventory value	-Rp1,200,000
COGS	Rp7,200,000

The negative inventory position has been recorded using provisional costing.

Now the purchase arrives:
\[
50 \times 65,000 = Rp3,250,000
\]

The first 20 units of the receipt cover the negative inventory.

The actual cost is Rp65,000 rather than Rp60,000.

Therefore, you need a cost adjustment:
\[
20 \times (65,000-60,000)
= Rp100,000
\]

Negative stock revaluation
Item	Amount
Original provisional COGS	Rp7,200,000
Cost adjustment	+Rp100,000
Final COGS	Rp7,300,000


Final inventory
Remaining units
30

Inventory value
Rp1,950,000

Moving average
Rp65,000

Valuation reconciled

This adjustment must also be posted to the General Ledger.

Important implementation rule: Keep provisional costing separate from final costing. When receipts cover negative quantities, revalue the affected prior issues using the actual receipt cost, including eligible landed costs.

If no reliable previous cost exists, require a configured provisional cost source (for example, standard cost or last purchase cost). If no source exists, put the issue into an unvalued exception queue and require approval rather than inventing a cost.

For an operationally urgent dispatch, you can still record the physical movement while keeping its financial valuation pending. The financial close must not treat that pending valuation as final.
Moving average algorithm
The inventory valuation service should follow this sequence:
def receive_inventory(product, quantity, unit_cost):    pool = lock_valuation_pool(product)    if pool.quantity < 0:        shortage = abs(pool.quantity)        matched_qty = min(quantity, shortage)        revalue_negative_issues(            pool=pool,            receipt_quantity=matched_qty,            actual_unit_cost=unit_cost        )        remaining_qty = quantity - matched_qty        add_remaining_inventory(            pool=pool,            quantity=remaining_qty,            unit_cost=unit_cost        )    else:        add_remaining_inventory(            pool=pool,            quantity=quantity,            unit_cost=unit_cost        )    recalculate_average(pool)



This is conceptual pseudocode. The actual implementation must also handle concurrent transactions, returns, landed costs, reversals and partial receipts.

7. Automatic accounting journal posting
The inventory engine must integrate with your accounting engine.

I would create a centralized posting rules configuration.
A. Purchase receipt
Assume purchase receipt of goods worth Rp10,000,000.
Account	Debit	Credit
Inventory	10,000,000	-
Goods Received Not Invoiced (GRNI)	-	10,000,000

Why GRNI?

Because goods may arrive before the supplier invoice.

You recognize the inventory and corresponding liability/accrual separately from supplier billing.
B. Supplier invoice received
Account	Debit	Credit
GRNI	10,000,000	-
Accounts Payable	-	10,000,000

The actual payable, VAT input and purchase price differences should be handled according to the supplier invoice and applicable tax treatment.
C. Sales delivery
Assume 50 units at Rp55,000 each.
Account	Debit	Credit
Cost of Goods Sold	2,750,000	-
Inventory	-	2,750,000

D. Customer invoice
Assume 50 units at Rp100,000 each, excluding VAT for this simplified example.
Account	Debit	Credit
Accounts Receivable	5,000,000	-
Sales Revenue	-	5,000,000

VAT must be handled through separate configurable tax rules.
E. Inventory adjustment
Suppose a stock count reveals 5 missing units at Rp55,000 each.
Account	Debit	Credit
Inventory Shrinkage Expense	275,000	-
Inventory	-	275,000

The account used depends on the reason for the discrepancy, materiality, and accounting policy.
F. Negative stock cost adjustment
From our earlier example:
Account	Debit	Credit
Cost of Goods Sold	100,000	-
Inventory	-	100,000

This increases the actual cost of goods sold and corrects the residual inventory valuation.

All these postings should be generated by the accounting posting service, not manually assembled by the sales or warehouse modules.

8. Stock reservation engine
This deserves its own module because stock reservation and physical inventory are not the same thing.

I would implement the following quantities:
Quantity	Meaning
On-hand	Recorded physical stock
Reserved	Allocated to customer orders
Available	On-hand minus reserved
Incoming	Expected purchase receipts
Backordered	Customer demand not yet fulfilled

Example:
Metric	Quantity
On-hand	100
Reserved	30
Available	70
Incoming	200
Backordered	50

The formula:
\[
Available = OnHand - Reserved
\]

Do not subtract incoming stock from available stock until it is physically received.

You can display Projected Available separately:
\[
Projected = OnHand - Reserved + Incoming
\]

Reservation workflow
Sales Order: 120 units


Reservation Engine

Check available quantity

100 units

Reserved


20 units

Backordered




Warehouse Fulfillment

Pick and dispatch available goods


A reservation should not generate an accounting journal because it does not change asset ownership or physical inventory.

When goods are issued, the reservation is consumed and the stock movement is created.

9. Reconciliation engine
This is what separates a serious accounting system from a simple inventory application.

You need three reconciliation layers.
A. Quantity reconciliation
Compare:
- Inventory movement ledger.
- Inventory balance snapshot.
- Physical warehouse count.
The basic formula:
\[
Closing\ Stock =
Opening + Receipts - Issues
+ Transfers_{In} - Transfers_{Out}
+ Adjustments
\]

Example:
Movement	Quantity
Opening	500
Purchase receipts	+300
Sales issues	-250
Transfer in	+100
Transfer out	-50
Stock adjustment	-5
Closing	595

If the physical count is 590, there is a discrepancy of 5 units.

Do not overwrite the inventory balance with 590.

Create a stock count, investigate the difference, approve an adjustment and create a new inventory movement.
B. Valuation reconciliation
Compare inventory quantity and cost against the accounting subledger.

For example:
Description	Value
Inventory subledger	Rp150,000,000
Inventory GL account	Rp150,000,000
Difference	Rp0

Reconciled
If they differ:
- Check failed posting events.
- Check unvalued negative inventory.
- Check cost revaluations.
- Check manual journal entries.
- Check rounding.
- Check transaction dates and closed periods.
The system must never automatically force the inventory balance to equal the GL balance without identifying the source of the difference.
C. Cross-company reconciliation
This is important for your multi-company group architecture.

Suppose PT A transfers 100 units to PT B.

This is not merely a warehouse transfer because the legal owner changes.

There must be two separate company records:

PT A
- Reduce its inventory.
- Record an intercompany receivable or sale, according to the agreed arrangement.
- Recognize the relevant intercompany revenue and cost.
PT B
- Recognize inventory received.
- Record an intercompany payable or purchase.
- Record applicable taxes and transfer pricing treatment.
Each company has its own journals.

The holding company can consolidate them separately and eliminate intercompany balances and transactions according to the consolidation rules.

A transfer between two warehouses of PT A, however, does not generate revenue or COGS.

10. Transaction integrity and concurrency
This is an engineering requirement, not an optional improvement.

Imagine:
- Warehouse has 10 units.
- Salesperson A requests 8 units.
- Salesperson B requests 7 units.
- Both transactions occur simultaneously.
Without proper concurrency handling, the system might reserve 15 units despite only having 10.
My implementation approach
Use PostgreSQL transactions and row-level locks.
from django.db import transaction@transaction.atomicdef issue_inventory(    company_id,    product_id,    warehouse_id,    quantity,    idempotency_key):    existing = find_existing_transaction(        company_id,        idempotency_key    )    if existing:        return existing    balance = (        InventoryBalance.objects        .select_for_update()        .get(            company_id=company_id,            product_id=product_id,            warehouse_id=warehouse_id        )    )    # Check company policy and permissions.    # Determine whether negative stock is permitted.    # Lock valuation pool.    # Calculate cost.    # Create immutable inventory movement.    # Update balance projection.    # Create accounting posting request.    # Write outbox event.



This is conceptual code; production implementation must also lock the relevant reservation and valuation records, handle absent balance rows safely, validate quantities, and ensure the posting operation is atomic.
Five mandatory engineering safeguards
1. Atomic transactions: Inventory movement and balance update cannot partially succeed.
2. Idempotency: Retrying an API request must not deduct stock twice.
3. Row-level locking: Concurrent operations cannot oversell stock silently.
4. Immutable history: Posted movements cannot be deleted.
5. Transactional outbox: Accounting events cannot disappear between database commit and event delivery.
I would also use database constraints for:
- Unique document numbers per company and document type.
- Unique idempotency keys within their defined scope.
- Non-negative reservations.
- Positive movement quantities, with movement direction stored separately.
- Valid company-to-warehouse relationships.
- Balanced posted journals.
One additional rule: never use a background worker as the only mechanism for creating the inventory balance. The authoritative movement and its balance projection must commit together.
11. Accounting period closing rules
Negative inventory must not be allowed to remain an invisible problem at month-end.

I recommend these controls:
Condition	Month-end treatment
Positive inventory, fully valued	Normal close
Negative inventory	Exception report and approval
Provisional cost	Revaluation or documented unresolved exception
Failed journal posting	Must resolve before close
Subledger/GL mismatch	Must investigate
Unapproved stock adjustment	Must resolve
Open warehouse count	Review outstanding differences

A finance manager should be able to see:
Inventory close dashboard — November 2026
Total inventory valueRp2.45B


Negative stock SKUs12


Provisional valuations8


GL differencesRp0



Close requires exception review
Illustrative dashboard data, not actual company figures.

A closed period must be protected from backdated modifications. Corrections should use controlled reopening or adjustment entries in an authorized open period, following the company's accounting policy.

For Indonesia, make tax and accounting treatment configurable for applicable PSAK requirements, VAT, withholding taxes and company tax settings. Do not hardcode tax rates or account mappings into the inventory engine.

12. Recommended Django application structure
Since you are developing this yourself, I would organize the project by business domain.
accounting_platform/
│
├── core/
│   ├── companies/
│   ├── branches/
│   ├── users/
│   └── permissions/
│
├── master_data/
│   ├── products/
│   ├── categories/
│   ├── units/
│   └── warehouses/
│
├── sales/
│   ├── orders/
│   ├── invoices/
│   ├── returns/
│   └── fulfillment/
│
├── purchasing/
│   ├── purchase_orders/
│   ├── receipts/
│   └── supplier_bills/
│
├── inventory/
│   ├── movements/
│   ├── balances/
│   ├── reservations/
│   ├── valuation/
│   ├── stock_counts/
│   ├── adjustments/
│   └── reconciliation/
│
├── accounting/
│   ├── chart_of_accounts/
│   ├── journals/
│   ├── general_ledger/
│   ├── posting/
│   └── fiscal_periods/
│
├── integrations/
│   ├── events/
│   ├── outbox/
│   └── idempotency/
│
└── reporting/
    ├── inventory_reports/
    ├── financial_reports/
    └── consolidation/


Technology selection
Layer	Technology
Backend	Django
API	Django REST Framework
Database	PostgreSQL
Async processing	Celery
Message broker	Redis
Database locking	PostgreSQL
Frontend	React / Next.js
API documentation	OpenAPI
File storage	S3-compatible storage
Deployment	Docker
Monitoring	Sentry + structured logging

You do not need Kafka, Kubernetes, or microservices for the first version.

PostgreSQL and a transactional outbox are sufficient for this architecture.
13. Implementation sequence
Do not build all the modules simultaneously. Build in dependency order.
Development roadmap
1. Phase 1 — Foundation
   Build first
   - Multi-company isolation
   - Company and warehouse master data
   - Product and unit management
   - Chart of accounts
   - Fiscal periods
2. Phase 2 — Inventory engine
   - Inventory movement ledger
   - Balance projection
   - Stock reservation
   - Negative stock policy
   - Moving average valuation
3. Phase 3 — Accounting integration
   - Journal posting service
   - Inventory control accounts
   - GRNI
   - COGS
   - Cost revaluation
4. Phase 4 — Business operations
   - Purchase orders
   - Goods receipts
   - Sales orders
   - Invoices
   - Goods issues
   - Partial fulfillment
   - Returns
5. Phase 5 — Enterprise controls
   - Stock count
   - Reconciliation
   - Approval workflows
   - Intercompany transfers
   - Audit logs
   - Period closing
6. Phase 6 — Reporting
   - Stock cards
   - Inventory valuation
   - Trial balance
   - Balance sheet
   - P&L
   - Cash flow

14. Mandatory acceptance tests
Before calling the inventory engine production-ready, I would require these tests.
Test case	Expected result
Purchase 100 units	Stock increases by 100
Sell 50 units	Stock decreases by 50
Sell 120 with 100 on-hand	Negative 20 under authorized hybrid policy
Sell 120 with strict policy	Goods issue blocked
Reserve 80 with 100 available	Available becomes 20
Reserve 120 with 100 available	Excess remains backordered
Receive stock after negative issue	Negative quantity is covered and cost revalued
Two simultaneous stock issues	No lost updates or unauthorized overselling
Duplicate invoice API request	No duplicate inventory deduction
Warehouse transfer	Source and destination movements balance
Intercompany transfer	Separate legal-entity accounting
Stock count discrepancy	Adjustment requires authorization
Reverse posted transaction	New reversal record, original remains intact
Close accounting period	No unauthorized backdated posting
Inventory vs GL	Reconciled balances

I would add property-based tests for the core invariants:
- Every inventory movement has a valid company and warehouse.
- Every posted financial journal balances.
- A transfer preserves total company-owned stock quantity.
- A reservation never becomes negative.
- Replaying an idempotent request never creates another movement.
- Inventory balance projections can be rebuilt from the movement ledger.
- Inventory valuation and the inventory control account reconcile.

15. Final architecture decisions to lock
Here is the specification I would treat as the baseline for your accounting platform.
Inventory Engine v1.0
Approved baseline
Architecture area	Decision
Legal entity	Multi-company isolation
Stock ownership	Company-specific
Warehouse	Multiple per company
Inventory model	Perpetual
Costing	Moving Weighted Average
Negative inventory	Hybrid
Stock reservation	Yes
Backorder	Yes
Partial delivery	Yes
Movement ledger	Immutable
Balance calculation	Transactional projection
Cost revaluation	Automatic
Journal posting	Centralized accounting engine
Intercompany	Separate books
Reconciliation	Automated
Period close	Controlled


Core architectural principle

Inventory is the operational subledger. The General Ledger is the financial book. Both must reconcile, but neither should overwrite the other's historical records.

My additional advice: Build the accounting engine before adding too many features
I want you to pay particular attention to three things:
1. Inventory valuation accuracy. A system that shows the correct quantity but incorrect inventory value will produce incorrect gross profit and balance sheets.
2. Multi-company data isolation. A group of companies must not accidentally share inventory ownership or financial balances.
3. Transaction integrity. A failed invoice, warehouse issue, or accounting posting must never leave the system in a partially committed state.
These three things will matter much more than having 100 different inventory screens.

Your long-term objective should be to build a genuine ERP-grade accounting platform, not just another bookkeeping application.

The next useful implementation milestone is the actual Django database schema and migrations for Phase 1 and Phase 2. That is where this architecture becomes executable software