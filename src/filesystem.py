import os
import sys


def sync_directory(path):
    """Flush directory entries on POSIX; Windows CRT cannot open directories."""
    if sys.platform == "win32":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
