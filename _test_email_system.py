"""End-to-end test of the email delivery fallback chain.

Since Resend API key is invalid and SMTP isn't configured, every email
should land in instance/outbox/ — this verifies the fallback chain works
and that OTPs / confirmations are never lost.
"""
import os
import sys
import glob

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import OUTBOX_DIR

# Clean any previous test files in outbox that match our test email
for f in glob.glob(os.path.join(OUTBOX_DIR, "*test_user*")):
    try:
        os.remove(f)
    except OSError:
        pass

# Now import from app *after* cleaning (import triggers lifespan-free code)
from app import (
    send_verification_email,
    send_password_reset_email,
    send_booking_email,
    send_bootstrap_admin_email,
    send_new_admin_shared_password_email,
    _dispatch_email,
    OUTBOX_DIR as APP_OUTBOX_DIR,
)

print("=" * 60)
print("EMAIL SYSTEM TEST - Fallback chain validation")
print("OUTBOX_DIR =", OUTBOX_DIR)
print("=" * 60)

results = []

# 1) Verification OTP email
print("\n[1/5] send_verification_email(otp=123456) ->", end=" ")
ok = send_verification_email("test_user_verify@example.com", "123456")
results.append(("send_verification_email", ok))
print("OK" if ok else "FAIL")

# 2) Password reset OTP email
print("[2/5] send_password_reset_email(otp=654321) ->", end=" ")
ok = send_password_reset_email("test_user_reset@example.com", "654321")
results.append(("send_password_reset_email", ok))
print("OK" if ok else "FAIL")

# 3) Booking confirmation email
print("[3/5] send_booking_email(booking_id=99999) ->", end=" ")
test_booking = {
    "booking_id": 99999,
    "name": "Test User Booking",
    "email": "test_user_booking@example.com",
    "phone": "+27 000 000 000",
    "package_name": "Safaricom Test Package",
    "destination": "Cape Town",
    "duration": "5 Days",
    "travel_date": "2099-01-01",
    "number_of_travelers": 2,
    "total_amount": 12345,
    "booking_date": "2026-09-15",
    "payment_method": "credit_card",
}
ok = send_booking_email("test_user_booking@example.com", test_booking)
results.append(("send_booking_email", ok))
print("OK" if ok else "FAIL")

# 4) Bootstrap admin email
print("[4/5] send_bootstrap_admin_email() ->", end=" ")
ok = send_bootstrap_admin_email(
    "test_admin_bootstrap@example.com", "test_admin", "TempP@ss123!"
)
results.append(("send_bootstrap_admin_email", ok))
print("OK" if ok else "FAIL")

# 5) New-admin shared-password email
print("[5/5] send_new_admin_shared_password_email() ->", end=" ")
ok = send_new_admin_shared_password_email(
    "test_admin_new@example.com", "new_test_admin"
)
results.append(("send_new_admin_shared_password_email", ok))
print("OK" if ok else "FAIL")

# Summary
print("\n" + "=" * 60)
print("SUMMARY")
print("=" * 60)
all_ok = True
for name, ok in results:
    status = "PASS" if ok else "FAIL"
    all_ok = all_ok and ok
    print(f"  {status}  {name}")

# Check outbox for evidence files
saved = sorted(glob.glob(os.path.join(OUTBOX_DIR, "*test_user*")) +
               glob.glob(os.path.join(OUTBOX_DIR, "*test_admin*")))
print(f"\nEmails saved to outbox: {len(saved)} file(s)")
for s in saved:
    print(f"  - {os.path.basename(s)}")

# Quick peek inside the verify OTP file
verify_files = [s for s in saved if "verify_otp" in s]
if verify_files:
    with open(verify_files[0], "r", encoding="utf-8") as f:
        content = f.read()
    if "123456" in content:
        print("\n[OK] OTP code (123456) was found in the saved verify email file!")
    else:
        print("\n[FAIL] OTP code NOT found in saved verify email content!")
        all_ok = False

print("\n" + ("ALL TESTS PASSED [OK]" if all_ok else "SOME TESTS FAILED [FAIL]"))
sys.exit(0 if all_ok else 1)
