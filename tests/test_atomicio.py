"""Tests for the shared atomic text writer.

Issue #175. Three writers put a document straight at its final path, so an
interrupted write truncated it and the previous good copy was already gone. The
blueprint mattered most: export_blueprint and import_blueprint are a matched
pair, so a truncated export outlived the run that produced it.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from discord_ferry.core import atomicio
from discord_ferry.core.atomicio import atomic_write_text, replace_with_retry


def test_writes_a_new_file(tmp_path: Path) -> None:
    target = tmp_path / "doc.json"

    atomic_write_text(target, '{"a": 1}')

    assert target.read_text(encoding="utf-8") == '{"a": 1}'


def test_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    target = tmp_path / "doc.json"

    atomic_write_text(target, "x")

    assert not (tmp_path / "doc.json.tmp").exists()
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
    real_write_text = Path.write_text

    def half_then_fail(self: Path, data: str, *args: object, **kwargs: object) -> None:
        real_write_text(self, data[: len(data) // 2], encoding="utf-8")
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "write_text", half_then_fail)
    with pytest.raises(OSError):
        atomic_write_text(target, '{"replacement": true}')

    monkeypatch.undo()
    assert target.read_text(encoding="utf-8") == '{"good": true}'


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
    assert not (tmp_path / "doc.json.tmp").exists()


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
    assert [p.name for p in tmp_path.iterdir()] == ["doc.json"]


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
