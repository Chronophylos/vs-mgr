import os
import shutil
import subprocess
from typing import List, Optional, Union

from interfaces import IProcessRunner, IFileSystem


class SystemInterface:
    """Interface for system operations such as executing commands and filesystem operations"""

    def __init__(
        self,
        console=None,
        process_runner: Optional[IProcessRunner] = None,
        filesystem: Optional[IFileSystem] = None,
        dry_run: bool = False,
    ):
        """Initialize SystemInterface

        Args:
            console: ConsoleManager instance for output (optional)
            process_runner: IProcessRunner implementation for executing commands
            filesystem: IFileSystem implementation for filesystem operations
            dry_run: Whether to run in dry-run mode
        """
        self.console = console
        self.process_runner = process_runner
        self.filesystem = filesystem
        self.dry_run = dry_run
        self.is_root = os.access("/root", os.W_OK)
        self.rsync_available = shutil.which("rsync") is not None

    def run_with_sudo(
        self, cmd: Union[List[str], str], **kwargs
    ) -> subprocess.CompletedProcess:
        """Execute a command with sudo if needed

        Args:
            cmd: Command as list of strings or single string
            **kwargs: Additional keyword arguments for subprocess.run

        Returns:
            subprocess.CompletedProcess: Result of the command execution
        """
        if self.dry_run:
            cmd_str = cmd if isinstance(cmd, str) else " ".join(cmd)
            if self.console:
                self.console.print(
                    f"[DRY RUN] Would run: {'sudo ' if not self.is_root else ''}{cmd_str}",
                    style="blue",
                )
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="", stderr=""
            )

        if isinstance(cmd, str):
            cmd = cmd.split()

        if self.process_runner:
            return self.process_runner.run_sudo(cmd, **kwargs)
        else:
            if self.is_root:
                return subprocess.run(cmd, **kwargs)
            else:
                cmd = ["sudo"] + cmd
                return subprocess.run(cmd, **kwargs)

    def run_mkdir(self, directory: str) -> bool:
        """Create a directory with appropriate permissions

        Args:
            directory: Directory path to create

        Returns:
            bool: True if successful, False otherwise
        """
        if self.console:
            self.console.log_message("DEBUG", f"Creating directory: {directory}")

        if self.dry_run:
            if self.console:
                self.console.print(
                    f"[DRY RUN] Would create directory: {directory}", style="blue"
                )
            return True

        try:
            if self.filesystem:
                self.filesystem.mkdir(directory, exist_ok=True)
            else:
                os.makedirs(directory, exist_ok=True)

            if self.console:
                self.console.log_message("INFO", f"Created directory: {directory}")
            return True
        except PermissionError:
            try:
                self.run_with_sudo(["mkdir", "-p", directory], check=True)
                if self.console:
                    self.console.log_message(
                        "INFO", f"Created directory with sudo: {directory}"
                    )
                return True
            except Exception as e:
                if self.console:
                    self.console.log_message(
                        "ERROR", f"Failed to create directory: {directory}: {e}"
                    )
                return False
        except Exception as e:
            if self.console:
                self.console.log_message(
                    "ERROR", f"Failed to create directory: {directory}: {e}"
                )
            return False

    def run_chown(self, owner: str, target: str, recursive: bool = False) -> bool:
        """Change ownership of a file or directory

        Args:
            owner: Owner string in format "user:group"
            target: Path to the file or directory
            recursive: Whether to apply recursively

        Returns:
            bool: True if successful, False otherwise
        """
        r_flag = "-R" if recursive else ""
        msg = (
            f"chown {r_flag} {owner} {target}"
            if recursive
            else f"chown {owner} {target}"
        )

        if self.console:
            self.console.log_message("DEBUG", f"Executing {msg}")

        if self.dry_run:
            if self.console:
                self.console.print(f"[DRY RUN] Would run: {msg}", style="blue")
            return True

        try:
            if self.filesystem:
                user, group = owner.split(":")
                return self.filesystem.chown(target, user, group, recursive)
            else:
                cmd = ["chown"]
                if recursive:
                    cmd.append("-R")
                cmd.extend([owner, target])
                self.run_with_sudo(cmd, check=True)
                if self.console:
                    self.console.log_message("INFO", f"{msg} successful")
                return True
        except Exception as e:
            if self.console:
                self.console.log_message("WARNING", f"{msg} failed: {e}")
            return False

    def which(self, command: str) -> Optional[str]:
        """Check if a command exists and return its path

        Args:
            command: Command to check for

        Returns:
            Optional[str]: Path to the command or None if not found
        """
        return shutil.which(command)

    def path_exists(self, path: str) -> bool:
        """Check if a path exists

        Args:
            path: Path to check

        Returns:
            bool: True if the path exists, False otherwise
        """
        if self.filesystem:
            return self.filesystem.exists(path)
        return os.path.exists(path)

    def is_file(self, path: str) -> bool:
        """Check if a path is a file

        Args:
            path: Path to check

        Returns:
            bool: True if the path is a file, False otherwise
        """
        if self.filesystem:
            return self.filesystem.exists(path) and not self.filesystem.isdir(path)
        return os.path.isfile(path)

    def is_dir(self, path: str) -> bool:
        """Check if a path is a directory

        Args:
            path: Path to check

        Returns:
            bool: True if the path is a directory, False otherwise
        """
        if self.filesystem:
            return self.filesystem.isdir(path)
        return os.path.isdir(path)

    def list_dir(self, path: str) -> List[str]:
        """List the contents of a directory

        Args:
            path: Directory path to list

        Returns:
            List[str]: List of filenames in the directory
        """
        if self.filesystem:
            return self.filesystem.listdir(path)
        return os.listdir(path)

    def remove(self, path: str) -> bool:
        """Remove a file

        Args:
            path: Path to the file to remove

        Returns:
            bool: True if successful, False otherwise
        """
        if self.dry_run:
            if self.console:
                self.console.print(f"[DRY RUN] Would remove file: {path}", style="blue")
            return True

        try:
            os.remove(path)
            return True
        except Exception as e:
            if self.console:
                self.console.log_message("ERROR", f"Failed to remove file {path}: {e}")
            return False

    def rmtree(self, path: str, ignore_errors: bool = False) -> bool:
        """Remove a directory tree

        Args:
            path: Path to the directory to remove
            ignore_errors: Whether to ignore errors

        Returns:
            bool: True if successful, False otherwise
        """
        if self.dry_run:
            if self.console:
                self.console.print(
                    f"[DRY RUN] Would remove directory tree: {path}", style="blue"
                )
            return True

        try:
            if self.filesystem:
                self.filesystem.rmtree(path)
            else:
                shutil.rmtree(path, ignore_errors=ignore_errors)
            return True
        except Exception as e:
            if self.console:
                self.console.log_message(
                    "ERROR", f"Failed to remove directory tree {path}: {e}"
                )
            return False

    def copy(self, src: str, dst: str) -> bool:
        """Copy a file

        Args:
            src: Source file path
            dst: Destination file path

        Returns:
            bool: True if successful, False otherwise
        """
        if self.dry_run:
            if self.console:
                self.console.print(
                    f"[DRY RUN] Would copy file: {src} to {dst}", style="blue"
                )
            return True

        try:
            shutil.copy2(src, dst)
            return True
        except Exception as e:
            if self.console:
                self.console.log_message(
                    "ERROR", f"Failed to copy file {src} to {dst}: {e}"
                )
            return False

    def copytree(self, src: str, dst: str) -> bool:
        """Copy an entire directory tree

        Args:
            src: Source directory path
            dst: Destination directory path

        Returns:
            bool: True if successful, False otherwise
        """
        if self.dry_run:
            if self.console:
                self.console.print(
                    f"[DRY RUN] Would copy directory tree: {src} to {dst}", style="blue"
                )
            return True

        try:
            shutil.copytree(src, dst)
            return True
        except Exception as e:
            if self.console:
                self.console.log_message(
                    "ERROR", f"Failed to copy directory tree {src} to {dst}: {e}"
                )
            return False

    def move(self, src: str, dst: str) -> bool:
        """Move a file or directory

        Args:
            src: Source path
            dst: Destination path

        Returns:
            bool: True if successful, False otherwise
        """
        if self.dry_run:
            if self.console:
                self.console.print(
                    f"[DRY RUN] Would move: {src} to {dst}", style="blue"
                )
            return True

        try:
            shutil.move(src, dst)
            return True
        except Exception as e:
            if self.console:
                self.console.log_message("ERROR", f"Failed to move {src} to {dst}: {e}")
            return False
