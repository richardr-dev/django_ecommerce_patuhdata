# Commerce reliability audit

Date: 2026-08-26

## Current architecture

| Area | Implementation |
|---|---|
| Shops / tenancy | `apps.shops`: organization, store/brand, staff roles, customers and audit records. Host/slug middleware resolves a store. |
| Catalogue | `apps.catalog`: categories, products, variants, images, warehouses and inventory levels. Store-scoped uniqueness exists for product slug/SKU. |
| Cart / checkout | Session cart in `apps.orders.cart`; server-rendered checkout reserves stock inside a database transaction. |
| Orders | `apps.orders`: immutable order lines, status history, allocations, shipping methods, coupons, returns and payment records. |
| Payments | Adapter interface with demo and Xendit Payment Sessions. Xendit callback token, amount, currency, reference and duplicate event are checked. Browser redirect does not mark paid. |
| Shipping | Adapter interface for real RajaOngkir and Biteship rates. Quotes are server-bound to destination and cart signature. |
| Back office | Authenticated merchant dashboard, fulfilment list, catalogue/inventory/configuration CRUD, Customer 360 and integration failures. Django admin remains the platform console. |
| Async / integrations | Durable database outbox exists, but the initial repository has no worker command that claims and retries events. |
| Database | PostgreSQL in Docker; SQLite for local development. Transaction locking is used at checkout and webhook handling. |
| Deployment | Docker/Gunicorn configuration. S3-compatible Django storage is configurable. No checked-in AWS IaC. |
| Tests | Checkout, tenancy, variants, inventory reservation, provider mapping, webhook authentication/idempotency and state transition tests. |
| Observability | Integration failure fields and dashboard alerts exist. Structured request logging, health probes and error monitoring are incomplete. |

## Risks found

### P0 — money, inventory and access

1. Checkout idempotency key is random per submitted request; repeated POSTs can create multiple orders.
2. Pricing calculations live inside the checkout view and are therefore difficult to reuse or prove consistent.
3. Base products reserve inventory, while variants immediately decrement stock. There is no consistent reserve/commit/release lifecycle or inventory ledger.
4. Coupon usage is validated outside the locking transaction, allowing limited-use voucher races.
5. Refund records and provider execution are incomplete; no database constraint prevents over-refunding.
6. Status rules exist, but inventory side effects are not coupled atomically to fulfilment transitions.
7. The customer order UUID is a bearer secret. It is difficult to guess, but it is not a substitute for an explicit access token/customer verification boundary.
8. Store resolution falls back to the first active store on localhost. This is acceptable for demo only and must never be enabled for unknown production hosts.

### P1 — operations and recovery

1. Outbox records have retry metadata but no claiming worker, dead-letter state or notification adapters.
2. Audit records do not yet contain before/after snapshots, organization or request IP.
3. Customer addresses and promotion per-customer usage are not normalized.
4. Shipment is embedded on the order rather than represented as a separate lifecycle record.
5. Dashboard metrics are lifetime-oriented and do not yet include all requested today/net/refund metrics.
6. No health endpoint, request correlation logging or production error-monitoring integration.
7. No executable backup/restore commands or verified restore record.

### P2 — after the core acceptance suite

1. ERP/CRM/WhatsApp provider implementations.
2. Product/category-specific promotions and customer segmentation.
3. Abandoned cart and repeat-purchase automation.
4. AWS IaC, staging promotion workflow and automated production deployment.

## Implementation order

P0 is fixed first: centralized pricing and idempotent checkout; consistent inventory ledger; payment,
refund and reconciliation rules; deterministic fulfilment; tenant/RBAC regression tests. P1 then adds
the outbox worker, health/logging, backup/restore and CI. P2 remains explicitly out of the core path.
