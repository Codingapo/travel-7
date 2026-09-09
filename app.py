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
from email.message import EmailMessage
from contextlib import asynccontextmanager
from typing import Optional
from email.utils import parseaddr

from fastapi import FastAPI, Request, HTTPException, Query, UploadFile, File
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.gzip import GZipMiddleware
from pydantic import BaseModel
from werkzeug.security import generate_password_hash, check_password_hash
from apscheduler.schedulers.background import BackgroundScheduler

from config import (
    SESSION_TIMEOUT_MINUTES, MAX_LOGIN_ATTEMPTS, LOGIN_LOCKOUT_MINUTES,
    RESEND_API_KEY, DEFAULT_ADMIN_PASSWORD, DEFAULT_ADMIN_EMAIL,
    DEFAULT_ADMIN_USERNAME,
)
from database import get_db_connection, init_db, backup_database, get_review_summary, upsert_review_summary
from ai_engine import train_demand_forecasting, perform_customer_segmentation, run_anomaly_detection, get_forecast_model_metadata
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
    confirm_password: str = ""

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
app = FastAPI(title="TravelIntel AI", lifespan=lifespan)
resend.api_key = RESEND_API_KEY or os.getenv("RESEND_API_KEY", "")
app.add_middleware(GZipMiddleware, minimum_size=1000)

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


def normalize_email(raw: str) -> str:
    return (raw or "").strip().lower()


def is_valid_email(raw: str) -> bool:
    parsed = parseaddr(raw)[1]
    return "@" in parsed and "." in parsed.split("@")[-1]

def is_valid_card_number(card_number: str) -> bool:
    """Validate a card number using the Luhn checksum."""
    digits = re.sub(r"\D", "", card_number or "")
    if not 12 <= len(digits) <= 19:
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
    return total % 10 == 0


def is_valid_card_expiry(value: str) -> bool:
    value = (value or "").strip()
    match = re.fullmatch(r"(0[1-9]|1[0-2])/(\d{2})", value)
    if not match:
        return False
    month, year = int(match.group(1)), 2000 + int(match.group(2))
    today = datetime.date.today()
    return (year, month) >= (today.year, today.month)

def validate_payment_details(payment_method: str, card_number: str = "", card_expiry: str = ""):
    if payment_method == "credit_card":
        if not is_valid_card_number(card_number):
            raise HTTPException(status_code=400, detail="Please enter a valid card number.")
        if not is_valid_card_expiry(card_expiry):
            raise HTTPException(status_code=400, detail="Please enter a valid, non-expired card expiry date (MM/YY).")
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
    """Send the one-time bootstrap administrator credentials."""
    if not resend.api_key:
        logger.warning("RESEND_API_KEY is not configured; bootstrap admin email was not sent to %s", email)
        return False
    resend.Emails.send({
        "from": "TravelIntel AI <reset@notify.moviewatchtv.fun>",
        "to": [email],
        "subject": "Your TravelIntel AI administrator account",
        "reply_to": "support@travelintel.ai",
        "html": f"""<h2>TravelIntel AI Administrator</h2>
        <p>Your administrator account has been created.</p>
        <p><b>Email:</b> {email}<br><b>Username:</b> {username}<br><b>Temporary password:</b> {temporary_password}</p>
        <p>Sign in at <b>/auth/login</b>. You will be required to change this temporary password immediately.</p>
        <p>If you did not expect this account, contact the system owner immediately.</p>"""
    })
    return True


def send_new_admin_shared_password_email(email: str, username: str) -> bool:
    """Notify a newly-added administrator that they share the universal admin password."""
    if not resend.api_key:
        logger.warning("RESEND_API_KEY is not configured; new-admin email was not sent to %s", email)
        return False
    resend.Emails.send({
        "from": "TravelIntel AI <reset@notify.moviewatchtv.fun>",
        "to": [email],
        "subject": "Your TravelIntel AI administrator account is ready",
        "reply_to": "support@travelintel.ai",
        "html": f"""<h2>TravelIntel AI Administrator Access</h2>
        <p>Your administrator account has been created and is linked to the shared platform password.</p>
        <p><b>Email:</b> {email}<br><b>Username:</b> {username}</p>
        <p>Use the current universal TravelIntel administrator password to sign in at <b>/auth/login</b>.
        If you do not already know it, contact any active administrator or the system owner.</p>
        <p>All administrators share one password. When any administrator changes the password, every account is updated automatically.</p>
        <p>If you did not expect this account, contact the system owner immediately.</p>"""
    })
    return True


