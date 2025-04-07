import os

import tomllib
from pydantic import BaseModel, ValidationError  # Import ValidationError
from typing import TYPE_CHECKING

# Import custom exceptions
from errors import ConfigError

if TYPE_CHECKING:
    from ui import ConsoleManager


# Constants - moved from main.py
# Get XDG config directory
XDG_CONFIG_HOME = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
XDG_CONFIG_PATH = os.path.join(XDG_CONFIG_HOME, "vs_manage", "config.toml")

CONFIG_FILES = [
    "./vs_manage.toml",  # Local directory (lowest priority)
    XDG_CONFIG_PATH,  # User config directory (XDG standard)
    "/etc/vs_manage.toml",  # System-wide config (highest priority)
]


class ServerSettings(BaseModel):
    """Server configuration settings validated with Pydantic"""

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


class ConfigManager:
    """Manages loading and generating configuration files"""

    def __init__(self, console: "ConsoleManager"):  # Use type hinting
        """Initialize ConfigManager with a ConsoleManager instance."""
        self.settings = ServerSettings()  # Start with defaults
        self.console = console
        # No direct logger instance needed here, use console for logging

    def load_config(self) -> ServerSettings:
        """Load configuration from TOML files using Pydantic for validation

        Returns:
            ServerSettings: The loaded configuration settings.

        Raises:
            ConfigError: If configuration loading or validation fails.
        """
        config_loaded = False
        loaded_path = "Defaults"

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
                            loaded_path = config_file
                            config_loaded = True
                            break  # Stop after loading the highest priority file
                        except ValidationError as validation_error:
                            # Raise a specific ConfigError for validation issues
                            err_msg = f"Validation error in configuration file '{config_file}': {validation_error}"
                            self.console.error(err_msg)
                            raise ConfigError(err_msg) from validation_error
                except OSError as e:
                    # Log file read errors as warnings but continue trying others
                    self.console.warning(
                        f"Could not read config file '{config_file}': {e}"
                    )
                except tomllib.TOMLDecodeError as e:
                    # Raise a specific ConfigError for parsing issues
                    err_msg = f"Error parsing TOML in config file '{config_file}': {e}"
                    self.console.error(err_msg)
                    raise ConfigError(err_msg) from e
                except Exception as e:
                    # Catch unexpected errors during loading
                    err_msg = (
                        f"Unexpected error loading config file '{config_file}': {e}"
                    )
                    self.console.error(err_msg, exc_info=True)
                    raise ConfigError(err_msg) from e
            else:
                self.console.debug(f"Config file not found: {config_file}")

        if not config_loaded:
            self.console.info(
                "No configuration file found or loaded. Using default settings."
            )
        else:
            self.console.debug(f"Final configuration loaded from: {loaded_path}")

        return self.settings

    def generate_config_file(self) -> str:
        """Generate a configuration file in the primary XDG config location.

        Returns:
            str: Path to the generated configuration file.

        Raises:
            ConfigError: If the directory or file cannot be created.
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

        # Write the default config file
        try:
            with open(config_file, "w") as f:
                f.write(
                    "# Vintage Story Server Management Script - Configuration File\n"
                )
                f.write(
                    "# This file was generated automatically. You can edit it to change settings.\n"
                )
                f.write("# Other possible configuration locations:\n")
                f.write("#   ./vs_manage.toml\n")
                f.write("#   /etc/vs_manage.toml\n\n")

                # Service settings
                f.write("# Service settings\n")
                f.write(f'service_name = "{self.settings.service_name}"\n\n')

                # Directory settings
                f.write("# Directory settings\n")
                f.write(f'server_dir = "{self.settings.server_dir}"\n')
                f.write(f'data_dir = "{self.settings.data_dir}"\n')
                f.write(f'temp_dir = "{self.settings.temp_dir}"\n')
                f.write(f'backup_dir = "{self.settings.backup_dir}"\n')
                f.write(f'log_dir = "{self.settings.log_dir}"\n\n')

                # User settings
                f.write("# User settings\n")
                f.write(f'server_user = "{self.settings.server_user}"\n\n')

                # Backup settings
                f.write("# Backup settings\n")
                f.write(f"max_backups = {self.settings.max_backups}\n\n")

                # Version checking settings
                f.write("# Version checking settings\n")
                f.write(f'downloads_base_url = "{self.settings.downloads_base_url}"\n')
                f.write(
                    f'game_version_api_url = "{self.settings.game_version_api_url}"\n'
                )

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
            self.console.error(err_msg, exc_info=True)
            raise ConfigError(err_msg) from e
