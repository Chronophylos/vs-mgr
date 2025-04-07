import os
import datetime
import tempfile
import tarfile
import zstandard
import subprocess
from typing import Optional, List

from interfaces import IFileSystem, IArchiver, ICompressor


class BackupManager:
    """Manages backup creation and rotation for Vintage Story server data"""

    def __init__(
        self,
        system_interface,
        filesystem: Optional[IFileSystem] = None,
        archiver: Optional[IArchiver] = None,
        compressor: Optional[ICompressor] = None,
        console=None,
        settings=None,
    ):
        """Initialize BackupManager

        Args:
            system_interface: SystemInterface instance for system operations
            filesystem: IFileSystem implementation for filesystem operations
            archiver: IArchiver implementation for archive operations
            compressor: ICompressor implementation for compression operations
            console: ConsoleManager instance for output (optional)
            settings: ServerSettings instance with configuration (optional)
        """
        self.system = system_interface
        self.filesystem = filesystem
        self.archiver = archiver
        self.compressor = compressor
        self.console = console
        self.settings = settings

        # Set default backup directory if not provided in settings
        self.backup_dir = settings.backup_dir if settings else "/srv/gameserver/backups"
        self.data_dir = settings.data_dir if settings else "/srv/gameserver/data/vs"
        self.max_backups = settings.max_backups if settings else 10
        self.server_user = settings.server_user if settings else "gameserver"

    def create_backup(self, ignore_failure: bool = False) -> Optional[str]:
        """Create a backup of the data directory

        Args:
            ignore_failure: Whether to ignore failures and continue

        Returns:
            Optional[str]: Path to the created backup file or None if failed
        """
        backup_timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_file = os.path.join(
            self.backup_dir, f"vs_data_backup_{backup_timestamp}.tar.zst"
        )

        # Calculate size of data directory
        if self.console:
            self.console.print(
                f"Calculating size of data directory ({self.data_dir})...",
                style="cyan",
            )
        try:
            if self.filesystem:
                data_size = self.filesystem.calculate_dir_size(self.data_dir)
                data_size_human = self._format_size(data_size)
            else:
                data_size = subprocess.run(
                    ["du", "-sh", self.data_dir],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                ).stdout.split()[0]
                data_size_human = data_size

            if self.console:
                self.console.print(f"Data size: {data_size_human}", style="yellow")
        except Exception:
            data_size_human = "N/A"
            if self.console:
                self.console.print(f"Data size: {data_size_human}", style="yellow")

        if self.console:
            self.console.print(f"Creating backup: {backup_file}", style="cyan")

        # Create backup directory if it doesn't exist
        if self.filesystem:
            self.filesystem.mkdir(self.backup_dir, exist_ok=True)
        else:
            self.system.run_mkdir(self.backup_dir)

        if self.system.dry_run:
            if self.console:
                self.console.print(
                    f"[DRY RUN] Would create backup of {self.data_dir} to {backup_file}",
                    style="blue",
                )
            return "example_backup_path.tar.zst"

        try:
            # Exclusion patterns
            exclude_patterns = [
                os.path.join(self.data_dir, "Backups"),
                os.path.join(self.data_dir, "BackupSave"),
                os.path.join(self.data_dir, "Cache"),
                os.path.join(self.data_dir, "Logs"),
            ]

            # If we have the archiver and compressor interfaces, use them
            if self.archiver and self.compressor:
                # Create a temporary tar file
                temp_tar = tempfile.NamedTemporaryFile(delete=False, suffix=".tar")
                temp_tar.close()

                # Create tar archive
                relative_exclude = [
                    os.path.relpath(p, self.data_dir) for p in exclude_patterns
                ]
                self.archiver.create(
                    self.data_dir, temp_tar.name, exclude_patterns=relative_exclude
                )

                # Compress with zstd
                self.compressor.compress(temp_tar.name, backup_file)

                # Clean up temporary tar file
                if self.filesystem:
                    self.filesystem.remove(temp_tar.name)
                else:
                    os.unlink(temp_tar.name)
            else:
                # Create a temporary tar file
                temp_tar = tempfile.NamedTemporaryFile(delete=False, suffix=".tar")
                temp_tar.close()

                # Create tar archive using tarfile directly
                with tarfile.open(temp_tar.name, "w") as tar:
                    dir_name = os.path.basename(self.data_dir)

                    for root, dirs, files in os.walk(self.data_dir):
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
                                os.path.relpath(file_path, self.data_dir),
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
            if self.filesystem:
                backup_size = self.filesystem.getsize(backup_file)
                backup_size_human = self._format_size(backup_size)
            else:
                backup_size_human = subprocess.run(
                    ["du", "-sh", backup_file],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                ).stdout.split()[0]

            if self.console:
                self.console.print(
                    f"Backup created successfully: {backup_file} ({backup_size_human})",
                    style="green",
                )

            # Set ownership
            if self.filesystem:
                user, group = self.server_user, self.server_user
                self.filesystem.chown(backup_file, user, group)
            else:
                self.system.run_chown(
                    f"{self.server_user}:{self.server_user}",
                    backup_file,
                )

            # Rotate backups
            if self.max_backups > 0:
                self._rotate_backups()

            return backup_file

        except Exception as e:
            if self.console:
                self.console.log_message("ERROR", f"Backup creation failed: {e}")
                self.console.print(f"ERROR: Backup creation failed! {e}", style="red")

            # Clean up potentially incomplete backup file
            if self.filesystem and self.filesystem.exists(backup_file):
                self.filesystem.remove(backup_file)
            elif os.path.exists(backup_file):
                os.remove(backup_file)

            if ignore_failure:
                if self.console:
                    self.console.print(
                        "Continuing despite backup failure (--ignore-backup-failure was specified)",
                        style="yellow",
                    )
                return None
            else:
                if self.console:
                    self.console.print(
                        "To proceed without a backup, run with --skip-backup or --ignore-backup-failure",
                        style="yellow",
                    )
                return None  # Signal failure

    def _rotate_backups(self) -> None:
        """Rotate backups keeping only the most recent ones according to max_backups setting"""
        if self.console:
            self.console.print(
                f"Rotating backups (keeping {self.max_backups} most recent)...",
                style="cyan",
            )

        # Get list of backups sorted by modification time (newest first)
        backups = []

        if self.filesystem:
            files = self.filesystem.listdir(self.backup_dir)
            for f in files:
                if f.startswith("vs_data_backup_") and f.endswith(".tar.zst"):
                    full_path = os.path.join(self.backup_dir, f)
                    backups.append((self.filesystem.getmtime(full_path), full_path))
        else:
            for f in os.listdir(self.backup_dir):
                if f.startswith("vs_data_backup_") and f.endswith(".tar.zst"):
                    full_path = os.path.join(self.backup_dir, f)
                    backups.append((os.path.getmtime(full_path), full_path))

        backups.sort(reverse=True)  # Sort newest first

        # Remove old backups beyond the limit
        old_backups = backups[self.max_backups :]
        if old_backups:
            if self.console:
                self.console.print(
                    f"Removing {len(old_backups)} old backups...", style="cyan"
                )
            for _, backup_path in old_backups:
                if self.system.dry_run:
                    if self.console:
                        self.console.print(
                            f"[DRY RUN] Would remove old backup: {backup_path}",
                            style="blue",
                        )
                else:
                    try:
                        if self.filesystem:
                            self.filesystem.remove(backup_path)
                        else:
                            os.remove(backup_path)

                        if self.console:
                            self.console.print(f"Removed old backup: {backup_path}")
                    except Exception as e:
                        if self.console:
                            self.console.log_message(
                                "ERROR",
                                f"Failed to remove old backup {backup_path}: {e}",
                            )

    def _format_size(self, size_bytes: int) -> str:
        """Format size in bytes to a human-readable string

        Args:
            size_bytes: Size in bytes

        Returns:
            str: Human-readable size string
        """
        # Define units and thresholds
        units = ["B", "KB", "MB", "GB", "TB"]
        unit_index = 0
        formatted_size = float(size_bytes)

        # Convert to appropriate unit
        while formatted_size >= 1024 and unit_index < len(units) - 1:
            formatted_size /= 1024
            unit_index += 1

        # Format with appropriate precision
        if formatted_size < 10:
            return f"{formatted_size:.2f} {units[unit_index]}"
        elif formatted_size < 100:
            return f"{formatted_size:.1f} {units[unit_index]}"
        else:
            return f"{formatted_size:.0f} {units[unit_index]}"

    def list_backups(self) -> List[tuple]:
        """List all available backups

        Returns:
            List[tuple]: List of tuples containing (timestamp, path, size) for each backup
        """
        backups = []
        if not os.path.exists(self.backup_dir):
            return backups

        if self.filesystem:
            files = self.filesystem.listdir(self.backup_dir)
            for f in files:
                if f.startswith("vs_data_backup_") and f.endswith(".tar.zst"):
                    full_path = os.path.join(self.backup_dir, f)
                    mtime = self.filesystem.getmtime(full_path)
                    size = self.filesystem.getsize(full_path)
                    size_str = self._format_size(size)
                    backups.append((mtime, full_path, size_str))
        else:
            for f in os.listdir(self.backup_dir):
                if f.startswith("vs_data_backup_") and f.endswith(".tar.zst"):
                    full_path = os.path.join(self.backup_dir, f)
                    mtime = os.path.getmtime(full_path)
                    # Get human-readable size
                    try:
                        size_str = subprocess.run(
                            ["du", "-sh", full_path],
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            text=True,
                            check=False,
                        ).stdout.split()[0]
                    except Exception:
                        size_str = "N/A"
                    backups.append((mtime, full_path, size_str))

        # Sort by timestamp (newest first)
        backups.sort(reverse=True)
        return backups