def send_verification_email(email, otp):
    if not resend.api_key:
        logger.warning("RESEND_API_KEY is not configured; verification email was not sent to %s", email)
        raise RuntimeError("Email service is not configured. Please contact support.")
    try:
        resend.Emails.send({
            "from": "TravelIntel AI <verify@notify.moviewatchtv.fun>",
            "to": [email],
            "subject": "Verify your TravelIntel AI account",
            "reply_to": "support@travelintel.ai",
            "headers": {
                "X-Entity-Ref-ID": os.urandom(8).hex(),
                "List-Unsubscribe": "<mailto:support@travelintel.ai?subject=unsubscribe>",
                "X-Priority": "1",
            },
            "html": f"""
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
        })
    except Exception:
        logger.exception("Failed to send verification email to %s", email)
        raise


def send_password_reset_email(email: str, otp: str):
    if not resend.api_key:
        logger.warning("RESEND_API_KEY is not configured; password reset email was not sent to %s", email)
        raise RuntimeError("Email service is not configured. Please contact support.")
    try:
        resend.Emails.send({
            "from": "TravelIntel AI <reset@notify.moviewatchtv.fun>",
            "to": [email],
            "subject": "Your TravelIntel AI password reset code",
            "reply_to": "support@travelintel.ai",
            "headers": {
                "X-Entity-Ref-ID": os.urandom(8).hex(),
                "List-Unsubscribe": "<mailto:support@travelintel.ai?subject=unsubscribe>",
                "X-Priority": "1",
            },
            "html": f"""
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
        })
    except Exception:
        logger.exception("Failed to send password reset email to %s", email)
        raise
    
