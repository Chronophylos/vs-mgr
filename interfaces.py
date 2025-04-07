from typing import Protocol, List, Any, Union, Optional, Tuple
from pathlib import Path


class IHttpClient(Protocol):
    """Protocol for HTTP client operations."""

    def get(self, url: str, stream: bool = False) -> Any:
        """Perform HTTP GET request.

        Args:
            url: The URL to request
            stream: Whether to stream the response

        Returns:
            Response object with content, status_code attributes
        """
        ...

    def head(self, url: str) -> Any:
        """Perform HTTP HEAD request.

        Args:
            url: The URL to request

        Returns:
            Response object with headers, status_code attributes
        """
        ...

    def download(self, url: str, dest_path: Union[str, Path]) -> bool:
        """Download a file from a URL to a destination path.

        Args:
            url: The URL to download from
            dest_path: The path to save the file to

        Returns:
            True if download was successful, False otherwise
        """
        ...


class IProcessRunner(Protocol):
    """Protocol for running system processes."""

    def run(
        self,
        command_args: List[str],
        check: bool = True,
        capture_output: bool = False,
        cwd: Optional[str] = None,
    ) -> Any:
        """Run a system command.

        Args:
            command_args: List of command and arguments
            check: Whether to check the return code
            capture_output: Whether to capture stdout/stderr
            cwd: Working directory to run the command in

        Returns:
            Object with returncode, stdout, stderr attributes
        """
        ...

    def run_sudo(
        self,
        command_args: List[str],
        check: bool = True,
        capture_output: bool = False,
        cwd: Optional[str] = None,
    ) -> Any:
        """Run a system command with sudo.

        Args:
            command_args: List of command and arguments
            check: Whether to check the return code
            capture_output: Whether to capture stdout/stderr
            cwd: Working directory to run the command in

        Returns:
            Object with returncode, stdout, stderr attributes
        """
        ...


class IFileSystem(Protocol):
    """Protocol for filesystem operations."""

    def mkdir(
        self, path: Union[str, Path], mode: int = 0o777, exist_ok: bool = False
    ) -> None:
        """Create a directory.

        Args:
            path: Path to create
            mode: Permission mode
            exist_ok: If False, raise an exception if directory exists
        """
        ...

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
        ...

    def copy(self, src: Union[str, Path], dst: Union[str, Path]) -> None:
        """Copy a file or directory.

        Args:
            src: Source path
            dst: Destination path
        """
        ...

    def move(self, src: Union[str, Path], dst: Union[str, Path]) -> None:
        """Move a file or directory.

        Args:
            src: Source path
            dst: Destination path
        """
        ...

    def rmtree(self, path: Union[str, Path]) -> None:
        """Remove a directory tree.

        Args:
            path: Path to remove
        """
        ...

    def remove(self, path: Union[str, Path]) -> None:
        """Remove a file.

        Args:
            path: Path to the file to remove
        """
        ...

    def exists(self, path: Union[str, Path]) -> bool:
        """Check if a path exists.

        Args:
            path: Path to check

        Returns:
            True if path exists, False otherwise
        """
        ...

    def isdir(self, path: Union[str, Path]) -> bool:
        """Check if a path is a directory.

        Args:
            path: Path to check

        Returns:
            True if path is a directory, False otherwise
        """
        ...

    def listdir(self, path: Union[str, Path]) -> List[str]:
        """List contents of a directory.

        Args:
            path: Path to list

        Returns:
            List of filenames in the directory
        """
        ...

    def getmtime(self, path: Union[str, Path]) -> float:
        """Get the modification time of a file.

        Args:
            path: Path to check

        Returns:
            Modification time as seconds since epoch
        """
        ...

    def getsize(self, path: Union[str, Path]) -> int:
        """Get the size of a file.

        Args:
            path: Path to check

        Returns:
            Size in bytes
        """
        ...

    def walk(self, path: Union[str, Path]) -> List[Tuple[str, List[str], List[str]]]:
        """Walk a directory tree.

        Args:
            path: Path to walk

        Returns:
            Generator yielding (dirpath, dirnames, filenames) tuples
        """
        ...

    def calculate_dir_size(self, path: Union[str, Path]) -> int:
        """Calculate the total size of a directory.

        Args:
            path: Path to calculate size for

        Returns:
            Size in bytes
        """
        ...


class IArchiver(Protocol):
    """Protocol for archive operations."""

    def extractall(
        self, archive_path: Union[str, Path], dest_path: Union[str, Path]
    ) -> bool:
        """Extract an archive.

        Args:
            archive_path: Path to the archive
            dest_path: Path to extract to

        Returns:
            True if successful, False otherwise
        """
        ...

    def create(
        self,
        source_dir: Union[str, Path],
        archive_path: Union[str, Path],
        exclude_patterns: List[str] = [],
    ) -> bool:
        """Create an archive.

        Args:
            source_dir: Directory to archive
            archive_path: Path to save the archive to
            exclude_patterns: Patterns to exclude

        Returns:
            True if successful, False otherwise
        """
        ...


class ICompressor(Protocol):
    """Protocol for compression operations."""

    def compress(
        self, source_path: Union[str, Path], dest_path: Union[str, Path]
    ) -> bool:
        """Compress a file.

        Args:
            source_path: Path to the file to compress
            dest_path: Path to save the compressed file to

        Returns:
            True if successful, False otherwise
        """
        ...

    def decompress(
        self, source_path: Union[str, Path], dest_path: Union[str, Path]
    ) -> bool:
        """Decompress a file.

        Args:
            source_path: Path to the compressed file
            dest_path: Path to save the decompressed file to

        Returns:
            True if successful, False otherwise
        """
        ...
