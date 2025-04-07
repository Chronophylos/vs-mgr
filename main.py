import signal
import sys

# Import our modules
from vs_manager.config import ConfigManager
from vs_manager.errors import ConfigError, VSManagerError
from vs_manager.ui import ConsoleManager
from vs_manager.system import SystemInterface
from vs_manager.services import ServiceManager
from vs_manager.versioning import VersionChecker
from vs_manager.backup import BackupManager
from vs_manager.updater import UpdateManager

# Import interface implementations
from vs_manager.process_runner import SubprocessProcessRunner
from vs_manager.filesystem import OsFileSystem
from vs_manager.http_client import RequestsHttpClient
from vs_manager.archiver import TarfileArchiver
from vs_manager.compressor import ZstdCompressor

# Import command handlers
from vs_manager.commands import (
    check_dependencies,
    cmd_info,
    cmd_check_version,
    perform_update,
)
from vs_manager.cli import setup_argument_parser


def main():
    # Initialize Console Manager
    console_mgr = ConsoleManager()

    # Parse command line arguments
    parser = setup_argument_parser()
    args = parser.parse_args()

    # Handle dry run
    dry_run = args.dry_run
    console_mgr.dry_run = dry_run

    # Initialize Config Manager
    config_mgr = ConfigManager(console=console_mgr)

    # Handle --generate-config
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

    # Setup full logging with directory from settings
    console_mgr.setup_logging(log_dir=settings.log_dir)

    # Initialize interfaces and components
    components = initialize_components(console_mgr, settings, dry_run)

    # Check dependencies
    if not check_dependencies(components["system"], console_mgr):
        return 1

    # Set up signal handlers
    setup_signal_handlers(components.get("update_mgr"))

    # Process commands
    return process_command(args, components, settings)


def initialize_components(console_mgr, settings, dry_run):
    """Initialize all system components and interfaces"""
    # Core interfaces
    process_runner = SubprocessProcessRunner()
    http_client = RequestsHttpClient()
    filesystem = OsFileSystem(process_runner=process_runner)
    archiver = TarfileArchiver()
    compressor = ZstdCompressor()

    # System interface
    system = SystemInterface(
        console=console_mgr,
        process_runner=process_runner,
        filesystem=filesystem,
        dry_run=dry_run,
    )

    # Service managers
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

    return {
        "system": system,
        "service_mgr": service_mgr,
        "version_checker": version_checker,
        "backup_mgr": backup_mgr,
        "update_mgr": update_mgr,
        "filesystem": filesystem,
        "console": console_mgr,
    }


def setup_signal_handlers(update_mgr):
    """Set up signal handlers for graceful cleanup"""

    def signal_handler(sig, frame):
        print("Received interrupt signal, attempting cleanup...")
        if update_mgr:
            update_mgr._cleanup()
        sys.exit(1)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)


def process_command(args, components, settings):
    """Process the command specified in args"""
    console = components["console"]
    result = 0

    try:
        if args.command == "update":
            # Update max_backups if specified
            if args.max_backups is not None:
                settings.max_backups = args.max_backups

            result = perform_update(
                components["update_mgr"],
                args.version,
                args.skip_backup,
                args.ignore_backup_failure,
            )

        elif args.command == "info":
            result = cmd_info(
                console,
                settings,
                components["service_mgr"],
                components["version_checker"],
                components["backup_mgr"],
                components["filesystem"],
                args.detailed,
            )

        elif args.command == "check-version":
            result = cmd_check_version(
                console, components["version_checker"], args.channel
            )

        elif args.command is None:
            setup_argument_parser().print_help()

        else:
            console.error(f"Unknown command: {args.command}")
            setup_argument_parser().print_help()
            result = 1

    except VSManagerError as e:
        console.error(f"Operation failed: {e}", exc_info=False)
        result = 1
    except Exception as e:
        console.exception(f"An unexpected error occurred: {e}")
        result = 1
    finally:
        console.info("Performing final cleanup...")
        if "update_mgr" in components:
            components["update_mgr"]._cleanup()

    return result


if __name__ == "__main__":
    sys.exit(main())
