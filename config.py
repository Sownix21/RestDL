# config.py
import os
import time
try:
    from dotenv import load_dotenv
except ImportError:  # Environment variables still work without optional .env loading.
    def load_dotenv():
        return False

load_dotenv()


def _env_int(name, default):
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return int(default)


def _env_float(name, default):
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)

class Config:
    # API Credentials
    API_ID = _env_int("API_ID", 0)
    API_HASH = os.getenv("API_HASH", "")
    BOT_TOKEN = os.getenv("BOT_TOKEN", "")
    SESSION_STRING = os.getenv("SESSION_STRING", None)
    ADMIN_USER_ID = _env_int("ADMIN_USER_ID", 0)
    SESSION_ENCRYPTION_KEY = os.getenv("SESSION_ENCRYPTION_KEY", "")
    ENVIRONMENT = os.getenv("ENVIRONMENT", "development").lower()
    
    # Configuration - REDUCED CONCURRENCY
    MAX_CONCURRENT_DOWNLOADS = _env_int("MAX_CONCURRENT_DOWNLOADS", 4)
    BATCH_SIZE = _env_int("BATCH_SIZE", 3)
    FLOOD_WAIT_DELAY = _env_float("FLOOD_WAIT_DELAY", 2.0)
    MAX_FILE_SIZE = _env_int("MAX_FILE_SIZE", 2000000000)
    MAX_ACTIVE_USER_SESSIONS = _env_int("MAX_ACTIVE_USER_SESSIONS", 100)
    SESSION_IDLE_TIMEOUT = _env_int("SESSION_IDLE_TIMEOUT", 1800)
    AUTH_FLOW_TIMEOUT = _env_int("AUTH_FLOW_TIMEOUT", 900)
    AUTH_ATTEMPTS_PER_HOUR = _env_int("AUTH_ATTEMPTS_PER_HOUR", 6)
    MAX_PENDING_AUTH = _env_int("MAX_PENDING_AUTH", 50)
    MAX_USER_TASKS = _env_int("MAX_USER_TASKS", 3)
    
    # Database
    DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///database/downloads.db")
    DATA_DIR = os.getenv("DATA_DIR", "database")
    DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "downloads")
    LOG_DIR = os.getenv("LOG_DIR", "logs")
    
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
        if cls.MAX_CONCURRENT_DOWNLOADS < 1 or cls.MAX_CONCURRENT_DOWNLOADS > 32:
            errors.append("MAX_CONCURRENT_DOWNLOADS must be between 1 and 32")
        if cls.MAX_FILE_SIZE < 1:
            errors.append("MAX_FILE_SIZE must be positive")
        if cls.MAX_ACTIVE_USER_SESSIONS < 1:
            errors.append("MAX_ACTIVE_USER_SESSIONS must be positive")
        if cls.MAX_USER_TASKS < 1:
            errors.append("MAX_USER_TASKS must be positive")
        if cls.AUTH_FLOW_TIMEOUT < 60:
            errors.append("AUTH_FLOW_TIMEOUT must be at least 60 seconds")
        if cls.AUTH_ATTEMPTS_PER_HOUR < 1 or cls.MAX_PENDING_AUTH < 1:
            errors.append("authorization limits must be positive")
        return errors
