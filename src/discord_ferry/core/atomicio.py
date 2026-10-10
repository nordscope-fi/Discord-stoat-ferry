"""Atomic text writes for the documents Ferry has to be able to read back.

Two defects live here, and both are cheap to reintroduce by writing the obvious
thing at a new call site.

``write_text`` truncates its target the moment it opens it, so a crash, a full
disk or a killed process partway through leaves a half-written file where a
complete one is expected, and the previous good copy is already gone. That was
issue #175. The blueprint was the case that mattered: ``export_blueprint`` and
``import_blueprint`` are a matched pair, so a truncated export is read back by a
later run and the damage outlives the run that caused it.

``Path.rename`` looks like the fix and is not, on Windows. It calls ``os.rename``,
which replaces the destination on POSIX and refuses it on Win32, where the
overwrite is ``MoveFileEx`` with ``MOVEFILE_REPLACE_EXISTING``. Reaching that
from Python means ``os.replace``, so ``Path.replace``. That was issue #172, and
because the destination only exists from the second write onward, it killed the
second checkpoint save of every migration on Windows.

Both are guarded. ``tests/test_atomicio.py`` asserts the previous file survives a
partial write and a failed swap, and exercises the swap under simulated Win32
semantics.

It also walks the four document-owning modules with ``ast`` and fails the build
on a direct ``write_text`` call in any of them. That catches the mistake someone
is actually likely to make, and no more: it does not see ``open(path, "w")``, an
aliased method, ``getattr``, or a fifth module that starts owning a document. It
is a tripwire on the obvious path, not proof of coverage.
"""

import contextlib
import errno
import os
import sys
import tempfile
import time
from pathlib import Path

# ADR-038. A holder such as OneDrive or an antivirus scan usually lets go within a
# few hundred milliseconds, so 5 attempts with delays of 0.05, 0.1, 0.2 and 0.4 s
# (0.75 s worst case) ride that out without stalling a checkpoint for long.
REPLACE_ATTEMPTS = 5
REPLACE_FIRST_DELAY_SECONDS = 0.05


def replace_with_retry(source: Path, destination: Path) -> None:
    """Swap *source* over *destination*, retrying a held-open destination on Windows.

    ``MoveFileEx`` fails with ``PermissionError`` (winerror 5 or 32) while another
    process holds *destination* open without ``FILE_SHARE_DELETE`` (issue #176).
    That clears by itself, so on Windows only, ``PermissionError`` only, the swap
    is retried with a doubling delay. After the last attempt the original error is
    raised unchanged. Any other error, and any error off Windows, is raised at once.

    The sleep is ``time.sleep``, so a caller inside the event loop blocks for up to
    0.75 s, and only when a Windows machine is already failing the swap.
    """
    delay = REPLACE_FIRST_DELAY_SECONDS
    for attempt in range(1, REPLACE_ATTEMPTS + 1):
        try:
            source.replace(destination)
            return
        except PermissionError:
            if sys.platform != "win32" or attempt == REPLACE_ATTEMPTS:
                raise
        time.sleep(delay)
        delay *= 2


def atomic_write_text(path: Path, text: str) -> None:
    """Write *text* to *path*, replacing any existing file, with no truncation window.

    The parent directory must already exist. Every caller creates it as part of
    its own work, and creating it here would change their behaviour rather than
    tidy it.

    The temporary file sits in the same directory as *path* (a rename across
    filesystems would not be atomic) under a random name, ``.<name>.<random>.tmp``.
    It is created with ``O_CREAT | O_EXCL`` and mode 0600, so an entry another
    account planted in a shared folder, a symlink included, is never opened or
    followed (#960). The final document therefore ends up owner-only too. A failed
    write or swap removes the temporary file. Nothing in Ferry reads a ``.tmp``
    path, so one left behind by a crash is inert, and a later write picks a new
    name instead of reusing it.

    Args:
        path: Final location of the document.
        text: Complete contents. Any redaction has already been applied by the
            caller, which is where ADR-014 puts it.
    """
    descriptor, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        # The handle is closed before the swap: Windows refuses to replace a file
        # this process still holds open.
        replace_with_retry(tmp_path, path)
    except BaseException:
        # A temp file Windows still holds open cannot be removed either. The caller
        # needs the error that stopped the write, not this one.
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise


def _open_no_follow(path: Path) -> int:
    """Open *path* for writing, truncating it, and refuse to follow a symlink.

    ``O_NOFOLLOW`` makes the open fail with ``ELOOP`` when the last component is a
    symlink, a dangling one included, so a link planted at a guessable name in a
    shared output folder cannot redirect the write (#960). The file is created
    with mode 0600 when it does not exist.

    ``O_NOFOLLOW`` does not exist on Windows, where this opens the path the plain
    way. There the file system, not this call, decides what a link may do.
    """
    # O_BINARY matters on Windows only: without it os.open uses CRT text mode, which
    # turns every LF into CRLF and corrupts a PNG signature (\r\n\x1a\n).
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_TRUNC
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    return os.open(path, flags, 0o600)


def write_bytes_no_follow(path: Path, data: bytes) -> None:
    """Write *data* to *path* in place, raising ``OSError`` if *path* is a symlink.

    For the writers that stay direct on purpose (avatars, role icons), where the
    file is read straight back and a temp-then-swap would add nothing. Unlike
    ``atomic_write_text`` the target is truncated as soon as it is opened, so use
    it only where a half-written file is harmless.
    """
    with os.fdopen(_open_no_follow(path), "wb") as handle:
        handle.write(data)


def write_text_no_follow(path: Path, text: str) -> None:
    """Write UTF-8 *text* to *path* in place, raising ``OSError`` if *path* is a symlink.

    The text counterpart of :func:`write_bytes_no_follow`, with the same newline
    handling as ``Path.write_text``.
    """
    with os.fdopen(_open_no_follow(path), "w", encoding="utf-8") as handle:
        handle.write(text)


def ensure_output_subdir(path: Path, root: Path) -> None:
    """Create *path* under *root* and raise ``OSError`` if a symlink sits on the way.

    Each component from *root* down to *path* is checked with ``is_symlink`` before
    it is created or used, so a link planted at ``role-icons`` or ``threads/<name>``
    cannot send later writes to another folder (#960). A link is refused even when
    it points back inside *root*. The resolved path must also stay inside the
    resolved *root*. *root* itself is the operator's choice and may be a link.

    New folders get mode 0700. There is a window between the check and the next
    ``mkdir`` that this does not close, which would need descriptor-relative
    operations the platform does not offer everywhere. The final ``resolve`` check
    narrows it.
    """
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise OSError(errno.EINVAL, "output subfolder is outside the output folder") from None
    root.mkdir(parents=True, exist_ok=True)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise OSError(errno.ELOOP, "output subfolder is a symlink")
        current.mkdir(mode=0o700, exist_ok=True)
    if not path.resolve().is_relative_to(root.resolve()):
        raise OSError(errno.EINVAL, "output subfolder resolves outside the output folder")
