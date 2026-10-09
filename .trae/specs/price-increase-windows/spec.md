# Price-Increase Windows (Admin + Pricing) — Spec

## Problem

Admins can configure a temporary, date-windowed **discount** (%, from, until) on each package via the Add/Edit modals in the admin Package Inventory. The discount applies to bookings whose travel date falls in the window, is visible as a % badge + strikethrough original price on the customer side, and auto-reverts to the base price at 23:59 on the end date.

There is no equivalent mechanism for a **temporary price INCREASE**. Admins must manually overwrite `price` when they want to charge a premium (peak season / high-demand window) and then manually reset it later — which is error-prone, leaves no audit trail of the original/base price, and makes it impossible to schedule a premium ahead of time.

The customer UI currently only knows how to render price-lower-than-base (discount) treatments; there is no semantic for a price-higher-than-base (premium) that should simply be treated as the new normal price during its window (no "was $X, now $Y" visual difference).

## Users

- **Admin**: Creates / edits packages in `/admin/packages.html` and configures both discount and increase windows side-by-side.
- **Customer (browse)**: Lands on `/index.html` (Package grid) and `/booking.html` (package detail) and sees either base price or increased price depending on whether today falls in an active increase window (both look the same visually — just a plain price number; no badge, no strikethrough, no "premium" callout).
- **Customer (booking)**: `/bookings` POST calculates the effective unit price the same way browse does, locked to the selected travel date, and stores the resulting `total_amount` on the booking row.

## Goals

