import os
import datetime
import tempfile
import traceback  # For detailed error logging
from typing import Optional, List, TYPE_CHECKING

from interfaces import IFileSystem, IArchiver, ICompressor
from errors import BackupError, FileSystemError, ProcessError  # Custom errors

if TYPE_CHECKING:
    from ui import ConsoleManager
    from config import ServerSettings  # For type hinting


class BackupManager:
    """Manages backup creation and rotation for Vintage Story server data"""

    def __init__(
        self,
        # system_interface: 'SystemInterface', # Primarily use filesystem now
        filesystem: IFileSystem,  # Assume required
        archiver: IArchiver,  # Assume required
        compressor: ICompressor,  # Assume required
        console: "ConsoleManager",  # Assume required
        settings: "ServerSettings",  # Assume required
        process_runner=None,  # Optional: For fallback size check
        system_interface=None,  # Optional: For dry_run check
    ):
        """Initialize BackupManager

        Args:
            filesystem: IFileSystem implementation for filesystem operations
            archiver: IArchiver implementation for archive operations
            compressor: ICompressor implementation for compression operations
            console: ConsoleManager instance for output
            settings: ServerSettings instance with configuration
            process_runner: Optional process runner for fallback size check
            system_interface: Optional SystemInterface for dry_run check
        """
        # self.system = system_interface
        self.filesystem = filesystem
        self.archiver = archiver
        self.compressor = compressor
        self.console = console
        self.settings = settings
        self.process_runner = process_runner  # Store if provided
        self.dry_run = getattr(system_interface, "dry_run", False)

        # Get settings, ensuring they exist
        self.backup_dir = settings.backup_dir
        self.data_dir = settings.data_dir
        self.max_backups = settings.max_backups
        self.server_user = settings.server_user

    def create_backup(self, ignore_failure: bool = False) -> Optional[str]:
        """Create a compressed tar backup of the data directory

        Raises:
            BackupError: If backup creation fails and ignore_failure is False
            FileSystemError: For underlying filesystem issues
            ProcessError: For issues running external commands (like size check fallback)

        Returns:
            Path to the created backup file or None if failed but ignored
        """
        backup_timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        # Define backup filename pattern within the manager
        backup_filename = f"vs_data_backup_{backup_timestamp}.tar.zst"
        backup_file_path = os.path.join(self.backup_dir, backup_filename)
        temp_tar_path = None  # To ensure cleanup

        self.console.info(
            f"Starting backup process for data directory: {self.data_dir}"
        )
        self.console.info(f"Target backup file: {backup_file_path}")

        # --- Pre-flight Checks & Setup ---
        try:
            # 1. Check Data Directory Existence
            if not self.filesystem.isdir(self.data_dir):
                raise BackupError(
                    f"Data directory not found or is not a directory: {self.data_dir}"
                )

            # 2. Calculate Data Directory Size (Best effort)
            self._log_data_size()

            # 3. Ensure Backup Directory Exists
            self.console.debug(f"Ensuring backup directory exists: {self.backup_dir}")
            self.filesystem.mkdir(self.backup_dir, exist_ok=True)
            # Attempt to set ownership early, warn if fails but continue
            try:
                self.filesystem.chown(
                    self.backup_dir, self.server_user, self.server_user, recursive=False
                )
            except Exception as chown_err:
                self.console.warning(
                    f"Could not set ownership of backup directory '{self.backup_dir}': {chown_err}"
                )

        except (FileSystemError, ProcessError) as e:
            # Filesystem errors during setup are critical
            raise BackupError(f"Filesystem error during backup setup: {e}") from e
        except Exception as e:
            raise BackupError(f"Unexpected error during backup setup: {e}") from e

        # --- Dry Run Check ---
        if self.dry_run:
            self.console.info(
                f"[DRY RUN] Would create backup of {self.data_dir} to {backup_file_path}"
            )
            self.console.info(
                "[DRY RUN] Skipping backup creation, rotation, and chown."
            )
            # In dry run, return the *intended* path, not a dummy one
            return backup_file_path

        # --- Backup Creation ---
        try:
            self.console.info("Creating temporary tar archive...")
            # Create a temporary file securely
            with tempfile.NamedTemporaryFile(
                delete=False, suffix=".tar", dir=self.settings.temp_dir
            ) as temp_tar_file:
                temp_tar_path = temp_tar_file.name
            self.console.debug(f"Temporary tar file: {temp_tar_path}")

            # Define exclusions relative to data_dir base
            exclude_patterns = ["Backups", "BackupSave", "Cache", "Logs"]
            self.console.debug(
                f"Excluding patterns relative to data_dir: {exclude_patterns}"
            )

            # Create tar archive using IArchiver
            if not self.archiver.create(
                self.data_dir, temp_tar_path, exclude_patterns=exclude_patterns
            ):
                # Archiver should ideally raise its own error, but handle bool return just in case
                raise BackupError(
                    f"Archiver failed to create temporary archive '{temp_tar_path}'"
                )
            self.console.info("Temporary tar archive created successfully.")

            self.console.info(f"Compressing archive to: {backup_file_path}")
            # Compress using ICompressor
            if not self.compressor.compress(temp_tar_path, backup_file_path):
                # Compressor should ideally raise its own error
                raise BackupError(
                    f"Compressor failed to compress '{temp_tar_path}' to '{backup_file_path}'"
                )
            self.console.info("Archive compressed successfully.")

            # Get backup size (best effort)
            backup_size_human = self._get_backup_size_human(backup_file_path)
            self.console.info(
                f"Backup created successfully: {backup_file_path} ({backup_size_human})"
            )

            # Set ownership of the final backup file
            self.console.debug(
                f"Setting ownership of {backup_file_path} to {self.server_user}"
            )
            if not self.filesystem.chown(
                backup_file_path, self.server_user, self.server_user
            ):
                self.console.warning(
                    f"Failed to set ownership on backup file: {backup_file_path}"
                )
                # Don't treat as fatal, but log it

            # Rotate backups
            if self.max_backups > 0:
                self._rotate_backups()
            else:
                self.console.info("Backup rotation skipped (max_backups <= 0).")

            return backup_file_path

        except (FileSystemError, ProcessError, BackupError) as e:
            # Catch specific known errors
            self.console.error(f"Backup creation failed: {e}")
            self.console.error(
                traceback.format_exc()
            )  # Log full traceback for debugging
            # Clean up potentially incomplete backup file
            self._cleanup_failed_backup(backup_file_path)
            if ignore_failure:
                self.console.warning(
                    "Continuing despite backup failure (--ignore-backup-failure specified)"
                )
                return None
            else:
                # Re-raise as BackupError to signal failure upstream
                raise BackupError(
                    f"Backup creation failed: {e}. To proceed without a backup, use --skip-backup or --ignore-backup-failure."
                ) from e

        except Exception as e:
            # Catch any unexpected errors
            self.console.error(
                f"Unexpected error during backup creation: {e}", exc_info=True
            )
            self._cleanup_failed_backup(backup_file_path)
            if ignore_failure:
                self.console.warning(
                    "Continuing despite unexpected backup failure (--ignore-backup-failure specified)"
                )
                return None
            else:
                raise BackupError(f"Unexpected backup failure: {e}") from e

        finally:
            # Ensure temporary tar file is always deleted
            if temp_tar_path and self.filesystem.exists(temp_tar_path):
                self.console.debug(f"Cleaning up temporary file: {temp_tar_path}")
                try:
                    self.filesystem.remove(temp_tar_path)
                except Exception as cleanup_e:
                    self.console.warning(
                        f"Failed to clean up temporary file '{temp_tar_path}': {cleanup_e}"
                    )

    def _log_data_size(self):
        """Log the size of the data directory (best effort)"""
        self.console.debug(f"Calculating size of data directory: {self.data_dir}")
        try:
            data_size = self.filesystem.calculate_dir_size(self.data_dir)
            data_size_human = self._format_size(data_size)
            self.console.info(f"Estimated data directory size: {data_size_human}")
        except FileSystemError as e:
            self.console.warning(
                f"Could not calculate data directory size using IFileSystem: {e}"
            )
        except NotImplementedError:
            self.console.debug(
                "IFileSystem.calculate_dir_size not implemented, trying fallback."
            )
            self._log_data_size_fallback()
        except Exception as e:
            self.console.warning(f"Error calculating data directory size: {e}")

    def _log_data_size_fallback(self):
        """Fallback method to log data size using 'du' command"""
        if self.process_runner:
            try:
                result = self.process_runner.run(
                    ["du", "-sh", self.data_dir],
                    capture_output=True,
                    check=False,  # Don't raise error, just check output
                )
                if result.returncode == 0 and result.stdout:
                    data_size_human = result.stdout.split()[0]
                    self.console.info(
                        f"Estimated data directory size (via du): {data_size_human}"
                    )
                else:
                    self.console.warning(
                        f"'du -sh' command failed or produced no output. Stderr: {result.stderr}"
                    )
            except ProcessError as e:
                self.console.warning(
                    f"Could not calculate data size using 'du' command: {e}"
                )
            except Exception as e:
                self.console.warning(f"Unexpected error running 'du' command: {e}")
        else:
            self.console.debug(
                "No process runner available for 'du' fallback size check."
            )

    def _get_backup_size_human(self, backup_file_path: str) -> str:
        """Get human-readable size of the backup file (best effort)"""
        try:
            size_bytes = self.filesystem.getsize(backup_file_path)
            return self._format_size(size_bytes)
        except FileSystemError as e:
            self.console.warning(
                f"Could not get backup file size using IFileSystem: {e}"
            )
        except Exception as e:
            self.console.warning(f"Error getting backup file size: {e}")
        return "N/A"

    def _cleanup_failed_backup(self, backup_file_path: str):
        """Attempt to remove an incomplete backup file"""
        try:
            if self.filesystem.exists(backup_file_path):
                self.console.info(
                    f"Attempting to clean up incomplete backup file: {backup_file_path}"
                )
                self.filesystem.remove(backup_file_path)
        except Exception as e:
            self.console.warning(
                f"Failed to clean up incomplete backup file '{backup_file_path}': {e}"
            )

    def _rotate_backups(self) -> None:
        """Rotate backups, keeping only the N most recent (N=max_backups)"""
        if self.max_backups <= 0:
            self.console.debug("Rotation skipped: max_backups is not positive.")
            return

        self.console.info(
            f"Rotating backups in '{self.backup_dir}' (keeping {self.max_backups})."
        )

        try:
            # List and sort backups by modification time
            backups = self._get_sorted_backups()

            if len(backups) <= self.max_backups:
                self.console.info(
                    f"Found {len(backups)} backups, which is within the limit of {self.max_backups}. No rotation needed."
                )
                return

            # Identify backups to delete
            backups_to_delete = backups[self.max_backups :]
            self.console.info(
                f"Found {len(backups)} backups. Will remove {len(backups_to_delete)} older backups."
            )

            # Delete old backups
            deleted_count = 0
            for _, backup_path in backups_to_delete:
                try:
                    self.console.debug(f"Removing old backup: {backup_path}")
                    self.filesystem.remove(backup_path)
                    deleted_count += 1
                except FileSystemError as e:
                    self.console.warning(
                        f"Failed to remove old backup '{backup_path}': {e}"
                    )
                except Exception as e:
                    self.console.error(
                        f"Unexpected error removing old backup '{backup_path}': {e}",
                        exc_info=True,
                    )

            self.console.info(f"Successfully removed {deleted_count} old backups.")

        except FileSystemError as e:
            # Treat failure to list/stat backups as non-fatal for rotation
            self.console.warning(
                f"Could not perform backup rotation due to filesystem error: {e}"
            )
        except Exception as e:
            self.console.error(
                f"Unexpected error during backup rotation: {e}", exc_info=True
            )

    def _get_sorted_backups(self) -> List[tuple]:
        """Get a list of backup files sorted by modification time (newest first)"""
        backups = []
        self.console.debug(f"Listing backup files in: {self.backup_dir}")
        try:
            files = self.filesystem.listdir(self.backup_dir)
            for f in files:
                # Use the pattern defined earlier
                if f.startswith("vs_data_backup_") and f.endswith(".tar.zst"):
                    full_path = os.path.join(self.backup_dir, f)
                    try:
                        mtime = self.filesystem.getmtime(full_path)
                        backups.append((mtime, full_path))
                    except FileSystemError as e:
                        self.console.warning(
                            f"Could not get modification time for '{full_path}': {e}"
                        )
                        # Assign a default old time if stat fails, so it might get rotated
                        backups.append((0.0, full_path))
        except FileSystemError as e:
            self.console.error(
                f"Failed to list backup directory '{self.backup_dir}': {e}"
            )
            raise  # Re-raise if listing fails

        # Sort by modification time, newest first
        backups.sort(key=lambda x: x[0], reverse=True)
        self.console.debug(f"Found {len(backups)} backup files.")
        return backups

    def _format_size(self, size_bytes: int) -> str:
        """Convert bytes to human-readable format (KB, MB, GB)"""
        if size_bytes is None:
            return "N/A"
        try:
            size_bytes = int(size_bytes)
            if size_bytes < 1024:
                return f"{size_bytes} B"
            elif size_bytes < 1024**2:
                return f"{size_bytes / 1024:.1f} KB"
            elif size_bytes < 1024**3:
                return f"{size_bytes / 1024**2:.1f} MB"
            else:
                return f"{size_bytes / 1024**3:.1f} GB"
        except (ValueError, TypeError):
            return "N/A"

    # Optional: Keep list_backups if needed by other parts (e.g., info command)
    def list_backups(self) -> List[tuple]:
        """List existing backups with size and date"""
        sorted_backups = self._get_sorted_backups()
        backup_details = []
        for mtime, path in sorted_backups:
            try:
                size = self.filesystem.getsize(path)
                size_human = self._format_size(size)
                date_human = datetime.datetime.fromtimestamp(mtime).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                backup_details.append((os.path.basename(path), size_human, date_human))
            except Exception as e:
                self.console.warning(f"Could not get details for backup '{path}': {e}")
                backup_details.append((os.path.basename(path), "N/A", "N/A"))
        return backup_details
