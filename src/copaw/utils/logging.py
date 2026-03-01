# -*- coding: utf-8 -*-
import logging
import os
import platform
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional


_LEVEL_MAP = {
    "critical": logging.CRITICAL,
    "error": logging.ERROR,
    "warning": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG,
}

# Top-level name for this package; only loggers under this name are shown.
LOG_NAMESPACE = "copaw"


def _enable_windows_ansi() -> None:
    """Enable ANSI escape code support on Windows 10+."""
    if platform.system() != "Windows":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        # STD_OUTPUT_HANDLE = -11, ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


# Call once at import time
_enable_windows_ansi()


class ColorFormatter(logging.Formatter):
    COLORS = {
        logging.DEBUG: "\033[34m",
        logging.INFO: "\033[32m",
        logging.WARNING: "\033[33m",
        logging.ERROR: "\033[31m",
        logging.CRITICAL: "\033[41m\033[97m",
    }
    RESET = "\033[0m"

    def format(self, record):
        # Disable colors if output is not a terminal (e.g. piped/redirected)
        use_color = hasattr(sys.stderr, "isatty") and sys.stderr.isatty()
        color = self.COLORS.get(record.levelno, "") if use_color else ""
        reset = self.RESET if use_color else ""
        level = f"{color}{record.levelname}{reset}"

        full_path = record.pathname
        cwd = os.getcwd()
        # Use os.path for cross-platform path prefix stripping
        try:
            if os.path.commonpath([full_path, cwd]) == cwd:
                full_path = os.path.relpath(full_path, cwd)
        except ValueError:
            # Different drives on Windows (e.g., C: vs D:) are not comparable.
            pass

        prefix = f"{level} {full_path}:{record.lineno}"
        original_msg = super().format(record)

        return f"{prefix} | {original_msg}"


class SuppressPathAccessLogFilter(logging.Filter):
    """
    Filter out uvicorn access log lines whose message contains any of the
    given path substrings. path_substrings: list of substrings; if any
    appears in the log message, the record is suppressed.
    Empty list = allow all.
    """

    def __init__(self, path_substrings: list[str]) -> None:
        super().__init__()
        self.path_substrings = path_substrings

    def filter(self, record: logging.LogRecord) -> bool:
        if not self.path_substrings:
            return True
        try:
            msg = record.getMessage()
            return not any(s in msg for s in self.path_substrings)
        except Exception:
            return True


def setup_logger(
    level: int | str = logging.INFO,
    log_dir: Optional[str] = None,
):
    """Configure logging to only output from this package (copaw), not deps.

    Args:
        level: Log level (int or string like 'info', 'debug').
        log_dir: Directory to write log file. If None, no file logging.
                 Defaults to ~/logs/ when called from app_cmd.
    """
    log_format = "%(asctime)s | %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    if isinstance(level, str):
        level = _LEVEL_MAP.get(level.lower(), logging.INFO)

    formatter = ColorFormatter(log_format, datefmt)
    # Plain formatter for file (no ANSI color codes)
    plain_formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(filename)s:%(lineno)d | %(funcName)s | %(message)s",
        datefmt=datefmt,
    )

    # Suppress third-party: root has no handler and high level.
    root = logging.getLogger()
    root.setLevel(logging.WARNING)
    root.handlers.clear()

    # Only attach handler to our namespace so only copaw.* logs are printed.
    logger = logging.getLogger(LOG_NAMESPACE)
    logger.setLevel(level)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    # Optional file handler
    if log_dir:
        try:
            log_path = Path(log_dir).expanduser()
            log_path.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            log_file = log_path / f"{timestamp}.log"
            file_handler = logging.FileHandler(log_file, encoding="utf-8")
            file_handler.setFormatter(plain_formatter)
            file_handler.setLevel(level)
            logger.addHandler(file_handler)
            logger.info("Log file: %s", log_file)
        except Exception as e:
            logger.warning("Failed to set up file logging at %s: %s", log_dir, e)

    return logger
