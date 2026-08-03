import os
import secrets

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, 'instance')
BACKUPS_DIR = os.path.join(BASE_DIR, 'backups')

DB_PATH = os.path.join(INSTANCE_DIR, 'travelintel.db')

# Generate a strong secret key if it doesn't exist yet, or keep it persistent?
# In production, we'd load this from an env var. Here we'll hardcode one or generate one per process.
SECRET_KEY = os.environ.get('SECRET_KEY', 'default-travelintel-ai-super-secret-key-2026')

SESSION_TIMEOUT_MINUTES = 30
MAX_LOGIN_ATTEMPTS = 5
LOGIN_LOCKOUT_MINUTES = 15

os.makedirs(INSTANCE_DIR, exist_ok=True)
os.makedirs(BACKUPS_DIR, exist_ok=True)
