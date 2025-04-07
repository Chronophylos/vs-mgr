"""Handles fetching, parsing, and comparing Vintage Story server versions.

Interacts with the official VS API (or a compatible one) to get version data
and verifies download URLs. Also provides methods to determine the currently
installed server version by checking log files.
"""

import os
import re
import json  # For parsing API response without jq
import requests  # For exception type hinting if needed
import shutil  # For shutil.which
import tempfile  # For temporary file handling
from typing import Optional, Dict, TYPE_CHECKING, Any
from packaging import version as packaging_version  # Rename to avoid conflict

from interfaces import IHttpClient, IProcessRunner  # Add IProcessRunner for jq
from errors import VersioningError, ProcessError

if TYPE_CHECKING:
    from ui import ConsoleManager
    from config import ServerSettings  # Use ServerSettings for consistency


class VersionChecker:
    """Provides methods for version checking, comparison, and verification.

    Uses an IHttpClient for API interactions and optionally IProcessRunner for jq.

    Attributes:
        server_dir (str): Path to the server installation directory.
        http_client (IHttpClient): Client for making HTTP requests.
        process_runner (Optional[IProcessRunner]): Runner for external processes (jq).
        console (ConsoleManager): Interface for logging and console output.
        downloads_base_url (str): Base URL for server downloads.
        game_version_api_url (str): URL for the game version API.
        jq_path (Optional[str]): Path to the jq executable, if found.
    """

    def __init__(
        self,
        server_dir: str,
        http_client: IHttpClient,
        console: "ConsoleManager",
        settings: "ServerSettings",  # Expect ServerSettings object
        process_runner: Optional[IProcessRunner] = None,  # Add process_runner
    ):
        """Initializes the VersionChecker.

        Args:
            server_dir: Path to the main server installation directory.
            http_client: Implementation of IHttpClient.
            console: The ConsoleManager instance.
            settings: The loaded ServerSettings instance.
            process_runner: Optional implementation of IProcessRunner (for jq).
        """
        self.server_dir = server_dir
        self.http_client = http_client
        self.console = console
        self.process_runner = process_runner

        # Get URLs from ServerSettings
        self.downloads_base_url = settings.downloads_base_url
        self.game_version_api_url = settings.game_version_api_url

        # Find jq path
        self.jq_path = shutil.which("jq")
        if self.jq_path:
            self.console.debug(f"Found jq executable at: {self.jq_path}")
        else:
            self.console.debug(
                "jq executable not found. JSON parsing will use Python dict access."
            )

    # --- Public Methods --- #

    def get_server_version(self) -> Optional[str]:
        """Attempts to determine the installed server version string (e.g., 'v1.19.4').

        Primarily tries to parse the server's main log file.

        Returns:
            The version string (including 'v' prefix) if found, otherwise None.
        """
        self.console.info(
            "Attempting to determine installed server version from log file..."
        )

        # Check if the presumed server DLL exists, as a proxy for installation
        dll_path = os.path.join(self.server_dir, "VintagestoryServer.dll")
        if not os.path.isfile(dll_path):
            self.console.warning(
                f"Server executable not found at expected location: {dll_path}. Cannot determine version."
            )
            return None

        version_str = self._get_version_from_log()
        if version_str:
            self.console.info(f"Determined version via log file: {version_str}")
            return version_str
        else:
            self.console.warning("Could not determine server version from log file.")
            return None

    def compare_versions(self, ver1_str: str, ver2_str: str) -> int:
        """Compares two semantic version strings (e.g., 'v1.2.3', '1.4.0-rc.1').

        Args:
            ver1_str: The first version string.
            ver2_str: The second version string.

        Returns:
            -1 if ver1 < ver2
             0 if ver1 == ver2
             1 if ver1 > ver2

        Raises:
            VersioningError: If either version string is invalid.
        """
        # Robustly remove potential 'v' prefix for comparison
        ver1_norm = ver1_str[1:] if ver1_str.startswith("v") else ver1_str
        ver2_norm = ver2_str[1:] if ver2_str.startswith("v") else ver2_str

        self.console.debug(
            f"Comparing normalized versions: '{ver1_norm}' and '{ver2_norm}'"
        )

        try:
            v1 = packaging_version.parse(ver1_norm)
            v2 = packaging_version.parse(ver2_norm)

            if v1 < v2:
                return -1
            elif v1 > v2:
                return 1
            else:
                return 0
        except packaging_version.InvalidVersion as e:
            err_msg = (
                f"Invalid version format encountered: '{ver1_str}' or '{ver2_str}'"
            )
            self.console.error(err_msg)
            raise VersioningError(err_msg) from e
        except Exception as e:
            err_msg = f"Unexpected error comparing versions '{ver1_str}' and '{ver2_str}': {e}"
            self.console.error(err_msg, exc_info=True)
            raise VersioningError(err_msg) from e

    def get_latest_version(self, channel: str = "stable") -> Optional[str]:
        """Gets the latest available version from the API for a specific channel.

        It fetches data from the API, extracts the relevant version based on the channel,
        and verifies that a downloadable server package exists for that version.

        Args:
            channel: The release channel (e.g., "stable", "unstable"). Defaults to "stable".

        Returns:
            The latest version string (without 'v' prefix) if found and verified,
            otherwise None.

        Raises:
            VersioningError: If API fetch, parsing, or verification fails.
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

            # Return version string *without* 'v' prefix as per API structure
            return latest_version

        except VersioningError as e:
            # Log specific errors already logged in helpers, re-raise for control flow
            self.console.error(f"Failed to get latest '{channel}' version: {e}")
            raise  # Re-raise caught VersioningError
        except Exception as e:
            # Catch unexpected errors during orchestration
            err_msg = f"Unexpected error getting latest '{channel}' version: {e}"
            self.console.error(err_msg, exc_info=True)
            raise VersioningError(err_msg) from e  # Wrap in VersioningError

    def verify_server_version(self, expected_version: str) -> bool:
        """Verifies if the currently running server matches the expected version.

        Compares the output of `get_server_version()` with the expected version.

        Args:
            expected_version: The version string expected (e.g., "v1.19.4" or "1.19.4").

        Returns:
            True if the versions match, False otherwise or if current version cannot be determined.
        """
        self.console.info(f"Verifying server version. Expecting: {expected_version}")
        try:
            current_version = self.get_server_version()
            if not current_version:
                self.console.warning(
                    "Could not determine current server version for verification."
                )
                return False

            # Use compare_versions for robust comparison (handles 'v' prefix)
            comparison_result = self.compare_versions(current_version, expected_version)

            if comparison_result == 0:
                self.console.info(f"Server version matches expected: {current_version}")
                return True
            else:
                self.console.error(
                    f"Server version mismatch! Expected '{expected_version}', Found '{current_version}'"
                )
                return False
        except VersioningError as e:
            self.console.error(f"Error during version comparison for verification: {e}")
            return False
        except Exception as e:
            self.console.error(
                f"Unexpected error during version verification: {e}", exc_info=True
            )
            return False  # Treat unexpected errors as verification failure

    def build_download_url(self, version_str: str, channel: str = "stable") -> str:
        """Constructs the expected download URL for a given server version.

        Args:
            version_str: The version string (e.g., "1.19.4"). Should not include 'v'.
            channel: The release channel (currently unused in URL but kept for consistency).

        Returns:
            The constructed download URL string.
        """
        # Ensure no 'v' prefix
        version_clean = version_str[1:] if version_str.startswith("v") else version_str
        # TODO: Confirm if channel affects URL path or filename structure in the future
        filename = f"vs_server_linux-x64_{version_clean}.tar.gz"
        url = f"{self.downloads_base_url}/{filename}"
        self.console.debug(f"Constructed download URL: {url}")
        return url

    # --- Private Helper Methods --- #

    def _get_version_from_log(self) -> Optional[str]:
        """Parses the server log file to find the last reported game version.

        Searches for lines containing "Game Version: vX.Y.Z".

        Returns:
            The version string (e.g., "v1.19.4") if found, otherwise None.
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

    def _fetch_version_data_from_api(self) -> Dict[str, Any]:
        """Fetches and parses version data from the VS API.

        Returns:
            The parsed JSON data as a dictionary.

        Raises:
            VersioningError: If the request fails, status code is non-200,
                           or the response is not valid JSON.
        """
        self.console.debug(f"Fetching data from API URL: {self.game_version_api_url}")
        try:
            # Assume http_client.get returns a response object with status_code and json() method
            response = self.http_client.get(self.game_version_api_url)

            if response.status_code != 200:
                err_msg = f"API request failed: {response.status_code} {response.reason if hasattr(response, 'reason') else ''}. URL: {self.game_version_api_url}"
                self.console.error(err_msg)
                # Include response text if available and helpful
                try:
                    body = response.text if hasattr(response, "text") else "N/A"
                    self.console.debug(
                        f"API Response Body:\n{body[:500]}..."
                    )  # Log truncated body
                except Exception:
                    pass  # Ignore errors getting body
                raise VersioningError(err_msg)

            # Attempt to parse JSON response
            try:
                data = response.json()
                if not isinstance(data, dict):  # Basic validation
                    raise ValueError("API response is not a JSON object")
                return data
            except (json.JSONDecodeError, ValueError, Exception) as json_e:
                err_msg = f"Failed to parse JSON response from API: {json_e}"
                self.console.error(err_msg)
                raise VersioningError(err_msg) from json_e

        except (
            requests.exceptions.RequestException
        ) as e:  # Catch HTTP client specific errors
            err_msg = f"HTTP request to API failed: {e}"
            self.console.error(err_msg)
            raise VersioningError(err_msg) from e
        except Exception as e:
            # Catch unexpected errors from http_client or response handling
            err_msg = f"Unexpected error fetching data from API: {e}"
            self.console.error(err_msg, exc_info=True)
            raise VersioningError(err_msg) from e

    def _extract_latest_version_from_response(
        self, api_response: Dict[str, Any], channel: str
    ) -> Optional[str]:
        """Extracts the latest version string for a given channel from the parsed API response.

        Prioritizes using `jq` if available and configured, otherwise falls back to
        manual Python dictionary traversal.

        Args:
            api_response: The parsed JSON data from the API.
            channel: The release channel (e.g., "stable", "unstable").

        Returns:
            The latest version string (e.g., "1.19.4") or None if not found.

        Raises:
            VersioningError: If parsing fails using either method.
        """
        self.console.debug(f"Extracting latest '{channel}' version from API data.")
        version_str: Optional[str] = None

        if self.jq_path and self.process_runner:  # Try jq first if available
            try:
                version_str = self._extract_with_jq(api_response, channel)
                if version_str:
                    self.console.debug(f"Extracted version via jq: {version_str}")
                    return version_str
                else:
                    self.console.debug(
                        f"jq query returned no result for channel '{channel}'. Trying Python fallback."
                    )
            except (ProcessError, VersioningError, Exception) as jq_err:
                self.console.warning(
                    f"jq extraction failed: {jq_err}. Trying Python fallback."
                )
                # Fall through to Python method
        else:
            self.console.debug(
                "jq not available or process_runner missing. Using Python dict access."
            )

        # Python fallback method
        try:
            version_str = self._extract_with_python(api_response, channel)
            if version_str:
                self.console.debug(f"Extracted version via Python: {version_str}")
                return version_str
            else:
                # Both methods failed or version not found
                raise VersioningError(
                    f"Version for channel '{channel}' not found in API response."
                )
        except (KeyError, IndexError, TypeError) as py_err:
            err_msg = f"Failed to extract version via Python dict access for channel '{channel}': {py_err}"
            self.console.error(err_msg)
            raise VersioningError(err_msg) from py_err
        except Exception as e:
            # Catch other unexpected errors from python extraction
            err_msg = f"Unexpected error during Python version extraction: {e}"
            self.console.error(err_msg, exc_info=True)
            raise VersioningError(err_msg) from e

    def _extract_with_jq(
        self, api_response: Dict[str, Any], channel: str
    ) -> Optional[str]:
        """Uses jq process to extract the version.

        Requires self.jq_path and self.process_runner to be set.

        Raises:
            ProcessError: If jq command fails (and check=True is used implicitly or explicitly).
            VersioningError: If jq output is unexpected or other errors occur.
        """
        if not self.jq_path or not self.process_runner:
            raise VersioningError(
                "Cannot use jq: path or process_runner not available."
            )

        # Example jq filter - **needs adjustment based on actual API structure**
        jq_filter = f'.channels."{channel}".latest'
        self.console.debug(f"Using jq filter: {jq_filter}")

        input_json_str = json.dumps(api_response)
        # We can't reliably pass stdin via IProcessRunner.run interface
        # Workaround: Write JSON to a temp file and have jq read it.
        temp_json_file = None
        try:
            # Create a temporary file to hold the JSON input
            with tempfile.NamedTemporaryFile(
                mode="w", delete=False, suffix=".json", encoding="utf-8"
            ) as tf:
                tf.write(input_json_str)
                temp_json_file = tf.name
            self.console.debug(
                f"Wrote API response to temporary file: {temp_json_file}"
            )

            # Modify jq command to read the filter and the temp file
            jq_cmd = [self.jq_path, "-r", jq_filter, temp_json_file]

            # IProcessRunner.run does not support text
            result = self.process_runner.run(
                jq_cmd,
                check=True,
                capture_output=True,
                # No input=... needed now
            )
            # Decode stdout
            output = result.stdout.decode("utf-8").strip() if result.stdout else ""

            if output and output != "null":
                if re.match(r"^\d+\.\d+(\.\d+)?(-\w+\.\d+)?$", output):
                    return output
                else:
                    raise VersioningError(
                        f"jq extracted invalid version format: '{output}'"
                    )
            else:
                return None

        except ProcessError as e:
            self.console.warning(
                f"jq command failed processing API data for channel '{channel}'. Filter: {jq_filter}. Error: {e}"
            )
            if e.__cause__:
                self.console.debug(f"Underlying jq error cause: {e.__cause__}")
            return None
        except (IOError, json.JSONDecodeError, Exception) as e:
            # Catch errors related to temp file or other issues
            self.console.warning(
                f"Unexpected error during jq processing (temp file/other): {e}"
            )
            return None
        finally:
            # Ensure temporary file is deleted
            if temp_json_file and os.path.exists(temp_json_file):
                try:
                    os.remove(temp_json_file)
                    self.console.debug(
                        f"Removed temporary jq input file: {temp_json_file}"
                    )
                except OSError as e:
                    self.console.warning(
                        f"Failed to remove temporary jq input file '{temp_json_file}': {e}"
                    )

    def _extract_with_python(
        self, api_response: Dict[str, Any], channel: str
    ) -> Optional[str]:
        """Uses Python dictionary access to extract the version.

        Raises:
            KeyError, IndexError, TypeError: If the expected structure is missing.
        """
        # Assuming the structure is like: {"latestVersions": {"stable": "1.x.y", "unstable": "1.z.w"}}
        # Adjust based on actual API response structure revealed by testing/docs
        if (
            "latestVersions" in api_response
            and channel in api_response["latestVersions"]
        ):
            version_str = api_response["latestVersions"][channel]
            if isinstance(version_str, str) and version_str:
                self.console.debug(f"Extracted version via dict access: {version_str}")
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

    def _verify_download_url(self, download_url: str) -> bool:
        """Verifies if a download URL likely exists by sending a HEAD request.

        Args:
            download_url: The URL to check.

        Returns:
            True if the URL returns a 2xx status code to a HEAD request, False otherwise.

        Raises:
            VersioningError: If the HTTP request itself fails unexpectedly.
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
