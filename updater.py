import os
import datetime
import time
import shutil
from typing import Optional, Tuple, TYPE_CHECKING

from interfaces import IHttpClient, IFileSystem, IArchiver, IProcessRunner
from errors import (
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
    from system import SystemInterface
    from services import ServiceManager
    from backup import BackupManager
    from versioning import VersionChecker
    from ui import ConsoleManager
    from config import ServerSettings


class UpdateManager:
    """Manages the update process for Vintage Story server"""

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
        process_runner: Optional[IProcessRunner] = None,
        system_interface: Optional["SystemInterface"] = None,
    ):
        """Initialize UpdateManager

        Args:
            service_manager: ServiceManager instance for service operations
            backup_manager: BackupManager instance for backup operations
            version_checker: VersionChecker instance for version checking
            http_client: IHttpClient implementation for HTTP operations
            filesystem: IFileSystem implementation for filesystem operations
            archiver: IArchiver implementation for archive operations
            console: ConsoleManager instance for output
            settings: ServerSettings instance with configuration
            process_runner: Optional IProcessRunner for rsync
            system_interface: Optional SystemInterface for dry_run and rsync check
        """
        self.service_mgr = service_manager
        self.backup_mgr = backup_manager
        self.version_checker = version_checker
        self.http_client = http_client
        self.filesystem = filesystem
        self.archiver = archiver
        self.console = console
        self.settings = settings
        self.process_runner = process_runner
        self.dry_run = getattr(system_interface, "dry_run", False)

        # Set defaults from settings if provided, or use hardcoded defaults
        if settings:
            self.server_dir = settings.server_dir
            self.data_dir = settings.data_dir
            self.temp_dir = settings.temp_dir
            self.downloads_base_url = settings.downloads_base_url
            self.service_name = settings.service_name
            self.server_user = settings.server_user
        else:
            self.server_dir = "/srv/gameserver/vintagestory"
            self.data_dir = "/srv/gameserver/data/vs"
            self.temp_dir = "/tmp/vs_update"
            self.downloads_base_url = "https://cdn.vintagestory.at/gamefiles"
            self.service_name = "vintagestoryserver"
            self.server_user = "gameserver"

        # State tracking
        self.server_stopped = False
        self.archive_name = ""

        # Determine rsync availability
        self.rsync_available = False
        if self.process_runner and shutil.which("rsync"):
            self.rsync_available = True
            self.console.debug("rsync command is available.")
        else:
            self.console.debug(
                "rsync command is not available or process_runner not provided."
            )

    def perform_update(
        self,
        new_version: str,
        skip_backup: bool = False,
        ignore_backup_failure: bool = False,
    ) -> Tuple[bool, Optional[str]]:
        """Update the server to the specified version

        Args:
            new_version: Version to update to (format X.Y.Z)
            skip_backup: Whether to skip creating a backup
            ignore_backup_failure: Whether to ignore backup failures

        Returns:
            tuple: (success, backup_file_path)
        """
        backup_file_path: Optional[str] = None
        success = False
        start_time = time.time()

        try:
            self.console.info(
                f"=== Starting Vintage Story Server Update to {new_version} ==="
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

            # 4. Download & Extract Update Archive
            self._ensure_temp_dir()
            self.archive_name = f"vs_server_linux-x64_{new_version}.tar.gz"
            self._download_server_archive(download_url)
            self._extract_server_archive()

            # 5. Update Server Files (rsync or fallback)
            self._update_server_files()

            # 6. Start Server & Verify Version
            self._start_and_verify_server(new_version)

            success = True
            self.console.info("=== Update Process Completed Successfully ===")

        except (
            UpdateError,
            ServiceError,
            BackupError,
            VersioningError,
            DownloadError,
            FileSystemError,
            ProcessError,
            DependencyError,
        ) as e:
            self.console.error(f"Update failed: {e}", exc_info=False)
            self.console.error("=== Update Process Failed ===")
            success = False
        except Exception as e:
            self.console.exception(f"An unexpected error occurred during update: {e}")
            self.console.error("=== Update Process Failed (Unexpected Error) ===")
            success = False
        finally:
            # 7. Cleanup
            self.cleanup()
            end_time = time.time()
            duration = end_time - start_time
            self.console.info(f"Update process finished in {duration:.2f} seconds.")

        return success, backup_file_path

    def _ensure_temp_dir(self):
        """Ensure the temporary directory exists"""
        self.console.debug(f"Ensuring temporary directory exists: {self.temp_dir}")
        try:
            self.filesystem.mkdir(self.temp_dir, exist_ok=True)
            # Attempt ownership change, warn on failure
            try:
                self.filesystem.chown(self.temp_dir, self.server_user, self.server_user)
            except Exception as chown_err:
                self.console.warning(
                    f"Could not set ownership on temp directory '{self.temp_dir}': {chown_err}"
                )
        except FileSystemError as e:
            raise UpdateError(
                f"Failed to create or access temporary directory '{self.temp_dir}': {e}"
            ) from e

    def _verify_service_and_url(self, new_version: str, download_url: str) -> None:
        """Verify that the service exists and the download URL is accessible"""
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
        if not self.version_checker._verify_download_url(download_url):
            raise VersioningError(
                f"Download URL verification failed for {download_url}. Check version number and network."
            )

        self.console.info("Preliminary checks passed.")

    def _stop_server(self) -> None:
        """Stop the server service"""
        self.console.info(f"Stopping server service: {self.service_name}...")
        try:
            self.service_mgr.run_systemctl("stop", self.service_name)
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
            self.console.info("Skipping backup (--skip-backup specified).")
            return None

        self.console.info("Starting server data backup...")
        try:
            backup_file_path = self.backup_mgr.create_backup(
                ignore_failure=ignore_backup_failure
            )
            if backup_file_path:
                self.console.info(f"Backup completed successfully: {backup_file_path}")
                return backup_file_path
            elif ignore_backup_failure:
                self.console.warning(
                    "Backup failed, but failure is ignored (--ignore-backup-failure specified)."
                )
                return None
            else:
                # This case should ideally be handled by create_backup raising an error
                # But we include it for robustness
                raise BackupError(
                    "Backup process failed (and failure was not ignored). See previous logs for details."
                )
        except BackupError as e:
            # If create_backup raises BackupError and ignore_backup_failure is False
            self.console.error(f"Backup failed: {e}")
            raise
        except Exception as e:
            # Catch unexpected errors during backup handling
            self.console.error(f"Unexpected error during backup: {e}", exc_info=True)
            if ignore_backup_failure:
                self.console.warning(
                    "Continuing update despite unexpected backup error (--ignore-backup-failure specified)."
                )
                return None
            else:
                raise BackupError(f"Unexpected error during backup: {e}") from e

    def _download_server_archive(self, download_url: str) -> None:
        """Download the server archive"""
        if not self.archive_name:
            raise DownloadError(
                "Internal error: archive_name not set before download attempt."
            )

        self.console.info(f"Downloading server archive from: {download_url}")
        self.console.info(f"Saving to: {self.archive_name}")

        if self.dry_run:
            self.console.info("[DRY RUN] Skipping download.")
            # Create a dummy file to allow extraction step to proceed in dry run if needed
            try:
                with open(self.archive_name, "w") as f:
                    f.write("dry run placeholder")
            except Exception as e:
                self.console.warning(
                    f"[DRY RUN] Could not create dummy archive file: {e}"
                )
            return

        try:
            # Use the http_client interface's download method
            success = self.http_client.download(download_url, self.archive_name)
            if not success:
                # http_client.download should ideally raise, but handle bool return
                raise DownloadError(
                    f"Download failed (reported by IHttpClient). Check URL and network: {download_url}"
                )
            self.console.info("Download completed successfully.")
        except DownloadError as e:
            self.console.error(f"Download failed: {e}")
            self._cleanup_downloaded_archive()
            raise
        except Exception as e:
            self.console.error(f"Unexpected error during download: {e}", exc_info=True)
            self._cleanup_downloaded_archive()
            raise DownloadError(
                f"Unexpected error downloading {download_url}: {e}"
            ) from e

    def _extract_server_archive(self) -> None:
        """Extract the downloaded server archive"""
        if not self.archive_name or not self.filesystem.exists(self.archive_name):
            # If download was skipped in dry run, the dummy file might exist
            if (
                self.dry_run
                and self.archive_name
                and self.filesystem.exists(self.archive_name)
            ):
                self.console.info(
                    "[DRY RUN] Skipping extraction of dummy archive file."
                )
                # Create dummy extraction dir for dry run consistency
                try:
                    self.filesystem.mkdir(self.archive_name, exist_ok=True)
                except:
                    pass
                return
            raise DownloadError(
                "Cannot extract archive: Downloaded archive file not found or path not set."
            )

        self.console.info(f"Extracting archive: {self.archive_name}")

        # Clean up previous extraction if it exists
        if self.filesystem.exists(self.archive_name):
            self.console.debug(
                f"Removing existing extraction directory: {self.archive_name}"
            )
            try:
                self.filesystem.rmtree(self.archive_name)
            except FileSystemError as e:
                raise DownloadError(
                    f"Failed to remove previous extraction directory '{self.archive_name}': {e}"
                ) from e

        if self.dry_run:
            self.console.info("[DRY RUN] Skipping extraction.")
            # Create dummy extraction dir
            try:
                self.filesystem.mkdir(self.archive_name, exist_ok=True)
            except:
                pass
            return

        try:
            # Extract using IArchiver
            success = self.archiver.extractall(self.archive_name, self.archive_name)
            if not success:
                # Archiver should ideally raise, but handle bool return
                raise DownloadError(
                    f"Extraction failed (reported by IArchiver). Archive: '{self.archive_name}'"
                )

            self.console.info("Extraction completed successfully.")
        except DownloadError as e:
            self.console.error(f"Extraction failed: {e}")
            self._cleanup_extracted_files()
            raise
        except Exception as e:
            self.console.error(
                f"Unexpected error during extraction: {e}", exc_info=True
            )
            self._cleanup_extracted_files()
            raise DownloadError(
                f"Unexpected error extracting '{self.archive_name}': {e}"
            ) from e

    def _update_server_files(self) -> None:
        """Update the server files using rsync if available, otherwise use fallback"""
        if not self.archive_name or not self.filesystem.isdir(self.archive_name):
            # Handle dry run case where dir might exist but is empty
            if (
                self.dry_run
                and self.archive_name
                and self.filesystem.exists(self.archive_name)
            ):
                self.console.info("[DRY RUN] Skipping server file update step.")
                return
            raise UpdateError(
                "Cannot update server files: Extracted update content not found."
            )

        # Decide update strategy
        if self.rsync_available and self.process_runner:
            self.console.info("Updating server files using rsync...")
            self._update_with_rsync()
        else:
            self.console.info(
                "rsync not available or no process runner. Updating server files using fallback method (move/copy)..."
            )
            self._update_with_fallback()

        self.console.info("Server files updated successfully.")

    def _update_with_rsync(self) -> None:
        """Update files using rsync"""
        if not self.process_runner:
            raise DependencyError(
                "rsync update requires a process runner, but none was provided."
            )
        if not self.rsync_available:
            raise DependencyError(
                "rsync command not found, cannot use rsync update method."
            )

        # Ensure source directory ends with / for rsync to copy contents
        source_dir = self.archive_name.rstrip("/") + "/"
        target_dir = self.server_dir

        rsync_cmd = [
            "rsync",
            "-av",
            "--delete",
            source_dir,
            target_dir,
        ]

        self.console.info(f"Running rsync: {' '.join(rsync_cmd)}")

        if self.dry_run:
            self.console.info("[DRY RUN] Skipping rsync execution.")
            return

        try:
            # Run rsync potentially with sudo if server dir requires it
            self.process_runner.run_sudo(rsync_cmd, check=True)
            self.console.info("rsync completed successfully.")
            # Ensure final ownership is correct after rsync
            self.console.debug(
                f"Ensuring ownership of server directory {target_dir}..."
            )
            self.filesystem.chown(
                target_dir, self.server_user, self.server_user, recursive=True
            )

        except ProcessError as e:
            raise UpdateError(f"rsync command failed: {e}") from e
        except FileSystemError as e:
            raise UpdateError(
                f"Filesystem error during/after rsync (e.g., chown): {e}"
            ) from e
        except Exception as e:
            raise UpdateError(f"Unexpected error during rsync update: {e}") from e

    def _update_with_fallback(self) -> None:
        """Fallback update method: move old files, copy new files"""
        self.console.warning(
            "Using fallback update method (less efficient and atomic than rsync)."
        )
        old_files_backup_dir = None
        try:
            # 1. Create a temporary backup location for old server files
            timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
            old_files_backup_dir = os.path.join(
                self.temp_dir, f"vs_old_files_{timestamp}"
            )
            self.console.info(
                f"Creating temporary backup of current server files in: {old_files_backup_dir}"
            )
            self.filesystem.mkdir(old_files_backup_dir, exist_ok=True)

            # 2. Move existing server files to the temporary backup location
            self.console.debug(
                f"Moving existing files from {self.server_dir} to {old_files_backup_dir}"
            )
            # List items in server_dir and move them individually
            items_to_move = self.filesystem.listdir(self.server_dir)
            moved_count = 0
            for item in items_to_move:
                src_path = os.path.join(self.server_dir, item)
                dst_path = os.path.join(old_files_backup_dir, item)
                try:
                    self.console.debug(f"Moving: {src_path} -> {dst_path}")
                    if self.dry_run:
                        self.console.info(
                            f"[DRY RUN] Would move {src_path} to {dst_path}"
                        )
                    else:
                        self.filesystem.move(src_path, dst_path)
                    moved_count += 1
                except FileSystemError as e:
                    # If moving fails, log and potentially try to rollback/stop
                    raise UpdateError(
                        f"Failed to move existing server file/dir '{src_path}': {e}. Update cannot proceed safely."
                    ) from e
            self.console.info(
                f"Successfully moved {moved_count} items to temporary backup."
            )

            # 3. Copy new files from extracted update path to server directory
            self.console.info(
                f"Copying new server files from {self.archive_name} to {self.server_dir}"
            )
            # Copy contents of extracted dir
            items_to_copy = self.filesystem.listdir(self.archive_name)
            copied_count = 0
            for item in items_to_copy:
                src_path = os.path.join(self.archive_name, item)
                dst_path = os.path.join(self.server_dir, item)
                try:
                    self.console.debug(f"Copying: {src_path} -> {dst_path}")
                    if self.dry_run:
                        self.console.info(
                            f"[DRY RUN] Would copy {src_path} to {dst_path}"
                        )
                    else:
                        # Use the filesystem interface copy method directly.
                        # The implementation (e.g., OsFileSystem) should handle
                        # whether src_path is a file or directory.
                        self.filesystem.copy(src_path, dst_path)
                    copied_count += 1
                except FileSystemError as e:
                    # If copying fails, log and potentially try to rollback
                    raise UpdateError(
                        f"Failed to copy new server file/dir '{src_path}': {e}. Server directory may be inconsistent."
                    ) from e
            self.console.info(
                f"Successfully copied {copied_count} items to server directory."
            )

            # 4. Set ownership of the server directory
            self.console.info(f"Setting final ownership for {self.server_dir}...")
            if not self.dry_run:
                if not self.filesystem.chown(
                    self.server_dir, self.server_user, self.server_user, recursive=True
                ):
                    self.console.warning(
                        f"Failed to set final ownership on {self.server_dir}. Check permissions manually."
                    )
            else:
                self.console.info(
                    f"[DRY RUN] Would set ownership of {self.server_dir} recursively for user {self.server_user}"
                )

            # 5. Cleanup the temporary backup of old files (optional, could keep for rollback)
            self.console.info(
                f"Removing temporary backup of old files: {old_files_backup_dir}"
            )
            if not self.dry_run:
                self.filesystem.rmtree(old_files_backup_dir)
            else:
                self.console.info(f"[DRY RUN] Would remove {old_files_backup_dir}")

        except (FileSystemError, UpdateError) as e:
            # If any step failed, re-raise the error
            raise
        except Exception as e:
            raise UpdateError(f"Unexpected error during fallback update: {e}") from e

    def _start_and_verify_server(self, new_version: str) -> None:
        """Start the server and verify its version"""
        self.console.info(f"Starting server service: {self.service_name}...")
        try:
            self.service_mgr.run_systemctl("start", self.service_name)
            self.server_stopped = False
        except ServiceError as e:
            raise ServiceError(
                f"Failed to start service '{self.service_name}' after update: {e}"
            ) from e

        # Wait for server to potentially start up
        self.console.info("Waiting for server to initialize before verification...")
        wait_time = 10
        if self.dry_run:
            self.console.info(f"[DRY RUN] Skipping {wait_time}s wait.")
        else:
            time.sleep(wait_time)

        # Check service status
        if not self.dry_run:
            if not self.service_mgr.check_server_status(self.service_name):
                # check_server_status logs warnings, raise error here
                raise ServiceError(
                    f"Service '{self.service_name}' did not become active after update."
                )
            else:
                self.console.info(f"Service '{self.service_name}' is active.")
        else:
            self.console.info(f"[DRY RUN] Skipping service active check.")

        # Verify server version (best effort)
        self.console.info("Verifying installed server version after update...")
        if self.dry_run:
            self.console.info(f"[DRY RUN] Skipping version verification.")
            return

        try:
            # Use VersionChecker's verification method
            if not self.version_checker.verify_server_version(new_version):
                # verify_server_version logs the error
                raise VersioningError(
                    f"Server version verification failed after update. Expected '{new_version}'. Check logs."
                )
            else:
                self.console.info("Server version verified successfully.")
        except VersioningError as e:
            raise
        except Exception as e:
            # Treat unexpected verification errors as warnings, as server might be okay
            self.console.warning(
                f"Could not definitively verify server version after update: {e}. Check manually."
            )

    def cleanup(self) -> None:
        """Clean up temporary files and attempt to restart server if needed"""
        self.console.info("Performing cleanup...")

        # Clean downloaded archive
        self._cleanup_downloaded_archive()

        # Clean extracted files
        self._cleanup_extracted_files()

        # Attempt to restart server if it was stopped by the script and not restarted
        if self.server_stopped:
            self.console.warning(
                "Update process ended with server potentially stopped. Attempting restart..."
            )
            try:
                self.service_mgr.run_systemctl("start", self.service_name)
                self.server_stopped = False
                self.console.info(
                    f"Restart command issued for service '{self.service_name}'."
                )
            except ServiceError as e:
                self.console.error(
                    f"Failed to issue restart command during cleanup: {e}"
                )
            except Exception as e:
                self.console.error(
                    f"Unexpected error issuing restart command during cleanup: {e}",
                    exc_info=True,
                )

        self.console.info("Cleanup finished.")

    def _cleanup_downloaded_archive(self):
        """Remove the downloaded archive file if it exists"""
        if self.archive_name and self.filesystem.exists(self.archive_name):
            self.console.debug(f"Removing downloaded archive: {self.archive_name}")
            try:
                if not self.dry_run:
                    self.filesystem.remove(self.archive_name)
                else:
                    self.console.info(f"[DRY RUN] Would remove {self.archive_name}")
            except Exception as e:
                self.console.warning(
                    f"Failed to remove downloaded archive '{self.archive_name}': {e}"
                )
        self.archive_name = ""

    def _cleanup_extracted_files(self):
        """Remove the extracted update files directory if it exists"""
        if self.archive_name and self.filesystem.exists(self.archive_name):
            self.console.debug(
                f"Removing extracted files directory: {self.archive_name}"
            )
            try:
                if not self.dry_run:
                    self.filesystem.rmtree(self.archive_name)
                else:
                    self.console.info(
                        f"[DRY RUN] Would remove directory tree {self.archive_name}"
                    )
            except Exception as e:
                self.console.warning(
                    f"Failed to remove extracted files directory '{self.archive_name}': {e}"
                )
        self.archive_name = ""
