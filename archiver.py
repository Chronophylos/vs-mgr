import tarfile
import os
from pathlib import Path
from typing import Union, List
import fnmatch

from interfaces import IArchiver


class SecurityError(Exception):
    """Exception raised for security-related issues."""

    pass


class TarfileArchiver(IArchiver):
    """Implementation of IArchiver using tarfile."""

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
        try:
            # Ensure destination directory exists
            os.makedirs(dest_path, exist_ok=True)

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
                            raise SecurityError("Attempted path traversal in tar file")
                    tar.extractall(path)

                safe_extract(tar, str(dest_path))
            return True
        except (tarfile.TarError, OSError, SecurityError) as e:
            print(f"Error extracting archive {archive_path}: {e}")
            return False

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
        try:
            # Ensure parent directory of archive exists
            archive_parent = os.path.dirname(archive_path)
            if archive_parent:
                os.makedirs(archive_parent, exist_ok=True)

            # Create archive filter function
            def filter_func(tarinfo):
                # Skip if file matches exclude patterns
                if exclude_patterns:
                    for pattern in exclude_patterns:
                        if fnmatch.fnmatch(tarinfo.name, pattern):
                            return None
                return tarinfo

            # Create the tarfile
            with tarfile.open(archive_path, "w") as tar:
                tar.add(
                    source_dir, arcname=os.path.basename(source_dir), filter=filter_func
                )

            return True
        except (tarfile.TarError, OSError) as e:
            print(f"Error creating archive {archive_path}: {e}")
            # Clean up partial archive if it exists
            if os.path.exists(archive_path):
                os.remove(archive_path)
            return False
