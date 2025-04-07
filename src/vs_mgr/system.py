"""Facade class providing a unified interface for system interactions.

This module wraps filesystem operations and process execution, potentially
delegating to implementations defined by `IFileSystem` and `IProcessRunner`
interfaces if provided during initialization. It handles dry-run logic
and basic permission elevation (sudo) needs.
"""

import os
import shutil
import subprocess
from typing import List, Optional, Union, TYPE_CHECKING
from interfaces import IProcessRunner, IFileSystem
from errors import ProcessError, FileSystemError

if TYPE_CHECKING:
    from ui import ConsoleManager


class SystemInterface:
    """Provides methods for interacting with the operating system.

    Acts as a facade over lower-level filesystem and process execution modules.
    It checks for dry-run mode and can optionally use injected dependencies
    (implementations of IFileSystem and IProcessRunner) for enhanced testability.
    If dependencies are not injected, it falls back to using standard library
    modules like `os`, `shutil`, and `subprocess` directly.

    Attributes:
        console (ConsoleManager): Instance for logging and user output.
        process_runner (Optional[IProcessRunner]): Delegate for process execution.
        filesystem (Optional[IFileSystem]): Delegate for filesystem operations.
        dry_run (bool): If True, operations are logged but not executed.
        is_root (bool): True if the script is likely running as root (Unix-like).
        rsync_available (bool): True if the 'rsync' command is found in PATH.
    """

    def __init__(
        self,
        console: "ConsoleManager",
        process_runner: Optional[IProcessRunner] = None,
        filesystem: Optional[IFileSystem] = None,
        dry_run: bool = False,
    ):
        """Initializes the SystemInterface.

        Args:
            console: The ConsoleManager instance for logging/output.
            process_runner: An object implementing the IProcessRunner interface.
            filesystem: An object implementing the IFileSystem interface.
            dry_run: Flag indicating if operations should be simulated.
        """
        self.console = console
        self.process_runner = process_runner
        self.filesystem = filesystem
        self.dry_run = dry_run
        # Check for root privileges (best effort for Unix-like)
        self.is_root = hasattr(os, "geteuid") and os.geteuid() == 0  # type: ignore
        self.rsync_available = self.which("rsync") is not None
        self.console.debug(
            f"SystemInterface initialized. Dry run: {self.dry_run}, Root: {self.is_root}, Rsync: {self.rsync_available}"
        )

    def run_with_sudo(
        self, cmd: Union[List[str], str], check: bool = True, **kwargs
    ) -> subprocess.CompletedProcess:
        """Executes a command, prepending 'sudo' if not running as root.

        Delegates to `IProcessRunner.run_sudo` if available, otherwise uses `subprocess`.
        Handles dry-run mode.

        Args:
            cmd: The command to run, either as a string or list of strings.
            check: If True, raises CalledProcessError on non-zero exit code. Defaults to True.
            **kwargs: Additional keyword arguments passed to the underlying run command
                      (e.g., `capture_output=True`, `cwd`).

        Returns:
            A CompletedProcess object representing the command result.

        Raises:
            ProcessError: If the command fails (and check=True) or command not found.
            FileNotFoundError: If the command executable is not found (caught and wrapped).
        """
        cmd_list: List[str] = cmd.split() if isinstance(cmd, str) else list(cmd)
        cmd_str = " ".join(cmd_list)

        if self.dry_run:
            prefix = "sudo " if not self.is_root else ""
            self.console.info(f"[DRY RUN] Would execute: {prefix}{cmd_str}")
            return subprocess.CompletedProcess(
                args=cmd_list, returncode=0, stdout=b"", stderr=b""
            )

        self.console.debug(f"Executing: {'sudo ' if not self.is_root else ''}{cmd_str}")
        try:
            if self.process_runner:
                # Assume IProcessRunner returns a CompletedProcess-like object or raises
                return self.process_runner.run_sudo(cmd_list, check=check, **kwargs)
            else:
                run_cmd = ["sudo"] + cmd_list if not self.is_root else cmd_list
                if kwargs.get("capture_output") and "text" not in kwargs:
                    kwargs["text"] = True
                result = subprocess.run(run_cmd, check=check, **kwargs)
                if result.stdout:
                    self.console.debug(f"Command stdout: {result.stdout.strip()}")
                if result.stderr:
                    self.console.debug(f"Command stderr: {result.stderr.strip()}")
                return result
        except subprocess.CalledProcessError as e:
            err_msg = (
                f"Command '{' '.join(e.cmd)}' failed with exit code {e.returncode}"
            )
            stderr_info = (
                e.stderr.strip()
                if isinstance(e.stderr, str)
                else e.stderr.decode().strip()
            )
            if stderr_info:
                err_msg += f": {stderr_info}"
            self.console.error(err_msg)
            raise ProcessError(err_msg) from e
        except FileNotFoundError as e:
            err_msg = f"Command not found: {cmd_list[0]}"
            self.console.error(err_msg)
            raise ProcessError(err_msg) from e  # Wrap FileNotFoundError in ProcessError
        except Exception as e:
            err_msg = f"An unexpected error occurred running command '{cmd_str}': {e}"
            self.console.error(err_msg, exc_info=True)
            raise ProcessError(err_msg) from e

    def run_mkdir(
        self, directory: str, owner: Optional[str] = None, recursive: bool = True
    ) -> None:
        """Creates a directory, ensuring parent directories exist, and optionally sets ownership.

        Uses `IFileSystem.mkdir` if available (assuming it handles recursion implicitly
        based on the `exist_ok` flag), otherwise `os.makedirs` or `sudo mkdir`.
        Handles dry-run mode.

        Args:
            directory: The absolute path of the directory to create.
            owner: Optional owner string ("user:group") to set after creation.
            recursive: Create parent directories if needed (used for fallback `mkdir -p`).
                       Defaults to True.

        Raises:
            FileSystemError: If the directory creation or ownership setting fails.
        """
        self.console.debug(f"Ensuring directory exists: {directory}")
        if self.dry_run:
            self.console.info(f"[DRY RUN] Would ensure directory exists: {directory}")
            if owner:
                self.console.info(f"[DRY RUN] Would chown {directory} to {owner}")
            return

        try:
            if self.filesystem:
                # IFileSystem.mkdir takes exist_ok, implicitly handling recursion
                self.filesystem.mkdir(directory, exist_ok=True)
                self.console.debug(f"IFileSystem.mkdir called for: {directory}")
            else:
                # Standard library attempt first
                try:
                    # os.makedirs handles recursion via exist_ok=True
                    os.makedirs(directory, exist_ok=True)
                    self.console.debug(
                        f"Created/verified directory using os.makedirs: {directory}"
                    )
                except PermissionError:
                    self.console.debug(
                        f"Permission denied for {directory}, attempting sudo mkdir."
                    )
                    cmd = ["mkdir"] + (["-p"] if recursive else []) + [directory]
                    self.run_with_sudo(
                        cmd, check=True
                    )  # Raises ProcessError on failure
                    self.console.info(f"Created directory with sudo: {directory}")

            # Set ownership if requested (after directory exists)
            if owner:
                self.run_chown(owner, directory, recursive=False)

        except (ProcessError, FileSystemError, OSError) as e:
            # Catch errors from run_with_sudo, run_chown, os.makedirs, or filesystem.mkdir
            err_msg = f"Failed to create or set owner for directory '{directory}': {e}"
            self.console.error(err_msg)
            # Wrap in FileSystemError for consistency
            if isinstance(e, (FileSystemError, ProcessError)):
                raise
            else:
                raise FileSystemError(err_msg) from e

    def run_chown(self, owner: str, target: str, recursive: bool = False) -> None:
        """Changes the ownership of a file or directory.

        Uses `IFileSystem.chown` if available, otherwise `sudo chown`.
        Handles dry-run mode.

        Args:
            owner: Owner string in the format "user:group".
            target: The path to the file or directory.
            recursive: If True, applies ownership change recursively (for directories).

        Raises:
            FileSystemError: If the ownership change fails, owner format is invalid,
                            or the target does not exist.
        """
        if not self.path_exists(target):
            # Use FileSystemError consistent with other methods
            raise FileSystemError(f"Target path for chown does not exist: {target}")

        r_flag = " -R" if recursive else ""
        action_desc = f"chown{r_flag} {owner} {target}"
        self.console.debug(f"Attempting: {action_desc}")

        if self.dry_run:
            self.console.info(f"[DRY RUN] Would execute: {action_desc}")
            return

        try:
            user, group = owner.split(":", 1)
        except ValueError:
            err_msg = f"Invalid owner format '{owner}'. Expected 'user:group'."
            self.console.error(err_msg)
            raise FileSystemError(err_msg)

        try:
            if self.filesystem:
                # Assume IFileSystem handles sudo internally if needed
                # The protocol returns bool, let's adapt
                success = self.filesystem.chown(target, user, group, recursive)
                if not success:
                    raise FileSystemError(
                        f"{action_desc} failed (reported by IFileSystem)"
                    )
                self.console.info(f"{action_desc} successful (via IFileSystem)")
            else:
                # Fallback to using chown command via run_with_sudo
                cmd = ["chown"] + (["-R"] if recursive else []) + [owner, target]
                self.run_with_sudo(cmd, check=True)  # Raises ProcessError on failure
                self.console.info(f"{action_desc} successful (via process)")

        except (ProcessError, FileSystemError) as e:
            # Catch errors from run_with_sudo or filesystem.chown
            err_msg = f"{action_desc} failed: {e}"
            self.console.error(err_msg)
            # Re-raise as FileSystemError if it wasn't already
            if isinstance(e, FileSystemError):
                raise
            else:
                raise FileSystemError(err_msg) from e

    def which(self, command: str) -> Optional[str]:
        """Finds the path to an executable command using `shutil.which`.

        Args:
            command: The name of the command to find.

        Returns:
            The absolute path to the command, or None if not found in PATH.
        """
        path = shutil.which(command)
        self.console.debug(f"shutil.which('{command}') found: {path}")
        return path

    def path_exists(self, path: str) -> bool:
        """Checks if a file or directory exists at the given path.

        Uses `IFileSystem.exists` if available, otherwise `os.path.exists`.

        Args:
            path: The path to check.

        Returns:
            True if the path exists, False otherwise.
        """
        try:
            if self.filesystem:
                return self.filesystem.exists(path)
            return os.path.exists(path)
        except Exception as e:
            # Log unexpected errors during check, but still return False
            self.console.warning(f"Error checking existence of '{path}': {e}")
            return False

    def is_file(self, path: str) -> bool:
        """Checks if the given path exists and is a regular file.

        Uses `IFileSystem.exists` and not `IFileSystem.isdir` if available,
        otherwise `os.path.isfile`.

        Args:
            path: The path to check.

        Returns:
            True if the path is a regular file, False otherwise.
        """
        try:
            if self.filesystem:
                # IFileSystem doesn't have isfile, use exists and not isdir
                return self.filesystem.exists(path) and not self.filesystem.isdir(path)
            return os.path.isfile(path)
        except Exception as e:
            self.console.warning(f"Error checking if '{path}' is a file: {e}")
            return False

    def is_dir(self, path: str) -> bool:
        """Checks if the given path exists and is a directory.

        Uses `IFileSystem.isdir` if available, otherwise `os.path.isdir`.

        Args:
            path: The path to check.

        Returns:
            True if the path is a directory, False otherwise.
        """
        try:
            if self.filesystem:
                return self.filesystem.isdir(path)
            return os.path.isdir(path)
        except Exception as e:
            self.console.warning(f"Error checking if '{path}' is a directory: {e}")
            return False

    def list_dir(self, path: str) -> List[str]:
        """Lists the contents (files and directories) of a given directory.

        Uses `IFileSystem.listdir` if available, otherwise `os.listdir`.

        Args:
            path: The directory path to list.

        Returns:
            A list of names of the entries in the directory.

        Raises:
            FileNotFoundError: If the path does not exist or is not a directory.
            PermissionError: If permission is denied to list the directory.
            FileSystemError: For other unexpected OS errors.
        """
        self.console.debug(f"Listing directory: {path}")
        try:
            if self.filesystem:
                return self.filesystem.listdir(path)
            return os.listdir(path)
        except (FileNotFoundError, PermissionError) as e:
            self.console.error(f"Failed to list directory '{path}': {e}")
            raise  # Re-raise specific, expected errors
        except Exception as e:
            err_msg = f"Unexpected error listing directory '{path}': {e}"
            self.console.error(err_msg)
            raise FileSystemError(err_msg) from e

    def remove(self, path: str) -> None:
        """Removes a file.

        Uses `IFileSystem.remove` if available, otherwise `os.remove`.
        Handles dry-run mode.

        Args:
            path: The path to the file to remove.

        Raises:
            FileNotFoundError: If the path does not exist or is a directory.
            PermissionError: If permission is denied to remove the file.
            FileSystemError: For other unexpected OS errors.
        """
        self.console.debug(f"Attempting to remove file: {path}")
        if self.dry_run:
            self.console.info(f"[DRY RUN] Would remove file: {path}")
            return

        try:
            if self.filesystem:
                self.filesystem.remove(path)
                self.console.debug(f"IFileSystem.remove called for: {path}")
            else:
                os.remove(path)
                self.console.info(f"Removed file: {path}")
        except (FileNotFoundError, PermissionError, IsADirectoryError) as e:
            self.console.error(f"Failed to remove file '{path}': {e}")
            raise
        except Exception as e:
            err_msg = f"Unexpected error removing file '{path}': {e}"
            self.console.error(err_msg)
            raise FileSystemError(err_msg) from e

    def rmtree(self, path: str, ignore_errors: bool = False) -> None:
        """Recursively removes a directory and its contents.

        Uses `IFileSystem.rmtree` if available (assuming it handles errors internally
        if its signature lacks `ignore_errors`), otherwise `shutil.rmtree`.
        Handles dry-run mode.

        Args:
            path: The path to the directory tree to remove.
            ignore_errors: If True, errors during removal will be logged as warnings
                           but not raised. Defaults to False.

        Raises:
            FileNotFoundError: If the path does not exist (and ignore_errors is False).
            NotADirectoryError: If the path is not a directory (and ignore_errors is False).
            PermissionError: If permission denied (and ignore_errors is False).
            FileSystemError: For unexpected OS errors (and ignore_errors is False).
        """
        self.console.debug(f"Attempting to remove directory tree: {path}")
        if self.dry_run:
            self.console.info(f"[DRY RUN] Would remove directory tree: {path}")
            return

        try:
            if self.filesystem:
                # IFileSystem.rmtree doesn't have ignore_errors, handle it here
                try:
                    self.filesystem.rmtree(path)
                    self.console.debug(f"IFileSystem.rmtree called for: {path}")
                except Exception as fs_e:
                    if not ignore_errors:
                        raise  # Re-raise if not ignoring
                    else:
                        self.console.warning(
                            f"Ignoring error during IFileSystem.rmtree('{path}'): {fs_e}"
                        )
            else:
                shutil.rmtree(path, ignore_errors=ignore_errors)
                self.console.info(f"Removed directory tree: {path}")
        except (FileNotFoundError, NotADirectoryError, PermissionError) as e:
            err_msg = f"Failed to remove directory tree '{path}': {e}"
            self.console.error(err_msg)
            if not ignore_errors:
                raise  # Re-raise specific, expected errors
        except Exception as e:
            # Catch potential errors from filesystem.rmtree if not ignored, or other unexpected issues
            err_msg = f"Unexpected error removing directory tree '{path}': {e}"
            self.console.error(err_msg)
            if not ignore_errors:
                raise FileSystemError(err_msg) from e
            else:
                # This case might be redundant if shutil handles ignore_errors internally,
                # but good for catching other potential exceptions.
                self.console.warning(
                    f"Ignoring unexpected error during rmtree: {err_msg}"
                )

    def copy(self, src: str, dst: str) -> None:
        """Copies a single file from source to destination.

        Uses `IFileSystem.copy` if available, otherwise `shutil.copy2`.
        Handles dry-run mode.

        Args:
            src: Path to the source file.
            dst: Path to the destination file or directory.

        Raises:
            FileNotFoundError: If the source file does not exist.
            PermissionError: If permission is denied.
            IsADirectoryError: If source is a directory (use copytree instead).
            shutil.SameFileError: If src and dst are the same file.
            FileSystemError: For other unexpected OS errors.
        """
        self.console.debug(f"Attempting to copy file: {src} to {dst}")
        if self.dry_run:
            self.console.info(f"[DRY RUN] Would copy file: {src} to {dst}")
            return

        try:
            if self.filesystem:
                # Assume IFileSystem.copy handles file vs dir src appropriately or raises
                self.filesystem.copy(src, dst)
                self.console.debug(f"IFileSystem.copy called for: {src} -> {dst}")
            else:
                # Check if source is a directory before calling copy2
                if os.path.isdir(src):
                    raise IsADirectoryError(
                        f"Source '{src}' is a directory. Use copytree instead."
                    )
                shutil.copy2(src, dst)  # Preserves metadata
                self.console.info(f"Copied file: {src} to {dst}")
        except (
            FileNotFoundError,
            PermissionError,
            IsADirectoryError,
            shutil.SameFileError,
        ) as e:
            self.console.error(f"Failed to copy file '{src}' to '{dst}': {e}")
            raise  # Re-raise specific errors
        except Exception as e:
            err_msg = f"Unexpected error copying file '{src}' to '{dst}': {e}"
            self.console.error(err_msg)
            raise FileSystemError(err_msg) from e

    def copytree(self, src: str, dst: str, dirs_exist_ok: bool = True) -> None:
        """Recursively copies a directory tree from source to destination.

        Uses `shutil.copytree` as `IFileSystem` doesn't define it.
        Handles dry-run mode.

        Args:
            src: Path to the source directory.
            dst: Path to the destination directory.
            dirs_exist_ok: If True, allows copying into an existing directory.
                           Defaults to True.

        Raises:
            FileNotFoundError: If src does not exist.
            NotADirectoryError: If src is not a directory.
            FileExistsError: If `dst` exists and `dirs_exist_ok` is False.
            PermissionError: If permissions are insufficient.
            FileSystemError: For other unexpected OS errors.
        """
        self.console.debug(f"Attempting to copy directory tree: {src} to {dst}")
        if self.dry_run:
            self.console.info(f"[DRY RUN] Would copy directory tree: {src} to {dst}")
            return

        try:
            # IFileSystem does not have copytree, use shutil directly
            if self.filesystem:
                self.console.warning(
                    "IFileSystem does not define copytree, falling back to shutil.copytree"
                )
                # We could potentially try to implement copytree using IFileSystem primitives
                # (walk, mkdir, copy) but that's complex. Fallback is simpler for now.

            # Check if source is a file before calling copytree
            if os.path.isfile(src):
                raise NotADirectoryError(f"Source '{src}' is a file. Use copy instead.")

            shutil.copytree(src, dst, dirs_exist_ok=dirs_exist_ok)
            self.console.info(f"Copied directory tree: {src} to {dst}")

        except (
            FileNotFoundError,
            NotADirectoryError,
            FileExistsError,
            PermissionError,
        ) as e:
            self.console.error(f"Failed to copy directory tree '{src}' to '{dst}': {e}")
            raise  # Re-raise specific errors
        except Exception as e:
            err_msg = f"Unexpected error copying directory tree '{src}' to '{dst}': {e}"
            self.console.error(err_msg)
            raise FileSystemError(err_msg) from e

    def move(self, src: str, dst: str) -> None:
        """Moves a file or directory from source to destination.

        Uses `IFileSystem.move` if available, otherwise `shutil.move`.
        Handles dry-run mode.

        Args:
            src: Path to the source file or directory.
            dst: Path to the destination.

        Raises:
            FileNotFoundError: If the source does not exist.
            PermissionError: If permission is denied.
            shutil.Error: For other move failures (e.g., destination exists on different filesystem).
            FileSystemError: For other unexpected OS errors.
        """
        self.console.debug(f"Attempting to move: {src} to {dst}")
        if self.dry_run:
            self.console.info(f"[DRY RUN] Would move: {src} to {dst}")
            return

        try:
            if self.filesystem:
                self.filesystem.move(src, dst)
                self.console.debug(f"IFileSystem.move called for: {src} -> {dst}")
            else:
                shutil.move(src, dst)
                self.console.info(f"Moved: {src} to {dst}")
        except (FileNotFoundError, PermissionError, shutil.Error) as e:
            self.console.error(f"Failed to move '{src}' to '{dst}': {e}")
            raise  # Re-raise specific errors
        except Exception as e:
            err_msg = f"Unexpected error moving '{src}' to '{dst}': {e}"
            self.console.error(err_msg)
            raise FileSystemError(err_msg) from e
