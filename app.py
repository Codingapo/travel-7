import resend
import random
import os
import datetime
import traceback
import smtplib
import logging
import secrets
from email.message import EmailMessage
from contextlib import asynccontextmanager
from typing import Optional
from email.utils import parseaddr

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.gzip import GZipMiddleware
from pydantic import BaseModel
from werkzeug.security import generate_password_hash, check_password_hash
from apscheduler.schedulers.background import BackgroundScheduler

from config import SESSION_TIMEOUT_MINUTES, MAX_LOGIN_ATTEMPTS, LOGIN_LOCKOUT_MINUTES
from database import get_db_connection, init_db, backup_database, get_review_summary
from ai_engine import train_demand_forecasting, perform_customer_segmentation, run_anomaly_detection
from reviews_scraper import fetch_reviews

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
    
    # Schedule DB Backups (Daily at 2:00 AM)
    scheduler.add_job(backup_database, 'cron', hour=2, minute=0)
    
    # Schedule AI Models (Daily at 2:00 AM)
    scheduler.add_job(train_demand_forecasting, 'cron', hour=2, minute=0)
    scheduler.add_job(perform_customer_segmentation, 'cron', hour=2, minute=0)
    
    # Schedule Anomaly Detection (Daily at 8:00 AM, 12:00 PM, 4:00 PM)
    scheduler.add_job(run_anomaly_detection, 'cron', hour=8, minute=0)
    scheduler.add_job(run_anomaly_detection, 'cron', hour=12, minute=0)
    scheduler.add_job(run_anomaly_detection, 'cron', hour=16, minute=0)
    
    # Schedule Reviews Scraper (Every 4 hours)
    scheduler.add_job(fetch_reviews, 'interval', hours=4)
    
    scheduler.start()
    
    yield
    
    scheduler.shutdown()

# --- App Init ---
app = FastAPI(title="TravelIntel AI", lifespan=lifespan)
resend.api_key = os.getenv("RESEND_API_KEY")
app.add_middleware(GZipMiddleware, minimum_size=1000)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

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
    if session.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Insufficient privileges")
    return session


def normalize_email(raw: str) -> str:
    return (raw or "").strip().lower()


def is_valid_email(raw: str) -> bool:
    parsed = parseaddr(raw)[1]
    return "@" in parsed and "." in parsed.split("@")[-1]

def send_verification_email(email, otp):

    resend.Emails.send({
        "from": "TravelIntel AI <verify@notify.moviewatchtv.fun>",
        "to": [email],
        "subject": "Verify your TravelIntel AI account",
        "html": f"""
        <h2>Welcome to TravelIntel AI</h2>

        <p>Your verification code is:</p>

        <h1>{otp}</h1>

        <p>This code expires in 10 minutes.</p>
        """
    })
def send_booking_email(to_email: str, booking: dict) -> bool:
    """Send booking confirmation via SMTP; fallback to local outbox file if SMTP is not configured."""
    smtp_host = os.environ.get("SMTP_HOST", "").strip()
    smtp_user = os.environ.get("SMTP_USERNAME", "").strip()
    smtp_pass = os.environ.get("SMTP_PASSWORD", "").strip()
    smtp_from = os.environ.get("SMTP_FROM", smtp_user or "noreply@travelintel.ai")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))

    subject = f"TravelIntel Booking Confirmation #{booking['booking_id']}"
    body = (
        f"Booking Confirmation\n\n"
        f"Reference: TI-{str(booking['booking_id']).zfill(5)}\n"
        f"Customer: {booking['name']}\n"
        f"Email: {booking['email']}\n"
        f"Phone: {booking['phone']}\n"
        f"Package: {booking['package_name']}\n"
        f"Destination: {booking['destination']}\n"
        f"Travel Date: {booking['travel_date']}\n"
        f"Travelers: {booking['number_of_travelers']}\n"
        f"Total: R {booking['total_amount']:,.2f}\n"
        f"Booking Date: {booking['booking_date']}\n\n"
        "Thank you for booking with TravelIntel AI."
    )

    if smtp_host and smtp_user and smtp_pass:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = smtp_from
        msg["To"] = to_email
        msg.set_content(body)
        with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        return True

    outbox_dir = os.path.join(BASE_DIR, "instance", "outbox")
    os.makedirs(outbox_dir, exist_ok=True)
    file_path = os.path.join(outbox_dir, f"booking_{booking['booking_id']}.txt")
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(f"TO: {to_email}\nSUBJECT: {subject}\n\n{body}\n")
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

