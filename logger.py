# logger.py
import logging
import os

def get_logger(name):
    """Setup logger with console and file handlers"""
    logger = logging.getLogger(name)
    
    log_level = getattr(logging, os.getenv("LOG_LEVEL", "INFO"), logging.INFO)
    logger.setLevel(log_level)
    # main.py owns the console and rotating per-run file handlers. Propagation
    # keeps every module on that single chain and avoids duplicate lines/files.
    logger.propagate = True
    
    return logger

LOGGER = get_logger
