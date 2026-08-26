# Commerce reliability audit and implementation plan

Audit date: 2026-08-26

## Repository map

1. **Apps:** `shops` owns organizations, stores, staff, customers and audits; `catalog` owns
   products, variants, images, warehouses and stock; `orders` owns cart, pricing, checkout, orders,
   inventory movements, promotions, fulfilment, returns and refunds; `integrations` owns adapters,
   webhooks, credentials and the outbox.
2. **Relationships:** Organization has Stores. Store-scoped commerce resources attach to Store.
   Products have Category, images, variants and warehouse InventoryLevels. Orders have immutable
   items, allocations, movements, status events, payment transactions, refunds and returns.
3. **Catalogue:** active product list/detail, store-scoped slugs/product SKUs, variant price/weight/
   stock, images, dimensions and SEO fields. Store is the brand boundary; tax is store-wide.
4. **Cart:** anonymous session cart supports variants, update/remove, stock caps and server-side
   recalculation. Durable expiry and logged-in merge are absent.
5. **Checkout:** validates cart/contact/address/shipping/promotion/provider, locks stock, snapshots
   totals and lines, reserves inventory, creates payment/outbox records, and uses a unique idempotency
   key. Shipping quotes are bound to destination and cart signature.
6. **Orders:** explicit transition graph from pending through completion/cancellation/refund. Each
   transition locks current state, applies inventory changes, appends history and emits an event.
7. **Payments/refunds:** PaymentTransaction records provider IDs, type/status, amount/currency,
   payload and reconciliation. Refund records amount/reason/operator/provider result. Demo and Xendit
   adapters exist; cumulative refunds are locked and capped at verified paid amount.
8. **Inventory:** base products use prioritized warehouse allocations; variants use stock/reserved.
   Checkout locks rows; reserve/commit/release are ledgered; packing commits; cancellation, failure and
   expiry release. PostgreSQL is required to exercise real row-lock concurrency.
9. **Customers/users:** Django User authenticates staff. Store Customer is unique by store+phone and
   holds contact/consent. Address history/tags are not normalized; metrics derive from scoped orders.
10. **Shipping:** manual methods plus Biteship/RajaOngkir quote adapters. Orders snapshot service,
    charge, ETA and tracking. Multi-shipment, booking and tracking sync are absent.
11. **Back office:** scoped dashboard, fulfilment transitions, required tracking, finance refund, CSV,
    customer view, commerce CRUD and integration/reconciliation failures. Django admin is the platform
    console.
12. **APIs:** no public commerce API. JSON endpoints provide shipping search/rates; Xendit has a
    provider webhook. Other flows are server-rendered.
13. **Webhooks:** constant-time token verification; JSON/provider/reference/amount/currency validation;
    payload deduplication and order locking. Late paid events after cancellation become reconciliation
    mismatches rather than changing the order.
14. **Workers:** a database outbox worker claims with `skip_locked`, sends email, retries with bounded
    backoff, and expires unpaid orders. Compose runs it separately. No Celery/RQ dependency.
15. **Database:** SQLite is local/test convenience; Docker uses PostgreSQL 17. Constraints protect
    tenant identifiers, reserved stock, refunds/payments/items and persisted financial totals.
16. **Deployment:** Docker/Gunicorn, optional S3 and an RDS-oriented design exist. No AWS IaC or
    automated staging/production deployment is checked in.
17. **Tests:** pricing, checkout, variants, inventory lifecycle, idempotency, transitions, providers,
    webhook auth/duplicate/mismatch/failure/late events, refunds, authorization, outbox/expiry,
    tracking and full happy path. SQLite cannot prove `select_for_update` behavior.
18. **Observability:** Django logs, integration/reconciliation screens and DB/outbox-aware `/healthz/`.
    Structured correlation, metrics/tracing and external exception monitoring remain absent.
19. **Auth/RBAC:** Django sessions/CSRF protect merchant writes. Scoped querysets and role checks cover
    store objects. Customer order detail requires its browser session or per-order access token.
20. **Tenancy:** host/slug middleware resolves Store; commerce lookups are store-scoped. Organization
    supports multiple brands. First-store fallback is restricted to localhost/testserver demos.

## P0 — resolved in this pass

- Restored missing payment/refund migration that had made checkout crash.
- Replaced UUID-only customer order access with a session/per-order-token boundary.
- Made transitions lock current state; blocked manual financial states; required shipment tracking.
- Released stock on payment failure/expiry and retained late paid events for reconciliation.
- Fixed successive partial refunds and added database checks for invalid financial values.
- Added the outbox notification/expiry worker and removed a nonexistent Compose env file.
- Added CI, readiness health, production secret/TLS guards and backup/restore guidance.

## P0 — remaining before real provider money

- Run last-item checkout with two independent PostgreSQL connections; SQLite ignores row locks.
- Certify current Xendit events/refunds/token rotation and courier quotes in provider sandboxes.
- Implement/certify courier shipment booking and tracking; current adapters quote only.
- Enforce SKU uniqueness across Product and ProductVariant together at Store scope. Current constraints
  cover products per store and variants per product, not cross-table collisions.
- Move payment-session HTTP creation outside the stock-lock transaction using a durable post-commit
  state while preserving idempotency and reservation recovery.

## P1 — operational readiness

- PostgreSQL concurrency tests for stock, coupon use and simultaneous refunds.
- Dead-letter state and idempotent provider notification keys.
- CustomerAddress and CouponRedemption models for history/per-customer limits.
- Append-only before/after/IP/organization audit context and DB/admin mutation protection.
- Structured request IDs, metrics and external error monitoring/alerting.
- Shipment lifecycle/exceptions/carrier callbacks and measured provider health.
- Correct today/net/refund/repeat-customer dashboard metrics.
- AWS IaC, private networking, Secrets Manager, RDS PITR alarms, staging promotion and a recorded restore
  drill using `OPERATIONS.md`.

## P2

ERP/CRM/WhatsApp adapters, segmentation, advanced promotions/analytics/themes/loyalty and all other
features outside the core transaction.

## Acceptance status

Automated: catalogue/variant/cart/pricing, reservation, idempotent order creation, demo payment,
verified/idempotent webhook behavior, fulfilment, single inventory commit, tracking shipment,
delivery/completion, failure/expiry release, partial/full refund rules, durable email events, RBAC and
tenant/customer-order isolation. Provider certification, PostgreSQL concurrency, real shipment booking,
AWS deployment and a recorded restore drill remain external production gates.
