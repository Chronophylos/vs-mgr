import re
import subprocess
import requests
from typing import Tuple

from vs_manager.errors import DependencyError
from vs_manager.ui import ConsoleManager
from vs_manager.system import SystemInterface
from vs_manager.services import ServiceManager
from vs_manager.versioning import VersionChecker
from vs_manager.backup import BackupManager
from vs_manager.filesystem import IFileSystem


def check_dependencies(system: SystemInterface, console: ConsoleManager) -> bool:
    """Check if required dependencies are installed"""
    critical_deps = ["wget", "tar", "systemctl"]
    recommended_deps = ["rsync"]
    missing_critical = []
    missing_recommended = []

    console.info("Checking dependencies...")

    # Check for critical dependencies
    for dep in critical_deps:
        if system.which(dep) is None:
            missing_critical.append(dep)

    if missing_critical:
        console.critical(
            f"Missing critical dependencies: {', '.join(missing_critical)}. Please install them and retry."
        )
        raise DependencyError(
            f"Missing critical dependencies: {', '.join(missing_critical)}"
        )

    # Check for recommended dependencies
    for dep in recommended_deps:
        if system.which(dep) is None:
            missing_recommended.append(dep)

    if missing_recommended:
        console.warning(
            f"Missing recommended dependencies: {', '.join(missing_recommended)}. Some features might be slower or unavailable (e.g., rsync for updates)."
        )

    console.info("Dependency check passed.")
    return True


def cmd_info(
    console: ConsoleManager,
    settings,
    service_mgr: ServiceManager,
    version_checker: VersionChecker,
    backup_manager: BackupManager,
    filesystem: IFileSystem,
    detailed: bool = False,
) -> int:
    """Display information about the current installation"""
    console.info("Gathering server information...")
    console.print("=== Vintage Story Server Information ===", style="green")

    # Check server status
    service_name = settings.service_name
    if service_mgr.is_service_active(service_name):
        console.print("Server Status:    Running", style="green")
    else:
        console.print("Server Status:    Stopped", style="yellow")

    # Get server version
    server_version = version_checker.get_server_version()
    if server_version:
        console.print(f"Server Version:   {server_version}", style="green")
    else:
        console.print("Server Version:   Unknown (could not determine)", style="yellow")

    # Show directory paths
    console.print(f"Server Directory: {settings.server_dir}")
    console.print(f"Data Directory:   {settings.data_dir}")
    console.print(f"Backup Directory: {settings.backup_dir}")

    if detailed:
        _display_detailed_info(console, settings, filesystem, backup_manager)

    return 0


def _display_detailed_info(
    console: ConsoleManager,
    settings,
    filesystem: IFileSystem,
    backup_manager: BackupManager,
):
    """Display detailed information about the server installation"""
    console.print("\n--- Detailed Information ---", style="cyan")

    # Server files size
    if filesystem.isdir(settings.server_dir):
        try:
            server_size_bytes = filesystem.calculate_dir_size(settings.server_dir)
            server_size_human = backup_manager._format_size(server_size_bytes)
            console.print(f"Server files size:  {server_size_human}")
        except Exception as e:
            console.warning(f"Could not calculate server directory size: {e}")
            console.print("Server files size:  N/A")
    else:
        console.print("Server files size:  N/A (Directory not found)")

    # Data directory size
    if filesystem.isdir(settings.data_dir):
        try:
            data_size_bytes = filesystem.calculate_dir_size(settings.data_dir)
            data_size_human = backup_manager._format_size(data_size_bytes)
            console.print(f"Data directory size: {data_size_human}")
        except Exception as e:
            console.warning(f"Could not calculate data directory size: {e}")
            console.print("Data directory size: N/A")
    else:
        console.print("Data directory size: N/A (Directory not found)")

    # Backup information (using BackupManager)
    if filesystem.isdir(settings.backup_dir):
        try:
            backup_details = backup_manager.list_backups()
            backup_count = len(backup_details)
            # Calculate total backup dir size using filesystem interface
            backup_dir_size_bytes = filesystem.calculate_dir_size(settings.backup_dir)
            backup_dir_size_human = backup_manager._format_size(backup_dir_size_bytes)

            console.print(f"Backup count:       {backup_count}")
            console.print(f"Backup dir size:    {backup_dir_size_human}")
            # Optionally display last backup details
            if backup_details:
                latest_backup = backup_details[0]
                console.print(
                    f"Latest Backup:      {latest_backup[0]} ({latest_backup[2]})"
                )

        except Exception as e:
            console.warning(f"Could not retrieve backup information: {e}")
            console.print("Backup count:       N/A")
            console.print("Backup dir size:    N/A")
    else:
        console.print("Backup count:       0 (Directory not found)")
        console.print("Backup dir size:    N/A")

    # Service status
    _display_service_status(console, settings.service_name)


