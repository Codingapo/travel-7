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

DB_PATH = os.path.join(INSTANCE_DIR, 'travelintel.db')

SECRET_KEY = os.environ.get('SECRET_KEY', 'default-travelintel-ai-super-secret-key-2026')

RESEND_API_KEY = os.environ.get('RESEND_API_KEY', '')
SERPAPI_KEY = os.environ.get('SERPAPI_KEY', '')
GOOGLE_PLACE_ID = os.environ.get('GOOGLE_PLACE_ID', '')

DEFAULT_ADMIN_EMAIL = os.environ.get('DEFAULT_ADMIN_EMAIL', 'skalahante@gmail.com')
DEFAULT_ADMIN_USERNAME = os.environ.get('DEFAULT_ADMIN_USERNAME', 'skalahante')
DEFAULT_ADMIN_PASSWORD = os.environ.get('DEFAULT_ADMIN_PASSWORD', 'TravelIntel#ChangeMe2026')

SESSION_TIMEOUT_MINUTES = 30
MAX_LOGIN_ATTEMPTS = 5
LOGIN_LOCKOUT_MINUTES = 15

os.makedirs(INSTANCE_DIR, exist_ok=True)
os.makedirs(BACKUPS_DIR, exist_ok=True)
