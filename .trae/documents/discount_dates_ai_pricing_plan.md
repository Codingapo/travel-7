# Discount Date Windows + AI Price Increase Recommendations — Implementation Plan

## Repository Research

### Current Architecture
- **Packages table** has a single `discount_percentage` (INTEGER DEFAULT 0) column — no date-window awareness. The discount is treated as permanently active wherever `discount_percentage > 0`.
- **Booking flow** at [app.py](file:///c:/Users/ajayi/OneDrive/Documents/GitHub/travel-7/app.py#L2580-L2593) applies the discount unconditionally if `discount_percentage > 0`, checking **neither** booking date nor travel date.
- **Admin forms** ([packages.html](file:///c:/Users/ajayi/OneDrive/Documents/GitHub/travel-7/templates/admin/packages.html)) have one `discount %` number input on both Add (line 156) and Edit (line 243) modals. No date inputs.
- **Customer booking page** ([booking.html](file:///c:/Users/ajayi/OneDrive/Documents/GitHub/travel-7/templates/booking.html#L362-L385)) shows discount badge + line-through price whenever `discountPct > 0`; does NOT re-evaluate based on the user-selected travel date.
- **AI recommendation engine** ([ai_engine.py](file:///c:/Users/ajayi/OneDrive/Documents/GitHub/travel-7/ai_engine.py#L1220-L1455)) computes a single `discount_suggested` value and has 3 output tiers: big discount (≥15%), small discount (5–14%), or "hold current price". There is **no price-increase recommendation path** even when all signals are strongly positive. The recommendation DB row stores only `discount_suggested` with no suggested date window.
- **Bookings schema** already has a `travel_date` column (DATE, stored as ISO string). The seeded data and booking POST both set this correctly.
- **Polling + cache-busting** already in place on admin pages — discounts that auto-expire will be reflected within each page's polling cadence without extra work.

### Hard Constraints (from project memory)
- Only real Google reviews are used (no seeded). Admin actions require the current admin password. Weather stats use daytime-high averages from `/forecast` endpoint, not instantaneous.

## Files and Modules

| File | Expected Change |
|---|---|
| [database.py](file:///c:/Users/ajayi/OneDrive/Documents/GitHub/travel-7/database.py) | Add 2 columns to Packages (`discount_start_date`, `discount_end_date`); expand Recommendations schema with `price_change_type`, `price_change_percent`, `rec_start_date`, `rec_end_date`. |
| [app.py](file:///c:/Users/ajayi/OneDrive/Documents/GitHub/travel-7/app.py) | Add date fields to Pydantic package models; persist in create/update endpoints; add `_effective_package_discount()` helper gating on travel_date; fix booking calc & CSV export; pass new rec columns to AI endpoint. |
| [ai_engine.py](file:///c:/Users/ajayi/OneDrive/Documents/GitHub/travel-7/ai_engine.py) | Refactor `generate_recommendations()` into 3-way classifier (DISCOUNT / INCREASE / HOLD); compute suggested date windows from peak travel month; store all 4 new recommendation columns. |
| [templates/admin/packages.html](file:///c:/Users/ajayi/OneDrive/Documents/GitHub/travel-7/templates/admin/packages.html) | Add `discount_start_date` + `discount_end_date` date inputs to Add form and Edit modal; gating Price cell rendering; show discount-period pill; include dates in JS payloads. |
| [templates/booking.html](file:///c:/Users/ajayi/OneDrive/Documents/GitHub/travel-7/templates/booking.html) | Accept dates in package payload; LIVE-reevaluate discount when travel-date changes; show discount-validity-range caption; hide discount if travel date outside window. |
| [templates/index.html](file:///c:/Users/ajayi/OneDrive/Documents/GitHub/travel-7/templates/index.html) | (Package card rendering logic) — only show discount badge if the discount is currently in-effect (today in start..end window); show "Valid until" tooltip. |
| [templates/admin/ai-analysis.html](file:///c:/Users/ajayi/OneDrive/Documents/GitHub/travel-7/templates/admin/ai-analysis.html) | Refactor `renderPackageRecommendations` to handle 3 pricing types (Discount red / Increase gold / Hold green); display rec start/end dates; new pill icons/labels. |

## Implementation Steps (dependency-ordered)

### Phase 1 — Database schema (must run first on startup)
1. In `database.py` inside the schema-migration block, call `_ensure_column` for:
   - `Packages.discount_start_date` (TEXT DEFAULT NULL, ISO `YYYY-MM-DD`)
   - `Packages.discount_end_date` (TEXT DEFAULT NULL)
   - `Recommendations.price_change_type` (TEXT DEFAULT 'hold' — enum: `'discount' | 'increase' | 'hold'`)
   - `Recommendations.price_change_percent` (REAL DEFAULT 0 — always positive; direction lives in `price_change_type`)
   - `Recommendations.rec_start_date` (DATE)
   - `Recommendations.rec_end_date` (DATE)

### Phase 2 — Backend model + package CRUD (app.py)
2. Add fields `discount_start_date: str | None = None` and `discount_end_date: str | None = None` to both `PackageCreateRequest` and `PackageUpdateRequest` Pydantic models.
3. In `create_package` (POST `/api/admin/packages`) and `update_package` (PUT `/api/admin/packages/{id}`):
   - Validate: if discount_start > discount_end → HTTP 400.
   - Validate: if discount_percentage == 0 then dates are optional; if discount_percentage > 0 then BOTH dates are required.
   - Persist the two new columns in INSERT + UPDATE SQL (adjust the column lists, placeholders, and params tuples).
4. Update every package SELECT (admin list, customer list, booking lookup, CSV) to also pull `discount_start_date`, `discount_end_date`.
5. **Define a single Python helper** `_effective_package_discount(pkg_row, travel_date_iso=None) -> int` that:
   - Returns `0` immediately if `discount_percentage == 0` OR either date is NULL.
   - If `travel_date_iso` is supplied → check that travel_date is BETWEEN start AND end (inclusive). This is the booking-time path.
   - If `travel_date_iso` is NULL (display contexts like admin list / customer browse) → check that TODAY is BETWEEN start AND end. For upcoming discounts, returns 0 for display but the admin row can still show "Upcoming" info pill.
6. **Replace the unconditional discount in the booking flow** at [app.py](file:///c:/Users/ajayi/OneDrive/Documents/GitHub/travel-7/app.py#L2589-L2593) with a call to the helper, passing the already-parsed `travel_date_obj.isoformat()`. Add booking-confirmation response fields showing what discount (if any) was applied, and the reason when it was skipped (e.g., "Travel date outside discount window").
7. Update CSV export + admin dashboard package SQL to include the new columns.
8. Update the AI-analysis API response SELECT (line 3472+) so `package_recommendations` includes the 4 new columns.

### Phase 3 — AI engine (ai_engine.py)
9. Refactor the pricing block in `generate_recommendations()`:
   - Keep all current signal accumulators but rename the variable from `discount` → `price_score`.
   - **New threshold logic** (example ranges; actual tuned values stay close to current):
     - `price_score >= +10` → type `'increase'`, magnitude = min(30, price_score) % (cap 30%).
     - `0 < price_score < 10` → type `'hold'`, magnitude = 0%.
     - `-10 <= price_score <= 0` → type `'hold'` / light `'discount'` option mirroring current wording.
     - `price_score < -10` → type `'discount'`, magnitude = min(30, -price_score) %.
   - The signal weights currently ADD for discount signals and SUBTRACT for premium signals. Invert direction: premium signals (high demand, peak/summer, high rating, positive news, low seat ratio) make `price_score` more POSITIVE toward an INCREASE. Weak signals make it NEGATIVE toward a DISCOUNT. Keep existing numeric sensitivities, just reorient.
10. **Compute the recommended date window**:
    - Reuse the per-package peak travel month already computed (`tmonth`).
    - Start date = first day of `tmonth` in `today.year` (or `today.year + 1` if `tmonth < today.month`, i.e., peak already passed this year).
    - End date = last day of `tmonth` (or 2 weeks after start if using a tighter peak).
    - For discount recommendations, shift the window forward/back to the off-peak month nearest the destination (deterministic via climate profile).
11. INSERT statements in `Recommendations` must now populate all 4 new columns. Rename deprecated `discount_suggested` → still write it (backward-compat) but the new columns are canonical.

### Phase 4 — Admin UI (packages.html)
12. In the **Add Package form** (line 152 grid), add two new `type="date"` inputs:
    - `#discountStartDate` (label: "Discount Valid From")
    - `#discountEndDate` (label: "Discount Valid Until")
13. In the **Edit modal** (line 235 grid), add the same two fields:
    - `#epDiscountStart` + `#epDiscountEnd`
    - `openEditPackageModal()` must populate them from the fetched package object (line 500s).
14. In the `submit` handlers (Add flow ~line 430, Edit flow ~line 585), read the date fields and include them in the JSON payload sent to the backend.
15. In `loadPackages()` → Price column rendering (lines 343-358):
    - Call a JS helper `isDiscountActive(pkg, today)` before showing the red "% OFF" pill.
    - When discount is defined but not active, add a small grey info pill, e.g., "Upcoming 15% · 1 Dec – 15 Jan" (future) or "Expired 15% · ended 30 Jun" (past).
    - When discount IS active, keep the current red pill + line-through style and add "Valid until <endDate>" underneath in green-400 text-xs.

### Phase 5 — Customer side
16. **booking.html**:
    - Add `discountStartDate`, `discountEndDate` to the vars set alongside `discountPct` (~line 363).
    - In the `travel-date` `onchange` handler (and `updateTotalPrice`), re-run:
      - compute `const effectiveDiscount = (travelDateInWindow(dates)) ? discountPct : 0;`
      - RE-calc `basePrice`, RE-render `#pkg-price-block`, and RE-render breakdown.
    - Add a tiny banner inside the price block that says:
      - Discount active for this travel date → **"Discount applied. Valid for travel: 1 Dec – 15 Jan 2027"** in green.
      - Discount exists but travel date out of range → **"This package has a 15% discount for travel 1 Dec – 15 Jan 2027. Adjust your travel date to unlock it."** in amber.
17. **index.html** package cards:
    - Render the "% OFF" badge only when `today()` falls inside the discount window. For future discounts add a "Upcoming discount · starts <date>" ribbon subcaption. For expired don't render anything.

### Phase 6 — AI Analysis UI (ai-analysis.html)
18. Rewrite `renderPackageRecommendations()` (line 395):
    - For each rec, read `price_change_type` (fallback: `discount_suggested > 0 → 'discount' else 'hold'` for older rows).
    - Three styling tiers:
      - **DISCOUNT** → reds (today's "fa-tags", "##% DISCOUNT suggested") + add date window line `Valid window: rec_start_date – rec_end_date`.
      - **INCREASE** → gold (`text-amber-400`, bg `bg-amber-500/10 border-amber-500/20`, icon `fa-arrow-trend-up`, label "##% PRICE INCREASE opportunity").
      - **HOLD** → emeralds (today's style) + add small note "No change needed for upcoming <peak/offpeak> window <dates>".
    - Add a 2-line footer on each rec card:
      - Line 1 — `<i class="fas fa-calendar"></i> Suggested window: <start> → <end>`
      - Line 2 — Generated date (existing).

## Dependencies and Considerations
- **Date types / ISO format**: SQLite has no native DATE; use ISO-8601 strings (`YYYY-MM-DD`). All comparisons use `date()` SQL helpers or Python `datetime.date.fromisoformat()` to avoid any locale-dependent issues.
- **Backward compat for old rows**: rows where start/end are NULL and `discount_percentage > 0` → treat as "permanently active" (legacy mode; equivalent to no window gating). This preserves existing seeded demo packages until the admin populates dates.
- **Recommendations schema**: Keep writing the deprecated `discount_suggested` column for 1–2 release cycles so any cached/stale AI analysis pages still render. But `price_change_type + price_change_percent` are canonical going forward.
- **Booking cancellation / reprice**: Out of scope. The discount is locked at booking-insert time (it's stored already-computed in `Bookings.total_amount`), which is the correct accounting behaviour.
- **Admin password requirement**: Remains unchanged (already enforced in create/update/delete package flows).
- **Polling**: No changes needed — admin list polling already refreshes every ~5–30s, so auto-expired discounts will naturally drop the highlight from the Price cell on the next polling tick, and CSV export will regenerate fresh data.

## Validation
1. **Schema migration smoke-test**: Start the server (which calls `init_db()`) and confirm no errors; run a quick SQLite PRAGMA table_info check for the 6 new columns.
2. **Admin create/edit workflow**: Use the Packages admin page to create a package with a 10% discount valid for a 1-week future window. Then create a second package with a 10% discount valid for a 1-week PAST window.
3. **Admin list rendering**: The first (future) package → must show "Upcoming 10% · <date range>" info pill, NO red % OFF badge, original price. The past package → "Expired 10%" pill only. The active window package → red OFF + green "Valid until".
4. **Customer browse (index.html)**: Active only shows %OFF. Future/expired show normal price.
5. **Booking date gating E2E**: Pick the future-window package. On booking.html:
   - Select a travel date **inside** the window → discounted price shown, green banner.
   - Change travel date to **outside** the window → automatically reverts to full price, amber hint appears.
   - Submit the booking inside window → confirm via `/api/bookings` payload & email confirmation total equals discounted amount.
6. **AI 3-way pricing**: Force a known strongly-signal package (high 4.7* rating, summer/peak, high demand, low seats) → run "Regenerate" AI, confirm it returns `price_change_type='increase'` with a +10–25% magnitude and sensible peak-month dates. Force a low-demand / low-rated / winter package → confirm it still recommends `'discount'`.
7. **AI UI render**: All 3 cards (Discount/Increase/Hold) render their correct colour scheme, icon, label, and `<start> → <end>` suggested date window.
8. **CSV export**: Download the bookings CSV; confirm it still loads and the `Discount_Percentage` column now shows only effective discounts (matches gating rules).
9. Run the existing `_smoketest_v2.py` `generate_recommendations()` test; it should still produce rows (confirming no regressions).

## Risks
- **Legacy NULL discount dates**: Mitigated by the "NULL ⇒ permanently active" fallback so existing seeded demo packages continue to work as before; the admin can optionally add date ranges later.
- **Booking-flow bug silently dropping discount**: Mitigated by writing a single `_effective_package_discount()` helper used everywhere (browse display + booking calc + CSV), instead of copy-pasted date checks.
- **Negative price_score direction confusion**: Mitigated by adding unit-style inline assertions during `generate_recommendations()` (comments + smoke test) that a strongly-positive-signal Cape Town summer package must yield type `increase`, not `discount`.
- **Customer-side JS travel-date re-render**: Mitigated by re-using the already-existing `updateTotalPrice()` function and making the discount recalc part of that single call-path (no scattered listeners).
- **Stale recommendation rows**: Mitigated by keeping old `discount_suggested` writes and adding the `if/else` fallback in `renderPackageRecommendations()` so pre-migration rows continue to render.
