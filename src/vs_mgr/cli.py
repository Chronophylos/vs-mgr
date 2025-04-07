import argparse


def setup_argument_parser():
    """Set up and return the argument parser for the command-line interface"""
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

    return parser