@app.get("/admin/login")
async def admin_login_page(request: Request):
    return templates.TemplateResponse(request, "admin/login.html")

@app.get("/dashboard")
async def dashboard(request: Request):
    session = get_session_from_request(request)
    if not session or session.get("role") != "admin":
        return RedirectResponse(url="/admin/login", status_code=302)
    return templates.TemplateResponse(request, "admin/dashboard.html")

@app.get("/admin/{page}.html")
async def render_admin_page(request: Request, page: str):
    if page == "login":
        return templates.TemplateResponse(request, "admin/login.html")
    
    # Block removed interfaces
    if page in ["users", "destinations"]:
        raise HTTPException(status_code=404, detail="Page not found")
        
    session = get_session_from_request(request)
    if not session or session.get("role") != "admin":
        return RedirectResponse(url="/admin/login", status_code=302)
    try:
        return templates.TemplateResponse(request, f"admin/{page}.html")
    except Exception:
        raise HTTPException(status_code=404, detail="Page not found")

@app.get("/{page}.html")
async def render_page(request: Request, page: str):
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

    if len(data.password) < 6:
        raise HTTPException(
            status_code=400,
            detail="Password too short"
        )


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


    send_verification_email(
        email,
        otp
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
        (username,password_hash,role,full_name,email)
        VALUES (?,?,?,?,?)
        """,
        (
            username,
            generate_password_hash(record["password"]),
            "customer",
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
@app.post("/api/auth/login")
async def customer_login(data: CustomerLoginRequest):
    email = normalize_email(data.email)
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM Users WHERE email=?", (email,))
    user = c.fetchone()
    conn.close()

    if user and check_password_hash(user['password_hash'], data.password):
        session_id = create_session(user['user_id'], user['role'])
        
        # Explicitly fetch the customer record to see what's in the DB
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT name FROM Customers WHERE user_id = ?", (user['user_id'],))
        cust = c.fetchone()
        conn.close()
        
        display_name = user['full_name']
        if cust and cust['name']:
            display_name = cust['name']

        response = JSONResponse(content={
            "success": True,
            "token": session_id,
            "user": {
                "user_id": user['user_id'],
                "email": user['email'],
                "username": user['username'],
                "full_name": display_name
            }
        })
        response.set_cookie(key="session_id", value=session_id, httponly=True, secure=False, samesite="lax", path="/")
        return response

    raise HTTPException(status_code=401, detail="Invalid email or password")


@app.post("/api/auth/forgot-password/request")
async def request_password_reset(data: CustomerLoginRequest):
    identifier = normalize_email(data.email)
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT user_id, email FROM Users WHERE email=? OR username=?", (identifier, identifier))
    user = c.fetchone()
    conn.close()
    if not user:
        raise HTTPException(status_code=404, detail="Account not found")

    # Generate 3-digit OTP
    otp = f"{secrets.randbelow(1000):03d}"
    password_reset_otps[identifier] = {
        "otp": otp,
        "expires_at": datetime.datetime.now() + datetime.timedelta(minutes=10),
        "attempts": 0,
    }
    
    # In a real app, send email. For prototype, return it.
    logger.info("Generated 3-digit OTP %s for %s", otp, identifier)
    return {"success": True, "data": {"message": "OTP generated", "otp": otp}}


class PasswordResetVerifyRequest(BaseModel):
    email: str
    password: str
    otp: str


@app.post("/api/auth/forgot-password/verify")
async def verify_password_reset(data: PasswordResetVerifyRequest):
    identifier = normalize_email(data.email)
    record = password_reset_otps.get(identifier)
    if not record:
        raise HTTPException(status_code=400, detail="OTP not requested or expired")
    if datetime.datetime.now() > record["expires_at"]:
        del password_reset_otps[identifier]
        raise HTTPException(status_code=400, detail="OTP expired")
    if record["attempts"] >= 5:
        del password_reset_otps[identifier]
        raise HTTPException(status_code=429, detail="Too many attempts, request new OTP")
    if record["otp"] != (data.otp or "").strip():
        record["attempts"] += 1
        raise HTTPException(status_code=400, detail="Invalid OTP")
    if len(data.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE Users SET password_hash=? WHERE email=? OR username=?", (generate_password_hash(data.password), identifier, identifier))
    conn.commit()
    conn.close()
    del password_reset_otps[identifier]
    return {"success": True, "data": {"message": "Password reset successful"}}

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
    c.execute("SELECT * FROM Users WHERE username=?", (data.username,))
    user = c.fetchone()
    conn.close()

    if user and user["role"] == "admin" and check_password_hash(user['password_hash'], data.password):
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
async def get_packages():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT package_id, package_name, destination, price, duration, description, availability_status, season_category, image_url FROM Packages ORDER BY package_id ASC")
    packages = [dict(row) for row in c.fetchall()]
    conn.close()
    return {"success": True, "data": packages}


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
        c.execute("SELECT package_name, destination, duration, price, availability_status FROM Packages WHERE package_id=?", (data.package_id,))
        pkg = c.fetchone()
        if not pkg:
            raise HTTPException(status_code=404, detail="Package not found")
        if pkg["availability_status"] != "Available":
            raise HTTPException(status_code=400, detail="This package is currently unavailable")

        total_amount = pkg['price'] * data.number_of_travelers

        # 3. Create Booking
        today = datetime.date.today().isoformat()
        c.execute(
            '''INSERT INTO Bookings (customer_id, package_id, booking_date, travel_date, number_of_travelers, total_amount, status, payment_method)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
            (customer_id, data.package_id, today, travel_date_obj.isoformat(), data.number_of_travelers, total_amount, 'confirmed', data.payment_method or "unknown")
        )
        booking_id = c.lastrowid
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
    c.execute(
        '''SELECT b.booking_id, p.package_name, p.destination, b.booking_date, b.travel_date, b.total_amount, b.status 
           FROM Bookings b 
           JOIN Customers c ON b.customer_id = c.customer_id 
           JOIN Packages p ON b.package_id = p.package_id
           WHERE c.user_id = ?
           ORDER BY b.booking_date DESC''',
        (session['user_id'],)
    )
    bookings = [dict(row) for row in c.fetchall()]
    conn.close()
    return {"success": True, "data": bookings}

# ============================================================
# ADMIN DASHBOARD API (FIXED — require_admin now reads cookies)
# ============================================================

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
    c.execute('''SELECT b.booking_id, COALESCE(c.name, 'Guest') as name, p.package_name, b.total_amount, b.status, b.booking_date
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
                "total_revenue": total_revenue,
                "total_users": total_customers,
                "avg_order_value": round(avg_order_value, 2)
            },
            "monthly_trends": monthly_trends,
            "destinations": destinations,
            "top_packages": top_packages,
            "recent_bookings": recent_bookings
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
    
    # Force count from the actual reviews list to avoid inconsistencies
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
        # fetch_reviews handles full sync (including deletions) by clearing old 'google' source reviews first.
        count = fetch_reviews()
        return {"success": True, "message": f"Successfully synced {count} reviews from Google.", "data": {"inserted": count}}
    except Exception as e:
        logger.error(f"Manual reviews refresh failed: {e}")
        return {"success": False, "error": str(e)}


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
