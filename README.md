# NiagaKit

NiagaKit is a Django-based, multi-tenant commerce backbone for agencies serving Indonesian
merchants. One deployment can host many isolated stores, each with a domain, theme, catalog,
orders, staff, and integration configuration.

## Run locally

```bash
cp .env.example .env
docker compose up --build
```

Open `http://localhost:8000`. This starts PostgreSQL 17, the web app (live code reload) and the
outbox worker. On a database with no stores, start-up seeds a demo merchant (`merchant` /
`merchant123`) and products; later restarts never reseed, so merchant edits are kept. Platform
operations are available at `/platform-admin/`. `docker compose ps` shows each service's health.

## Run in production with Docker

`compose.prod.yaml` runs the built image with `DEBUG=0`, gunicorn, the worker, a private
PostgreSQL, and [Caddy](https://caddyserver.com) in front for automatic HTTPS certificates.

```bash
cp .env.production.example .env.production   # fill in SECRET_KEY, POSTGRES_PASSWORD, domains
docker compose -f compose.prod.yaml --env-file .env.production up -d --build
```

- **Domains:** list every brand domain in both `SITE_ADDRESS` (Caddy) and `ALLOWED_HOSTS`
  (Django), and point their DNS at the server. `SITE_ADDRESS=localhost` gives a local HTTPS trial.
- **Data:** PostgreSQL and uploaded media live in the `postgres_data` and `media` volumes; Caddy
  serves `/media/` directly. Back up both (see `docs/OPERATIONS.md`).
- **Updates:** `git pull` then rerun the `up -d --build` command; `web` applies migrations on start
  and the worker waits for it.
- **Security:** the image never contains `.env*`, `.git` or local databases (`.dockerignore`), runs
  as a non-root user, and the database port is not published. Raise `SECURE_HSTS_SECONDS` once
  every domain is HTTPS-only.

Without Docker for the app (PostgreSQL still runs in Docker):

```bash
python3.13 -m venv .venv
.venv/bin/pip install -e '.[dev]'
docker compose up -d db
.venv/bin/python manage.py migrate
.venv/bin/python manage.py seed_demo
.venv/bin/python manage.py runserver
```

Local commands load `.env`, whose `DATABASE_URL` points at the Compose PostgreSQL 17 service on
`localhost:5433`, so local runs match production. Inside Compose, the web and worker containers
override it with `db:5432`. If `DATABASE_URL` is unset, settings fall back to `db.sqlite3`; that
fallback exists only for the quick CI job and should not be used for development.

## Storefront design and catalog scale

- **Templates:** `/merchant/settings/` offers one-click storefront templates (Retail, Minimalis,
  Butik, Segar, Elektronik) that set brand colour, page background, font, cover layout, and logo
  size. Logo, cover photo, and copy are never overwritten, and every value stays editable.
  Presets live in `apps/shops/storefront_templates.py`.
- **Cover page:** upload a cover photo, choose a layout (text + info panel, full photo, centred),
  and set the photo darkness so headlines stay readable. Active carousel slides take precedence.
- **Logo:** small/medium/large sizing, optional store name beside it, and it doubles as favicon.
- **10,000+ SKUs:** the storefront catalog (24/page), merchant product list and stock pages
  (50/page) are paginated with stable ordering and composite indexes; low-stock figures are
  computed in SQL; product pickers take a SKU instead of listing every product; CSV import accepts
  10,000 rows / 10 MB per file using bulk writes. Load-test locally with
  `python manage.py seed_catalog --count 10000`.

## Brand storefront

Everything a brand's own DTC website needs beyond checkout, managed per store from `/merchant/`:

- **Product reviews:** only signed-in customers with a delivered/completed order for the product can
  rate it (1–5★, one review per customer, editable). Names show as "Ayu S.". The back office
  (*Ulasan produk*) can hide reviews and post a public reply, but never create or edit one.
  Ratings show on the product page and in its structured data.
- **Wishlist:** customers save products from the product page; see `/account/wishlist/`.
- **Brand pages:** About, FAQ, size/brew guides and similar at `/pages/<slug>/` (*Halaman brand*),
  linked from the header and/or footer. Bodies are plain text, so merchant input cannot inject HTML.
- **Newsletter:** a footer signup with explicit marketing consent, deduplicated per store, with
  POST-only unsubscribe links (`/newsletter/unsubscribe/<token>/`) and CSV export of active
  subscribers (*Newsletter*). Sending campaigns is left to the merchant's email tool.
- **Discovery & SEO:** related products from the same category, per-store `/robots.txt` and
  `/sitemap.xml`, canonical URLs, Open Graph tags, product `seo_title`/`seo_description`, and
  schema.org `Product` JSON-LD with offer and aggregate rating.

## Implemented MVP

- Tenant resolution by custom domain or store slug; all catalog/order lookups are store-scoped.
- Two starter storefront themes, responsive catalog and product pages.
- PostgreSQL-backed multi-warehouse inventory with priority allocation, safety stock, transactional
  reservation, allocation traceability, and immutable order lines.
- DOKU Checkout as the primary gateway: hosted checkout, signed idempotent notifications,
  amount/currency verification, a status check before expiring unpaid orders, manual-transfer
  refunds, and a local demo fallback. Xendit remains available as a secondary adapter.
- Gateway sessions are created after the stock/coupon transaction commits, so a slow gateway never
  holds inventory locks; a failed session cancels the order and releases its reservation.
- Biteship courier booking from the merchant order screen, with waybill capture and automatic
  tracking sync that moves orders to shipped/delivered.
- SKUs are unique per store across products and variants.
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

Production secrets must stay in a secret manager. Certify DOKU notifications, status checks and
each enabled channel, and Biteship booking/tracking, in their sandboxes before launch (see
`docs/INTEGRATIONS.md`). Never store card numbers or CVV.

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

With the default `.env`, `pytest` runs against PostgreSQL (start it with `docker compose up -d db`),
including the race-condition tests in `tests/test_postgres_concurrency.py`, which are skipped on
SQLite. CI runs the suite on both databases on every push.
