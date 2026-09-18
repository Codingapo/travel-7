try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # python-dotenv isn't installed - environment variables (e.g. for Google
    # Reviews) can still be set directly in the shell/OS instead of via a
    # .env file. See SETUP_GOOGLE_REVIEWS.md / COMPLETE_SETUP_GUIDE.md.
    pass

import resend
import random
import os
import datetime
import traceback
import smtplib
import logging
import secrets
import re
import sqlite3
import uuid
import csv
import io
from email.message import EmailMessage
from contextlib import asynccontextmanager
from typing import Optional
from email.utils import parseaddr

from fastapi import FastAPI, Request, HTTPException, Query, UploadFile, File
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.gzip import GZipMiddleware
from pydantic import BaseModel
from werkzeug.security import generate_password_hash, check_password_hash
from apscheduler.schedulers.background import BackgroundScheduler

from config import (
    SESSION_TIMEOUT_MINUTES, MAX_LOGIN_ATTEMPTS, LOGIN_LOCKOUT_MINUTES,
    RESEND_API_KEY, DEFAULT_ADMIN_PASSWORD, DEFAULT_ADMIN_EMAIL,
    DEFAULT_ADMIN_USERNAME, OUTBOX_DIR,
    SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD,
    SMTP_USE_TLS, SMTP_FROM_EMAIL,
)
from database import get_db_connection, init_db, backup_database, get_review_summary, upsert_review_summary
from ai_engine import train_demand_forecasting, perform_customer_segmentation, run_anomaly_detection, get_forecast_model_metadata, generate_recommendations
from google_reviews_sync import fetch_reviews, calculate_sentiment


# ============================================================
# PASSWORD STRENGTH VALIDATION
# ============================================================

PASSWORD_MIN_LENGTH = 12
PASSWORD_REQUIREMENTS_DESC = (
    f"Password must be at least {PASSWORD_MIN_LENGTH} characters and include "
    "an uppercase letter, a lowercase letter, a number, and a special character."
)

_SPECIAL_CHARS = set(r"!@#$%^&*()_+-=[]{}|;:,.<>?/~`")


def generate_temporary_password(length: int = 16) -> str:
    """Generate a strong temporary password that always meets policy.

    Guarantees: uppercase, lowercase, digit, and special character.
    Uses the secrets module (cryptographically secure).
    """
    if length < PASSWORD_MIN_LENGTH:
        length = PASSWORD_MIN_LENGTH

    upper = "ABCDEFGHJKLMNPQRSTUVWXYZ"      # exclude I/O for readability
    lower = "abcdefghijkmnopqrstuvwxyz"    # exclude l
    digits = "23456789"                    # exclude 0/1
    special = "!@#$%^&*-_=+"

    # Ensure at least one of each required class
    chars = [
        secrets.choice(upper),
        secrets.choice(lower),
        secrets.choice(digits),
        secrets.choice(special),
    ]
    pool = upper + lower + digits + special
    chars.extend(secrets.choice(pool) for _ in range(length - 4))
    # Shuffle so the required characters are not always at the front
    for i in range(len(chars) - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        chars[i], chars[j] = chars[j], chars[i]
    return "".join(chars)


def validate_password_strength(password: str, forbidden_substrings: Optional[list] = None) -> None:
    """Validate a password against standard security requirements.

    Raises HTTPException(400) with a specific message if validation fails.

    Requirements:
      - Minimum PASSWORD_MIN_LENGTH characters
      - At least one uppercase letter (A-Z)
      - At least one lowercase letter (a-z)
      - At least one digit (0-9)
      - At least one special character (from a defined set)
      - Must not contain any of the forbidden substrings (e.g. username, email
        local-part) – case-insensitive check.
    """
    if not password:
        raise HTTPException(status_code=400, detail="Password is required.")

    errors = []

    if len(password) < PASSWORD_MIN_LENGTH:
        errors.append(f"be at least {PASSWORD_MIN_LENGTH} characters long")
    if not re.search(r"[A-Z]", password):
        errors.append("include at least one uppercase letter (A-Z)")
    if not re.search(r"[a-z]", password):
        errors.append("include at least one lowercase letter (a-z)")
    if not re.search(r"[0-9]", password):
        errors.append("include at least one number (0-9)")
    if not any(ch in _SPECIAL_CHARS for ch in password):
        errors.append(
            "include at least one special character (e.g. !@#$%^&*)"
        )

    if forbidden_substrings:
        pwd_lower = password.lower()
        for token in forbidden_substrings:
            if token and isinstance(token, str):
                token_lower = token.strip().lower()
                if token_lower and len(token_lower) >= 3 and token_lower in pwd_lower:
                    errors.append("not contain your username or email address")
                    break

    if errors:
        if len(errors) == 1:
            detail = f"Password must {errors[0]}."
        else:
            detail = "Password must " + ", ".join(errors[:-1]) + f", and {errors[-1]}."
        raise HTTPException(status_code=400, detail=detail)


# --- Pydantic Models ---
class LoginRequest(BaseModel):
    username: str
    password: str

class CustomerLoginRequest(BaseModel):
    email: str
    password: str

class CustomerRegisterRequest(BaseModel):
    email: str
    password: str

class BookingRequest(BaseModel):
    package_id: int
    name: str
    email: str
    phone: str
    travel_date: str
    number_of_travelers: int
    payment_method: str = "unknown"
    address: Optional[str] = ""
    card_number: Optional[str] = ""
    card_expiry: Optional[str] = ""
    bank_name: Optional[str] = ""
    account_number: Optional[str] = ""

class ReportRequest(BaseModel):
    start_date: str
    end_date: str

class AdminForgotPasswordRequest(BaseModel):
    email: str

class AdminCreateRequest(BaseModel):
    username: str
    email: str
    full_name: str = ""
    confirm_password: str = ""         # current admin's password (for confirmation)

class AdminStatusRequest(BaseModel):
    account_status: str
    confirm_password: str = ""

class ReviewRequest(BaseModel):
    reviewer_name: str
    review_text: str
    rating: int
    review_date: Optional[str] = None

class ReviewSummaryRequest(BaseModel):
    average_rating: float
    total_reviews: int

class ProfileUpdateRequest(BaseModel):
    full_name: str
    phone: str
    address: Optional[str] = ""
    payment_method: Optional[str] = ""
    card_number: Optional[str] = ""
    card_expiry: Optional[str] = ""
    bank_name: Optional[str] = ""
    account_number: Optional[str] = ""

# --- Scheduler ---
scheduler = BackgroundScheduler()

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()

    # Send bootstrap administrator credentials once. The password is never stored
    # in the source code; it comes from DEFAULT_ADMIN_PASSWORD.
    try:
        conn = get_db_connection(); c = conn.cursor()
        c.execute("SELECT user_id, username, email, must_change_password FROM Users WHERE is_admin=1 AND LOWER(email)=? LIMIT 1", (DEFAULT_ADMIN_EMAIL.lower(),))
        bootstrap = c.fetchone()
        c.execute("CREATE TABLE IF NOT EXISTS System_Settings (setting_key TEXT PRIMARY KEY, setting_value TEXT)")
        c.execute("SELECT setting_value FROM System_Settings WHERE setting_key='bootstrap_admin_email_sent'")
        already_sent = c.fetchone()
        if bootstrap and bootstrap["must_change_password"] and not already_sent:
            try:
                sent = send_bootstrap_admin_email(bootstrap["email"], bootstrap["username"], DEFAULT_ADMIN_PASSWORD)
                if sent:
                    c.execute("INSERT OR REPLACE INTO System_Settings(setting_key,setting_value) VALUES('bootstrap_admin_email_sent','1')")
                    conn.commit()
            except Exception:
                logger.exception("Failed to send bootstrap administrator credentials")
        conn.close()
    except Exception:
        logger.exception("Bootstrap administrator email setup failed")
    
    # Schedule DB Backups (Daily at 2:00 AM)
    scheduler.add_job(backup_database, 'cron', hour=2, minute=0)
    
    # Schedule AI Models (Daily at 2:00 AM)
    scheduler.add_job(train_demand_forecasting, 'cron', hour=2, minute=0)
    scheduler.add_job(perform_customer_segmentation, 'cron', hour=2, minute=0)
    scheduler.add_job(generate_recommendations, 'cron', hour=2, minute=10)
    
    # Schedule Anomaly Detection (Daily at 8:00 AM, 12:00 PM, 4:00 PM)
    scheduler.add_job(run_anomaly_detection, 'cron', hour=8, minute=0)
    scheduler.add_job(run_anomaly_detection, 'cron', hour=12, minute=0)
    scheduler.add_job(run_anomaly_detection, 'cron', hour=16, minute=0)
    
    # Schedule Reviews Scraper (Once daily).
    # A full sync now pages through SerpApi until every review is fetched
    # (previously it only ever grabbed the first ~8), which costs roughly
    # 1 SerpApi search credit per ~8 reviews. For a business with 100+
    # reviews that's already a meaningful chunk of SerpApi's free monthly
    # quota (100-250 searches/month), so this runs once a day instead of
    # every 4 hours to avoid burning through it. Admins can still get an
    # on-demand full re-sync any time via the "Refresh" button on the
    # admin Reviews page.
    scheduler.add_job(fetch_reviews, 'cron', hour=3, minute=0)
    
    scheduler.start()
    
    yield
    
    scheduler.shutdown()

# --- App Init ---
resend.api_key = RESEND_API_KEY or os.getenv("RESEND_API_KEY", "")
app = FastAPI(title="TravelIntel AI", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1000)


@app.middleware("http")
async def no_cache_admin_and_api(request: Request, call_next):
    """Prevent browser/intermediary caching of all admin pages and JSON API
    responses so new bookings and Google reviews are always visible instantly.

    Static files under /static are intentionally exempt — those use far-future
    expiry via content-hashed filenames elsewhere.
    """
    path = request.url.path or ""
    is_admin_html = (
        path == "/dashboard"
        or path.startswith("/admin/")
    )
    is_api = path.startswith("/api/")

    response = await call_next(request)

    if is_admin_html or is_api:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        response.headers["X-Content-Type-Options"] = "nosniff"

    return response


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOADS_DIR = os.path.join(BASE_DIR, "static", "uploads", "packages")
os.makedirs(UPLOADS_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

MAX_IMAGE_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB
_ALLOWED_IMAGE_MIME_TO_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}

# --- Logging ---
LOGS_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOGS_DIR, exist_ok=True)
logger = logging.getLogger("travelintel")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    fh = logging.FileHandler(os.path.join(LOGS_DIR, "system_errors.log"), encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(fh)


# ============================================================
# EMAIL DELIVERY SYSTEM — Unified Fallback Chain
# Strategy:  1) Resend API    2) SMTP (if configured)    3) Local outbox file
# Every email function should call _dispatch_email() below.
# ============================================================

def _save_email_to_outbox(to_email: str, subject: str, html_body: str,
                          text_body: Optional[str] = None,
                          prefix: str = "email") -> str:
    """Save an email to instance/outbox/ as a last-resort delivery mechanism.

    Returns the path of the saved file so callers can log it.
    """
    os.makedirs(OUTBOX_DIR, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_to = re.sub(r"[^a-zA-Z0-9._-]+", "_", to_email)
    filename = f"{prefix}_{ts}_{uuid.uuid4().hex[:6]}_{safe_to}.txt"
    path = os.path.join(OUTBOX_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"TO: {to_email}\n")
        f.write(f"SUBJECT: {subject}\n")
        f.write(f"SAVED-AT: {datetime.datetime.now().isoformat()}\n")
        f.write("-" * 50 + "\n\n")
        if text_body:
            f.write(text_body)
        else:
            f.write(html_body)
    logger.info("Email saved to local outbox (no online delivery available). path=%s", path)
    return path


def _send_email_via_smtp(to_email: str, subject: str, html_body: str,
                         text_body: Optional[str] = None,
                         from_email: Optional[str] = None,
                         reply_to: Optional[str] = None) -> bool:
    """Send email via SMTP. Uses SMTP_* env vars. Returns True on success."""
    if not SMTP_HOST or not SMTP_USERNAME or not SMTP_PASSWORD:
        logger.debug("SMTP not configured; skipping SMTP fallback.")
        return False
    sender = from_email or SMTP_FROM_EMAIL or SMTP_USERNAME
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = to_email
        if reply_to:
            msg["Reply-To"] = reply_to
        if text_body:
            msg.set_content(text_body)
            msg.add_alternative(html_body, subtype="html")
        else:
            msg.set_content(html_body, subtype="html")
        if SMTP_USE_TLS:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
                server.starttls()
                server.login(SMTP_USERNAME, SMTP_PASSWORD)
                server.send_message(msg)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
                server.login(SMTP_USERNAME, SMTP_PASSWORD)
                server.send_message(msg)
        logger.info("Email sent successfully via SMTP. to=%s subject=%s", to_email, subject)
        return True
    except Exception as e:
        logger.warning("SMTP send failed. to=%s error=%s", to_email, str(e))
        return False


def _dispatch_email(to_email: str, subject: str, html_body: str,
                    text_body: Optional[str] = None,
                    from_email: str = "TravelIntel AI <noreply@travelintel.ai>",
                    reply_to: Optional[str] = None,
                    outbox_prefix: str = "email",
                    require_online: bool = False) -> bool:
    """Unified email dispatcher.

    Tries delivery in this order:
      1. Resend API (if api_key configured and valid)
      2. SMTP (if SMTP_* variables configured)
      3. Local instance/outbox/ file (always succeeds unless disk full)

    If require_online=True, function returns False instead of falling back to
    outbox-only storage (used by callers that need real delivery guarantees).
    """
    # --- 1) Resend API ---
    if resend.api_key:
        try:
            payload = {
                "from": from_email,
                "to": [to_email],
                "subject": subject,
                "html": html_body,
            }
            if reply_to:
                payload["reply_to"] = reply_to
            if text_body:
                payload["text"] = text_body
            response = resend.Emails.send(payload)
            logger.info("Email sent successfully via Resend. to=%s subject=%s resend_id=%s",
                        to_email, subject, str(response)[:120])
            return True
        except Exception as e:
            logger.warning("Resend API send failed. to=%s subject=%s error=%s",
                           to_email, subject, str(e))

    # --- 2) SMTP Fallback ---
    sent_smtp = _send_email_via_smtp(
        to_email=to_email,
        subject=subject,
        html_body=html_body,
        text_body=text_body,
        from_email=(parseaddr(from_email)[1] if from_email else None),
        reply_to=reply_to,
    )
    if sent_smtp:
        return True

    # --- 3) Local outbox save ---
    if require_online:
        logger.error("Online email delivery failed AND require_online=True. "
                     "to=%s subject=%s", to_email, subject)
        return False
    try:
        _save_email_to_outbox(
            to_email=to_email,
            subject=subject,
            html_body=html_body,
            text_body=text_body,
            prefix=outbox_prefix,
        )
        return True
    except Exception as e:
        logger.exception("FATAL: Could not save email to outbox either. to=%s error=%s",
                         to_email, str(e))
        return False


# --- Session & Security State ---
# persistent session and lockout tracking
login_attempts = {}
password_reset_otps = {}
registration_otps = {}
# Throttle for the background SerpApi review sync triggered on page load.
_last_review_sync_at = None

def create_session(user_id: int, role: str):
    session_id = os.urandom(24).hex()
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "INSERT INTO Sessions (session_id, user_id, role, last_activity) VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
        (session_id, user_id, role)
    )
    conn.commit()
    conn.close()
    return session_id

def get_session_from_request(request: Request) -> Optional[dict]:
    """Get session without raising — returns None if not authenticated."""
    session_id = request.cookies.get("session_id")
    if not session_id:
        authorization = request.headers.get("Authorization", "")
        if authorization.lower().startswith("bearer "):
            session_id = authorization[7:].strip()
    if not session_id:
        return None
    
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT user_id, role, last_activity FROM Sessions WHERE session_id = ?", (session_id,))
    row = c.fetchone()
    
    if not row:
        conn.close()
        return None
        
    # Check timeout
    try:
        # SQLite CURRENT_TIMESTAMP is UTC
        last_activity = datetime.datetime.fromisoformat(row["last_activity"].replace(' ', 'T'))
    except:
        last_activity = datetime.datetime.utcnow()
        
    now = datetime.datetime.utcnow()
    if (now - last_activity).total_seconds() > SESSION_TIMEOUT_MINUTES * 60:
        c.execute("DELETE FROM Sessions WHERE session_id = ?", (session_id,))
        conn.commit()
        conn.close()
        return None
    
    # Update last activity
    c.execute("UPDATE Sessions SET last_activity = CURRENT_TIMESTAMP WHERE session_id = ?", (session_id,))
    conn.commit()
    conn.close()
    
    return {
        "user_id": row["user_id"],
        "role": row["role"],
        "last_activity": last_activity
    }

def verify_session(request: Request):
    session = get_session_from_request(request)
    if not session:
        raise HTTPException(status_code=401, detail="Please login first")
    return session

def require_admin(request: Request):
    session = get_session_from_request(request)
    if not session:
        raise HTTPException(status_code=401, detail="Please login first")
    admin = get_active_admin(session["user_id"])
    if not admin:
        raise HTTPException(status_code=403, detail="Administrator account is inactive or no longer authorised")
    return {**session, "admin": admin}


def get_logged_in_redirect(session: dict) -> Optional[str]:
    """If the session belongs to an active user, return where they should go.

    - must_change_password → /auth/change-password
    - active admin         → /dashboard
    - active customer      → /home
    - inactive / missing   → None (treat as not logged in)
    """
    if not session or not session.get("user_id"):
        return None
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT is_admin, role, user_type, must_change_password, account_status "
        "FROM Users WHERE user_id=? LIMIT 1",
        (session["user_id"],),
    )
    user = c.fetchone()
    conn.close()
    if not user or user["account_status"] != "active":
        return None
    if bool(user["must_change_password"] if "must_change_password" in user.keys() else 0):
        return "/auth/change-password"
    is_admin = (
        int(user["is_admin"] or 0) == 1
        or user["role"] == "admin"
        or user["user_type"] == "admin"
    )
    if is_admin and get_active_admin(session["user_id"]):
        return "/dashboard"
    return "/home"


def normalize_email(raw: str) -> str:
    return (raw or "").strip().lower()


def is_valid_email(raw: str) -> bool:
    parsed = parseaddr(raw)[1]
    return "@" in parsed and "." in parsed.split("@")[-1]

CARD_BRAND_RULES = [
    ("Visa", re.compile(r"^4[0-9]{12}(?:[0-9]{3})?$"), (13, 16)),
    ("Mastercard", re.compile(r"^(?:5[1-5][0-9]{2}|222[1-9]|22[3-9][0-9]|2[3-6][0-9]{2}|27[01][0-9]|2720)[0-9]{12}$"), (16, 16)),
    ("American Express", re.compile(r"^3[47][0-9]{13}$"), (15, 15)),
    ("Discover", re.compile(r"^6(?:011|5[0-9]{2})[0-9]{12,}$"), (16, 19)),
    ("Diners Club", re.compile(r"^3(?:0[0-5]|[68][0-9])[0-9]{11}$"), (14, 14)),
    ("JCB", re.compile(r"^(?:2131|1800|35\d{3})\d{11}$"), (15, 16)),
    ("Maestro", re.compile(r"^(?:5[0678]\d\d|6304|6390|67\d\d)\d{8,15}$"), (12, 19)),
]

