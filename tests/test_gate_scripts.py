"""Subprocess drivers for the instruction-layer gate scripts.

The scripts are the deliverable, so these tests drive them the way `/df-ship` and
CI do: run them, read the exit code, read stderr. Anything that asserts on the
script's source text instead would be a source-string test, which let an
eleven-release outage ship green once already.

Every gate here is checked in both directions. A test that only ever sees the
passing case cannot distinguish a working guard from one that always exits 0.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOC_REFS = REPO / "scripts" / "assert-doc-refs.sh"


def _run(script: Path, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script)], cwd=cwd, capture_output=True, text=True, check=False
    )


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)


def test_doc_refs_passes_on_the_current_tree() -> None:
    """SC-3.2: the tree is clean today, so no baseline file is needed.

    A failure here means the extraction is wrong, not that the docs drifted.
    """
    result = _run(DOC_REFS, REPO)
    assert result.returncode == 0, result.stderr


def test_doc_refs_fails_on_a_broken_citation(tmp_path: Path) -> None:
    """SC-3.1: a cited path that does not resolve fails the guard."""
    _init_repo(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "CLAUDE.md").write_text("See `src/does_not_exist.py` for details.\n")

    result = _run(DOC_REFS, tmp_path)

    assert result.returncode == 1
    assert "does_not_exist.py" in result.stderr


def test_doc_refs_strips_line_numbers_and_fragments(tmp_path: Path) -> None:
    """SC-3.3: `config.py:42` resolves once the suffix is removed.

    Without this, every file:line citation in the instruction layer would be a
    false positive and the guard would be switched off within a day.
    """
    _init_repo(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "config.py").write_text("x = 1\n")
    (tmp_path / "CLAUDE.md").write_text("See `src/config.py:42` and `src/config.py#section`.\n")

    result = _run(DOC_REFS, tmp_path)

    assert result.returncode == 0, result.stderr


def test_doc_refs_is_not_blinded_by_a_global_gitignore(tmp_path: Path) -> None:
    """SC-3.4: a developer's global excludes must not hide drift.

    The guard passes `-c core.excludesfile=/dev/null` for exactly this. No global
    file is set on this machine today, so this guards against a future one.
    """
    _init_repo(tmp_path)
    excludes = tmp_path / "global_ignore"
    excludes.write_text("*.py\n")
    subprocess.run(["git", "config", "core.excludesfile", str(excludes)], cwd=tmp_path, check=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "CLAUDE.md").write_text("See `src/does_not_exist.py`.\n")

    result = _run(DOC_REFS, tmp_path)

    assert result.returncode == 1, "the global excludes file hid the drift"


def test_doc_refs_exits_2_outside_a_repository(tmp_path: Path) -> None:
    """Cannot-run must block, not pass.

    A gate that cannot run is not a gate that passed. The opposite decision is on
    record going wrong: a guard that failed open did nothing and reported nothing.
    """
    result = _run(DOC_REFS, tmp_path)

    assert result.returncode == 2


def test_adr_index_parity_fails_when_a_file_has_no_row(tmp_path: Path) -> None:
    """SC-3.5, direction one: an ADR the index does not list."""
    _init_repo(tmp_path)
    adr = tmp_path / "docs" / "architecture" / "adr"
    adr.mkdir(parents=True)
    (tmp_path / "CLAUDE.md").write_text("nothing cited\n")
    (adr / "README.md").write_text("| [001](001-a.md) | A | x |\n")
    (adr / "001-a.md").touch()
    (adr / "002-b.md").touch()

    result = _run(DOC_REFS, tmp_path)

    assert result.returncode == 1
    assert "002" in result.stderr


def test_adr_index_parity_fails_when_a_row_has_no_file(tmp_path: Path) -> None:
    """SC-3.5, direction two: an index row pointing at nothing.

    Both directions matter. Checking only one would have missed the ADR-018
    collision, where a concurrent session took a number that a new file would
    then have overwritten.
    """
    _init_repo(tmp_path)
    adr = tmp_path / "docs" / "architecture" / "adr"
    adr.mkdir(parents=True)
    (tmp_path / "CLAUDE.md").write_text("nothing cited\n")
    (adr / "README.md").write_text("| [001](001-a.md) | A | x |\n| [003](003-c.md) | C | x |\n")
    (adr / "001-a.md").touch()

    result = _run(DOC_REFS, tmp_path)

    assert result.returncode == 1
    assert "003" in result.stderr


def test_adr_index_parity_passes_when_they_match(tmp_path: Path) -> None:
    """The control for the two tests above.

    Without it, a guard that always exited 1 would pass both parity tests.
    """
    _init_repo(tmp_path)
    adr = tmp_path / "docs" / "architecture" / "adr"
    adr.mkdir(parents=True)
    (tmp_path / "CLAUDE.md").write_text("nothing cited\n")
    (adr / "README.md").write_text("| [001](001-a.md) | A | x |\n")
    (adr / "001-a.md").touch()

    result = _run(DOC_REFS, tmp_path)

    assert result.returncode == 0, result.stderr
