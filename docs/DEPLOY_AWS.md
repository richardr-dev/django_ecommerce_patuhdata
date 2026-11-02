# Deploy Lakuin on AWS (one EC2 t3.small)

One server runs everything from `compose.prod.yaml`: Django (gunicorn), the worker,
PostgreSQL, daily database backups and Caddy, which issues HTTPS certificates for
`lakuin.store` and each store subdomain on first visit. The CloudFormation template
[`deploy/aws/ec2-t3-small.yaml`](../deploy/aws/ec2-t3-small.yaml) creates the server.

| Resource | What it is | Approx. cost |
|---|---|---|
| EC2 `t3.small` | 2 vCPU, 2 GiB RAM, Ubuntu 24.04, Docker, 2 GiB swap | ~US$19/month |
| EBS gp3 30 GiB | Encrypted disk: OS, database, uploads, local backups | ~US$3/month |
| Elastic IP | Fixed public address for DNS | ~US$4/month |
| Daily snapshots | 7 days of whole-disk snapshots (02:00 WIB) | ~US$1–3/month |
| Route 53 zone | Optional; only if Route 53 hosts your DNS | US$0.50/month |

Prices vary by region; check the AWS Pricing Calculator. t3 instances default to
"unlimited" CPU credits: sustained high CPU is billed a little extra instead of throttled.

## 1. Before you start

1. Create the AWS account, turn on **MFA for the root user**, and create an IAM user (or
   IAM Identity Center user) for daily work.
2. **Billing → Budgets**: add a monthly cost budget (e.g. US$40) with an email alert.
3. Choose the region. **Asia Pacific (Jakarta) `ap-southeast-3`** keeps data in Indonesia,
   but it is an opt-in region: enable it under **Account → AWS Regions** first.
   Singapore `ap-southeast-1` works without opt-in.
4. Push this branch to GitHub so the server can clone it.

## 2. Create the server

Console: **CloudFormation → Create stack → With new resources → Upload a template file**,
choose `deploy/aws/ec2-t3-small.yaml`, name the stack `lakuin`, and set:

- `HostedZoneId`: leave empty unless Route 53 hosts `lakuin.store` (see step 3).
- `KeyName` / `SshCidr`: leave empty. You connect through Session Manager instead, so
  port 22 stays closed.

Tick "I acknowledge that AWS CloudFormation might create IAM resources" and create.
After ~3 minutes, the stack's **Outputs** tab shows `PublicIp`.

CLI alternative:

```bash
aws cloudformation deploy --region ap-southeast-3 --stack-name lakuin \
  --template-file deploy/aws/ec2-t3-small.yaml --capabilities CAPABILITY_IAM
aws cloudformation describe-stacks --region ap-southeast-3 --stack-name lakuin \
  --query "Stacks[0].Outputs"
```

## 3. Point the domain at the server

**Option A, DNS stays at your registrar.** Add three A records with value `PublicIp`:

| Name | Type | Value |
|---|---|---|
| `@` | A | PublicIp |
| `www` | A | PublicIp |
| `*` | A | PublicIp |

**Option B, Route 53.** Create a public hosted zone for `lakuin.store`, copy its four
NS servers into the nameserver settings at your registrar, then update the stack with
`HostedZoneId` set to the zone ID; the template creates the three records.

Check from your laptop (it can take minutes to hours to propagate):

```bash
dig +short lakuin.store
dig +short coba.lakuin.store   # any name; the wildcard must answer with the same IP
```

## 4. Install the app on the server

Open a shell: **EC2 → Instances → lakuin-web → Connect → Session Manager → Connect**.

```bash
sudo -iu ubuntu
cloud-init status --wait     # waits until Docker is installed ("status: done")
docker --version
```

If the GitHub repository is private, give the server a read-only deploy key:

```bash
ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519 -C lakuin-server
cat ~/.ssh/id_ed25519.pub
# GitHub → repository → Settings → Deploy keys → Add deploy key (leave "write" off)
ssh -o StrictHostKeyChecking=accept-new -T git@github.com   # "successfully authenticated"
```

Clone the deploy branch:

```bash
git clone -b feature/aws-ec2-deploy git@github.com:richardr-dev/django_ecommerce_patuhdata.git lakuin
cd lakuin
```

Create `.env.production`. Never commit it: open your local `.env.production`, copy
everything, then on the server:

```bash
nano .env.production    # paste, then Ctrl+O, Enter, Ctrl+X
chmod 600 .env.production
```

Before the first start, check in that file:

- `PLATFORM_DOMAIN=lakuin.store`, `SITE_ADDRESS=lakuin.store, www.lakuin.store`,
  `PUBLIC_BASE_URL=https://lakuin.store`