def detect_card_brand(card_number: str) -> Optional[str]:
    digits = re.sub(r"\D", "", card_number or "")
    if not digits:
        return None
    for brand, pattern, _ in CARD_BRAND_RULES:
        if pattern.match(digits):
            return brand
    if 12 <= len(digits) <= 19:
        return "Generic"
    return None

def is_valid_card_number(card_number: str) -> bool:
    """Validate a card number using the Luhn checksum + brand-specific length checks."""
    digits = re.sub(r"\D", "", card_number or "")
    if not 12 <= len(digits) <= 19:
        return False
    if digits in {
        "0000000000000", "1234567890123", "0123456789012",
        "0000000000000000", "1111111111111111", "2222222222222222",
        "3333333333333333", "4444444444444444", "5555555555555555",
        "6666666666666666", "7777777777777777", "8888888888888888",
        "9999999999999999", "1234567890123456", "1234123412341234",
    }:
        return False
    total = 0
    parity = len(digits) % 2
    for i, char in enumerate(digits):
        digit = int(char)
        if i % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    if total % 10 != 0:
        return False
    brand = detect_card_brand(card_number)
    if brand and brand != "Generic":
        for b, pattern, (min_len, max_len) in CARD_BRAND_RULES:
            if b == brand:
                if not (min_len <= len(digits) <= max_len):
                    return False
                break
    return True

def is_valid_card_cvv(cvv: str, card_number: str = "") -> bool:
    digits = re.sub(r"\D", "", cvv or "")
    if not digits or len(digits) < 3 or len(digits) > 4:
        return False
    if len(set(digits)) == 1:
        return False
    if card_number:
        brand = detect_card_brand(card_number)
        if brand == "American Express":
            return len(digits) == 4
        elif brand in {"Visa", "Mastercard", "Discover"}:
            return len(digits) == 3
    return True

def is_valid_card_expiry(value: str) -> bool:
    value = (value or "").strip()
    match = re.fullmatch(r"(0[1-9]|1[0-2])/(\d{2})", value)
    if not match:
        return False
    month, year = int(match.group(1)), 2000 + int(match.group(2))
    today = datetime.date.today()
    return (year, month) >= (today.year, today.month)

SA_BANK_NAMES = {
    "absa", "abs bank", "abs group",
    "standard bank", "standard bank group",
    "fnb", "first national bank",
    "nedbank",
    "capitec", "capitec bank",
    "tyme bank", "tymebank", "tyme",
    "discovery bank", "discovery",
    "investec", "investec bank",
    "bidvest bank", "bidvest",
    "sasfin", "sasfin bank",
    "albaraka bank", "albaraka",
    "bank of china", "boc",
    "bank zerox", "zerox",
    "grindrod bank", "grindrod",
    "hbz bank", "hbz",
    "mercantile bank", "mercantile",
    "old mutual bank", "old mutual",
    "rbc bank", "rbc royal bank",
    "state bank", "sarb",
    "vbs mutual bank", "vbs",
    "barclays",
    "uob bank", "uob",
    "dbs bank", "dbs",
    "hsbc", "hsbc bank",
    "citi bank", "citibank", "citi",
    "wells fargo",
    "chase", "jpmorgan", "jp morgan",
    "bank of america", "boa",
    "goldman sachs",
    "deutsche bank",
    "barclays bank",
    "standard chartered", "stanchart",
    "scotiabank", "bank of nova scotia",
    "bmo", "bank of montreal",
    "national australia bank", "nab",
    "anz", "anz bank",
    "commonwealth bank", "commbank",
    "westpac",
    "sberbank",
    "banco do brasil",
    "itau",
    "bradesco",
    "caixa",
    "societe generale", "sg",
    "bnp paribas", "bnp",
    "credit agricole",
    "lcl", "le credit lyonnais",
    "santander",
    "bbva",
    "caixa bank",
    "ing", "ing bank",
    "rabobank",
    "abn amro",
    "nordea",
    "danske bank",
    "swedbank",
    "seb bank",
    "handelsbanken",
    "dnb bank",
    "commerzbank",
    "kfw",
    "unicredit",
    "intesa sanpaolo",
    "banco santander",
    "icbc",
    "boc", "china construction bank",
    "agricultural bank of china",
    "mufg", "mizuho",
    "smbc",
    "kb", "kookmin bank",
    "shinhan bank",
    "hdfc", "hdfc bank",
    "icici", "icici bank",
    "sbi", "state bank of india",
    "axis bank", "axis",
    "kotak mahindra", "kotak",
    "yes bank",
    "rbc",
    "td bank", "toronto dominion",
}

SA_BANK_WHITELIST = frozenset({
    "Absa Bank",
    "Standard Bank",
    "FNB",
    "Nedbank",
    "Capitec Bank",
    "TymeBank",
    "Discovery Bank",
    "Investec Bank",
    "Bidvest Bank",
    "Sasfin Bank",
    "Albaraka Bank",
    "African Bank",
    "Old Mutual Bank",
    "Grindrod Bank",
    "Mercantile Bank",
})

SA_BANK_ACCOUNT_RULES = {
    "Absa Bank":       (7, 10),
    "Standard Bank":   (8, 11),
    "FNB":             (8, 10),
    "Nedbank":         (8, 11),
    "Capitec Bank":    (12, 16),
    "TymeBank":        (11, 13),
    "Discovery Bank":  (9, 11),
    "Investec Bank":   (8, 10),
    "Bidvest Bank":    (8, 11),
    "Sasfin Bank":     (8, 10),
    "Albaraka Bank":   (8, 10),
    "African Bank":    (9, 11),
    "Old Mutual Bank": (8, 10),
    "Grindrod Bank":   (8, 10),
    "Mercantile Bank": (8, 10),
}

def normalize_bank_name(raw: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (raw or "").lower()).strip()

def is_valid_bank_name(bank_name: str) -> bool:
    """Strict bank name validation — must exactly match the SA dropdown whitelist."""
    if not bank_name:
        return False
    bn = (bank_name or "").strip()
    return bn in SA_BANK_WHITELIST


def is_valid_account_number(account_number: str, bank_name: str = "") -> bool:
    if not account_number:
        return False
    raw = (account_number or "").strip()
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return False
    if len(digits) < 6 or len(digits) > 18:
        return False
    if len(set(digits)) == 1:
        return False
    if all(digits[i] >= digits[i - 1] for i in range(1, len(digits))) and len(digits) >= 5:
        return False
    if all(digits[i] <= digits[i - 1] for i in range(1, len(digits))) and len(digits) >= 5:
        return False

    bank_key = (bank_name or "").strip()
    if bank_key and bank_key in SA_BANK_ACCOUNT_RULES:
        min_len, max_len = SA_BANK_ACCOUNT_RULES[bank_key]
        if not (min_len <= len(digits) <= max_len):
            return False

    return True

def validate_payment_details(
    payment_method: str,
    card_number: str = "",
    card_expiry: str = "",
    cvv: str = "",
    bank_name: str = "",
    account_number: str = "",
):
    if not payment_method or payment_method == "unknown":
        return True
    if payment_method == "credit_card":
        if not card_number:
            raise HTTPException(status_code=400, detail="Please enter your card number.")
        if not is_valid_card_number(card_number):
            raise HTTPException(status_code=400, detail="Please enter a valid credit card number.")
        if not card_expiry:
            raise HTTPException(status_code=400, detail="Please enter your card expiry date.")
        if not is_valid_card_expiry(card_expiry):
            raise HTTPException(status_code=400, detail="Please enter a valid, non-expired card expiry date (MM/YY).")
        if cvv is not None and cvv != "":
            if not is_valid_card_cvv(cvv, card_number):
                raise HTTPException(status_code=400, detail="Please enter a valid CVV security code (3-4 digits on the back of your card).")
    elif payment_method == "eft":
        if not bank_name:
            raise HTTPException(status_code=400, detail="Please select your bank from the dropdown list.")
        if not is_valid_bank_name(bank_name):
            raise HTTPException(
                status_code=400,
                detail="Please select a valid South African bank from the dropdown list."
            )
        if not account_number:
            raise HTTPException(status_code=400, detail="Please enter your bank account number.")
        digits_only = re.sub(r"\D", "", account_number or "")
        bank_stripped = (bank_name or "").strip()
        if not is_valid_account_number(account_number, bank_name):
            detail = "Please enter a valid bank account number (digits only, no test numbers like 12345 or all the same digit)."
            if bank_stripped in SA_BANK_ACCOUNT_RULES:
                min_len, max_len = SA_BANK_ACCOUNT_RULES[bank_stripped]
                dlen = len(digits_only)
                if dlen and (dlen < min_len or dlen > max_len):
                    detail = (
                        f"Invalid account number length for {bank_stripped}: "
                        f"you entered {dlen} digit{'s' if dlen != 1 else ''}, but "
                        f"{bank_stripped} requires {min_len}-{max_len} digits."
                    )
            raise HTTPException(status_code=400, detail=detail)
    return True


def get_active_admin(user_id: int):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT user_id, username, email, role, full_name, account_status, is_admin "
        "FROM Users WHERE user_id=?",
        (user_id,),
    )
    row = c.fetchone()
    conn.close()
    if not row or int(row["is_admin"] or 0) != 1 or row["account_status"] != "active":
        return None
    return dict(row)


def send_bootstrap_admin_email(email: str, username: str, temporary_password: str) -> bool:
    """Send the one-time bootstrap administrator credentials.

    Uses unified fallback chain: Resend → SMTP → local outbox.
    Returns True if the email was dispatched (either online or to outbox).
    """
    html = f"""<h2>TravelIntel AI Administrator</h2>
    <p>Your administrator account has been created.</p>
    <p><b>Email:</b> {email}<br><b>Username:</b> {username}<br><b>Temporary password:</b> {temporary_password}</p>
    <p>Sign in at <b>/auth/login</b>. You will be required to change this temporary password immediately.</p>
    <p>If you did not expect this account, contact the system owner immediately.</p>"""

    text = (
        f"TravelIntel AI Administrator\n\n"
        f"Your administrator account has been created.\n\n"
        f"Email: {email}\n"
        f"Username: {username}\n"
        f"Temporary password: {temporary_password}\n\n"
        f"Sign in at /auth/login. You will be required to change this temporary password immediately."
    )

    return _dispatch_email(
        to_email=email,
        subject="Your TravelIntel AI administrator account",
        html_body=html,
        text_body=text,
        from_email="TravelIntel AI <reset@notify.moviewatchtv.fun>",
        reply_to="support@travelintel.ai",
        outbox_prefix="admin_bootstrap",
    )


def send_new_admin_shared_password_email(email: str, username: str) -> bool:
    """Notify a newly-added administrator that they share the universal admin password."""
    html = f"""<h2>TravelIntel AI Administrator Access</h2>
    <p>Your administrator account has been created and is linked to the shared platform password.</p>
    <p><b>Email:</b> {email}<br><b>Username:</b> {username}</p>
    <p>Use the current universal TravelIntel administrator password to sign in at <b>/auth/login</b>.
    If you do not already know it, contact any active administrator or the system owner.</p>
    <p>All administrators share one password. When any administrator changes the password, every account is updated automatically.</p>
    <p>If you did not expect this account, contact the system owner immediately.</p>"""

    text = (
        f"TravelIntel AI Administrator Access\n\n"
        f"Your administrator account has been created and is linked to the shared platform password.\n\n"
        f"Email: {email}\nUsername: {username}\n\n"
        f"Use the current universal TravelIntel administrator password to sign in at /auth/login.\n"
        f"All administrators share one password. When any administrator changes the password, every account is updated automatically."
    )

    return _dispatch_email(
        to_email=email,
        subject="Your TravelIntel AI administrator account is ready",
        html_body=html,
        text_body=text,
        from_email="TravelIntel AI <reset@notify.moviewatchtv.fun>",
        reply_to="support@travelintel.ai",
        outbox_prefix="admin_added",
    )


def send_verification_email(email, otp) -> bool:
    """Send a registration OTP verification email.

    Uses unified fallback chain. The email is ALWAYS saved to outbox if
    online delivery fails, so the OTP is never lost to the system.
    Returns True on success (either online or outbox saved).
    """
    html = f"""
    <!DOCTYPE html>
    <html>
    <body style="font-family: Arial, sans-serif; background:#f4f7fb; padding:30px;">
        <div style="max-width:560px; margin:auto; background:white; padding:30px; border-radius:12px;">
            <h2 style="color:#2563eb;">TravelIntel AI</h2>
            <p>Welcome to TravelIntel AI! Please verify your email address to activate your account.</p>
            <p>Your verification code is:</p>
            <div style="font-size:32px;font-weight:bold;letter-spacing:8px;text-align:center;padding:20px;background:#f1f5f9;border-radius:10px;margin:20px 0;">
                {otp}
            </div>
            <p>This code expires in 10 minutes.</p>
            <p>If you did not create a TravelIntel AI account, you can safely ignore this email.</p>
        </div>
    </body>
    </html>
    """

    text = (
        f"TravelIntel AI — Email Verification\n\n"
        f"Welcome to TravelIntel AI! Please verify your email address to activate your account.\n\n"
        f"Your verification code is:  {otp}\n\n"
        f"This code expires in 10 minutes.\n\n"
        f"If you did not create a TravelIntel AI account, you can safely ignore this email."
    )

    return _dispatch_email(
        to_email=email,
        subject="Verify your TravelIntel AI account",
        html_body=html,
        text_body=text,
        from_email="TravelIntel AI <verify@notify.moviewatchtv.fun>",
        reply_to="support@travelintel.ai",
        outbox_prefix="verify_otp",
    )


def send_password_reset_email(email: str, otp: str) -> bool:
    """Send a password-reset OTP code. Falls back to outbox on failure."""
    html = f"""
    <!DOCTYPE html>
    <html>
    <body style="font-family: Arial, sans-serif; background:#f4f7fb; padding:30px;">
        <div style="max-width:600px; margin:auto; background:white; padding:30px; border-radius:12px;">
            <h2 style="color:#2563eb;">TravelIntel AI</h2>

            <p>We received a request to reset your password.</p>

            <p>Your password reset verification code is:</p>

            <div style="
                font-size:32px;
                font-weight:bold;
                letter-spacing:8px;
                text-align:center;
                padding:20px;
                background:#f1f5f9;
                border-radius:10px;
                margin:20px 0;
            ">
                {otp}
            </div>

            <p>This code expires in 10 minutes.</p>

            <p>If you did not request a password reset, you can safely ignore this email.</p>

            <p>TravelIntel AI</p>
        </div>
    </body>
    </html>
    """

    text = (
        f"TravelIntel AI — Password Reset\n\n"
        f"We received a request to reset your password.\n\n"
        f"Your password reset verification code is:  {otp}\n\n"
        f"This code expires in 10 minutes.\n\n"
        f"If you did not request a password reset, you can safely ignore this email.\n\n"
        f"TravelIntel AI"
    )

    return _dispatch_email(
        to_email=email,
        subject="Your TravelIntel AI password reset code",
        html_body=html,
        text_body=text,
        from_email="TravelIntel AI <reset@notify.moviewatchtv.fun>",
        reply_to="support@travelintel.ai",
        outbox_prefix="reset_otp",
    )
    
