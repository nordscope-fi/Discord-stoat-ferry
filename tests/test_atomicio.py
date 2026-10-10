"""Tests for the shared atomic text writer.

Issue #175. Three writers put a document straight at its final path, so an
interrupted write truncated it and the previous good copy was already gone. The
blueprint mattered most: export_blueprint and import_blueprint are a matched
pair, so a truncated export outlived the run that produced it.
"""

from __future__ import annotations

import ast
import os
import stat
import sys
from pathlib import Path
from typing import IO

import pytest

from discord_ferry.core import atomicio
from discord_ferry.core.atomicio import (
    atomic_write_text,
    ensure_output_subdir,
    replace_with_retry,
    write_bytes_no_follow,
    write_text_no_follow,
)


def test_writes_a_new_file(tmp_path: Path) -> None:
    target = tmp_path / "doc.json"

    atomic_write_text(target, '{"a": 1}')

    assert target.read_text(encoding="utf-8") == '{"a": 1}'


def _leftovers(directory: Path, name: str = "doc.json") -> list[str]:
    """Every staging file left in *directory*, whatever name the writer gave it."""
    return sorted(p.name for p in directory.iterdir() if p.name != name)


def test_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    target = tmp_path / "doc.json"

    atomic_write_text(target, "x")

    assert not (tmp_path / "doc.json.tmp").exists()
    assert _leftovers(tmp_path) == []
    assert [p.name for p in tmp_path.iterdir()] == ["doc.json"]


def test_overwrites_an_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "doc.json"
    atomic_write_text(target, "first")

    atomic_write_text(target, "second")

    assert target.read_text(encoding="utf-8") == "second"


def test_overwrites_under_windows_rename_semantics(
    windows_filesystem: None, tmp_path: Path
) -> None:
    """The helper must not reintroduce #172.

    windows_filesystem makes Path.rename refuse an existing destination, the way
    Win32 MoveFile does. A helper written with rename rather than replace fails
    here and passes everywhere else, which is the whole reason the fixture exists.
    """
    target = tmp_path / "doc.json"
    atomic_write_text(target, "first")

    atomic_write_text(target, "second")

    assert target.read_text(encoding="utf-8") == "second"


