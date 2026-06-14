"""Shared logging utility"""

import logging
import os


def get_logger(name: str, tag: str) -> logging.Logger:
    """Allow configurable logging levels for individual instances"""
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, log_level, logging.INFO))
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(f"%(asctime)s [{tag}] %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    return logger
