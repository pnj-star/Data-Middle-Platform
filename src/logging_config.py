"""Centralized logging configuration for data-middle-platform."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from .config import config as app_config

LOG_LEVEL = getattr(logging, app_config.app.log_level.upper(), logging.INFO)

# Root logger for this package
logger = logging.getLogger("pipeline")
logger.setLevel(LOG_LEVEL)

# Console handler
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(LOG_LEVEL)
console_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
))
logger.addHandler(console_handler)

# File handler (persistent log to data dir)
_data_dir = Path(app_config.data_dir_abs)
_data_dir.mkdir(parents=True, exist_ok=True)
file_handler = logging.FileHandler(_data_dir / "pipeline.log", encoding="utf-8")
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s - %(filename)s:%(lineno)d - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
))
logger.addHandler(file_handler)

# Prevent propagation to root logger to avoid duplicate output
logger.propagate = False


def get_logger(name: str) -> logging.Logger:
    """Get a child logger under the 'pipeline' namespace."""
    return logger.getChild(name)
