import os
import logging
import tomllib
from pydantic import BaseModel


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

    def __init__(self, console=None):
        """Initialize ConfigManager with default settings and console for output

        Args:
            console: ConsoleManager instance for output (optional)
        """
        self.settings = ServerSettings()
        self.console = console
        self.logger = logging.getLogger("vs_manage")

    def load_config(self) -> ServerSettings:
        """Load configuration from TOML files using Pydantic for validation

        Returns:
            ServerSettings: The loaded configuration settings
        """
        config_loaded = False

        for config_file in CONFIG_FILES:
            if os.path.isfile(config_file):
                try:
                    # Attempt to read the TOML config file
                    with open(config_file, "rb") as f:
                        config_data = tomllib.load(f)

                    # Check if config data was loaded
                    if config_data:
                        # Create settings from the loaded data
                        try:
                            self.settings = ServerSettings(**config_data)
                            if self.console:
                                self.console.print(
                                    f"Loading configuration from {config_file}...",
                                    style="cyan",
                                )
                            self.logger.info(f"Loaded configuration from {config_file}")
                            config_loaded = True
                            break
                        except Exception as validation_error:
                            if self.console:
                                self.console.print(
                                    f"Error validating configuration values: {validation_error}",
                                    style="red",
                                )
                            self.logger.error(
                                f"Configuration validation error: {validation_error}"
                            )
                except Exception as e:
                    if self.console:
                        self.console.print(
                            f"Warning: Failed to load configuration from {config_file}: {e}",
                            style="yellow",
                        )
                    self.logger.warning(
                        f"Failed to load configuration from {config_file}: {e}"
                    )

        if not config_loaded:
            if self.console:
                self.console.print(
                    "No configuration file found, using default values.", style="yellow"
                )
            self.logger.info("Using default configuration values")
            # Keep using the default ServerSettings that was created in __init__

        return self.settings

    def generate_config_file(self) -> str:
        """Generate a configuration file in accordance with XDG standards

        Returns:
            str: Path to the generated configuration file
        """
        # Determine the appropriate config location
        config_dir = os.path.dirname(XDG_CONFIG_PATH)
        config_file = XDG_CONFIG_PATH

        # Create the config directory if it doesn't exist
        if not os.path.exists(config_dir):
            try:
                os.makedirs(config_dir, exist_ok=True)
                if self.console:
                    self.console.print(
                        f"Created configuration directory: {config_dir}", style="cyan"
                    )
            except Exception as e:
                if self.console:
                    self.console.print(
                        f"Error creating directory {config_dir}: {e}", style="red"
                    )
                    self.console.print(
                        "Falling back to current directory", style="yellow"
                    )
                config_file = "./vs_manage.toml"

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

            if self.console:
                self.console.print(
                    f"Configuration file created: {config_file}", style="green"
                )
                self.console.print(
                    "This file will be loaded automatically on next run.", style="cyan"
                )

            self.logger.info(f"Configuration file created: {config_file}")
            return config_file

        except Exception as e:
            if self.console:
                self.console.print(
                    f"Error creating configuration file: {e}", style="red"
                )
            self.logger.error(f"Error creating configuration file: {e}")
            return ""
