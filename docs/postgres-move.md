# Moving a SQLite database to PostgreSQL

Used for the local move on 2026-11-13. The same steps work for a server. SQLite is never modified, so
going back is changing `DATABASE_URL`.

1. **Back up** `db.sqlite3` and `.env`.
2. **Apply pending migrations to SQLite**, so both sides have the same tables: `manage.py migrate`.
3. **Fingerprint the source**: `manage.py db_fingerprint > fp-sqlite.json` (row counts of every table and
   the trial balance of every store).
4. **Create the target**: a role and an empty database (`CREATE ROLE ... LOGIN CREATEDB PASSWORD ...;`
   `CREATE DATABASE ... OWNER ...;`), then `DATABASE_URL=postgres://... manage.py migrate`.
5. **Clear what migrations seeded** (billing plans...): `DATABASE_URL=... manage.py flush --no-input`.
6. **Copy**: `manage.py dumpdata --natural-foreign --natural-primary -e contenttypes -e auth.permission
   -e admin.logentry -e sessions -o dump.json`, then `DATABASE_URL=... manage.py loaddata dump.json`
   (loaddata resets the sequences itself).
7. **Compare**: `DATABASE_URL=... manage.py db_fingerprint > fp-pg.json`; the two files must be equal.
   Then `manage.py shell` a `controls.run_checks(store, today)` on the new database.
8. **Switch** `DATABASE_URL` in `.env` and restart. Keep `db.sqlite3` until a week of normal use.

Notes: stock movements are append-only on PostgreSQL (a trigger), so a copy must insert, never update,
them; run the load into an empty database only. Sessions are not copied (everyone logs in again).
