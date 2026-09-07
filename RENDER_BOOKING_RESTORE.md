# Render booking-data restore

This version adds a protected administrator-only booking restore tool.

## How to use on Render

1. Deploy this version.
2. Log in at `/auth/login` with an active administrator account.
3. Open `/dashboard`.
4. Find **Booking Data Tools**.
5. Click **Restore 72 Bookings** and confirm.
6. The endpoint writes to the database used by the running Render service.
7. Refresh the dashboard.

The restore is guarded by the normal administrator session. It will not create duplicate bookings: if the database already contains at least one booking, it leaves existing data unchanged.

When starting from an empty database with the existing customer/package seed data, it creates:
- 72 confirmed bookings
- 173 travellers
- target revenue of R3,816,500.00 (subject to any existing data; the tool only runs when the booking table is empty)

Every restore is recorded in `Admin_Audit_Log`, including the administrator who performed it.

## Render persistence

If the Render service uses SQLite, attach a Render Persistent Disk and store the SQLite database on that persistent filesystem. Otherwise a redeploy/restart can replace an ephemeral filesystem and the generated bookings may disappear.

The application currently uses:
`instance/travelintel.db`

Do not put `.env`, API keys, or production secrets in Git.
