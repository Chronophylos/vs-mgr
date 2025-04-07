import time
import subprocess
from typing import Optional

from interfaces import IProcessRunner


class ServiceManager:
    """Manages systemd service operations"""

    def __init__(
        self,
        system_interface,
        process_runner: Optional[IProcessRunner] = None,
        console=None,
    ):
        """Initialize ServiceManager

        Args:
            system_interface: SystemInterface instance for system operations
            process_runner: IProcessRunner implementation for process operations
            console: ConsoleManager instance for output (optional)
        """
        self.system = system_interface
        self.process_runner = process_runner
        self.console = console

    def check_service_exists(self, service_name: str) -> bool:
        """Check if the systemd service exists

        Args:
            service_name: Name of the service to check

        Returns:
            bool: True if the service exists, False otherwise
        """
        try:
            if self.process_runner:
                result = self.process_runner.run(
                    ["systemctl", "list-unit-files", f"{service_name}.service"],
                    check=False,
                    capture_output=True,
                )
                return f"{service_name}.service" in result.stdout
            else:
                result = subprocess.run(
                    ["systemctl", "list-unit-files", f"{service_name}.service"],
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                return f"{service_name}.service" in result.stdout
        except Exception as e:
            if self.console:
                self.console.log_message(
                    "ERROR", f"Failed to check service existence: {e}"
                )
            return False

    def run_systemctl(self, action: str, service: str) -> bool:
        """Execute systemctl command on a service

        Args:
            action: Action to perform (start, stop, restart, status)
            service: Name of the service

        Returns:
            bool: True if successful, False otherwise
        """
        msg = f"systemctl {action} {service}.service"
        if self.console:
            self.console.log_message("DEBUG", f"Executing {msg}")

        if self.system.dry_run:
            if self.console:
                self.console.print(f"[DRY RUN] Would run: {msg}", style="blue")
            return True

        try:
            self.system.run_with_sudo(
                ["systemctl", action, f"{service}.service"], check=True
            )
            if self.console:
                self.console.log_message("INFO", f"{msg} successful")
            return True
        except Exception as e:
            if self.console:
                self.console.log_message("ERROR", f"{msg} failed: {e}")
            return False

    def is_service_active(self, service_name: str) -> bool:
        """Check if a systemd service is active

        Args:
            service_name: Name of the service to check

        Returns:
            bool: True if the service is active, False otherwise
        """
        try:
            if self.process_runner:
                result = self.process_runner.run(
                    ["systemctl", "is-active", f"{service_name}.service"],
                    check=False,
                    capture_output=True,
                )
                return result.stdout.strip() == "active"
            else:
                result = subprocess.run(
                    ["systemctl", "is-active", f"{service_name}.service"],
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                return result.stdout.strip() == "active"
        except Exception:
            return False

    def check_server_status(
        self, service_name: str, max_attempts: int = 5, wait_time: int = 3
    ) -> bool:
        """Check if the server is running after restart

        Args:
            service_name: Name of the service to check
            max_attempts: Maximum number of attempts to check status
            wait_time: Time to wait between attempts in seconds

        Returns:
            bool: True if the server is running, False otherwise
        """
        if self.console:
            self.console.print(
                f"Checking server status ({service_name})...", style="cyan"
            )

        # Check status up to max_attempts times
        for i in range(1, max_attempts + 1):
            time.sleep(wait_time)
            if self.is_service_active(service_name):
                if self.console:
                    self.console.print("Server is running.", style="green")
                return True
            if i == max_attempts:
                if self.console:
                    self.console.print(
                        f"WARNING: Server did not report active status after {max_attempts} checks.",
                        style="red",
                    )
                    self.console.print(
                        f"Check status manually: systemctl status {service_name}.service",
                        style="yellow",
                    )
                return False
            if self.console:
                self.console.print(
                    f"Waiting for server status (attempt {i} of {max_attempts})...",
                    style="yellow",
                )

        # This should not be reached due to the i==max_attempts check in the loop
        if self.console:
            self.console.print(
                "Error: Loop finished unexpectedly in check_server_status.", style="red"
            )
        return False

    def get_service_status(self, service_name: str) -> Optional[str]:
        """Get the status of a service (running, stopped, or unknown)

        Args:
            service_name: Name of the service to check

        Returns:
            Optional[str]: Status of the service or None if error
        """
        if self.is_service_active(service_name):
            return "running"

        # Check if service exists but is not running
        try:
            if self.process_runner:
                result = self.process_runner.run(
                    ["systemctl", "list-unit-files", f"{service_name}.service"],
                    check=False,
                    capture_output=True,
                )
                if f"{service_name}.service" in result.stdout:
                    return "stopped"
                return "unknown"
            else:
                result = subprocess.run(
                    ["systemctl", "list-unit-files", f"{service_name}.service"],
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                if f"{service_name}.service" in result.stdout:
                    return "stopped"
                return "unknown"
        except Exception:
            return None
