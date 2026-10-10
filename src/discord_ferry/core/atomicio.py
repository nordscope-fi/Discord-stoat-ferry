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
