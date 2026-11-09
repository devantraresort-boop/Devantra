# Verification status — 9 November 2026

Passed locally:

- `pytest tests/test_inventory.py tests/test_inventory_queries.py -q`: **10 passed**. These execute real SQLite/SQLAlchemy queries and constraints, room pricing and closures, expired holds, transaction rollback, idempotency uniqueness, multi-room query counts, and an independent nightly-counter comparison over 300 generated scenarios.
- `python -m pyflakes backend tests`: no findings.
- `python -m compileall -q backend tests`: passed.
- `node --check script.js` and `node --check admin.js`: passed.
- `git diff --check`: passed.

Not verified in this environment:

- Full API and migration suite: collection is blocked by missing runtime dependencies. Installing requirements failed because this session cannot reach the package index. Cached packages were sufficient for the ten tests above, but not for FastAPI/Pydantic/Alembic and the remaining dependencies.
- PostgreSQL integration/concurrency and migrations: added to the GitHub Actions matrix; not executed here.
- Browser interactions: cached Chromium failed to launch because the environment denied its process permissions. JavaScript syntax checks are not a substitute for a browser test.
- Docker build, Render/Vercel deployment, live Razorpay and email: not run; these need the respective runtime/accounts and configured credentials.
- GitHub remote access: `git ls-remote origin HEAD` failed with `Could not resolve host: github.com`.

Before release, install `requirements-dev.txt`, run the full suite, apply migrations against a test PostgreSQL database, exercise the website in a browser, and verify signed Razorpay test events/refunds and real email delivery. The current implementation is not represented as a verified production deployment or as load-tested at any specific scale.
