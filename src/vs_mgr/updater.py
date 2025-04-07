"""Manages the Vintage Story server update process.

Orchestrates stopping the server, backing up data, downloading the new version,
extracting, updating files (using rsync if available), starting the server,
and performing final verification and cleanup.

Relies on injected dependencies for service management, backups, version checking,
HTTP requests, filesystem operations, and archiving.
"""

import os
import time
import traceback
from typing import Optional, Tuple, TYPE_CHECKING

from vs_mgr.interfaces import IHttpClient, IFileSystem, IArchiver, IProcessRunner
from vs_mgr.errors import (
    UpdateError,
    BackupError,
    ServiceError,
    VersioningError,
    DownloadError,
    FileSystemError,
    ProcessError,
    DependencyError,
)

if TYPE_CHECKING:
    # Use full paths for clarity if needed, or keep as is if unambiguous
    from vs_mgr.system import SystemInterface
    from vs_mgr.services import ServiceManager
    from vs_mgr.backup import BackupManager
    from vs_mgr.versioning import VersionChecker
    from vs_mgr.ui import ConsoleManager
    from vs_mgr.config import ServerSettings


class UpdateManager:
    """Orchestrates the full server update process.

    Coordinates various components (ServiceManager, BackupManager, VersionChecker, etc.)
    to perform the update safely and robustly.

    Attributes:
        service_mgr (ServiceManager): Manages server service interactions.
        backup_mgr (BackupManager): Manages data backups.
        version_checker (VersionChecker): Checks current and latest versions.
        http_client (IHttpClient): Performs HTTP downloads.
        filesystem (IFileSystem): Performs filesystem operations.
        archiver (IArchiver): Handles archive extraction.
        console (ConsoleManager): Handles logging and user output.
        settings (ServerSettings): Contains application configuration.
        process_runner (Optional[IProcessRunner]): Used for optional commands like rsync.
        dry_run (bool): If True, simulate operations without making changes.
        server_dir (str): Path to the main server installation directory.
        data_dir (str): Path to the server data directory.
        temp_dir (str): Path to the temporary directory for downloads/extraction.
        service_name (str): Name of the systemd service.
        server_user (str): User string for ownership.
        rsync_available (bool): True if rsync command is detected.
        server_stopped (bool): Internal state tracking if the server was stopped.
        archive_name (str): Internal state tracking for the downloaded archive filename.
        _extracted_path (Optional[str]): Path where archive was extracted.
    """

    def __init__(
        self,
        service_manager: "ServiceManager",
        backup_manager: "BackupManager",
        version_checker: "VersionChecker",
        http_client: IHttpClient,
        filesystem: IFileSystem,
        archiver: IArchiver,
        console: "ConsoleManager",
        settings: "ServerSettings",
        # Pass SystemInterface to get dry_run status and use its methods if needed
        system_interface: "SystemInterface",
        process_runner: Optional[IProcessRunner] = None,  # Still needed for rsync
    ):
        """Initializes the UpdateManager.

        Args:
            service_manager: Instance for service operations.
            backup_manager: Instance for backup operations.
            version_checker: Instance for version checking.
            http_client: Instance for HTTP operations.
            filesystem: Instance for filesystem operations.
            archiver: Instance for archive operations.
            console: Instance for logging/output.
            settings: Loaded ServerSettings.
            system_interface: Instance for system checks (dry_run, which).
            process_runner: Optional instance for running processes (like rsync).
        """
        self.service_mgr = service_manager
        self.backup_mgr = backup_manager
        self.version_checker = version_checker
        self.http_client = http_client
        self.filesystem = filesystem
        self.archiver = archiver
        self.console = console
        self.settings = settings
        self.process_runner = process_runner  # Store optional runner
        self.dry_run = system_interface.dry_run  # Get dry_run from SystemInterface

        # Store required settings
        self.server_dir = settings.server_dir
        self.data_dir = settings.data_dir
        self.temp_dir = settings.temp_dir
        self.service_name = settings.service_name
        self.server_user = settings.server_user  # Keep as single user string for now

        # State tracking
        self.server_stopped = False
        self.archive_name = ""  # Will be set during download
        self._extracted_path: Optional[str] = None  # Path where archive was extracted

        # Determine rsync availability using SystemInterface
        self.rsync_available = system_interface.which("rsync") is not None
        self.console.debug(
            f"UpdateManager initialized. Dry Run: {self.dry_run}, Rsync available: {self.rsync_available}"
        )

    # --- Main Update Orchestration --- #

    def perform_update(
        self,
        new_version: str,
        skip_backup: bool = False,
        ignore_backup_failure: bool = False,
    ) -> Tuple[bool, Optional[str]]:
        """Orchestrates the server update to the specified version.

        Executes the update steps sequentially, handling errors and cleanup.

        Args:
            new_version: The target version string (e.g., "1.19.4").
            skip_backup: If True, skips the data backup step.
            ignore_backup_failure: If True, continues the update even if backup fails.

        Returns:
            A tuple containing: (success_flag, backup_file_path)
            `success_flag` (bool): True if the update completed successfully, False otherwise.
            `backup_file_path` (Optional[str]): Path to the backup created, or None.
        """
        backup_file_path: Optional[str] = None
        success = False
        start_time = time.time()

        try:
            self.console.info(
                f"=== Starting Vintage Story Server Update to v{new_version} ==="
            )
            if self.dry_run:
                self.console.info("DRY RUN MODE ENABLED - No changes will be made.")

            # 1. Preliminary Checks (Service exists, URL valid)
            download_url = self.version_checker.build_download_url(new_version)
            self._verify_service_and_url(new_version, download_url)

            # 2. Stop Server
            self._stop_server()

            # 3. Backup Server Data
            backup_file_path = self._handle_backup(skip_backup, ignore_backup_failure)

            # 4. Prepare Temp Directory
            self._ensure_temp_dir()  # Ensure temp dir exists and is clean

            # 5. Download & Extract Update Archive
            archive_path = self._download_server_archive(new_version, download_url)
            self._extracted_path = self._extract_server_archive(archive_path)

            # 6. Update Server Files (using rsync or Python fallback)
            self._update_server_files(self._extracted_path)

            # 7. Start Server & Verify Version
            self._start_and_verify_server(new_version)

            success = True
            self.console.info(
                f"=== Update to v{new_version} Completed Successfully ==="
            )

        except (
            # List expected, specific errors first
            UpdateError,
            ServiceError,
            BackupError,
            VersioningError,
            DownloadError,
            FileSystemError,
            ProcessError,
            DependencyError,
            FileNotFoundError,
            NotADirectoryError,
            PermissionError,
        ) as e:
            self.console.error(f"UPDATE FAILED: {e}", exc_info=False)
            self.console.debug(traceback.format_exc())  # Use imported traceback
            self.console.error("=== Update Process Failed ===")
            success = False
        except Exception as e:
            # Catch any truly unexpected errors
            self.console.exception(
                f"UNEXPECTED ERROR during update: {e}"
            )  # Logs with traceback
            self.console.error("=== Update Process Failed (Unexpected Error) ===")
            success = False
        finally:
            # 8. Cleanup Temporary Files/Directories
            self._cleanup()
            end_time = time.time()
            duration = end_time - start_time
            self.console.info(f"Update process finished in {duration:.2f} seconds.")

        return success, backup_file_path

    # --- Update Steps (Private Helpers) --- #

    def _ensure_temp_dir(self) -> None:
        """Ensures the temporary directory exists and is clean.

        Raises:
            UpdateError: If the directory cannot be created or cleaned.
        """
        self.console.debug(
            f"Ensuring temporary directory exists and is clean: {self.temp_dir}"
        )
        try:
            if self.filesystem.exists(self.temp_dir):
                if not self.dry_run:
                    self.console.debug(
                        f"Removing existing temp directory content: {self.temp_dir}"
                    )
                    self.filesystem.rmtree(self.temp_dir)  # Use rmtree from IFileSystem
                else:
                    self.console.info(
                        f"[DRY RUN] Would remove temp directory content: {self.temp_dir}"
                    )

            if not self.dry_run:
                self.filesystem.mkdir(self.temp_dir, exist_ok=True)
                # Attempt ownership change, warn on failure
                try:
                    # Assuming chown needs user, group separately
                    if not self.filesystem.chown(
                        self.temp_dir, self.server_user, self.server_user
                    ):
                        self.console.warning(
                            f"IFileSystem failed to set ownership on temp dir: {self.temp_dir}"
                        )
                except (
                    FileSystemError,
                    ProcessError,
                    NotImplementedError,
                    ValueError,
                ) as chown_err:
                    self.console.warning(
                        f"Could not set ownership on temp directory '{self.temp_dir}': {chown_err}"
                    )
                except Exception as chown_err:
                    self.console.warning(
                        f"Unexpected error setting ownership on temp dir '{self.temp_dir}': {chown_err}"
                    )
            else:
                self.console.info(
                    f"[DRY RUN] Would create temp directory: {self.temp_dir}"
                )
                self.console.info(
                    f"[DRY RUN] Would chown {self.temp_dir} to {self.server_user}"
                )

        except (FileSystemError, ProcessError) as e:
            raise UpdateError(
                f"Failed to prepare temporary directory '{self.temp_dir}': {e}"
            ) from e
        except Exception as e:
            raise UpdateError(
                f"Unexpected error preparing temporary directory '{self.temp_dir}': {e}"
            ) from e

    def _verify_service_and_url(self, new_version: str, download_url: str) -> None:
        """Verifies service existence and download URL accessibility.

        Raises:
            ServiceError: If the service is not found or status check fails.
            VersioningError: If the download URL verification fails.
            UpdateError: For unexpected errors during checks.
        """
        self.console.info("Performing preliminary checks...")
        # Check service existence
        service_status = self.service_mgr.get_service_status(self.service_name)
        if service_status == "not-found":
            raise ServiceError(
                f"Service '{self.service_name}' does not exist. Check configuration."
            )
        elif service_status == "error":
            raise ServiceError(
                f"Could not determine status for service '{self.service_name}'."
            )
        else:
            self.console.debug(
                f"Service '{self.service_name}' found (Status: {service_status})."
            )

        # Verify download URL
        self.console.info(f"Verifying download URL for version {new_version}...")
        if not self.version_checker.verify_download_url(download_url):
            raise VersioningError(
                f"Download URL verification failed for {download_url}. Check version number and network."
            )

        self.console.info("Preliminary checks passed.")

    def _stop_server(self) -> None:
        """Stop the server service"""
        self.console.info(f"Stopping server service: {self.service_name}...")
        try:
            # Use the renamed method from ServiceManager
            self.service_mgr.run_systemctl_action("stop", self.service_name)
            self.server_stopped = True
            # Optional: Add a short delay or check status to confirm stop
            time.sleep(2)
            if self.service_mgr.is_service_active(self.service_name):
                self.console.warning(
                    f"Service '{self.service_name}' still reported as active after stop command."
                )
            else:
                self.console.info(
                    f"Service '{self.service_name}' stopped successfully."
                )
        except ServiceError as e:
            raise ServiceError(
                f"Failed to stop service '{self.service_name}': {e}"
            ) from e
        except Exception as e:
            raise ServiceError(
                f"Unexpected error stopping service '{self.service_name}': {e}"
            ) from e

    def _handle_backup(
        self, skip_backup: bool, ignore_backup_failure: bool
    ) -> Optional[str]:
        """Handle the backup process according to settings"""
        if skip_backup:
            self.console.info("Skipping data backup as requested (--skip-backup).")
            return None

        self.console.info("Starting data backup...")
        try:
            # Ensure the call matches the expected signature (if it changed)
            # Assuming create_backup takes ignore_failure as a keyword arg
            backup_file_path = self.backup_mgr.create_backup(
                ignore_failure=ignore_backup_failure
            )
            if backup_file_path:
                self.console.info(f"Data backup completed: {backup_file_path}")
                return backup_file_path
            elif ignore_backup_failure:
                self.console.warning(
                    "Backup failed, but continuing due to --ignore-backup-failure."
                )
                return None
            else:
                # This case should ideally be handled by create_backup raising an error
                # if ignore_failure is False, but we add a safeguard.
                raise BackupError("Backup failed and ignore_backup_failure is False.")
        except BackupError as e:
            # Specific backup errors are caught and potentially re-raised
            if not ignore_backup_failure:
                self.console.error(f"Backup failed critically: {e}")
                raise  # Re-raise if not ignoring
            else:
                self.console.warning(f"Ignoring backup failure as requested: {e}")
                backup_file_path = None  # Ensure path is None
        except Exception as e:
            # Catch unexpected errors during the backup call
            err_msg = f"Unexpected error during backup process: {e}"
            self.console.error(err_msg, exc_info=True)
            if not ignore_backup_failure:
                raise UpdateError(err_msg) from e
            else:
                self.console.warning(
                    f"Ignoring unexpected backup failure as requested: {e}"
                )
                backup_file_path = None

        return backup_file_path

    def _download_server_archive(self, version: str, download_url: str) -> str:
        """Downloads the server archive file to the temporary directory.

        Args:
            version: The version string (used for filename).
            download_url: The URL to download from.

        Returns:
            The absolute path to the downloaded archive file.

        Raises:
            DownloadError: If the download fails.
            UpdateError: For other unexpected errors.
        """
        self.archive_name = self.settings.server_archive_format.format(version=version)
        archive_path = os.path.join(self.temp_dir, self.archive_name)
        self.console.info(
            f"Downloading server archive: {self.archive_name} from {download_url}"
        )

        if self.dry_run:
            self.console.info(f"[DRY RUN] Would download to: {archive_path}")
            # Need to return a plausible path for subsequent dry-run steps
            return archive_path

        try:
            success = self.http_client.download(download_url, archive_path)
            if not success:
                raise DownloadError(
                    f"Download failed (HTTP client reported failure) for {download_url}"
                )
            self.console.info(f"Download successful: {archive_path}")
            return archive_path
        except DownloadError as e:
            # Re-raise specific download errors
            self.console.error(f"Download failed: {e}")
            raise
        except Exception as e:
            # Wrap other errors
            err_msg = f"Unexpected error downloading {download_url}: {e}"
            self.console.error(err_msg, exc_info=True)
            raise DownloadError(err_msg) from e

    def _extract_server_archive(self, archive_path: str) -> str:
        """Extracts the downloaded server archive.

        Args:
            archive_path: Absolute path to the downloaded .tar.gz archive.

        Returns:
            The absolute path to the directory where files were extracted.

        Raises:
            UpdateError: If extraction fails or the expected directory isn't found.
            FileSystemError: For underlying filesystem issues during extraction.
        """
        extract_base_dir = os.path.join(self.temp_dir, "extracted")
        # The archive extracts into a subdirectory defined in settings
        expected_extracted_path = os.path.join(
            extract_base_dir, self.settings.extracted_dir_name
        )

        self.console.info(
            f"Extracting archive '{os.path.basename(archive_path)}' to '{extract_base_dir}'"
        )

        if self.dry_run:
            self.console.info(
                f"[DRY RUN] Would extract '{archive_path}' to '{extract_base_dir}'"
            )
            self.console.info(
                f"[DRY RUN] Assuming extracted content path: {expected_extracted_path}"
            )
            # Ensure the assumed path exists for dry-run copy steps
            # No, don't create dirs in dry run. Assume copy steps handle it.
            return expected_extracted_path

        try:
            # Ensure the base extraction directory exists
            self.filesystem.mkdir(extract_base_dir, exist_ok=True)

            # Extract using IArchiver
            success = self.archiver.extractall(archive_path, extract_base_dir)
            if not success:
                raise UpdateError(
                    f"Archive extraction failed (archiver reported failure) for {archive_path}"
                )

            # Verify the expected subdirectory exists after extraction
            if not self.filesystem.isdir(expected_extracted_path):
                raise UpdateError(
                    f"Extraction successful, but expected directory '{expected_extracted_path}' not found."
                )

            self.console.info(
                f"Extraction complete. Server files located in: {expected_extracted_path}"
            )
            return expected_extracted_path

        except (UpdateError, FileSystemError) as e:
            self.console.error(f"Extraction failed: {e}")
            raise
        except Exception as e:
            err_msg = f"Unexpected error extracting {archive_path}: {e}"
            self.console.error(err_msg, exc_info=True)
            raise UpdateError(err_msg) from e

    def _update_server_files(self, extracted_path: str) -> None:
        """Updates the live server directory using extracted files.

        Prefers using rsync if available, otherwise falls back to a Python-based copy.

        Args:
            extracted_path: The path containing the newly extracted server files.

        Raises:
            UpdateError: If both rsync and the fallback method fail.
            FileSystemError: For underlying issues during file operations.
            ProcessError: For issues running rsync.
        """
        self.console.info(
            f"Updating server files in '{self.server_dir}' from '{extracted_path}'"
        )
        if self.rsync_available and self.process_runner:
            try:
                self._update_with_rsync(extracted_path)
                self.console.info("Server files updated using rsync.")
                return  # Success via rsync
            except (ProcessError, FileSystemError, UpdateError) as rsync_err:
                self.console.warning(
                    f"rsync update failed: {rsync_err}. Attempting fallback..."
                )
            except Exception as rsync_err:
                self.console.warning(
                    f"Unexpected error during rsync update: {rsync_err}. Attempting fallback..."
                )
        else:
            self.console.info(
                "rsync not available or process runner missing. Using Python fallback for update."
            )

        # Fallback if rsync failed or wasn't available
        try:
            self._update_with_fallback(extracted_path)
            self.console.info("Server files updated using Python fallback method.")
        except (FileSystemError, Exception) as fallback_err:
            err_msg = f"Update failed using both rsync (if attempted) and Python fallback: {fallback_err}"
            self.console.error(err_msg)
            raise UpdateError(err_msg) from fallback_err

    def _update_with_rsync(self, source_dir: str) -> None:
        """Updates server files using the rsync command.

        Args:
            source_dir: Path to the directory containing the new server files.

        Raises:
            DependencyError: If rsync or process_runner is missing.
            ProcessError: If the rsync command fails.
            UpdateError: For other errors during the process.
        """
        if not self.rsync_available or not self.process_runner:
            raise DependencyError(
                "rsync command or process runner not available for rsync update."
            )

        self.console.info("Using rsync to update server files...")
        # Ensure source path ends with / for rsync to copy contents
        source_path_rsync = os.path.join(source_dir, "")
        target_path = self.server_dir

        # Basic rsync command options
        # -a: archive mode (recursive, preserves links, perms, times, owner, group)
        # -v: verbose (optional, good for logging)
        # --delete: delete extraneous files from destination dirs
        # --exclude: patterns to exclude (relative to source)
        rsync_cmd = [
            "rsync",
            "-av",
            "--delete",
            # Add excludes if necessary, e.g., --exclude='config/serverconfig.json'
            # Ensure paths in exclude are relative to the source directory
            source_path_rsync,
            target_path,
        ]

        if self.dry_run:
            rsync_cmd.insert(1, "--dry-run")  # Add --dry-run flag
            self.console.info(f"[DRY RUN] Would run rsync: {' '.join(rsync_cmd)}")
            return  # Don't actually run in dry run

        try:
            # Run rsync using SystemInterface to handle sudo if necessary
            # NOTE: This requires process_runner to be passed to __init__
            # Remove text=True, handle bytes if needed
            result = self.process_runner.run_sudo(
                rsync_cmd, check=True, capture_output=True
            )
            # Decode stdout/stderr if captured and needed for logging
            stdout_log = (
                result.stdout.decode("utf-8", errors="ignore").strip()
                if result.stdout
                else "(no stdout)"
            )
            stderr_log = (
                result.stderr.decode("utf-8", errors="ignore").strip()
                if result.stderr
                else "(no stderr)"
            )
            self.console.debug(f"rsync stdout:\n{stdout_log}")
            if stderr_log:
                self.console.debug(f"rsync stderr:\n{stderr_log}")
        except ProcessError as e:
            raise UpdateError(f"rsync command failed: {e}") from e
        except Exception as e:
            raise UpdateError(f"Unexpected error running rsync: {e}") from e

    def _update_with_fallback(self, source_dir: str) -> None:
        """Updates server files using Python's shutil and os (fallback method).

        Attempts to mimic `rsync -a --delete` behavior.
        *NOTE:* This is less efficient and potentially less robust than rsync.

        Args:
            source_dir: Path to the directory containing the new server files.

        Raises:
            FileSystemError: If file operations fail.
            UpdateError: For other errors during the process.
        """
        self.console.info("Using Python fallback to update server files...")
        target_dir = self.server_dir

        if self.dry_run:
            self.console.info(
                f"[DRY RUN] Would compare and copy files from '{source_dir}' to '{target_dir}'"
            )
            self.console.info(
                f"[DRY RUN] Would delete extraneous files in '{target_dir}'"
            )
            return

        try:
            # 1. Copy/update files from source to target
            self.console.debug(
                f"Copying/updating files from {source_dir} to {target_dir}"
            )
            copied_count = 0
            updated_count = 0

            # Use IFileSystem walk if available, otherwise os.walk
            walk_method = (
                self.filesystem.walk if hasattr(self.filesystem, "walk") else os.walk
            )

            for src_dirpath, src_dirnames, src_filenames in walk_method(source_dir):
                rel_path = os.path.relpath(src_dirpath, source_dir)
                dst_dirpath = os.path.join(target_dir, rel_path)

                # Create directories in destination if they don't exist
                if not self.filesystem.isdir(dst_dirpath):
                    self.console.debug(f"Creating directory: {dst_dirpath}")
                    self.filesystem.mkdir(dst_dirpath, exist_ok=True)
                    # Try setting ownership on newly created dirs
                    try:
                        user, group = self.server_user.split(":")
                        self.filesystem.chown(dst_dirpath, user, group)
                    except Exception as chown_e:
                        self.console.warning(
                            f"Could not chown new dir '{dst_dirpath}': {chown_e}"
                        )

                for filename in src_filenames:
                    src_filepath = os.path.join(src_dirpath, filename)
                    dst_filepath = os.path.join(dst_dirpath, filename)
                    should_copy = False

                    if not self.filesystem.exists(dst_filepath):
                        should_copy = True
                        action = "copying"
                        copied_count += 1
                    else:
                        # Compare modification times (basic check)
                        try:
                            src_mtime = self.filesystem.getmtime(src_filepath)
                            dst_mtime = self.filesystem.getmtime(dst_filepath)
                            if src_mtime > dst_mtime:
                                should_copy = True
                                action = "updating"
                                updated_count += 1
                        except (FileSystemError, NotImplementedError) as mtime_e:
                            self.console.warning(
                                f"Could not compare mtime for '{filename}', copying anyway: {mtime_e}"
                            )
                            should_copy = True
                            action = "copying (mtime error)"
                            # Avoid double counting if exists check failed
                            if not self.filesystem.exists(dst_filepath):
                                copied_count += 1
                            else:
                                updated_count += 1

                    if should_copy:
                        self.console.debug(
                            f"Fallback {action}: {filename} to {dst_dirpath}"
                        )
                        self.filesystem.copy(src_filepath, dst_filepath)
                        # Try setting ownership on copied/updated files
                        try:
                            user, group = self.server_user.split(":")
                            self.filesystem.chown(dst_filepath, user, group)
                        except Exception as chown_e:
                            self.console.warning(
                                f"Could not chown updated file '{dst_filepath}': {chown_e}"
                            )

            self.console.info(
                f"Fallback copy finished. Copied: {copied_count}, Updated: {updated_count}"
            )

            # 2. Delete extraneous files/dirs from target (mimic --delete)
            self.console.info("Checking for extraneous files in target directory...")
            deleted_count = 0
            walk_method = (
                self.filesystem.walk if hasattr(self.filesystem, "walk") else os.walk
            )
            for dst_dirpath, dst_dirnames, dst_filenames in walk_method(target_dir):
                rel_path = os.path.relpath(dst_dirpath, target_dir)
                src_dirpath_check = os.path.join(source_dir, rel_path)

                # Delete extraneous files
                for filename in dst_filenames:
                    dst_filepath = os.path.join(dst_dirpath, filename)
                    src_filepath_check = os.path.join(src_dirpath_check, filename)
                    if not self.filesystem.exists(src_filepath_check):
                        try:
                            self.console.debug(
                                f"Deleting extraneous file: {dst_filepath}"
                            )
                            self.filesystem.remove(dst_filepath)
                            deleted_count += 1
                        except (FileSystemError, Exception) as remove_err:
                            self.console.warning(
                                f"Failed to delete extraneous file '{dst_filepath}': {remove_err}"
                            )

                # Mark extraneous directories for deletion after iterating files in the current dir
                dirs_to_delete = []
                for dirname in dst_dirnames:
                    dst_subdirpath = os.path.join(dst_dirpath, dirname)
                    src_subdirpath_check = os.path.join(src_dirpath_check, dirname)
                    if not self.filesystem.exists(src_subdirpath_check):
                        try:
                            # Check if the directory in the destination is empty
                            if not self.filesystem.listdir(dst_subdirpath):
                                dirs_to_delete.append(dst_subdirpath)
                            else:
                                self.console.debug(
                                    f"Skipping deletion of non-empty extraneous directory: {dst_subdirpath}"
                                )
                        except (
                            FileSystemError,
                            NotImplementedError,
                        ) as check_err:
                            # Error checking dir, can't mark for deletion
                            self.console.warning(
                                f"Could not check/delete extraneous directory '{dst_subdirpath}': {check_err}"
                            )

                # Delete the marked directories for the current level
                if dirs_to_delete:
                    # If using os.walk default (topdown=True), modifying dst_dirnames *here*
                    # prevents os.walk from descending into directories we are about to delete.
                    # If IFileSystem.walk isn't topdown or doesn't support modification,
                    # this might not be necessary or might behave differently.
                    for dir_to_del in dirs_to_delete:
                        basename_to_del = os.path.basename(dir_to_del)
                        try:
                            self.console.debug(
                                f"Deleting extraneous empty directory: {dir_to_del}"
                            )
                            self.filesystem.rmtree(dir_to_del)
                            deleted_count += 1
                            # If walk_method is os.walk, modify dst_dirnames in-place
                            if (
                                walk_method is os.walk
                                and basename_to_del in dst_dirnames
                            ):
                                dst_dirnames.remove(basename_to_del)
                        except (
                            FileSystemError,
                            NotImplementedError,
                        ) as del_err:
                            self.console.warning(
                                f"Failed during deletion of extraneous directory '{dir_to_del}': {del_err}"
                            )

            if deleted_count > 0:
                self.console.info(
                    f"Deleted {deleted_count} extraneous file(s)/directory(ies)."
                )
            else:
                self.console.info("No extraneous files found to delete.")

        except (FileSystemError, ValueError, NotImplementedError) as e:
            raise UpdateError(
                f"Python fallback update failed during file operations: {e}"
            ) from e
        except Exception as e:
            raise UpdateError(
                f"Unexpected error during Python fallback update: {e}"
            ) from e

    def _start_and_verify_server(self, expected_version: str) -> None:
        """Starts the server and verifies its version after the update.

        Args:
            expected_version: The version string the server should report.

        Raises:
            ServiceError: If the server fails to start.
            VersioningError: If the server reports an unexpected version or version check fails.
            UpdateError: For other errors during the process.
        """
        self.console.info(f"Starting server service: {self.service_name}...")
        try:
            self.service_mgr.run_systemctl_action("start", self.service_name)
            # Wait for the service to become active
            if not self.service_mgr.wait_for_service_active(self.service_name):
                # Error already logged by wait_for_service_active
                raise ServiceError(
                    f"Service '{self.service_name}' failed to become active after starting."
                )
            self.server_stopped = False  # Mark as running again
            self.console.info(f"Service '{self.service_name}' started successfully.")
        except ServiceError as e:
            raise UpdateError(
                f"Failed to start or verify service '{self.service_name}': {e}"
            ) from e
        except Exception as e:
            raise UpdateError(
                f"Unexpected error starting service '{self.service_name}': {e}"
            ) from e

        # Verify version after start
        self.console.info("Verifying server version after update...")
        try:
            # Add a small delay before checking version, server might need time
            time.sleep(5)
            current_version = self.version_checker.get_server_version()
            if current_version:
                self.console.info(f"Server reported version: {current_version}")
                # Use compare_versions for robustness
                comparison = self.version_checker.compare_versions(
                    current_version, expected_version
                )
                if comparison == 0:
                    self.console.info(
                        f"Version verification successful (Expected: {expected_version}, Found: {current_version})."
                    )
                else:
                    raise VersioningError(
                        f"Version mismatch after update! Expected '{expected_version}', but server reported '{current_version}'."
                    )
            else:
                raise VersioningError(
                    "Could not determine server version after update. Check server logs."
                )
        except VersioningError:
            # Re-raise specific versioning errors
            raise
        except Exception as e:
            err_msg = f"Unexpected error during post-update version verification: {e}"
            self.console.error(err_msg, exc_info=True)
            # Treat verification failure as a potential update issue
            raise UpdateError(err_msg) from e

    def _cleanup(self) -> None:
        """Cleans up temporary files and directories created during the update.

        Logs errors but does not raise them, as cleanup failure is usually non-critical.
        """
        self.console.info("Performing cleanup...")

        # Clean downloaded archive
        if self.archive_name:
            archive_path = os.path.join(self.temp_dir, self.archive_name)
            try:
                if self.filesystem.exists(archive_path):
                    self.console.debug(f"Removing downloaded archive: {archive_path}")
                    self.filesystem.remove(archive_path)
            except (FileSystemError, Exception) as e:
                self.console.warning(
                    f"Failed to remove downloaded archive '{archive_path}': {e}"
                )

        # Clean extracted files directory
        if self._extracted_path:
            try:
                if self.filesystem.isdir(self._extracted_path):
                    # Check if it's inside the temp_dir for safety
                    if self._extracted_path.startswith(
                        os.path.abspath(self.temp_dir)
                    ):  # Fix syntax error context
                        self.console.debug(
                            f"Removing extracted files directory: {self._extracted_path}"
                        )
                        self.filesystem.rmtree(self._extracted_path)
                    else:
                        self.console.warning(
                            f"Skipping cleanup of extracted path outside temp dir: {self._extracted_path}"
                        )
            except (FileSystemError, Exception) as e:
                self.console.warning(
                    f"Failed to remove extracted files directory '{self._extracted_path}': {e}"
                )

        # Optional: Clean the entire temp dir if it's empty or safe to do so
        # ... (optional cleanup logic remains commented) ...

        self.console.info("Cleanup finished.")
