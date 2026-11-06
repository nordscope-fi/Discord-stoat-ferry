"""Current Discord-source inventory and conservative export decisions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping


def _is_snowflake(value: object) -> bool:
    return isinstance(value, str) and value.isascii() and value.isdecimal() and int(value) > 0


@dataclass(frozen=True)
class SourceInventory:
    """The exact current source IDs and whether the read completed."""

    ids: tuple[str, ...]
    complete: bool


@dataclass(frozen=True)
class ExportDecision:
    """A full or bounded guild export request chosen from verified evidence."""

    scope: Literal["full", "bounded"]
    expected_ids: tuple[str, ...]
    after_id: str | None
    before_id: str | None
    reason: str


def _full(inventory: SourceInventory, reason: str) -> ExportDecision:
    return ExportDecision("full", inventory.ids, None, None, reason)


def select_export_decision(
    inventory: SourceInventory,
    high_water: Mapping[str, str],
    failed_ids: Mapping[str, tuple[str, ...]] | None,
    previous_complete: bool,
) -> ExportDecision:
    """Return a bound only when every source and retry has coverage proof."""
    if not inventory.ids or not inventory.complete or not previous_complete or failed_ids is None:
        return _full(inventory, "coverage is not proven")
    if len(inventory.ids) != len(set(inventory.ids)) or any(
        not _is_snowflake(source_id) for source_id in inventory.ids
    ):
        return _full(inventory, "inventory is invalid")
    markers = tuple(high_water.get(source_id) for source_id in inventory.ids)
    if any(not _is_snowflake(marker) for marker in markers):
        return _full(inventory, "coverage is not proven")
    if any(source_id not in inventory.ids for source_id in failed_ids):
        return _full(inventory, "retry coverage is not proven")
    protected = tuple(message_id for ids in failed_ids.values() for message_id in ids)
    if any(not _is_snowflake(message_id) for message_id in protected):
        return _full(inventory, "retry evidence is invalid")
    candidates = [int(marker) for marker in markers if marker is not None]
    candidates.extend(int(message_id) - 1 for message_id in protected)
    return ExportDecision(
        "bounded", inventory.ids, str(min(candidates)), None, "complete current coverage"
    )
