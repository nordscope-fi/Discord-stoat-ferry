"""Valid Stoat ids for tests that save a state and load it back.

``load_state`` refuses a saved destination id that is not a 26-character Crockford
base32 ULID (#970), so a test that round-trips a state through ``state.json`` needs
ids of that shape. These are not real ULIDs, but they pass the same shape check and
sort in ``n`` order, like real ones.
"""

from __future__ import annotations


def stoat_id(n: int) -> str:
    """A well-formed 26-character Stoat id, distinct per ``n`` and sorted by it."""
    return f"01J5T{n:021d}"


#: A well-formed Stoat server id for states that are saved and loaded back.
SERVER_ID = stoat_id(9000)
