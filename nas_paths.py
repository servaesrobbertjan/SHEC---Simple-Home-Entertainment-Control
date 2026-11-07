"""Lets the same config.json work on Windows and Linux.

On Windows a NAS folder is a UNC path like \\\\NAS\\video\\films.
On Linux the share \\\\NAS\\video is mounted (by setup_linux.py) at /mnt/shec/nas/video,
so \\\\NAS\\video\\films becomes /mnt/shec/nas/video/films.
"""
import platform
import re

MOUNT_BASE = "/mnt/shec"
IS_WINDOWS = platform.system() == "Windows"


def split_unc(path):
    """'\\\\host\\share\\a\\b' or '//host/share/a/b' -> (host, share, ['a', 'b']), else None."""
    m = re.match(r"^(?:\\\\|//)([^\\/]+)[\\/]+([^\\/]+)(.*)$", path or "")
    if not m:
        return None
    host, share, rest = m.groups()
    return host, share, [p for p in re.split(r"[\\/]+", rest) if p]


def mount_point(host, share):
    return f"{MOUNT_BASE}/{host.lower()}/{share.lower()}"


def local_path(path):
    """Translate a UNC path to where it lives on this machine."""
    if IS_WINDOWS:
        return path
    unc = split_unc(path)
    if not unc:
        return path
    host, share, parts = unc
    return "/".join([mount_point(host, share), *parts])
