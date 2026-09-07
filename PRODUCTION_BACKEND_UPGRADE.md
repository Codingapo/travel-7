# TravelIntel AI — Production Backend Upgrade

This build keeps the existing visual interface and changes the backend to be non-destructive and database-first.

## Database safety

- Package definitions are no longer embedded in application code.
- Application startup never seeds, resets, replaces, or deletes packages.
- Existing package IDs and booking records are preserved.
- Package records have `is_active`, `deleted_at`, `created_at`, and `updated_at`.
- Archived packages remain available for historical booking joins and cannot receive new bookings.
- Package CRUD uses transactions and server-side validation.
- Duplicate package detection is performed server-side and in the admin UI.
- Admin mutations are audited in `Admin_Audit_Log`.

## Package management

Admin package management now supports:

- create
- edit
- archive (soft delete)
- activate/deactivate
- capacity updates
- image URL validation
- image upload (JPEG/PNG/GIF/WebP, 5 MB maximum)
- search/filter/sort API support

Uploaded images use random server-side filenames and are stored under `static/uploads/packages`.

## Booking integrity

Booking creation uses a SQLite `BEGIN IMMEDIATE` transaction so capacity is re-read while the write lock is held. This prevents concurrent bookings from overselling the same package.

Cancellation is non-destructive:

`POST /api/bookings/<booking_id>/cancel`

Cancellation records status, timestamp, actor and optional reason, and restores capacity exactly once.

## CSRF and rate limiting

State-changing package and booking operations require a server-generated double-submit CSRF token:

- cookie: `csrf_token`
- header: `X-CSRF-Token`

Sensitive package/booking mutation routes also have basic per-IP rate limiting.

Session cookies are `HttpOnly`, `SameSite=Lax`, and marked `Secure` automatically when served over HTTPS.

## SerpApi caching

Google Reviews SerpApi responses are cached in the `SerpApi_Cache` table.

- normalized request parameters generate a SHA-256 cache key
- fresh cache entries avoid API calls
- concurrent identical requests use a short database-backed lease
- failed refreshes can fall back to stale cache
- API credentials are never part of the cache key or logged

The cache TTL can be configured with:

`SERPAPI_REVIEWS_CACHE_TTL`

Default: 900 seconds.

## One-time package import

Initial packages are imported only through the explicit setup endpoint:

`POST /api/setup/import-initial-packages`

It requires:

`X-Setup-Token: <INITIAL_PACKAGE_IMPORT_TOKEN>`

The token must be supplied through the environment. No package payload is stored in source code.

The import is database-guarded by:

`initial_package_import_completed`

After completion, subsequent imports are no-ops. Existing matching packages are skipped rather than overwritten.

## Important deployment rule

Do not add package seeding to startup scripts, Docker entrypoints, migration hooks, health checks, or deployment commands.

Normal startup is intentionally:

`connect database → run additive schema migrations → read existing data → serve application`

The legacy demo booking restoration endpoint remains present only as a protected, non-destructive compatibility endpoint and is disabled.

## Verification performed

- Python syntax compilation passed for `app.py`, `database.py`, and `google_reviews_sync.py`.
- JavaScript syntax checks passed for all static JS files.
- Inline JavaScript syntax checks passed for the modified package, booking, profile and dashboard templates.
- A clean database initialization test created **0 packages and 0 bookings**, confirming no startup package seeding.
- A copy of the included historical backup retained all **14 packages, 2 bookings, and 2 customers** after schema migration, confirming the migration is additive.