- `SECRET_KEY` and `POSTGRES_PASSWORD` are filled. Keep `POSTGRES_PASSWORD` unchanged
  after the first start: the database is created with it.
- `PAYMENT_SECRETS_KEY` is filled and backed up: sellers' gateway secrets are encrypted
  with it, and they must re-enter them if it is lost or changed.
- `PLATFORM_SUPPORT_EMAIL`, and DOKU keys for Lakuin subscription billing. Store checkouts
  pay each seller's own accounts, which sellers set up in back office > Pembayaran.

Start everything (the first build takes several minutes on a t3.small):

```bash
./deploy/aws/deploy.sh
docker compose -f compose.prod.yaml --env-file .env.production logs -f caddy web
```

Wait until Caddy logs `certificate obtained successfully` for `lakuin.store`, then press
Ctrl+C. Create the platform admin account:

```bash
docker compose -f compose.prod.yaml --env-file .env.production exec web \
  python manage.py createsuperuser
```

### Wildcard certificate for store subdomains (recommended)

Without it, Caddy requests a certificate for each new store on its first visit. That first
page takes a few seconds, and Let's Encrypt allows only 50 certificates a week for
lakuin.store, so a busy signup week would leave new stores without HTTPS.

1. In Cloudflare: **My Profile → API Tokens → Create Token → "Edit zone DNS"**, limited to
   the zone `lakuin.store`. Copy the token.
2. On the server, add to `.env.production`:
   ```
   CLOUDFLARE_API_TOKEN=<the token>
   CADDYFILE=Caddyfile.wildcard
   ```
3. `docker compose -f compose.prod.yaml --env-file .env.production up -d --build caddy`

Caddy then proves ownership through a temporary `_acme-challenge` TXT record and keeps one
`*.lakuin.store` certificate renewed. Sellers' custom domains still get on-demand certificates.
Keep the `*` A record in Cloudflare grey-clouded (DNS only).

## 5. Check it works

1. `https://lakuin.store` shows the Lakuin landing page with a valid padlock.
2. `https://www.lakuin.store` redirects to `https://lakuin.store`.
3. Sign up at `https://lakuin.store/start/`; the new store opens at
   `https://<alamat>.lakuin.store`. Its certificate is issued on that first visit, so the
   first load takes a few seconds.
4. `https://tidak-ada.lakuin.store` (a name nobody registered) fails with a certificate
   error: Caddy asked Django, which refused, so no certificate is requested for it.
5. PT Patuhdata's DOKU dashboard (subscriptions): set the notification URL to
   `https://lakuin.store/integrations/webhooks/doku/`. Each seller's own gateway uses the
   store-specific URL shown on their Pembayaran page.

## Updating

From your laptop, push to the branch the server tracks. Then on the server:

```bash
sudo -iu ubuntu
cd lakuin && ./deploy/aws/deploy.sh
```

## Backups and recovery

### Off-site backups, automatic recovery and alerts

The stack also creates a private, versioned S3 bucket (30 days of backups), alarms that
recover the server on an AWS hardware fault and reboot it when it stops responding, a CPU
alarm, and an SNS topic for alerts. Once the stack is updated, install the hourly uploader on
the server (it also alerts when the disk is 85% full or no backup ran for 26 hours):

```
sudo /home/ubuntu/lakuin/deploy/aws/install-offsite-backup.sh <BackupBucketName> <AlertTopicArn>
```

Both values are stack outputs. To receive the alerts, update the stack with
`AlertEmail=you@example.com` and click the confirmation link AWS emails you.
The server can upload but not delete backups, so a compromised server cannot erase them.


- **Database + uploads:** the `backup` container writes a dump every 24 hours to
  `~/lakuin/backups` (kept 14 days). To restore one:

  ```bash
  c="docker compose -f compose.prod.yaml --env-file .env.production"
  $c stop web worker
  $c exec backup /scripts/restore.sh /backups/db-<timestamp>.dump /backups/media-<timestamp>.tar.gz
  $c start web worker
  ```
- **Whole disk:** daily EBS snapshots, kept 7 days (`SnapshotRetainDays`). To recover a
  broken server: EC2 → Snapshots → create a volume from the latest snapshot and attach it
  to a new instance.
- Termination protection is on and the disk is kept if the instance is deleted, so a
  mistaken click does not destroy the database.
- Snapshots stay in the same AWS account and region. For a copy elsewhere, periodically
  download `~/lakuin/backups` or sync it to an S3 bucket.

## When to outgrow this

Move up to `t3.medium` (update the stack's `InstanceType`; the server restarts) when
memory stays above ~80% (`free -h`) or pages slow down at peak. Move the database to RDS
and uploads to S3 once you have enough paying sellers that a few hours of downtime would
hurt; both need small code and configuration changes.
