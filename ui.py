"""Provides a centralized manager for console output and logging using the Rich library."""

import logging
import os
from typing import Optional
from rich.console import Console
from rich.logging import RichHandler


class ConsoleManager:
    """Manages console output and logging configuration using Rich.

    Handles setting up both console (Rich) and optional file logging handlers.
    Provides wrapper methods for logging at different levels and direct printing.

    Attributes:
        console (Console): The Rich Console instance for output.
        dry_run (bool): If True, indicates dry-run mode, affecting logging setup.
        log_dir (Optional[str]): The directory designated for log files.
        logger (logging.Logger): The logger instance used for logging messages.
    """

    def __init__(self, dry_run: bool = False):
        """Initializes the ConsoleManager.

        Sets up a basic Rich handler immediately, which can be overridden by
        calling setup_logging later.

        Args:
            dry_run (bool): Whether the application is running in dry-run mode.
        """
        self.console = Console()
        self.dry_run = dry_run
        self.log_dir: Optional[str] = None
        self.logger = logging.getLogger("vs_manage")  # Use a named logger
        self._logging_configured = False

        # Basic console config in case setup_logging isn't called or fails
        # This ensures self.console is always usable.
        logging.basicConfig(
            level=logging.WARNING,  # Default to WARNING if not configured
            handlers=[
                RichHandler(rich_tracebacks=True, console=self.console, show_path=False)
            ],
            force=True,  # Override any root logger config from libraries
        )
        self.logger.setLevel(logging.INFO)  # Default level for our logger

    def setup_logging(
        self, log_dir: Optional[str] = None, log_level: int = logging.INFO
    ):
        """Configures logging handlers (console and optional file).

        Removes existing handlers associated with this logger and the root logger
        to ensure a clean setup. Configures console logging using RichHandler
        and adds a FileHandler if `log_dir` is provided and not in dry-run mode.

        Args:
            log_dir: The directory to store log files. If None, only console logging is used.
            log_level: The minimum logging level (e.g., logging.INFO, logging.DEBUG).
                     Defaults to logging.INFO.
        """
        if self._logging_configured and self.log_dir == log_dir:
            self.logger.debug(
                f"Logging already configured for {log_dir}. Skipping setup."
            )
            return

        self.log_dir = log_dir
        log_file = None
        handlers: list[logging.Handler] = []  # Explicit type hint

        # --- Console Handler (Always Active) ---
        console_handler = RichHandler(
            rich_tracebacks=True,
            console=self.console,
            show_path=False,
            level=log_level,  # Set level for the handler
        )
        handlers.append(console_handler)

        # --- File Handler (Conditional) ---
        if self.log_dir and not self.dry_run:
            log_file = os.path.join(self.log_dir, "vs_manage.log")
            try:
                os.makedirs(self.log_dir, exist_ok=True)
                file_handler = logging.FileHandler(log_file, encoding="utf-8")
                # Basic formatter for the file to keep it clean
                formatter = logging.Formatter(
                    "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"
                )
                file_handler.setFormatter(formatter)
                file_handler.setLevel(log_level)  # Set level for the handler
                handlers.append(file_handler)
            except OSError as e:
                # Use logger.warning here instead of print, after basic config
                self.logger.warning(
                    f"Could not create log directory or file '{log_file}': {e}. File logging disabled."
                )
                log_file = None  # Ensure log_file reflects the failure

        # --- Apply Configuration ---
        # Clear existing handlers from our logger AND the root logger to avoid duplication
        # or interference from libraries that configure the root logger.
        for handler in self.logger.handlers[:]:
            self.logger.removeHandler(handler)
        for handler in logging.root.handlers[:]:
            logging.root.removeHandler(handler)

        # Configure our specific logger
        self.logger.setLevel(log_level)
        for handler in handlers:
            self.logger.addHandler(handler)

        self.logger.propagate = False  # Crucial: Prevent messages flowing to root

        self._logging_configured = True

        # --- Post-Setup Logging ---
        if self.dry_run:
            self.info("[DRY RUN MODE] Simulation active. No changes will be made.")
        elif log_file:
            self.info(
                f"Logging initialized (Level: {logging.getLevelName(log_level)}). Output to console and file: {log_file}"
            )
        else:
            self.info(
                f"Logging initialized (Level: {logging.getLevelName(log_level)}). Output to console only."
            )

    def print(
        self,
        *objects: object,
        sep: str = " ",
        end: str = "\n",
        style: Optional[str] = None,
        **kwargs,
    ):
        """Prints objects directly to the console using Rich, bypassing logging.

        Useful for immediate user feedback that shouldn't clutter logs.
        Mimics the built-in print function signature more closely.

        Args:
            *objects: Objects to print.
            sep: Separator between objects.
            end: String appended after the last object.
            style: Rich style string (optional).
            **kwargs: Additional keyword arguments passed to `rich.console.Console.print`.
        """
        self.console.print(*objects, sep=sep, end=end, style=style, **kwargs)

    # --- Logging Methods (Wrappers around self.logger) ---

    def debug(self, message: str, **kwargs):
        """Logs a message with level DEBUG on the 'vs_manage' logger."""
        self.logger.debug(message, **kwargs)

    def info(self, message: str, **kwargs):
        """Logs a message with level INFO on the 'vs_manage' logger."""
        self.logger.info(message, **kwargs)

    def warning(self, message: str, **kwargs):
        """Logs a message with level WARNING on the 'vs_manage' logger."""
        self.logger.warning(message, **kwargs)

    def error(self, message: str, exc_info=False, **kwargs):
        """Logs a message with level ERROR on the 'vs_manage' logger.

        Args:
            message: The error message string.
            exc_info (bool): If True, exception information is added to the log message.
                            Defaults to False.
            **kwargs: Additional keyword arguments for the logger.
        """
        self.logger.error(message, exc_info=exc_info, **kwargs)

    def exception(self, message: str, **kwargs):
        """Logs a message with level ERROR on the 'vs_manage' logger, including exception info.

        This is a convenience method equivalent to calling `error` with `exc_info=True`.

        Args:
            message: The error message string.
            **kwargs: Additional keyword arguments for the logger.
        """
        self.logger.exception(message, **kwargs)

    def critical(self, message: str, **kwargs):
        """Logs a message with level CRITICAL on the 'vs_manage' logger."""
        self.logger.critical(message, **kwargs)
