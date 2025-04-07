"""Manages the creation, rotation, and listing of server data backups.

Uses injected interfaces for filesystem, archiving, and compression operations
to ensure testability and flexibility.
"""

import os
import datetime
import tempfile
import traceback
from typing import Optional, List, TYPE_CHECKING, Tuple

from interfaces import IFileSystem, IArchiver, ICompressor, IProcessRunner
from errors import BackupError, FileSystemError, ProcessError

if TYPE_CHECKING:
    from ui import ConsoleManager
    from config import ServerSettings
    from system import SystemInterface  # Needed for dry_run


class BackupManager:
    """Handles backup creation (tar + zstd), rotation, and listing.

    Relies on injected dependencies for core operations.

    Attributes:
        filesystem (IFileSystem): Interface for filesystem interaction.
        archiver (IArchiver): Interface for creating tar archives.
        compressor (ICompressor): Interface for compressing archives (zstd).
        console (ConsoleManager): Interface for logging and output.
        settings (ServerSettings): Loaded application settings.
        process_runner (Optional[IProcessRunner]): Optional interface for process execution.
        dry_run (bool): Indicates if operations should be simulated.
        backup_dir (str): Path to the backup storage directory.
        data_dir (str): Path to the server data directory to back up.
        max_backups (int): Maximum number of backups to keep.
        server_user (str): User/group string (e.g., "user:group") to set ownership of backups.
        temp_dir (str): Path to the temporary directory for intermediate files.
    """

    def __init__(
        self,
        filesystem: IFileSystem,
        archiver: IArchiver,
        compressor: ICompressor,
        console: "ConsoleManager",
        settings: "ServerSettings",
        system_interface: "SystemInterface",  # Pass SystemInterface for dry_run
        process_runner: Optional[
            IProcessRunner
        ] = None,  # Keep optional for size fallback
    ):
        """Initializes the BackupManager.

        Args:
            filesystem: Implementation of IFileSystem.
            archiver: Implementation of IArchiver.
            compressor: Implementation of ICompressor.
            console: The ConsoleManager instance.
            settings: The ServerSettings instance.
            system_interface: The SystemInterface instance (for dry_run).
            process_runner: Optional implementation of IProcessRunner.
        """
        self.filesystem = filesystem
        self.archiver = archiver
        self.compressor = compressor
        self.console = console
        self.settings = settings
        self.process_runner = process_runner
        # Get dry_run status from SystemInterface
        self.dry_run = system_interface.dry_run

        # Store relevant settings
        self.backup_dir = settings.backup_dir
        self.data_dir = settings.data_dir
        self.max_backups = settings.max_backups
        # Format user/group string for chown
        self.server_user = f"{settings.server_user}:{settings.server_user}"
        self.temp_dir = settings.temp_dir

        self.console.debug("BackupManager initialized.")

    # --- Public Methods --- #

    def create_backup(self, ignore_failure: bool = False) -> Optional[str]:
        """Creates a compressed tarball backup (.tar.zst) of the server data directory.

        Steps:
        1. Perform pre-flight checks (data dir exists, backup dir exists/creatable).
        2. Calculate and log estimated data size (best effort).
        3. If not dry run:
           a. Create a temporary tar archive excluding specified patterns.
           b. Compress the temporary tar archive using zstandard.
           c. Set ownership of the final backup file.
           d. Clean up the temporary tar file.
           e. Rotate old backups based on `max_backups` setting.
        4. Return the path to the created backup file (or intended path in dry run).

        Args:
            ignore_failure: If True, log errors but return None instead of raising BackupError.

        Returns:
            The absolute path to the created backup file, or None if backup failed
            and `ignore_failure` was True.

        Raises:
            BackupError: If any step fails and `ignore_failure` is False.
            FileSystemError: May be raised by underlying filesystem operations.
            ProcessError: May be raised by underlying process operations (e.g., size check).
        """
        backup_timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_filename = f"vs_data_backup_{backup_timestamp}.tar.zst"
        # Ensure backup_file_path is absolute
        backup_file_path = os.path.abspath(
            os.path.join(self.backup_dir, backup_filename)
        )
        temp_tar_path = None  # Define outside try for finally block

        self.console.info(f"Starting backup of '{self.data_dir}'")
        self.console.info(f"Target backup file: {backup_file_path}")

        # --- Pre-flight Checks ---
        try:
            self._perform_preflight_checks()
        except (FileNotFoundError, FileSystemError, ProcessError) as e:
            raise BackupError(f"Backup pre-flight check failed: {e}") from e
        except Exception as e:
            raise BackupError(
                f"Unexpected error during backup pre-flight check: {e}"
            ) from e

        # --- Dry Run Check ---
        if self.dry_run:
            self.console.info(f"[DRY RUN] Would create backup: {backup_file_path}")
            self.console.info("[DRY RUN] Skipping actual file operations and rotation.")
            return backup_file_path  # Return intended path

        # --- Backup Creation ---
        try:
            temp_tar_path = self._create_temporary_archive()
            self._compress_archive(temp_tar_path, backup_file_path)
            self._finalize_backup(backup_file_path)
            return backup_file_path

        except (FileSystemError, ProcessError, BackupError, FileNotFoundError) as e:
            self.console.error(f"Backup process failed: {e}")
            self.console.debug(traceback.format_exc())  # Debug level for full traceback
            self._cleanup_failed_backup(backup_file_path)
            if ignore_failure:
                self.console.warning(
                    "Continuing after backup failure (--ignore-backup-failure)"
                )
                return None
            else:
                # Re-raise as BackupError for consistent API
                if not isinstance(e, BackupError):
                    raise BackupError(
                        f"Backup creation failed: {e}. Use --ignore-backup-failure to proceed."
                    ) from e
                else:
                    raise e  # Re-raise original BackupError
        except Exception as e:
            self.console.error(f"Unexpected error during backup: {e}", exc_info=True)
            self._cleanup_failed_backup(backup_file_path)
            if ignore_failure:
                self.console.warning(
                    "Continuing after unexpected backup failure (--ignore-backup-failure)"
                )
                return None
            else:
                raise BackupError(f"Unexpected backup failure: {e}") from e
        finally:
            # Ensure temporary tar file is always removed if it exists
            if temp_tar_path and self.filesystem.exists(temp_tar_path):
                self.console.debug(f"Cleaning up temporary file: {temp_tar_path}")
                try:
                    self.filesystem.remove(temp_tar_path)
                except (FileSystemError, Exception) as cleanup_e:
                    # Log cleanup failure but don't raise, as backup might have succeeded
                    self.console.warning(
                        f"Failed to clean up temp file '{temp_tar_path}': {cleanup_e}"
                    )

    def list_backups(self) -> List[Tuple[str, str, str]]:
        """Lists existing backups with size and modification date.

        Returns:
            A list of tuples: (filename, human_readable_size, modification_date_string).
            Returns an empty list if the backup directory doesn't exist or on error.
        """
        self.console.info(f"Listing backups in {self.backup_dir}...")
        backup_list_details = []
        try:
            sorted_backups = self._get_sorted_backups()  # Handles dir not existing
            if not sorted_backups:
                if self.filesystem.isdir(self.backup_dir):
                    self.console.info("No backup files found matching the pattern.")
                # If dir doesn't exist, _get_sorted_backups logs a warning
                return []

            for backup_path, mtime in sorted_backups:
                filename = os.path.basename(backup_path)
                size_str = self._get_backup_size_human(backup_path)
                date_str = datetime.datetime.fromtimestamp(mtime).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                backup_list_details.append((filename, size_str, date_str))

            return backup_list_details

        except (FileSystemError, Exception) as e:
            self.console.error(f"Failed to list backups: {e}")
            return []  # Return empty list on error

    # --- Private Helper Methods --- #

    def _perform_preflight_checks(self) -> None:
        """Runs checks before starting the backup file operations."""
        if not self.filesystem.isdir(self.data_dir):
            raise FileNotFoundError(f"Data directory not found: {self.data_dir}")

        self._log_data_size()  # Log estimated size

        # Ensure backup directory exists and has correct owner if possible
        self.console.debug(f"Ensuring backup directory exists: {self.backup_dir}")
        try:
            self.filesystem.mkdir(self.backup_dir, exist_ok=True)
            # Split user/group for chown call if using IFileSystem
            user, group = self.server_user.split(":")
            if not self.filesystem.chown(self.backup_dir, user, group, recursive=False):
                self.console.warning(
                    f"IFileSystem.chown reported failure for backup directory '{self.backup_dir}'"
                )
        except (FileSystemError, ProcessError, NotImplementedError, ValueError) as err:
            # Catch specific errors from mkdir/chown/split
            self.console.warning(
                f"Could not ensure/set ownership on backup directory '{self.backup_dir}': {err}"
            )
        except Exception as err:  # Catch broader errors too
            self.console.warning(
                f"Unexpected error ensuring backup directory '{self.backup_dir}': {err}"
            )

    def _create_temporary_archive(self) -> str:
        """Creates the temporary tar archive."""
        # Create temporary tar file securely
        self.console.info("Creating temporary tar archive...")
        if not self.filesystem.isdir(self.temp_dir):
            self.console.debug(f"Creating temporary directory: {self.temp_dir}")
            self.filesystem.mkdir(self.temp_dir, exist_ok=True)

        with tempfile.NamedTemporaryFile(
            delete=False, suffix=".tar", dir=self.temp_dir
        ) as temp_tar_file:
            temp_tar_path = os.path.abspath(temp_tar_file.name)
        self.console.debug(f"Temporary tar file path: {temp_tar_path}")

        # Define exclusions (relative to data_dir for tar)
        exclude_patterns = ["Backups/", "BackupSave/", "Cache/", "Logs/"]
        self.console.debug(
            f"Excluding patterns relative to data_dir: {exclude_patterns}"
        )

        # --- Archiving ---
        if not self.archiver.create(
            self.data_dir, temp_tar_path, exclude_patterns=exclude_patterns
        ):
            raise BackupError(
                f"Archiver failed to create '{temp_tar_path}'. Check archiver logs/output."
            )
        self.console.info(f"Temporary archive created: {temp_tar_path}")
        return temp_tar_path

    def _compress_archive(self, temp_tar_path: str, backup_file_path: str) -> None:
        """Compresses the temporary tar archive to the final backup file."""
        self.console.info(
            f"Compressing '{os.path.basename(temp_tar_path)}' to '{os.path.basename(backup_file_path)}'"
        )
        if not self.compressor.compress(temp_tar_path, backup_file_path):
            raise BackupError(
                f"Compressor failed to compress to '{backup_file_path}'. Check compressor logs/output."
            )
        self.console.info("Compression successful.")

    def _finalize_backup(self, backup_file_path: str) -> None:
        """Performs post-creation steps: logging size, setting ownership, rotating."""
        backup_size_human = self._get_backup_size_human(backup_file_path)
        self.console.info(f"Backup created: {backup_file_path} ({backup_size_human})")

        # Set ownership
        self.console.debug(
            f"Setting ownership of '{backup_file_path}' to {self.server_user}"
        )
        try:
            user, group = self.server_user.split(":")
            if not self.filesystem.chown(backup_file_path, user, group):
                self.console.warning(
                    f"IFileSystem reported failure setting ownership on: {backup_file_path}"
                )
        except (
            FileSystemError,
            ProcessError,
            NotImplementedError,
            ValueError,
        ) as chown_err:
            self.console.warning(
                f"Failed to set ownership on backup file '{backup_file_path}': {chown_err}"
            )
        except Exception as chown_err:  # Catch broader errors
            self.console.warning(
                f"Unexpected error setting ownership on backup file '{backup_file_path}': {chown_err}"
            )

        # Rotate backups
        self._rotate_backups()  # Rotation handles its own errors/logging

    def _log_data_size(self) -> None:
        """Logs the estimated size of the data directory using IFileSystem.

        Falls back to logging a warning if calculation fails.
        """
        self.console.debug(f"Calculating size of data directory: {self.data_dir}")
        try:
            data_size = self.filesystem.calculate_dir_size(self.data_dir)
            data_size_human = self._format_size(data_size)
            self.console.info(f"Estimated data directory size: ~{data_size_human}")
        except FileSystemError as e:
            self.console.warning(
                f"Could not calculate data directory size via IFileSystem: {e}"
            )
        except NotImplementedError:
            # This indicates a problem with the IFileSystem implementation provided
            self.console.warning(
                "IFileSystem.calculate_dir_size is not implemented. Cannot estimate data size."
            )
        except Exception as e:
            self.console.warning(f"Error calculating data directory size: {e}")

    def _get_backup_size_human(self, backup_file_path: str) -> str:
        """Gets the size of the final backup file in human-readable format.

        Args:
            backup_file_path: The path to the backup file.

        Returns:
            A human-readable string representation of the file size (e.g., "1.2 GiB")
            or "N/A" if the size cannot be determined.
        """
        try:
            size_bytes = self.filesystem.getsize(backup_file_path)
            return self._format_size(size_bytes)
        except (FileSystemError, FileNotFoundError, NotImplementedError) as e:
            self.console.warning(
                f"Could not get size of backup file '{backup_file_path}': {e}"
            )
            return "N/A"
        except Exception as e:
            self.console.warning(
                f"Unexpected error getting size of backup file '{backup_file_path}': {e}"
            )
            return "N/A"

    def _cleanup_failed_backup(self, backup_file_path: Optional[str]) -> None:
        """Attempts to remove a potentially incomplete backup file after a failure.

        Logs errors but does not raise them.

        Args:
            backup_file_path: The path to the backup file to remove, if it exists.
        """
        if not backup_file_path:  # If path wasn't even determined
            return
        try:
            if self.filesystem.exists(backup_file_path):
                self.console.warning(
                    f"Attempting to clean up failed/incomplete backup: {backup_file_path}"
                )
                self.filesystem.remove(backup_file_path)
                self.console.info(f"Cleaned up: {backup_file_path}")
        except (FileSystemError, Exception) as e:
            self.console.error(
                f"Failed to cleanup incomplete backup file '{backup_file_path}': {e}"
            )

    def _rotate_backups(self) -> None:
        """Removes the oldest backups if the number exceeds `max_backups`.

        Only considers files matching the pattern `vs_data_backup_*.tar.zst`.
        Logs actions and any errors encountered during rotation.
        """
        if self.max_backups <= 0:
            self.console.debug("Backup rotation skipped (max_backups <= 0).")
            return

        self.console.info(
            f"Rotating backups in '{self.backup_dir}'. Keeping newest {self.max_backups}."
        )

        try:
            backups = self._get_sorted_backups()
        except (FileSystemError, Exception) as e:
            self.console.error(
                f"Failed to list backups for rotation in '{self.backup_dir}': {e}"
            )
            return  # Cannot rotate if listing fails

        backups_to_delete = backups[self.max_backups :]

        if not backups_to_delete:
            self.console.info("No old backups need rotation.")
            return

        self.console.info(f"Found {len(backups_to_delete)} old backup(s) to remove.")
        deleted_count = 0
        for backup_path, _ in backups_to_delete:
            if self.dry_run:
                self.console.info(f"[DRY RUN] Would delete old backup: {backup_path}")
                deleted_count += 1
                continue
            try:
                self.console.debug(f"Deleting old backup: {backup_path}")
                self.filesystem.remove(backup_path)
                deleted_count += 1
                self.console.info(
                    f"Deleted old backup: {os.path.basename(backup_path)}"
                )
            except (FileSystemError, Exception) as e:
                # Log error for the specific file but continue trying to delete others
                self.console.error(f"Failed to delete old backup '{backup_path}': {e}")

        self.console.info(
            f"Backup rotation completed. {deleted_count} old backup(s) removed."
        )

    def _get_sorted_backups(self) -> List[Tuple[str, float]]:
        """Gets a list of backup files sorted by modification time (newest first).

        Filters files based on the expected naming pattern.

        Returns:
            A list of tuples, where each tuple contains (absolute_path, modification_time).

        Raises:
            FileSystemError: If listing the backup directory or getting mtime fails.
        """
        self.console.debug(f"Listing backups in: {self.backup_dir}")
        backup_pattern = "vs_data_backup_"
        suffix = ".tar.zst"
        backups = []

        try:
            if not self.filesystem.isdir(self.backup_dir):
                self.console.warning(
                    f"Backup directory '{self.backup_dir}' does not exist. Cannot list backups."
                )
                return []

            for filename in self.filesystem.listdir(self.backup_dir):
                if filename.startswith(backup_pattern) and filename.endswith(suffix):
                    file_path = os.path.abspath(os.path.join(self.backup_dir, filename))
                    try:
                        mtime = self.filesystem.getmtime(file_path)
                        backups.append((file_path, mtime))
                    except (FileSystemError, NotImplementedError) as mtime_e:
                        self.console.warning(
                            f"Could not get modification time for '{file_path}', skipping: {mtime_e}"
                        )
                    except Exception as mtime_e:  # Catch broader errors
                        self.console.warning(
                            f"Unexpected error getting mtime for '{file_path}', skipping: {mtime_e}"
                        )

            # Sort by modification time, descending (newest first)
            backups.sort(key=lambda item: item[1], reverse=True)
            self.console.debug(f"Found {len(backups)} backup files matching pattern.")
            return backups

        except (FileSystemError, Exception) as e:
            # If listdir itself fails
            self.console.error(
                f"Error listing backup directory '{self.backup_dir}': {e}"
            )
            raise FileSystemError(
                f"Could not list backup directory '{self.backup_dir}'"
            ) from e

    def _format_size(self, size_bytes: int) -> str:
        """Converts bytes to a human-readable string (KiB, MiB, GiB).

        Args:
            size_bytes: Size in bytes.

        Returns:
            Human-readable size string.
        """
        if size_bytes < 1024:
            return f"{size_bytes} B"
        kib = size_bytes / 1024
        if kib < 1024:
            return f"{kib:.1f} KiB"
        mib = kib / 1024
        if mib < 1024:
            return f"{mib:.1f} MiB"
        gib = mib / 1024
        return f"{gib:.1f} GiB"