def send_booking_email(to_email: str, booking: dict) -> bool:
    """
    Send a premium booking confirmation email using Resend.

    Returns:
        True  -> Email successfully submitted to Resend
        False -> Email could not be sent
    """

    try:
        booking_id = booking["booking_id"]

        # --------------------------------------------------
        # Booking details
        # --------------------------------------------------

        reference = f"TI-{str(booking_id).zfill(5)}"

        customer_name = booking.get("name", "Valued Traveller")
        customer_email = booking.get("email", to_email)
        phone = booking.get("phone", "Not provided")

        package_name = booking.get(
            "package_name",
            "Travel Package"
        )

        destination = booking.get(
            "destination",
            "Destination"
        )

        duration = booking.get(
            "duration",
            "Not specified"
        )

        travel_date = booking.get(
            "travel_date",
            "Not specified"
        )

        travelers = booking.get(
            "number_of_travelers",
            1
        )

        total_amount = booking.get(
            "total_amount",
            0
        )

        booking_date = booking.get(
            "booking_date",
            "Not specified"
        )

        payment_method = booking.get(
            "payment_method",
            "Not specified"
        )

        subject = (
            f"✈️ Booking Confirmed — "
            f"{destination} | {reference}"
        )


        # --------------------------------------------------
        # Premium HTML Email
        # --------------------------------------------------

        html = f"""
        <!DOCTYPE html>

        <html lang="en">

        <head>

            <meta charset="UTF-8">

            <meta name="viewport"
                  content="width=device-width, initial-scale=1.0">

            <title>
                Booking Confirmation
            </title>

        </head>


        <body style="
            margin:0;
            padding:0;
            background-color:#f1f5f9;
            font-family:
                -apple-system,
                BlinkMacSystemFont,
                'Segoe UI',
                Roboto,
                Arial,
                sans-serif;
            color:#0f172a;
        ">


        <!-- Main Wrapper -->

        <table
            width="100%"
            cellpadding="0"
            cellspacing="0"
            border="0"
            style="
                background-color:#f1f5f9;
                padding:40px 15px;
            "
        >

        <tr>

        <td align="center">


        <!-- Email Container -->

        <table
            width="100%"
            cellpadding="0"
            cellspacing="0"
            border="0"
            style="
                max-width:620px;
                background:#ffffff;
                border-radius:24px;
                overflow:hidden;
                box-shadow:
                    0 20px 50px
                    rgba(15,23,42,0.10);
            "
        >


        <!-- Hero Header -->

        <tr>

        <td style="
            background:
                linear-gradient(
                    135deg,
                    #2563eb 0%,
                    #1d4ed8 50%,
                    #1e40af 100%
                );
            padding:42px 35px;
            text-align:center;
            color:#ffffff;
        ">

            <div style="
                font-size:42px;
                margin-bottom:12px;
            ">
                ✈️
            </div>

            <h1 style="
                margin:0;
                font-size:28px;
                line-height:1.3;
                font-weight:800;
                color:#ffffff;
            ">
                Your Trip Is Confirmed!
            </h1>

            <p style="
                margin:12px 0 0;
                font-size:16px;
                line-height:1.6;
                color:#dbeafe;
            ">
                Get ready for an unforgettable journey
                with TravelIntel AI.
            </p>

        </td>

        </tr>


        <!-- Confirmation Badge -->

        <tr>

        <td style="
            padding:30px 35px 10px;
            text-align:center;
        ">

            <div style="
                display:inline-block;
                background:#dcfce7;
                color:#166534;
                padding:10px 20px;
                border-radius:999px;
                font-size:14px;
                font-weight:700;
            ">
                ✓ BOOKING CONFIRMED
            </div>

            <p style="
                margin:15px 0 0;
                font-size:14px;
                color:#64748b;
            ">
                Booking Reference
            </p>

            <p style="
                margin:5px 0 0;
                font-size:24px;
                font-weight:800;
                letter-spacing:2px;
                color:#2563eb;
            ">
                {reference}
            </p>

        </td>

        </tr>


        <!-- Greeting -->

        <tr>

        <td style="
            padding:25px 35px 10px;
        ">

            <h2 style="
                margin:0 0 10px;
                font-size:22px;
                color:#0f172a;
            ">
                Hello {customer_name}! 👋
            </h2>

            <p style="
                margin:0;
                font-size:15px;
                line-height:1.7;
                color:#64748b;
            ">
                Thank you for choosing TravelIntel AI.
                Your booking has been successfully confirmed.
                Below you'll find everything you need for
                your upcoming adventure.
            </p>

        </td>

        </tr>


        <!-- Destination Highlight -->

        <tr>

        <td style="
            padding:25px 35px;
        ">

            <table
                width="100%"
                cellpadding="0"
                cellspacing="0"
                style="
                    background:#eff6ff;
                    border:1px solid #dbeafe;
                    border-radius:18px;
                "
            >

            <tr>

            <td style="
                padding:25px;
                text-align:center;
            ">

                <div style="
                    font-size:14px;
                    color:#64748b;
                    margin-bottom:8px;
                ">
                    YOUR DESTINATION
                </div>

                <div style="
                    font-size:28px;
                    font-weight:800;
                    color:#1d4ed8;
                ">
                    🌍 {destination}
                </div>

                <div style="
                    margin-top:8px;
                    font-size:15px;
                    color:#64748b;
                ">
                    {package_name}
                </div>

            </td>

            </tr>

            </table>

        </td>

        </tr>


        <!-- Booking Details -->

        <tr>

        <td style="
            padding:0 35px 25px;
        ">

            <h3 style="
                margin:0 0 15px;
                font-size:18px;
                color:#0f172a;
            ">
                🧳 Your Booking Details
            </h3>


            <table
                width="100%"
                cellpadding="0"
                cellspacing="0"
                style="
                    border:1px solid #e2e8f0;
                    border-radius:16px;
                    overflow:hidden;
                "
            >

            <tr style="
                background:#f8fafc;
            ">

                <td style="
                    padding:15px;
                    color:#64748b;
                    font-size:14px;
                ">
                    Travel Date
                </td>

                <td style="
                    padding:15px;
                    text-align:right;
                    font-weight:700;
                    font-size:14px;
                ">
                    📅 {travel_date}
                </td>

            </tr>


            <tr>

                <td style="
                    padding:15px;
                    color:#64748b;
                    font-size:14px;
                ">
                    Duration
                </td>

                <td style="
                    padding:15px;
                    text-align:right;
                    font-weight:700;
                    font-size:14px;
                ">
                    ⏱️ {duration}
                </td>

            </tr>


            <tr style="
                background:#f8fafc;
            ">

                <td style="
                    padding:15px;
                    color:#64748b;
                    font-size:14px;
                ">
                    Travellers
                </td>

                <td style="
                    padding:15px;
                    text-align:right;
                    font-weight:700;
                    font-size:14px;
                ">
                    👥 {travelers}
                </td>

            </tr>


            <tr>

                <td style="
                    padding:15px;
                    color:#64748b;
                    font-size:14px;
                ">
                    Payment Method
                </td>

                <td style="
                    padding:15px;
                    text-align:right;
                    font-weight:700;
                    font-size:14px;
                ">
                    💳 {payment_method}
                </td>

            </tr>


            <tr style="
                background:#f8fafc;
            ">

                <td style="
                    padding:15px;
                    color:#64748b;
                    font-size:14px;
                ">
                    Booking Date
                </td>

                <td style="
                    padding:15px;
                    text-align:right;
                    font-weight:700;
                    font-size:14px;
                ">
                    {booking_date}
                </td>

            </tr>

            </table>

        </td>

        </tr>


        <!-- Total -->

        <tr>

        <td style="
            padding:0 35px 30px;
        ">

            <table
                width="100%"
                cellpadding="0"
                cellspacing="0"
                style="
                    background:#0f172a;
                    border-radius:18px;
                "
            >

            <tr>

            <td style="
                padding:25px;
            ">

                <div style="
                    color:#94a3b8;
                    font-size:14px;
                ">
                    TOTAL BOOKING VALUE
                </div>

                <div style="
                    margin-top:6px;
                    color:#ffffff;
                    font-size:30px;
                    font-weight:800;
                ">
                    R {total_amount:,.2f}
                </div>

            </td>

            <td style="
                padding:25px;
                text-align:right;
                vertical-align:middle;
            ">

                <div style="
                    width:48px;
                    height:48px;
                    line-height:48px;
                    text-align:center;
                    border-radius:50%;
                    background:#2563eb;
                    color:#ffffff;
                    font-size:22px;
                ">
                    ✓
                </div>

            </td>

            </tr>

            </table>

        </td>

        </tr>


        <!-- Contact Information -->

        <tr>

        <td style="
            padding:0 35px 30px;
        ">

            <div style="
                background:#f8fafc;
                border-radius:16px;
                padding:20px;
            ">

                <h3 style="
                    margin:0 0 10px;
                    font-size:16px;
                ">
                    📩 Booking Contact
                </h3>

                <p style="
                    margin:5px 0;
                    font-size:14px;
                    color:#64748b;
                ">
                    Email: {customer_email}
                </p>

                <p style="
                    margin:5px 0;
                    font-size:14px;
                    color:#64748b;
                ">
                    Phone: {phone}
                </p>

            </div>

        </td>

        </tr>


        <!-- Next Steps -->

        <tr>

        <td style="
            padding:0 35px 30px;
        ">

            <h3 style="
                margin:0 0 12px;
                font-size:18px;
            ">
                ✨ What's Next?
            </h3>

            <p style="
                margin:0;
                font-size:14px;
                line-height:1.8;
                color:#64748b;
            ">
                Keep this email for your records and make sure
                your travel documents are ready before departure.
                Your booking reference
                <strong>{reference}</strong>
                may be required when contacting our support team.
            </p>

        </td>

        </tr>


        <!-- Footer -->

        <tr>

        <td style="
            background:#f8fafc;
            padding:30px 35px;
            text-align:center;
            border-top:1px solid #e2e8f0;
        ">

            <div style="
                font-size:18px;
                font-weight:800;
                color:#2563eb;
            ">
                TravelIntel AI
            </div>

            <p style="
                margin:8px 0;
                font-size:13px;
                color:#64748b;
            ">
                Smart travel. Better journeys.
            </p>

            <p style="
                margin:15px 0 0;
                font-size:12px;
                color:#94a3b8;
                line-height:1.6;
            ">
                This is an automated booking confirmation.
                Please do not reply directly to this email.
            </p>

        </td>

        </tr>


        </table>


        <!-- Copyright -->

        <p style="
            margin:25px 0 0;
            font-size:12px;
            color:#94a3b8;
            text-align:center;
        ">
            © {datetime.datetime.now().year}
            TravelIntel AI. All rights reserved.
        </p>


        </td>

        </tr>

        </table>

        </body>

        </html>
        """


        # --------------------------------------------------
        # Plain-text fallback
        # --------------------------------------------------

        text = f"""
TravelIntel AI — BOOKING CONFIRMED

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


        # --------------------------------------------------
        # Send using Resend
        # --------------------------------------------------

        response = resend.Emails.send({
            "from": "TravelIntel AI <bookings@notify.moviewatchtv.fun>",
            "to": [to_email],
            "subject": subject,
            "html": html,
            "text": text,
        })


        logger.info(
            "Booking confirmation email sent successfully. "
            "booking_id=%s email=%s resend_response=%s",
            booking_id,
            to_email,
            response
        )

        return True


    except Exception as e:

        logger.exception(
            "Failed to send booking confirmation email. "
            "booking_id=%s email=%s error=%s",
            booking.get("booking_id", "unknown"),
            to_email,
            str(e)
        )

        return False

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
    return templates.TemplateResponse(request, "auth.html")

@app.get("/auth/register")
async def auth_register_page(request: Request):
    return templates.TemplateResponse(request, "auth.html")

@app.get("/auth/change-password")
async def auth_change_password_page(request: Request):
    session = get_session_from_request(request)
    if not session:
        return RedirectResponse(url="/auth/login", status_code=302)
    return templates.TemplateResponse(request, "change-password.html")

@app.get("/admin/login")
async def admin_login_page(request: Request):
    return RedirectResponse(url="/auth/login", status_code=302)

@app.get("/admin/forgot-password")
async def admin_forgot_password_page(request: Request):
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
    c.execute("SELECT COUNT(*) AS total FROM Reviews")
    review_count = int(c.fetchone()["total"] or 0)
    c.execute("SELECT COALESCE(AVG(rating),0) AS avg_rating FROM Reviews")
    review_average = float(c.fetchone()["avg_rating"] or 0)
    c.execute("""SELECT b.booking_id, COALESCE(c.name, 'Guest') AS name, p.package_name,
                        b.number_of_travelers, b.total_amount, b.status, b.booking_date
                 FROM Bookings b
                 LEFT JOIN Customers c ON b.customer_id = c.customer_id
                 LEFT JOIN Packages p ON b.package_id = p.package_id
                 ORDER BY b.booking_id DESC LIMIT 10""")
    recent_bookings = [dict(row) for row in c.fetchall()]
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
    }
    return templates.TemplateResponse(request, "admin/dashboard.html", {"request": request, "initial_dashboard": initial_dashboard})

@app.get("/admin/{page}.html")
async def render_admin_page(request: Request, page: str):
    if page == "login":
        return templates.TemplateResponse(request, "admin/login.html")
    
    # Block removed interfaces
    if page in ["users", "destinations"]:
        raise HTTPException(status_code=404, detail="Page not found")
        
    session = get_session_from_request(request)
    if not session or not get_active_admin(session["user_id"]):
        return RedirectResponse(url="/admin/login", status_code=302)
    try:
        return templates.TemplateResponse(request, f"admin/{page}.html")
    except Exception:
        raise HTTPException(status_code=404, detail="Page not found")

@app.get("/{page}.html")
async def render_page(request: Request, page: str):
    if page == "auth":
        return RedirectResponse(url="/auth/login", status_code=302)
    try:
        return templates.TemplateResponse(request, f"{page}.html")
    except Exception:
        raise HTTPException(status_code=404, detail="Page not found")

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
        send_verification_email(
            email,
            otp
        )
    except Exception:
        registration_otps.pop(email, None)
        logger.exception("Failed to send registration verification email to %s", email)
        raise HTTPException(
            status_code=500,
            detail="Unable to send verification email. Please try again or contact support."
        )

    return {
        "success": True,
        "message": "Verification email sent"
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
        send_verification_email(
            email,
            otp
        )
    except Exception:
        logger.exception("Failed to resend registration verification email to %s", email)
        raise HTTPException(
            status_code=500,
            detail="Unable to resend verification code. Please try again or contact support."
        )

    return {
        "success": True,
        "message": "A new verification code has been sent"
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
        send_password_reset_email(account_email, otp)
        logger.info("Admin password reset OTP sent to %s", account_email)
        return {"success": True, "data": {"message": "Password reset code sent to the administrator email.", "email": account_email}}
    except Exception:
        password_reset_otps.pop(account_email, None)
        logger.exception("Failed to send admin password reset email to %s", account_email)
        raise HTTPException(status_code=500, detail="Unable to send password reset email. Please try again.")



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
    conn = get_db_connection(); c = conn.cursor()
    c.execute("SELECT * FROM Users WHERE user_id=?", (session["user_id"],))
    user_row = c.fetchone()
    user = dict(user_row) if user_row else None
    if not user or not check_password_hash(user["password_hash"], data.current_password):
        conn.close(); raise HTTPException(status_code=400, detail="Current password is incorrect.")
    username_val = user.get("username") or ""
    email_val = user.get("email") or ""
    email_local_val = email_val.split("@")[0] if "@" in email_val else email_val
    validate_password_strength(
        data.new_password,
        forbidden_substrings=[username_val, email_val, email_local_val]
    )
    new_hash = generate_password_hash(data.new_password)
    is_admin = int(user.get("is_admin") or 0) == 1 or user.get("role") == "admin" or user.get("user_type") == "admin"
    if is_admin:
        c.execute("UPDATE Users SET password_hash=?, must_change_password=0, password_changed_at=CURRENT_TIMESTAMP WHERE user_id=?", (new_hash, session["user_id"]))
        conn.commit()
        conn.close()
        admins_updated_count = sync_admin_password_to_all(new_hash, exclude_user_id=session["user_id"])
        total_admins_affected = admins_updated_count + 1
        record_admin_audit(
            session,
            "Changed universal admin password",
            "admin",
            None,
            f"Admin {user.get('username')} updated the shared platform password; applied to {total_admins_affected} administrator account(s)."
        )
    else:
        c.execute("UPDATE Users SET password_hash=?, must_change_password=0, password_changed_at=CURRENT_TIMESTAMP WHERE user_id=?", (new_hash, session["user_id"]))
        conn.commit(); conn.close()
    return {"success": True, "data": {"message": "Password changed successfully.", "redirect": "/dashboard" if session.get("role") == "admin" else "/home"}}


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
        send_password_reset_email(account_email, otp)
        is_admin_flag = int(user.get("is_admin") or 0) == 1 or user.get("role") == "admin" or user.get("user_type") == "admin"
        logger.info("Password reset OTP sent to %s (is_admin=%s)", account_email, is_admin_flag)
        return {"success": True, "data": {"message": "Password reset code sent to your email.", "email": account_email}}
    except Exception:
        password_reset_otps.pop(account_email, None)
        logger.exception("Failed to send password reset email to %s", account_email)
        raise HTTPException(status_code=500, detail="Unable to send password reset email. Please try again.")


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
        if is_admin_reset:
            c.execute(
                "UPDATE Users SET password_hash=?, must_change_password=0, password_changed_at=CURRENT_TIMESTAMP WHERE is_admin=1",
                (new_hash,),
            )
            rows_updated = c.rowcount
        else:
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

        # When an admin triggers a universal password reset via OTP (Forgot Password flow)
        # we also record the action under Admin Activity for full audit transparency.
        if is_admin_reset:
            c.execute("SELECT user_id, username FROM Users WHERE is_admin=1 AND LOWER(email)=? LIMIT 1", (email,))
            initiator_row = c.fetchone()
            initiator = dict(initiator_row) if initiator_row else None
            if initiator and initiator.get("user_id"):
                pseudo_session = {"user_id": initiator["user_id"]}
                audit_details = (
                    f"Password reset via OTP for email {email}; "
                    f"new password universally synced to {rows_updated} administrator account(s)."
                )
                record_admin_audit(
                    pseudo_session,
                    "Changed universal admin password",
                    "admin",
                    None,
                    audit_details,
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

    validate_payment_details(data.payment_method, data.card_number, data.card_expiry)

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


@app.post("/admin/logout")
async def admin_logout(request: Request):
    session_id = request.cookies.get("session_id")
    if session_id:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("DELETE FROM Sessions WHERE session_id = ?", (session_id,))
        conn.commit()
        conn.close()
    response = JSONResponse(content={"success": True})
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

    final_image = _resolve_package_image(data.image_url, data.image_file_ref)

    conn = get_db_connection(); c = conn.cursor()
    try:
        status = "Available" if data.available_spots > 0 else "Unavailable"
        c.execute("""INSERT INTO Packages
            (package_name,destination,price,duration,description,availability_status,season_category,image_url,available_spots,total_spots)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (name,destination,float(data.price),int(data.duration),data.description.strip(),status,data.season_category.strip(),final_image,int(data.available_spots),int(data.available_spots)))
        package_id = c.lastrowid
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise HTTPException(status_code=409, detail="Unable to create package. A package with these details may already exist.") from exc
    finally:
        conn.close()
    record_admin_audit(session, "Created package", "package", package_id, f"{name} | price={data.price} | seats={data.available_spots}")
    return {"success": True, "data": {"package_id": package_id, "message": "Package created successfully."}}


