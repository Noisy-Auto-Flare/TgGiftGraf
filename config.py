import os
from dotenv import load_dotenv

load_dotenv()

API_ID = int(os.getenv("API_ID", 0))
API_HASH = os.getenv("API_HASH", "")
SESSION_NAME = os.getenv("SESSION_NAME", "gift_crawler")

START_USERNAMES = [u.strip() for u in os.getenv("START_USERNAMES", "").split(",") if u.strip()]
TARGET_CHATS = [u.strip() for u in os.getenv("TARGET_CHATS", "").split(",") if u.strip()]

DB_PATH = os.getenv("DB_PATH", "gifts.db")

CRAWL_DELAY_MIN = float(os.getenv("CRAWL_DELAY_MIN", 2))
CRAWL_DELAY_MAX = float(os.getenv("CRAWL_DELAY_MAX", 5))
CHAT_SCAN_INTERVAL = int(os.getenv("CHAT_SCAN_INTERVAL", 120))
RESCAN_THRESHOLD_DAYS = int(os.getenv("RESCAN_THRESHOLD_DAYS", 7))

ANALYTICS_INTERVAL_HOURS = int(os.getenv("ANALYTICS_INTERVAL_HOURS", 6))
MAX_CRAWL_QUEUE_SIZE = int(os.getenv("MAX_CRAWL_QUEUE_SIZE", 50000))

SCAN_SELF_DIALOGS = os.getenv("SCAN_SELF_DIALOGS", "true").lower() == "true"

CRAWL_SINGLE_RUN = os.getenv("CRAWL_SINGLE_RUN", "false").lower() == "true"

# Лимиты хранилища
MAX_AVATARS_SIZE_MB = int(os.getenv("MAX_AVATARS_SIZE_MB", 300))
AVATARS_DIR = "static/avatars"
