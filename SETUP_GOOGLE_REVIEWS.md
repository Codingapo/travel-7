# Getting real Google Reviews working (free, no billing account)

## What was actually wrong

See `COMPLETE_FREE_SETUP_GUIDE.md` for the full explanation and beginner
walkthrough. Short version: the old scraper fabricated fake reviews whenever
it failed to parse Google's search HTML (which was almost always). It's been
replaced with `google_reviews_sync.py`, which pulls real data from Google via
**SerpApi** - a third-party API that has a genuinely free tier with **no
credit card required**, unlike Google's own Places API (which requires a
linked billing account even to use its free quota).

## Environment variables needed

```
SERPAPI_KEY=your_serpapi_key_here
GOOGLE_PLACE_ID=your_google_place_id_here
```

Set these in a `.env` file in the project root (already auto-loaded by
`app.py` and `google_reviews_sync.py` via `python-dotenv`, already listed in
`requirements.txt`).

## Where to get each value

- **SERPAPI_KEY**: sign up free at https://serpapi.com/users/sign_up (no card
  needed) then copy the key from your dashboard.
- **GOOGLE_PLACE_ID**: use Google's free Place ID Finder tool at
  https://developers.google.com/maps/documentation/places/web-service/place-id
  and search for `Dalani, 11 Pierre St, Bendor Ext 30, Polokwane, 0699`.

## Testing

```
python google_reviews_sync.py
```

Success looks like:
```
Starting SerpApi Google Reviews sync...
Synced 20 real Google reviews (rating=4.6, total_reviews=139).
```

## Ongoing updates

- Runs automatically every 4 hours via the existing `apscheduler` job in `app.py`.
- Click **Refresh** on the admin Reviews page to sync immediately at any time.
- Each sync pulls up to 20 of the most recent reviews (`num=20`,
  `sort_by=newestFirst`), so newly posted Google reviews will appear on the
  next scheduled sync (within 4 hours) or immediately via Refresh.

## Free tier limits

SerpApi's free plan includes roughly 100-250 searches/month (check your
dashboard for your exact allowance) with no card required. Syncing every 4
hours uses about 180 searches/month, comfortably within the free tier. If you
click Refresh very frequently in addition to the scheduled syncs, you could
exceed the free monthly quota - in that case, syncing simply pauses until
next month, or you can upgrade if you ever need it. Nothing breaks; it just
stops updating and the last good data stays in place.
