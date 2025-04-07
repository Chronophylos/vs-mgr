import requests
from typing import Union, Any
from pathlib import Path
import os
import shutil

from vs_mgr.interfaces import IHttpClient


class RequestsHttpClient(IHttpClient):
    """Implementation of IHttpClient using requests library."""

    def get(self, url: str, stream: bool = False) -> Any:
        """Perform HTTP GET request.

        Args:
            url: The URL to request
            stream: Whether to stream the response

        Returns:
            Response object with content, status_code attributes
        """
        return requests.get(url, stream=stream)

    def head(self, url: str) -> Any:
        """Perform HTTP HEAD request.

        Args:
            url: The URL to request

        Returns:
            Response object with headers, status_code attributes
        """
        return requests.head(url)

    def download(self, url: str, dest_path: Union[str, Path]) -> bool:
        """Download a file from a URL to a destination path.

        Args:
            url: The URL to download from
            dest_path: The path to save the file to

        Returns:
            True if download was successful, False otherwise
        """
        try:
            # Ensure directory exists
            dest_dir = os.path.dirname(dest_path)
            if dest_dir and not os.path.exists(dest_dir):
                os.makedirs(dest_dir)

            # Stream download to handle large files efficiently
            with requests.get(url, stream=True) as response:
                response.raise_for_status()

                with open(dest_path, "wb") as f:
                    shutil.copyfileobj(response.raw, f)

            return True
        except (requests.RequestException, IOError, OSError) as e:
            print(f"Error downloading file from {url}: {e}")
            # Clean up partial download if it exists
            if os.path.exists(dest_path):
                os.remove(dest_path)
            return False
