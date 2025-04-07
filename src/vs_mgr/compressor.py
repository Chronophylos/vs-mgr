import os
import zstandard as zstd
from pathlib import Path
from typing import Union

from vs_mgr.interfaces import ICompressor


class ZstdCompressor(ICompressor):
    """Implementation of ICompressor using zstandard."""

    def __init__(self, compression_level: int = 3):
        """Initialize ZstdCompressor with specified compression level.

        Args:
            compression_level: Level of compression (1-22, higher = better compression but slower)
        """
        self.compression_level = compression_level

    def compress(
        self, source_path: Union[str, Path], dest_path: Union[str, Path]
    ) -> bool:
        """Compress a file.

        Args:
            source_path: Path to the file to compress
            dest_path: Path to save the compressed file to

        Returns:
            True if successful, False otherwise
        """
        try:
            # Ensure parent directory of destination exists
            dest_dir = os.path.dirname(dest_path)
            if dest_dir:
                os.makedirs(dest_dir, exist_ok=True)

            # Create a compressor with the specified compression level
            cctx = zstd.ZstdCompressor(level=self.compression_level)

            # Read input file and compress to output file
            with open(source_path, "rb") as input_file:
                with open(dest_path, "wb") as output_file:
                    cctx.copy_stream(input_file, output_file)

            return True
        except (IOError, OSError, zstd.ZstdError) as e:
            print(f"Error compressing {source_path}: {e}")
            # Clean up partial output if it exists
            if os.path.exists(dest_path):
                os.remove(dest_path)
            return False

    def decompress(
        self, source_path: Union[str, Path], dest_path: Union[str, Path]
    ) -> bool:
        """Decompress a file.

        Args:
            source_path: Path to the compressed file
            dest_path: Path to save the decompressed file to

        Returns:
            True if successful, False otherwise
        """
        try:
            # Ensure parent directory of destination exists
            dest_dir = os.path.dirname(dest_path)
            if dest_dir:
                os.makedirs(dest_dir, exist_ok=True)

            # Create a decompressor
            dctx = zstd.ZstdDecompressor()

            # Read compressed input file and decompress to output file
            with open(source_path, "rb") as input_file:
                with open(dest_path, "wb") as output_file:
                    dctx.copy_stream(input_file, output_file)

            return True
        except (IOError, OSError, zstd.ZstdError) as e:
            print(f"Error decompressing {source_path}: {e}")
            # Clean up partial output if it exists
            if os.path.exists(dest_path):
                os.remove(dest_path)
            return False
