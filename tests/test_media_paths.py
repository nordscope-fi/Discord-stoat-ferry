"""Tests for the export path containment policy."""

from pathlib import Path

import pytest

from discord_ferry.parser.media_paths import (
    contained_media_path,
    is_media_escape,
    is_safe_filename_component,
)


def test_in_root_relative_returns_resolved_candidate(tmp_path: Path) -> None:
    (tmp_path / "inside").mkdir()
    target = tmp_path / "inside" / "normal.png"
    target.write_bytes(b"x")
    assert contained_media_path(tmp_path, "inside/normal.png") == target.resolve()


@pytest.mark.parametrize("relative", ["../outside-marker.txt", "/etc/hosts"])
def test_escape_variants_return_none(tmp_path: Path, relative: str) -> None:
    assert contained_media_path(tmp_path, relative) is None


def test_absolute_inside_root_returns_none(tmp_path: Path) -> None:
    target = tmp_path / "inside.png"
    target.write_bytes(b"x")
    assert contained_media_path(tmp_path, str(target)) is None


def test_symlink_escape_returns_none(tmp_path: Path) -> None:
    marker = tmp_path.parent / (tmp_path.name + "-marker.txt")
    marker.write_text("MARKER")
    (tmp_path / "link-marker.txt").symlink_to(marker)
    assert contained_media_path(tmp_path, "link-marker.txt") is None


def test_symlink_loop_returns_none_without_raising(tmp_path: Path) -> None:
    (tmp_path / "loop").symlink_to("loop")
    assert contained_media_path(tmp_path, "loop") is None


def test_symlinked_root_still_contains_children(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "inside.png").write_bytes(b"x")
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    assert contained_media_path(alias, "inside.png") == (real / "inside.png").resolve()


def test_is_media_escape_classification(tmp_path: Path) -> None:
    assert is_media_escape(tmp_path, "../x.png") is True
    assert is_media_escape(tmp_path, "inside.png") is False
    assert is_media_escape(tmp_path, "https://cdn.invalid/x.png") is False
    assert is_media_escape(tmp_path, "") is False


def test_is_safe_filename_component() -> None:
    assert is_safe_filename_component("1234567890") is True
    assert is_safe_filename_component("../../x") is False
    assert is_safe_filename_component("/abs/x") is False
    assert is_safe_filename_component("") is False
    assert is_safe_filename_component("123\n") is False
