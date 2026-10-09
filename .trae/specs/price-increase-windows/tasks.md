# Price-Increase Windows — Task Plan

## Task List

### Task 1: Database Schema Migration
**Priority**: high
**Status**: pending

Add 3 new `_ensure_column` calls in [database.py](file:///c:/Users/ajayi/OneDrive/Documents/GitHub/travel-7/database.py#L411-L417) immediately under the existing discount_percentage / discount_start_date / discount_end_date lines:
- `increase_percentage` INTEGER DEFAULT 0
- `increase_start_date` TEXT DEFAULT NULL
- `increase_end_date` TEXT DEFAULT NULL

Order them to keep `percentage → start_date → end_date` mirroring the discount trio.

#### Test Requirements

| TR | Type | Rule |
|---|---|---|
| T1-1 | rule | `python -c "import database; database.init_db()"` runs without error. |
| T1-2 | rule | New SQLite columns exist and default correctly (0 / NULL / NULL for existing rows). |

---

### Task 2: Pydantic Models + CRUD SQL + Validation
**Priority**: high
**Status**: pending

Scope: [app.py](file:///c:/Users/ajayi/OneDrive/Documents/GitHub/travel-7/app.py)

1. **PackageCreateRequest / PackageUpdateRequest** (around L2217): add 3 increase fields mirroring discount.
2. **POST /api/admin/packages** create:
   - Add validation block (0 ≤ inc ≤ 100, required-together with non-zero %, ISO parse, start ≤ end, no past dates).
   - Append columns + bind params to INSERT SQL.
   - Build increase_window_snippet alongside discount_window_snippet and concatenate both into the `record_admin_audit` extra_info.
3. **PUT /api/admin/packages/{package_id}** update:
   - Same validation block. Same "past dates allowed if unchanged" leniency (compare to `before.get('increase_start_date')` / `before.get('increase_end_date')`).
   - Append columns + binds to UPDATE SQL.
   - Build before/after snippets mirroring discount pattern.
4. **GET /api/admin/packages** list SELECT: add the 3 increase_* columns.
5. **GET /api/packages** and **GET /packages** browse SELECT (around L2199): add the 3 increase_* columns; then after converting rows to dicts, compute `effective_price` per row using the compose formula (Task 3 helper).
6. **GET /api/packages/{id}** single-package SELECT + `effective_price` add.
7. **GET /api/dataset/export-bookings.csv** — no changes needed (historical booking total_amount); verify the path.

#### Test Requirements

| TR | Type | Rule |
|---|---|---|
| T2-1 | rule | `py_compile` on app.py succeeds after edits. |
| T2-2 | rule | POST with increase=15 and no dates → 400 detail referencing Increase start/end dates. |
| T2-3 | rule | POST with increase=15 + start=yesterday + end=tomorrow → 400 start-date-in-past. |
| T2-4 | rule | PUT unchanged existing increase_start_date already-in-past → 200 OK, no error. |
| T2-5 | rule | GET `/api/packages` row contains `increase_percentage`, `increase_start_date`, `increase_end_date`, and `effective_price` keys. |

---

### Task 3: Effective-Increase Helper + Composable Pricing
**Priority**: high
**Status**: pending

Scope: [app.py](file:///c:/Users/ajayi/OneDrive/Documents/GitHub/travel-7/app.py#L2254-L2315)

1. Add `_effective_package_increase_pct(pkg_row, travel_date_iso=None) -> int` right after `_effective_package_discount_pct`; mirror logic line-by-line (today-check / travel_date-check / NULL-as-permanent semantics).
2. Create a single small wrapper `_compute_effective_unit_price(pkg_row, travel_date_iso=None) -> float` that implements:
   - disc = `_effective_package_discount_pct`
   - if disc > 0 → base * (1 − disc/100) (discount wins; ignore increase)
   - else inc = `_effective_package_increase_pct` → base * (1 + inc/100)
   - clamp result ≥ 0.01
3. **Create booking block** (around L2728): replace ad-hoc unit_price computation with the wrapper.

#### Test Requirements

| TR | Type | Rule |
|---|---|---|
| T3-1 | rule | Package base=1000, inc=20, window active, no discount → `_compute_effective_unit_price` returns 1200.0. |
| T3-2 | rule | Same package + active discount=10 → returns 900.0 (discount wins, inc ignored). |
| T3-3 | rule | Same package after end_date passes → returns exactly 1000.0 (auto-revert). |
| T3-4 | rule | POST booking for travel_date inside the increase window, n=2 travelers, total_amount == 2400.00 (exact match of 2×1200). |

---

### Task 4: Admin Package Inventory UI Fields + JS Validation
**Priority**: high
**Status**: pending

Scope: [packages.html](file:///c:/Users/ajayi/OneDrive/Documents/GitHub/travel-7/templates/admin/packages.html)

1. **Add form** (under discount date pair inputs): inject one 4-cell row — increase % number input, then Valid From / Valid Until date inputs (match discount label style). Use IDs `increase`, `increaseStartDate`, `increaseEndDate`.
2. **Edit form** (under discount date pair in the modal, L252-L255): inject 3 cells — IDs `epIncrease`, `epIncreaseStart`, `epIncreaseEnd`.
3. **DOMContentLoaded min-date loop**: extend the 4-element array with the 4 new IDs so all 8 date inputs get `el.min = todayISO`.
4. **openEditPackageModal(pkg)**: populate 3 new fields from `pkg.increase_percentage` / `increase_start_date` / `increase_end_date`.
5. **Add-form submit handler**: after the existing discount validation block, add the same 5 checks for increase (required-together, ISO-length sanity, start ≤ end, no past dates).
6. **Edit-form confirm handler** `confirmEditWithPassword()`: same validation with leniency — only reject past date if `discountStart !== origStart` equivalent for increase fields against `currentEditPackage.increase_*`.
7. **Add-form payload build**: include increase_* keys on the payload object.
8. **Edit-form payload build**: include increase_* keys on the payload object.
9. (Optional) Admin table Price column: surface the active-increase window for admin visibility, e.g. a muted amber line "· +X% valid S → E" if an increase is active/upcoming. At minimum, `loadPackages()` data already contains the keys after T2-5.

#### Test Requirements

| TR | Type | Rule |
|---|---|---|
| T4-1 | rule | Add form rendered HTML contains `<input id="increaseStartDate" type="date">` with a non-empty `min` attribute set to today's ISO after DOMContentLoaded. |
| T4-2 | rule | In Add form, entering increase > 0 and blank start date → inline red message references "Increase Valid From" / "Price Increase Valid Until" and blocks fetch POST. |
| T4-3 | rule | Edit modal populates the 3 increase fields correctly on open. |
| T4-4 | rule | Edit modal: change only epName, original increase_start already-past unchanged → submit allows through → fetch PUT reaches server (no JS-side block). |
| T4-5 | rule | Edit modal: change epIncreaseStart to a past date → client-side error message blocks PUT before fetch. |

---

### Task 5: Customer Browse + Booking Price Rendering
**Priority**: high
**Status**: pending

Scope:
- [main.js](file:///c:/Users/ajayi/OneDrive/Documents/GitHub/travel-7/static/js/main.js)
- [templates/index.html](file:///c:/Users/ajayi/OneDrive/Documents/GitHub/travel-7/templates/index.html) — cache-bust `?v=` bump for `main.js`
- [templates/booking.html](file:///c:/Users/ajayi/OneDrive/Documents/GitHub/travel-7/templates/booking.html) + any inline JS that shows price.

1. **main.js** `loadPackages` / render card: wherever raw `p.price` is formatted as the main price number, replace with `Number(p.effective_price || p.price)`. Keep the discount-badge logic intact (it should still strike through effective base from... wait — base price must be `p.price`, effective is `p.effective_price`. Discount logic compares against `discount_percentage` and strikes through `p.price` when discount is active. When an increase is active and no discount: `effective_price > p.price` but we render JUST effective_price as the single plain number — NO strikethrough, NO +badge.)

   Simplest rendering rule for each card price cell:
   - If active discount (per `_browseDiscountStatus` tier): render strikethrough `p.price` + big red `effective_price` with % badge → same as today (because effective_price already has discount baked in, per our server-side compose rule discount wins anyway).
   - Else: render one plain bold number `effective_price` with ZERO extra elements.

2. **booking.html**: anywhere the package price is shown on page load (header, summary, etc.) — server side or via JS fetch — use `pkg.effective_price` as the displayed amount. Because the POST computes its own number server-side, even if a stale client displays a wrong interim number the server is authoritative (and the displayed amount in summary right before submit must reflect the selected travel date; recheck).

3. **Bump cache-bust version** on `main.js` include (currently main.js?v=… per project_memory: use `?v=1.4` or higher).

#### Test Requirements

| TR | Type | Rule |
|---|---|---|
| T5-1 | rule | Browse card for an active-increase package has a single price number, NO `<s>` strikethrough element, NO "+%" badge element. |
| T5-2 | rule | Browse card for active-discount package still renders strikethrough, badge, and discounted price as before (discount wins). |
| T5-3 | rule | Booking page loads and displays the `effective_price` (higher than base) as a plain price without any callout. |
| T5-4 | rule | Browser DevTools console: `main.js?v=` URL contains a version string strictly higher than the pre-change value recorded in the repo baseline. |

---

### Task 6: End-to-end Validation + Audit Checks
**Priority**: medium
**Status**: pending

1. Run py_compile on all modified Python files.
2. Run GetDiagnostics for any VS Code / type errors.
3. Create a synthetic scenario by curling or clicking through the UI end-to-end and inspecting:
   - Package create via form → DB row has increase_* populated.
   - `/api/packages` returns correct effective_price.
   - Booking POST with travel_date in window → `total_amount` = effective × travelers.
   - Booking POST with travel_date outside window → total_amount = base × travelers.
   - Audit DB row `Admin_Audit` for the create/update has the increase snippet in `extra_info`.
4. Hard refresh (Ctrl+Shift+R) of `/` after bumping `main.js` cache-version confirms new price rendering without cache.

#### Test Requirements

| TR | Type | Rule |
|---|---|---|
| T6-1 | rule | Python syntax check + diagnostics both pass with zero errors. |
| T6-2 | rule | `/api/admin/audit-logs` or Admin Audit page for the CRUD actions includes increase window text in the action payload. |
| T6-3 | rule | Two identical bookings except travel_date: in-window vs out-window `total_amount` values differ by exactly the increase % when no discount. |

---

## Criterion → Task Coverage Map

| AC | Tasks |
|---|---|
| AC-1, AC-2 | Task 2 |
| AC-3 | Task 2 |
| AC-4 | Task 2 + Task 6 |
| AC-5, AC-6 | Task 2 + Task 3 |
| AC-7 | Task 3 |
| AC-8 | Task 3 |
| AC-9 | Task 5 |
| AC-10 | Task 3 + Task 5 |
| AC-11, AC-12, AC-13 | Task 4 |