1. Parity with discount fields on admin Add/Edit forms: three new fields — **% Increase in price**, **Increase start date**, **Increase end date** — with the same validation rules (required together with non-zero %, start ≤ end, no past dates when *changing* the field).
2. Engine parity: server-side `_effective_package_increase_pct()` helper that mirrors `_effective_package_discount_pct()` semantics.
3. Premium pricing applies **to all travel dates** during the active increase window (same gating rules as discount: browse checks TODAY, booking check re-selected `travel_date` + today vs end_date).
4. Auto-revert at 23:59 on end date. The next day on the server (or client) a browse lookup yields base price again.
5. Customer UI: during an active increase, the number shown IS the new price (no visual "before/after" difference — no badge, no strikethrough, no "Premium" label). It should appear to the customer as if the base price simply changed.
6. Existing discount behavior is preserved and composes correctly with increase (increase *and* discount cannot/shouldn't be active simultaneously; if configured that way, which one wins must be deterministic and safe: **discount wins** because it is a promotional offer with explicit travel-date gating, while increase is a back-office base-price adjustment).

## Non-Goals

- No new UI to apply AI-recommended increase windows 1-click. This change is form-level only and mirrors how admins manually set discount fields today.
- No historical base-price tracking / price history table. The single `price` column continues to store the base price; increases are computed from it at read time.
- No change to the `/api/packages` API shape other than surfacing the 3 new columns (same way discount_* already is).
- No change to the Booking table schema. The booking continues to store the actual total charged at booking time.
- No customer-facing callouts, labels, or badges for the premium period.

## Functional Requirements

### DB / Schema (FR-1)

Packages table gains 3 new nullable columns:
- `increase_percentage INTEGER DEFAULT 0`
- `increase_start_date TEXT DEFAULT NULL`
- `increase_end_date TEXT DEFAULT NULL`

These MUST be created idempotently via `_ensure_column` in [database.py](file:///c:/Users/ajayi/OneDrive/Documents/GitHub/travel-7/database.py) in the same block as the existing `discount_percentage` / `discount_start_date` / `discount_end_date` ensures so every existing deployment migrates on next server startup.

### Pydantic Models (FR-2)

`PackageCreateRequest` and therefore `PackageUpdateRequest` MUST gain 3 new optional fields mirroring their discount counterparts:
```
increase_percentage: int = 0
increase_start_date: str | None = None
increase_end_date: str | None = None
```

### Admin CRUD Validation (FR-3)

Both the **POST /api/admin/packages** create and **PUT /api/admin/packages/{id}** update endpoints MUST enforce:
- `increase_percentage` between 0 and (some sensible cap — 100, same as discount percentage so a package can at most double in price via this mechanism).
- If `increase_percentage > 0`, then BOTH `increase_start_date` AND `increase_end_date` are required.
- ISO date validation (YYYY-MM-DD) for both, with explicit 400 detail messages matching discount error strings format (just swap "Discount" → "Increase").
- `start ≤ end`.
- **Past dates rejected when changing the field** (but allowed if the stored value is unchanged — this is the same rule we use for discounts in the update endpoint today). Exceptions compare against `before['increase_start_date']` / `before['increase_end_date']`.

Both INSERT and UPDATE SQL MUST include the 3 new columns in their column lists and bind the corresponding values. Audit log snippets MUST include `increase=N% window=start→end` the same way discount snippets do.

### Admin Package Inventory UI (FR-4)

[packages.html](file:///c:/Users/ajayi/OneDrive/Documents/GitHub/travel-7/templates/admin/packages.html) Add form and Edit modal each gain 3 new inputs placed directly BELOW the existing 3 discount inputs:
- Number input `increase` / `epIncrease` — range 0..100, default 0.
- Date inputs `increaseStartDate` / `epIncreaseStart` and `increaseEndDate` / `epIncreaseEnd` with `min=today` set dynamically on DOMContentLoaded (the same loop that sets it for discount date inputs today).

JavaScript submit-validation blocks on BOTH Add form and Edit confirm handler MUST add:
1. Increase required-together check matching discount required-together wording but for Increase.
2. start ≤ end check for increase dates.
3. Past-date reject check, with Edit-form leniency matching the original values loaded from `currentEditPackage.increase_*`.

Labels use the same layman-friendly uppercase tracking-widest style as the Discount labels. Suggested labels: "Price Increase % (0–100)", "Price Increase Valid From", "Price Increase Valid Until".

The admin package inventory TABLE (rendered by `loadPackages`) already shows discount badges / expiry pill in the Price column. That same renderer SHOULD surface the active increase window textually so the admin can glance and verify both are configured correctly (a 1-line suffix such as "· +20% 01 Dec – 31 Dec" in muted amber text, or hidden — but during Edit modal the fields are populated). The minimum requirement is that the edit modal pre-populates all 3 increase fields correctly in `openEditPackageModal`.

The `/api/admin/packages` SELECT must return the 3 increase columns.

### Effective-Price Engine (FR-5)

New helper `_effective_package_increase_pct(pkg_row, travel_date_iso=None) -> int` mirrors the discount helper in [app.py](file:///c:/Users/ajayi/OneDrive/Documents/GitHub/travel-7/app.py) line-by-line:
- If `increase_percentage == 0` → return 0.
- If both start/end are NULL → treat as permanently active (legacy compatibility, same as discount).
- If `travel_date_iso` is provided (booking flow):
  - Server today must be ≤ end_date (i.e. increase hasn't auto-reverted past 23:59 end).
  - AND travel date must be in [start, end].
  - If both pass return the percent, else 0.
- If `travel_date_iso is None` (browse flow):
  - Only return non-zero if **today** falls in [start, end].
- Return value clamped to [0, 100].

Composability rule (FR-5a): in every place where effective unit price is computed, **discount is applied first if active, and increase is NEVER applied if a non-zero discount is active for the same lookup**. Rationale: the increase effectively just raises the base price; but if there is a promotion on, the promotional price should be computed from the base price (NOT the inflated one). Implementation formula:
```
base = float(pkg['price'])
disc_pct = _effective_package_discount(pkg, travel_date_iso)
if disc_pct > 0:
    unit = base * (1 - disc_pct / 100)
else:
    inc_pct = _effective_package_increase_pct(pkg, travel_date_iso)
    unit = base * (1 + inc_pct / 100)
```

### Apply Effective Price Everywhere Price is Read (FR-6)

Effective price must be computed from the raw `p.price` column using both helpers in the following places:

1. **Customer browse / grid** — `GET /api/packages` and `GET /packages`. The SELECT still returns raw price plus increase_* and discount_* fields; EFFECTIVE PRICE is computed per-row server-side and added as a new `effective_price` field on the returned package dict. This keeps **both** clients (`main.js` browse grid **and** booking page) from having to implement correct cross-timezone date math on their own. The response continues to include `price` (base) unchanged.

2. **Customer booking detail** — `GET /api/packages/{id}`. Same treatment: return `effective_price` alongside base `price` + raw increase/discount fields.

3. **Booking POST** — `create_booking` unit_price calculation block. Replace the current `if discount_pct > 0: ...` block with the compose formula above so travel dates in peak periods correctly pay the premium, travel dates in promo periods pay the discounted price, and overlap resolves deterministically to discount.

4. **Admin dataset CSV export** — the current code applies `effective_discount_percent` when it computes `final_amount_per_traveler` for each booking row. That path must also be audited: since bookings store total_amount at booking time (which is already the resolved, real price the customer paid), historical rows DON'T need to be recomputed through increase_pct. Requirement: the export continues to use `r["total_amount"]` for totals and the `price_per_person` column reflects base price (historical) as today. No change needed here but must be checked.

### Customer Browse Rendering (FR-7)

`main.js` Package grid renderer (`renderPackages` / `loadPackages` / per-card price cell) MUST be updated to:
- Prefer `pkg.effective_price` as the number shown as the **main / plain price** in every card.
- Continue using the raw discount fields for discount-badge + strikethrough logic ONLY when a discount is actually active (`_browseDiscountStatus().tier == 'active' || 'legacy'`).
- During an active increase (effective_price > base price AND no active discount):
  - Plain number display, NO strikethrough of base price.
  - NO amber / "+20%" badge.
  - NO callout of any kind. It must look identical to a normal base price.
- If both an increase AND a discount happen to be configured (rare edge case, and our compose rule applies discount anyway): displays existing discount treatment (strikethrough base, discounted price shown, badge present) — exactly what happens today, no new UI.

### Customer Booking Page Rendering (FR-8)

`templates/booking.html` and its JS are audited to:
- Use `pkg.effective_price` for the displayed price on page load.
- Still compute discount-badge / date-range text correctly (legacy behavior) when a discount applies.
- Re-resolve final booking-unit price from server via the existing `/api/packages/{id}` read, but more importantly the **booking POST** is the authoritative calculation so even if client JS is stale the server-side number wins.

### Bookings Customer Listing (FR-9)

Customer `/profile.html` "My Bookings" view, and any other view that renders `total_amount` from stored `Bookings` rows — MUST remain unchanged; because `total_amount` is the historical realized price, no increase re-computation is applied there.

## Non-Functional Requirements

- **NFR-1 Deterministic fallback for NULL columns**: Same convention as discount — if both start/end are NULL and percent > 0, treat as permanently active (forward-compat with any future UI that wants a permanent price bump without end date).
- **NFR-2 Timezone-safe "end of day" expiry**: Comparison must be DATE-ONLY, using `datetime.date.today()` (server) on the server and `new Date(); setHours(0,0,0,0)` on client, to match existing discount expiry semantics (end of 23:59 on end_date means next calendar day = fully gone).
- **NFR-3 Cache busting**: If any customer JS file (`main.js`) changes, bump its `?v=` query string in the HTML include (per project conventions). If only server code + template change, no cache-bust needed for templates.
- **NFR-4 Audit parity**: `record_admin_audit` Create/Update package entries already include discount window. Must concatenate the increase window snippet alongside.
- **NFR-5 Symmetry with discount past-date rule**: The update endpoint already rejects "past dates unless unchanged" for discount. Increase MUST use the same pattern.

## Assumptions

- SQLite `TEXT` dates remain ISO-8601 (`YYYY-MM-DD`) exactly like discount today.
- `% Increase` is an integer percent (0–100), same as discount. If decimals are needed later, the cap + column type can change independently.
- A package whose effective unit price rounds to < 0.01 after math SHOULD be clamped to a minimum of 0.01 in the unit_price before multiplying by travelers.

## Open Questions

None. If admin accidentally configures discount AND increase for overlapping dates, the deterministic resolution (discount wins, implemented in FR-5a) is the policy.

## Acceptance Criteria

| ID | Type | Rule |
|---|---|---|
| AC-1 | rule | Creating a package with `increase_percentage > 0` but missing either increase_start_date or increase_end_date returns HTTP 400 with a message that explicitly mentions Increase start date / end date. |
| AC-2 | rule | Creating a package with increase_start_date = yesterday returns HTTP 400 "… cannot be in the past …" on the start date. |
| AC-3 | rule | Updating a package and sending the same existing increase_start_date = 2 days ago does NOT return 400; only sending a *changed* date in the past returns 400. (Matches discount update leniency.) |
| AC-4 | rule | Updating a package stores increase_* columns. Server logs (audit record row in DB for that update) contains the increase_percentage value and window in the extra_info field, same format as discount. |
| AC-5 | rule | Given a package with base price 1000, increase_percentage = 20, start_date = today, end_date = today + 7 days — GET `/api/packages` returns for that row: `effective_price == 1200.0` AND `price == 1000.0`. |
| AC-6 | rule | Same package as AC-5 when queried next calendar day after end_date: `effective_price == 1000.0` (auto-revert at end of 23:59 on end_date). |
| AC-7 | rule | Same package as AC-5 if I POST a booking with travel_date = today: unit_price used in total_amount calculation is 1200.0 (× travelers, rounded to 2 decimals), and booking's `total_amount` equals `1200.0 * n_travelers` exactly. |
| AC-8 | rule | Same package as AC-5 if I also set discount_percentage = 10 with start=today and end=today — the booking resolve and `/api/packages` effective_price both show `1000 * 0.9 = 900` (discount wins; increase has zero effect when any discount is active for the same lookup). |
| AC-9 | rule | Customer browse `/` package cards for an actively-increased package (no discount active) show ONLY a single plain price number. There is NO strikethrough element, no "+" badge, no color styling different from a vanilla base-price package. (Diff the rendered card HTML with a plain base-price package; they should be structurally identical.) |
| AC-10 | rule | Customer booking page `/booking.html?package_id=N` for actively-increased package uses `effective_price` as its visible price. The submit action's final total_amount paid matches server POST's calculated total (implicitly covered by using server-authoritative calc in POST). |
| AC-11 | rule | Admin `/admin/packages.html` DOMContentLoaded sets `min=todayISO` on the two new increase date inputs, same as it already does for discount date inputs. |
| AC-12 | rule | Edit-modal pre-fill (`openEditPackageModal(pkg)`) correctly populates `epIncrease`, `epIncreaseStart`, `epIncreaseEnd` fields from `pkg.increase_percentage / increase_start_date / increase_end_date`. |
| AC-13 | rubric | Implementation fidelity across discount↔increase parity (form layout, validation messages, endpoint checks) — scale 0–2. Pass threshold ≥ 1.5. Evidence: code diffs between increase and discount blocks in packages.html and app.py Package CRUD endpoints. |
