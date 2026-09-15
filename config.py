import os
import secrets

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.abspath(os.path.dirname(__file__)), ".env"))
except ImportError:
    pass

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, 'instance')
BACKUPS_DIR = os.path.join(BASE_DIR, 'backups')
OUTBOX_DIR = os.path.join(INSTANCE_DIR, 'outbox')

DB_PATH = os.path.join(INSTANCE_DIR, 'travelintel.db')

SECRET_KEY = os.environ.get('SECRET_KEY', 'default-travelintel-ai-super-secret-key-2026')

RESEND_API_KEY = os.environ.get('RESEND_API_KEY', '')
SERPAPI_KEY = os.environ.get('SERPAPI_KEY', '')
GOOGLE_PLACE_ID = os.environ.get('GOOGLE_PLACE_ID', '')

SMTP_HOST = os.environ.get('SMTP_HOST', '')
SMTP_PORT = int(os.environ.get('SMTP_PORT', '587'))
SMTP_USERNAME = os.environ.get('SMTP_USERNAME', '')
SMTP_PASSWORD = os.environ.get('SMTP_PASSWORD', '')
SMTP_USE_TLS = os.environ.get('SMTP_USE_TLS', 'true').lower() not in ('false', '0', 'no')
SMTP_FROM_EMAIL = os.environ.get('SMTP_FROM_EMAIL', '')

DEFAULT_ADMIN_EMAIL = os.environ.get('DEFAULT_ADMIN_EMAIL', 'skalahante@gmail.com')
DEFAULT_ADMIN_USERNAME = os.environ.get('DEFAULT_ADMIN_USERNAME', 'skalahante')
DEFAULT_ADMIN_PASSWORD = os.environ.get('DEFAULT_ADMIN_PASSWORD', 'TravelIntel#ChangeMe2026')

SESSION_TIMEOUT_MINUTES = 15
MAX_LOGIN_ATTEMPTS = 5
LOGIN_LOCKOUT_MINUTES = 15

os.makedirs(INSTANCE_DIR, exist_ok=True)
os.makedirs(BACKUPS_DIR, exist_ok=True)
os.makedirs(OUTBOX_DIR, exist_ok=True)