def send_booking_email(to_email: str, booking: dict) -> bool:
    """Send a premium booking confirmation email.

    Uses unified fallback chain:
      1) Resend API    2) SMTP (if configured)    3) instance/outbox/

    Returns True if the email was dispatched through any channel.
    """

    booking_id = booking.get("booking_id", "unknown")
    reference = f"TI-{str(booking_id).zfill(5)}" if booking_id != "unknown" else "TI-?????"

    customer_name = booking.get("name", "Valued Traveller")
    customer_email = booking.get("email", to_email)
    phone = booking.get("phone", "Not provided")
    package_name = booking.get("package_name", "Travel Package")
    destination = booking.get("destination", "Destination")
    duration = booking.get("duration", "Not specified")
    travel_date = booking.get("travel_date", "Not specified")
    travelers = booking.get("number_of_travelers", 1)
    total_amount = booking.get("total_amount", 0)
    booking_date = booking.get("booking_date", "Not specified")
    payment_method = booking.get("payment_method", "Not specified")

    subject = f"✈️ Booking Confirmed — {destination} | {reference}"

    html = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Booking Confirmation</title>
    </head>
    <body style="margin:0;padding:0;background-color:#f1f5f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;color:#0f172a;">
    <table width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#f1f5f9;padding:40px 15px;">
      <tr><td align="center">
        <table width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:620px;background:#ffffff;border-radius:24px;overflow:hidden;box-shadow:0 20px 50px rgba(15,23,42,0.10);">
          <tr><td style="background:linear-gradient(135deg,#2563eb 0%,#1d4ed8 50%,#1e40af 100%);padding:42px 35px;text-align:center;color:#ffffff;">
            <div style="font-size:42px;margin-bottom:12px;">✈️</div>
            <h1 style="margin:0;font-size:28px;line-height:1.3;font-weight:800;color:#ffffff;">Your Trip Is Confirmed!</h1>
            <p style="margin:12px 0 0;font-size:15px;line-height:1.5;color:#dbeafe;">Thank you, {customer_name}. We can't wait to travel with you.</p>
          </td></tr>
          <tr><td style="padding:20px 35px;background:#eff6ff;border-bottom:1px solid #dbeafe;">
            <div style="display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:10px;">
              <div>
                <div style="font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;color:#64748b;margin-bottom:4px;">Booking Reference</div>
                <div style="font-family:'Courier New',Courier,monospace;font-size:22px;font-weight:800;color:#1d4ed8;letter-spacing:2px;">{reference}</div>
              </div>
              <div style="text-align:right;">
                <div style="font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;color:#64748b;margin-bottom:4px;">Booking Date</div>
                <div style="font-size:15px;color:#0f172a;font-weight:600;">{booking_date}</div>
              </div>
            </div>
          </td></tr>
          <tr><td style="padding:35px;">
            <h2 style="margin:0 0 20px;font-size:18px;font-weight:700;color:#0f172a;padding-bottom:10px;border-bottom:2px solid #e2e8f0;">Trip Summary</h2>
            <table width="100%" cellpadding="0" cellspacing="0" border="0" style="font-size:15px;">
              <tr><td style="padding:10px 0;color:#64748b;width:35%;">Package</td><td style="padding:10px 0;font-weight:600;color:#0f172a;">{package_name}</td></tr>
              <tr><td style="padding:10px 0;color:#64748b;">Destination</td><td style="padding:10px 0;font-weight:600;color:#0f172a;">📍 {destination}</td></tr>
              <tr><td style="padding:10px 0;color:#64748b;">Travel Date</td><td style="padding:10px 0;font-weight:600;color:#0f172a;">{travel_date}</td></tr>
              <tr><td style="padding:10px 0;color:#64748b;">Duration</td><td style="padding:10px 0;font-weight:600;color:#0f172a;">{duration}</td></tr>
              <tr><td style="padding:10px 0;color:#64748b;">Travellers</td><td style="padding:10px 0;font-weight:600;color:#0f172a;">👥 {travelers} traveller{'s' if travelers != 1 else ''}</td></tr>
            </table>
          </td></tr>
          <tr><td style="padding:25px 35px;background:#f8fafc;border-top:1px solid #e2e8f0;border-bottom:1px solid #e2e8f0;">
            <h2 style="margin:0 0 18px;font-size:18px;font-weight:700;color:#0f172a;">Payment Details</h2>
            <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px;">
              <div>
                <div style="font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;color:#64748b;margin-bottom:6px;">Payment Method</div>
                <div style="font-size:15px;font-weight:600;color:#0f172a;text-transform:capitalize;">{payment_method}</div>
              </div>
              <div style="text-align:right;">
                <div style="font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;color:#64748b;margin-bottom:6px;">Total Paid</div>
                <div style="font-size:26px;font-weight:800;color:#16a34a;">R {total_amount:,.2f}</div>
              </div>
            </div>
          </td></tr>
          <tr><td style="padding:35px;">
            <h2 style="margin:0 0 20px;font-size:18px;font-weight:700;color:#0f172a;padding-bottom:10px;border-bottom:2px solid #e2e8f0;">Traveller Information</h2>
            <table width="100%" cellpadding="0" cellspacing="0" border="0" style="font-size:15px;">
              <tr><td style="padding:10px 0;color:#64748b;width:30%;">Full Name</td><td style="padding:10px 0;font-weight:600;color:#0f172a;">{customer_name}</td></tr>
              <tr><td style="padding:10px 0;color:#64748b;">Email</td><td style="padding:10px 0;font-weight:500;color:#1d4ed8;">✉️ {customer_email}</td></tr>
              <tr><td style="padding:10px 0;color:#64748b;">Phone</td><td style="padding:10px 0;font-weight:600;color:#0f172a;">📞 {phone}</td></tr>
            </table>
          </td></tr>
          <tr><td style="padding:30px 35px 40px;background:linear-gradient(180deg,#ffffff 0%,#f8fafc 100%);">
            <div style="background:#eff6ff;border:1px solid #bfdbfe;border-radius:16px;padding:24px;text-align:center;">
              <div style="font-size:32px;margin-bottom:10px;">🎉</div>
              <h3 style="margin:0 0 10px;font-size:18px;font-weight:700;color:#1d4ed8;">Your adventure begins here!</h3>
              <p style="margin:0;font-size:14px;color:#475569;line-height:1.6;">We've received your booking. If you have any questions, need to make changes, or want to add extras, please don't hesitate to get in touch.</p>
              <p style="margin:16px 0 0;font-size:14px;font-weight:600;color:#1e40af;">Contact: support@travelintel.ai</p>
            </div>
          </td></tr>
          <tr><td style="padding:25px 35px;background:#0f172a;color:#cbd5e1;text-align:center;">
            <div style="font-size:22px;font-weight:800;color:#ffffff;letter-spacing:-0.5px;margin-bottom:8px;">✈️ TravelIntel AI</div>
            <div style="font-size:13px;color:#94a3b8;margin-bottom:18px;">Smart travel. Better journeys.</div>
            <table width="100%" cellpadding="0" cellspacing="0" border="0" style="font-size:12px;color:#64748b;">
              <tr><td align="center" style="padding:6px 0;">Email: <a href="mailto:support@travelintel.ai" style="color:#93c5fd;text-decoration:none;">support@travelintel.ai</a></td></tr>
            </table>
          </td></tr>
        </table>
      </td></tr>
    </table>
    <p style="margin:25px 0 0;font-size:12px;color:#94a3b8;text-align:center;">© {datetime.datetime.now().year} TravelIntel AI. All rights reserved.</p>
    </body></html>
    """

    text = f"""TravelIntel AI — BOOKING CONFIRMED

Hello {customer_name},

Your trip has been successfully booked!

BOOKING REFERENCE
{reference}

TRIP DETAILS
----------------------------------------
Package: {package_name}
Destination: {destination}
Travel Date: {travel_date}
Duration: {duration}
Travellers: {travelers}

PAYMENT
----------------------------------------
Payment Method: {payment_method}
Total: R {total_amount:,.2f}

CUSTOMER DETAILS
----------------------------------------
Name: {customer_name}
Email: {customer_email}
Phone: {phone}

Booking Date: {booking_date}

Thank you for choosing TravelIntel AI.

