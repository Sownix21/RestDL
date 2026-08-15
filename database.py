# database.py
import os
import time
from sqlalchemy import create_engine, Column, Integer, BigInteger, String, DateTime, Boolean, Float, Text, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool
from datetime import datetime
from config import Config
from logger import get_logger

logger = get_logger(__name__)
Base = declarative_base()

class DownloadHistory(Base):
    __tablename__ = 'download_history'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, nullable=False)
    chat_id = Column(String, nullable=False)
    message_id = Column(Integer, nullable=False)
    file_name = Column(String)
    file_size = Column(Integer)
    media_type = Column(String)
    download_time = Column(DateTime, default=datetime.utcnow)
    success = Column(Boolean, default=True)
    error_message = Column(Text)
    url = Column(String)

class UserSettings(Base):
    __tablename__ = 'user_settings'
    
    user_id = Column(BigInteger, primary_key=True)
    auto_download = Column(Boolean, default=True)
    max_file_size = Column(Integer, default=2000000000)
    preferred_quality = Column(String, default="high")
    forward_chat_id = Column(String, nullable=True)
    forward_chat_title = Column(String, nullable=True)


class UserProfile(Base):
    __tablename__ = 'user_profiles'

    user_id = Column(BigInteger, primary_key=True)
    language = Column(String(2), nullable=False, default="en")
    onboarding_complete = Column(Boolean, nullable=False, default=False)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class TelegramCredential(Base):
    __tablename__ = 'telegram_credentials'

    user_id = Column(BigInteger, primary_key=True)
    api_id_encrypted = Column(Text, nullable=False)
    api_hash_encrypted = Column(Text, nullable=False)
    session_encrypted = Column(Text, nullable=False)
    telegram_user_id = Column(String, nullable=True)
    telegram_username = Column(String, nullable=True)
    phone_hint = Column(String, nullable=True)
    status = Column(String, nullable=False, default="active")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class ConversationState(Base):
    __tablename__ = 'conversation_states'

    user_id = Column(BigInteger, primary_key=True)
    state = Column(String, nullable=False)
    payload = Column(Text, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class DownloadJob(Base):
    __tablename__ = 'download_jobs'

    id = Column(String, primary_key=True)
    user_id = Column(BigInteger, nullable=False, index=True)
    status = Column(String, nullable=False, default="queued")
    chat_id = Column(String, nullable=True)
    message_id = Column(Integer, nullable=True)
    progress = Column(Integer, nullable=False, default=0)
    total = Column(Integer, nullable=False, default=0)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

class Database:
    def __init__(self):
        os.makedirs(Config.DATA_DIR, exist_ok=True)
        
        engine_options = {"pool_pre_ping": True, "pool_recycle": 3600}
        if Config.DATABASE_URL.startswith("sqlite"):
            engine_options.update({
                "connect_args": {"check_same_thread": False, "timeout": 30},
                "poolclass": NullPool,
            })
        self.engine = create_engine(Config.DATABASE_URL, **engine_options)
        
        Base.metadata.create_all(self.engine)
        if Config.DATABASE_URL.startswith("sqlite"):
            with self.engine.begin() as connection:
                connection.execute(text("PRAGMA journal_mode=WAL"))
                connection.execute(text("PRAGMA synchronous=NORMAL"))
        self.Session = sessionmaker(bind=self.engine)
    
    def get_session(self):
        max_retries = 5
        for attempt in range(max_retries):
            try:
                session = self.Session()
                session.execute(text("SELECT 1"))
                return session
            except Exception as e:
                error_msg = str(e)
                if "database is locked" in error_msg and attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    logger.warning(f"Database locked, retrying in {wait_time}s... (attempt {attempt + 1}/{max_retries})")
                    time.sleep(wait_time)
                else:
                    if attempt == max_retries - 1:
                        logger.error(f"Failed to get database session after {max_retries} attempts: {e}")
                    raise
        return None
    
    def add_download_record(self, **kwargs):
        session = None
        try:
            session = self.get_session()
            if session is None:
                logger.warning("Could not get database session, skipping record")
                return None
            record = DownloadHistory(**kwargs)
            session.add(record)
            session.commit()
            session.refresh(record)
            return record.id
        except Exception as e:
            if session:
                session.rollback()
            logger.error(f"Failed to add download record: {e}")
            return None
        finally:
            if session:
                session.close()
    
    def get_user_settings(self, user_id):
        session = None
        try:
            session = self.get_session()
            if session is None:
                logger.warning("Could not get database session, returning default settings")
                return UserSettings(user_id=user_id)
            
            settings = session.query(UserSettings).filter_by(user_id=user_id).first()
            if not settings:
                settings = UserSettings(user_id=user_id)
                session.add(settings)
                session.commit()
                session.refresh(settings)
            
            session.expunge(settings)
            return settings
        except Exception as e:
            logger.error(f"Failed to get user settings: {e}")
            return UserSettings(user_id=user_id)
        finally:
            if session:
                session.close()

    def get_user_profile(self, user_id):
        session = self.get_session()
        try:
            profile = session.query(UserProfile).filter_by(user_id=user_id).first()
            if not profile:
                profile = UserProfile(user_id=user_id)
                session.add(profile)
                session.commit()
                session.refresh(profile)
            session.expunge(profile)
            return profile
        finally:
            session.close()

    def update_user_profile(self, user_id, **kwargs):
        session = self.get_session()
        try:
            profile = session.query(UserProfile).filter_by(user_id=user_id).first()
            if not profile:
                profile = UserProfile(user_id=user_id)
                session.add(profile)
            for key, value in kwargs.items():
                if hasattr(profile, key):
                    setattr(profile, key, value)
            session.commit()
            session.refresh(profile)
            session.expunge(profile)
            return profile
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def save_telegram_credential(self, user_id, **kwargs):
        session = self.get_session()
        try:
            item = session.query(TelegramCredential).filter_by(user_id=user_id).first()
            if not item:
                item = TelegramCredential(user_id=user_id, **kwargs)
                session.add(item)
            else:
                for key, value in kwargs.items():
                    setattr(item, key, value)
            session.commit()
            session.refresh(item)
            session.expunge(item)
            return item
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def get_telegram_credential(self, user_id):
        session = self.get_session()
        try:
            item = session.query(TelegramCredential).filter_by(user_id=user_id).first()
            if item:
                session.expunge(item)
            return item
        finally:
            session.close()

    def delete_telegram_credential(self, user_id):
        session = self.get_session()
        try:
            deleted = session.query(TelegramCredential).filter_by(user_id=user_id).delete()
            session.commit()
            return bool(deleted)
        finally:
            session.close()

    def set_conversation_state(self, user_id, state, payload=None, expires_at=None):
        session = self.get_session()
        try:
            item = session.query(ConversationState).filter_by(user_id=user_id).first()
            if not item:
                item = ConversationState(user_id=user_id, state=state)
                session.add(item)
            item.state = state
            item.payload = payload
            item.expires_at = expires_at
            session.commit()
        finally:
            session.close()

    def get_conversation_state(self, user_id):
        session = self.get_session()
        try:
            item = session.query(ConversationState).filter_by(user_id=user_id).first()
            if item:
                session.expunge(item)
            return item
        finally:
            session.close()

    def clear_conversation_state(self, user_id):
        session = self.get_session()
        try:
            session.query(ConversationState).filter_by(user_id=user_id).delete()
            session.commit()
        finally:
            session.close()

    def clear_user_download_state(self, user_id):
        session = self.get_session()
        try:
            count = session.query(DownloadJob).filter(
                DownloadJob.user_id == user_id,
                DownloadJob.status.in_(["queued", "running", "paused", "failed"])
            ).delete(synchronize_session=False)
            session.commit()
            return count
        finally:
            session.close()
    
    def update_user_settings(self, user_id, **kwargs):
        session = None
        try:
            session = self.get_session()
            if session is None:
                logger.warning("Could not get database session, skipping update")
                return None
            
            settings = session.query(UserSettings).filter_by(user_id=user_id).first()
            if settings:
                for key, value in kwargs.items():
                    setattr(settings, key, value)
                session.commit()
                session.refresh(settings)
                session.expunge(settings)
            else:
                settings = UserSettings(user_id=user_id, **kwargs)
                session.add(settings)
                session.commit()
                session.refresh(settings)
                session.expunge(settings)
            return settings
        except Exception as e:
            if session:
                session.rollback()
            logger.error(f"Failed to update user settings: {e}")
            return None
        finally:
            if session:
                session.close()
    
    def get_stats(self, user_id=None):
        session = None
        try:
            session = self.get_session()
            if session is None:
                logger.warning("Could not get database session, returning empty stats")
                return {'total': 0, 'successful': 0, 'failed': 0, 'total_size': 0}
            
            query = session.query(DownloadHistory)
            if user_id:
                query = query.filter_by(user_id=user_id)
            
            total = query.count()
            successful = query.filter_by(success=True).count()
            total_size_query = query.with_entities(DownloadHistory.file_size).all()
            total_size = sum(size[0] for size in total_size_query if size[0] and size[0] is not None)
            
            return {
                'total': total,
                'successful': successful,
                'failed': total - successful,
                'total_size': total_size or 0
            }
        except Exception as e:
            logger.error(f"Failed to get stats: {e}")
            return {'total': 0, 'successful': 0, 'failed': 0, 'total_size': 0}
        finally:
            if session:
                session.close()
    
    def get_download_history(self, user_id, limit=10):
        session = None
        try:
            session = self.get_session()
            if session is None:
                return []
            
            records = session.query(DownloadHistory)\
                .filter_by(user_id=user_id)\
                .order_by(DownloadHistory.download_time.desc())\
                .limit(limit)\
                .all()
            
            for record in records:
                session.expunge(record)
            return records
        except Exception as e:
            logger.error(f"Failed to get download history: {e}")
            return []
        finally:
            if session:
                session.close()

__all__ = [
    'Database', 'DownloadHistory', 'UserSettings', 'UserProfile',
    'TelegramCredential', 'ConversationState', 'DownloadJob'
]
