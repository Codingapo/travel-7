# TravelIntel update summary

## Unified authentication
- `/auth/login` is the single login page.
- `/auth/register` opens the same `auth.html` template in registration mode.
- The backend checks the email against `Users.user_type` and returns either `admin` or `client`.
- Admins are redirected to `/dashboard`; clients are redirected to `/home`.
- Direct `/auth.html` is redirected to `/auth/login`.
- `/` and `/home` render the same home page.

## Packages
- Customer package cards can be sorted by recommended, price low-to-high, price high-to-low, or most booked.
- Admins can create packages with package name, destination, price, available seats, duration, description, category and optional image URL.
- Custom packages are preserved across application restarts.
- Availability is displayed as a number, e.g. `10 Seats Available` or `0 Seats Available`.

## Booking and card validation
- Card numbers are checked in real time with the Luhn algorithm.
- Card expiry is checked in real time and must be a valid, non-expired `MM/YY` value.
- The backend repeats the card number and expiry validation before accepting a booking.
- Package capacity is checked again on the server so the browser cannot bypass availability.

## AI analysis
The AI analysis is date-scoped. It filters bookings, customers and reviews to the selected range, calculates KPIs, ranks destinations/packages, evaluates sentiment, reads the latest demand forecast and anomaly alerts, then generates plain-language insights and recommendations.

The AI page now also displays a short explanation of those steps.

## Latest dashboard and AI improvements
- Dashboard booking KPI now calculates total bookings and total travellers from Bookings.number_of_travelers.
- Recent booking activity shows the number of people on each booking and the amount paid.
- Dashboard now includes a Google Reviews summary card with review count, rating, source, and manual sync link.
- Google Reviews continue to come from the real Google Business Profile through SerpApi using SERPAPI_KEY and GOOGLE_PLACE_ID; no fake reviews are generated.
- AI Analysis now shows model training transparency: algorithm, sample count, training metrics, training window, features, target, and forecast horizon.
- Administrators can manually retrain the demand, segmentation, and anomaly models from the AI Analysis page; retraining is recorded in the admin audit log.
- Existing dark/glass dashboard UI was preserved; additions are integrated into the existing cards and navigation rather than replacing the interface.
