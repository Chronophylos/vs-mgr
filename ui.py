import logging
import os
from typing import Optional
from rich.console import Console
from rich.logging import RichHandler


class ConsoleManager:
    """Manages console output and logging with rich formatting"""

    def __init__(self, log_dir: Optional[str] = None, dry_run: bool = False):
        """Initialize ConsoleManager with console and logging configuration

        Args:
            log_dir: Directory to store log files (optional)
            dry_run: Whether to run in dry-run mode (optional)
        """
        # Setup console for rich output
        self.console = Console()
        self.dry_run = dry_run
        self.log_dir = log_dir

        # Initialize logger
        self.setup_logging()

    def setup_logging(self):
        """Set up logging configuration"""
        log_dir = self.log_dir
        log_file = None

        if log_dir:
            log_file = os.path.join(log_dir, "vs_manage.log")

            # Create log directory if it doesn't exist
            if not self.dry_run:
                try:
                    os.makedirs(log_dir, exist_ok=True)
                except Exception as e:
                    # If we can't create the log directory, use a fallback approach
                    # This can happen during early initialization before config is loaded
                    self.console.print(
                        f"Warning: Could not create log directory {log_dir}: {e}",
                        style="yellow",
                    )
                    log_file = None

        # Configure logging
        handlers = []
        # Add rich handler
        handlers.append(RichHandler(rich_tracebacks=True, console=self.console))

        # Add file handler if we have a valid log file
        if log_file and not self.dry_run:
            try:
                handlers.append(logging.FileHandler(log_file))
            except Exception as e:
                self.console.print(
                    f"Warning: Could not set up log file {log_file}: {e}",
                    style="yellow",
                )

        # Reset basic config to ensure handlers are updated
        for handler in logging.root.handlers[:]:
            logging.root.removeHandler(handler)

        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(message)s",
            handlers=handlers,
            force=True,
        )
        self.logger = logging.getLogger("vs_manage")

        if self.dry_run:
            self.log_message(
                "INFO", "[DRY RUN MODE] Simulating operations without making changes"
            )

    def log_message(self, level: str, message: str):
        """Log a message with the specified level and handle console output

        Args:
            level: Log level (INFO, WARNING, ERROR, DEBUG)
            message: The message to log
        """
        if level == "INFO":
            self.logger.info(message)
            self.console.print(message, style="cyan")
        elif level == "WARNING":
            self.logger.warning(message)
            self.console.print(f"WARNING: {message}", style="yellow")
        elif level == "ERROR":
            self.logger.error(message)
            self.console.print(f"ERROR: {message}", style="red")
        elif level == "DEBUG":
            self.logger.debug(message)
            if os.environ.get("DEBUG_MODE", "false").lower() in ("true", "yes", "1"):
                self.console.print(f"DEBUG: {message}", style="blue")

    def print(self, message: str, style: Optional[str] = None, **kwargs):
        """Print a message to the console with optional styling

        Args:
            message: The message to print
            style: Rich style string (optional)
            **kwargs: Additional keyword arguments for Console.print
        """
        self.console.print(message, style=style, **kwargs)
