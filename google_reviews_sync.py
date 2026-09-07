"""
Real Google Reviews sync via SerpApi's Google Maps Reviews API.

This replaces the old approach of scraping Google Search's HTML directly,
which never worked reliably and, whenever it came up empty, silently
GENERATED FAKE REVIEWS instead of real ones. That's why the admin panel
never matched the business's actual Google reviews.

This module uses SerpApi (https://serpapi.com) instead of Google's own
Places API, specifically because SerpApi has a free tier (no credit card
required) that comfortably covers syncing every few hours, whereas Google's
Places API requires a billing account to be linked even to stay within the
free usage tier. See COMPLETE_FREE_SETUP_GUIDE.md in the project root for
the exact, no-cost setup steps.

Behavior on failure (missing API key/place ID, network error, quota, etc.):
  The existing reviews already stored in the database are left untouched.
  Nothing fake is ever generated or inserted.
"""
import datetime
import os
from typing import Dict, List, Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import requests

from database import get_db_connection, upsert_review_summary

SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "")
GOOGLE_PLACE_ID = os.environ.get("GOOGLE_PLACE_ID", "")

SERPAPI_URL = "https://serpapi.com/search"


def calculate_sentiment(text: str) -> float:
    positive_words = [
        "great", "excellent", "good", "amazing", "fantastic", "seamless",
        "loved", "beautiful", "perfect", "friendly", "helpful", "highly recommend", "professional",
    ]
    negative_words = [
        "bad", "terrible", "awful", "poor", "delayed", "steep", "issue",
        "complaint", "expensive", "late", "disappointing", "worst", "unhappy",
    ]
    text_lower = (text or "").lower()
    pos_count = sum(1 for word in positive_words if word in text_lower)
    neg_count = sum(1 for word in negative_words if word in text_lower)
    total = pos_count + neg_count
    if total == 0:
        return 0.0
    return (pos_count - neg_count) / total


def _parse_review_date(review: Dict) -> str:
    iso_date = review.get("iso_date")
    if iso_date:
        try:
            return datetime.datetime.fromisoformat(iso_date.replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            pass
    return datetime.date.today().isoformat()



# SerpApi's google_maps_reviews engine only returns ~8-10 reviews per page.
# Getting the full set (e.g. all 138) requires following serpapi_pagination
# -> next_page_token across multiple requests. MAX_PAGES is a safety cap so
# a misbehaving/huge listing can't loop forever or blow through the whole
# monthly SerpApi quota in one sync.
MAX_PAGES = 25


def _fetch_all_review_pages(base_params: Dict) -> (List[Dict], Optional[Dict]):
    """Follow next_page_token until Google/SerpApi has no more pages, the
    safety cap is hit, or we've collected as many reviews as place_info
    reports exist. Returns (raw_reviews, place_info_from_first_page)."""
    all_raw_reviews: List[Dict] = []
    place_info: Optional[Dict] = None
    total_reviews: Optional[int] = None
    params = dict(base_params)
    next_page_token = None

    for page_num in range(1, MAX_PAGES + 1):
        if next_page_token:
            params["next_page_token"] = next_page_token

        try:
            resp = requests.get(SERPAPI_URL, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.RequestException as exc:
            print(f"SerpApi request failed on page {page_num}: {exc}. Using what was fetched so far.")
            break

        status = (data.get("search_metadata") or {}).get("status")
        if status != "Success":
            error = data.get("error") or data
            print(f"SerpApi returned status={status} on page {page_num}: {error}. Using what was fetched so far.")
            break

        # Only the first page includes place_info (rating/total review count).
        if page_num == 1:
            place_info = data.get("place_info") or {}
            total_reviews = place_info.get("reviews")

        page_reviews = data.get("reviews", []) or []
        all_raw_reviews.extend(page_reviews)
        print(f"SerpApi page {page_num}: {len(page_reviews)} reviews (running total {len(all_raw_reviews)}).")

        if total_reviews and len(all_raw_reviews) >= int(total_reviews):
            break

        next_page_token = ((data.get("serpapi_pagination") or {}).get("next_page_token"))
        if not next_page_token:
            break

    return all_raw_reviews, place_info


def fetch_reviews() -> int:
    """Sync real reviews + rating from Google via SerpApi. Returns the number
    of written reviews stored. Never fabricates data - on any failure it just
    leaves the database as-is and returns 0."""
    print("Starting SerpApi Google Reviews sync...")

    if not SERPAPI_KEY:
        print("SERPAPI_KEY is not set - skipping sync. See COMPLETE_FREE_SETUP_GUIDE.md.")
        return 0
    if not GOOGLE_PLACE_ID:
        print("GOOGLE_PLACE_ID is not set - skipping sync. See COMPLETE_FREE_SETUP_GUIDE.md.")
        return 0

    base_params = {
        "engine": "google_maps_reviews",
        "place_id": GOOGLE_PLACE_ID,
        "sort_by": "newestFirst",
        "hl": "en",
        "api_key": SERPAPI_KEY,
    }

    raw_reviews, place_info = _fetch_all_review_pages(base_params)

    if place_info is None:
        # The very first request failed outright.
        print("SerpApi sync failed before returning any data. Keeping existing reviews unchanged.")
        return 0

    average_rating = place_info.get("rating")
    total_reviews = place_info.get("reviews")

    if average_rating is not None and total_reviews is not None:
        upsert_review_summary("google", float(average_rating), int(total_reviews))

    fetched_reviews: List[Dict] = []
    for r in raw_reviews:
        text = (r.get("snippet") or "").strip()
        if not text:
            continue
        fetched_reviews.append({
            "rating": int(r.get("rating") or 5),
            "reviewer_name": (r.get("user") or {}).get("name", "Google Reviewer"),
            "review_text": text[:1000],
            "review_date": _parse_review_date(r),
        })

    if not fetched_reviews:
        print("SerpApi returned no written reviews. Keeping existing reviews unchanged.")
        return 0

    conn = get_db_connection()
    c = conn.cursor()
    # Full sync: replace previously-stored Google reviews with the current
    # live set from Google (fetched via SerpApi).
    c.execute("DELETE FROM Reviews WHERE source='google'")
    inserted = 0
    for row in fetched_reviews:
        sentiment = calculate_sentiment(row["review_text"])
        c.execute(
            """INSERT INTO Reviews (source, reviewer_name, review_text, rating, sentiment_score, review_date)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("google", row["reviewer_name"], row["review_text"], row["rating"], sentiment, row["review_date"]),
        )
        inserted += 1
    conn.commit()
    conn.close()

    print(f"Synced {inserted} real Google reviews (rating={average_rating}, total_reviews={total_reviews}).")
    return inserted


if __name__ == "__main__":
    fetch_reviews()