def _display_service_status(console: ConsoleManager, service_name: str):
    """Display the status of the server service"""
    console.print(f"\n--- Service Status ({service_name}) ---", style="cyan")
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
            console.print(line)
    except Exception:
        console.print(
            "Could not retrieve service status (service might not exist or permissions issue).",
            style="yellow",
        )


def cmd_check_version(
    console: ConsoleManager, version_checker: VersionChecker, channel: str = "stable"
) -> int:
    """Check if a new version of Vintage Story is available"""
    console.info(f"Checking for latest version on channel '{channel}'...")
    console.print("=== Vintage Story Version Check ===", style="green")
    console.print(
        f"Checking for latest available version in the {channel} channel...",
        style="cyan",
    )

    # Get current version
    current_version = version_checker.get_server_version()
    if not current_version:
        console.print("Version comparison will not be available.", style="yellow")
        current_version = "unknown"
    else:
        console.print(f"Current server version: {current_version}")

    # Get latest version
    latest_version = version_checker.get_latest_version(channel)
    if not latest_version:
        console.print(
            "Error: Could not determine latest available version.", style="red"
        )
        console.print("Check your internet connection and try again.", style="yellow")
        return 1

    latest_version_v = f"v{latest_version}"
    console.print(f"Latest available version: {latest_version_v}")

    # Compare versions
    if current_version != "unknown":
        _display_version_comparison(
            console, version_checker, current_version, latest_version_v, channel
        )

    # Final sanity check on download URL
    _verify_update_url(console, version_checker, latest_version, channel)

    return 0


def _display_version_comparison(
    console: ConsoleManager,
    version_checker: VersionChecker,
    current_version: str,
    latest_version: str,
    channel: str,
):
    """Display the comparison between current and latest versions"""
    comparison = version_checker.compare_versions(current_version, latest_version)

    if comparison == "newer":
        console.print(
            f"✓ Your server is running a newer version than the latest {channel} release.",
            style="green",
        )
    elif comparison == "same":
        console.print(
            f"✓ Your server is up to date with the latest {channel} release.",
            style="green",
        )
    else:  # older
        console.print(
            "! A newer version is available. Consider updating your server.",
            style="yellow",
        )
        # Extract version number without 'v' prefix
        latest_version_no_prefix = (
            latest_version[1:] if latest_version.startswith("v") else latest_version
        )
        console.print(
            f"Update command: python main.py update {latest_version_no_prefix}",
            style="cyan",
        )


def _verify_update_url(
    console: ConsoleManager, version_checker: VersionChecker, version: str, channel: str
):
    """Verify the update URL is accessible"""
    update_url = f"{version_checker.downloads_base_url}/{channel}/vs_server_linux-x64_{version}.tar.gz"
    try:
        response = requests.head(update_url, timeout=10)
        if response.status_code == 200:
            console.print(f"✓ Update file URL verified: {update_url}", style="green")
        else:
            console.print(
                f"⚠ Warning: Could not confirm availability of update file URL: {update_url}",
                style="yellow",
            )
    except Exception:
        console.print(
            f"⚠ Warning: Could not confirm availability of update file URL: {update_url}",
            style="yellow",
        )


def perform_update(
    update_mgr, version: str, skip_backup: bool, ignore_backup_failure: bool
) -> Tuple[bool, int]:
    """Execute the update process"""
    if not re.match(r"^\d+\.\d+\.\d+$", version):
        update_mgr.console.print(
            f"Error: Invalid version format: '{version}'. Expected format X.Y.Z",
            style="red",
        )
        return False, 1

    # Execute update
    success, _ = update_mgr.perform_update(version, skip_backup, ignore_backup_failure)
    return success, 0 if success else 1