def test_a_partial_write_leaves_the_previous_file_intact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """This is the property #175 is actually about.

    The failure has to *truncate*, not merely raise. An earlier version of this
    test patched write_text to raise before writing anything, and a direct write
    then left the target untouched too, so it passed against the exact defect it
    was written to catch. The mutation harness caught that. This one writes half
    the new content and then fails, which is what a full disk does.
    """
    target = tmp_path / "doc.json"
    atomic_write_text(target, '{"good": true}')
    real_fdopen = os.fdopen

    class _HalfThenFail:
        def __init__(self, handle: IO[str]) -> None:
            self._handle = handle

        def __enter__(self) -> _HalfThenFail:
            return self

        def __exit__(self, *exc: object) -> None:
            self._handle.close()

        def write(self, data: str) -> int:
            self._handle.write(data[: len(data) // 2])
            raise OSError(28, "No space left on device")

    monkeypatch.setattr(atomicio.os, "fdopen", lambda *a, **k: _HalfThenFail(real_fdopen(*a, **k)))
    with pytest.raises(OSError):
        atomic_write_text(target, '{"replacement": true}')

    monkeypatch.undo()
    assert target.read_text(encoding="utf-8") == '{"good": true}'
    assert _leftovers(tmp_path) == []


def test_a_failed_swap_leaves_the_previous_file_intact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The other half: the write succeeds and the swap is what fails."""
    target = tmp_path / "doc.json"
    atomic_write_text(target, '{"good": true}')

    def refuse(self: Path, *args: object, **kwargs: object) -> None:
        raise PermissionError(13, "destination held open")

    monkeypatch.setattr(Path, "replace", refuse)
    with pytest.raises(PermissionError):
        atomic_write_text(target, '{"new": true}')

    monkeypatch.undo()
    assert target.read_text(encoding="utf-8") == '{"good": true}'
    assert _leftovers(tmp_path) == []


# --- Retry on a held-open destination (#176) --------------------------------
# On Windows, MoveFileEx fails with PermissionError when another process holds the
# destination open without FILE_SHARE_DELETE (OneDrive, antivirus). The swap is
# retried a few times on PermissionError, on Windows only.


class _FlakyReplace:
    """Stand-in for Path.replace that raises *error* for the first *failures* calls."""

    def __init__(self, failures: int, error: OSError) -> None:
        self.failures = failures
        self.error = error
        self.calls = 0
        self._real = Path.replace

    def __call__(self, source: Path, destination: Path) -> Path:
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        return self._real(source, destination)


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record the delays the retry asks for, without waiting."""
    recorded: list[float] = []
    monkeypatch.setattr(atomicio.time, "sleep", recorded.append)
    return recorded


def _flaky(
    monkeypatch: pytest.MonkeyPatch, failures: int, error: OSError | None = None
) -> _FlakyReplace:
    flaky = _FlakyReplace(failures, error or PermissionError(13, "held open"))
    # A plain function, so Path binds it as a method. A callable instance would not bind.
    monkeypatch.setattr(Path, "replace", lambda source, destination: flaky(source, destination))
    return flaky


def test_retries_a_held_open_destination_on_windows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sleeps: list[float]
) -> None:
    target = tmp_path / "doc.json"
    target.write_text("old", encoding="utf-8")
    monkeypatch.setattr(sys, "platform", "win32")
    flaky = _flaky(monkeypatch, failures=3)

    atomic_write_text(target, "new")

    assert flaky.calls == 4
    assert sleeps == [0.05, 0.1, 0.2]
    assert target.read_text(encoding="utf-8") == "new"
    assert _leftovers(tmp_path) == []


def test_succeeds_on_the_last_attempt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sleeps: list[float]
) -> None:
    target = tmp_path / "doc.json"
    target.write_text("old", encoding="utf-8")
    monkeypatch.setattr(sys, "platform", "win32")
    flaky = _flaky(monkeypatch, failures=atomicio.REPLACE_ATTEMPTS - 1)

    atomic_write_text(target, "new")

    assert flaky.calls == atomicio.REPLACE_ATTEMPTS
    assert sleeps == [0.05, 0.1, 0.2, 0.4]
    assert target.read_text(encoding="utf-8") == "new"


def test_gives_up_after_the_last_attempt_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sleeps: list[float]
) -> None:
    target = tmp_path / "doc.json"
    target.write_text("old", encoding="utf-8")
    monkeypatch.setattr(sys, "platform", "win32")
    error = PermissionError(13, "held open")
    flaky = _flaky(monkeypatch, failures=atomicio.REPLACE_ATTEMPTS, error=error)

    with pytest.raises(PermissionError) as raised:
        atomic_write_text(target, "new")

    assert raised.value is error
    assert flaky.calls == atomicio.REPLACE_ATTEMPTS
    assert sleeps == [0.05, 0.1, 0.2, 0.4]
    monkeypatch.undo()
    assert target.read_text(encoding="utf-8") == "old"
    assert _leftovers(tmp_path) == []


def test_does_not_retry_other_oserrors_on_windows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sleeps: list[float]
) -> None:
    target = tmp_path / "doc.json"
    monkeypatch.setattr(sys, "platform", "win32")
    flaky = _flaky(monkeypatch, failures=1, error=OSError(28, "No space left on device"))

    with pytest.raises(OSError, match="No space"):
        atomic_write_text(target, "new")

    assert flaky.calls == 1
    assert sleeps == []


def test_does_not_retry_permission_errors_off_windows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sleeps: list[float]
) -> None:
    target = tmp_path / "doc.json"
    monkeypatch.setattr(sys, "platform", "linux")
    flaky = _flaky(monkeypatch, failures=1)

    with pytest.raises(PermissionError):
        atomic_write_text(target, "new")

    assert flaky.calls == 1
    assert sleeps == []


def test_replace_with_retry_moves_a_file_without_sleeping(
    tmp_path: Path, sleeps: list[float]
) -> None:
    source = tmp_path / "a.part"
    destination = tmp_path / "a"
    source.write_text("x", encoding="utf-8")

    replace_with_retry(source, destination)

    assert destination.read_text(encoding="utf-8") == "x"
    assert sleeps == []


