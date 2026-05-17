# src/treeadmin/logging_config.py

from __future__ import annotations

import logging
import logging.config
import os
from pathlib import Path


def setup_logging(mode: str) -> None:
    if mode not in {"server", "client"}:
        raise ValueError("mode must be 'server' or 'client'")

    log_level = os.getenv("TREEADMIN_LOG_LEVEL", "INFO").upper()
    log_dir = Path(os.getenv("TREEADMIN_LOG_DIR", "logs"))
    log_dir.mkdir(parents=True, exist_ok=True)

    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {
                    "format": (
                        "%(asctime)s | %(levelname)-8s | %(name)s | "
                        "%(threadName)s | %(message)s"
                    )
                },
                "audit": {
                    "format": (
                        "%(asctime)s | %(levelname)-8s | AUDIT | "
                        "%(name)s | %(message)s"
                    )
                },
            },
            "handlers": {
                "app_file": {
                    "class": "logging.handlers.RotatingFileHandler",
                    "level": log_level,
                    "formatter": "default",
                    "filename": str(log_dir / f"treeadmin-{mode}.log"),
                    "maxBytes": 2_000_000,
                    "backupCount": 5,
                    "encoding": "utf-8",
                },
                "error_file": {
                    "class": "logging.handlers.RotatingFileHandler",
                    "level": "ERROR",
                    "formatter": "default",
                    "filename": str(log_dir / f"treeadmin-{mode}-errors.log"),
                    "maxBytes": 2_000_000,
                    "backupCount": 5,
                    "encoding": "utf-8",
                },
                "audit_file": {
                    "class": "logging.handlers.RotatingFileHandler",
                    "level": "INFO",
                    "formatter": "audit",
                    "filename": str(log_dir / f"treeadmin-{mode}-audit.log"),
                    "maxBytes": 2_000_000,
                    "backupCount": 10,
                    "encoding": "utf-8",
                },
            },
            "loggers": {
                "src.treeadmin": {
                    "level": log_level,
                    "handlers": ["app_file", "error_file"],
                    "propagate": False,
                },
                "treeadmin.audit": {
                    "level": "INFO",
                    "handlers": ["audit_file"],
                    "propagate": False,
                },
            },
        }
    )