import os
import re
import requests
import shutil
from typing import Optional, Dict, TYPE_CHECKING
from packaging import version

from interfaces import IHttpClient, IProcessRunner
from errors import VersioningError

if TYPE_CHECKING:
    from ui import ConsoleManager


class VersionChecker:
    """Handles version checking and comparison for Vintage Story server"""

    def __init__(
        self,
        server_dir: str,
        http_client: IHttpClient,
        process_runner: IProcessRunner,
        console: "ConsoleManager",
        settings: Optional[Dict] = None,
    ):
        """Initialize VersionChecker

        Args:
            server_dir: Path to the server directory
            http_client: IHttpClient implementation for API requests
            process_runner: IProcessRunner implementation for running commands
            console: ConsoleManager instance for output
            settings: Optional dictionary for additional configuration
        """
        self.server_dir = server_dir
        self.http_client = http_client
        self.process_runner = process_runner
        self.console = console
        # Get URLs from settings if provided, otherwise use defaults
        self.downloads_base_url = getattr(
            settings, "downloads_base_url", "https://cdn.vintagestory.at/gamefiles"
        )
        self.game_version_api_url = getattr(
            settings,
            "game_version_api_url",
            "https://mods.vintagestory.at/api/gameversions",
        )
        self.dotnet_path = shutil.which("dotnet")
        self.jq_path = shutil.which("jq")

    def get_server_version(self) -> Optional[str]:
        """Attempt to get the installed server version string (e.g., 'v1.2.3').

        Tries the dotnet command first, then falls back to log parsing.

        Returns:
            Version string if found, otherwise None.
        """
        self.console.info("Attempting to determine installed server version...")
        dll_path = os.path.join(self.server_dir, "VintagestoryServer.dll")

        if not os.path.isfile(dll_path):
            self.console.warning(
                f"Server executable not found at expected location: {dll_path}"
            )
            return None

        # 1. Try using dotnet command (preferred)
        if self.dotnet_path:
            self.console.debug(
                f"Attempting version check via dotnet at: {self.dotnet_path}"
            )
            version_str = self._get_version_via_dotnet()
            if version_str:
                self.console.info(f"Determined version via dotnet: {version_str}")
                return version_str
            else:
                self.console.debug("Failed to get version via dotnet, trying log file.")
        else:
            self.console.debug(
                "dotnet command not found, skipping direct version check."
            )

        # 2. Fallback to log file check
        self.console.debug("Attempting version check via log file parsing.")
        version_str = self._get_version_from_log()
        if version_str:
            self.console.info(f"Determined version via log file: {version_str}")
            return version_str

        self.console.warning("Could not determine server version from dotnet or logs.")
        return None

    def _get_version_via_dotnet(self) -> Optional[str]:
        """Get server version using the dotnet command via IProcessRunner.

        Returns:
            Optional[str]: Version string (with 'v' prefix) or None if unavailable
        """
        if not self.dotnet_path:
            return None  # Should not happen if called via get_server_version

        try:
            # Run dotnet VintagestoryServer.dll --version in the server directory
            result = self.process_runner.run(
                [self.dotnet_path, "VintagestoryServer.dll", "--version"],
                check=False,  # Don't raise error immediately, check return code
                capture_output=True,
                cwd=self.server_dir,  # Run in the server directory
            )

            if result.returncode != 0 or not result.stdout:
                self.console.warning(
                    f"dotnet --version command failed (Code: {result.returncode}). Stdout: '{result.stdout}'. Stderr: '{result.stderr}'"
                )
                return None

            # Extract version using regex (e.g., "v1.19.0-rc.1", "1.18.15")
            # Allow optional 'v' prefix and potential suffixes like -rc.1
            match = re.search(r"v?(\d+\.\d+\.\d+(?:[.-]\S*)?)", result.stdout)
            if match:
                version_str = match.group(1)  # Get the numeric part
                # Ensure 'v' prefix for consistency
                version_str_with_v = f"v{version_str}"
                self.console.debug(
                    f"Parsed version from dotnet output: {version_str_with_v}"
                )
                return version_str_with_v
            else:
                self.console.warning(
                    f"Could not parse version from dotnet output: {result.stdout}"
                )
                return None
        except Exception as e:
            self.console.error(f"Error running dotnet --version: {e}", exc_info=True)
            return None

    def _get_version_from_log(self) -> Optional[str]:
        """Get server version from the server-main.log file.

        Returns:
            Optional[str]: Version string (with 'v' prefix) or None if unavailable
        """
        # Construct the likely log file path relative to server_dir
        log_file = os.path.join(
            self.server_dir, "..", "data", "Logs", "server-main.log"
        )
        # Normalize the path (e.g., /srv/vs/../data -> /srv/data)
        log_file = os.path.normpath(log_file)

        self.console.debug(f"Looking for log file at: {log_file}")

        if not os.path.isfile(log_file):
            self.console.debug(f"Log file not found at: {log_file}")
            # Try alternative common location (less ideal)
            alt_log_file = "/srv/gameserver/data/vs/Logs/server-main.log"
            if os.path.isfile(alt_log_file):
                log_file = alt_log_file
                self.console.debug(
                    f"Found log file at alternative location: {log_file}"
                )
            else:
                self.console.debug("Alternative log location not found either.")
                return None

        try:
            with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                # Read lines in reverse for efficiency, hoping version is near the end
                for line in reversed(list(f)):
                    if "Game Version: v" in line:
                        # More specific regex: v followed by digits.digits.digits
                        match = re.search(r"(v\d+\.\d+\.\d+)", line)
                        if match:
                            version_str = match.group(1)
                            self.console.debug(
                                f"Found version in log line: {version_str}"
                            )
                            return version_str
            self.console.debug(f"Version string not found in log file: {log_file}")
            return None
        except OSError as e:
            self.console.warning(f"Could not read log file '{log_file}': {e}")
            return None
        except Exception as e:
            self.console.error(
                f"Unexpected error reading log file '{log_file}': {e}", exc_info=True
            )
            return None

    def compare_versions(self, ver1: str, ver2: str) -> str:
        """Compare two semantic version strings (e.g., 'v1.2.3', '1.4.0-rc.1').

        Args:
            ver1: First version to compare
            ver2: Second version to compare

        Returns:
            str: 'newer' if ver1 > ver2, 'older' if ver1 < ver2, 'same' if equal
        """
        # Robustly remove potential 'v' prefix
        ver1_norm = ver1[1:] if ver1.startswith("v") else ver1
        ver2_norm = ver2[1:] if ver2.startswith("v") else ver2

        self.console.debug(f"Comparing versions: '{ver1_norm}' and '{ver2_norm}'")

        try:
            v1 = version.parse(ver1_norm)
            v2 = version.parse(ver2_norm)

            if v1 > v2:
                return "newer"
            elif v1 < v2:
                return "older"
            else:
                return "same"
        except version.InvalidVersion:
            self.console.error(
                f"Invalid version format encountered: '{ver1}' or '{ver2}'"
            )
            raise VersioningError(f"Cannot compare invalid versions: {ver1}, {ver2}")
        except Exception as e:
            self.console.error(
                f"Unexpected error comparing versions '{ver1}' and '{ver2}': {e}",
                exc_info=True,
            )
            raise VersioningError(f"Error comparing versions: {e}")

    def get_latest_version(self, channel: str = "stable") -> Optional[str]:
        """Get the latest available version from the API for the specified channel.

        Verifies the download URL exists before returning the version.

        Returns:
            Version string (without 'v' prefix) or None if error/not found.
        """
        self.console.info(
            f"Checking for latest '{channel}' version via API: {self.game_version_api_url}"
        )

        try:
            api_response = self._fetch_version_data_from_api()
            if not api_response:
                # Error already logged by _fetch_version_data_from_api
                return None

            latest_version = self._extract_latest_version_from_response(
                api_response, channel
            )
            if not latest_version:
                self.console.error(
                    f"Could not determine latest '{channel}' version from API response."
                )
                return None

            self.console.info(
                f"Latest '{channel}' version reported by API: {latest_version}"
            )

            # Verify that this version has a downloadable server package
            download_url = self.build_download_url(latest_version, channel)
            if not self._verify_download_url(download_url):
                # Error logged by _verify_download_url
                return None

            # Return version string *without* 'v' prefix as per API convention
            return latest_version

        except VersioningError as e:
            self.console.error(f"Failed to get latest version: {e}")
            return None
        except Exception as e:
            self.console.error(
                f"Unexpected error getting latest version: {e}", exc_info=True
            )
            return None

    def _fetch_version_data_from_api(self) -> Optional[Dict]:
        """Fetch version data from the Vintage Story API using IHttpClient.

        Returns:
            Optional[Dict]: API response data or None if error
        """
        self.console.debug(f"Fetching data from API URL: {self.game_version_api_url}")
        try:
            response = self.http_client.get(self.game_version_api_url)

            # Check response status code
            if response.status_code != 200:
                err_msg = f"API request failed with status code {response.status_code}. URL: {self.game_version_api_url}"
                self.console.error(err_msg)
                raise VersioningError(err_msg)

            # Attempt to parse JSON response
            return response.json()
        except requests.exceptions.RequestException as e:
            err_msg = f"HTTP request failed when contacting API: {e}"
            self.console.error(err_msg)
            raise VersioningError(err_msg) from e
        except Exception as e:  # Includes JSONDecodeError
            err_msg = f"Failed to fetch or parse version data from API: {e}"
            self.console.error(err_msg, exc_info=True)
            raise VersioningError(err_msg) from e

    def _extract_latest_version_from_response(
        self, api_response: Dict, channel: str
    ) -> Optional[str]:
        """Extract the latest version for the given channel from the API response.

        Uses jq if available for potentially complex JSON parsing, otherwise uses Python dict access.

        Returns:
            Optional[str]: Latest version string (without 'v' prefix) or None if error
        """
        self.console.debug(f"Extracting latest '{channel}' version from API response.")

        # --- Using jq (if available) --- NOT IMPLEMENTED YET, USING PYTHON DICT ACCESS
        # if self.jq_path:
        #     try:
        #         # Example jq query - needs refinement based on actual JSON structure
        #         jq_query = f'.versions[] | select(.channel==\"{channel}\") | .version' # Placeholder
        #         # ... (run jq via self.process_runner, parse output)
        #         self.console.debug(f"Extracted version using jq: {version_str}")
        #         return version_str
        #     except Exception as e:
        #         self.console.warning(f"jq query failed, falling back to Python dict access: {e}")

        # --- Using Python Dictionary Access (Fallback/Default) ---
        try:
            # Assuming the structure is like: {"latestVersions": {"stable": "1.x.y", "unstable": "1.z.w"}}
            # Adjust based on actual API response structure revealed by testing/docs
            if (
                "latestVersions" in api_response
                and channel in api_response["latestVersions"]
            ):
                version_str = api_response["latestVersions"][channel]
                if isinstance(version_str, str) and version_str:
                    self.console.debug(
                        f"Extracted version via dict access: {version_str}"
                    )
                    return version_str
                else:
                    self.console.warning(
                        f"Invalid version format found for channel '{channel}' in API response: {version_str}"
                    )
                    return None
            else:
                self.console.warning(
                    f"Could not find '{channel}' version in API response structure."
                )
                return None
        except KeyError as e:
            self.console.error(
                f"Missing expected key '{e}' in API response while extracting version."
            )
            return None
        except Exception as e:
            self.console.error(
                f"Error processing API response for version extraction: {e}",
                exc_info=True,
            )
            return None

    def _verify_download_url(self, download_url: str) -> bool:
        """Verify that the download URL exists by sending a HEAD request.

        Args:
            download_url: URL to verify

        Returns:
            bool: True if the URL exists, False otherwise
        """
        self.console.debug(
            f"Verifying download URL availability (HEAD request): {download_url}"
        )
        try:
            response = self.http_client.head(download_url)
            if response.status_code == 200:
                self.console.debug(
                    f"Download URL verified successfully (Status: {response.status_code})."
                )
                return True
            else:
                self.console.warning(
                    f"Download URL verification failed. Status code {response.status_code} for URL: {download_url}"
                )
                return False
        except requests.exceptions.RequestException as e:
            self.console.warning(
                f"HTTP request failed during download URL verification: {e}"
            )
            return False
        except Exception as e:
            self.console.error(
                f"Unexpected error verifying download URL '{download_url}': {e}",
                exc_info=True,
            )
            return False

    def verify_server_version(self, expected_version: str) -> bool:
        """Verify the installed server version matches the expected version.

        Args:
            expected_version: Version to check against (with or without 'v' prefix)

        Returns:
            bool: True if versions match, False otherwise
        """
        self.console.info(
            f"Verifying installed server version against expected: {expected_version}"
        )
        current_version = self.get_server_version()
        if not current_version:
            self.console.error(
                "Cannot verify version: Failed to determine current server version."
            )
            return False

        # Normalize versions for comparison (remove 'v')
        norm_current = (
            current_version[1:] if current_version.startswith("v") else current_version
        )
        norm_expected = (
            expected_version[1:]
            if expected_version.startswith("v")
            else expected_version
        )

        if norm_current == norm_expected:
            self.console.info(f"Server version matches expected: {current_version}")
            return True
        else:
            self.console.error(
                f"Version mismatch! Expected '{expected_version}' but found '{current_version}'."
            )
            return False

    def build_download_url(self, version_str: str, channel: str = "stable") -> str:
        """Construct the download URL for a given version and channel.

        Args:
            version_str: Version string (without 'v' prefix)
            channel: Release channel ('stable' or 'unstable')

        Returns:
            str: Download URL
        """
        # Ensure version doesn't have 'v' prefix for URL construction
        version_clean = version_str[1:] if version_str.startswith("v") else version_str
        # Assume Linux x64 for now, could be made configurable
        filename = f"vs_server_linux-x64_{version_clean}.tar.gz"
        url = f"{self.downloads_base_url}/{channel}/{filename}"
        self.console.debug(f"Constructed download URL: {url}")
        return url
