"""Shared logging configuration: console + a rotating-by-run file under /logs."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path


def setup_logging(logs_dir: Path, level: int = logging.INFO) -> logging.Logger:
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / f"pipeline_{datetime.now():%Y%m%d_%H%M%S}.log"

    logger = logging.getLogger("pipeline")
    logger.setLevel(level)
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger
