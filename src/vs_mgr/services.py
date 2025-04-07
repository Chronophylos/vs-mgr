"""Manages interactions with system services, primarily systemd, via a process runner."""

import time
from typing import TYPE_CHECKING, Literal

from vs_mgr.interfaces import IProcessRunner
from vs_mgr.errors import ServiceError, ProcessError

if TYPE_CHECKING:
    from vs_mgr.system import SystemInterface
    from vs_mgr.ui import ConsoleManager

# Define a type for service status
ServiceStatus = Literal["running", "stopped", "not-found", "error"]


class ServiceManager:
    """Provides methods to control and query systemd services.

    Uses an injected `IProcessRunner` to execute `systemctl` commands.
    Relies on `SystemInterface` for dry-run checks and sudo execution.

    Attributes:
        system (SystemInterface): Interface for system operations (like dry_run).
        process_runner (IProcessRunner): Interface for running external processes.
        console (ConsoleManager): Interface for logging and console output.
    """

    def __init__(
        self,
        system_interface: "SystemInterface",
        process_runner: IProcessRunner,
        console: "ConsoleManager",
    ):
        """Initializes the ServiceManager.

        Args:
            system_interface: The SystemInterface instance.
            process_runner: The IProcessRunner implementation.
            console: The ConsoleManager instance.
        """
        self.system = system_interface
        self.process_runner = process_runner
        self.console = console

    def _run_systemctl_status_check(self, args: list[str]) -> str:
        """Helper to run systemctl checks that don't require sudo and return stdout."""
        try:
            # Status checks usually don't need sudo
            result = self.process_runner.run(
                args,
                check=False,  # Don't raise on non-zero, check stdout/return code
                capture_output=True,
            )
            # Decode stdout bytes to string
            output = result.stdout.decode("utf-8").strip() if result.stdout else ""
            return output
        except ProcessError as e:
            # Log if process runner itself failed (e.g., systemctl not found)
            self.console.error(f"Failed to run systemctl command {' '.join(args)}: {e}")
            raise ServiceError(f"Failed to query service status: {e}") from e
        except Exception as e:
            self.console.error(
                f"Unexpected error running {' '.join(args)}: {e}", exc_info=True
            )
            raise ServiceError(f"Unexpected error querying service status: {e}") from e

    def check_service_exists(self, service_name: str) -> bool:
        """Checks if the systemd service unit file exists.

        Args:
            service_name: The name of the service (without .service).

        Returns:
            True if the service unit file exists, False otherwise.

        Raises:
            ServiceError: If the check command fails unexpectedly.
        """
        unit_name = f"{service_name}.service"
        self.console.debug(f"Checking existence of unit file: {unit_name}")
        try:
            # 'list-unit-files' output includes the unit name if it exists
            output = self._run_systemctl_status_check(
                ["systemctl", "list-unit-files", unit_name]
            )
            exists = unit_name in output
            self.console.debug(f"Unit file '{unit_name}' exists: {exists}")
            return exists
        except ServiceError:
            # Already logged in helper, just return False for existence check
            return False
        except Exception as e:  # Catch any other unexpected error
            self.console.error(
                f"Unexpected error checking existence for service '{service_name}': {e}"
            )
            return False

    def run_systemctl_action(
        self,
        action: Literal["start", "stop", "restart", "enable", "disable"],
        service_name: str,
    ) -> None:
        """Executes a systemctl action (start, stop, restart, enable, disable) on a service.

        Requires sudo privileges.

        Args:
            action: The systemctl action to perform.
            service_name: The name of the service (without .service).

        Raises:
            ServiceError: If the systemctl command fails.
        """
        unit_name = f"{service_name}.service"
        cmd_list = ["systemctl", action, unit_name]
        cmd_str = " ".join(cmd_list)
        self.console.info(f"Requesting action '{action}' for service '{unit_name}'")

        # Dry run check is handled within system.run_with_sudo
        # try/except for ProcessError is handled within system.run_with_sudo
        try:
            # Use the SystemInterface to handle sudo and potential ProcessErrors
            self.system.run_with_sudo(cmd_list, check=True)
            self.console.info(f"Successfully executed: {cmd_str}")
        except ProcessError as e:
            # Wrap ProcessError in ServiceError for context
            err_msg = f"Failed to execute '{cmd_str}': {e}"
            self.console.error(err_msg)
            raise ServiceError(err_msg) from e
        except Exception as e:
            # Catch other potential unexpected errors
            err_msg = f"Unexpected error during '{cmd_str}': {e}"
            self.console.error(err_msg, exc_info=True)
            raise ServiceError(err_msg) from e

    def is_service_active(self, service_name: str) -> bool:
        """Checks if a systemd service is currently active (running).

        Args:
            service_name: The name of the service (without .service).

        Returns:
            True if the service is active, False otherwise or if status cannot be determined.

        Raises:
            ServiceError: If the status check command fails unexpectedly.
        """
        unit_name = f"{service_name}.service"
        self.console.debug(f"Checking active state for service: {unit_name}")
        try:
            # 'is-active' returns exit code 0 and prints "active" if active
            result = self.process_runner.run(
                ["systemctl", "is-active", unit_name],
                check=False,  # is-active returns non-zero for inactive states
                capture_output=True,
            )
            # Check both exit code and stdout for robustness
            stdout_str = result.stdout.decode("utf-8").strip() if result.stdout else ""
            is_active = result.returncode == 0 and stdout_str == "active"
            self.console.debug(f"Service '{unit_name}' active state: {is_active}")
            return is_active
        except ProcessError as e:
            # If the runner itself failed (e.g., systemctl not found)
            self.console.error(f"Failed to check active state for '{unit_name}': {e}")
            raise ServiceError(
                f"Failed to check active state for '{unit_name}': {e}"
            ) from e
        except Exception as e:
            # Catch other unexpected errors, assume inactive
            self.console.warning(
                f"Could not determine active state for service '{unit_name}': {e}. Assuming inactive.",
                exc_info=False,  # Don't need full traceback for a warning
            )
            return False

    def wait_for_service_active(
        self, service_name: str, max_attempts: int = 5, wait_time: int = 3
    ) -> bool:
        """Waits for a service to become active, checking periodically.

        Args:
            service_name: The name of the service (without .service).
            max_attempts: Maximum number of times to check the status.
            wait_time: Seconds to wait between attempts.

        Returns:
            True if the service becomes active within the attempts, False otherwise.

        Raises:
            ServiceError: If a status check command fails unexpectedly during polling.
        """
        unit_name = f"{service_name}.service"
        self.console.info(f"Waiting for service '{unit_name}' to become active...")

        for i in range(1, max_attempts + 1):
            try:
                if self.is_service_active(service_name):
                    self.console.info(f"Service '{unit_name}' is active.")
                    return True
            except ServiceError as e:
                # If a check fails, raise it, as we can't know the state
                self.console.error(f"Error checking service status during wait: {e}")
                raise

            if i < max_attempts:
                self.console.debug(
                    f"Service not active yet. Waiting {wait_time}s... (Attempt {i}/{max_attempts})"
                )
                time.sleep(wait_time)
            else:
                self.console.warning(
                    f"Service '{unit_name}' did not become active after {max_attempts} checks ({max_attempts * wait_time} seconds)."
                )
                # Suggest manual check
                self.console.warning(
                    f"Check status manually: sudo systemctl status {unit_name}"
                )
                return False

        return (
            False  # Should not be reached due to the loop logic, but added for clarity
        )

    def get_service_status(self, service_name: str) -> ServiceStatus:
        """Determines the overall status of a service.

        Checks if the service is active, then if it exists.

        Args:
            service_name: The name of the service (without .service).

        Returns:
            A literal string: 'running', 'stopped', 'not-found', or 'error'.
        """
        unit_name = f"{service_name}.service"
        self.console.debug(f"Getting comprehensive status for service '{unit_name}'.")
        try:
            if self.is_service_active(service_name):
                self.console.debug(f"Service '{unit_name}' determined to be: running")
                return "running"

            # If not active, check if the unit file exists
            if self.check_service_exists(service_name):
                self.console.debug(f"Service '{unit_name}' determined to be: stopped")
                return "stopped"
            else:
                self.console.debug(f"Service '{unit_name}' determined to be: not-found")
                return "not-found"

        except ServiceError as e:
            # Handle errors raised by the check methods
            self.console.error(
                f"Error getting status for service '{unit_name}': {e}", exc_info=True
            )
            return "error"
        except Exception as e:
            # Catch any other unexpected errors
            self.console.error(
                f"Unexpected error getting status for service '{unit_name}': {e}",
                exc_info=True,
            )
            return "error"
