import os
import requests
import tarfile
import datetime
from typing import Optional, Tuple

from interfaces import IHttpClient, IFileSystem, IArchiver


class UpdateManager:
    """Manages the update process for Vintage Story server"""

    def __init__(
        self,
        system_interface,
        service_manager=None,
        backup_manager=None,
        version_checker=None,
        http_client: Optional[IHttpClient] = None,
        filesystem: Optional[IFileSystem] = None,
        archiver: Optional[IArchiver] = None,
        console=None,
        settings=None,
    ):
        """Initialize UpdateManager

        Args:
            system_interface: SystemInterface instance for system operations
            service_manager: ServiceManager instance for service operations (optional)
            backup_manager: BackupManager instance for backup operations (optional)
            version_checker: VersionChecker instance for version checking (optional)
            http_client: IHttpClient implementation for HTTP operations
            filesystem: IFileSystem implementation for filesystem operations
            archiver: IArchiver implementation for archive operations
            console: ConsoleManager instance for output (optional)
            settings: ServerSettings instance with configuration (optional)
        """
        self.system = system_interface
        self.service_mgr = service_manager
        self.backup_mgr = backup_manager
        self.version_checker = version_checker
        self.http_client = http_client
        self.filesystem = filesystem
        self.archiver = archiver
        self.console = console

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
        download_url = (
            f"{self.downloads_base_url}/stable/vs_server_linux-x64_{new_version}.tar.gz"
        )
        self.archive_name = f"vs_server_linux-x64_{new_version}.tar.gz"

        self._display_update_intro(new_version)

        # Verify service and URL
        if not self._verify_service_and_url(new_version, download_url):
            return False, None

        # Stop the server
        if not self._stop_server():
            return False, None

        # Create backup if requested
        backup_file = self._handle_backup(skip_backup, ignore_backup_failure)
        if backup_file is None and not skip_backup and not ignore_backup_failure:
            return False, None

        # Download and extract server files
        if not self._download_and_extract_files(download_url):
            return False, None

        # Update server files
        if not self._update_server_files():
            return False, None

        # Start server and verify
        if not self._start_and_verify_server(new_version):
            return False, None

        # Convert empty string to None for consistency
        backup_result = backup_file if backup_file else None
        self._display_update_completion(
            new_version, backup_result, skip_backup, ignore_backup_failure
        )
        return True, backup_result

    def _display_update_intro(self, new_version: str) -> None:
        """Display initial update information

        Args:
            new_version: Version being updated to
        """
        if self.console:
            self.console.print("=== Vintage Story Server Update ===", style="green")
            self.console.log_message(
                "INFO", f"Starting update to version {new_version}"
            )

            if self.system.dry_run:
                self.console.print(
                    "[DRY RUN MODE] Simulating update without making changes",
                    style="blue",
                )
                self.console.log_message(
                    "INFO", "Running in dry-run mode (simulation only)"
                )

            self.console.print(f"Target version:   {new_version}", style="cyan")
            self.console.print(f"Server directory: {self.server_dir}", style="cyan")
            self.console.print(f"Data directory:   {self.data_dir}", style="cyan")

    def _verify_service_and_url(self, new_version: str, download_url: str) -> bool:
        """Verify that the service exists and the download URL is accessible

        Args:
            new_version: Version being updated to
            download_url: URL to download the server files from

        Returns:
            bool: True if the service exists and the URL is accessible, False otherwise
        """
        # Check service existence
        if self.service_mgr and not self.service_mgr.check_service_exists(
            self.service_name
        ):
            if self.console:
                self.console.print(
                    f"Error: Service {self.service_name} does not exist. Please check the service name.",
                    style="red",
                )
                self.console.log_message(
                    "ERROR", f"Service {self.service_name} does not exist."
                )
            return False

        if self.console:
            self.console.log_message("INFO", f"Service {self.service_name} exists.")

        # Verify download URL
        if self.console:
            self.console.print(f"Verifying download URL: {download_url}", style="cyan")
        try:
            if self.http_client:
                response = self.http_client.head(download_url)
                if response.status_code != 200:
                    if self.console:
                        self.console.print(
                            "Error: Could not access download URL.", style="red"
                        )
                        self.console.print(
                            f"Check the version number ('{new_version}') and network connection.",
                            style="red",
                        )
                        self.console.log_message(
                            "ERROR", f"Failed to verify download URL: {download_url}"
                        )
                    return False
            else:
                response = requests.head(download_url, timeout=10)
                if response.status_code != 200:
                    if self.console:
                        self.console.print(
                            "Error: Could not access download URL.", style="red"
                        )
                        self.console.print(
                            f"Check the version number ('{new_version}') and network connection.",
                            style="red",
                        )
                        self.console.log_message(
                            "ERROR", f"Failed to verify download URL: {download_url}"
                        )
                    return False
        except Exception as e:
            if self.console:
                self.console.print(f"Error verifying download URL: {e}", style="red")
                self.console.log_message(
                    "ERROR", f"Failed to verify download URL: {download_url}"
                )
            return False

        if self.console:
            self.console.print("Download URL verified.", style="green")
            self.console.log_message("INFO", f"Download URL verified: {download_url}")
        return True

    def _stop_server(self) -> bool:
        """Stop the server service

        Returns:
            bool: True if the server was stopped successfully, False otherwise
        """
        if self.console:
            self.console.print(
                f"Stopping server ({self.service_name})...", style="cyan"
            )
        if not self.service_mgr or not self.service_mgr.run_systemctl(
            "stop", self.service_name
        ):
            if self.console:
                self.console.print(
                    "Error: Failed to stop the server service.", style="red"
                )
                self.console.print(
                    f"Check service status: systemctl status {self.service_name}.service",
                    style="yellow",
                )
                self.console.log_message(
                    "ERROR", f"Failed to stop server service {self.service_name}"
                )
            return False

        self.server_stopped = (
            True  # Mark that we stopped the server (for cleanup logic)
        )
        if self.console:
            self.console.print("Server stopped.", style="green")
        return True

    def _handle_backup(
        self, skip_backup: bool, ignore_backup_failure: bool
    ) -> Optional[str]:
        """Handle the backup process according to settings

        Args:
            skip_backup: Whether to skip creating a backup
            ignore_backup_failure: Whether to ignore backup failures

        Returns:
            Optional[str]: Path to the created backup file or None if skipped or failed
        """
        backup_file = ""
        if skip_backup:
            if self.console:
                self.console.print("Skipping backup as requested.", style="yellow")
                self.console.log_message(
                    "INFO", "Backup creation skipped as requested (--skip-backup)"
                )
            return backup_file

        if not self.backup_mgr:
            if self.console:
                self.console.print(
                    "No backup manager available. Skipping backup.", style="yellow"
                )
            return backup_file

        backup_result = self.backup_mgr.create_backup(ignore_backup_failure)
        if backup_result is None and not ignore_backup_failure:
            # Backup failed and ignore_failure is false
            if self.console:
                self.console.print("Update aborted due to backup failure.", style="red")
                self.console.log_message(
                    "ERROR", "Update aborted: backup creation failed"
                )
            return None

        backup_file = backup_result if backup_result else ""
        if backup_file:
            if self.console:
                self.console.print("Backup step completed successfully.", style="green")
                self.console.log_message(
                    "INFO", f"Backup created successfully at {backup_file}"
                )
        elif ignore_backup_failure:
            if self.console:
                self.console.print(
                    "Backup failed, but continuing as --ignore-backup-failure was specified.",
                    style="yellow",
                )
                self.console.log_message(
                    "WARNING",
                    "Backup creation failed but continuing (--ignore-backup-failure)",
                )

        return backup_file

    def _download_and_extract_files(self, download_url: str) -> bool:
        """Download and extract the server files

        Args:
            download_url: URL to download the server files from

        Returns:
            bool: True if successful, False otherwise
        """
        # Download the server archive
        if not self._download_server_archive(download_url):
            return False

        # Extract files
        if not self._extract_server_archive():
            return False

        # Sanity check before modifying the server directory
        if (
            not self.system.is_dir(self.server_dir)
            or not self.server_dir
            or self.server_dir == "/"
        ):
            if self.console:
                self.console.print(
                    f"CRITICAL ERROR: Invalid SERVER_DIR defined: '{self.server_dir}'. Aborting update to prevent data loss.",
                    style="red",
                )
            return False

        return True

    def _download_server_archive(self, download_url: str) -> bool:
        """Download the server archive

        Args:
            download_url: URL to download the archive from

        Returns:
            bool: True if the download was successful, False otherwise
        """
        if self.console:
            self.console.print(f"Downloading server archive...", style="cyan")
            self.console.log_message(
                "INFO", f"Downloading server archive from: {download_url}"
            )

        # Create temp directory if it doesn't exist
        if self.filesystem:
            self.filesystem.mkdir(self.temp_dir, exist_ok=True)
        else:
            self.system.run_mkdir(self.temp_dir)

        target_file = os.path.join(self.temp_dir, self.archive_name)

        if self.system.dry_run:
            if self.console:
                self.console.print(
                    f"[DRY RUN] Would download server archive to: {target_file}",
                    style="blue",
                )
            return True

        try:
            if self.http_client:
                success = self.http_client.download(download_url, target_file)
                if not success:
                    if self.console:
                        self.console.print(
                            f"Error: Failed to download server archive.", style="red"
                        )
                    return False
            else:
                response = requests.get(download_url, stream=True, timeout=300)
                if response.status_code != 200:
                    if self.console:
                        self.console.print(
                            f"Error: Download failed with status code {response.status_code}",
                            style="red",
                        )
                    return False

                with open(target_file, "wb") as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        f.write(chunk)

            if self.console:
                self.console.print(f"Download completed: {target_file}", style="green")
                self.console.log_message(
                    "INFO", f"Server archive downloaded to: {target_file}"
                )
            return True
        except Exception as e:
            if self.console:
                self.console.print(
                    f"Error downloading server archive: {e}", style="red"
                )
                self.console.log_message(
                    "ERROR", f"Failed to download server archive: {e}"
                )
            return False

    def _extract_server_archive(self) -> bool:
        """Extract the server archive

        Returns:
            bool: True if the extraction was successful, False otherwise
        """
        archive_path = os.path.join(self.temp_dir, self.archive_name)
        extract_dir = os.path.join(self.temp_dir, "extracted")

        if self.console:
            self.console.print(f"Extracting server archive...", style="cyan")
            self.console.log_message(
                "INFO", f"Extracting server archive: {archive_path} to {extract_dir}"
            )

        # Create extraction directory if it doesn't exist
        if self.filesystem:
            self.filesystem.mkdir(extract_dir, exist_ok=True)
        else:
            self.system.run_mkdir(extract_dir)

        if self.system.dry_run:
            if self.console:
                self.console.print(
                    f"[DRY RUN] Would extract server archive to: {extract_dir}",
                    style="blue",
                )
            return True

        try:
            if self.archiver:
                success = self.archiver.extractall(archive_path, extract_dir)
                if not success:
                    if self.console:
                        self.console.print(
                            f"Error: Failed to extract server archive.", style="red"
                        )
                    return False
            else:
                with tarfile.open(archive_path) as tar:
                    # Create a safer extraction function to avoid path traversal attacks
                    def is_within_directory(directory, target):
                        abs_directory = os.path.abspath(directory)
                        abs_target = os.path.abspath(target)
                        prefix = os.path.commonprefix([abs_directory, abs_target])
                        return prefix == abs_directory

                    def safe_extract(tar, path):
                        for member in tar.getmembers():
                            member_path = os.path.join(path, member.name)
                            if not is_within_directory(path, member_path):
                                raise Exception("Attempted path traversal in tar file")
                        tar.extractall(path)

                    safe_extract(tar, extract_dir)

            if self.console:
                self.console.print(f"Extraction completed.", style="green")
                self.console.log_message(
                    "INFO", f"Server archive extracted to: {extract_dir}"
                )
            return True
        except Exception as e:
            if self.console:
                self.console.print(f"Error extracting server archive: {e}", style="red")
                self.console.log_message("ERROR", f"Failed to extract archive: {e}")
            return False

    def _update_server_files(self) -> bool:
        """Update the server files using rsync or fallback method

        Returns:
            bool: True if successful, False otherwise
        """
        if self.console:
            self.console.print(
                f"Updating server files in {self.server_dir}...", style="cyan"
            )

        if self.system.rsync_available:
            return self._update_with_rsync()
        else:
            return self._update_with_fallback()

    def _update_with_rsync(self) -> bool:
        """Update server files using rsync (preferred method)

        Returns:
            bool: True if successful, False otherwise
        """
        if self.console:
            self.console.print(
                "Using rsync for safe, precise updates...", style="green"
            )
        if self.system.dry_run:
            if self.console:
                self.console.print(
                    f"[DRY RUN] Would rsync from {self.temp_dir}/ to {self.server_dir}/",
                    style="blue",
                )
            return True

        try:
            self.system.run_with_sudo(
                [
                    "rsync",
                    "-a",
                    "--delete",
                    "--exclude=serverconfig.json",
                    "--exclude=Mods/",
                    "--exclude=modconfig/",
                    f"{self.temp_dir}/",
                    f"{self.server_dir}/",
                ],
                check=True,
            )
            return True
        except Exception as e:
            if self.console:
                self.console.print(
                    f"Error: rsync failed to update server files in {self.server_dir}: {e}",
                    style="red",
                )
            return False

    def _update_with_fallback(self) -> bool:
        """Update server files using fallback method (when rsync is not available)

        Returns:
            bool: True if successful, False otherwise
        """
        if self.console:
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

        if not self.system.dry_run:
            if self.console:
                self.console.print(
                    "Do you want to continue with the fallback method? (y/N)",
                    style="yellow",
                )
                response = input().lower()
                if response not in ("y", "yes"):
                    if self.console:
                        self.console.print(
                            "Update aborted. Please install rsync and try again.",
                            style="cyan",
                        )
                    return False

        if self.console:
            self.console.print(
                "Proceeding with improved fallback update method...", style="yellow"
            )

        # Create a timestamp for the temporary backup directory
        backup_timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        old_files_dir = os.path.join(
            self.server_dir, f".old_update_files_{backup_timestamp}"
        )

        return self._move_files_to_backup(old_files_dir) and self._copy_new_files(
            old_files_dir
        )

    def _move_files_to_backup(self, old_files_dir: str) -> bool:
        """Move current server files to temporary backup location

        Args:
            old_files_dir: Directory to move files to

        Returns:
            bool: True if successful, False otherwise
        """
        if self.console:
            self.console.print(
                f"Moving current server files to temporary location: {old_files_dir}",
                style="blue",
            )
        if self.system.dry_run:
            if self.console:
                self.console.print(
                    f"[DRY RUN] Would move current server files to: {old_files_dir}",
                    style="blue",
                )
            return True

        try:
            # Create backup directory
            self.system.run_mkdir(old_files_dir)

            # List of paths to preserve
            preserve_paths = [
                os.path.join(self.server_dir, "serverconfig.json"),
                os.path.join(self.server_dir, "Mods"),
                os.path.join(self.server_dir, "modconfig"),
            ]

            # Move files except preserved ones
            for item in self.system.list_dir(self.server_dir):
                item_path = os.path.join(self.server_dir, item)

                # Skip temporary backup directories and preserved paths
                if item.startswith(".old_update_files_") or any(
                    os.path.samefile(item_path, path)
                    if os.path.exists(path)
                    else item == os.path.basename(path)
                    for path in preserve_paths
                ):
                    continue

                # Move the item to backup directory
                self.system.move(item_path, os.path.join(old_files_dir, item))
            return True
        except Exception as e:
            if self.console:
                self.console.print(
                    f"Error: Failed to move server files to temporary location: {e}",
                    style="red",
                )
            # Clean up the temporary directory
            self.system.rmtree(old_files_dir, ignore_errors=True)
            return False

    def _copy_new_files(self, old_files_dir: str) -> bool:
        """Copy new files from temporary directory to server directory

        Args:
            old_files_dir: Backup directory containing the original files

        Returns:
            bool: True if successful, False otherwise
        """
        if self.console:
            self.console.print("Copying new server files...", style="blue")
        if self.system.dry_run:
            if self.console:
                self.console.print(
                    f"[DRY RUN] Would copy new server files from {self.temp_dir} to {self.server_dir}",
                    style="blue",
                )
                self.console.print(
                    "[DRY RUN] Would remove temporary backup directory after successful update",
                    style="blue",
                )
            return True

        try:
            # Copy new files
            for item in self.system.list_dir(self.temp_dir):
                src_path = os.path.join(self.temp_dir, item)
                dst_path = os.path.join(self.server_dir, item)

                if self.system.is_dir(src_path):
                    if self.system.path_exists(dst_path):
                        self.system.rmtree(dst_path)
                    self.system.copytree(src_path, dst_path)
                else:
                    self.system.copy(src_path, dst_path)

            # If copy was successful, clean up the temporary backup
            if self.console:
                self.console.print(
                    "Update successful. Cleaning up temporary backup...", style="blue"
                )
            self.system.rmtree(old_files_dir)
            return True
        except Exception as e:
            if self.console:
                self.console.print(
                    f"Error: Failed to copy new server files: {e}", style="red"
                )
                self.console.print(
                    "Attempting to restore from temporary backup...", style="yellow"
                )

            # Attempt to restore from the temporary backup
            try:
                for item in self.system.list_dir(old_files_dir):
                    src_path = os.path.join(old_files_dir, item)
                    dst_path = os.path.join(self.server_dir, item)

                    if self.system.is_dir(src_path):
                        if self.system.path_exists(dst_path):
                            self.system.rmtree(dst_path)
                        self.system.copytree(src_path, dst_path)
                    else:
                        self.system.copy(src_path, dst_path)
                if self.console:
                    self.console.print(
                        "Restored server files from temporary backup.",
                        style="green",
                    )
            except Exception as restore_error:
                if self.console:
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
        """Start the server and verify its status and version

        Args:
            new_version: Version to verify

        Returns:
            bool: True if successful, False otherwise
        """
        # Set ownership
        if self.console:
            self.console.print(
                f"Setting ownership for {self.server_dir} to {self.server_user}:{self.server_user}...",
                style="cyan",
            )
        self.system.run_chown(
            f"{self.server_user}:{self.server_user}",
            self.server_dir,
            recursive=True,
        )
        if self.console:
            self.console.print("Server files updated.", style="green")

        # Start the server
        if self.console:
            self.console.print(
                f"Starting server ({self.service_name})...", style="cyan"
            )
        if not self.service_mgr or not self.service_mgr.run_systemctl(
            "start", self.service_name
        ):
            if self.console:
                self.console.print(
                    "Error: Failed to start the server service after update.",
                    style="red",
                )
                self.console.print(
                    f"Check service status: systemctl status {self.service_name}.service",
                    style="yellow",
                )
            return False

        self.server_stopped = False  # Mark server as successfully started
        if self.console:
            self.console.print("Server start command issued.", style="green")

        # Verify status and version after start
        status_ok = True
        version_ok = True

        if self.service_mgr:
            status_ok = self.service_mgr.check_server_status(self.service_name)

        if self.version_checker:
            version_ok = self.version_checker.verify_server_version(new_version)

        if not status_ok:
            if self.console:
                self.console.print(
                    "Warning: Server status check reported potential issues after start.",
                    style="yellow",
                )

        if not version_ok:
            if self.console:
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
        """Display update completion message

        Args:
            new_version: Version updated to
            backup_file: Path to the backup file (if any)
            skip_backup: Whether backup was skipped
            ignore_backup_failure: Whether backup failures were ignored
        """
        if self.console:
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

    def cleanup(self) -> None:
        """Clean up temporary files and restart server if necessary"""
        # Clean up temporary update directory
        if (
            self.temp_dir
            and self.system.path_exists(self.temp_dir)
            and self.temp_dir != "/"
        ):
            if self.console:
                self.console.print(
                    f"Cleaning up temporary directory: {self.temp_dir}",
                    style="blue",
                )
            if not self.system.dry_run:
                self.system.rmtree(self.temp_dir, ignore_errors=True)
            elif self.console:
                self.console.print(
                    f"[DRY RUN] Would remove temporary directory: {self.temp_dir}",
                    style="blue",
                )

        # Clean up downloaded archive
        if self.archive_name and os.path.isfile(f"/tmp/{self.archive_name}"):
            if self.console:
                self.console.print(
                    f"Cleaning up downloaded archive: /tmp/{self.archive_name}",
                    style="blue",
                )
            if not self.system.dry_run:
                os.remove(f"/tmp/{self.archive_name}")
            elif self.console:
                self.console.print(
                    f"[DRY RUN] Would remove archive: /tmp/{self.archive_name}",
                    style="blue",
                )

        # Attempt to restart server if it was stopped by this script and is not currently running
        if self.server_stopped and self.service_mgr:
            try:
                if not self.service_mgr.is_service_active(self.service_name):
                    if self.console:
                        self.console.print(
                            f"Attempting to restart server ({self.service_name}) after script interruption/error...",
                            style="yellow",
                        )
                    if self.service_mgr.check_service_exists(self.service_name):
                        if self.service_mgr.run_systemctl("start", self.service_name):
                            if self.console:
                                self.console.print(
                                    "Server restart command issued successfully.",
                                    style="green",
                                )
                                self.console.log_message(
                                    "INFO",
                                    f"Server {self.service_name} restarted after script interruption.",
                                )
                        elif self.console:
                            self.console.print(
                                f"Failed to issue server restart command. Check status manually: systemctl status {self.service_name}.service",
                                style="red",
                            )
                            self.console.log_message(
                                "ERROR",
                                f"Failed to restart server {self.service_name} after script interruption.",
                            )
                    elif self.console:
                        self.console.print(
                            f"Service {self.service_name} does not exist. Cannot restart.",
                            style="yellow",
                        )
                        self.console.log_message(
                            "WARNING",
                            f"Cannot restart non-existent service {self.service_name}.",
                        )
            except Exception as e:
                if self.console:
                    self.console.log_message(
                        "ERROR", f"Error during cleanup restart attempt: {e}"
                    )
