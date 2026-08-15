# config.py
import os
import time
try:
    from dotenv import load_dotenv
except ImportError:  # Environment variables still work without optional .env loading.
    def load_dotenv():
        return False

load_dotenv()

class Config:
    # API Credentials
    API_ID = int(os.getenv("API_ID", 0))
    API_HASH = os.getenv("API_HASH", "")
    BOT_TOKEN = os.getenv("BOT_TOKEN", "")
    SESSION_STRING = os.getenv("SESSION_STRING", None)
    ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0") or 0)
    SESSION_ENCRYPTION_KEY = os.getenv("SESSION_ENCRYPTION_KEY", "")
    ENVIRONMENT = os.getenv("ENVIRONMENT", "development").lower()
    
    # Configuration - REDUCED CONCURRENCY
    MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", 4))
    BATCH_SIZE = int(os.getenv("BATCH_SIZE", 3))
    FLOOD_WAIT_DELAY = float(os.getenv("FLOOD_WAIT_DELAY", 2.0))
    MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", 2000000000))
    
    # Database
    DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///database/downloads.db")
    DATA_DIR = os.getenv("DATA_DIR", "database")
    
    # Forward chat
    FORWARD_CHAT_ID = os.getenv("FORWARD_CHAT_ID", None)
    
    # Bot start time
    BOT_START_TIME = time.time()
    
    # Logging
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

    @classmethod
    def validate(cls):
        errors = []
        if not cls.API_ID or not cls.API_HASH:
            errors.append("API_ID and API_HASH are required for the bot client")
        if not cls.BOT_TOKEN:
            errors.append("BOT_TOKEN is required")
        if not cls.ADMIN_USER_ID:
            errors.append("ADMIN_USER_ID must be set on the server")
        if cls.ENVIRONMENT == "production" and not cls.SESSION_ENCRYPTION_KEY:
            errors.append("SESSION_ENCRYPTION_KEY is required in production")
        return errors
