import argparse
import re
import signal
import sys
import subprocess
import requests

# Import our modules
from config import ConfigManager, ServerSettings
from errors import ConfigError, DependencyError, VSManagerError
from ui import ConsoleManager
from system import SystemInterface
from services import ServiceManager
from versioning import VersionChecker
from backup import BackupManager
from updater import UpdateManager

# Import interface implementations
from process_runner import SubprocessProcessRunner
from filesystem import OsFileSystem, IFileSystem
from http_client import RequestsHttpClient
from archiver import TarfileArchiver
from compressor import ZstdCompressor


def main():
    # Initialize Console Manager FIRST, but without log dir yet
    console_mgr = ConsoleManager()

    # Set up argument parser
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
        help="Number of backups to keep (default: 10)",
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
    dry_run = args.dry_run
    console_mgr.dry_run = dry_run

    # Initialize Config Manager AFTER console manager
    config_mgr = ConfigManager(console=console_mgr)

    # Handle --generate-config before loading settings or setting up full logging
    if args.generate_config:
        config_mgr.generate_config_file()
        console_mgr.info("Sample configuration file generated. Exiting.")
        return 0

    # Load configuration
    try:
        settings = config_mgr.load_config()
    except ConfigError as e:
        console_mgr.critical(f"Failed to load configuration: {e}")
        return 1
    except VSManagerError as e:
        console_mgr.critical(f"An unexpected error occurred during configuration: {e}")
        return 1

    # Now setup full logging with the directory from settings
    console_mgr.setup_logging(log_dir=settings.log_dir)

    # Initialize interfaces
    process_runner = SubprocessProcessRunner()
    http_client = RequestsHttpClient()
    filesystem = OsFileSystem(process_runner=process_runner)
    archiver = TarfileArchiver()
    compressor = ZstdCompressor()

    # Initialize system interface
    system = SystemInterface(
        console=console_mgr,
        process_runner=process_runner,
        filesystem=filesystem,
        dry_run=dry_run,
    )

    # Initialize other managers
    service_mgr = ServiceManager(
        system_interface=system,
        process_runner=process_runner,
        console=console_mgr,
    )

    version_checker = VersionChecker(
        server_dir=settings.server_dir,
        http_client=http_client,
        console=console_mgr,
        settings=settings,
    )

    backup_mgr = BackupManager(
        system_interface=system,
        filesystem=filesystem,
        archiver=archiver,
        compressor=compressor,
        console=console_mgr,
        settings=settings,
    )

    update_mgr = UpdateManager(
        system_interface=system,
        service_manager=service_mgr,
        backup_manager=backup_mgr,
        version_checker=version_checker,
        http_client=http_client,
        filesystem=filesystem,
        archiver=archiver,
        console=console_mgr,
        settings=settings,
    )

    # Check dependencies
    if not check_dependencies(system, console_mgr):
        return 1

    # Set up signal handlers for cleanup
    def signal_handler(sig, frame):
        console_mgr.warning("Received interrupt signal, attempting cleanup...")
        if "update_mgr" in locals():
            update_mgr._cleanup()
        sys.exit(1)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    result = 0

    try:
        # Handle command execution
        if args.command == "update":
            if not re.match(r"^\d+\.\d+\.\d+$", args.version):
                console_mgr.print(
                    f"Error: Invalid version format: '{args.version}'. Expected format X.Y.Z",
                    style="red",
                )
                result = 1
            else:
                # Update max_backups if specified
                if args.max_backups is not None:
                    settings.max_backups = args.max_backups

                # Execute update
                success, _ = update_mgr.perform_update(
                    args.version, args.skip_backup, args.ignore_backup_failure
                )
                result = 0 if success else 1

        elif args.command == "info":
            # Show server information
            result = cmd_info(
                console_mgr,
                settings,
                service_mgr,
                version_checker,
                backup_mgr,
                filesystem,
                args.detailed,
            )

        elif args.command == "check-version":
            # Check for version updates
            result = cmd_check_version(console_mgr, version_checker, args.channel)

        elif args.command is None:
            parser.print_help()
            result = 0

        else:
            console_mgr.error(f"Unknown command: {args.command}")
            parser.print_help()
            result = 1

    except VSManagerError as e:
        console_mgr.error(f"Operation failed: {e}", exc_info=False)
        result = 1
    except Exception as e:
        console_mgr.exception(f"An unexpected error occurred: {e}")
        result = 1

    finally:
        console_mgr.info("Performing final cleanup...")
        if "update_mgr" in locals():
            update_mgr._cleanup()

    return result


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
    settings: ServerSettings,
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
    settings: ServerSettings,
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


if __name__ == "__main__":
    sys.exit(main())
