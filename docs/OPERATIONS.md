# Production operations

## Runtime processes

Run the web process with Gunicorn and exactly one or more outbox workers with
`python manage.py process_outbox --loop`. The worker delivers notifications with bounded
exponential retry and expires unpaid payment sessions, releasing their inventory reservations.
Configure a production email backend; the default console backend is local-only.

The worker never stops on a single failure: each periodic task (order expiry, cart recovery,
shipment sync) and each order, shipment or email inside it is isolated, failures are logged, and a
database outage is retried with backoff. Compose restarts web and worker with `unless-stopped`; use
the equivalent ECS service setting in production.

### Health check and alerts

`/healthz/` is answered before host validation and tenant lookup, so load balancer probes by instance
IP work. It returns **503 only when the database is unreachable**; background problems never take
web servers out of rotation. Instead the JSON body carries alert fields for monitoring:

| Field | Alert when |
|---|---|
| `alerts` contains `worker_stale` | `worker_last_seen_seconds` > 300 or missing: the worker is down or stuck |
| `alerts` contains `outbox_dead_events` | an event used all 8 attempts; fix the cause, then **Retry** in Merchant → Integrasi |
| `alerts` contains `worker_task_errors` | the last worker cycle had a failing task (see logs) |

Events claimed by a worker that crashed are picked up again after 10 minutes. **Retry** gives a
dead or failed event a fresh set of 8 attempts.

### Logs and error alerts

Logs go to stdout as `time level logger [request_id] message`. Every response carries an
`X-Request-ID` header (an incoming safe `X-Request-ID` is reused), so a customer's error report can be
matched to its log lines. Set `ADMINS=Name:email,...` to email a traceback for every unhandled server
error and worker failure when `DEBUG=0`, or alert on `ERROR` lines in your log platform instead.

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
