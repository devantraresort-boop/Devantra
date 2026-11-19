# Frontend

The guest site (`index.html`) and admin panel (`admin.html`) use plain HTML, CSS
and JavaScript served by FastAPI. No separate frontend build is required.

- `css/styles.css`: public resort theme.
- `css/admin-base.css` and `css/admin.css`: admin foundations and components.
- `js/script.js`: availability, bookings, payments, enquiries and public content.
- `js/admin.js`: authenticated resort management.
- `assets/resort/`: optimized property photography.
- `assets/developers/`: developer portraits.
- `assets/brand/`: resort branding.
- `googlef17034cc86bbb01d.html`: Google verification; retain its contents and filename.

Use `/assets/...`, `/css/...` and `/js/...` URLs in pages; filesystem paths include
`frontend/`, but browser URLs do not. Do not add credentials or guest data here.
Admin-uploaded room photos remain in the database and are served by the API.

Start the app from the repository root with `python -m uvicorn backend.app.main:app
--reload`. Run `node --check frontend/js/script.js` and
`node --check frontend/js/admin.js` from the root after script changes.
