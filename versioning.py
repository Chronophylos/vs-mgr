import os
import re
import subprocess
import requests
import shutil
from typing import Optional, Dict
from packaging import version

from interfaces import IHttpClient


class VersionChecker:
    """Handles version checking and comparison for Vintage Story server"""

    def __init__(
        self,
        server_dir: str,
        http_client: Optional[IHttpClient] = None,
        system_interface=None,
        console=None,
    ):
        """Initialize VersionChecker

        Args:
            server_dir: Path to the server directory
            http_client: IHttpClient implementation for API requests
            system_interface: SystemInterface instance for system operations (optional)
            console: ConsoleManager instance for output (optional)
        """
        self.server_dir = server_dir
        self.http_client = http_client
        self.system = system_interface
        self.console = console
        # These URLs would ideally come from config, but for now we'll set them here
        self.downloads_base_url = "https://cdn.vintagestory.at/gamefiles"
        self.game_version_api_url = "https://mods.vintagestory.at/api/gameversions"

    def get_server_version(self) -> Optional[str]:
        """Attempt to get the server version

        Returns:
            Optional[str]: Version string (with 'v' prefix) or None if unavailable
        """
        dll_path = os.path.join(self.server_dir, "VintagestoryServer.dll")
        if not os.path.isfile(dll_path):
            if self.console:
                self.console.print(
                    f"⚠ Server executable not found: {dll_path}", style="yellow"
                )
            return None

        # Try using dotnet command if available
        dotnet_path = (
            self.system.which("dotnet") if self.system else shutil.which("dotnet")
        )
        if dotnet_path:
            version_str = self._get_version_via_dotnet()
            if version_str:
                return version_str

        # Fallback to log file check
        return self._get_version_from_log()

    def _get_version_via_dotnet(self) -> Optional[str]:
        """Get server version using dotnet command

        Returns:
            Optional[str]: Version string (with 'v' prefix) or None if unavailable
        """
        try:
            # Change to server directory and run dotnet command
            current_dir = os.getcwd()
            os.chdir(self.server_dir)
            result = subprocess.run(
                ["dotnet", "VintagestoryServer.dll", "--version"],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            os.chdir(current_dir)  # Change back to original directory

            if result.returncode != 0 or not result.stdout:
                if self.console:
                    self.console.print(
                        "⚠ Failed to get server version using --version flag (check permissions or dotnet install).",
                        style="yellow",
                    )
                return None

            # Extract version using regex
            match = re.search(r"v?(\d+\.\d+\.\d+)", result.stdout)
            if match:
                version_str = match.group(0)
                # Ensure 'v' prefix for consistency
                if not version_str.startswith("v"):
                    version_str = f"v{version_str}"
                return version_str
            else:
                if self.console:
                    self.console.print(
                        f"⚠ Could not parse version from output: {result.stdout}",
                        style="yellow",
                    )
                return None
        except Exception as e:
            if self.console:
                self.console.print(
                    f"⚠ Error getting server version: {e}", style="yellow"
                )
            return None

    def _get_version_from_log(self) -> Optional[str]:
        """Get server version from the log file

        Returns:
            Optional[str]: Version string (with 'v' prefix) or None if unavailable
        """
        # This method assumes data_dir is provided or can be constructed
        # We'll use a fallback approach for this implementation
        potential_log_locations = [
            os.path.join(
                os.path.dirname(self.server_dir),
                "data",
                "vs",
                "Logs",
                "server-main.log",
            ),
            os.path.join("/srv/gameserver/data/vs", "Logs", "server-main.log"),
        ]

        for log_file in potential_log_locations:
            if os.path.isfile(log_file):
                try:
                    with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                        for line in f:
                            if "Game Version: v" in line:
                                match = re.search(r"v\d+\.\d+\.\d+", line)
                                if match:
                                    return match.group(0)
                except Exception:
                    pass

        return None

    def compare_versions(self, ver1: str, ver2: str) -> str:
        """Compare two semantic version strings and return 'newer', 'older', or 'same'

        Args:
            ver1: First version to compare
            ver2: Second version to compare

        Returns:
            str: 'newer' if ver1 > ver2, 'older' if ver1 < ver2, 'same' if equal
        """
        # Remove 'v' prefix if present
        ver1 = ver1[1:] if ver1.startswith("v") else ver1
        ver2 = ver2[1:] if ver2.startswith("v") else ver2

        try:
            v1 = version.parse(ver1)
            v2 = version.parse(ver2)

            if v1 > v2:
                return "newer"
            elif v1 < v2:
                return "older"
            else:
                return "same"
        except Exception as e:
            if self.console:
                self.console.log_message("ERROR", f"Error comparing versions: {e}")
            # Default to 'same' on error
            return "same"

    def get_latest_version(self, channel: str = "stable") -> Optional[str]:
        """Get the latest available version from the API

        Args:
            channel: Release channel ('stable' or 'unstable')

        Returns:
            Optional[str]: Latest version string (without 'v' prefix) or None if error
        """
        if self.console:
            self.console.print(
                "Checking for latest version via official API...", style="cyan"
            )

        api_response = self._fetch_version_data_from_api()
        if not api_response:
            return None

        latest_version = self._extract_latest_version_from_response(
            api_response, channel
        )
        if not latest_version:
            if self.console:
                self.console.print(
                    f"Error: Could not determine latest {channel} version from API response",
                    style="red",
                )
            return None

        # Verify that this version has a downloadable server package
        download_url = f"{self.downloads_base_url}/{channel}/vs_server_linux-x64_{latest_version}.tar.gz"
        if not self._verify_download_url(download_url):
            return None

        if self.console:
            self.console.print(
                f"✓ Verified download URL: {download_url}", style="green"
            )
        return latest_version

    def _fetch_version_data_from_api(self) -> Optional[Dict]:
        """Fetch version data from the Vintage Story API

        Returns:
            Optional[Dict]: API response data or None if error
        """
        try:
            if self.http_client:
                response = self.http_client.get(self.game_version_api_url)
            else:
                response = requests.get(self.game_version_api_url)

            if response.status_code != 200:
                if self.console:
                    self.console.print(
                        f"Error: API request failed with status code {response.status_code}",
                        style="red",
                    )
                return None

            return response.json()
        except Exception as e:
            if self.console:
                self.console.print(
                    f"Error: Failed to fetch version data from API: {e}", style="red"
                )
            return None

    def _extract_latest_version_from_response(
        self, api_response: Dict, channel: str
    ) -> Optional[str]:
        """Extract the latest version from the API response

        Args:
            api_response: API response dictionary
            channel: Release channel ('stable' or 'unstable')

        Returns:
            Optional[str]: Latest version string (without 'v' prefix) or None if error
        """
        try:
            # Extract versions
            versions = []
            for version_info in api_response["gameversions"]:
                if "name" in version_info:
                    version_name = version_info["name"]
                    # Filter out release candidates and pre-releases for stable channel
                    if channel == "stable" and (
                        "-rc" in version_name or "-pre" in version_name
                    ):
                        continue
                    versions.append(version_name)

            # Find the latest version using semantic versioning
            latest_stable = None
            latest_stable_without_v = None

            for version_str in versions:
                # Remove 'v' prefix for comparison
                version_without_v = (
                    version_str[1:] if version_str.startswith("v") else version_str
                )

                if latest_stable is None:
                    latest_stable = version_str
                    latest_stable_without_v = version_without_v
                else:
                    comparison = self.compare_versions(
                        f"v{latest_stable_without_v}", f"v{version_without_v}"
                    )
                    if comparison == "older":
                        latest_stable = version_str
                        latest_stable_without_v = version_without_v

            if latest_stable_without_v:
                if self.console:
                    self.console.print(
                        f"Latest {channel} version from API: {latest_stable} ({latest_stable_without_v})",
                        style="green",
                    )
                return latest_stable_without_v
            return None
        except Exception as e:
            if self.console:
                self.console.print(f"Error extracting version data: {e}", style="red")
            return None

    def _verify_download_url(self, download_url: str) -> bool:
        """Verify that a download URL exists by performing a HEAD request

        Args:
            download_url: URL to verify

        Returns:
            bool: True if the URL exists, False otherwise
        """
        try:
            if self.console:
                self.console.log_message(
                    "DEBUG", f"Checking download URL: {download_url}"
                )

            # If we have an http_client interface, use it
            if self.http_client:
                response = self.http_client.head(download_url)
            else:
                response = requests.head(download_url)

            exists = response.status_code == 200
            if not exists and self.console:
                self.console.print(
                    f"Download URL not available: {download_url} (HTTP {response.status_code})",
                    style="yellow",
                )
            return exists
        except Exception as e:
            if self.console:
                self.console.log_message("ERROR", f"Error verifying download URL: {e}")
            return False

    def verify_server_version(self, expected_version: str) -> bool:
        """Verify if the running server version matches the expected version

        Args:
            expected_version: Version to check against (with or without 'v' prefix)

        Returns:
            bool: True if versions match, False otherwise
        """
        expected_version_v = (
            f"v{expected_version}"
            if not expected_version.startswith("v")
            else expected_version
        )
        if self.console:
            self.console.print(
                f"Verifying server version (expecting {expected_version_v})...",
                style="cyan",
            )

        # Try direct version check first
        installed_version = self.get_server_version()
        if installed_version:
            if self.console:
                self.console.print(
                    f"Detected server version via --version: {installed_version}",
                    style="cyan",
                )
            if installed_version == expected_version_v:
                if self.console:
                    self.console.print(
                        f"✓ Server is running the expected version {installed_version}",
                        style="green",
                    )
                return True
            else:
                if self.console:
                    self.console.print(
                        f"⚠ WARNING: Server reports version {installed_version}, but expected {expected_version_v}",
                        style="yellow",
                    )
                    self.console.print(
                        "  The update might not have fully applied or direct check is inaccurate. Will check logs.",
                        style="yellow",
                    )
        else:
            if self.console:
                self.console.print(
                    "Could not get version via --version flag. Proceeding to log check.",
                    style="yellow",
                )

        # Final verification via log - this is duplicating some logic from get_server_version
        # but we want to be explicit about the result for verification
        return self._verify_version_from_log(expected_version_v)

    def _verify_version_from_log(self, expected_version: str) -> bool:
        """Verify server version by checking log files

        Args:
            expected_version: Expected version string (with 'v' prefix)

        Returns:
            bool: True if versions match, False otherwise
        """
        if self.console:
            self.console.print(
                "Falling back to log file check for version verification...",
                style="yellow",
            )

        # This method assumes data_dir is provided or can be constructed
        # We'll use a fallback approach for this implementation
        potential_log_locations = [
            os.path.join(
                os.path.dirname(self.server_dir),
                "data",
                "vs",
                "Logs",
                "server-main.log",
            ),
            os.path.join("/srv/gameserver/data/vs", "Logs", "server-main.log"),
        ]

        # Wait a moment for log file to potentially update
        import time

        time.sleep(2)

        for log_file in potential_log_locations:
            if os.path.isfile(log_file):
                try:
                    with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                        for line in f:
                            if "Game Version: v" in line:
                                match = re.search(r"v\d+\.\d+\.\d+", line)
                                if match:
                                    log_version = match.group(0)
                                    if self.console:
                                        self.console.print(
                                            f"Detected server version from log: {log_version}",
                                            style="cyan",
                                        )
                                    if log_version == expected_version:
                                        if self.console:
                                            self.console.print(
                                                f"✓ Server log confirms expected version {log_version}",
                                                style="green",
                                            )
                                        return True
                                    else:
                                        if self.console:
                                            self.console.print(
                                                f"⚠ WARNING: Server log shows version {log_version}, but expected {expected_version}",
                                                style="yellow",
                                            )
                                            self.console.print(
                                                "  The update likely did not apply correctly.",
                                                style="yellow",
                                            )
                                        return False
                    if self.console:
                        self.console.print(
                            f"⚠ Could not detect server version from log file ({log_file}). Verification incomplete.",
                            style="yellow",
                        )
                except Exception as e:
                    if self.console:
                        self.console.print(
                            f"⚠ Error reading log file: {e}", style="yellow"
                        )
                    continue

        if self.console:
            self.console.print(
                "⚠ No usable log files found. Cannot verify version from log.",
                style="yellow",
            )
        return False
