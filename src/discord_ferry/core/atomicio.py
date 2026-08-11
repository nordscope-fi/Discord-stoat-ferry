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

from pathlib import Path


def atomic_write_text(path: Path, text: str) -> None:
    """Write *text* to *path*, replacing any existing file, with no truncation window.

    The parent directory must already exist. Every caller creates it as part of
    its own work, and creating it here would change their behaviour rather than
    tidy it.

    The temporary file is *path* with ``.tmp`` appended, so a document at
    ``state.json`` stages through ``state.json.tmp``. Nothing in Ferry reads a
    ``.tmp`` path, so one left behind by a crash is inert and the next write
    overwrites it.

    Args:
        path: Final location of the document.
        text: Complete contents. Any redaction has already been applied by the
            caller, which is where ADR-014 puts it.
    """
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(text, encoding="utf-8")
    tmp_path.replace(path)