# --- The guard --------------------------------------------------------------
# The #172 review noted that nothing stopped a new writer using Path.rename
# again: the grep was a one-time check and the simulated fixture only patches
# Path.rename. This runs on every pull request instead.

_DOCUMENT_MODULES = (
    "state.py",
    "discord/metadata.py",
    "reporter.py",
    "blueprint.py",
)


def test_document_modules_never_write_text_directly() -> None:
    """The four modules owning Ferry's durable documents must go through the helper.

    Scoped to four named modules rather than all of src/ on purpose. A repo-wide
    ban would fire on the avatar, banner, role-icon and thread-archive writers,
    which are legitimately direct, and would then need an allowlist that grows
    until the rule means nothing.
    """
    src_root = Path(__file__).resolve().parents[1] / "src" / "discord_ferry"
    offenders: list[str] = []

    for rel in _DOCUMENT_MODULES:
        path = src_root / rel
        assert path.exists(), f"{rel} moved; update _DOCUMENT_MODULES"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "write_text"
            ):
                offenders.append(f"{rel}:{node.lineno}")

    assert not offenders, (
        "these modules own durable documents and must call atomic_write_text "
        f"rather than write_text directly: {offenders}"
    )


def test_a_cleanup_failure_does_not_replace_the_original_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If removing the temp file also fails, the caller still sees why the write failed."""
    target = tmp_path / "doc.json"

    def refuse(self: Path, *args: object, **kwargs: object) -> None:
        raise PermissionError(13, "destination held open")

    def stuck(self: Path, *args: object, **kwargs: object) -> None:
        raise PermissionError(32, "temp file still open")

    monkeypatch.setattr(Path, "replace", refuse)
    monkeypatch.setattr(Path, "unlink", stuck)
    with pytest.raises(PermissionError, match="destination held open"):
        atomic_write_text(target, '{"new": true}')


# --- Random exclusive staging name (#960) ------------------------------------
# The staging file used to be "<name>.tmp", a name anyone with entry-creation
# rights in a shared folder could predict and plant a symlink at. write_text
# follows symlinks, so the write landed in whatever the link pointed at.

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="needs POSIX symlinks and modes")


@posix_only
def test_a_symlink_at_the_old_staging_name_is_not_written_through(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "owner-only.txt"
    victim.write_text("secret", encoding="utf-8")
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "state.json.tmp").symlink_to(victim)
    target = shared / "state.json"

    atomic_write_text(target, "new content")

    assert victim.read_text(encoding="utf-8") == "secret"
    assert target.read_text(encoding="utf-8") == "new content"
    assert not target.is_symlink()


@posix_only
def test_a_dangling_symlink_at_the_old_staging_name_creates_nothing_outside(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    planted_goal = outside / "created-by-attacker-link"
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "doc.json.tmp").symlink_to(planted_goal)

    atomic_write_text(shared / "doc.json", "new content")

    assert not planted_goal.exists()
    assert (shared / "doc.json").read_text(encoding="utf-8") == "new content"


def test_the_staging_name_is_not_predictable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[str] = []
    real_replace = atomicio.replace_with_retry

    def spy(source: Path, destination: Path) -> None:
        seen.append(source.name)
        real_replace(source, destination)

    monkeypatch.setattr(atomicio, "replace_with_retry", spy)
    target = tmp_path / "doc.json"

    atomic_write_text(target, "a")
    atomic_write_text(target, "b")

    assert len(seen) == 2
    assert seen[0] != seen[1]
    assert "doc.json.tmp" not in seen
    assert all(name.startswith(".doc.json.") and name.endswith(".tmp") for name in seen)


def test_two_writes_to_one_target_do_not_collide(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "doc.json"
    staged: list[Path] = []
    real_replace = atomicio.replace_with_retry

    def hold(source: Path, destination: Path) -> None:
        # Start a second write while the first one's staging file still exists.
        staged.append(source)
        if len(staged) == 1:
            atomic_write_text(target, "inner")
        real_replace(source, destination)

    monkeypatch.setattr(atomicio, "replace_with_retry", hold)
    atomic_write_text(target, "outer")

    assert len({p.name for p in staged}) == 2
    assert target.read_text(encoding="utf-8") == "outer"
    assert _leftovers(tmp_path) == []


@posix_only
def test_the_written_file_is_owner_only(tmp_path: Path) -> None:
    """Staging through mkstemp gives 0600, and the swap carries it onto the document."""
    target = tmp_path / "doc.json"

    atomic_write_text(target, "x")

    assert stat.S_IMODE(target.stat().st_mode) == 0o600


# --- Direct writes that refuse a planted link (#960) -------------------------


def test_write_bytes_no_follow_writes_a_new_file(tmp_path: Path) -> None:
    target = tmp_path / "a.png"

    write_bytes_no_follow(target, b"\x89PNG-bytes")

    assert target.read_bytes() == b"\x89PNG-bytes"


def test_write_bytes_no_follow_replaces_an_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "a.png"
    target.write_bytes(b"a much longer previous body")

    write_bytes_no_follow(target, b"new")

    assert target.read_bytes() == b"new"


def test_write_text_no_follow_writes_utf8(tmp_path: Path) -> None:
    target = tmp_path / "a.md"

    write_text_no_follow(target, "caf\u00e9\nline two")

    assert target.read_bytes().decode("utf-8").replace("\r\n", "\n") == "caf\u00e9\nline two"


@posix_only
def test_write_bytes_no_follow_refuses_a_planted_link(tmp_path: Path) -> None:
    victim = tmp_path / "owner-only.txt"
    victim.write_text("secret", encoding="utf-8")
    link = tmp_path / "a.png"
    link.symlink_to(victim)

    with pytest.raises(OSError):
        write_bytes_no_follow(link, b"new")

    assert victim.read_text(encoding="utf-8") == "secret"


@posix_only
def test_write_text_no_follow_refuses_a_dangling_planted_link(tmp_path: Path) -> None:
    goal = tmp_path / "created-through-the-link"
    link = tmp_path / "a.md"
    link.symlink_to(goal)

    with pytest.raises(OSError):
        write_text_no_follow(link, "new")

    assert not goal.exists()


# --- Output subfolders that refuse a planted link (#960) ---------------------


def test_ensure_output_subdir_creates_nested_folders(tmp_path: Path) -> None:
    target = tmp_path / "out" / "threads" / "general"

    ensure_output_subdir(target, tmp_path / "out")

    assert target.is_dir()


def test_ensure_output_subdir_accepts_an_existing_real_folder(tmp_path: Path) -> None:
    (tmp_path / "icons").mkdir()

    ensure_output_subdir(tmp_path / "icons", tmp_path)
    ensure_output_subdir(tmp_path / "icons", tmp_path)

    assert (tmp_path / "icons").is_dir()


@posix_only
def test_ensure_output_subdir_makes_new_folders_owner_only(tmp_path: Path) -> None:
    ensure_output_subdir(tmp_path / "a" / "b", tmp_path)

    assert stat.S_IMODE((tmp_path / "a").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "a" / "b").stat().st_mode) == 0o700


@posix_only
@pytest.mark.parametrize("planted", ["leaf", "middle"])
def test_ensure_output_subdir_refuses_a_planted_link(tmp_path: Path, planted: str) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "out"
    root.mkdir()
    if planted == "leaf":
        (root / "icons").symlink_to(outside, target_is_directory=True)
        target = root / "icons"
    else:
        (root / "threads").symlink_to(outside, target_is_directory=True)
        target = root / "threads" / "general"

    with pytest.raises(OSError):
        ensure_output_subdir(target, root)

    assert list(outside.iterdir()) == []


@posix_only
def test_ensure_output_subdir_refuses_a_link_to_a_folder_inside_the_root(tmp_path: Path) -> None:
    """A link is refused even when it stays inside the root: the check is per component."""
    root = tmp_path / "out"
    (root / "real").mkdir(parents=True)
    (root / "icons").symlink_to(root / "real", target_is_directory=True)

    with pytest.raises(OSError):
        ensure_output_subdir(root / "icons", root)


def test_ensure_output_subdir_refuses_a_path_outside_the_root(tmp_path: Path) -> None:
    (tmp_path / "out").mkdir()

    with pytest.raises(OSError):
        ensure_output_subdir(tmp_path / "elsewhere", tmp_path / "out")

    assert not (tmp_path / "elsewhere").exists()
