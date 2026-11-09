# Devantara Resort

A FastAPI backend and responsive guest/admin website, using SQLAlchemy, PostgreSQL, Razorpay and a durable email outbox. SQLite remains available for local development. Deployment does not require Docker.

## What works

- Room categories, inventory, capacity, nightly price overrides and closed dates managed by administrators.
- Availability calculated from live holds and confirmed bookings. Room locks serialize reservations, payment confirmation and inventory edits on PostgreSQL.
- Server-calculated prices and idempotent booking requests (`Idempotency-Key`). The guest form reuses the key after a network error.
- Guest booking lookup, cancellation, payment retry/status refresh, and reviews after checkout. Reviews appear publicly only after moderation.
- Razorpay orders, signature verification, captured-payment confirmation, duplicate webhook handling, partial/full refunds, and refund reconciliation after uncertain gateway responses.
- Admin authentication with Argon2 passwords, expiring JWTs, password changes, and token revocation on sign-out. Sign-out/password changes revoke **all sessions for that administrator**.
- Editable public contact details and cancellation policy; room/content management; enquiry tracking; paginated bookings, payments, reviews, enquiries and audit history.
- Email jobs stored in the same transaction as the business action, retry/backoff, stale-worker recovery, and failed-delivery visibility. HTTP email delivery via Resend or SMTP.
- Production configuration validation, bounded request bodies, security headers, request IDs, rate limits, health endpoints, database migrations and CI for SQLite/PostgreSQL.

Room inventory, prices, policies and contact details start empty. Add the resort's real information in `/admin`; no sample reservations or default admin password are installed.

## Run locally on macOS/Linux

Use Python 3.13 (the supported deployment version). From this directory:

```sh
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
cp .env.example .env
python -c 'import secrets; print(secrets.token_urlsafe(48))'
```

Put the generated secret in `.env` as `JWT_SECRET`. Keep `ENVIRONMENT=development` and the SQLite URL for local testing. Then:

```sh
python -m backend.app.migrate
python -m backend.app.create_admin
python -m uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000 --no-access-log
```

Open `http://127.0.0.1:8000`, `/admin`, or `/docs`. In a second terminal, activate the same environment and start the retry worker:

```sh
python -m backend.app.worker
```

On Windows use `py -3.13 -m venv .venv`, `.\.venv\Scripts\Activate.ps1`, and `Copy-Item .env.example .env`; the remaining `python -m ...` commands are the same.

Successful mutations attempt a small email batch after the response. The worker or scheduled maintenance is still required for retries when no visitors are active. Pending holds stop consuming inventory at expiry even if the worker is offline.

## Configure payments and email

In `.env` or the hosting dashboard:

- `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`: use test credentials first.
- `RAZORPAY_TEST_MODE=true`: switch to `false` together with live keys when ready.
- `RAZORPAY_WEBHOOK_SECRET`: secret for the signed webhook at `/api/payments/webhook`.
- Subscribe to `payment.captured`, `payment.failed`, `order.paid`, `refund.processed`, `refund.failed`.
- For Resend: `RESEND_API_KEY` and `EMAIL_FROM` using a verified sender domain.
- For SMTP: `SMTP_HOST`, `SMTP_PORT`, `SMTP_FROM_EMAIL`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_USE_TLS=true`.

When Resend is configured, it takes precedence over SMTP. Missing email credentials leave jobs queued; missing Razorpay credentials disable online payment without pretending a booking is confirmed. Resend account/domain setup and Razorpay business activation must be completed in your accounts.

Booking references are unguessable guest-access credentials: keep them private. Lookup/cancellation in the UI send the reference and email in POST bodies. Legacy GET lookup is retained for compatibility; do not log query strings at your proxy.

## Deployment

See [the deployment guide](docs/DEPLOYMENT.md) for Render + Neon, Vercel + Neon, optional Docker, maintenance scheduling and backups. These files prepare deployments; they do not create or purchase hosting.

- `render.yaml`: single-process free Render web service using an external PostgreSQL database.
- `vercel.json` + `pyproject.toml`: FastAPI entrypoint for Vercel; use external PostgreSQL and a scheduler.
- `Dockerfile` + `compose.yaml`: optional local/self-hosted PostgreSQL, API, migration job and worker.

A free service can be useful for a preview, but sleeping instances, provider quotas and missing operational checks mean it is not a production availability guarantee. The code has not been load-tested at a claimed traffic level.

## Verify changes

```sh
python -m pytest -q
python -m pyflakes backend tests
node --check script.js
node --check admin.js
python -m alembic check
```

The test suite covers pricing, inventory races, booking retries, authorization/revocation, signed payment events, late payments, refunds, outbox retries, moderation, configuration and migrations. External payment and email calls are mocked.

For the PostgreSQL concurrency path, set `TEST_DATABASE_URL` to a **disposable database whose name ends in `_test`**. Tests create isolated schemas and remove only those schemas afterwards. GitHub Actions supplies PostgreSQL automatically and runs both database variants.

Apply migrations before running a new release: `python -m backend.app.migrate`. This command acquires a PostgreSQL advisory lock so concurrent app starts cannot run migrations simultaneously. Do not run schema changes from individual API requests. Back up an existing database before upgrading.

## Important operational behavior

- An expired/cancelled booking receiving a late payment remains unconfirmed. It is shown as paid, an email asks the guest to contact the resort, and the admin can refund it.
- Cancellation does **not** imply a refund. Refund amounts and cancellation policy are decisions for the resort administrator.
- A refund request is committed before contacting Razorpay and uses a stable gateway idempotency key. If the outcome is unknown, further refunds are blocked. Use **Check refund status** in Payments. If no matching receipt is returned, check Razorpay/support before taking further action; do not clear the pending record to blindly retry.
- Refunds initiated outside this application require manual reconciliation. Their webhooks are not automatically mapped to arbitrary bookings.
- Email delivery is at least once. Resend receives a stable retry key; SMTP can redeliver after a crash between sending and recording completion. A `sent` outbox status means provider acceptance, not proof of delivery to an inbox.
- Memory rate limits apply to one process. Configure shared Redis (`RATE_LIMIT_STORAGE_URI`) before adding API workers/replicas or using serverless scaling.
- Prices are treated as the final booking total in INR. Configure tax-inclusive rates; automated GST invoices, tax calculations, SMS and channel-manager integrations are not implemented.
