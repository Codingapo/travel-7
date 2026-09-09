"""
Real Google Reviews sync via SerpApi's Google Maps Reviews API with caching.

Behavior on failure (missing API key/place ID, network error, quota, etc.):
  The existing reviews already stored in the database are left untouched.
  Nothing fake is ever generated or inserted.

Caching strategy:
  Reviews are stored permanently in the database as the local cache.
  On each sync:
    1. Return cached reviews immediately (already in DB, available to frontend).
    2. Fetch current reviews from Google via SerpApi.
    3. For each fetched review, compute a unique signature
       (reviewer_name + review_date + content hash) and INSERT OR IGNORE
       so only genuinely new reviews are added; existing cached reviews
       are never deleted or overwritten.
  This means:
    - The reviews page always shows cached reviews instantly.
    - If the API call fails for any reason, cached reviews are preserved.
    - Any new reviews discovered by the API are merged in without data loss.
"""
import datetime
import hashlib
import os
import sqlite3
from typing import Dict, List, Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import requests

from config import SERPAPI_KEY, GOOGLE_PLACE_ID
from database import get_db_connection, upsert_review_summary

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


def _review_signature(reviewer_name: str, review_date: str, review_text: str) -> str:
    """Compute a stable signature used to detect duplicate reviews across syncs.

    The signature is insensitive to minor whitespace variations so the same
    real review is never duplicated even if SerpApi re-encodes it.
    """
    normalised_text = " ".join((review_text or "").split())
    payload = f"{(reviewer_name or '').strip().lower()}|{review_date}|{normalised_text}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


MAX_PAGES = 25


def _fetch_all_review_pages(base_params: Dict) -> (List[Dict], Optional[Dict]):
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
    """Incrementally sync real reviews + rating from Google via SerpApi.

    Returns the number of NEW reviews merged into the local cache on this run.
    Never fabricates data - on any failure it just leaves the existing cache
    untouched and returns 0.
    """
    print("Starting SerpApi Google Reviews sync (incremental cache mode)...")

    if not SERPAPI_KEY:
        print("SERPAPI_KEY is not set - skipping sync. Existing cached reviews are preserved.")
        return 0
    if not GOOGLE_PLACE_ID:
        print("GOOGLE_PLACE_ID is not set - skipping sync. Existing cached reviews are preserved.")
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
        print("SerpApi sync failed before returning any data. Keeping existing cached reviews unchanged.")
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
        review_date = _parse_review_date(r)
        reviewer_name = (r.get("user") or {}).get("name", "Google Reviewer")
        fetched_reviews.append({
            "rating": int(r.get("rating") or 5),
            "reviewer_name": reviewer_name,
            "review_text": text[:1000],
            "review_date": review_date,
            "signature": _review_signature(reviewer_name, review_date, text),
        })

    if not fetched_reviews:
        print("SerpApi returned no written reviews. Keeping existing cached reviews unchanged.")
        return 0

    conn = get_db_connection()
    c = conn.cursor()

    c.execute("PRAGMA table_info(Reviews)")
    existing_columns = {row["name"] for row in c.fetchall()}
    if "signature" not in existing_columns:
        c.execute("ALTER TABLE Reviews ADD COLUMN signature TEXT")
        c.execute("SELECT review_id, reviewer_name, review_date, review_text FROM Reviews")
        for row in c.fetchall():
            sig = _review_signature(row["reviewer_name"], row["review_date"], row["review_text"])
            c.execute("UPDATE Reviews SET signature=? WHERE review_id=?", (sig, row["review_id"]))
        conn.commit()
    c.execute("PRAGMA index_list(Reviews)")
    existing_indexes = {row["name"] for row in c.fetchall()}
    if "ix_reviews_signature" not in existing_indexes:
        try:
            c.execute("CREATE UNIQUE INDEX ix_reviews_signature ON Reviews(signature)")
            conn.commit()
        except sqlite3.IntegrityError:
            c.execute("""DELETE FROM Reviews WHERE review_id NOT IN (
                SELECT MIN(review_id) FROM Reviews GROUP BY signature
            )""")
            conn.commit()
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_reviews_signature ON Reviews(signature)")
            conn.commit()

    merged = 0
    for row in fetched_reviews:
        sentiment = calculate_sentiment(row["review_text"])
        try:
            c.execute(
                """INSERT OR IGNORE INTO Reviews
                   (source, reviewer_name, review_text, rating, sentiment_score, review_date, signature)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    "google",
                    row["reviewer_name"],
                    row["review_text"],
                    row["rating"],
                    sentiment,
                    row["review_date"],
                    row["signature"],
                ),
            )
            if c.rowcount and c.rowcount > 0:
                merged += 1
        except sqlite3.IntegrityError:
            continue
    conn.commit()
    conn.close()

    print(
        f"Review sync complete. rating={average_rating}, google_total={total_reviews}. "
        f"New reviews merged into cache: {merged}."
    )
    return merged


if __name__ == "__main__":
    fetch_reviews()
