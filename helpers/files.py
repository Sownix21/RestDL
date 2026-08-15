# helpers/files.py
import os
import re
import shutil
from pathlib import Path
from datetime import datetime
from logger import get_logger

logger = get_logger(__name__)


def sanitize_filename(filename: str, fallback: str = "download.bin") -> str:
    """Make Telegram-provided filenames safe for the local filesystem."""
    name = str(filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    if not name or name in {".", ".."}:
        name = fallback
    stem, suffix = os.path.splitext(name)
    if len(name) > 180:
        name = f"{stem[:max(1, 180 - len(suffix))]}{suffix[:20]}"
    return name

def get_download_path(message_id: int, filename: str) -> str:
    """Get download path for a file"""
    downloads_dir = Path("downloads")
    downloads_dir.mkdir(exist_ok=True)
    
    date_dir = downloads_dir / datetime.now().strftime("%Y-%m-%d")
    date_dir.mkdir(exist_ok=True)
    
    message_dir = date_dir / str(message_id)
    message_dir.mkdir(exist_ok=True)
    
    return str(message_dir / sanitize_filename(filename))

async def check_file_size(file_size: int, max_size: int = None) -> tuple:
    """Check if file size is within limits"""
    if not max_size:
        from config import Config
        max_size = Config.MAX_FILE_SIZE
    
    if file_size > max_size:
        return False, f"File too large: {get_readable_file_size(file_size)} (max: {get_readable_file_size(max_size)})"
    return True, "OK"

def get_readable_file_size(size_in_bytes: int) -> str:
    """Convert bytes to human readable format"""
    if not size_in_bytes:
        return "0 B"
    
    size_name = ("B", "KB", "MB", "GB", "TB", "PB")
    i = int(min(len(size_name) - 1, (size_in_bytes.bit_length() - 1) // 10))
    p = 1024 ** i
    return f"{size_in_bytes / p:.2f} {size_name[i]}"

def get_readable_time(seconds: float) -> str:
    """Convert seconds to human readable time format"""
    if seconds < 60:
        return f"{seconds:.1f}s"
    elif seconds < 3600:
        minutes = seconds / 60
        return f"{minutes:.1f}m"
    elif seconds < 86400:
        hours = seconds / 3600
        return f"{hours:.1f}h"
    else:
        days = seconds / 86400
        return f"{days:.1f}d"

def cleanup_download(file_path: str) -> bool:
    """Clean up a single downloaded file"""
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
            logger.debug(f"Cleaned up: {file_path}")
            return True
    except Exception as e:
        logger.error(f"Failed to cleanup {file_path}: {e}")
    return False

def cleanup_old_downloads(days: int = 7) -> tuple:
    """Clean up downloads older than specified days"""
    downloads_dir = Path("downloads")
    if not downloads_dir.exists():
        return 0, 0
    
    files_removed = 0
    bytes_freed = 0
    cutoff = datetime.now().timestamp() - (days * 24 * 60 * 60)
    
    try:
        for root, dirs, files in os.walk(downloads_dir):
            for file in files:
                file_path = Path(root) / file
                if file_path.stat().st_mtime < cutoff:
                    bytes_freed += file_path.stat().st_size
                    file_path.unlink()
                    files_removed += 1
        
        for root, dirs, files in os.walk(downloads_dir, topdown=False):
            for dir_name in dirs:
                dir_path = Path(root) / dir_name
                try:
                    if not any(dir_path.iterdir()):
                        dir_path.rmdir()
                except OSError:
                    pass
    except Exception as e:
        logger.error(f"Cleanup error: {e}")
    
    return files_removed, bytes_freed

def cleanup_downloads_root() -> tuple:
    """Clean up all downloaded files in downloads directory"""
    return cleanup_old_downloads(0)

__all__ = [
    'get_download_path',
    'sanitize_filename',
    'check_file_size',
    'get_readable_file_size',
    'get_readable_time',
    'cleanup_download',
    'cleanup_old_downloads',
    'cleanup_downloads_root',
]
