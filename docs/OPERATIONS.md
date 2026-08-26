# Production operations

## Runtime processes

Run the web process with Gunicorn and exactly one or more outbox workers with
`python manage.py process_outbox --loop`. The worker delivers notifications with bounded
exponential retry and expires unpaid payment sessions, releasing their inventory reservations.
Alert when `/healthz/` returns 503, when worker processes are absent, or when failed outbox events
reach eight attempts. Configure a production email backend; the default console backend is local-only.

## PostgreSQL backups and restore verification

Use RDS automated backups with point-in-time recovery, a minimum 14-day retention period, encryption,
and a separate AWS account or vault for retained snapshots. Create a pre-deployment snapshot for risky
migrations. Never treat snapshot creation alone as a verified backup.

Quarterly restore test:

1. Restore the latest automated snapshot into an isolated private RDS instance.
2. Connect with a temporary least-privilege credential and run `python manage.py migrate --check`.
3. Compare counts for stores, orders, payment transactions, inventory movements, and audit logs with
   the source backup timestamp.
4. Execute a storefront read smoke test and a non-provider demo checkout against the restored database.
5. Record snapshot ID, recovery point, start/end time, row-count comparison, tester, and outcome.
6. Destroy the isolated restore and temporary credential after evidence is retained.

Target recovery objectives for the first production customer: RPO 24 hours at worst (prefer RDS PITR
within five minutes) and RTO four hours. Reassess after measured order volume increases.

## Deployment and rollback

Build one immutable image, run tests and `manage.py check --deploy`, deploy it to staging, run the
checkout/payment-demo smoke path, then promote the same image to production. Run migrations as a
single pre-deploy task. Migrations in this repository are additive and reversible where feasible.
Rollback application tasks to the prior image; do not reverse a data migration until its reverse
behavior and current data have been reviewed. Confirm `/healthz/`, web error rate, outbox failures,
payment mismatches, and checkout completion after every deployment.
