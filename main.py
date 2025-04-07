import argparse
import datetime
import logging
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
from typing import Optional
import re
import requests
import zstandard
from packaging import version
from rich.console import Console
from rich.logging import RichHandler
from pydantic import BaseModel
import tomllib


# Constants
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


class VSServerManager:
    def __init__(self):
        # Setup console for rich output
        self.console = Console()

        # Global state variables
        self.server_stopped = False
        self.archive_name = ""
        self.dry_run = False

        # Simple check for admin privileges - might not be 100% accurate on all systems
        self.is_root = os.access("/root", os.W_OK)
        self.rsync_available = shutil.which("rsync") is not None

        # Initialize settings with defaults (will be replaced in load_config)
        self.settings = ServerSettings()

        # Initialize logger
        self.setup_logging()

    def setup_logging(self):
        """Set up logging configuration"""
        log_dir = self.settings.log_dir
        log_file = os.path.join(log_dir, "vs_manage.log")

        # Create log directory if it doesn't exist
        if not self.dry_run:
            try:
                os.makedirs(log_dir, exist_ok=True)
            except Exception as e:
                # If we can't create the log directory, use a fallback approach
                # This can happen during early initialization before config is loaded
                print(f"Warning: Could not create log directory {log_dir}: {e}")
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
                print(f"Warning: Could not set up log file {log_file}: {e}")

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
            self.logger.info(
                "[DRY RUN MODE] Simulating operations without making changes"
            )

    def load_config(self):
        """Load configuration from TOML files using Pydantic for validation"""
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
                            self.console.print(
                                f"Loading configuration from {config_file}...",
                                style="cyan",
                            )
                            self.logger.info(f"Loaded configuration from {config_file}")
                            config_loaded = True
                            break
                        except Exception as validation_error:
                            self.console.print(
                                f"Error validating configuration values: {validation_error}",
                                style="red",
                            )
                            self.logger.error(
                                f"Configuration validation error: {validation_error}"
                            )
                except Exception as e:
                    self.console.print(
                        f"Warning: Failed to load configuration from {config_file}: {e}",
                        style="yellow",
                    )
                    self.logger.warning(
                        f"Failed to load configuration from {config_file}: {e}"
                    )

        if not config_loaded:
            self.console.print(
                "No configuration file found, using default values.", style="yellow"
            )
            self.logger.info("Using default configuration values")
            # Keep using the default ServerSettings that was created in __init__

        # Update logging with potentially new log_dir
        self.setup_logging()

    def run_with_sudo(self, *args, **kwargs):
        """Execute a command with sudo if needed"""
        if self.is_root:
            return subprocess.run(*args, **kwargs)
        else:
            cmd = args[0]
            if isinstance(cmd, list):
                cmd = ["sudo"] + cmd
            else:
                cmd = f"sudo {cmd}"
            return subprocess.run(cmd, **kwargs)

    def log_message(self, level: str, message: str):
        """Log a message with the specified level and handle console output"""
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

    def check_service_exists(self, service_name: str) -> bool:
        """Check if the systemd service exists"""
        try:
            result = subprocess.run(
                ["systemctl", "list-unit-files", f"{service_name}.service"],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            return f"{service_name}.service" in result.stdout
        except Exception as e:
            self.log_message("ERROR", f"Failed to check service existence: {e}")
            return False

    def run_systemctl(self, action: str, service: str) -> bool:
        """Wrapper for systemctl operations"""
        msg = f"systemctl {action} {service}.service"
        self.logger.debug(f"Executing {msg}")

        if self.dry_run:
            self.console.print(f"[DRY RUN] Would run: {msg}", style="blue")
            return True

        try:
            self.run_with_sudo(["systemctl", action, f"{service}.service"], check=True)
            self.log_message("INFO", f"{msg} successful")
            return True
        except Exception as e:
            self.log_message("ERROR", f"{msg} failed: {e}")
            return False

    def run_mkdir(self, directory: str) -> bool:
        """Wrapper for directory creation"""
        self.logger.debug(f"Creating directory: {directory}")

        if self.dry_run:
            self.console.print(
                f"[DRY RUN] Would create directory: {directory}", style="blue"
            )
            return True

        try:
            os.makedirs(directory, exist_ok=True)
            self.log_message("INFO", f"Created directory: {directory}")
            return True
        except PermissionError:
            try:
                self.run_with_sudo(["mkdir", "-p", directory], check=True)
                self.log_message("INFO", f"Created directory with sudo: {directory}")
                return True
            except Exception as e:
                self.log_message(
                    "ERROR", f"Failed to create directory: {directory}: {e}"
                )
                return False
        except Exception as e:
            self.log_message("ERROR", f"Failed to create directory: {directory}: {e}")
            return False

    def run_chown(self, owner: str, target: str, recursive: bool = False) -> bool:
        """Wrapper for chown operations"""
        r_flag = "-R" if recursive else ""
        msg = (
            f"chown {r_flag} {owner} {target}"
            if recursive
            else f"chown {owner} {target}"
        )
        self.logger.debug(f"Executing {msg}")

        if self.dry_run:
            self.console.print(f"[DRY RUN] Would run: {msg}", style="blue")
            return True

        try:
            cmd = ["chown"]
            if recursive:
                cmd.append("-R")
            cmd.extend([owner, target])
            self.run_with_sudo(cmd, check=True)
            self.log_message("INFO", f"{msg} successful")
            return True
        except Exception as e:
            self.log_message("WARNING", f"{msg} failed: {e}")
            return False

    def check_dependencies(self) -> bool:
        """Check if required dependencies are installed"""
        critical_deps = ["wget", "tar", "systemctl"]
        recommended_deps = ["rsync"]
        opt_deps = ["dotnet", "jq"]
        missing_deps = []

        self.console.print("Checking for required dependencies...", style="cyan")

        # Check for critical dependencies
        for dep in critical_deps:
            if shutil.which(dep) is None:
                missing_deps.append(dep)

        if missing_deps:
            self.console.print(
                f"Error: Missing required dependencies: {', '.join(missing_deps)}",
                style="red",
            )
            self.console.print(
                "Please install these dependencies before running this script.",
                style="yellow",
            )
            return False

        # Check for zstd specifically for backups
        if shutil.which("zstd") is None:
            self.console.print(
                "Warning: 'zstd' is not installed. Backup functionality may be limited.",
                style="yellow",
            )
            # For Python implementation, we'll use the zstandard module instead

        # Check for recommended dependencies
        for dep in recommended_deps:
            if dep == "rsync" and shutil.which(dep) is not None:
                self.rsync_available = True
                self.console.print(
                    "✓ rsync is available (recommended for safer updates)",
                    style="green",
                )
            elif dep == "rsync":
                self.rsync_available = False
                self.console.print("⚠ IMPORTANT: rsync is not installed!", style="red")
                self.console.print(
                    "  Server updates will use a fallback method that is LESS SAFE and could potentially cause data loss.",
                    style="red",
                )
                self.console.print(
                    "  It is STRONGLY RECOMMENDED to install rsync before proceeding with updates.",
                    style="red",
                )
                self.console.print(
                    "  On most systems, you can install it with: apt install rsync (Debian/Ubuntu) or yum install rsync (RHEL/CentOS)",
                    style="yellow",
                )

                # Prompt for confirmation if not in dry-run mode
                if not self.dry_run:
                    self.console.print(
                        "Do you want to continue without rsync? (y/N)", style="yellow"
                    )
                    response = input().lower()
                    if response not in ("y", "yes"):
                        self.console.print(
                            "Exiting. Please install rsync and try again.", style="cyan"
                        )
                        return False
                    self.console.print(
                        "Proceeding without rsync (not recommended)...", style="yellow"
                    )

        # Check for optional dependencies
        for dep in opt_deps:
            if shutil.which(dep) is None:
                self.console.print(
                    f"Note: Optional dependency '{dep}' not found.", style="yellow"
                )
                if dep == "dotnet":
                    self.console.print(
                        "  Some version checking features will be limited.",
                        style="yellow",
                    )
                    self.console.print(
                        "  Consider installing dotnet for direct version verification.",
                        style="yellow",
                    )
                elif dep == "jq":
                    self.console.print(
                        "  JSON parsing for version checks will use Python methods.",
                        style="yellow",
                    )

        self.console.print("All required dependencies are available.", style="green")
        return True

    def cleanup(self, exit_code: int = 0):
        """Clean up temporary files and restart server if necessary"""
        # Clean up temporary update directory
        if (
            self.settings.temp_dir
            and os.path.isdir(self.settings.temp_dir)
            and self.settings.temp_dir != "/"
        ):
            self.console.print(
                f"Cleaning up temporary directory: {self.settings.temp_dir}",
                style="blue",
            )
            if not self.dry_run:
                shutil.rmtree(self.settings.temp_dir, ignore_errors=True)
            else:
                self.console.print(
                    f"[DRY RUN] Would remove temporary directory: {self.settings.temp_dir}",
                    style="blue",
                )

        # Clean up downloaded archive
        if self.archive_name and os.path.isfile(f"/tmp/{self.archive_name}"):
            self.console.print(
                f"Cleaning up downloaded archive: /tmp/{self.archive_name}",
                style="blue",
            )
            if not self.dry_run:
                os.remove(f"/tmp/{self.archive_name}")
            else:
                self.console.print(
                    f"[DRY RUN] Would remove archive: /tmp/{self.archive_name}",
                    style="blue",
                )

        # Attempt to restart server if it was stopped by this script and is not currently running
        if self.server_stopped:
            try:
                service_name = self.settings.service_name
                if not self.is_service_active(service_name):
                    self.console.print(
                        f"Attempting to restart server ({service_name}) after script interruption/error...",
                        style="yellow",
                    )
                    if self.check_service_exists(service_name):
                        if self.run_systemctl("start", service_name):
                            self.console.print(
                                "Server restart command issued successfully.",
                                style="green",
                            )
                            self.log_message(
                                "INFO",
                                f"Server {service_name} restarted after script interruption.",
                            )
                        else:
                            self.console.print(
                                f"Failed to issue server restart command. Check status manually: systemctl status {service_name}.service",
                                style="red",
                            )
                            self.log_message(
                                "ERROR",
                                f"Failed to restart server {service_name} after script interruption.",
                            )
                    else:
                        self.console.print(
                            f"Service {service_name} does not exist. Cannot restart.",
                            style="yellow",
                        )
                        self.log_message(
                            "WARNING",
                            f"Cannot restart non-existent service {service_name}.",
                        )
            except Exception as e:
                self.log_message("ERROR", f"Error during cleanup restart attempt: {e}")

        return exit_code

    def is_service_active(self, service_name: str) -> bool:
        """Check if a systemd service is active"""
        try:
            result = subprocess.run(
                ["systemctl", "is-active", f"{service_name}.service"],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            return result.stdout.strip() == "active"
        except Exception:
            return False

    def compare_versions(self, ver1: str, ver2: str) -> str:
        """Compare two semantic version strings and return 'newer', 'older', or 'same'"""
        # Remove 'v' prefix if present
        ver1 = ver1[1:] if ver1.startswith("v") else ver1
        ver2 = ver2[1:] if ver2.startswith("v") else ver2

        try:
            v1 = version.parse(ver1)
            v2 = version.parse(ver2)

            if v1 > v2:
                return "newer"
            elif v1 < v2:
                return "older"
            else:
                return "same"
        except Exception as e:
            self.log_message("ERROR", f"Error comparing versions: {e}")
            # Default to 'same' on error
            return "same"

    def get_server_version(self) -> Optional[str]:
        """Attempt to get the server version"""
        dll_path = os.path.join(self.settings.server_dir, "VintagestoryServer.dll")
        if not os.path.isfile(dll_path):
            self.console.print(
                f"⚠ Server executable not found: {dll_path}", style="yellow"
            )
            return None

        if shutil.which("dotnet") is None:
            self.console.print(
                "⚠ Cannot check version: 'dotnet' command not found.", style="yellow"
            )
            return None

        try:
            # Change to server directory and run dotnet command
            current_dir = os.getcwd()
            os.chdir(self.settings.server_dir)
            result = subprocess.run(
                ["dotnet", "VintagestoryServer.dll", "--version"],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            os.chdir(current_dir)  # Change back to original directory

            if result.returncode != 0 or not result.stdout:
                self.console.print(
                    "⚠ Failed to get server version using --version flag (check permissions or dotnet install).",
                    style="yellow",
                )
                return None

            # Extract version using regex
            match = re.search(r"v?(\d+\.\d+\.\d+)", result.stdout)
            if match:
                version_str = match.group(0)
                # Ensure 'v' prefix for consistency
                if not version_str.startswith("v"):
                    version_str = f"v{version_str}"
                return version_str
            else:
                self.console.print(
                    f"⚠ Could not parse version from output: {result.stdout}",
                    style="yellow",
                )
                return None
        except Exception as e:
            self.console.print(f"⚠ Error getting server version: {e}", style="yellow")
            return None

    def verify_server_version(self, expected_version: str) -> bool:
        """Verify if the running server version matches the expected version"""
        expected_version_v = (
            f"v{expected_version}"
            if not expected_version.startswith("v")
            else expected_version
        )
        self.console.print(
            f"Verifying server version (expecting {expected_version_v})...",
            style="cyan",
        )

        # Try direct version check first
        installed_version = self.get_server_version()
        if installed_version:
            self.console.print(
                f"Detected server version via --version: {installed_version}",
                style="cyan",
            )
            if installed_version == expected_version_v:
                self.console.print(
                    f"✓ Server is running the expected version {installed_version}",
                    style="green",
                )
                return True
            else:
                self.console.print(
                    f"⚠ WARNING: Server reports version {installed_version}, but expected {expected_version_v}",
                    style="yellow",
                )
                self.console.print(
                    "  The update might not have fully applied or direct check is inaccurate. Will check logs.",
                    style="yellow",
                )
        else:
            self.console.print(
                "Could not get version via --version flag. Proceeding to log check.",
                style="yellow",
            )

        # Fallback: Check log file
        self.console.print(
            "Falling back to log file check for version verification...", style="yellow"
        )
        log_file = os.path.join(self.settings.data_dir, "Logs", "server-main.log")

        # Wait a moment for log file to potentially update
        import time

        time.sleep(2)

        if os.path.isfile(log_file):
            try:
                with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        if "Game Version: v" in line:
                            match = re.search(r"v\d+\.\d+\.\d+", line)
                            if match:
                                log_version = match.group(0)
                                self.console.print(
                                    f"Detected server version from log: {log_version}",
                                    style="cyan",
                                )
                                if log_version == expected_version_v:
                                    self.console.print(
                                        f"✓ Server log confirms expected version {log_version}",
                                        style="green",
                                    )
                                    return True
                                else:
                                    self.console.print(
                                        f"⚠ WARNING: Server log shows version {log_version}, but expected {expected_version_v}",
                                        style="yellow",
                                    )
                                    self.console.print(
                                        "  The update likely did not apply correctly.",
                                        style="yellow",
                                    )
                                    return False
                self.console.print(
                    f"⚠ Could not detect server version from log file ({log_file}). Verification incomplete.",
                    style="yellow",
                )
                return False
            except Exception as e:
                self.console.print(f"⚠ Error reading log file: {e}", style="yellow")
                return False
        else:
            self.console.print(
                f"⚠ Log file not found: {log_file}. Cannot verify version from log.",
                style="yellow",
            )
            return False

    def check_server_status(self) -> bool:
        """Check if the server is running after restart"""
        service_name = self.settings.service_name
        self.console.print(f"Checking server status ({service_name})...", style="cyan")

        # Check status up to 5 times
        for i in range(1, 6):
            import time

            time.sleep(3)
            if self.is_service_active(service_name):
                self.console.print("Server is running.", style="green")
                return True
            if i == 5:
                self.console.print(
                    "WARNING: Server did not report active status after 5 checks.",
                    style="red",
                )
                self.console.print(
                    f"Check status manually: systemctl status {service_name}.service",
                    style="yellow",
                )
                return False
            self.console.print(
                f"Waiting for server status (attempt {i} of 5)...", style="yellow"
            )

        # This should not be reached due to the i==5 check in the loop
        self.console.print(
            "Error: Loop finished unexpectedly in check_server_status.", style="red"
        )
        return False

    def create_backup(self, ignore_failure: bool) -> Optional[str]:
        """Create a backup of the data directory"""
        backup_timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_file = os.path.join(
            self.settings.backup_dir, f"vs_data_backup_{backup_timestamp}.tar.zst"
        )

        # Calculate size of data directory
        self.console.print(
            f"Calculating size of data directory ({self.settings.data_dir})...",
            style="cyan",
        )
        try:
            data_size = subprocess.run(
                ["du", "-sh", self.settings.data_dir],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            ).stdout.split()[0]
            self.console.print(f"Data size: {data_size}", style="yellow")
        except Exception:
            data_size = "N/A"
            self.console.print(f"Data size: {data_size}", style="yellow")

        self.console.print(f"Creating backup: {backup_file}", style="cyan")

        # Create backup directory if it doesn't exist
        self.run_mkdir(self.settings.backup_dir)

        if self.dry_run:
            self.console.print(
                f"[DRY RUN] Would create backup of {self.settings.data_dir} to {backup_file}",
                style="blue",
            )
            return "example_backup_path.tar.zst"

        try:
            # Create a temporary tar file
            temp_tar = tempfile.NamedTemporaryFile(delete=False, suffix=".tar")
            temp_tar.close()

            # Exclusion patterns
            exclude_patterns = [
                os.path.join(self.settings.data_dir, "Backups"),
                os.path.join(self.settings.data_dir, "BackupSave"),
                os.path.join(self.settings.data_dir, "Cache"),
                os.path.join(self.settings.data_dir, "Logs"),
            ]

            # Create tar archive
            with tarfile.open(temp_tar.name, "w") as tar:
                dir_name = os.path.basename(self.settings.data_dir)

                for root, dirs, files in os.walk(self.settings.data_dir):
                    # Check if this directory should be excluded
                    skip = False
                    for pattern in exclude_patterns:
                        if root.startswith(pattern):
                            skip = True
                            break
                    if skip:
                        continue

                    # Add files to tar archive
                    for file in files:
                        file_path = os.path.join(root, file)
                        arc_name = os.path.join(
                            dir_name,
                            os.path.relpath(file_path, self.settings.data_dir),
                        )
                        tar.add(file_path, arcname=arc_name)

            # Compress with zstd
            with open(temp_tar.name, "rb") as tar_file:
                with open(backup_file, "wb") as compressed_file:
                    compressor = zstandard.ZstdCompressor(level=9)
                    compressor.copy_stream(tar_file, compressed_file)

            # Clean up temporary tar file
            os.unlink(temp_tar.name)

            # Get backup size
            backup_size = subprocess.run(
                ["du", "-sh", backup_file],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            ).stdout.split()[0]

            self.console.print(
                f"Backup created successfully: {backup_file} ({backup_size})",
                style="green",
            )

            # Set ownership
            self.run_chown(
                f"{self.settings.server_user}:{self.settings.server_user}",
                backup_file,
            )

            # Rotate backups
            if self.settings.max_backups > 0:
                self._rotate_backups()

            return backup_file

        except Exception as e:
            self.log_message("ERROR", f"Backup creation failed: {e}")
            self.console.print(f"ERROR: Backup creation failed! {e}", style="red")

            # Clean up potentially incomplete backup file
            if os.path.exists(backup_file):
                os.remove(backup_file)

            if ignore_failure:
                self.console.print(
                    "Continuing despite backup failure (--ignore-backup-failure was specified)",
                    style="yellow",
                )
                return None
            else:
                self.console.print(
                    "To proceed without a backup, run with --skip-backup or --ignore-backup-failure",
                    style="yellow",
                )
                return None  # Signal failure

    def _rotate_backups(self):
        """Rotate backups keeping only the most recent ones according to MAX_BACKUPS setting"""
        self.console.print(
            f"Rotating backups (keeping {self.settings.max_backups} most recent)...",
            style="cyan",
        )

        # Get list of backups sorted by modification time (newest first)
        backups = []
        for f in os.listdir(self.settings.backup_dir):
            if f.startswith("vs_data_backup_") and f.endswith(".tar.zst"):
                full_path = os.path.join(self.settings.backup_dir, f)
                backups.append((os.path.getmtime(full_path), full_path))

        backups.sort(reverse=True)  # Sort newest first

        # Remove old backups beyond the limit
        old_backups = backups[self.settings.max_backups :]
        if old_backups:
            self.console.print(
                f"Removing {len(old_backups)} old backups...", style="cyan"
            )
            for _, backup_path in old_backups:
                os.remove(backup_path)
        else:
            self.console.print(
                f"No backups to rotate (total count <= {self.settings.max_backups}).",
                style="cyan",
            )

    def get_latest_version(self, channel: str = "stable") -> Optional[str]:
        """Get the latest available version from the API"""
        self.console.print(
            "Checking for latest version via official API...", style="cyan"
        )

        api_response = self._fetch_version_data_from_api()
        if not api_response:
            return None

        latest_version = self._extract_latest_version_from_response(
            api_response, channel
        )
        if not latest_version:
            self.console.print(
                f"Error: Could not determine latest {channel} version from API response",
                style="red",
            )
            return None

        # Verify that this version has a downloadable server package
        download_url = f"{self.settings.downloads_base_url}/{channel}/vs_server_linux-x64_{latest_version}.tar.gz"
        if not self._verify_download_url(download_url):
            return None

        self.console.print(f"✓ Verified download URL: {download_url}", style="green")
        return latest_version

    def _fetch_version_data_from_api(self) -> Optional[dict]:
        """Fetch version data from the API"""
        try:
            response = requests.get(self.settings.game_version_api_url, timeout=10)
            if response.status_code != 200:
                self.console.print(
                    f"Error: Could not connect to game versions API. Status code: {response.status_code}",
                    style="red",
                )
                self.console.print(
                    "Check your internet connection and try again.", style="yellow"
                )
                return None

            api_response = response.json()

            # Check if response contains gameversions
            if "gameversions" not in api_response:
                self.console.print("Error: Invalid response from API", style="red")
                return None

            self.console.print(
                "✓ Successfully retrieved version data from API", style="green"
            )
            return api_response
        except Exception as e:
            self.console.print(f"Error during version data fetch: {e}", style="red")
            return None

    def _extract_latest_version_from_response(
        self, api_response: dict, channel: str
    ) -> Optional[str]:
        """Extract the latest version from the API response"""
        try:
            # Extract versions
            versions = []
            for version_info in api_response["gameversions"]:
                if "name" in version_info:
                    version_name = version_info["name"]
                    # Filter out release candidates and pre-releases for stable channel
                    if channel == "stable" and (
                        "-rc" in version_name or "-pre" in version_name
                    ):
                        continue
                    versions.append(version_name)

            # Find the latest version using semantic versioning
            latest_stable = None
            latest_stable_without_v = None

            for version_str in versions:
                # Remove 'v' prefix for comparison
                version_without_v = (
                    version_str[1:] if version_str.startswith("v") else version_str
                )

                if latest_stable is None:
                    latest_stable = version_str
                    latest_stable_without_v = version_without_v
                else:
                    comparison = self.compare_versions(
                        f"v{latest_stable_without_v}", f"v{version_without_v}"
                    )
                    if comparison == "older":
                        latest_stable = version_str
                        latest_stable_without_v = version_without_v

            if latest_stable_without_v:
                self.console.print(
                    f"Latest {channel} version from API: {latest_stable} ({latest_stable_without_v})",
                    style="green",
                )
                return latest_stable_without_v
            return None
        except Exception as e:
            self.console.print(f"Error extracting version data: {e}", style="red")
            return None

    def _verify_download_url(self, download_url: str) -> bool:
        """Verify that the download URL is accessible"""
        try:
            response = requests.head(download_url, timeout=10)
            if response.status_code != 200:
                self.console.print(
                    f"Error: Found version via API, but no download available at {download_url}",
                    style="red",
                )
                return False
            return True
        except Exception as e:
            self.console.print(
                f"Error: Could not verify download URL: {e}", style="red"
            )
            return False

    def cmd_update(
        self,
        new_version: str,
        skip_backup: bool = False,
        ignore_backup_failure: bool = False,
    ) -> int:
        """Update the server to the specified version"""
        download_url = f"{self.settings.downloads_base_url}/stable/vs_server_linux-x64_{new_version}.tar.gz"
        self.archive_name = f"vs_server_linux-x64_{new_version}.tar.gz"

        self._display_update_intro(new_version)

        # Verify service and URL
        if not self._verify_service_and_url(new_version, download_url):
            return 1

        # Stop the server
        if not self._stop_server():
            return 1

        # Create backup if requested
        backup_file = self._handle_backup(skip_backup, ignore_backup_failure)
        if backup_file is None and not skip_backup and not ignore_backup_failure:
            return 1

        # Download and extract server files
        if not self._download_and_extract_files(download_url):
            return 1

        # Update server files
        if not self._update_server_files():
            return 1

        # Start server and verify
        if not self._start_and_verify_server(new_version):
            return 1

        # Convert empty string to None for consistency
        backup_result = backup_file if backup_file else None
        self._display_update_completion(
            new_version, backup_result, skip_backup, ignore_backup_failure
        )
        return 0

    def _display_update_intro(self, new_version: str):
        """Display initial update information"""
        self.console.print("=== Vintage Story Server Update ===", style="green")
        self.log_message("INFO", f"Starting update to version {new_version}")

        if self.dry_run:
            self.console.print(
                "[DRY RUN MODE] Simulating update without making changes", style="blue"
            )
            self.log_message("INFO", "Running in dry-run mode (simulation only)")

        self.console.print(f"Target version:   {new_version}", style="cyan")
        self.console.print(
            f"Server directory: {self.settings.server_dir}", style="cyan"
        )
        self.console.print(f"Data directory:   {self.settings.data_dir}", style="cyan")

    def _verify_service_and_url(self, new_version: str, download_url: str) -> bool:
        """Verify that the service exists and the download URL is accessible"""
        # Check service existence
        if not self.check_service_exists(self.settings.service_name):
            self.console.print(
                f"Error: Service {self.settings.service_name} does not exist. Please check the service name.",
                style="red",
            )
            self.log_message(
                "ERROR", f"Service {self.settings.service_name} does not exist."
            )
            return False

        self.log_message("INFO", f"Service {self.settings.service_name} exists.")

        # Verify download URL
        self.console.print(f"Verifying download URL: {download_url}", style="cyan")
        try:
            response = requests.head(download_url, timeout=10)
            if response.status_code != 200:
                self.console.print("Error: Could not access download URL.", style="red")
                self.console.print(
                    f"Check the version number ('{new_version}') and network connection.",
                    style="red",
                )
                self.log_message(
                    "ERROR", f"Failed to verify download URL: {download_url}"
                )
                return False
        except Exception as e:
            self.console.print(f"Error verifying download URL: {e}", style="red")
            self.log_message("ERROR", f"Failed to verify download URL: {download_url}")
            return False

        self.console.print("Download URL verified.", style="green")
        self.log_message("INFO", f"Download URL verified: {download_url}")
        return True

    def _stop_server(self) -> bool:
        """Stop the server service"""
        self.console.print(
            f"Stopping server ({self.settings.service_name})...", style="cyan"
        )
        if not self.run_systemctl("stop", self.settings.service_name):
            self.console.print("Error: Failed to stop the server service.", style="red")
            self.console.print(
                f"Check service status: systemctl status {self.settings.service_name}.service",
                style="yellow",
            )
            self.log_message(
                "ERROR", f"Failed to stop server service {self.settings.service_name}"
            )
            return False

        self.server_stopped = (
            True  # Mark that we stopped the server (for cleanup logic)
        )
        self.console.print("Server stopped.", style="green")
        return True

    def _handle_backup(
        self, skip_backup: bool, ignore_backup_failure: bool
    ) -> Optional[str]:
        """Handle the backup process according to settings"""
        backup_file = ""
        if skip_backup:
            self.console.print("Skipping backup as requested.", style="yellow")
            self.log_message(
                "INFO", "Backup creation skipped as requested (--skip-backup)"
            )
            return backup_file

        backup_result = self.create_backup(ignore_backup_failure)
        if backup_result is None and not ignore_backup_failure:
            # Backup failed and ignore_failure is false
            self.console.print("Update aborted due to backup failure.", style="red")
            self.log_message("ERROR", "Update aborted: backup creation failed")
            return None

        backup_file = backup_result if backup_result else ""
        if backup_file:
            self.console.print("Backup step completed successfully.", style="green")
            self.log_message("INFO", f"Backup created successfully at {backup_file}")
        elif ignore_backup_failure:
            self.console.print(
                "Backup failed, but continuing as --ignore-backup-failure was specified.",
                style="yellow",
            )
            self.log_message(
                "WARNING",
                "Backup creation failed but continuing (--ignore-backup-failure)",
            )

        return backup_file

    def _download_and_extract_files(self, download_url: str) -> bool:
        """Download and extract the server files"""
        # Download the server archive
        if not self._download_server_archive(download_url):
            return False

        # Extract files
        if not self._extract_server_archive():
            return False

        # Sanity check before modifying the server directory
        if (
            not os.path.isdir(self.settings.server_dir)
            or not self.settings.server_dir
            or self.settings.server_dir == "/"
        ):
            self.console.print(
                f"CRITICAL ERROR: Invalid SERVER_DIR defined: '{self.settings.server_dir}'. Aborting update to prevent data loss.",
                style="red",
            )
            return False

        return True

    def _download_server_archive(self, download_url: str) -> bool:
        """Download the server archive from the given URL"""
        self.console.print(f"Downloading {self.archive_name}...", style="cyan")
        if self.dry_run:
            self.console.print(
                f"[DRY RUN] Would download {download_url} to /tmp/{self.archive_name}",
                style="blue",
            )
            return True

        try:
            with requests.get(download_url, stream=True) as r:
                r.raise_for_status()
                total_size = int(r.headers.get("content-length", 0))

                with open(f"/tmp/{self.archive_name}", "wb") as f:
                    downloaded = 0
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            if total_size > 0:
                                percent = (downloaded / total_size) * 100
                                self.console.print(
                                    f"Progress: {percent:.1f}%", end="\r"
                                )
        except Exception as e:
            self.console.print(
                f"Error: Failed to download server files from {download_url}: {e}",
                style="red",
            )
            return False

        self.console.print(
            f"Download complete (/tmp/{self.archive_name}).", style="green"
        )
        return True

    def _extract_server_archive(self) -> bool:
        """Extract the server archive to the temporary directory"""
        self.console.print(
            f"Extracting files to {self.settings.temp_dir}...", style="cyan"
        )
        if self.dry_run:
            self.console.print(
                f"[DRY RUN] Would extract archive to {self.settings.temp_dir}",
                style="blue",
            )
            return True

        # Create empty temp directory
        os.makedirs(self.settings.temp_dir, exist_ok=True)
        for item in os.listdir(self.settings.temp_dir):
            item_path = os.path.join(self.settings.temp_dir, item)
            if os.path.isdir(item_path):
                shutil.rmtree(item_path)
            else:
                os.remove(item_path)

        # Extract archive
        try:
            with tarfile.open(f"/tmp/{self.archive_name}") as tar:
                tar.extractall(path=self.settings.temp_dir)
        except Exception as e:
            self.console.print(
                f"Error: Failed to extract server files from /tmp/{self.archive_name}: {e}",
                style="red",
            )
            return False

        self.console.print("Extraction complete.", style="green")
        return True

    def _update_server_files(self) -> bool:
        """Update the server files using rsync or fallback method"""
        self.console.print(
            f"Updating server files in {self.settings.server_dir}...", style="cyan"
        )

        if self.rsync_available:
            return self._update_with_rsync()
        else:
            return self._update_with_fallback()

    def _update_with_rsync(self) -> bool:
        """Update server files using rsync (preferred method)"""
        self.console.print("Using rsync for safe, precise updates...", style="green")
        if self.dry_run:
            self.console.print(
                f"[DRY RUN] Would rsync from {self.settings.temp_dir}/ to {self.settings.server_dir}/",
                style="blue",
            )
            return True

        try:
            subprocess.run(
                [
                    "rsync",
                    "-a",
                    "--delete",
                    "--exclude=serverconfig.json",
                    "--exclude=Mods/",
                    "--exclude=modconfig/",
                    f"{self.settings.temp_dir}/",
                    f"{self.settings.server_dir}/",
                ],
                check=True,
            )
            return True
        except Exception as e:
            self.console.print(
                f"Error: rsync failed to update server files in {self.settings.server_dir}: {e}",
                style="red",
            )
            return False

    def _update_with_fallback(self) -> bool:
        """Update server files using fallback method (when rsync is not available)"""
        self.console.print(
            "WARNING: Using fallback update method (rsync not available)",
            style="red",
        )
        self.console.print(
            "This method is less precise but has been improved for safety",
            style="red",
        )
        self.console.print(
            "It is still recommended to install rsync before proceeding.",
            style="yellow",
        )

        if not self.dry_run:
            self.console.print(
                "Do you want to continue with the fallback method? (y/N)",
                style="yellow",
            )
            response = input().lower()
            if response not in ("y", "yes"):
                self.console.print(
                    "Update aborted. Please install rsync and try again.",
                    style="cyan",
                )
                return False

        self.console.print(
            "Proceeding with improved fallback update method...", style="yellow"
        )

        # Create a timestamp for the temporary backup directory
        backup_timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        old_files_dir = os.path.join(
            self.settings.server_dir, f".old_update_files_{backup_timestamp}"
        )

        return self._move_files_to_backup(old_files_dir) and self._copy_new_files(
            old_files_dir
        )

    def _move_files_to_backup(self, old_files_dir: str) -> bool:
        """Move current server files to temporary backup location"""
        self.console.print(
            f"Moving current server files to temporary location: {old_files_dir}",
            style="blue",
        )
        if self.dry_run:
            self.console.print(
                f"[DRY RUN] Would move current server files to: {old_files_dir}",
                style="blue",
            )
            return True

        try:
            # Create backup directory
            os.makedirs(old_files_dir, exist_ok=True)

            # List of paths to preserve
            preserve_paths = [
                os.path.join(self.settings.server_dir, "serverconfig.json"),
                os.path.join(self.settings.server_dir, "Mods"),
                os.path.join(self.settings.server_dir, "modconfig"),
            ]

            # Move files except preserved ones
            for item in os.listdir(self.settings.server_dir):
                item_path = os.path.join(self.settings.server_dir, item)

                # Skip temporary backup directories and preserved paths
                if item.startswith(".old_update_files_") or any(
                    os.path.samefile(item_path, path)
                    if os.path.exists(path)
                    else item == os.path.basename(path)
                    for path in preserve_paths
                ):
                    continue

                # Move the item to backup directory
                shutil.move(item_path, os.path.join(old_files_dir, item))
            return True
        except Exception as e:
            self.console.print(
                f"Error: Failed to move server files to temporary location: {e}",
                style="red",
            )
            # Clean up the temporary directory
            shutil.rmtree(old_files_dir, ignore_errors=True)
            return False

    def _copy_new_files(self, old_files_dir: str) -> bool:
        """Copy new files from temporary directory to server directory"""
        self.console.print("Copying new server files...", style="blue")
        if self.dry_run:
            self.console.print(
                f"[DRY RUN] Would copy new server files from {self.settings.temp_dir} to {self.settings.server_dir}",
                style="blue",
            )
            self.console.print(
                "[DRY RUN] Would remove temporary backup directory after successful update",
                style="blue",
            )
            return True

        try:
            # Copy new files
            for item in os.listdir(self.settings.temp_dir):
                src_path = os.path.join(self.settings.temp_dir, item)
                dst_path = os.path.join(self.settings.server_dir, item)

                if os.path.isdir(src_path):
                    if os.path.exists(dst_path):
                        shutil.rmtree(dst_path)
                    shutil.copytree(src_path, dst_path)
                else:
                    shutil.copy2(src_path, dst_path)

            # If copy was successful, clean up the temporary backup
            self.console.print(
                "Update successful. Cleaning up temporary backup...", style="blue"
            )
            shutil.rmtree(old_files_dir)
            return True
        except Exception as e:
            self.console.print(
                f"Error: Failed to copy new server files: {e}", style="red"
            )
            self.console.print(
                "Attempting to restore from temporary backup...", style="yellow"
            )

            # Attempt to restore from the temporary backup
            try:
                for item in os.listdir(old_files_dir):
                    src_path = os.path.join(old_files_dir, item)
                    dst_path = os.path.join(self.settings.server_dir, item)

                    if os.path.isdir(src_path):
                        if os.path.exists(dst_path):
                            shutil.rmtree(dst_path)
                        shutil.copytree(src_path, dst_path)
                    else:
                        shutil.copy2(src_path, dst_path)
                self.console.print(
                    "Restored server files from temporary backup.",
                    style="green",
                )
            except Exception as restore_error:
                self.console.print(
                    f"CRITICAL: Failed to restore server files! Server may be in an inconsistent state: {restore_error}",
                    style="red",
                )
                self.console.print(
                    f"Manual intervention required. Backup files are at: {old_files_dir}",
                    style="red",
                )
            return False

    def _start_and_verify_server(self, new_version: str) -> bool:
        """Start the server and verify its status and version"""
        # Set ownership
        self.console.print(
            f"Setting ownership for {self.settings.server_dir} to {self.settings.server_user}:{self.settings.server_user}...",
            style="cyan",
        )
        self.run_chown(
            f"{self.settings.server_user}:{self.settings.server_user}",
            self.settings.server_dir,
            recursive=True,
        )
        self.console.print("Server files updated.", style="green")

        # Start the server
        self.console.print(
            f"Starting server ({self.settings.service_name})...", style="cyan"
        )
        if not self.run_systemctl("start", self.settings.service_name):
            self.console.print(
                "Error: Failed to start the server service after update.", style="red"
            )
            self.console.print(
                f"Check service status: systemctl status {self.settings.service_name}.service",
                style="yellow",
            )
            return False

        self.server_stopped = False  # Mark server as successfully started
        self.console.print("Server start command issued.", style="green")

        # Verify status and version after start
        status_ok = self.check_server_status()
        version_ok = self.verify_server_version(new_version)

        if not status_ok:
            self.console.print(
                "Warning: Server status check reported potential issues after start.",
                style="yellow",
            )

        if not version_ok:
            self.console.print(
                "Warning: Server version verification reported potential issues after start.",
                style="yellow",
            )

        # Return success even with warnings
        return True

    def _display_update_completion(
        self,
        new_version: str,
        backup_file: Optional[str],
        skip_backup: bool,
        ignore_backup_failure: bool,
    ):
        """Display update completion message"""
        self.console.print("=== Update process completed ===", style="green")
        self.console.print(
            f"Vintage Story server update process finished for version {new_version}",
            style="green",
        )

        if backup_file:
            self.console.print(f"Backup created at: {backup_file}", style="cyan")
        elif not skip_backup and ignore_backup_failure:
            self.console.print(
                "Reminder: Backup creation was attempted but failed (failure was ignored).",
                style="yellow",
            )

    def cmd_info(self, detailed: bool = False) -> int:
        """Display information about the current installation"""
        self.console.print("=== Vintage Story Server Information ===", style="green")

        # Check server status
        service_name = self.settings.service_name
        if self.is_service_active(service_name):
            self.console.print("Server Status:    Running", style="green")
        else:
            self.console.print("Server Status:    Stopped", style="yellow")

        # Get server version
        server_version = self.get_server_version()
        if server_version:
            self.console.print(f"Server Version:   {server_version}", style="green")
        else:
            self.console.print(
                "Server Version:   Unknown (could not determine)", style="yellow"
            )

        # Show directory paths
        self.console.print(f"Server Directory: {self.settings.server_dir}")
        self.console.print(f"Data Directory:   {self.settings.data_dir}")
        self.console.print(f"Backup Directory: {self.settings.backup_dir}")

        if detailed:
            self._display_detailed_info(service_name)

        return 0

    def _display_detailed_info(self, service_name: str):
        """Display detailed information about the server installation"""
        self.console.print("\n--- Detailed Information ---", style="cyan")

        # Server files size
        if os.path.isdir(self.settings.server_dir):
            try:
                server_size = subprocess.run(
                    ["du", "-sh", self.settings.server_dir],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                ).stdout.split()[0]
                self.console.print(f"Server files size:  {server_size}")
            except Exception:
                self.console.print("Server files size:  N/A")

        # Data directory size
        if os.path.isdir(self.settings.data_dir):
            try:
                data_size = subprocess.run(
                    ["du", "-sh", self.settings.data_dir],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                ).stdout.split()[0]
                self.console.print(f"Data directory size: {data_size}")
            except Exception:
                self.console.print("Data directory size: N/A")

        # Backup information
        if os.path.isdir(self.settings.backup_dir):
            backup_count = 0
            try:
                backup_files = [
                    f
                    for f in os.listdir(self.settings.backup_dir)
                    if f.startswith("vs_data_backup_") and f.endswith(".tar.zst")
                ]
                backup_count = len(backup_files)

                backup_size = subprocess.run(
                    ["du", "-sh", self.settings.backup_dir],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                ).stdout.split()[0]

                self.console.print(f"Backup count:       {backup_count}")
                self.console.print(f"Backup dir size:    {backup_size}")
            except Exception:
                self.console.print(f"Backup count:       {backup_count}")
                self.console.print("Backup dir size:    N/A")

        # Service status
        self._display_service_status(service_name)

    def _display_service_status(self, service_name: str):
        """Display the status of the server service"""
        self.console.print(f"\n--- Service Status ({service_name}) ---", style="cyan")
        try:
            result = subprocess.run(
                ["systemctl", "status", f"{service_name}.service", "--no-pager"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            # Show first few lines
            status_lines = result.stdout.splitlines()[:3]
            for line in status_lines:
                self.console.print(line)
        except Exception:
            self.console.print(
                "Could not retrieve service status (service might not exist or permissions issue).",
                style="yellow",
            )

    def cmd_check_version(self, channel: str = "stable") -> int:
        """Check if a new version of Vintage Story is available"""
        self.console.print("=== Vintage Story Version Check ===", style="green")
        self.console.print(
            f"Checking for latest available version in the {channel} channel...",
            style="cyan",
        )

        # Get current version
        current_version = self.get_server_version()
        if not current_version:
            self.console.print(
                "Version comparison will not be available.", style="yellow"
            )
            current_version = "unknown"
        else:
            self.console.print(f"Current server version: {current_version}")

        # Get latest version
        latest_version = self.get_latest_version(channel)
        if not latest_version:
            self.console.print(
                "Error: Could not determine latest available version.", style="red"
            )
            self.console.print(
                "Check your internet connection and try again.", style="yellow"
            )
            return 1

        latest_version_v = f"v{latest_version}"
        self.console.print(f"Latest available version: {latest_version_v}")

        # Compare versions
        if current_version != "unknown":
            self._display_version_comparison(current_version, latest_version_v, channel)

        # Final sanity check on download URL
        self._verify_update_url(latest_version, channel)

        return 0

    def _display_version_comparison(
        self, current_version: str, latest_version: str, channel: str
    ):
        """Display the comparison between current and latest versions"""
        comparison = self.compare_versions(current_version, latest_version)

        if comparison == "newer":
            self.console.print(
                f"✓ Your server is running a newer version than the latest {channel} release.",
                style="green",
            )
        elif comparison == "same":
            self.console.print(
                f"✓ Your server is up to date with the latest {channel} release.",
                style="green",
            )
        else:  # older
            self.console.print(
                "! A newer version is available. Consider updating your server.",
                style="yellow",
            )
            # Extract version number without 'v' prefix
            latest_version_no_prefix = (
                latest_version[1:] if latest_version.startswith("v") else latest_version
            )
            self.console.print(
                f"Update command: python main.py update {latest_version_no_prefix}",
                style="cyan",
            )

    def _verify_update_url(self, version: str, channel: str):
        """Verify the update URL is accessible"""
        update_url = f"{self.settings.downloads_base_url}/{channel}/vs_server_linux-x64_{version}.tar.gz"
        try:
            response = requests.head(update_url, timeout=10)
            if response.status_code == 200:
                self.console.print(
                    f"✓ Update file URL verified: {update_url}", style="green"
                )
            else:
                self.console.print(
                    f"⚠ Warning: Could not confirm availability of update file URL: {update_url}",
                    style="yellow",
                )
        except Exception:
            self.console.print(
                f"⚠ Warning: Could not confirm availability of update file URL: {update_url}",
                style="yellow",
            )

    def generate_config_file(self):
        """Generate a configuration file in accordance with XDG standards"""
        # Determine the appropriate config location
        config_dir = os.path.dirname(XDG_CONFIG_PATH)
        config_file = XDG_CONFIG_PATH

        # Create the config directory if it doesn't exist
        if not os.path.exists(config_dir):
            try:
                os.makedirs(config_dir, exist_ok=True)
                self.console.print(
                    f"Created configuration directory: {config_dir}", style="cyan"
                )
            except Exception as e:
                self.console.print(
                    f"Error creating directory {config_dir}: {e}", style="red"
                )
                self.console.print("Falling back to current directory", style="yellow")
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

            self.console.print(
                f"Configuration file created: {config_file}", style="green"
            )
            self.console.print(
                "This file will be loaded automatically on next run.", style="cyan"
            )
        except Exception as e:
            self.console.print(f"Error creating configuration file: {e}", style="red")


def main():
    vs_mgr = VSServerManager()
    result = 0

    # Load configuration
    vs_mgr.load_config()

    # Setup argument parser
    parser = argparse.ArgumentParser(
        description="Vintage Story Server Management Script",
        epilog="For command-specific help, use: %(prog)s <command> --help",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate operations without making changes",
    )
    parser.add_argument(
        "--generate-config",
        action="store_true",
        help="Generate a sample configuration file",
    )

    # Create subparsers for commands
    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # 'update' command
    update_parser = subparsers.add_parser(
        "update", help="Update the server to a specific version"
    )
    update_parser.add_argument(
        "version", help="Version number to update to (format X.Y.Z)"
    )
    update_parser.add_argument(
        "--skip-backup", action="store_true", help="Skip creating a backup"
    )
    update_parser.add_argument(
        "--ignore-backup-failure",
        action="store_true",
        help="Continue even if backup fails",
    )
    update_parser.add_argument(
        "--max-backups",
        type=int,
        help=f"Number of backups to keep (default: {vs_mgr.settings.max_backups})",
    )

    # 'info' command
    info_parser = subparsers.add_parser(
        "info", help="Display information about the current installation"
    )
    info_parser.add_argument(
        "--detailed", action="store_true", help="Show additional server information"
    )

    # 'check-version' command
    check_version_parser = subparsers.add_parser(
        "check-version", help="Check for available updates"
    )
    check_version_parser.add_argument(
        "--channel",
        choices=["stable", "unstable"],
        default="stable",
        help="Check for versions in the specified channel (default: stable)",
    )

    # Parse arguments
    args = parser.parse_args()

    # Handle --dry-run
    if args.dry_run:
        vs_mgr.dry_run = True
        vs_mgr.setup_logging()  # Reinitialize logging with dry-run setting

    # Handle --generate-config
    if args.generate_config:
        vs_mgr.generate_config_file()
        return 0

    # Check dependencies
    if not vs_mgr.check_dependencies():
        return 1

    # Set up signal handlers for cleanup
    def signal_handler(sig, frame):
        vs_mgr.console.print(
            "Received interrupt signal, cleaning up...", style="yellow"
        )
        sys.exit(vs_mgr.cleanup(1))

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        # Handle command execution
        if args.command == "update":
            if not re.match(r"^\d+\.\d+\.\d+$", args.version):
                vs_mgr.console.print(
                    f"Error: Invalid version format: '{args.version}'. Expected format X.Y.Z",
                    style="red",
                )
                result = 1
            else:
                # Update max_backups if specified
                if args.max_backups is not None:
                    vs_mgr.settings.max_backups = args.max_backups

                result = vs_mgr.cmd_update(
                    args.version, args.skip_backup, args.ignore_backup_failure
                )
        elif args.command == "info":
            result = vs_mgr.cmd_info(args.detailed)
        elif args.command == "check-version":
            result = vs_mgr.cmd_check_version(args.channel)
        elif args.command is None:
            parser.print_help()
            result = 0
        else:
            vs_mgr.console.print(f"Unknown command: {args.command}", style="red")
            parser.print_help()
            result = 1
    except Exception as e:
        vs_mgr.log_message("ERROR", f"An unexpected error occurred: {e}")
        import traceback

        vs_mgr.log_message("ERROR", traceback.format_exc())
        result = 1
    finally:
        vs_mgr.cleanup(result)

    return result


if __name__ == "__main__":
    sys.exit(main())
