"""Structured, level-configurable logging setup. Call setup_logging() once at process start."""
from __future__ import annotations

import logging

from config.settings import get_settings

_CONFIGURED = False


def setup_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    level = getattr(logging, get_settings().log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S%z",
    )
    _CONFIGURED = True
