# NiagaKit

NiagaKit is a Django-based, multi-tenant commerce backbone for agencies serving Indonesian
merchants. One deployment can host many isolated stores, each with a domain, theme, catalog,
orders, staff, and integration configuration.

## Run locally

```bash
cp .env.example .env
docker compose up --build
```

Open `http://localhost:8000`. The seed command creates a demo merchant (`merchant` /
`merchant123`) and products. Platform operations are available at `/platform-admin/`.

Without Docker:

```bash
python3.13 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python manage.py migrate
.venv/bin/python manage.py seed_demo
.venv/bin/python manage.py runserver
```

Local commands load `.env` and use `db.sqlite3` by default. Docker Compose overrides
`DATABASE_URL` with its PostgreSQL service connection.

## Implemented MVP

- Tenant resolution by custom domain or store slug; all catalog/order lookups are store-scoped.
- Two starter storefront themes, responsive catalog and product pages.
- PostgreSQL-backed multi-warehouse inventory with priority allocation, safety stock, transactional
  reservation, allocation traceability, and immutable order lines.
- DOKU Checkout with hosted checkout, authenticated idempotent webhooks, amount/currency
  verification, expiry handling, and a local demo fallback.
- Django admin for platform/store operations, integration credentials, and durable outbox events.
- PostgreSQL/Docker production parity, static assets, migrations, seed data, and checkout tests.
- Indonesian checkout with normalized WhatsApp numbers, structured regional addresses, delivery
  notes, postal codes, configurable shipping, and optional email.
- QRIS, virtual-account, e-wallet, and COD selection; vouchers, free-shipping rules, and correct
  shipping/discount/tax totals.
- Search/category/sort controls, cart quantity editing, product measurements, variants, multiple
  images, customer profiles, tracking timelines, returns, notifications, and audit logs.
- Merchant fulfillment workspace and accounting CSV export at `/merchant/orders/`.
- Group/holding ownership with multiple brands, merchant brand switching, role-aware access, and a
  dedicated back office at `/merchant/` for sales KPIs, daily revenue, top products, low-stock
  alerts, catalog/variant setup, warehouses, inventory, shipping, vouchers, and store settings.

## Brand isolation and database placement

`Organization` is the control-plane owner and each `Store` is an isolated brand boundary. Every
commerce query remains scoped by `store_id`, including dashboard and back-office writes. The
`database_alias` field records deployment placement only; connection strings and credentials must
remain in environment configuration or a secret manager.

For the diagram's strict separate-database topology, deploy one web/worker unit per database alias
and route each brand domain to its assigned unit. Each unit runs the same NiagaKit code and shares
gateway, WMS, ERP, and messaging adapters, while its transactional database contains only that
brand's data. Do not dynamically accept a database alias from a request or store credentials in
`Store.database_alias`. The default local setup remains a shared database with mandatory tenant
scoping, which is easier to operate for small merchants.

The seeded store includes regular and express delivery plus voucher `HEMAT10`. When Biteship test
credentials and an origin postal code are configured, checkout replaces those seeded prices with
server-verified Biteship courier rates based on destination and actual cart measurements.

Production secrets must stay in a secret manager. Complete DOKU refund/reconciliation and
Biteship shipment-booking certification before launch. Never store card numbers or CVV.

## Architecture

```text
Merchant domain -> CloudFront/WAF -> ALB -> Django web (ECS Fargate)
                                             |-- RDS PostgreSQL
                                             |-- ElastiCache/Redis (sessions, cache)
                                             |-- S3 (product media, exports)
                                             |-- SQS -> worker (email, accounting, webhooks)
                                             `-- Secrets Manager

Customer checkout -> payment adapter -> gateway hosted page
Gateway webhook -> signature + replay check -> payment event -> order -> outbox
Outbox -> accounting adapter / shipping adapter / merchant notification
```

Use a shared database with mandatory `store_id` boundaries for the first 50–200 small merchants.
Add PostgreSQL row-level security before exposing a merchant API. Move large or regulated merchants
to dedicated databases when justified. Keep the storefront modular; do not create separate code
forks per client.

## Product phases

1. **Sellable pilot:** merchant onboarding, domains/TLS, catalog/import, checkout, one gateway,
   one courier aggregator, order admin, invoice/CSV accounting export, backups, monitoring.
2. **Repeatable SaaS:** merchant dashboard, theme editor, plans/billing, roles/audit log, coupons,
   variants, shipping zones, notification templates, returns/refunds, API keys and webhooks.
3. **Scale:** queue workers, Redis, search, analytics warehouse, RLS, multi-AZ, DR drills, fraud
   controls, reconciliation, rate limits, and an integration marketplace.

Before selling, add Terms/Privacy, retention and deletion workflows, Indonesian PDP-law review,
incident response, restore tests, gateway/courier certification, and an accountant/legal review.
Calling the product a “marketplace” or holding/splitting merchant funds can introduce materially
different licensing, escrow, tax, and payout obligations; the safer initial model is software where
each merchant contracts directly with its gateway.

## Commercial model (planning assumptions, not a quote)

Do not price every site as scratch development. Sell setup plus recurring platform/support:

| Package | Suggested setup | Suggested monthly | Scope |
|---|---:|---:|---|
| Starter | Rp7–15 juta | Rp500 ribu–1.5 juta | theme, domain, 1 gateway, CSV export |
| Growth | Rp20–50 juta | Rp2–5 juta | custom branding, accounting/courier integration, SLA |
| Business | Rp60–150+ juta | Rp6–15+ juta | workflow/API work, migration, training, priority SLA |

Target setup gross margin after the backbone stabilizes: 55–70%. Target recurring gross margin:
65–80%. Quote external gateway, WhatsApp/SMS, courier, domain, and unusually heavy traffic as
pass-through or metered costs. Require a paid discovery phase for custom accounting systems.

Example Growth deal: Rp30m setup + Rp3m/month. If delivery consumes Rp12m labor and allocated first-
year hosting/support is Rp10m, first-year revenue is Rp66m and contribution is roughly Rp44m (67%)
before sales, tax, and company overhead. Treat this as a model—replace every assumption with your
actual labor and AWS bills.

## AWS deployment choices

- **Pilot:** one Lightsail instance/container plus managed PostgreSQL and S3. Lowest operational
  complexity; accept that this is not highly available.
- **Production SaaS:** ECS Fargate across two AZs, ALB, RDS PostgreSQL Multi-AZ, S3/CloudFront, SQS,
  Secrets Manager, WAF, CloudWatch, daily backups, and AWS Budgets.
- **Larger scale:** add Redis, read replica/search/warehouse only after metrics justify them.

Create separate AWS accounts for production and non-production, use Infrastructure as Code, least-
privilege IAM, encrypted storage, point-in-time recovery, alarms, vulnerability updates, and a
quarterly restore exercise. Maintain cost per store and gross margin dashboards from day one.

## Quality commands

```bash
.venv/bin/ruff check .
.venv/bin/pytest -q
.venv/bin/python manage.py check --deploy
```
