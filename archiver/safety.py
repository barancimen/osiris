"""A last line of defence for your files: refuse to change anything outside the places we write to.

The code only ever opens your files for reading, but it also runs third-party parsers on them, and
a bug there, or a crafted file, shouldn't be able to reach them. This hook sees each file operation
made by Python code in this process, including in libraries, and raises PermissionError if it would
create, change or delete something outside the output directory, the system temp directory or
~/.cache. Reading is never affected. It can't see what other programs do, or native code that makes
its own system calls: tesseract only gets a page image on stdin, and pdfium only reads.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOTS_ENV = "ARCHIVER_WRITE_ROOTS"
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
# event -> which arguments are paths whose target gets changed
EVENTS = {"os.remove": (0,), "os.rename": (0, 1), "os.rmdir": (0,), "os.truncate": (0,), "os.link": (1,),
          "os.symlink": (1,), "os.mkdir": (0,), "os.chmod": (0,), "os.chown": (0,), "os.utime": (0,),
          "shutil.rmtree": (0,), "shutil.move": (0, 1)}
# these act on the name itself, so a symlink is judged by where it sits rather than what it points at
ON_THE_NAME = {"os.remove", "os.rename", "os.rmdir", "os.symlink", "os.link", "shutil.rmtree", "shutil.move"}

_roots: tuple[str, ...] = ()
_installed = False


def _resolve(path, dir_fd=None, on_name: bool = False) -> str | None:
    """The absolute location, or None when there's no way to tell."""
    if isinstance(path, int):
        return None
    path = os.fsdecode(path)
    if not os.path.isabs(path):
        if dir_fd is not None:  # how shutil.rmtree works; it was already judged by its top directory
            try:
                path = os.path.join(os.readlink(f"/proc/self/fd/{dir_fd}"), path)
            except OSError:
                return None
        else:
            path = os.path.join(os.getcwd(), path)
    if on_name:
        head, tail = os.path.split(os.path.normpath(path))
        return os.path.join(os.path.realpath(head), tail)
    return os.path.realpath(path)


def allowed(location: str) -> bool:
    if "__pycache__" in location.split(os.sep):  # Python's own compiled files
        return True
    return any(location == root or location.startswith(root + os.sep) for root in _roots)


def _refuse(action: str, location: str) -> None:
    message = f"archiver refused to {action} {location}: it's outside the folders archiver writes to"
    print(message, file=sys.stderr)
    raise PermissionError(message)


def _hook(event: str, args: tuple) -> None:
    if event == "open":
        path, _, flags = args
        if isinstance(flags, int) and flags & WRITE_FLAGS and path is not None:
            location = _resolve(path)
            if location is not None and not allowed(location):
                _refuse("write to", location)
        return
    positions = EVENTS.get(event)
    if positions is None:
        return
    dir_fd = args[-1] if event in ("os.remove", "os.rmdir", "shutil.rmtree") else None
    for i in positions:
        location = _resolve(args[i], dir_fd if isinstance(dir_fd, int) and dir_fd >= 0 else None, event in ON_THE_NAME)
        if location is not None and not allowed(location):
            _refuse(f"change ({event})", location)


def install(*writable: str | os.PathLike) -> None:
    """Allow writes under these paths, and turn the guard on for good: audit hooks can't be removed."""
    global _roots, _installed
    home = Path.home()
    roots = [*writable, tempfile.gettempdir(), home / ".cache", "/dev"]
    _roots = tuple(os.path.realpath(p) for p in roots)
    os.environ[ROOTS_ENV] = os.pathsep.join(_roots)
    if not _installed:
        _installed = True
        sys.addaudithook(_hook)


def install_from_env() -> None:
    """For worker processes, which a spawn start method doesn't give the parent's hook."""
    if not _installed and os.environ.get(ROOTS_ENV):
        install(*os.environ[ROOTS_ENV].split(os.pathsep))
