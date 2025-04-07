import time
from typing import TYPE_CHECKING

from interfaces import IProcessRunner
from errors import ServiceError

if TYPE_CHECKING:
    from system import SystemInterface
    from ui import ConsoleManager


class ServiceManager:
    """Manages systemd service operations using IProcessRunner."""

    def __init__(
        self,
        system_interface: "SystemInterface",
        process_runner: IProcessRunner,
        console: "ConsoleManager",
    ):
        """Initialize ServiceManager.

        Args:
            system_interface: SystemInterface instance for system operations
            process_runner: IProcessRunner implementation for process operations
            console: ConsoleManager instance for output
        """
        self.system = system_interface
        self.process_runner = process_runner
        self.console = console

    def check_service_exists(self, service_name: str) -> bool:
        """Check if the systemd service exists.

        Args:
            service_name: Name of the service to check

        Returns:
            bool: True if the service exists, False otherwise
        """
        self.console.debug(f"Checking existence of service: {service_name}.service")
        try:
            result = self.process_runner.run(
                ["systemctl", "list-unit-files", f"{service_name}.service"],
                check=False,
                capture_output=True,
            )
            exists = f"{service_name}.service" in result.stdout
            self.console.debug(f"Service '{service_name}' exists: {exists}")
            return exists
        except Exception as e:
            self.console.error(
                f"Failed to check existence for service '{service_name}': {e}",
                exc_info=True,
            )
            return False

    def run_systemctl(self, action: str, service_name: str) -> None:
        """Execute systemctl command on a service, raising ServiceError on failure.

        Args:
            action: Action to perform (start, stop, restart, status)
            service_name: Name of the service
        """
        cmd_list = ["systemctl", action, f"{service_name}.service"]
        cmd_str = " ".join(cmd_list)
        self.console.info(f"Executing: {cmd_str}")

        if self.system.dry_run:
            self.console.info(f"[DRY RUN] Would run: {cmd_str}")
            return

        try:
            self.system.run_with_sudo(cmd_list, check=True)
            self.console.info(f"Successfully executed: {cmd_str}")
        except Exception as e:
            err_msg = f"Failed to execute '{cmd_str}': {e}"
            self.console.error(err_msg)
            raise ServiceError(err_msg) from e

    def is_service_active(self, service_name: str) -> bool:
        """Check if a systemd service is active.

        Args:
            service_name: Name of the service to check

        Returns:
            bool: True if the service is active, False otherwise
        """
        self.console.debug(f"Checking active state for service: {service_name}.service")
        try:
            result = self.process_runner.run(
                ["systemctl", "is-active", f"{service_name}.service"],
                check=False,
                capture_output=True,
            )
            is_active = result.stdout.strip() == "active"
            self.console.debug(f"Service '{service_name}' active state: {is_active}")
            return is_active
        except Exception as e:
            self.console.warning(
                f"Could not determine active state for service '{service_name}': {e}",
                exc_info=False,
            )
            return False

    def check_server_status(
        self, service_name: str, max_attempts: int = 5, wait_time: int = 3
    ) -> bool:
        """Check if the server service becomes active within a given time.

        Args:
            service_name: Name of the service to check
            max_attempts: Maximum number of attempts to check status
            wait_time: Time to wait between attempts in seconds

        Returns:
            bool: True if the server is running, False otherwise
        """
        self.console.info(f"Waiting for service '{service_name}' to become active...")

        for i in range(1, max_attempts + 1):
            if self.is_service_active(service_name):
                self.console.info(f"Service '{service_name}' is active.")
                return True

            if i < max_attempts:
                self.console.debug(
                    f"Service not active yet. Waiting {wait_time}s... (Attempt {i}/{max_attempts})"
                )
                time.sleep(wait_time)
            else:
                self.console.warning(
                    f"Service '{service_name}' did not become active after {max_attempts} checks ({max_attempts * wait_time} seconds)."
                )
                self.console.warning(
                    f"Check status manually: sudo systemctl status {service_name}.service"
                )
                return False

        return False

    def get_service_status(self, service_name: str) -> str:
        """Get the status: 'running', 'stopped', 'not-found', or 'error'.

        Args:
            service_name: Name of the service to check

        Returns:
            str: Status of the service
        """
        self.console.debug(f"Getting status for service '{service_name}'.")
        try:
            if self.is_service_active(service_name):
                self.console.debug(f"Service '{service_name}' is active (running).")
                return "running"

            if self.check_service_exists(service_name):
                self.console.debug(
                    f"Service '{service_name}' exists but is not active (stopped)."
                )
                return "stopped"
            else:
                self.console.debug(f"Service '{service_name}' unit file not found.")
                return "not-found"

        except Exception as e:
            self.console.error(
                f"Error getting status for service '{service_name}': {e}", exc_info=True
            )
            return "error"