Smart travel. Better journeys.
"""

    return _dispatch_email(
        to_email=to_email,
        subject=subject,
        html_body=html,
        text_body=text,
        from_email="TravelIntel AI <bookings@notify.moviewatchtv.fun>",
        reply_to="support@travelintel.ai",
        outbox_prefix=f"booking_{booking_id}" if booking_id != "unknown" else "booking",
    )

# --- Global Exception Handlers ---
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code == 401 and request.url.path.startswith("/dashboard"):
        return RedirectResponse(url="/admin/login")
    return JSONResponse(
        status_code=exc.status_code,
        content={"success": False, "error": exc.detail}
    )

@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    traceback.print_exc()
    logger.exception("Unhandled exception on path=%s", request.url.path)
    return JSONResponse(
        status_code=500,
        content={"success": False, "error": "Internal server error"}
    )

# ============================================================
# FRONTEND PAGE ROUTES (fixed: @app.get instead of @app.route)
# ============================================================

@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")

@app.get("/home")
async def home(request: Request):
    return templates.TemplateResponse(request, "index.html")

@app.get("/auth/login")
async def auth_login_page(request: Request):
    # Already logged in → skip the login form and go straight to the app
    session = get_session_from_request(request)
    if session:
        dest = get_logged_in_redirect(session)
        if dest:
            return RedirectResponse(url=dest, status_code=302)
    return templates.TemplateResponse(request, "auth.html")

@app.get("/auth/register")
async def auth_register_page(request: Request):
    # Already logged in → no need to register again
    session = get_session_from_request(request)
    if session:
        dest = get_logged_in_redirect(session)
        if dest:
            return RedirectResponse(url=dest, status_code=302)
    return templates.TemplateResponse(request, "auth.html")

@app.get("/auth/change-password")
async def auth_change_password_page(request: Request):
    session = get_session_from_request(request)
    if not session:
        return RedirectResponse(url="/auth/login", status_code=302)
    return templates.TemplateResponse(request, "change-password.html")

@app.get("/admin/login")
async def admin_login_page(request: Request):
    # Honour existing session the same way /auth/login does
    session = get_session_from_request(request)
    if session:
        dest = get_logged_in_redirect(session)
        if dest:
            return RedirectResponse(url=dest, status_code=302)
    return RedirectResponse(url="/auth/login", status_code=302)

@app.get("/admin/forgot-password")
async def admin_forgot_password_page(request: Request):
    session = get_session_from_request(request)
    if session:
        dest = get_logged_in_redirect(session)
        if dest:
            return RedirectResponse(url=dest, status_code=302)
    return templates.TemplateResponse(request, "forgot-password.html")

@app.get("/forgot-password")
async def forgot_password_page(request: Request):
    # Shared customer/admin forgot-password page — redirect if already logged in
    session = get_session_from_request(request)
    if session:
        dest = get_logged_in_redirect(session)
        if dest:
            return RedirectResponse(url=dest, status_code=302)
    return templates.TemplateResponse(request, "forgot-password.html")

@app.get("/dashboard")
async def dashboard(request: Request):
    session = get_session_from_request(request)
    if not session or not get_active_admin(session["user_id"]):
        return RedirectResponse(url="/auth/login", status_code=302)

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) AS total FROM Bookings WHERE status != 'cancelled'")
    total_bookings = int(c.fetchone()["total"] or 0)
    c.execute("SELECT COALESCE(SUM(number_of_travelers),0) AS total FROM Bookings WHERE status != 'cancelled'")
    total_travelers = int(c.fetchone()["total"] or 0)
    c.execute("SELECT COALESCE(SUM(total_amount),0) AS total FROM Bookings WHERE status != 'cancelled'")
    total_revenue = float(c.fetchone()["total"] or 0)
    c.execute("SELECT COUNT(*) AS total FROM Customers")
    total_customers = int(c.fetchone()["total"] or 0)
    c.execute("SELECT COUNT(*) AS total FROM Reviews WHERE source='google'")
    review_count = int(c.fetchone()["total"] or 0)
    c.execute("SELECT COALESCE(AVG(rating),0) AS avg_rating FROM Reviews WHERE source='google'")
    review_average = float(c.fetchone()["avg_rating"] or 0)
    c.execute("""SELECT b.booking_id, COALESCE(c.name, 'Guest') AS name, p.package_name,
                        b.number_of_travelers, b.total_amount, b.status, b.booking_date
                 FROM Bookings b
                 LEFT JOIN Customers c ON b.customer_id = c.customer_id
                 LEFT JOIN Packages p ON b.package_id = p.package_id
                 ORDER BY b.booking_id DESC LIMIT 10""")
    recent_bookings = [dict(row) for row in c.fetchall()]
    feedback_summary = _generate_customer_feedback_summary(c, limit=10)
    conn.close()
    initial_dashboard = {
        "total_bookings": total_bookings,
        "total_travelers": total_travelers,
        "total_revenue": total_revenue,
        "total_customers": total_customers,
        "avg_order_value": total_revenue / total_bookings if total_bookings else 0,
        "review_count": review_count,
        "review_average": review_average,
        "recent_bookings": recent_bookings,
        "feedback_summary": feedback_summary,
    }
    return templates.TemplateResponse(request, "admin/dashboard.html", {"request": request, "initial_dashboard": initial_dashboard})


# ---- Clean page URLs (no .html in the address bar) ----
# Reserved path segments that must NOT be treated as HTML page names
_RESERVED_PAGE_NAMES = frozenset({
    "api", "static", "admin", "auth", "dashboard", "home", "packages",
    "bookings", "docs", "openapi.json", "redoc",
})


@app.get("/admin/{page}")
async def render_admin_page(request: Request, page: str):
    """Serve admin/*.html templates at clean URLs like /admin/packages."""
    # Dedicated handlers already cover these
    if page in {"login", "logout", "forgot-password"}:
        if page == "login":
            return RedirectResponse(url="/auth/login", status_code=302)
        if page == "forgot-password":
            return RedirectResponse(url="/admin/forgot-password", status_code=302)
        raise HTTPException(status_code=404, detail="Page not found")

    # Block removed interfaces
    if page in {"users", "destinations"}:
        raise HTTPException(status_code=404, detail="Page not found")

    session = get_session_from_request(request)
    if not session or not get_active_admin(session["user_id"]):
        return RedirectResponse(url="/auth/login", status_code=302)
    try:
        return templates.TemplateResponse(request, f"admin/{page}.html")
    except Exception:
        raise HTTPException(status_code=404, detail="Page not found")


# ---- Backward-compatible redirects: old .html URLs → clean URLs ----
@app.get("/admin/{page}.html")
async def redirect_admin_html(page: str):
    return RedirectResponse(url=f"/admin/{page}", status_code=301)


@app.get("/{page}.html")
async def redirect_page_html(page: str):
    if page == "auth":
        return RedirectResponse(url="/auth/login", status_code=301)
    if page == "forgot-password":
        return RedirectResponse(url="/forgot-password", status_code=301)
    return RedirectResponse(url=f"/{page}", status_code=301)

# ============================================================
# CUSTOMER AUTH API ROUTES (NEW — were missing entirely)
# ============================================================

@app.post("/api/auth/register")
async def customer_register(data: CustomerRegisterRequest):

    email = normalize_email(data.email)

    if not is_valid_email(email):
        raise HTTPException(
            status_code=400,
            detail="Invalid email"
        )

    email_local = email.split("@")[0] if "@" in email else email
    validate_password_strength(data.password, forbidden_substrings=[email, email_local])

    conn = get_db_connection()
    c = conn.cursor()

    c.execute(
        "SELECT * FROM Users WHERE email=?",
        (email,)
    )

    if c.fetchone():
        conn.close()
        raise HTTPException(
            status_code=400,
            detail="Email already registered"
        )

    conn.close()


    otp = str(random.randint(100000,999999))


    registration_otps[email] = {
        "otp": otp,
        "password": data.password,
        "expires": datetime.datetime.now()
        + datetime.timedelta(minutes=10)
    }
    print("REGISTER OTP:", email, otp)

    try:
        email_ok = send_verification_email(email, otp)
    except Exception:
        email_ok = False
        logger.exception("Unhandled error sending registration verification email to %s", email)

    if not email_ok:
        registration_otps.pop(email, None)
        raise HTTPException(
            status_code=500,
            detail="Unable to send verification email. Please try again or contact support at " + DEFAULT_ADMIN_EMAIL + "."
        )

    return {
        "success": True,
        "message": (
            "Verification code dispatched. If you don't receive it within 2 minutes, "
            "check your spam folder or contact support at " + DEFAULT_ADMIN_EMAIL + "."
        )
    }

class VerifyRegistrationRequest(BaseModel):
    email: str
    otp: str



@app.post("/api/auth/verify-registration")
async def verify_registration(data: VerifyRegistrationRequest):

    email = normalize_email(data.email)


    record = registration_otps.get(email)


    if not record:
        raise HTTPException(
            status_code=400,
            detail="OTP expired or not requested"
        )


    if datetime.datetime.now() > record["expires"]:
        del registration_otps[email]

        raise HTTPException(
            status_code=400,
            detail="OTP expired"
        )


    if record["otp"] != data.otp:
        raise HTTPException(
            status_code=400,
            detail="Incorrect OTP"
        )


    conn = get_db_connection()
    c = conn.cursor()


    username = email.split("@")[0]


    c.execute(
        """
        INSERT INTO Users
        (username,password_hash,role,user_type,full_name,email)
        VALUES (?,?,?,?,?,?)
        """,
        (
            username,
            generate_password_hash(record["password"]),
            "customer",
            "client",
            username,
            email
        )
    )


    user_id = c.lastrowid


    c.execute(
        """
        INSERT INTO Customers
        (user_id,name,email)
        VALUES (?,?,?)
""",
        (
            user_id,
            username,
            email
        )
    )


    conn.commit()
    conn.close()


    del registration_otps[email]


    return {
        "success": True,
        "message": "Account created successfully"
    }

@app.post("/api/auth/register/resend")
async def resend_registration_otp(data: VerifyRegistrationRequest):

    email = normalize_email(data.email)

    if not is_valid_email(email):
        raise HTTPException(
            status_code=400,
            detail="Invalid email"
        )

    # Generate a fresh OTP and overwrite the pending registration
    otp = str(random.randint(100000, 999999))

    registration_otps[email] = {
        "otp": otp,
        "password": registration_otps.get(email, {}).get("password", ""),
        "expires": datetime.datetime.now()
        + datetime.timedelta(minutes=10)
    }
    print("RE-SEND REGISTER OTP:", email, otp)

    try:
        email_ok = send_verification_email(email, otp)
    except Exception:
        email_ok = False
        logger.exception("Unhandled error resending registration verification email to %s", email)

    if not email_ok:
        raise HTTPException(
            status_code=500,
            detail="Unable to resend verification code. Please try again or contact support at " + DEFAULT_ADMIN_EMAIL + "."
        )

    return {
        "success": True,
        "message": (
            "A new verification code has been dispatched. If you don't receive it within 2 minutes, "
            "check your spam folder or contact support at " + DEFAULT_ADMIN_EMAIL + "."
        )
    }

@app.post("/api/auth/login")
async def unified_login(data: CustomerLoginRequest):
    email = normalize_email(data.email)
    if not is_valid_email(email):
        raise HTTPException(status_code=400, detail="Please enter a valid email address.")

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM Users WHERE LOWER(email)=? LIMIT 1", (email,))
    user = c.fetchone()
    conn.close()

    if not user or user["account_status"] != "active" or not check_password_hash(user["password_hash"], data.password):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    user_type = user["user_type"] if user["user_type"] in {"admin", "client"} else ("admin" if int(user["is_admin"] or 0) == 1 or user["role"] == "admin" else "client")
    session_role = "admin" if user_type == "admin" else "customer"
    session_id = create_session(user["user_id"], session_role)

    display_name = user["full_name"] or user["username"] or email.split("@")[0]
    if user_type == "client":
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT name FROM Customers WHERE user_id = ?", (user["user_id"],))
        cust = c.fetchone()
        conn.close()
        if cust and cust["name"]:
            display_name = cust["name"]

    response = JSONResponse(content={
        "success": True,
        "token": session_id,
        "user": {
            "user_id": user["user_id"],
            "email": user["email"],
            "username": user["username"],
            "full_name": display_name,
            "user_type": user_type,
            "role": session_role,
            "is_admin": user_type == "admin",
            "must_change_password": bool(user["must_change_password"] if "must_change_password" in user.keys() else 0)
        },
        "redirect": "/auth/change-password" if bool(user["must_change_password"] if "must_change_password" in user.keys() else 0) else ("/dashboard" if user_type == "admin" else "/home")
    })
    response.set_cookie(key="session_id", value=session_id, httponly=True, secure=False, samesite="lax", path="/")
    return response


@app.get("/api/auth/me")
async def get_current_user(request: Request):
    session = get_session_from_request(request)
    if not session:
        return JSONResponse(content={"success": False, "error": "Not authenticated"}, status_code=401)

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT user_id, email, username, full_name, user_type, role, is_admin, must_change_password, account_status FROM Users WHERE user_id = ? LIMIT 1", (session["user_id"],))
    user = c.fetchone()
    conn.close()

    if not user:
        return JSONResponse(content={"success": False, "error": "User not found"}, status_code=401)

    user_type = user["user_type"] if user["user_type"] in {"admin", "client"} else ("admin" if int(user["is_admin"] or 0) == 1 or user["role"] == "admin" else "client")
    display_name = user["full_name"] or user["username"] or (user["email"].split("@")[0] if user["email"] else "User")

    if user_type == "client":
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT name FROM Customers WHERE user_id = ? LIMIT 1", (user["user_id"],))
        cust = c.fetchone()
        conn.close()
        if cust and cust["name"]:
            display_name = cust["name"]

    return JSONResponse(content={
        "success": True,
        "user": {
            "user_id": user["user_id"],
            "email": user["email"],
            "username": user["username"],
            "full_name": display_name,
            "user_type": user_type,
            "role": "admin" if user_type == "admin" else "customer",
            "is_admin": user_type == "admin",
            "must_change_password": bool(user["must_change_password"] if "must_change_password" in user.keys() else 0),
            "account_status": user["account_status"]
        }
    })


@app.post("/api/admin/forgot-password/request")
async def request_admin_password_reset(data: AdminForgotPasswordRequest):
    """Send a reset OTP to the email belonging to an active admin account."""
    email = normalize_email(data.email)
    if not is_valid_email(email):
        raise HTTPException(status_code=400, detail="Enter a valid administrator email address.")

    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT user_id, email, username FROM Users "
        "WHERE LOWER(email)=? AND is_admin=1 AND account_status='active' LIMIT 1",
        (email,),
    )
    admin = c.fetchone()
    conn.close()

    if not admin:
        raise HTTPException(status_code=404, detail="No active administrator is registered with that email.")

    account_email = normalize_email(admin["email"])
    otp = f"{secrets.randbelow(1000000):06d}"
    password_reset_otps[account_email] = {
        "otp": otp,
        "expires_at": datetime.datetime.utcnow() + datetime.timedelta(minutes=10),
        "attempts": 0,
    }

    try:
        email_ok = send_password_reset_email(account_email, otp)
    except Exception:
        email_ok = False
        logger.exception("Unhandled error sending admin password reset email to %s", account_email)

    if not email_ok:
        password_reset_otps.pop(account_email, None)
        raise HTTPException(
            status_code=500,
            detail="Unable to send password reset email. Please try again or contact support at " + DEFAULT_ADMIN_EMAIL + "."
        )

    logger.info("Admin password reset OTP dispatched to %s", account_email)
    return {
        "success": True,
        "data": {
            "message": (
                "Password reset code dispatched to the administrator email. "
                "If you don't receive it within 2 minutes, check your spam folder "
                "or contact support at " + DEFAULT_ADMIN_EMAIL + "."
            ),
            "email": account_email,
        },
    }



class AdminDeleteRequest(BaseModel):
    confirm_password: str

class AdminActionConfirmRequest(BaseModel):
    confirm_password: str

class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


def get_shared_admin_password_hash() -> Optional[str]:
    """Fetch the password hash from any active admin to use as the shared credential."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT password_hash FROM Users "
        "WHERE is_admin=1 AND account_status='active' "
        "ORDER BY user_id ASC LIMIT 1"
    )
    row = c.fetchone()
    conn.close()
    if row and row["password_hash"]:
        return row["password_hash"]
    return None


def sync_admin_password_to_all(new_password_hash: str, exclude_user_id: Optional[int] = None) -> int:
    """Overwrite every admin account's password hash with the given one.

    Returns the number of updated rows.
    """
    conn = get_db_connection()
    c = conn.cursor()
    if exclude_user_id is not None:
        c.execute(
            "UPDATE Users SET password_hash=?, password_changed_at=CURRENT_TIMESTAMP "
            "WHERE is_admin=1 AND user_id != ?",
            (new_password_hash, exclude_user_id),
        )
    else:
        c.execute(
            "UPDATE Users SET password_hash=?, password_changed_at=CURRENT_TIMESTAMP "
            "WHERE is_admin=1",
            (new_password_hash,),
        )
    count = c.rowcount
    conn.commit()
    conn.close()
    return count


def verify_admin_password(user_id: int, password: str) -> bool:
    """Verify the current admin password for sensitive action confirmation."""
    if not password:
        return False
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT password_hash FROM Users WHERE user_id=? AND is_admin=1 LIMIT 1", (user_id,))
    row = c.fetchone()
    conn.close()
    if not row or not row["password_hash"]:
        return False
    return check_password_hash(row["password_hash"], password)

@app.post("/api/auth/change-password")
async def change_password(data: ChangePasswordRequest, request: Request):
    session = get_session_from_request(request)
    if not session:
        raise HTTPException(status_code=401, detail="Please login first")

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM Users WHERE user_id=?", (session["user_id"],))
    user_row = c.fetchone()
    user = dict(user_row) if user_row else None

    if not user or not check_password_hash(user["password_hash"], data.current_password):
        conn.close()
        raise HTTPException(status_code=400, detail="Current password is incorrect.")

    username_val = user.get("username") or ""
    email_val = user.get("email") or ""
    email_local_val = email_val.split("@")[0] if "@" in email_val else email_val
    validate_password_strength(
        data.new_password,
        forbidden_substrings=[username_val, email_val, email_local_val]
    )

    new_hash = generate_password_hash(data.new_password)
    is_admin = (
        int(user.get("is_admin") or 0) == 1
        or user.get("role") == "admin"
        or user.get("user_type") == "admin"
    )

    c.execute(
        "UPDATE Users SET password_hash=?, must_change_password=0, password_changed_at=CURRENT_TIMESTAMP WHERE user_id=?",
        (new_hash, session["user_id"])
    )
    conn.commit()
    conn.close()

    if is_admin:
        record_admin_audit(
            session,
            "Changed own admin password",
            "admin",
            session["user_id"],
            f"Admin {user.get('username')} updated their own password."
        )

    return {
        "success": True,
        "data": {
            "message": "Password changed successfully.",
            "redirect": "/dashboard" if session.get("role") == "admin" else "/home"
        }
    }

class PasswordResetRequest(BaseModel):
    email: str
    password: Optional[str] = ""

class PasswordResetVerifyRequest(BaseModel):
    email: str
    password: str
    otp: str


@app.post("/api/auth/forgot-password/request")
async def request_customer_password_reset(data: PasswordResetRequest):
    """Send a reset OTP to the email belonging to an active customer OR admin account.

    Both customers and administrators use the same shared forgot-password page
    (/forgot-password.html) linked from the unified sign-in page. This endpoint
    looks up the email across all active Users regardless of role — admin resets
    then universally sync the new password to every admin via the verify endpoint.
    """
    email = normalize_email(data.email)
    if not is_valid_email(email):
        raise HTTPException(status_code=400, detail="Enter a valid email address.")

    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT user_id, email, username, is_admin, role, user_type FROM Users "
        "WHERE LOWER(email)=? AND account_status='active' LIMIT 1",
        (email,),
    )
    user_row = c.fetchone()
    conn.close()

    if not user_row:
        raise HTTPException(status_code=404, detail="No active account is registered with that email.")
    user = dict(user_row)

    account_email = normalize_email(user["email"])
    otp = f"{secrets.randbelow(1000000):06d}"
    password_reset_otps[account_email] = {
        "otp": otp,
        "expires_at": datetime.datetime.utcnow() + datetime.timedelta(minutes=10),
        "attempts": 0,
    }

    try:
        email_ok = send_password_reset_email(account_email, otp)
    except Exception:
        email_ok = False
        logger.exception("Unhandled error sending password reset email to %s", account_email)

    if not email_ok:
        password_reset_otps.pop(account_email, None)
        raise HTTPException(
            status_code=500,
            detail="Unable to send password reset email. Please try again or contact support at " + DEFAULT_ADMIN_EMAIL + "."
        )

    is_admin_flag = int(user.get("is_admin") or 0) == 1 or user.get("role") == "admin" or user.get("user_type") == "admin"
    logger.info("Password reset OTP dispatched to %s (is_admin=%s)", account_email, is_admin_flag)
    return {
        "success": True,
        "data": {
            "message": (
                "Password reset code dispatched to your email. "
                "If you don't receive it within 2 minutes, check your spam folder "
                "or contact support at " + DEFAULT_ADMIN_EMAIL + "."
            ),
            "email": account_email,
        },
    }


@app.post("/api/auth/forgot-password/verify")
async def verify_password_reset(data: PasswordResetVerifyRequest):

    email = normalize_email(data.email)
    otp = (data.otp or "").strip()

    if not is_valid_email(email):
        raise HTTPException(
            status_code=400,
            detail="Invalid email address"
        )

    if not otp.isdigit() or len(otp) != 6:
        raise HTTPException(
            status_code=400,
            detail="Please enter the 6-digit verification code"
        )

    email_local = email.split("@")[0] if "@" in email else email
    validate_password_strength(data.password, forbidden_substrings=[email, email_local])

    record = password_reset_otps.get(email)

    if not record:
        raise HTTPException(
            status_code=400,
            detail="OTP not requested or expired"
        )

    # Check expiry
    if datetime.datetime.utcnow() > record["expires_at"]:

        del password_reset_otps[email]

        raise HTTPException(
            status_code=400,
            detail="OTP expired. Please request a new code."
        )

    # Limit incorrect attempts
    if record["attempts"] >= 5:

        del password_reset_otps[email]

        raise HTTPException(
            status_code=429,
            detail="Too many incorrect attempts. Please request a new OTP."
        )

    # Verify OTP
    if record["otp"] != otp:

        record["attempts"] += 1

        raise HTTPException(
            status_code=400,
            detail="Invalid OTP"
        )

    # Update password
    conn = get_db_connection()
    c = conn.cursor()

    try:
        # Determine if this is an admin reset so we can sync universally
        c.execute("SELECT is_admin, role, user_type FROM Users WHERE email=? LIMIT 1", (email,))
        target_row = c.fetchone()
        target_user = dict(target_row) if target_row else None
        is_admin_reset = (
            target_user is not None and (
                int(target_user.get("is_admin") or 0) == 1
                or target_user.get("role") == "admin"
                or target_user.get("user_type") == "admin"
            )
        )

      
        new_hash = generate_password_hash(data.password)
        c.execute(
            """
            UPDATE Users
            SET password_hash=?, must_change_password=0, password_changed_at=CURRENT_TIMESTAMP
            WHERE email=?
            """,
            (new_hash, email)
        )
        rows_updated = c.rowcount





        if rows_updated == 0:
            raise HTTPException(
                status_code=404,
                detail="Account not found"
            )

        conn.commit()

        # Record audit when an admin resets their own password via OTP
        if is_admin_reset and rows_updated > 0:
            c.execute(
                "SELECT user_id, username FROM Users WHERE is_admin=1 AND LOWER(email)=? LIMIT 1",
                (email,),
            )
            initiator_row = c.fetchone()
            initiator = dict(initiator_row) if initiator_row else None
            if initiator and initiator.get("user_id"):
                pseudo_session = {"user_id": initiator["user_id"]}
                record_admin_audit(
                    pseudo_session,
                    "Changed own admin password (via OTP)",
                    "admin",
                    initiator["user_id"],
                    f"Password reset via OTP for {email}.",
                )

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()

    # Delete OTP after successful password reset
    del password_reset_otps[email]

    return {
        "success": True,
        "data": {
            "message": "Password reset successful. You can now login with your new password."
        }
    }

# ============================================================
# PROFILE API ROUTES (NEW — were missing entirely)
# ============================================================

@app.get("/api/profile")
async def get_profile(request: Request):
    session = get_session_from_request(request)
    
    empty_user = {k: "" for k in ["full_name", "email", "phone", "address", "payment_method", "card_number", "card_expiry", "bank_name", "account_number"]}
    
    if not session:
        return {"success": True, "user": empty_user}

    conn = get_db_connection()
    c = conn.cursor()
    
    # Get profile details from Customers table
    c.execute(
        '''SELECT name, email, phone, address, payment_method, card_number, card_expiry, bank_name, account_number
           FROM Customers
           WHERE user_id = ?''',
        (session['user_id'],)
    )
    row = c.fetchone()
    conn.close()

    if not row:
        return {"success": True, "user": empty_user}

    return {
        "success": True,
        "user": {
            "full_name": row['name'] or '',
            "email": row['email'] or '',
            "phone": row['phone'] or '',
            "address": row['address'] or '',
            "payment_method": row['payment_method'] or '',
            "card_number": row['card_number'] or '',
            "card_expiry": row['card_expiry'] or '',
            "bank_name": row['bank_name'] or '',
            "account_number": row['account_number'] or '',
        }
    }


@app.put("/api/profile")
async def update_profile(request: Request, data: ProfileUpdateRequest):
    session = get_session_from_request(request)
    if not session:
        raise HTTPException(status_code=401, detail="Please login first")

    validate_payment_details(
        data.payment_method,
        data.card_number,
        data.card_expiry,
        "",
        data.bank_name,
        data.account_number,
    )

    conn = get_db_connection()
    c = conn.cursor()

    try:
        c.execute("UPDATE Users SET full_name=? WHERE user_id=?",
                  (data.full_name, session['user_id']))
        c.execute("UPDATE Customers SET name=?, phone=?, address=?, payment_method=?, card_number=?, card_expiry=?, bank_name=?, account_number=? WHERE user_id=?",
                  (data.full_name, data.phone, data.address, data.payment_method, data.card_number, data.card_expiry, data.bank_name, data.account_number, session['user_id']))
        conn.commit()
    except Exception:
        conn.rollback()
        raise HTTPException(status_code=500, detail="Update failed")
    finally:
        conn.close()

    return {"success": True, "data": {"message": "Profile updated successfully"}}

# ============================================================
# ADMIN AUTH ROUTES (FIXED — response now includes token)
# ============================================================

@app.post("/admin/login")
async def login(data: LoginRequest, request: Request):
    client_ip = request.client.host if request.client and request.client.host else "default"

    now = datetime.datetime.now()
    if client_ip in login_attempts:
        attempts = login_attempts[client_ip]
        attempts = [t for t in attempts if (now - t).total_seconds() < LOGIN_LOCKOUT_MINUTES * 60]
        login_attempts[client_ip] = attempts
        if len(attempts) >= MAX_LOGIN_ATTEMPTS:
            raise HTTPException(status_code=429, detail="Too many attempts. Account temporarily locked.")

    conn = get_db_connection()
    c = conn.cursor()
    identifier = normalize_email(data.username)
    c.execute(
        "SELECT * FROM Users WHERE (username=? OR LOWER(email)=?) AND is_admin=1 LIMIT 1",
        (data.username.strip(), identifier),
    )
    user = c.fetchone()
    conn.close()

    if user and user["account_status"] == "active" and check_password_hash(user['password_hash'], data.password):
        if client_ip in login_attempts:
            del login_attempts[client_ip]

        session_id = create_session(user['user_id'], user['role'])
        response = JSONResponse(content={
            "success": True,
            "token": session_id,
            "data": {"message": "Login successful"}
        })
        response.set_cookie(key="session_id", value=session_id, httponly=True, secure=False, samesite="lax", path="/")
        logger.info("Admin login success username=%s ip=%s", data.username, client_ip)
        return response

    if client_ip not in login_attempts:
        login_attempts[client_ip] = []
    login_attempts[client_ip].append(now)
    logger.warning("Admin login failure username=%s ip=%s", data.username, client_ip)

    raise HTTPException(status_code=401, detail="Invalid credentials")


@app.post("/api/auth/logout")
@app.post("/admin/logout")
async def logout(request: Request):
    """Clear the current session cookie and DB row (works for admin and customer)."""
    session_id = request.cookies.get("session_id")
    if not session_id:
        authorization = request.headers.get("Authorization", "")
        if authorization.lower().startswith("bearer "):
            session_id = authorization[7:].strip()
    if session_id:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("DELETE FROM Sessions WHERE session_id = ?", (session_id,))
        conn.commit()
        conn.close()
    response = JSONResponse(content={"success": True, "message": "Logged out successfully."})
    response.delete_cookie("session_id", path="/")
    return response

# ============================================================
# PACKAGES API (works on both /packages and /api/packages)
# ============================================================

@app.get("/packages")
@app.get("/api/packages")
async def get_packages(sort: str = "default"):
    conn = get_db_connection()
    c = conn.cursor()

    order_by = {
        "default": "p.package_id ASC",
        "price_asc": "p.price ASC, p.package_id ASC",
        "price_desc": "p.price DESC, p.package_id ASC",
        "bookings_desc": "booking_count DESC, p.package_id ASC",
    }.get((sort or "default").lower(), "p.package_id ASC")

    c.execute(f"""
        SELECT p.package_id, p.package_name, p.destination, p.price, p.duration,
               p.description, p.availability_status, p.season_category, p.image_url,
               COALESCE(p.available_spots,0) AS available_spots,
               COALESCE(p.total_spots,0) AS total_spots,
               COALESCE(p.discount_percentage,0) AS discount_percentage,
               COUNT(CASE WHEN b.status != 'cancelled' THEN b.booking_id END) AS booking_count
        FROM Packages p
        LEFT JOIN Bookings b ON b.package_id = p.package_id
        GROUP BY p.package_id
        ORDER BY {order_by}
    """)
    packages = [dict(row) for row in c.fetchall()]
    conn.close()
    return {"success": True, "data": packages, "sort": sort}


class PackageCreateRequest(BaseModel):
    package_name: str
    destination: str
    price: float
    available_spots: int = 0
    duration: int = 1
    description: str = ""
    season_category: str = "standard"
    image_url: str = ""
    image_file_ref: str = ""
    discount_percentage: int = 0
    confirm_password: str = ""

class PackageUpdateRequest(PackageCreateRequest):
    pass


def _resolve_package_image(image_url: str, image_file_ref: str) -> str:
    """Choose the final image_url value. Explicit image_url (non-empty) wins over file_ref."""
    chosen = (image_url or "").strip()
    if not chosen:
        chosen = (image_file_ref or "").strip()
    return chosen


@app.post("/api/admin/packages")
async def create_package(data: PackageCreateRequest, request: Request):
    session = require_admin(request)
    session = get_session_from_request(request) or {}
    if not verify_admin_password(session["user_id"], (data.confirm_password or "").strip()):
        raise HTTPException(status_code=403, detail="Current admin password is incorrect.")

    name = data.package_name.strip()
    destination = data.destination.strip()
    if not name or not destination:
        raise HTTPException(status_code=400, detail="Package name and destination are required.")
    if data.price <= 0:
        raise HTTPException(status_code=400, detail="Price must be greater than 0.")
    if data.available_spots < 0:
        raise HTTPException(status_code=400, detail="Available seats cannot be negative.")
    if data.duration < 1:
        raise HTTPException(status_code=400, detail="Duration must be at least 1 day.")
    discount = int(data.discount_percentage or 0)
    if discount < 0 or discount > 100:
        raise HTTPException(status_code=400, detail="Discount % must be between 0 and 100.")

    final_image = _resolve_package_image(data.image_url, data.image_file_ref)

    conn = get_db_connection(); c = conn.cursor()
    try:
        status = "Available" if data.available_spots > 0 else "Unavailable"
        c.execute("""INSERT INTO Packages
            (package_name,destination,price,duration,description,availability_status,season_category,image_url,available_spots,total_spots,discount_percentage)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (name,destination,float(data.price),int(data.duration),data.description.strip(),status,data.season_category.strip(),final_image,int(data.available_spots),int(data.available_spots),discount))
        package_id = c.lastrowid
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise HTTPException(status_code=409, detail="Unable to create package. A package with these details may already exist.") from exc
    finally:
        conn.close()
    record_admin_audit(session, "Created package", "package", package_id, f"{name} | price={data.price} | seats={data.available_spots} | discount={discount}%")
    return {"success": True, "data": {"package_id": package_id, "message": "Package created successfully."}}


@app.put("/api/admin/packages/{package_id}")
async def update_package(package_id: int, data: PackageUpdateRequest, request: Request):
    session = require_admin(request)
    session = get_session_from_request(request) or {}
    if not verify_admin_password(session["user_id"], (data.confirm_password or "").strip()):
        raise HTTPException(status_code=403, detail="Current admin password is incorrect.")

    if data.price <= 0 or data.available_spots < 0 or data.duration < 1:
        raise HTTPException(status_code=400, detail="Price, duration and available seats must be valid.")
    discount = int(data.discount_percentage or 0)
    if discount < 0 or discount > 100:
        raise HTTPException(status_code=400, detail="Discount % must be between 0 and 100.")
    conn = get_db_connection(); c = conn.cursor()
    c.execute("SELECT * FROM Packages WHERE package_id=?", (package_id,))
    before_row = c.fetchone()
    if not before_row:
        conn.close(); raise HTTPException(status_code=404, detail="Package not found")
    before = dict(before_row)
    final_image = _resolve_package_image(data.image_url, data.image_file_ref)
    status = "Available" if data.available_spots > 0 else "Unavailable"
    c.execute("""UPDATE Packages SET package_name=?,destination=?,price=?,duration=?,description=?,availability_status=?,season_category=?,image_url=?,available_spots=?,total_spots=?,discount_percentage=? WHERE package_id=?""",
              (data.package_name.strip(),data.destination.strip(),float(data.price),int(data.duration),data.description.strip(),status,data.season_category.strip(),final_image,int(data.available_spots),int(data.available_spots),discount,package_id))
    conn.commit(); conn.close()
    before_snippet = f"{before.get('package_name','')} | price={before.get('price','')} | seats={before.get('available_spots','')} | discount={before.get('discount_percentage',0)}%"
    after_snippet = f"{data.package_name} | price={data.price} | seats={data.available_spots} | discount={discount}%"
    record_admin_audit(
        session, "Updated package", "package", package_id,
        f"BEFORE: ({before_snippet})  AFTER: ({after_snippet})"
    )
    return {"success": True, "data": {"message": "Package updated successfully."}}


@app.delete("/api/admin/packages/{package_id}")
async def delete_package(package_id: int, request: Request):
    class _DeleteBody(BaseModel):
        confirm_password: str = ""
    try:
        body = await request.json()
    except Exception:
        body = {}
    confirm_password = str(body.get("confirm_password", "") or "").strip()
    session = require_admin(request)
    session = get_session_from_request(request) or {}
    if not verify_admin_password(session["user_id"], confirm_password):
        raise HTTPException(status_code=403, detail="Current admin password is incorrect.")

    conn = get_db_connection(); c = conn.cursor()
    c.execute("SELECT package_id, package_name, destination, price FROM Packages WHERE package_id=?", (package_id,))
    pkg_row = c.fetchone()
    if not pkg_row:
        conn.close(); raise HTTPException(status_code=404, detail="Package not found")
    pkg = dict(pkg_row)

    c.execute("SELECT COUNT(booking_id) AS cnt FROM Bookings WHERE package_id=?", (package_id,))
    total_bookings = int((c.fetchone() or {})["cnt"] or 0)
    if total_bookings > 0:
        conn.close()
        raise HTTPException(
            status_code=409,
            detail=f"Cannot delete package: it has {total_bookings} associated booking(s) (historical integrity). Mark it unavailable instead."
        )

    c.execute("DELETE FROM Packages WHERE package_id=?", (package_id,))
    conn.commit(); conn.close()
    record_admin_audit(
        session, "Deleted package", "package", package_id,
        f"Deleted package: {pkg['package_name']} | {pkg['destination']} | price={pkg['price']} (no associated bookings)"
    )
    return {"success": True, "data": {"message": "Package deleted successfully."}}

@app.get("/api/admin/packages")
async def get_admin_packages(request: Request):
    require_admin(request)
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT package_id, package_name, destination, price, duration, description, availability_status, season_category, image_url, COALESCE(available_spots,0) as available_spots, COALESCE(total_spots,0) as total_spots, COALESCE(discount_percentage,0) as discount_percentage FROM Packages ORDER BY package_id ASC")
    packages = [dict(row) for row in c.fetchall()]
    conn.close()
    return {"success": True, "data": packages}


@app.post("/api/admin/packages/upload-image")
async def upload_package_image(request: Request, image: UploadFile = File(...)):
    session = get_session_from_request(request) or {}
    if not session or session.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin authentication required")

    content_type = (image.content_type or "").lower()
    if content_type == "image/svg+xml" or content_type.startswith("image/svg"):
        raise HTTPException(
            status_code=400,
            detail="Only PNG, JPEG, GIF, and WEBP images are allowed (SVG is disabled)."
        )
    if content_type not in _ALLOWED_IMAGE_MIME_TO_EXT:
        raise HTTPException(
            status_code=400,
            detail="Only PNG, JPEG, GIF, and WEBP images are allowed (SVG is disabled)."
        )

    ext = _ALLOWED_IMAGE_MIME_TO_EXT[content_type]
    raw_bytes = await image.read(MAX_IMAGE_UPLOAD_BYTES + 1)
    if len(raw_bytes) > MAX_IMAGE_UPLOAD_BYTES:
        try:
            await image.close()
        except Exception:
            pass
        raise HTTPException(
            status_code=413,
            detail="Image exceeds the 5 MB size limit."
        )
    if not raw_bytes:
        raise HTTPException(status_code=400, detail="Empty image upload.")

    try:
        await image.close()
    except Exception:
        pass

    unique_name = uuid.uuid4().hex + ext
    save_path = os.path.join(UPLOADS_DIR, unique_name)
    with open(save_path, "wb") as fh:
        fh.write(raw_bytes)

    session = get_session_from_request(request) or {}
    actor = session.get("username") or (session.get("email") or "").split("@")[0] or "admin"
    try:
        record_admin_audit(
            session,
            "Uploaded package image",
            "image",
            None,
            f"uploaded as /static/uploads/packages/{unique_name} ({len(raw_bytes)} bytes, {content_type})"
        )
    except Exception:
        pass

    return {
        "success": True,
        "data": {
            "url": f"/static/uploads/packages/{unique_name}",
            "content_type": content_type,
            "bytes": len(raw_bytes)
        }
    }


class PackageSpotsRequest(BaseModel):
    available_spots: int
    total_spots: int = 0
    confirm_password: str = ""


@app.put("/api/admin/packages/{package_id}/spots")
async def set_package_spots(package_id: int, data: PackageSpotsRequest, request: Request):
    session = require_admin(request)
    session = get_session_from_request(request) or {}
    if not verify_admin_password(session["user_id"], (data.confirm_password or "").strip()):
        raise HTTPException(status_code=403, detail="Current admin password is incorrect.")

    if data.available_spots < 0:
        raise HTTPException(status_code=400, detail="Available spots cannot be negative")

    total = data.total_spots if data.total_spots and data.total_spots > 0 else data.available_spots
    if data.available_spots > total:
        total = data.available_spots

    conn = get_db_connection()
    c = conn.cursor()

    c.execute("SELECT package_id FROM Packages WHERE package_id=?", (package_id,))
    if not c.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="Package not found")

    # Update spots and derive availability status
    if data.available_spots > 0:
        status = "Available"
    else:
        status = "Unavailable"

    c.execute(
        "UPDATE Packages SET available_spots=?, total_spots=?, availability_status=? WHERE package_id=?",
        (data.available_spots, total, status, package_id)
    )
    conn.commit()
    conn.close()

    return {"success": True, "data": {"message": "Package availability updated", "available_spots": data.available_spots, "total_spots": total, "availability_status": status}}


@app.get("/api/packages/{package_id}")
async def get_package_by_id(package_id: int):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""SELECT package_id, package_name, destination, price, duration,
                        description, availability_status, season_category, image_url,
                        COALESCE(available_spots,0) AS available_spots,
                        COALESCE(total_spots,0) AS total_spots,
                        COALESCE(discount_percentage,0) AS discount_percentage
                 FROM Packages WHERE package_id=?""", (package_id,))
    package = c.fetchone()
    conn.close()
    if not package:
        raise HTTPException(status_code=404, detail="Package not found")
    return {"success": True, "data": dict(package)}

# ============================================================
# BOOKINGS API
# ============================================================

@app.post("/bookings")
async def create_booking(data: BookingRequest, request: Request):
    if data.number_of_travelers < 1:
        raise HTTPException(status_code=400, detail="Number of travelers must be at least 1")
    if data.number_of_travelers > 10:
        raise HTTPException(status_code=400, detail="Maximum 10 travelers per booking")

    try:
        travel_date_obj = datetime.date.fromisoformat(data.travel_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid travel date format")
    if travel_date_obj < datetime.date.today():
        raise HTTPException(status_code=400, detail="Travel date cannot be in the past")
    validate_payment_details(
        data.payment_method,
        data.card_number,
        data.card_expiry,
        "",
        data.bank_name,
        data.account_number,
    )

    email = normalize_email(data.email)
    if not is_valid_email(email):
        raise HTTPException(status_code=400, detail="Please provide a valid email address")

    session = get_session_from_request(request)
    
    conn = get_db_connection()
    c = conn.cursor()

    try:
        # 1. Handle Customer/User Linkage
        if session and session.get("role") == "customer":
            user_id = session['user_id']
            # Find existing customer record for this user
            c.execute("SELECT customer_id FROM Customers WHERE user_id=?", (user_id,))
            cust = c.fetchone()

            if not cust:
                # No record linked to this account yet — check if a guest booking
                # already created a Customers row under this same email, and if so,
                # link it to this account instead of creating a duplicate. This keeps
                # any prior guest bookings visible in "My Bookings" going forward.
                c.execute("SELECT customer_id FROM Customers WHERE LOWER(TRIM(email))=? AND user_id IS NULL", (email.strip().lower(),))
                cust = c.fetchone()
                if cust:
                    c.execute("UPDATE Customers SET user_id=? WHERE customer_id=?", (user_id, cust['customer_id']))

            if cust:
                customer_id = cust['customer_id']
                # Update existing record
                c.execute(
                    '''UPDATE Customers SET name=?, email=?, phone=?, address=?, payment_method=?, card_number=?, card_expiry=?, bank_name=?, account_number=? 
                       WHERE customer_id=?''',
                    (data.name, email, data.phone, data.address, data.payment_method, data.card_number, data.card_expiry, data.bank_name, data.account_number, customer_id)
                )
            else:
                # Create new record for this user
                c.execute(
                    '''INSERT INTO Customers (user_id, name, email, phone, address, payment_method, card_number, card_expiry, bank_name, account_number) 
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                    (user_id, data.name, email, data.phone, data.address, data.payment_method, data.card_number, data.card_expiry, data.bank_name, data.account_number)
                )
                customer_id = c.lastrowid
            
            # Sync name back to Users table
            c.execute("UPDATE Users SET full_name=? WHERE user_id=?", (data.name, user_id))
        else:
            # Fallback for guest booking (lookup by email)
            c.execute("SELECT customer_id FROM Customers WHERE email=?", (email,))
            cust = c.fetchone()
            if cust:
                customer_id = cust['customer_id']
                # Update guest details too
                c.execute(
                    '''UPDATE Customers SET name=?, phone=?, address=?, payment_method=?, card_number=?, card_expiry=?, bank_name=?, account_number=? 
                       WHERE customer_id=?''',
                    (data.name, data.phone, data.address, data.payment_method, data.card_number, data.card_expiry, data.bank_name, data.account_number, customer_id)
                )
            else:
                c.execute(
                    '''INSERT INTO Customers (name, email, phone, address, payment_method, card_number, card_expiry, bank_name, account_number) 
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                    (data.name, email, data.phone, data.address, data.payment_method, data.card_number, data.card_expiry, data.bank_name, data.account_number)
                )
                customer_id = c.lastrowid

        # 2. Package Validation
        c.execute("SELECT package_name, destination, duration, price, availability_status, COALESCE(available_spots,0) as available_spots, COALESCE(discount_percentage,0) as discount_percentage FROM Packages WHERE package_id=?", (data.package_id,))
        pkg = c.fetchone()
        if not pkg:
            raise HTTPException(status_code=404, detail="Package not found")
        if pkg["availability_status"] != "Available":
            raise HTTPException(status_code=400, detail="This package is currently unavailable")
        if int(pkg["available_spots"] or 0) < data.number_of_travelers:
            raise HTTPException(status_code=400, detail="Not enough available spots for this package")

        discount_pct = max(0, min(100, int(pkg["discount_percentage"] or 0)))
        unit_price = float(pkg['price'])
        if discount_pct > 0:
            unit_price = unit_price * (1 - discount_pct / 100.0)
        total_amount = round(unit_price, 2) * data.number_of_travelers

        # 3. Create Booking
        today = datetime.date.today().isoformat()
        c.execute(
            '''INSERT INTO Bookings (customer_id, package_id, booking_date, travel_date, number_of_travelers, total_amount, status, payment_method)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
            (customer_id, data.package_id, today, travel_date_obj.isoformat(), data.number_of_travelers, total_amount, 'confirmed', data.payment_method or "unknown")
        )
        booking_id = c.lastrowid

        # 4. Decrement available spots (capacity-based by number of travelers)
        c.execute(
            "UPDATE Packages SET available_spots = MAX(0, COALESCE(available_spots,0) - ?) WHERE package_id=?",
            (data.number_of_travelers, data.package_id)
        )
        # Auto-mark package as Unavailable if spots reach 0
        c.execute(
            "UPDATE Packages SET availability_status = CASE WHEN COALESCE(available_spots,0) <= 0 THEN 'Unavailable' ELSE 'Available' END WHERE package_id=?",
            (data.package_id,)
        )
        conn.commit()

        booking_summary = {
            "booking_id": booking_id,
            "name": data.name,
            "email": email,
            "phone": data.phone,
            "package_name": pkg["package_name"],
            "destination": pkg["destination"],
            "duration": f"{pkg['duration']} Days",
            "travel_date": travel_date_obj.isoformat(),
            "number_of_travelers": data.number_of_travelers,
            "total_amount": total_amount,
            "booking_date": today,
            "payment_method": data.payment_method or "unknown",
        }
        email_sent = send_booking_email(email, booking_summary)
    except Exception as e:
        conn.rollback()
        logger.error(f"Booking error: {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail="Booking process failed")
    finally:
        conn.close()

    return {"success": True, "data": {"message": "Booking successful!", "booking_id": booking_id, "booking": booking_summary, "email_sent": email_sent}}


@app.post("/api/bookings/{booking_id}/resend-email")
async def resend_booking_email(booking_id: int):
    if booking_id <= 0:
        raise HTTPException(status_code=400, detail="Invalid booking ID")

    conn = get_db_connection()
    c = conn.cursor()

    try:
        c.execute(
            '''SELECT b.booking_id, b.booking_date, b.travel_date, b.number_of_travelers,
                      b.total_amount, b.status, b.payment_method,
                      c.name, c.email, c.phone, c.address,
                      p.package_name, p.destination, p.duration, p.price
               FROM Bookings b
               JOIN Customers c ON b.customer_id = c.customer_id
               JOIN Packages p ON b.package_id = p.package_id
               WHERE b.booking_id = ?''',
            (booking_id,)
        )
        row = c.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Booking not found")

        booking_summary = {
            "booking_id": row["booking_id"],
            "name": row["name"] or "Valued Traveller",
            "email": row["email"],
            "phone": row["phone"] or "Not provided",
            "package_name": row["package_name"] or "Travel Package",
            "destination": row["destination"] or "Destination",
            "duration": f"{row['duration']} Days" if row["duration"] else "Not specified",
            "travel_date": row["travel_date"],
            "number_of_travelers": row["number_of_travelers"],
            "total_amount": row["total_amount"],
            "booking_date": row["booking_date"],
            "payment_method": row["payment_method"] or "unknown",
        }

        if not booking_summary["email"] or not is_valid_email(booking_summary["email"]):
            raise HTTPException(status_code=400, detail="No valid email address associated with this booking")

        email_sent = send_booking_email(booking_summary["email"], booking_summary)

        if not email_sent:
            raise HTTPException(status_code=500, detail="Failed to resend booking confirmation email. Please try again later.")

        return {"success": True, "message": "Booking confirmation email resent successfully"}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to resend booking confirmation email. booking_id=%s error=%s", booking_id, str(e))
        raise HTTPException(status_code=500, detail="An error occurred while resending the email. Please try again later.")
    finally:
        conn.close()


@app.get("/api/my-bookings")
async def get_my_bookings(request: Request):
    session = get_session_from_request(request)
    if not session or session.get("role") != "customer":
        raise HTTPException(status_code=401, detail="Please login first")

    conn = get_db_connection()
    c = conn.cursor()
    # Match bookings by linked account (user_id) OR by matching email, as a safety
    # net so historical bookings made before the account existed (or under a guest
    # checkout) still show up here instead of appearing "lost".
    c.execute("SELECT email FROM Users WHERE user_id=?", (session['user_id'],))
    urow = c.fetchone()
    account_email = (urow['email'] or '').strip().lower() if urow else ''

    c.execute(
        '''SELECT DISTINCT b.booking_id, p.package_name, p.destination, b.booking_date, b.travel_date, b.total_amount, b.status 
           FROM Bookings b 
           JOIN Customers c ON b.customer_id = c.customer_id 
           JOIN Packages p ON b.package_id = p.package_id
           WHERE c.user_id = ? OR LOWER(TRIM(c.email)) = ?
           ORDER BY b.booking_date DESC''',
        (session['user_id'], account_email)
    )
    bookings = [dict(row) for row in c.fetchall()]
    conn.close()
    return {"success": True, "data": bookings}

@app.get("/api/bookings")
async def get_all_bookings(request: Request):
    require_admin(request)
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''SELECT b.booking_id, COALESCE(c.name, 'Guest') as name, p.package_name, p.destination, b.booking_date, b.total_amount, b.status 
                 FROM Bookings b 
                 LEFT JOIN Customers c ON b.customer_id = c.customer_id 
                 JOIN Packages p ON b.package_id = p.package_id
                 ORDER BY b.booking_date DESC LIMIT 50''')
    bookings = [dict(row) for row in c.fetchall()]
    conn.close()
    return {"success": True, "data": bookings}

@app.get("/api/admin/ai/training")
async def get_ai_training_info(request: Request):
    require_admin(request)
    metadata = get_forecast_model_metadata()
    return {"success": True, "data": metadata}

@app.post("/api/admin/ai/retrain")
async def retrain_ai_models(request: Request):
    session = require_admin(request)
    train_demand_forecasting()
    perform_customer_segmentation()
    run_anomaly_detection()
    generate_recommendations()
    record_admin_audit(session, "Retrained AI models", "ai", None, "Demand forecasting, customer segmentation, anomaly detection and package recommendations")
    return {"success": True, "data": get_forecast_model_metadata()}

@app.post("/api/admin/ai/recommendations/generate")
async def generate_package_recommendations(request: Request):
    session = require_admin(request)
    try:
        generate_recommendations()
        record_admin_audit(session, "Generated AI recommendations", "ai", None, "Per-package weather/news/demand recommendations")
        return {"success": True, "message": "Recommendations generated."}
    except Exception as e:
        return {"success": False, "error": str(e)}


FEEDBACK_POSITIVE_WORDS = [
    "great", "excellent", "good", "amazing", "fantastic", "seamless",
    "loved", "beautiful", "perfect", "friendly", "helpful", "recommend",
    "professional", "wonderful", "unforgettable", "incredible", "outstanding",
    "brilliant", "smooth", "fun", "safe", "worth", "relaxing", "unique",
    "magic", "delicious", "luxury", "impressive", "knowledgeable", "patient"
]
FEEDBACK_NEGATIVE_WORDS = [
    "bad", "terrible", "awful", "poor", "delayed", "steep", "issue",
    "complaint", "expensive", "late", "disappointing", "worst", "unhappy",
    "slow", "smaller", "repetitive", "pity", "underwhelming", "intense",
    "niggle", "crowded", "noisy", "dirty", "rude", "broke", "broken",
    "missed", "cold", "boring", "confusing", "overpriced"
]
SERVICE_WORDS = [
    "guide", "staff", "driver", "service", "helpful", "friendly",
    "professional", "transfer", "timely", "reception", "check"
]
PRICE_WORDS = ["price", "expensive", "worth", "value", "cost", "rand", "overpriced", "cheap"]
FOOD_WORDS = ["food", "breakfast", "lunch", "dinner", "buffet", "restaurant", "delicious", "meal"]
ACCOM_WORDS = ["hotel", "room", "resort", "lodge", "riad", "bungalow", "villa", "accommodation"]
DEST_WORDS = [
    "hike", "beach", "safari", "tour", "monument", "museum", "temple",
    "waterfall", "mountain", "sunset", "island", "snorkel", "dive", "cruise"
]


def _generate_customer_feedback_summary(cursor, limit: int = 10) -> str:
    """Summarise the most recent `limit` reviews into 3-4 plain-English lines.

    The summary reflects actual themes seen in the latest reviews (positive
    vs. negative sentiment, mentions of service quality, food, price,
    accommodation and destination activities) so it changes as new reviews
    arrive.
    """
    cursor.execute("""
        SELECT reviewer_name, review_text, rating, sentiment_score, review_date
        FROM Reviews
        WHERE source='google'
        ORDER BY
            CASE WHEN review_date IS NOT NULL THEN 0 ELSE 1 END,
            review_date DESC,
            review_id DESC
        LIMIT ?
    """, (limit,))
    rows = cursor.fetchall()

    if not rows:
        return (
            "No customer reviews have been collected yet. "
            "After the first real review or Google Reviews sync, this card "
            "will show a rolling short summary of recent feedback, including "
            "sentiment and the top themes travellers are talking about."
        )

    total = len(rows)
    total_ratings = sum(int(r["rating"] or 0) for r in rows)
    avg_rating = round(total_ratings / total, 1) if total else 0.0

    pos_cnt = 0
    neg_cnt = 0
    neu_cnt = 0
    service_mentions = 0
    price_mentions = 0
    food_mentions = 0
    accom_mentions = 0
    dest_mentions = 0
    combined_text = ""

    for r in rows:
        text = (r["review_text"] or "").lower()
        combined_text += " " + text
        sent = float(r["sentiment_score"] or 0.0)
        if sent > 0.1:
            pos_cnt += 1
        elif sent < -0.1:
            neg_cnt += 1
        else:
            neu_cnt += 1
        if any(w in text for w in SERVICE_WORDS):
            service_mentions += 1
        if any(w in text for w in PRICE_WORDS):
            price_mentions += 1
        if any(w in text for w in FOOD_WORDS):
            food_mentions += 1
        if any(w in text for w in ACCOM_WORDS):
            accom_mentions += 1
        if any(w in text for w in DEST_WORDS):
            dest_mentions += 1

    raw_pos = sum(1 for w in FEEDBACK_POSITIVE_WORDS if w in combined_text)
    raw_neg = sum(1 for w in FEEDBACK_NEGATIVE_WORDS if w in combined_text)
    if raw_pos + raw_neg > 0:
        keyword_score = (raw_pos - raw_neg) / (raw_pos + raw_neg)
    else:
        keyword_score = 0.0

    if avg_rating >= 4.5:
        tone = "extremely positive"
    elif avg_rating >= 4.0:
        tone = "largely positive"
    elif avg_rating >= 3.2:
        tone = "generally positive with minor criticisms"
    elif avg_rating >= 2.5:
        tone = "mixed"
    else:
        tone = "concerning and needs attention"

    sentiment_line = (
        f"Across the last {total} reviews the average rating is {avg_rating}/5 "
        f"and the overall tone is {tone}."
    )
    sentiment_breakdown_parts = []
    if pos_cnt:
        sentiment_breakdown_parts.append(f"{pos_cnt} positive")
    if neu_cnt:
        sentiment_breakdown_parts.append(f"{neu_cnt} neutral")
    if neg_cnt:
        sentiment_breakdown_parts.append(f"{neg_cnt} negative")
    if sentiment_breakdown_parts:
        sentiment_line += " (" + ", ".join(sentiment_breakdown_parts) + ")."
    else:
        sentiment_line += "."

    theme_scores = sorted([
        ("service and staff", service_mentions),
        ("accommodation", accom_mentions),
        ("food and dining", food_mentions),
        ("tour and activity experiences", dest_mentions),
        ("value for money", price_mentions),
    ], key=lambda x: -x[1])

    top_themes = [(n, c) for n, c in theme_scores if c > 0]
    if top_themes:
        lead_name, lead_count = top_themes[0]
        themes_line = f"The most discussed topic is {lead_name} (raised in {lead_count} of {total} reviews"
        if len(top_themes) > 1:
            next_name, next_count = top_themes[1]
            themes_line += f"), followed by {next_name} ({next_count})."
        else:
            themes_line += ")."
    else:
        themes_line = "Recent feedback does not cluster around any single topic yet."

    recency_line = ""
    latest = rows[0]
    latest_date = latest["review_date"] or "recently"
    latest_name = latest["reviewer_name"] or "A guest"
    latest_rating = int(latest["rating"] or 0)
    latest_comment = (latest["review_text"] or "").strip()
    if len(latest_comment) > 110:
        latest_comment = latest_comment[:109].rstrip() + "…"
    if latest_comment:
        recency_line = (
            f"The most recent feedback came from {latest_name} on {latest_date}, "
            f"who rated their trip {latest_rating}/5 and wrote: \"{latest_comment}\""
        )

    positive_line = ""
    if keyword_score >= 0.2:
        positive_line = (
            "On balance positive language clearly outweighs the negatives, "
            "with guests frequently highlighting the quality of guides, "
            "smooth logistics and once-in-a-lifetime moments."
        )
    elif keyword_score <= -0.15 or neg_cnt >= max(2, total // 3):
        positive_line = (
            "A few recurring complaints are visible in the latest batch, "
            "so operations teams should review the negative items before "
            "they trend into wider dissatisfaction."
        )
    else:
        positive_line = (
            "Sentiment is mixed - the strong praise is balanced by "
            "isolated minor gripes that are typical of a busy travel season."
        )

    lines = [sentiment_line, themes_line]
    if recency_line:
        lines.append(recency_line)
    lines.append(positive_line)
    return " ".join(lines)


@app.get("/api/admin/dashboard-stats")
async def get_dashboard_stats(request: Request):
    require_admin(request)
    conn = get_db_connection()
    c = conn.cursor()
    
    # 1. KPIs
    c.execute("SELECT COUNT(*) as total FROM Bookings WHERE status != 'cancelled'")
    total_bookings = c.fetchone()["total"] or 0
    
    c.execute("SELECT SUM(total_amount) as revenue FROM Bookings WHERE status != 'cancelled'")
    total_revenue = c.fetchone()["revenue"] or 0
    
    c.execute("SELECT COUNT(*) as total FROM Customers")
    total_customers = c.fetchone()["total"] or 0

    c.execute("SELECT COALESCE(SUM(number_of_travelers), 0) as total FROM Bookings WHERE status != 'cancelled'")
    total_travelers = int(c.fetchone()["total"] or 0)

    c.execute("SELECT COALESCE(AVG(rating), 0) as avg_rating, COUNT(*) as total FROM Reviews WHERE source='google'")
    review_stats = c.fetchone()
    review_average = round(float(review_stats["avg_rating"] or 0), 1)
    review_count = int(review_stats["total"] or 0)

    avg_order_value = total_revenue / total_bookings if total_bookings > 0 else 0
    
    # 2. Monthly Trends (last 6 months)
    c.execute('''SELECT strftime('%Y-%m', booking_date) as month, COUNT(*) as count, SUM(total_amount) as revenue
                 FROM Bookings 
                 WHERE booking_date >= date('now', '-6 months') AND status != 'cancelled'
                 GROUP BY month ORDER BY month ASC''')
    monthly_trends = [dict(row) for row in c.fetchall()]
    
    # 3. Top Destinations
    c.execute('''SELECT p.destination, COUNT(*) as count, SUM(b.total_amount) as revenue
                 FROM Bookings b
                 JOIN Packages p ON b.package_id = p.package_id
                 WHERE b.status != 'cancelled'
                 GROUP BY p.destination ORDER BY count DESC LIMIT 5''')
    destinations = [dict(row) for row in c.fetchall()]

    # 4. Top Packages
    c.execute('''SELECT p.package_name, COUNT(*) as count, SUM(b.total_amount) as revenue
                 FROM Bookings b
                 JOIN Packages p ON b.package_id = p.package_id
                 WHERE b.status != 'cancelled'
                 GROUP BY p.package_name ORDER BY count DESC LIMIT 5''')
    top_packages = [dict(row) for row in c.fetchall()]
    
    # 5. Recent Activity
    c.execute('''SELECT b.booking_id, COALESCE(c.name, 'Guest') as name, p.package_name,
                        b.number_of_travelers, b.total_amount, b.status, b.booking_date
                 FROM Bookings b
                 LEFT JOIN Customers c ON b.customer_id = c.customer_id
                 JOIN Packages p ON b.package_id = p.package_id
                 ORDER BY b.booking_id DESC LIMIT 10''')
    recent_bookings = [dict(row) for row in c.fetchall()]

    # 6. Customer Feedback rolling summary (last 10 reviews, 3-4 line text)
    feedback_summary = _generate_customer_feedback_summary(c, limit=10)
    
    conn.close()
    return {
        "success": True, 
        "data": {
            "kpis": {
                "total_bookings": total_bookings,
                "total_packages_booked": total_bookings,
                "total_travelers": total_travelers,
                "total_revenue": total_revenue,
                "total_users": total_customers,
                "avg_order_value": round(avg_order_value, 2),
            "review_count": review_count,
            "review_average": review_average
            },
            "monthly_trends": monthly_trends,
            "destinations": destinations,
            "top_packages": top_packages,
            "recent_bookings": recent_bookings,
            "feedback_summary": feedback_summary
        }
    }

# ============================================================
# ADMIN ACCOUNT MANAGEMENT
# ============================================================

def record_admin_audit(session: dict, action: str, entity_type: str = "system", entity_id: Optional[int] = None, details: str = ""):
    """Record who changed what so every administrator has a shared audit trail.

    Always stores ONLY the bare username of the performing administrator in
    actor_name — never the generic "System Administrator" label and never the
    full name in parentheses — so the activity view displays exactly the
    username that triggered each change.
    """
    conn = get_db_connection(); c = conn.cursor()
    c.execute("SELECT username FROM Users WHERE user_id=?", (session.get("user_id"),))
    actor = c.fetchone()
    actor_name = (actor["username"] if actor and actor["username"] else "Unknown admin")
    c.execute("INSERT INTO Admin_Audit_Log(actor_user_id,actor_name,action,entity_type,entity_id,details) VALUES(?,?,?,?,?,?)", (session.get("user_id"), actor_name, action, entity_type, entity_id, details))
    conn.commit(); conn.close()


@app.get("/api/admin/audit-log")
async def get_admin_audit_log(request: Request, limit: int = 50):
    require_admin(request)
    limit = max(1, min(int(limit), 200))
    conn = get_db_connection(); c = conn.cursor()
    c.execute("SELECT audit_id,actor_user_id,actor_name,action,entity_type,entity_id,details,created_at FROM Admin_Audit_Log ORDER BY audit_id DESC LIMIT ?", (limit,))
    rows = [dict(r) for r in c.fetchall()]; conn.close()
    return {"success": True, "data": rows}


@app.get("/api/admin/admins")
async def get_admins(request: Request):
    require_admin(request)
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
        SELECT user_id, username, email, full_name, account_status, created_at
        FROM Users
        WHERE is_admin=1
        ORDER BY created_at ASC, user_id ASC
    """)
    admins = [dict(r) for r in c.fetchall()]
    conn.close()
    return {"success": True, "data": admins}


@app.get("/api/admin/admins/check")
async def check_admin_credential_available(request: Request, username: str = "", email: str = ""):
    """Lightweight real-time check used while typing in the create-admin form.

    Returns {"available": True} for each validated field, or the specific
    user-facing error message otherwise. The endpoint is designed for
    per-field debounced lookups so the frontend can show errors *under*
    the respective input immediately.
    """
    require_admin(request)
    result = {"username": {"available": True, "message": ""}, "email": {"available": True, "message": ""}}
    conn = get_db_connection(); c = conn.cursor()
    try:
        if username:
            c.execute("SELECT 1 FROM Users WHERE is_admin=1 AND LOWER(username)=? LIMIT 1", (username.strip().lower(),))
            if c.fetchone():
                result["username"] = {"available": False, "message": "This username has already been taken"}
        if email:
            norm = normalize_email(email)
            c.execute("SELECT 1 FROM Users WHERE is_admin=1 AND LOWER(email)=? LIMIT 1", (norm,))
            if c.fetchone():
                result["email"] = {"available": False, "message": "An admin account with this email already exists"}
    finally:
        conn.close()
    return {"success": True, "data": result}

@app.post("/api/admin/admins")
async def create_admin(data: AdminCreateRequest, request: Request):
    session = require_admin(request)

    # Confirm the current admin's password before creating another admin
    if not verify_admin_password(session["user_id"], data.confirm_password or ""):
        raise HTTPException(status_code=403, detail="Invalid administrator password. Action rejected.")

    username = data.username.strip()
    email = normalize_email(data.email)
    full_name = data.full_name.strip() or username

    if len(username) < 3:
        raise HTTPException(status_code=400, detail="Username must be at least 3 characters.")
    if not is_valid_email(email):
        raise HTTPException(status_code=400, detail="Enter a valid email address.")

    conn = get_db_connection()
    c = conn.cursor()

    c.execute(
        "SELECT user_id FROM Users WHERE is_admin=1 AND LOWER(username)=? LIMIT 1",
        (username.lower(),)
    )
    if c.fetchone():
        conn.close()
        raise HTTPException(status_code=409, detail="This username has already been taken")

    c.execute(
        "SELECT user_id FROM Users WHERE is_admin=1 AND LOWER(email)=? LIMIT 1",
        (email,)
    )
    if c.fetchone():
        conn.close()
        raise HTTPException(status_code=409, detail="An admin account with this email already exists")

    # System generates a strong temporary password (always includes a special character)
    temporary_password = generate_temporary_password(16)
    password_hash_to_store = generate_password_hash(temporary_password)
    must_change = 1   # force the new admin to change password on first login

    try:
        c.execute(
            """INSERT INTO Users
               (username, password_hash, role, user_type, full_name, email,
                account_status, is_admin, must_change_password)
               VALUES (?, ?, 'admin', 'admin', ?, ?, 'active', 1, ?)""",
            (username, password_hash_to_store, full_name, email, must_change)
        )
        admin_id = c.lastrowid
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        conn.close()
        raise HTTPException(status_code=409, detail="Username or email is already registered.")
    conn.close()

    # Email the temporary password only to the new admin (never returned in the API response)
    email_sent = False
    try:
        email_sent = send_bootstrap_admin_email(email, username, temporary_password)
    except Exception:
        logger.exception("Failed to send new administrator credentials to %s", email)

    record_admin_audit(
        session,
        "Created administrator",
        "admin",
        admin_id,
        f"Created {username} ({email}); system temporary password emailed={email_sent}"
    )

    return {
        "success": True,
        "data": {
            "user_id": admin_id,
            "message": (
                "Administrator created successfully. A system-generated temporary password "
                "was emailed to the new administrator. They will be required to change it on first login."
            ),
            "email_sent": email_sent,
        },
    }

@app.patch("/api/admin/admins/{user_id}/status")
async def update_admin_status(user_id: int, data: AdminStatusRequest, request: Request):
    session = require_admin(request)

    # Requirement 2: Verify confirmation password before executing the action
    if not verify_admin_password(session["user_id"], data.confirm_password or ""):
        raise HTTPException(status_code=403, detail="Invalid administrator password. Action rejected.")

    status = (data.account_status or "").strip().lower()
    if status not in {"active", "disabled"}:
        raise HTTPException(status_code=400, detail="Status must be active or disabled.")

    if user_id == session["user_id"] and status == "disabled":
        raise HTTPException(status_code=400, detail="You cannot disable your own administrator account.")

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT user_id FROM Users WHERE user_id=? AND is_admin=1", (user_id,))
    if not c.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="Administrator not found.")

    c.execute("SELECT username, email FROM Users WHERE user_id=?", (user_id,))
    target = c.fetchone()
    c.execute("UPDATE Users SET account_status=? WHERE user_id=?", (status, user_id))
    if status == "disabled":
        c.execute("DELETE FROM Sessions WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()
    action_label = "Disabled administrator" if status == "disabled" else "Reactivated administrator"
    record_admin_audit(session, action_label, "admin", user_id, f"{target['username']} ({target['email']})")
    return {"success": True, "data": {"message": f"Administrator {status}."}}


@app.delete("/api/admin/admins/{user_id}")
async def delete_admin(user_id: int, data: AdminDeleteRequest, request: Request):
    session = require_admin(request)

    # Requirement 2: Verify confirmation password before executing the action
    if not verify_admin_password(session["user_id"], data.confirm_password or ""):
        raise HTTPException(status_code=403, detail="Invalid administrator password. Action rejected.")

    if user_id == session["user_id"]:
        raise HTTPException(status_code=400, detail="You cannot delete your own administrator account.")

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT user_id, username, email FROM Users WHERE user_id=? AND is_admin=1", (user_id,))
    target = c.fetchone()
    if not target:
        conn.close()
        raise HTTPException(status_code=404, detail="Administrator not found.")

    target_username = target["username"]
    target_email = target["email"]

    try:
        c.execute("DELETE FROM Sessions WHERE user_id=?", (user_id,))
        c.execute("DELETE FROM Admin_Audit_Log WHERE actor_user_id=?", (user_id,))
        c.execute("DELETE FROM Users WHERE user_id=?", (user_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise HTTPException(status_code=500, detail="Failed to delete administrator.")
    conn.close()

    record_admin_audit(session, "Deleted administrator", "admin", None, f"Deleted {target_username} ({target_email})")
    return {"success": True, "data": {"message": "Administrator deleted permanently."}}


# ============================================================
# AI ANALYSIS API
# ============================================================

@app.get("/api/admin/ai-analysis")
async def get_ai_analysis(
    request: Request,
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
):
    require_admin(request)

    today = datetime.date.today()
    try:
        start = datetime.date.fromisoformat(start_date) if start_date else today - datetime.timedelta(days=180)
        end = datetime.date.fromisoformat(end_date) if end_date else today
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date filter. Use YYYY-MM-DD.")
    if start > end:
        raise HTTPException(status_code=400, detail="Start date cannot be after end date.")
    if end > today:
        raise HTTPException(status_code=400, detail="End date cannot be later than today.")

    start_str, end_str = start.isoformat(), end.isoformat()
    conn = get_db_connection()
    c = conn.cursor()

    # KPIs scoped to the selected date range.
    c.execute("SELECT COUNT(*) as total FROM Bookings WHERE status != 'cancelled' AND booking_date BETWEEN ? AND ?", (start_str, end_str))
    total_bookings = int(c.fetchone()["total"] or 0)
    c.execute("SELECT COALESCE(SUM(total_amount),0) FROM Bookings WHERE status != 'cancelled' AND booking_date BETWEEN ? AND ?", (start_str, end_str))
    total_revenue = float(c.fetchone()[0] or 0)
    c.execute("SELECT COUNT(*) FROM Customers WHERE date(created_at) BETWEEN ? AND ?", (start_str, end_str))
    total_customers = int(c.fetchone()[0] or 0)
    c.execute("SELECT COUNT(*) FROM Users WHERE role='customer' AND date(created_at) BETWEEN ? AND ?", (start_str, end_str))
    new_users_30 = int(c.fetchone()[0] or 0)
    c.execute("SELECT COALESCE(SUM(number_of_travelers),0) FROM Bookings WHERE status != 'cancelled' AND booking_date BETWEEN ? AND ?", (start_str, end_str))
    total_travelers = int(c.fetchone()[0] or 0)
    avg_order_value = total_revenue / total_bookings if total_bookings > 0 else 0

    # Reviews + sentiment — mirror the Reviews page exactly:
    # use the real aggregate rating/count from Review_Summary (google),
    # but compute sentiment + written-review counts from the stored rows
    # WITHOUT scoping by review_date. This matches what the admin sees on
    # the /admin/reviews.html page, not a narrow date-slice of reviews.
    c.execute("SELECT COUNT(*) as cnt, AVG(rating) as avg_rating FROM Reviews WHERE source='google'")
    rv_stored = c.fetchone()
    stored_count = int(rv_stored["cnt"] or 0)
    stored_avg = float(rv_stored["avg_rating"] or 0.0)
    c.execute("SELECT average_rating, total_reviews FROM Review_Summary WHERE source='google'")
    rs = c.fetchone()
    if rs and int(rs["total_reviews"] or 0) > 0:
        google_count = int(rs["total_reviews"])
        google_avg = round(float(rs["average_rating"] or stored_avg), 1)
    else:
        google_count = stored_count
        google_avg = round(stored_avg, 1)
    total_reviews = google_count
    avg_rating = google_avg

    c.execute("""SELECT
        SUM(CASE WHEN sentiment_score > 0.1 THEN 1 ELSE 0 END) as positive,
        SUM(CASE WHEN sentiment_score BETWEEN -0.1 AND 0.1 THEN 1 ELSE 0 END) as neutral,
        SUM(CASE WHEN sentiment_score < -0.1 THEN 1 ELSE 0 END) as negative
        FROM Reviews
        WHERE source='google'""")
    sent = c.fetchone()
    sentiment = {
        "positive": int(sent["positive"] or 0),
        "neutral": int(sent["neutral"] or 0),
        "negative": int(sent["negative"] or 0),
    }

    # Top destinations and packages for the selected range.
    c.execute("""SELECT p.destination, COUNT(*) as bookings, COALESCE(SUM(b.total_amount),0) as revenue
        FROM Bookings b JOIN Packages p ON b.package_id = p.package_id
        WHERE b.status != 'cancelled' AND b.booking_date BETWEEN ? AND ?
        GROUP BY p.destination ORDER BY revenue DESC LIMIT 5""", (start_str, end_str))
    destinations = [dict(r) for r in c.fetchall()]

    c.execute("""SELECT p.package_name, COUNT(*) as bookings, COALESCE(SUM(b.total_amount),0) as revenue
        FROM Bookings b JOIN Packages p ON b.package_id = p.package_id
        WHERE b.status != 'cancelled' AND b.booking_date BETWEEN ? AND ?
        GROUP BY p.package_name ORDER BY revenue DESC LIMIT 5""", (start_str, end_str))
    packages = [dict(r) for r in c.fetchall()]

    c.execute("""SELECT
        SUM(CASE WHEN COALESCE(available_spots,0) > 0 THEN 1 ELSE 0 END) as available,
        SUM(CASE WHEN COALESCE(available_spots,0) <= 0 THEN 1 ELSE 0 END) as sold_out
        FROM Packages""")
    inv = c.fetchone()
    inventory = {"available": int(inv["available"] or 0), "sold_out": int(inv["sold_out"] or 0)}

    c.execute("""SELECT strftime('%Y-%m', booking_date) as month, COUNT(*) as count, SUM(total_amount) as revenue
        FROM Bookings
        WHERE booking_date BETWEEN ? AND ? AND status != 'cancelled'
        GROUP BY month ORDER BY month ASC""", (start_str, end_str))
    monthly = [dict(r) for r in c.fetchall()]

    c.execute("""SELECT alert_type, description, severity, status, detected_at
        FROM Alerts WHERE date(detected_at) BETWEEN ? AND ?
        ORDER BY detected_at DESC LIMIT 5""", (start_str, end_str))
    alerts = [dict(r) for r in c.fetchall()]

    # Forecast is generated separately; expose the latest forecast as context.
    c.execute("SELECT forecast_date, period_start, period_end, predicted_demand, confidence FROM Forecasts ORDER BY forecast_date DESC LIMIT 1")
    fc = c.fetchone()
    forecast = dict(fc) if fc else None

    # Customer segmentation: ALL customers grouped by preferences field (K-Means output)
    customer_segments = {"Budget Travelers": [], "Regular Travelers": [], "Luxury Seekers": [], "Unsegmented": []}
    c.execute("""SELECT c.customer_id, c.name, c.email, c.phone,
                        COALESCE(c.preferences, 'Unsegmented') as segment,
                        COUNT(b.booking_id) as booking_count,
                        COALESCE(SUM(b.total_amount), 0) as total_spend
                 FROM Customers c
                 LEFT JOIN Bookings b ON c.customer_id = b.customer_id AND b.status != 'cancelled'
                 GROUP BY c.customer_id, c.name, c.email, c.phone, c.preferences
                 ORDER BY c.name ASC""")
    all_customers = [dict(r) for r in c.fetchall()]
    for cust in all_customers:
        seg = cust["segment"]
        if seg in customer_segments:
            customer_segments[seg].append(cust)
        else:
            customer_segments["Unsegmented"].append(cust)

    # Latest anomaly z-score for plain-English description
    c.execute("SELECT log_date, prediction_value, anomaly_flag FROM Analytics_Log ORDER BY log_date DESC LIMIT 1")
    latest_anomaly = c.fetchone()
    anomaly_z = float(latest_anomaly["prediction_value"]) if latest_anomaly and latest_anomaly["prediction_value"] is not None else 0.0
    if anomaly_z > 3:
        anomaly_description = "Today's bookings are much higher than usual"
    elif anomaly_z > 1.5:
        anomaly_description = "Today's bookings are slightly higher than usual"
    elif anomaly_z < -3:
        anomaly_description = "Today's bookings are much lower than usual"
    elif anomaly_z < -1.5:
        anomaly_description = "Today's bookings are slightly lower than usual"
    else:
        anomaly_description = "Today's bookings are within the normal range"

    conn.close()

    # Forecast reliability from training metadata R²
    training_meta = get_forecast_model_metadata()
    r2_value = 0.0
    if training_meta.get("trained"):
        r2_value = float(training_meta.get("r2_training_weekly", training_meta.get("r2_training", 0.0)))
    if r2_value > 0.7:
        forecast_reliability = {"level": "High", "label": "High", "r2": round(r2_value, 4)}
    elif r2_value >= 0.4:
        forecast_reliability = {"level": "Fair", "label": "Fair", "r2": round(r2_value, 4)}
    else:
        forecast_reliability = {"level": "Low", "label": "Low", "r2": round(r2_value, 4)}

    # ---- AI-Generated Insights ----
    insights = []
    recommendations = []

    if total_bookings > 0:
        insights.append(f"Booking volume is healthy at {total_bookings} confirmed bookings generating R{total_revenue:,.2f} in revenue.")
    else:
        insights.append("No confirmed bookings yet. Focus on marketing and package visibility to drive first sales.")

    if avg_order_value > 0:
        insights.append(f"Average order value is R{avg_order_value:,.2f}. Upselling premium packages and add-ons could lift this further.")
    else:
        insights.append("Average order value cannot be computed yet due to lack of booking data.")

    if total_reviews > 0:
        pos_pct = sentiment["positive"] / total_reviews * 100
        insights.append(f"Customer sentiment is {pos_pct:.1f}% positive across {total_reviews} reviews with an average rating of {avg_rating}/5.")
    else:
        insights.append("No reviews captured yet. Consider enabling the reviews sync to gather customer feedback.")

    if destinations:
        top_dest = destinations[0]
        insights.append(f"'{top_dest['destination']}' is the top-performing destination with {top_dest['bookings']} bookings and R{top_dest['revenue']:,.0f} revenue.")
    else:
        insights.append("No destination performance data available yet.")

    if inventory:
        if inventory["sold_out"] > 0:
            insights.append(f"{inventory['sold_out']} package(s) are currently sold out. Replenish inventory to capture demand.")
        else:
            insights.append("All packages currently have available inventory.")

    if forecast:
        insights.append(f"AI demand forecast predicts ~{forecast['predicted_demand']:.0f} future bookings over the next period (confidence {forecast['confidence'] or 'N/A'}).")

    # Recommendations
    if sentiment["negative"] > 0 and sentiment["negative"] / max(total_reviews, 1) > 0.1:
        recommendations.append("Negative sentiment is elevated. Prioritise responding to unfavourable reviews and improving service quality.")
    else:
        recommendations.append("Customer sentiment is stable. Continue delivering high-quality service and gather more reviews.")

    if inventory and inventory["sold_out"] > 0:
        recommendations.append("Replenish available spots for sold-out packages to avoid losing prospective customers.")

    if destinations and len(destinations) >= 2:
        concentration = destinations[0]["revenue"] / max(total_revenue, 1) * 100
        if concentration > 60:
            recommendations.append(f"Revenue is highly concentrated in '{destinations[0]['destination']}' ({concentration:.0f}%). Diversify the destination portfolio to reduce risk.")
    else:
        recommendations.append("Expand destination offerings to drive growth once data accumulates.")

    recommendations.append("Monitor real-time dashboard KPIs and AI alerts daily for proactive business decisions.")

    # Recommendations table data (will be populated by generate_recommendations)
    conn2 = get_db_connection(); c2 = conn2.cursor()
    c2.execute("""SELECT r.recommendation_id, r.package_id, r.date, r.recommendation_text,
                         r.discount_suggested, p.package_name, p.destination
                  FROM Recommendations r
                  LEFT JOIN Packages p ON r.package_id = p.package_id
                  WHERE r.date = date('now')
                  ORDER BY r.date DESC, r.discount_suggested DESC
                  LIMIT 20""")
    rec_table_rows = [dict(r) for r in c2.fetchall()]
    conn2.close()

    return {
        "success": True,
        "data": {
            "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "date_range": {"start": start_str, "end": end_str},
            "overview": {
                "total_bookings": total_bookings,
                "total_packages_booked": total_bookings,
                "total_travelers": total_travelers,
                "total_revenue": round(total_revenue, 2),
                "total_customers": total_customers,
                "new_users_30_days": new_users_30,
                "avg_order_value": round(avg_order_value, 2),
                "total_reviews": total_reviews,
                "average_rating": avg_rating,
            },
            "sentiment": sentiment,
            "destinations": destinations,
            "packages": packages,
            "inventory": inventory,
            "monthly_trends": monthly,
            "recent_alerts": alerts,
            "forecast": forecast,
            "customer_segments": customer_segments,
            "forecast_reliability": forecast_reliability,
            "anomaly_description": anomaly_description,
            "anomaly_z_raw": round(anomaly_z, 4),
            "insights": insights,
            "recommendations": recommendations,
            "package_recommendations": rec_table_rows,
            "how_it_works": {
                "summary": "The AI analysis reads only the selected date range, aggregates bookings, revenue, travellers, customers and reviews, then combines those results with the latest demand forecast and anomaly alerts.",
                "steps": [
                    "Filter bookings, customers and reviews by the selected start and end dates.",
                    "Calculate KPIs such as bookings, travellers, revenue, average order value and average rating.",
                    "Rank the best-performing destinations and packages using bookings and revenue.",
                    "Measure review sentiment as positive, neutral or negative from stored sentiment scores.",
                    "Use the forecasting model for future demand when enough booking history exists.",
                    "Use anomaly detection alerts to flag unusual booking patterns.",
                    "Generate plain-language insights and recommendations from the calculated results."
                ]
            }
        }
    }

# ============================================================
# REPORTS API (FIXED — SQL now has proper JOINs)
# ============================================================

@app.post("/api/reports/generate")
async def generate_report(request: Request, data: ReportRequest):
    require_admin(request)
    try:
        start_date = datetime.date.fromisoformat(data.start_date)
        end_date = datetime.date.fromisoformat(data.end_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD.")
    if start_date > end_date:
        raise HTTPException(status_code=400, detail="Start date cannot be after end date.")
    today = datetime.date.today()
    if end_date > today:
        raise HTTPException(status_code=400, detail="End date cannot be later than today.")

    start_str = start_date.isoformat()
    end_str = end_date.isoformat()

    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        """SELECT COUNT(*) as total
           FROM Bookings
           WHERE status != 'cancelled' AND booking_date BETWEEN ? AND ?""",
        (start_str, end_str),
    )
    total_bookings = int(c.fetchone()["total"] or 0)
    c.execute(
        """SELECT COALESCE(SUM(total_amount), 0) as revenue
           FROM Bookings
           WHERE status != 'cancelled' AND booking_date BETWEEN ? AND ?""",
        (start_str, end_str),
    )
    total_revenue = float(c.fetchone()["revenue"] or 0)

    c.execute(
        """SELECT p.destination, COUNT(*) as bookings, COALESCE(SUM(b.total_amount),0) as revenue
           FROM Bookings b
           JOIN Packages p ON b.package_id = p.package_id
           WHERE b.status != 'cancelled' AND b.booking_date BETWEEN ? AND ?
           GROUP BY p.destination
           ORDER BY revenue DESC
           LIMIT 5""",
        (start_str, end_str),
    )
    top_destinations = [dict(r) for r in c.fetchall()]

    c.execute("SELECT COUNT(*) as total FROM Users WHERE role='customer'")
    total_users = int(c.fetchone()["total"] or 0)
    c.execute(
        """SELECT COUNT(*) as total
           FROM Users
           WHERE role='customer' AND created_at >= date('now', '-30 days')""",
    )
    new_users_30_days = int(c.fetchone()["total"] or 0)

    c.execute(
        """SELECT payment_method, COUNT(*) as count
           FROM Bookings
           WHERE status != 'cancelled' AND booking_date BETWEEN ? AND ?
           GROUP BY payment_method
           ORDER BY count DESC""",
        (start_str, end_str),
    )
    payment_split = [dict(r) for r in c.fetchall()]

    c.execute(
        """SELECT COUNT(*) as cnt, AVG(rating) as avg_rating
           FROM Reviews
           WHERE source='google' AND review_date BETWEEN ? AND ?""",
        (start_str, end_str),
    )
    rv = c.fetchone()
    total_reviews = int(rv["cnt"] or 0)
    avg_rating = round(float(rv["avg_rating"] or 0), 1)

    c.execute(
        """SELECT
               SUM(CASE WHEN sentiment_score > 0.1 THEN 1 ELSE 0 END) as positive,
               SUM(CASE WHEN sentiment_score BETWEEN -0.1 AND 0.1 THEN 1 ELSE 0 END) as neutral,
               SUM(CASE WHEN sentiment_score < -0.1 THEN 1 ELSE 0 END) as negative
           FROM Reviews
           WHERE source='google' AND review_date BETWEEN ? AND ?""",
        (start_str, end_str),
    )
    sentiment = c.fetchone()

    c.execute(
        """SELECT alert_type, description, severity, status, detected_at
           FROM Alerts
           WHERE date(detected_at) BETWEEN ? AND ?
           ORDER BY detected_at DESC
           LIMIT 5""",
        (start_str, end_str),
    )
    recent_alerts = [dict(r) for r in c.fetchall()]

    c.execute(
        """SELECT forecast_date, period_start, period_end, predicted_demand, confidence
           FROM Forecasts
           ORDER BY forecast_date DESC
           LIMIT 1"""
    )
    latest_forecast = c.fetchone()
    conn.close()

    if total_bookings == 0 and total_reviews == 0 and not recent_alerts and not latest_forecast:
        raise HTTPException(status_code=400, detail="No data available for selected date range")

    report = {
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "period": f"{start_str} to {end_str}",
        "bookings": {
            "total_bookings": total_bookings,
            "total_revenue": f"R {total_revenue:,.2f}",
            "top_destinations": top_destinations,
            "payment_split": payment_split,
        },
        "users": {
            "total_users": total_users,
            "new_users_30_days": new_users_30_days,
        },
        "reviews": {
            "total_reviews": total_reviews,
            "average_rating": avg_rating,
            "sentiment": {
                "positive": int(sentiment["positive"] or 0),
                "neutral": int(sentiment["neutral"] or 0),
                "negative": int(sentiment["negative"] or 0),
            },
        },
        "forecast": dict(latest_forecast) if latest_forecast else None,
        "recent_alerts": recent_alerts,
        "insights": [
            "Destination revenue concentration should guide campaign allocation and inventory planning.",
            "Payment-method distribution can inform checkout UX improvements and conversion optimization.",
            "User-growth and segmentation indicators reveal opportunities for personalized package targeting.",
            "Recent alerts should be monitored daily for proactive service recovery.",
        ],
    }

    # Save report generation metadata
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "INSERT INTO Reports (start_date, end_date, total_bookings, total_revenue) VALUES (?, ?, ?, ?)",
        (start_str, end_str, total_bookings, total_revenue)
    )
    conn.commit()
    conn.close()

    return {"success": True, "report": report}

@app.get("/api/reports/last-generated")
async def get_last_generated_report(request: Request):
    require_admin(request)
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT created_at FROM Reports ORDER BY created_at DESC LIMIT 1")
    row = c.fetchone()
    conn.close()
    if row:
        raw_ts = row["created_at"]
        try:
            # Handle standard SQLite timestamp format (YYYY-MM-DD HH:MM:SS)
            # and convert to a more human-readable "Date at Time" format
            dt = datetime.datetime.strptime(raw_ts, "%Y-%m-%d %H:%M:%S")
            formatted = dt.strftime("%B %d, %Y at %H:%M")
        except:
            try:
                # Fallback for ISO format
                dt = datetime.datetime.fromisoformat(raw_ts.replace('Z', '+00:00'))
                formatted = dt.strftime("%B %d, %Y at %H:%M")
            except:
                formatted = raw_ts
        return {"success": True, "generated_at": formatted}
    return {"success": True, "generated_at": "Never"}


# ============================================================
# BOOKING DATASET CSV EXPORT (Kaggle-style format)
# ============================================================

@app.get("/api/dataset/export-bookings.csv")
async def export_bookings_csv(request: Request,
                               start_date: Optional[str] = None,
                               end_date: Optional[str] = None):
    require_admin(request)
    conn = get_db_connection()
    c = conn.cursor()

    where_clauses = ["b.status != 'cancelled'"]
    params: list = []
    if start_date:
        where_clauses.append("b.booking_date >= ?")
        params.append(start_date)
    if end_date:
        where_clauses.append("b.booking_date <= ?")
        params.append(end_date)
    where_sql = " AND ".join(where_clauses)

    c.execute(f"""
        SELECT
            b.booking_id,
            c.customer_id,
            COALESCE(c.name, 'Guest')                           AS customer_name,
            COALESCE(c.email, '')                                AS customer_email,
            COALESCE(u.contact_number, '')                       AS customer_phone,
            p.package_id,
            p.package_name,
            p.destination,
            COALESCE(p.season_category, 'standard')              AS region,
            b.booking_date,
            b.travel_date,
            b.number_of_travelers,
            p.price                                              AS price_per_person,
            COALESCE(p.discount_percentage, 0)                   AS discount_percentage,
            b.total_amount,
            ROUND(
                CAST(b.total_amount AS REAL)
                / (CASE WHEN b.number_of_travelers > 0 THEN b.number_of_travelers ELSE 1 END)
                * (1.0 - COALESCE(p.discount_percentage, 0) / 100.0),
                2
            )                                                    AS final_amount_per_traveler,
            COALESCE(b.payment_method, 'unknown')                AS payment_method,
            b.status
        FROM Bookings b
        LEFT JOIN Customers c ON b.customer_id = c.customer_id
        LEFT JOIN Users     u ON c.user_id    = u.user_id
        JOIN      Packages  p ON b.package_id = p.package_id
        WHERE {where_sql}
        ORDER BY b.booking_id ASC
    """, params)
    rows = c.fetchall()
    conn.close()

    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
    writer.writerow([
        "Booking_ID",
        "Customer_ID",
        "Customer_Name",
        "Customer_Email",
        "Customer_Phone",
        "Package_ID",
        "Package_Name",
        "Destination",
        "Region",
        "Booking_Date",
        "Travel_Date",
        "Number_of_Travelers",
        "Price_Per_Person",
        "Discount_Percentage",
        "Total_Amount",
        "Final_Amount_Per_Traveler",
        "Payment_Method",
        "Booking_Status",
    ])
    for r in rows:
        writer.writerow([
            r["booking_id"],
            r["customer_id"],
            r["customer_name"],
            r["customer_email"],
            r["customer_phone"],
            r["package_id"],
            r["package_name"],
            r["destination"],
            r["region"],
            r["booking_date"],
            r["travel_date"],
            r["number_of_travelers"],
            r["price_per_person"],
            r["discount_percentage"],
            r["total_amount"],
            r["final_amount_per_traveler"],
            r["payment_method"],
            r["status"],
        ])

    csv_bytes = buf.getvalue().encode("utf-8-sig")
    filename_suffix = ""
    if start_date or end_date:
        filename_suffix = f"_{start_date or 'all'}_to_{end_date or 'all'}"
    filename = f"travelintel_bookings_dataset{filename_suffix}.csv"

    return Response(
        content=csv_bytes,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename=\"{filename}\"",
        },
    )


# ============================================================
# REVIEWS, DESTINATIONS, AND USERS ANALYTICS
# ============================================================

def _trigger_background_review_sync() -> None:
    """Fire-and-forget SerpApi review sync in a daemon thread.

    The reviews page must remain fast: it returns the cached reviews
    immediately, then this spawns a short-lived background worker that
    fetches new reviews from Google and merges them into the cache. If the
    API call fails for any reason (no key, network error, quota), the
    existing cached reviews are left untouched and the thread simply exits.

    The sync is throttled to at most once per SYNC_COOLDOWN_MINUTES so that
    frequent page polling (e.g. every 5 seconds) does not burn through the
    SerpApi free-tier quota. Every poll still returns the cached reviews
    instantly; only the actual API call is rate-limited.
    """
    import threading
    global _last_review_sync_at
    cooldown_minutes = 60
    now = datetime.datetime.utcnow()
    if _last_review_sync_at and (now - _last_review_sync_at).total_seconds() < cooldown_minutes * 60:
        return
    _last_review_sync_at = now

    def _worker():
        try:
            merged = fetch_reviews()
            logger.info("Background review sync merged %s new review(s).", merged)
        except Exception:
            logger.exception("Background review sync failed; cached reviews preserved.")

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()


@app.get("/api/reviews")
async def get_reviews_dashboard(request: Request):
    require_admin(request)
    conn = get_db_connection()
    c = conn.cursor()

    c.execute("SELECT COUNT(*) as cnt, AVG(rating) as avg_rating FROM Reviews WHERE source='google'")
    overview = c.fetchone()

    c.execute(
        """SELECT review_id, reviewer_name, review_text, rating, sentiment_score, review_date
           FROM Reviews
           WHERE source='google'
           ORDER BY review_date DESC, review_id DESC"""
    )
    rows = c.fetchall()
    reviews = [dict(row) for row in rows]
    print(f"DEBUG: Fetched {len(reviews)} reviews from database.")

    c.execute(
        """SELECT
               SUM(CASE WHEN sentiment_score > 0.1 THEN 1 ELSE 0 END) as positive,
               SUM(CASE WHEN sentiment_score BETWEEN -0.1 AND 0.1 THEN 1 ELSE 0 END) as neutral,
               SUM(CASE WHEN sentiment_score < -0.1 THEN 1 ELSE 0 END) as negative
           FROM Reviews
           WHERE source='google'"""
    )
    sentiment = c.fetchone()
    
    # The written reviews list reflects the most recent batch pulled from
    # Google (via SerpApi), but the true aggregate rating and total review
    # count ARE the real, complete figures from Google - pull those from
    # Review_Summary rather than counting stored rows.
    c.execute("SELECT average_rating, total_reviews FROM Review_Summary WHERE source='google'")
    summary_row = c.fetchone()
    if summary_row and summary_row["total_reviews"]:
        google_rating = summary_row["average_rating"] or (overview["avg_rating"] or 4.6)
        google_count = summary_row["total_reviews"]
    else:
        google_count = len(reviews)
        google_rating = overview["avg_rating"] or 4.6
    
    # Try to get synced_at from Review_Summary
    c.execute("SELECT updated_at FROM Review_Summary WHERE source='google'")
    row = c.fetchone()
    if row:
        # Convert DB timestamp to pretty format
        raw_ts = row["updated_at"]
        try:
            # Handle standard SQLite timestamp format (YYYY-MM-DD HH:MM:SS)
            dt = datetime.datetime.strptime(raw_ts, "%Y-%m-%d %H:%M:%S")
            formatted = dt.strftime("%Y-%m-%d %H:%M:%S")
        except:
            try:
                # Fallback for ISO format
                dt = datetime.datetime.fromisoformat(raw_ts.replace('Z', '+00:00'))
                formatted = dt.strftime("%Y-%m-%d %H:%M:%S")
            except:
                formatted = raw_ts
        synced_at = formatted
    else:
        synced_at = "Never"
    conn.close()

    # Serve cached reviews immediately for a fast page load, then kick off a
    # background sync that merges any new Google reviews into the cache. The
    # response is never blocked on the API call, and if the API fails the
    # cached reviews are preserved (see _trigger_background_review_sync).
    _trigger_background_review_sync()

    return {
        "success": True,
        "data": {
            "average_rating": round(float(google_rating), 1),
            "total_reviews": int(google_count),
            "reviews": reviews,
            "summary": {
                "positive": int(sentiment["positive"] or 0),
                "neutral": int(sentiment["neutral"] or 0),
                "negative": int(sentiment["negative"] or 0),
            },
            "source_synced_at": synced_at,
        },
    }


@app.post("/api/reviews/refresh")
async def refresh_reviews_api(request: Request):
    require_admin(request)
    try:
        count = fetch_reviews()
        if count == 0:
            return {
                "success": True,
                "message": "Sync ran, but no new data was retrieved from Google (check server logs / "
                            "SETUP_GOOGLE_REVIEWS.md - most likely the API key or Place ID isn't configured "
                            "yet). Existing reviews were left unchanged.",
                "data": {"inserted": 0},
            }
        return {"success": True, "message": f"Successfully synced {count} real review(s) from Google.", "data": {"inserted": count}}
    except Exception as e:
        logger.error(f"Manual reviews refresh failed: {e}")
        return {"success": False, "error": str(e)}


# ------------------------------------------------------------------
# Manual review management (free, no API key required).
# Lets an admin type in the business's real Google reviews by hand,
# copying from the actual Google Reviews page. Fully free, no billing
# account, no scraping, no third-party service required.
# ------------------------------------------------------------------

@app.post("/api/reviews")
async def add_review(data: ReviewRequest, request: Request):
    require_admin(request)
    if data.rating < 1 or data.rating > 5:
        raise HTTPException(status_code=400, detail="Rating must be between 1 and 5.")
    if not data.review_text.strip():
        raise HTTPException(status_code=400, detail="Review text is required.")

    review_date = data.review_date or datetime.date.today().isoformat()
    sentiment = calculate_sentiment(data.review_text)

    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        """INSERT INTO Reviews (source, reviewer_name, review_text, rating, sentiment_score, review_date)
           VALUES ('google', ?, ?, ?, ?, ?)""",
        (data.reviewer_name.strip() or "Google Reviewer", data.review_text.strip(), data.rating, sentiment, review_date),
    )
    new_id = c.lastrowid
    conn.commit()
    conn.close()
    return {"success": True, "message": "Review added.", "data": {"review_id": new_id}}


@app.put("/api/reviews/{review_id}")
async def update_review(review_id: int, data: ReviewRequest, request: Request):
    require_admin(request)
    if data.rating < 1 or data.rating > 5:
        raise HTTPException(status_code=400, detail="Rating must be between 1 and 5.")
    if not data.review_text.strip():
        raise HTTPException(status_code=400, detail="Review text is required.")

    review_date = data.review_date or datetime.date.today().isoformat()
    sentiment = calculate_sentiment(data.review_text)

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT review_id FROM Reviews WHERE review_id=?", (review_id,))
    if not c.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="Review not found.")

    c.execute(
        """UPDATE Reviews SET reviewer_name=?, review_text=?, rating=?, sentiment_score=?, review_date=?
           WHERE review_id=?""",
        (data.reviewer_name.strip() or "Google Reviewer", data.review_text.strip(), data.rating, sentiment, review_date, review_id),
    )
    conn.commit()
    conn.close()
    return {"success": True, "message": "Review updated."}


@app.delete("/api/reviews/{review_id}")
async def delete_review(review_id: int, request: Request):
    require_admin(request)
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT review_id FROM Reviews WHERE review_id=?", (review_id,))
    if not c.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="Review not found.")
    c.execute("DELETE FROM Reviews WHERE review_id=?", (review_id,))
    conn.commit()
    conn.close()
    return {"success": True, "message": "Review deleted."}


@app.post("/api/reviews/summary")
async def update_review_summary(data: ReviewSummaryRequest, request: Request):
    """Manually set the overall star rating and total review count shown at
    the top of the admin Reviews page, to match the real numbers on Google
    (e.g. '4.6 (139)') without needing an API key."""
    require_admin(request)
    if data.average_rating < 0 or data.average_rating > 5:
        raise HTTPException(status_code=400, detail="Average rating must be between 0 and 5.")
    if data.total_reviews < 0:
        raise HTTPException(status_code=400, detail="Total reviews cannot be negative.")
    upsert_review_summary("google", data.average_rating, data.total_reviews)
    return {"success": True, "message": "Overall rating updated."}


@app.get("/api/destinations")
async def get_destination_analytics(request: Request):
    require_admin(request)
    conn = get_db_connection()
    c = conn.cursor()

    c.execute("SELECT COUNT(DISTINCT destination) as cnt FROM Packages")
    total_destinations = int(c.fetchone()["cnt"] or 0)

    c.execute(
        """SELECT p.destination,
                  COUNT(b.booking_id) as bookings,
                  COALESCE(SUM(b.total_amount), 0) as revenue
           FROM Packages p
           LEFT JOIN Bookings b ON b.package_id = p.package_id AND b.status != 'cancelled'
           GROUP BY p.destination
           ORDER BY revenue DESC, bookings DESC"""
    )
    by_destination = [dict(row) for row in c.fetchall()]

    c.execute(
        """SELECT strftime('%Y-%m', b.booking_date) as month,
                  p.destination,
                  COUNT(*) as count
           FROM Bookings b
           JOIN Packages p ON b.package_id = p.package_id
           WHERE b.booking_date >= date('now', '-6 months')
           GROUP BY strftime('%Y-%m', b.booking_date), p.destination
           ORDER BY month ASC"""
    )
    trend_rows = [dict(row) for row in c.fetchall()]
    conn.close()

    top_destination = by_destination[0]["destination"] if by_destination else None
    return {
        "success": True,
        "data": {
            "total_destinations": total_destinations,
            "top_destination": top_destination,
            "destinations": by_destination,
            "trends": trend_rows,
        },
    }


@app.get("/api/users/analytics")
async def get_user_analytics(request: Request):
    require_admin(request)
    conn = get_db_connection()
    c = conn.cursor()

    c.execute("SELECT COUNT(*) as cnt FROM Users WHERE role='customer'")
    total_users = int(c.fetchone()["cnt"] or 0)

    c.execute("SELECT COUNT(*) as cnt FROM Users WHERE role='customer' AND created_at >= datetime('now', '-30 days')")
    new_users_30_days = int(c.fetchone()["cnt"] or 0)

    c.execute(
        """SELECT payment_method, COUNT(*) as count
           FROM Bookings
           WHERE status != 'cancelled'
           GROUP BY payment_method
           ORDER BY count DESC"""
    )
    payment_method_segmentation = [dict(row) for row in c.fetchall()]

    c.execute(
        """WITH customer_stats AS (
               SELECT c.customer_id, COUNT(b.booking_id) as booking_count
               FROM Customers c
               LEFT JOIN Bookings b ON b.customer_id = c.customer_id AND b.status != 'cancelled'
               GROUP BY c.customer_id
           )
           SELECT
             CASE
               WHEN booking_count <= 1 THEN 'One-time'
               WHEN booking_count BETWEEN 2 AND 3 THEN 'Repeat'
               ELSE 'Frequent'
             END as segment,
             COUNT(*) as count
           FROM customer_stats
           GROUP BY segment
           ORDER BY count DESC"""
    )
    frequency_segmentation = [dict(row) for row in c.fetchall()]

    c.execute(
        """SELECT
               CASE
                 WHEN number_of_travelers = 1 THEN 'Solo'
                 WHEN number_of_travelers = 2 THEN 'Couple'
                 WHEN number_of_travelers BETWEEN 3 AND 4 THEN 'Small Group'
                 ELSE 'Large Group'
               END as segment,
               COUNT(*) as count
           FROM Bookings
           WHERE status != 'cancelled'
           GROUP BY segment
           ORDER BY count DESC"""
    )
    traveler_segmentation = [dict(row) for row in c.fetchall()]

    c.execute(
        """SELECT c.customer_id, c.name, c.email, COALESCE(c.preferences, 'Unsegmented') as segment,
                  COUNT(b.booking_id) as bookings,
                  COALESCE(SUM(b.total_amount), 0) as spend,
                  MAX(b.booking_date) as last_booking_date
           FROM Customers c
           LEFT JOIN Bookings b ON b.customer_id = c.customer_id
           GROUP BY c.customer_id, c.name, c.email, c.preferences
           ORDER BY spend DESC, bookings DESC
           LIMIT 25"""
    )
    users = [dict(row) for row in c.fetchall()]

    c.execute(
        """SELECT strftime('%Y-%m', created_at) as month, COUNT(*) as count
           FROM Users
           WHERE role='customer' AND created_at >= date('now', '-6 months')
           GROUP BY strftime('%Y-%m', created_at)
           ORDER BY month ASC"""
    )
    signup_trends = [dict(row) for row in c.fetchall()]

    c.execute(
        """WITH customer_month AS (
               SELECT strftime('%Y-%m', b.booking_date) as month,
                      c.customer_id,
                      COUNT(b.booking_id) as booking_count
               FROM Customers c
               LEFT JOIN Bookings b ON b.customer_id = c.customer_id AND b.status != 'cancelled'
               WHERE b.booking_date >= date('now', '-6 months')
               GROUP BY strftime('%Y-%m', b.booking_date), c.customer_id
           )
           SELECT month,
                  SUM(CASE WHEN booking_count <= 1 THEN 1 ELSE 0 END) as one_time_count,
                  SUM(CASE WHEN booking_count BETWEEN 2 AND 3 THEN 1 ELSE 0 END) as repeat_count,
                  SUM(CASE WHEN booking_count >= 4 THEN 1 ELSE 0 END) as frequent_count
           FROM customer_month
           GROUP BY month
           ORDER BY month ASC"""
    )
    frequency_trends = [dict(row) for row in c.fetchall()]

    c.execute(
        """SELECT strftime('%Y-%m', booking_date) as month, payment_method, COUNT(*) as count
           FROM Bookings
           WHERE booking_date >= date('now', '-6 months') AND status != 'cancelled'
           GROUP BY strftime('%Y-%m', booking_date), payment_method
           ORDER BY month ASC"""
    )
    payment_method_trends = [dict(row) for row in c.fetchall()]
    conn.close()

    return {
        "success": True,
        "data": {
            "total_users": total_users,
            "new_users_30_days": new_users_30_days,
            "segmentation": {
                "payment_method": payment_method_segmentation,
                "booking_frequency": frequency_segmentation,
                "traveler_type": traveler_segmentation,
            },
            "users": users,
            "signup_trends": signup_trends,
            "segmentation_trends": {
                "booking_frequency": frequency_trends,
                "payment_method": payment_method_trends,
            },
        },
    }


# ============================================================
# PUBLIC PAGE CATCH-ALL (registered last so it never shadows APIs)
# Serves templates at clean URLs: /profile, /my-bookings, etc.
# ============================================================

@app.get("/{page}")
async def render_public_page(request: Request, page: str):
    """Serve top-level *.html templates at clean URLs (no .html in the bar).

    Registered last so static/API routes like /packages, /api/*, /dashboard
    always take priority.
    """
    if page in _RESERVED_PAGE_NAMES:
        raise HTTPException(status_code=404, detail="Page not found")
    if page == "auth":
        return RedirectResponse(url="/auth/login", status_code=302)
    try:
        return templates.TemplateResponse(request, f"{page}.html")
    except Exception:
        raise HTTPException(status_code=404, detail="Page not found")
