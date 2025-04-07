import subprocess
import os
from typing import List, Optional, Any

from vs_mgr.interfaces import IProcessRunner


class SubprocessProcessRunner(IProcessRunner):
    """Implementation of IProcessRunner using subprocess."""

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
            CompletedProcess object with returncode, stdout, stderr attributes
        """
        return subprocess.run(
            command_args, check=check, capture_output=capture_output, text=True, cwd=cwd
        )

    def run_sudo(
        self,
        command_args: List[str],
        check: bool = True,
        capture_output: bool = False,
        cwd: Optional[str] = None,
    ) -> Any:
        """Run a system command with sudo if needed.

        Args:
            command_args: List of command and arguments
            check: Whether to check the return code
            capture_output: Whether to capture stdout/stderr
            cwd: Working directory to run the command in

        Returns:
            CompletedProcess object with returncode, stdout, stderr attributes
        """
        # Only add sudo if not running as root and not on Windows
        if os.name != "nt" and os.geteuid() != 0:
            command_args = ["sudo"] + command_args

        return self.run(command_args, check, capture_output, cwd)