@app.put("/api/admin/packages/{package_id}")
async def update_package(package_id: int, data: PackageUpdateRequest, request: Request):
    session = require_admin(request)
    session = get_session_from_request(request) or {}
    if not verify_admin_password(session["user_id"], (data.confirm_password or "").strip()):
        raise HTTPException(status_code=403, detail="Current admin password is incorrect.")

    if data.price <= 0 or data.available_spots < 0 or data.duration < 1:
        raise HTTPException(status_code=400, detail="Price, duration and available seats must be valid.")
    conn = get_db_connection(); c = conn.cursor()
    c.execute("SELECT * FROM Packages WHERE package_id=?", (package_id,))
    before_row = c.fetchone()
    if not before_row:
        conn.close(); raise HTTPException(status_code=404, detail="Package not found")
    before = dict(before_row)
    final_image = _resolve_package_image(data.image_url, data.image_file_ref)
    status = "Available" if data.available_spots > 0 else "Unavailable"
    c.execute("""UPDATE Packages SET package_name=?,destination=?,price=?,duration=?,description=?,availability_status=?,season_category=?,image_url=?,available_spots=?,total_spots=? WHERE package_id=?""",
              (data.package_name.strip(),data.destination.strip(),float(data.price),int(data.duration),data.description.strip(),status,data.season_category.strip(),final_image,int(data.available_spots),int(data.available_spots),package_id))
    conn.commit(); conn.close()
    before_snippet = f"{before.get('package_name','')} | price={before.get('price','')} | seats={before.get('available_spots','')}"
    after_snippet = f"{data.package_name} | price={data.price} | seats={data.available_spots}"
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
    c.execute("SELECT package_id, package_name, destination, price, duration, description, availability_status, season_category, image_url, COALESCE(available_spots,0) as available_spots, COALESCE(total_spots,0) as total_spots FROM Packages ORDER BY package_id ASC")
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
    c.execute("SELECT * FROM Packages WHERE package_id=?", (package_id,))
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
    validate_payment_details(data.payment_method, data.card_number, data.card_expiry)

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
        c.execute("SELECT package_name, destination, duration, price, availability_status, COALESCE(available_spots,0) as available_spots FROM Packages WHERE package_id=?", (data.package_id,))
        pkg = c.fetchone()
        if not pkg:
            raise HTTPException(status_code=404, detail="Package not found")
        if pkg["availability_status"] != "Available":
            raise HTTPException(status_code=400, detail="This package is currently unavailable")
        if int(pkg["available_spots"] or 0) < data.number_of_travelers:
            raise HTTPException(status_code=400, detail="Not enough available spots for this package")

        total_amount = pkg['price'] * data.number_of_travelers

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
    record_admin_audit(session, "Retrained AI models", "ai", None, "Demand forecasting, customer segmentation and anomaly detection")
    return {"success": True, "data": get_forecast_model_metadata()}

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

    c.execute("SELECT COALESCE(AVG(rating), 0) as avg_rating, COUNT(*) as total FROM Reviews")
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
            "recent_bookings": recent_bookings
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

    # Requirement 2: Verify confirmation password before executing the action
    if not verify_admin_password(session["user_id"], data.confirm_password or ""):
        raise HTTPException(status_code=403, detail="Invalid administrator password. Action rejected.")

    username = data.username.strip()
    email = normalize_email(data.email)
    full_name = data.full_name.strip() or username
    if len(username) < 3:
        raise HTTPException(status_code=400, detail="Username must be at least 3 characters.")
    if not is_valid_email(email):
        raise HTTPException(status_code=400, detail="Enter a valid email address.")

    # Requirement 3: Unique admin credential validation with specific error messages
    conn = get_db_connection(); c = conn.cursor()
    c.execute("SELECT user_id, username, email FROM Users WHERE is_admin=1 AND LOWER(username)=? LIMIT 1", (username.lower(),))
    dup_username = c.fetchone()
    if dup_username:
        conn.close()
        raise HTTPException(status_code=409, detail="This username has already been taken")

    c.execute("SELECT user_id, username, email FROM Users WHERE is_admin=1 AND LOWER(email)=? LIMIT 1", (email,))
    dup_email = c.fetchone()
    if dup_email:
        conn.close()
        raise HTTPException(status_code=409, detail="An admin account with this email already exists")

    # Requirement 1: Assign the current shared admin password to new admins
    shared_hash = get_shared_admin_password_hash()
    if shared_hash:
        shared_password_for_email = None
        password_hash_to_store = shared_hash
        must_change = 0
    else:
        shared_password_for_email = DEFAULT_ADMIN_PASSWORD
        password_hash_to_store = generate_password_hash(DEFAULT_ADMIN_PASSWORD)
        must_change = 1

    try:
        c.execute("""INSERT INTO Users
            (username,password_hash,role,user_type,full_name,email,account_status,is_admin,must_change_password)
            VALUES (?,?,'admin','admin',?,?, 'active',1,?)""",
            (username, password_hash_to_store, full_name, email, must_change))
        admin_id = c.lastrowid
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback(); conn.close()
        raise HTTPException(status_code=409, detail="Username or email is already registered.")
    conn.close()

    email_sent = False
    try:
        if shared_password_for_email:
            email_sent = send_bootstrap_admin_email(email, username, shared_password_for_email)
        else:
            email_sent = send_new_admin_shared_password_email(email, username)
    except Exception:
        logger.exception("Failed to send new administrator credentials to %s", email)
    record_admin_audit(session, "Created administrator", "admin", admin_id, f"Created {username} ({email}); credentials emailed={email_sent}")
    return {
        "success": True,
        "data": {
            "user_id": admin_id,
            "message": (
                "Administrator created successfully. The universal administrator password "
                "was applied to the new account. Credentials were emailed to the new administrator."
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
        start = datetime.date.fromisoformat(start_date) if start_date else today - datetime.timedelta(days=30)
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

    # Reviews + sentiment
    c.execute("SELECT COUNT(*) as cnt, AVG(rating) as avg_rating FROM Reviews WHERE review_date BETWEEN ? AND ?", (start_str, end_str))
    rv = c.fetchone()
    total_reviews = int(rv["cnt"] or 0)
    avg_rating = round(float(rv["avg_rating"] or 0), 1)
    c.execute("""SELECT
        SUM(CASE WHEN sentiment_score > 0.1 THEN 1 ELSE 0 END) as positive,
        SUM(CASE WHEN sentiment_score BETWEEN -0.1 AND 0.1 THEN 1 ELSE 0 END) as neutral,
        SUM(CASE WHEN sentiment_score < -0.1 THEN 1 ELSE 0 END) as negative
        FROM Reviews
        WHERE review_date BETWEEN ? AND ?""", (start_str, end_str))
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
    conn.close()

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
            "insights": insights,
            "recommendations": recommendations,
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
           WHERE review_date BETWEEN ? AND ?""",
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
           WHERE review_date BETWEEN ? AND ?""",
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

    c.execute("SELECT COUNT(*) as cnt, AVG(rating) as avg_rating FROM Reviews")
    overview = c.fetchone()

    c.execute(
        """SELECT review_id, reviewer_name, review_text, rating, sentiment_score, review_date
           FROM Reviews
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
           FROM Reviews"""
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
