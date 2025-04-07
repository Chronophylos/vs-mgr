class VSManagerError(Exception):
    """Base exception for vs-manager application errors."""

    pass


class ConfigError(VSManagerError):
    """Exception related to configuration loading or validation."""

    pass


class DependencyError(VSManagerError):
    """Exception related to missing system dependencies."""

    pass


class ServiceError(VSManagerError):
    """Exception related to system service interactions (systemctl)."""

    pass


class BackupError(VSManagerError):
    """Exception related to backup creation or rotation."""

    pass


class DownloadError(VSManagerError):
    """Exception related to downloading files."""

    pass


class UpdateError(VSManagerError):
    """Exception related to the server update process."""

    pass


class VerificationError(VSManagerError):
    """Exception related to verification failures (e.g., checksums, signatures)."""

    pass


class FileSystemError(VSManagerError):
    """Exception related to file system operations."""

    pass


class ProcessError(VSManagerError):
    """Exception related to running external processes."""

    pass


class VersioningError(VSManagerError):
    """Exception related to version fetching or comparison."""

    pass
