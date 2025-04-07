import logging
import os
from typing import Optional
from rich.console import Console
from rich.logging import RichHandler


class ConsoleManager:
    """Manages console output and logging with rich formatting"""

    def __init__(self, dry_run: bool = False):
        """Initialize ConsoleManager. Logging is set up separately via setup_logging."""
        self.console = Console()
        self.dry_run = dry_run
        self.log_dir: Optional[str] = None
        self.logger = logging.getLogger("vs_manage")  # Get logger instance
        self._logging_configured = False

        # Basic config in case setup_logging isn't called immediately
        logging.basicConfig(
            level=logging.INFO,
            handlers=[RichHandler(rich_tracebacks=True, console=self.console)],
        )

    def setup_logging(self, log_dir: Optional[str] = None):
        """Set up logging configuration, potentially with a file handler.

        Args:
            log_dir: Directory to store log files (optional). If provided, enables file logging.
        """
        if self._logging_configured and self.log_dir == log_dir:
            # Avoid redundant configuration if called multiple times with same dir
            return

        self.log_dir = log_dir
        log_file = None
        handlers = []

        # Console Handler (always add)
        handlers.append(
            RichHandler(rich_tracebacks=True, console=self.console, show_path=False)
        )

        # File Handler (add if log_dir is provided and not dry run)
        if self.log_dir and not self.dry_run:
            log_file = os.path.join(self.log_dir, "vs_manage.log")
            try:
                os.makedirs(self.log_dir, exist_ok=True)
                handlers.append(logging.FileHandler(log_file))
            except Exception as e:
                self.console.print(
                    f"[yellow]Warning:[/yellow] Could not create log directory or file {log_file}: {e}",
                )

        # Apply new configuration
        # Remove existing handlers attached to the root logger or our specific logger
        for handler in logging.root.handlers[:]:
            logging.root.removeHandler(handler)
        for handler in self.logger.handlers[:]:
            self.logger.removeHandler(
                handler
            )  # Remove handlers specific to our logger too

        logging.basicConfig(
            level=logging.INFO,  # Set base level for root logger
            format="%(asctime)s [%(levelname)-8s] %(message)s",  # Format for file logger
            handlers=handlers,
            force=True,  # Override existing config
        )

        # Set level specifically for our logger if needed (e.g., DEBUG)
        # For now, it inherits INFO from root. Can be made configurable later.
        # self.logger.setLevel(logging.DEBUG)

        self.logger.propagate = False  # Prevent messages from propagating to the root logger if it has handlers

        self._logging_configured = True

        if self.dry_run:
            self.info("[DRY RUN MODE] Simulating operations without making changes")
        elif log_file:
            self.debug(
                f"Logging initialized. Console and file ({log_file}) handlers active."
            )
        else:
            self.debug("Logging initialized. Console handler active.")

    def print(self, message: str, style: Optional[str] = None, **kwargs):
        """Print a message directly to the console with optional styling.
           Use this for direct user feedback not necessarily meant for logs.

        Args:
            message: The message to print
            style: Rich style string (optional)
            **kwargs: Additional keyword arguments for Console.print
        """
        self.console.print(message, style=style, **kwargs)

    # --- Logging Methods ---

    def debug(self, message: str, **kwargs):
        """Log a DEBUG level message."""
        self.logger.debug(message, **kwargs)

    def info(self, message: str, **kwargs):
        """Log an INFO level message."""
        self.logger.info(message, **kwargs)

    def warning(self, message: str, **kwargs):
        """Log a WARNING level message."""
        self.logger.warning(message, **kwargs)

    def error(self, message: str, exc_info=False, **kwargs):
        """Log an ERROR level message.

        Args:
            message: The error message.
            exc_info: If True, include exception information in the log.
            **kwargs: Additional arguments for the logger.
        """
        self.logger.error(message, exc_info=exc_info, **kwargs)

    def exception(self, message: str, **kwargs):
        """Log an ERROR level message with exception information included."""
        # This is equivalent to self.error(message, exc_info=True)
        self.logger.exception(message, **kwargs)

    def critical(self, message: str, **kwargs):
        """Log a CRITICAL level message."""
        self.logger.critical(message, **kwargs)
