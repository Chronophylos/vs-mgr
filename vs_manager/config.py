"""Manages loading and validation of server configuration from TOML files."""

import os

import tomllib
from pydantic import BaseModel, ValidationError
from typing import TYPE_CHECKING

# Import custom exceptions
from errors import ConfigError

if TYPE_CHECKING:
    from ui import ConsoleManager


# --- Constants ---
# Get XDG config directory
XDG_CONFIG_HOME = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
XDG_CONFIG_PATH = os.path.join(XDG_CONFIG_HOME, "vs_manage", "config.toml")

# Configuration file search paths (from lowest to highest priority)
CONFIG_FILES = [
    "./vs_manage.toml",
    XDG_CONFIG_PATH,
    "/etc/vs_manage.toml",
]


# --- Pydantic Model ---
class ServerSettings(BaseModel):
    """Defines the structure and default values for server configuration.

    Uses Pydantic for data validation.
    """

    # Service settings
    service_name: str = "vintagestoryserver"

    # Directory settings
    server_dir: str = "/srv/gameserver/vintagestory"
    data_dir: str = "/srv/gameserver/data/vs"
    temp_dir: str = "/tmp/vs_update"
    backup_dir: str = "/srv/gameserver/backups"
    log_dir: str = "/var/log/vs_manage"

    # User settings
    server_user: str = "gameserver"

    # Backup settings
    max_backups: int = 10

    # Version checking
    downloads_base_url: str = "https://cdn.vintagestory.at/gamefiles"
    game_version_api_url: str = "https://mods.vintagestory.at/api/gameversions"


# --- Configuration Management ---
class ConfigManager:
    """Handles loading configuration from files and generating a default config.

    Attributes:
        settings (ServerSettings): The validated configuration settings.
        console (ConsoleManager): Instance for logging and user output.
    """

    def __init__(self, console: "ConsoleManager"):
        """Initializes the ConfigManager.

        Args:
            console: An instance of ConsoleManager for logging.
        """
        self.settings = ServerSettings()  # Start with Pydantic defaults
        self.console = console
        # Logging is handled via the passed ConsoleManager instance

    def load_config(self) -> ServerSettings:
        """Loads configuration from the first found TOML file in the search path.

        It merges the loaded settings over the defaults and validates them.

        Returns:
            The validated ServerSettings instance.

        Raises:
            ConfigError: If a config file is found but is invalid (parsing error,
                         validation error) or if there's an unexpected issue loading it.
        """
        config_loaded = False

        for config_file in CONFIG_FILES:
            if os.path.isfile(config_file):
                self.console.debug(f"Attempting to load config: {config_file}")
                try:
                    with open(config_file, "rb") as f:
                        config_data = tomllib.load(f)

                    if config_data:
                        try:
                            # Validate and update settings
                            new_settings = ServerSettings(**config_data)
                            self.settings = new_settings  # Update instance settings
                            self.console.info(
                                f"Successfully loaded configuration from {config_file}"
                            )
                            config_loaded = True
                            break  # Stop after loading the highest priority file
                        except ValidationError as validation_error:
                            # Raise a specific ConfigError for validation issues
                            err_msg = f"Validation error in configuration file '{config_file}': {validation_error}"
                            self.console.error(err_msg)
                            raise ConfigError(err_msg) from validation_error
                except OSError as e:
                    # File read errors are logged but don't stop the process
                    # unless it's the only potential config source that fails.
                    self.console.warning(
                        f"Could not read config file '{config_file}': {e}"
                    )
                except tomllib.TOMLDecodeError as e:
                    # Invalid TOML syntax
                    err_msg = f"Error parsing TOML in config file '{config_file}': {e}"
                    self.console.error(err_msg)
                    raise ConfigError(err_msg) from e
                except Exception as e:
                    # Catch unexpected errors during loading
                    err_msg = (
                        f"Unexpected error loading config file '{config_file}': {e}"
                    )
                    self.console.error(
                        err_msg, exc_info=True
                    )  # Include traceback for unexpected errors
                    raise ConfigError(err_msg) from e
            else:
                self.console.debug(f"Config file not found: {config_file}")

        if not config_loaded:
            self.console.info(
                "No configuration file found or loaded. Using default settings."
            )
        # else: # Debug log seems redundant if info log above states success
        # self.console.debug(f"Final configuration loaded from: {loaded_path}")

        return self.settings

    def generate_config_file(self) -> str:
        """Generates a default configuration file in the primary XDG location.

        Creates the necessary directory if it doesn't exist.

        Returns:
            The absolute path to the generated configuration file.

        Raises:
            ConfigError: If the directory or file cannot be created due to permissions
                         or other OS-level issues.
        """
        config_dir = os.path.dirname(XDG_CONFIG_PATH)
        config_file = XDG_CONFIG_PATH
        self.console.info(f"Attempting to generate default config at: {config_file}")

        # Create the config directory
        try:
            os.makedirs(config_dir, exist_ok=True)
            self.console.debug(f"Ensured configuration directory exists: {config_dir}")
        except OSError as e:
            err_msg = f"Failed to create configuration directory '{config_dir}': {e}"
            self.console.error(err_msg)
            raise ConfigError(err_msg) from e

        # Write the default config file content
        config_content = f"""# Vintage Story Server Management Script - Configuration File
# This file was generated automatically. You can edit it to change settings.
# Configuration files are loaded in this priority order:
#   1. /etc/vs_manage.toml
#   2. {XDG_CONFIG_PATH}
#   3. ./vs_manage.toml

# Service settings
service_name = "{self.settings.service_name}"

# Directory settings
server_dir = "{self.settings.server_dir}"
data_dir = "{self.settings.data_dir}"
temp_dir = "{self.settings.temp_dir}"
backup_dir = "{self.settings.backup_dir}"
log_dir = "{self.settings.log_dir}"

# User settings
server_user = "{self.settings.server_user}"

# Backup settings
max_backups = {self.settings.max_backups}

# Version checking settings
downloads_base_url = "{self.settings.downloads_base_url}"
game_version_api_url = "{self.settings.game_version_api_url}"
"""

        try:
            with open(config_file, "w", encoding="utf-8") as f:
                f.write(config_content)

            self.console.info(
                f"Successfully generated configuration file: {config_file}"
            )
            self.console.print(
                f"Configuration file created: [bold cyan]{config_file}[/bold cyan]"
            )
            self.console.print(
                "This file will be loaded automatically on the next run.", style="dim"
            )
            return config_file

        except OSError as e:
            err_msg = f"Failed to write configuration file '{config_file}': {e}"
            self.console.error(err_msg)
            raise ConfigError(err_msg) from e
        except Exception as e:  # Catch any other unexpected write errors
            err_msg = f"An unexpected error occurred while generating config file '{config_file}': {e}"
            self.console.error(err_msg, exc_info=True)  # Include traceback
            raise ConfigError(err_msg) from e
