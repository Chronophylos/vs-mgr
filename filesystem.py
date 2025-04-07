import os
import shutil
from pathlib import Path
from typing import Union, List, Tuple, Optional
import subprocess

from interfaces import IFileSystem, IProcessRunner


class OsFileSystem(IFileSystem):
    """Implementation of IFileSystem using os, shutil, and pathlib."""

    def __init__(self, process_runner: Optional[IProcessRunner] = None):
        """Initialize OsFileSystem with optional process runner for privileged operations.

        Args:
            process_runner: Process runner for operations requiring privileges
        """
        self.process_runner = process_runner

    def mkdir(
        self, path: Union[str, Path], mode: int = 0o777, exist_ok: bool = False
    ) -> None:
        """Create a directory.

        Args:
            path: Path to create
            mode: Permission mode
            exist_ok: If False, raise an exception if directory exists
        """
        os.makedirs(path, mode=mode, exist_ok=exist_ok)

    def chown(
        self, path: Union[str, Path], user: str, group: str, recursive: bool = False
    ) -> bool:
        """Change ownership of a file or directory.

        Args:
            path: Path to change ownership of
            user: User to set as owner
            group: Group to set as owner
            recursive: Whether to change ownership recursively

        Returns:
            True if successful, False otherwise
        """
        if not self.process_runner:
            raise RuntimeError("Process runner required for chown operations")

        try:
            cmd = ["chown"]
            if recursive:
                cmd.append("-R")
            cmd.append(f"{user}:{group}")
            cmd.append(str(path))

            self.process_runner.run_sudo(cmd)
            return True
        except subprocess.SubprocessError:
            return False

    def copy(self, src: Union[str, Path], dst: Union[str, Path]) -> None:
        """Copy a file or directory.

        Args:
            src: Source path
            dst: Destination path
        """
        src_path = Path(src)
        if src_path.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)

    def move(self, src: Union[str, Path], dst: Union[str, Path]) -> None:
        """Move a file or directory.

        Args:
            src: Source path
            dst: Destination path
        """
        shutil.move(src, dst)

    def rmtree(self, path: Union[str, Path]) -> None:
        """Remove a directory tree.

        Args:
            path: Path to remove
        """
        shutil.rmtree(path)

    def exists(self, path: Union[str, Path]) -> bool:
        """Check if a path exists.

        Args:
            path: Path to check

        Returns:
            True if path exists, False otherwise
        """
        return os.path.exists(path)

    def isdir(self, path: Union[str, Path]) -> bool:
        """Check if a path is a directory.

        Args:
            path: Path to check

        Returns:
            True if path is a directory, False otherwise
        """
        return os.path.isdir(path)

    def listdir(self, path: Union[str, Path]) -> List[str]:
        """List contents of a directory.

        Args:
            path: Path to list

        Returns:
            List of filenames in the directory
        """
        return os.listdir(path)

    def getmtime(self, path: Union[str, Path]) -> float:
        """Get the modification time of a file.

        Args:
            path: Path to check

        Returns:
            Modification time as seconds since epoch
        """
        return os.path.getmtime(path)

    def getsize(self, path: Union[str, Path]) -> int:
        """Get the size of a file.

        Args:
            path: Path to check

        Returns:
            Size in bytes
        """
        return os.path.getsize(path)

    def remove(self, path: Union[str, Path]) -> None:
        """Remove a file.

        Args:
            path: Path to the file to remove
        """
        os.remove(path)

    def walk(self, path: Union[str, Path]) -> List[Tuple[str, List[str], List[str]]]:
        """Walk a directory tree.

        Args:
            path: Path to walk

        Returns:
            Generator yielding (dirpath, dirnames, filenames) tuples
        """
        return list(os.walk(path))

    def calculate_dir_size(self, path: Union[str, Path]) -> int:
        """Calculate the total size of a directory.

        Args:
            path: Path to calculate size for

        Returns:
            Size in bytes
        """
        total_size = 0
        for dirpath, dirnames, filenames in os.walk(path):
            for filename in filenames:
                file_path = os.path.join(dirpath, filename)
                # Skip if it's a symbolic link
                if not os.path.islink(file_path):
                    total_size += os.path.getsize(file_path)
        return total_size
