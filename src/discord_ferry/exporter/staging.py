"""Contained, Ferry-owned Discord export staging directories."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

from discord_ferry.errors import ExportError
from discord_ferry.parser.dce_parser import parse_single_export
from discord_ferry.state import MigrationState, StagedInputWitness


_STAGE_OWNER_NAME = ".ferry-owned-stage"
_STAGE_OWNER_VERSION = "ferry-owned-stage-v1"


def _stage_owner_contents(stage: Path) -> str:
    return f"{_STAGE_OWNER_VERSION}:{stage.name}\n"


def _require_stage_owner(stage: Path) -> None:
    marker = stage / _STAGE_OWNER_NAME
    if marker.is_symlink() or not marker.is_file():
        raise ExportError("Staged export is not Ferry-owned")
    try:
        contents = marker.read_text(encoding="utf-8")
    except OSError as exc:
        raise ExportError("Staged export ownership cannot be verified") from exc
    if contents != _stage_owner_contents(stage):
        raise ExportError("Staged export is not Ferry-owned")


def create_stage(output_root: Path, run_id: str) -> Path:
    """Create one owned stage below output_root without accepting path syntax."""
    if not run_id.isascii() or not run_id.replace("-", "").isalnum():
        raise ExportError("Invalid export stage identifier")
    if output_root.is_symlink():
        raise ExportError("Export output root is linked")
    root = output_root / "staged-exports"
    stage = root / run_id
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink():
        raise ExportError("Export stage root is linked")
    if stage.exists() or stage.is_symlink():
        raise ExportError("Export stage already exists")
    stage.mkdir()
    if not stage.resolve().is_relative_to(root.resolve()):
        raise ExportError("Export stage is outside output")
    try:
        with (stage / _STAGE_OWNER_NAME).open("x", encoding="utf-8") as marker:
            marker.write(_stage_owner_contents(stage))
    except OSError as exc:
        raise ExportError("Export stage ownership could not be recorded") from exc
    return stage


def _owned_stage(stage: Path, output_root: Path) -> Path:
    """Return a real, contained owned stage or refuse every indirection."""
    root = output_root / "staged-exports"
    if output_root.is_symlink() or root.is_symlink() or stage.is_symlink():
        raise ExportError("Linked staged export input")
    try:
        resolved_root = root.resolve(strict=True)
        resolved_stage = stage.resolve(strict=True)
    except OSError as exc:
        raise ExportError("Staged export input is unavailable") from exc
    if not resolved_stage.is_dir() or resolved_stage.parent != resolved_root:
        raise ExportError("Export stage is outside output")
    _require_stage_owner(stage)
    return resolved_stage


def inspect_stage(
    stage: Path, output_root: Path, scope: Literal["full", "bounded"]
) -> StagedInputWitness:
    """Read only owned stage JSON and retain every exported source, including empty ones."""
    owned_stage = _owned_stage(stage, output_root)
    ids: list[str] = []
    for path in owned_stage.glob("*.json"):
        if path.is_symlink():
            raise ExportError("Linked staged export input")
        try:
            source_id = parse_single_export(path, metadata_only=True).channel.id
        except (
            AttributeError,
            ExportError,
            KeyError,
            OSError,
            TypeError,
            UnicodeError,
            ValueError,
        ) as exc:
            raise ExportError(f"Invalid staged export JSON '{path.name}'") from exc
        if (
            not isinstance(source_id, str)
            or not source_id.isascii()
            or not source_id.isdecimal()
            or int(source_id) == 0
        ):
            raise ExportError("Invalid staged export source ID")
        ids.append(source_id)
    if len(ids) != len(set(ids)):
        raise ExportError("Duplicate source JSON in staged export")
    source_ids = tuple(sorted(ids))
    digest = hashlib.sha256("\n".join(source_ids).encode("ascii")).hexdigest()
    return StagedInputWitness(
        str(Path("staged-exports") / owned_stage.name),
        source_ids,
        digest,
        scope,
        True,
        False,
    )


def validate_coverage(witness: StagedInputWitness, expected_ids: tuple[str, ...]) -> bool:
    """Accept only an exact, non-empty current inventory match."""
    return bool(expected_ids) and witness.source_ids == tuple(sorted(set(expected_ids)))
