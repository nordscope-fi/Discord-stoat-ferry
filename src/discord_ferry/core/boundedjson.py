"""Load one whole JSON document from disk with a size bound and a clean nesting error."""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING, Any

import ijson  # type: ignore[import-untyped]

if TYPE_CHECKING:
    from pathlib import Path


_SCALAR_EVENTS = frozenset({"null", "boolean", "integer", "double", "number", "string"})


class JsonLoadRefusedError(ValueError):
    """The file was refused for its size or nesting, not because it is malformed.

    A ``ValueError`` so existing callers that report a bad file keep working, and a
    distinct class so a caller that skips malformed files can still stop on this.
    """


def load_bounded_json(path: Path, max_bytes: int) -> Any:
    """Read ``path`` as JSON, refusing it before the read if it is over ``max_bytes``.

    The size comes from ``stat()``, so an oversized file is refused without
    allocating anything for its content. Deep nesting makes ``json.loads`` raise
    ``RecursionError``, which is turned into the same ``ValueError`` family as the
    size refusal. A malformed file still raises ``json.JSONDecodeError``, and a
    missing file still raises ``FileNotFoundError``.

    Raises:
        JsonLoadRefusedError: If the file is over ``max_bytes`` or is nested too deeply.
    """
    size = path.stat().st_size
    if size > max_bytes:
        raise JsonLoadRefusedError(
            f"{path.name} is too large to load ({size} bytes, limit {max_bytes})"
        )
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except RecursionError:
        raise JsonLoadRefusedError(f"{path.name} is nested too deeply to load") from None


def load_bounded_json_metadata(path: Path, max_bytes: int) -> dict[str, Any]:
    """Stream the DCE top-level metadata without constructing its messages array.

    The returned mapping contains the direct scalar members of ``guild`` and
    ``channel``, the scalar ``messageCount`` and ``exportedAt`` values, and an
    empty list when ``messages`` is an array. Other top-level content is parsed
    but not retained. Size and nesting refusals match :func:`load_bounded_json`.
    """
    size = path.stat().st_size
    if size > max_bytes:
        raise JsonLoadRefusedError(
            f"{path.name} is too large to load ({size} bytes, limit {max_bytes})"
        )

    result: dict[str, Any] = {}
    top_level_is_map = False
    saw_event = False
    depth = 0
    top_level_key = ""
    object_field = ""

    try:
        with path.open("rb") as source:
            for _prefix, event, value in ijson.parse(source):
                if not saw_event:
                    saw_event = True
                    top_level_is_map = event == "start_map"

                if event in {"start_map", "start_array"}:
                    depth += 1
                    if depth > sys.getrecursionlimit():
                        raise JsonLoadRefusedError(f"{path.name} is nested too deeply to load")

                if event == "map_key" and depth == 1:
                    top_level_key = str(value)
                    object_field = ""
                    if top_level_key in {
                        "guild",
                        "channel",
                        "messages",
                        "messageCount",
                        "exportedAt",
                    }:
                        result[top_level_key] = None
                elif event == "map_key" and depth == 2 and top_level_key in {"guild", "channel"}:
                    object_field = str(value)
                elif event == "start_map" and depth == 2 and top_level_key in {"guild", "channel"}:
                    result[top_level_key] = {}
                elif event == "start_array" and depth == 2 and top_level_key == "messages":
                    result["messages"] = []
                elif (
                    event in _SCALAR_EVENTS
                    and depth == 1
                    and top_level_key in {"messageCount", "exportedAt"}
                ):
                    result[top_level_key] = value
                elif (
                    event in _SCALAR_EVENTS and depth == 2 and top_level_key in {"guild", "channel"}
                ):
                    container = result.get(top_level_key)
                    if isinstance(container, dict):
                        container[object_field] = value

                if event in {"end_map", "end_array"}:
                    depth -= 1
    except ijson.common.JSONError as exc:
        raise ValueError(str(exc)) from exc

    if not top_level_is_map:
        raise ValueError("The top-level JSON value must be an object")
    return result
