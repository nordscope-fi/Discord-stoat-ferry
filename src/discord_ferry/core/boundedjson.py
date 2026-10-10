"""Load one whole JSON document from disk with a size bound and a clean nesting error."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path


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
