# Deployment and operations

Keep payment, email and database secrets in the host's environment settings. Do not put them in JavaScript, GitHub, screenshots or chat. Use a separate test database and test Razorpay account for previews.

## Budget-friendly preview: Render + Neon

1. Create a Neon PostgreSQL project. Copy its connection string with TLS (`sslmode=require`). Standard `postgres://` / `postgresql://` URLs are normalized to the Psycopg driver. Use a pooled URL for serverless deployments.
2. Connect this GitHub repo to a Render Python web service, or import `render.yaml` as a Blueprint. Choose the free plan for a preview.
3. Build: `pip install -r requirements.txt`.
4. Start: `python -m backend.app.migrate && uvicorn backend.app.main:app --host 0.0.0.0 --port $PORT --no-access-log`.
5. Set the following environment variables using the actual assigned hostname:

```dotenv
ENVIRONMENT=production
DATABASE_URL=postgresql+psycopg://USER:PASSWORD@NEON_HOST/DATABASE?sslmode=require
JWT_SECRET=<random-secret-at-least-32-characters>
ALLOWED_HOSTS=["YOUR-SERVICE.onrender.com","localhost","127.0.0.1"]
CORS_ORIGINS=["https://YOUR-SERVICE.onrender.com"]
RESORT_TIMEZONE=Asia/Kolkata
DATABASE_POOL_SIZE=3
DATABASE_MAX_OVERFLOW=2
RAZORPAY_TEST_MODE=true
MAINTENANCE_SECRET=<different-random-secret>
```

6. Run `python -m backend.app.create_admin` from your local environment against the same Neon database (or an authenticated hosting shell). This prompts for a password without printing it. There is no public bootstrap endpoint.
7. Add real inventory and contact details through `/admin`. Configure Razorpay test keys and webhook secret; verify successful and failed payments before using live credentials.
8. Configure Resend over HTTPS for guest emails. **Render Free blocks SMTP ports 25, 465 and 587**; use `RESEND_API_KEY` and `EMAIL_FROM`, not SMTP on that plan.
9. Configure an external scheduler to POST to `/api/internal/maintenance` every 1–5 minutes, with `Authorization: Bearer <MAINTENANCE_SECRET>`. An authenticated server/CI scheduler can also run `python -m backend.app.worker --once` against the same database. Free-tier schedulers/hosts may delay jobs; no precise delivery SLA is implied.

Render Free sleeps after idle periods. A first request, including a webhook, can be slow; Razorpay retries must be tested. Use an always-on instance before relying on prompt live payment handling. Neon free quotas also need monitoring. No hosting subscription is created by this repository.

Sources: [Render FastAPI](https://render.com/docs/deploy-fastapi), [Render Free limits](https://render.com/docs/free), [Neon pricing/limits](https://neon.com/pricing), [Resend send API](https://resend.com/docs/api-reference/emails/send-email).

## Vercel + Neon

The repository declares `backend.app.main:app` in `pyproject.toml`; `vercel.json` selects FastAPI. The existing website assets are served by explicit application routes. No directory listing or source-folder static mount is exposed.

1. Import the GitHub repository into Vercel using the FastAPI framework.
2. Set the production variables above with the actual Vercel/custom domain, and set `DATABASE_NULL_POOL=true` with Neon's pooled PostgreSQL URL. Never use SQLite or a local disk database on Vercel.
3. Apply migrations once from a trusted machine/CI environment with production `DATABASE_URL_UNPOOLED` set to Neon's direct connection URL: `python -m backend.app.migrate`. Both this command and Alembic prefer the direct URL; runtime queries keep using the pooled `DATABASE_URL`. The app reads `.env.local` after `.env`, with process environment variables taking precedence. Do this before sending traffic to the new version; the Vercel function does not migrate on cold start.
4. Create the administrator with the CLI and configure Razorpay and email.
5. Configure shared Redis for rate-limit state across function instances. Configure an external POST scheduler for maintenance; `vercel.json` does not silently add a paid cron schedule.
6. Add each preview hostname explicitly if previews need access, using a separate preview database. Avoid a broad wildcard host/origin policy.

Vercel's free Hobby plan is for personal/non-commercial use; check commercial plan costs before deploying the real resort business. Docker is not required by either Vercel or native Render.

Sources: [FastAPI on Vercel](https://vercel.com/docs/frameworks/backend/fastapi), [Python runtime](https://vercel.com/docs/functions/runtimes/python), [Hobby rules](https://vercel.com/docs/plans/hobby).

## Optional Docker setup

Copy `.env.example` to `.env`, set `JWT_SECRET`, and set `POSTGRES_PASSWORD` to a URL-safe value (for example `python -c 'import secrets; print(secrets.token_hex(32))'`).

```sh
docker compose up --build -d
docker compose exec api python -m backend.app.create_admin
```

Open `http://127.0.0.1:8000`. PostgreSQL data is stored in the named `postgres_data` volume. The migration job completes before API and worker startup. Containers run the application as an unprivileged user. The database has no public host port.

For a public server, terminate HTTPS at a reverse proxy, set `ENVIRONMENT=production`, and configure real `ALLOWED_HOSTS` / `CORS_ORIGINS`. Keep `localhost`/`127.0.0.1` in allowed hosts if using the supplied container health check. Trust forwarded headers only from the actual proxy; do not expose an unrestricted proxy-header trust configuration to the internet. The supplied port binding is intentionally loopback-only.

After an image/code update:

```sh
docker compose build
docker compose run --rm migrate
docker compose up -d api worker
```

Do not run `docker compose down -v` against a database you need to keep: `-v` deletes its volume.

## Backups and monitoring

For self-hosted PostgreSQL, take encrypted off-host backups on a schedule. Example backup (run from the repository):

```sh
docker compose exec -T db pg_dump -U resort -d devantara -Fc > devantara-backup.dump
```

Restore into a separate database/environment first and verify inventory, bookings, payment references, admins and migrations. For Neon/managed databases, choose retention/PITR appropriate to the business and test a restore; a free database plan should not be assumed to include sufficient recovery coverage. Add backup file extensions to your private storage policy; backups contain guest information.

Monitor `/api/health` for database readiness and `/api/health/live` for process liveness. Alert on repeated 5xx, payment/webhook failures, aged pending refunds, failed email counts, database capacity and provider quotas. Admin Settings displays delivery counts and can requeue failed jobs. Application logs contain request IDs and route templates, not request bodies/query strings; configure host/proxy logging similarly.

Scale API processes only with PostgreSQL and shared Redis rate-limit storage. Each process otherwise has its own pool and rate counters. Account for `DATABASE_POOL_SIZE + DATABASE_MAX_OVERFLOW` per process within database connection limits. Outbox workers use `FOR UPDATE SKIP LOCKED` and leases so several workers can share the queue.

## Before live bookings

Run the complete tests against PostgreSQL, exercise checkout/webhooks/refunds with Razorpay test credentials, test email delivery with the configured sender, and run a browser smoke test on the deployed URL. Verify inventory and tax-inclusive rates, cancellation/refund terms, guest privacy text, domain/HTTPS and backups. Complete these operational checks before describing the deployment as production-ready; the configuration files alone do not establish reliability or traffic capacity.
