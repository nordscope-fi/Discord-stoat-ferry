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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

REPO = Path(__file__).resolve().parent.parent
DOC_REFS = REPO / "scripts" / "assert-doc-refs.sh"
DEFERRALS = REPO / "scripts" / "check-deferrals.sh"
FIELDS = REPO / "scripts" / "check-deferral-fields.sh"

FOUR_FIELDS = (
    "## Deferral Justification\n"
    "Why not now: blocked on the upstream release\n"
    "Cost comparison: 2h now, 2h later, no asymmetry\n"
    "Owner: Peter Sterkenburg\n"
    "Trigger: when the upstream release lands\n"
)


def _run(script: Path, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script)], cwd=cwd, capture_output=True, text=True, check=False
    )


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)


def test_doc_refs_matches_the_checkout_it_is_run_in() -> None:
    """SC-3.2, and the branch's own central fact, asserted rather than assumed.

    The correct answer differs by checkout, and both answers are real assertions:

    - **Locally** the instruction layer is present, the tree has no drift, so the
      guard must pass. A failure here means the extraction broke, not that the
      docs drifted.
    - **In CI** the instruction layer does not exist. `CLAUDE.md` and
      `.claude/rules/` are gitignored, so `actions/checkout` never fetches them.
      The guard must then refuse with exit 2, because checking nothing is not
      passing.

    This is not a skip dressed as a test. Each branch asserts the behaviour that
    is correct for that environment, and the CI branch is the one that caught the
    first version of this test asserting exit 0 unconditionally: it passed
    locally and failed on all three CI Python versions, which is precisely the
    local-only assumption this whole branch exists to make visible.
    """
    instruction_layer_present = (REPO / "CLAUDE.md").exists() or (
        REPO / ".claude" / "rules"
    ).is_dir()

    result = _run(DOC_REFS, REPO)

    if instruction_layer_present:
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode == 2, (
            "with no instruction layer to read, the guard must refuse rather than "
            f"report clean. Got {result.returncode}: {result.stderr}"
        )
        assert "Checking nothing is not passing" in result.stderr


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


def test_adr_index_parity_fails_when_the_row_format_changes(tmp_path: Path) -> None:
    """The row->file direction must not silently stop checking.

    That direction depends on the index's `| [NNN]` table format. If the format
    changes, the extraction finds nothing and checks nothing, which looks exactly
    like a pass. Found during chunk 1's review, by testing the guard rather than
    reading it: the second-opinion model returned zero findings on this file.
    """
    _init_repo(tmp_path)
    adr = tmp_path / "docs" / "architecture" / "adr"
    adr.mkdir(parents=True)
    (tmp_path / "CLAUDE.md").write_text("nothing cited\n")
    (adr / "README.md").write_text("- [001](001-a.md) A\n")  # not the table format
    (adr / "001-a.md").touch()

    result = _run(DOC_REFS, tmp_path)

    assert result.returncode == 1
    assert "stopped checking" in result.stderr


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


# --- the deferral sweep -------------------------------------------------------
#
# Each case builds its own throwaway repository rather than switching branches in
# a shared one. A `git checkout -b` here trips branch-guard, which runs its status
# check in its own working directory and so reports the Ferry tree's state rather
# than the temp repo's.


def _repo_with_change(path: Path, content: str, *, with_checker: bool = True) -> Path:
    """Seed a repo, then add one commit introducing `content`.

    The sweep resolves its four-field checker relative to its own location, so
    testing the missing-checker path means copying the sweep somewhere the
    checker is absent, not deleting it from the repo under test. An earlier
    version of this test deleted the repo's copy and proved nothing, because the
    sweep was still finding the real one beside itself.
    """
    scripts = path / "scripts"
    scripts.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (scripts / DEFERRALS.name).write_bytes(DEFERRALS.read_bytes())
    (scripts / DEFERRALS.name).chmod(0o755)
    if with_checker:
        (scripts / FIELDS.name).write_bytes(FIELDS.read_bytes())
        (scripts / FIELDS.name).chmod(0o755)
    (path / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=path, check=True)
    (path / "notes.md").write_text(content)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "change"], cwd=path, check=True)
    return path


def _sweep(cwd: Path, base: str = "HEAD~1") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "scripts/check-deferrals.sh", base],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def test_sweep_fires_on_an_unjustified_deferral(tmp_path: Path) -> None:
    """SC-2.1. Demonstrated against real history too: 5 of 110 merged PR bodies."""
    repo = _repo_with_change(tmp_path, "This is future work, tracked separately.\n")

    result = _sweep(repo)

    assert result.returncode == 1
    assert "future work" in result.stderr.lower()


def test_sweep_accepts_a_four_field_justification(tmp_path: Path) -> None:
    """SC-2.2: a real justification in the same diff accounts for the match."""
    repo = _repo_with_change(tmp_path, f"This is future work.\n\n{FOUR_FIELDS}")

    result = _sweep(repo)

    assert result.returncode == 0, result.stderr


def test_sweep_rejects_a_justification_heading_with_no_fields(tmp_path: Path) -> None:
    """SC-2.3: the TBD hole.

    Checking for the heading alone let a two-line block silence every match in the
    diff, recreating the exact failure this gate closes. The block is piped into
    the one place the four fields are defined.
    """
    repo = _repo_with_change(tmp_path, "This is future work.\n\n## Deferral Justification\nTBD\n")

    result = _sweep(repo)

    assert result.returncode == 1


def test_sweep_fails_closed_when_the_field_checker_is_missing(tmp_path: Path) -> None:
    """A justification it cannot check must not be treated as valid.

    Failing open here would silence every match whenever the checker went missing,
    which is how a guard ends up doing nothing while reporting nothing.
    """
    repo = _repo_with_change(tmp_path, f"This is future work.\n\n{FOUR_FIELDS}", with_checker=False)

    result = _sweep(repo)

    assert result.returncode == 2
    assert "cannot be checked" in result.stderr


def test_sweep_ignores_a_phrase_on_an_untouched_line(tmp_path: Path) -> None:
    """SC-2.4: added lines only, never the whole diff.

    Scanning the whole diff reported text three lines from an edit that nobody had
    written. Harmless inside a skill, a blocked pull request once it runs in CI.
    """
    scripts = tmp_path / "scripts"
    scripts.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (scripts / DEFERRALS.name).write_bytes(DEFERRALS.read_bytes())
    (scripts / DEFERRALS.name).chmod(0o755)
    (tmp_path / "notes.md").write_text("This is future work.\nline2\nline3\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=tmp_path, check=True)
    (tmp_path / "notes.md").write_text("This is future work.\nline2\nEDITED\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "edit"], cwd=tmp_path, check=True)

    result = _sweep(tmp_path)

    assert result.returncode == 0, result.stderr


def test_sweep_exits_2_on_a_missing_base_ref(tmp_path: Path) -> None:
    """SC-2.5: cannot-run blocks, and the error names the remedy.

    A shallow CI checkout is the real case, and `fetch-depth` in the message is
    what turns a red build into a one-line fix.
    """
    repo = _repo_with_change(tmp_path, "clean\n")

    result = _sweep(repo, base="origin/definitely-not-a-ref")

    assert result.returncode == 2
    assert "fetch-depth" in result.stderr


def test_sweep_announces_a_missing_gh_rather_than_passing_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SC-2.6: the one degradation, and it must be audible.

    The PR body is the source that matters: 0 of 60 Ferry commits match the
    pattern while 5 of 110 PR bodies do, because rebase merges keep the body out
    of git. Degrading silently would reproduce the blind spot this source closes.
    """
    repo = _repo_with_change(tmp_path, "clean\n")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    result = _sweep(repo)

    assert result.returncode == 0, result.stderr
    assert "PR body: unavailable" in result.stderr


# --- the CI wiring ------------------------------------------------------------
#
# Asserting on the workflow's text is appropriate here: a workflow file IS text,
# and there is no behaviour to exercise locally. This is not the source-string
# antipattern, which is about asserting on code text as a proxy for what the code
# does.

CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"


def _gates_job() -> str:
    """The `gates:` job block, comments stripped, header to next job or EOF.

    Comments are removed so these assertions test configuration rather than
    prose. The first version of this helper kept them and the matrix assertion
    failed against the word "matrix" inside its own explanatory comment.
    """
    lines = CI_WORKFLOW.read_text().splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.strip() == "gates:")
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i] and not lines[i].startswith("    ") and lines[i].startswith("  "):
            end = i
            break
    return "\n".join(ln for ln in lines[start:end] if not ln.strip().startswith("#"))


def test_ci_runs_the_deferral_sweep() -> None:
    """SC-2.7: the sweep is actually wired into CI, not merely written."""
    assert "check-deferrals.sh" in _gates_job()


def test_ci_gates_job_sets_gh_token() -> None:
    """Without it the sweep degrades on EVERY CI run while still passing.

    gh is preinstalled on ubuntu-latest but does not authenticate on its own, so
    the pull request body, the one source that finds Ferry's real deferrals, would
    never be read. It would keep working locally, where a developer's gh session
    is already authenticated, which is the worst failure shape available.
    """
    job = _gates_job()
    assert "GH_TOKEN" in job
    assert "pull-requests: read" in job


def test_ci_gates_job_fetches_full_history() -> None:
    """The sweep resolves origin/main; a shallow clone makes it exit 2."""
    assert "fetch-depth: 0" in _gates_job()


def test_ci_gates_job_is_not_in_the_python_matrix() -> None:
    """One run per pull request, not three.

    lint-and-test runs a three-version matrix. A step there would run the sweep
    three times and make three gh calls for one answer.
    """
    job = _gates_job()
    assert "strategy:" not in job
    assert "matrix" not in job


# --- both gates must be anchored to the repo root ------------------------------
#
# Found by the whole-branch review, which read the three scripts side by side.
# Per-chunk review could not see it: each script was correct in isolation and
# they disagreed with each other. assert-doc-refs.sh trusted cwd, so from a
# subdirectory it found none of its inputs, checked nothing, and printed
# "clean" with exit 0. A check that stopped checking without failing, inside the
# script written to stop checks doing that.


def test_doc_refs_still_catches_drift_from_a_subdirectory(tmp_path: Path) -> None:
    """Running from a subdirectory must not turn a real failure into a pass."""
    _init_repo(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "CLAUDE.md").write_text("See `src/does_not_exist.py`.\n")

    from_root = _run(DOC_REFS, tmp_path)
    from_sub = _run(DOC_REFS, tmp_path / "src")

    assert from_root.returncode == 1
    assert from_sub.returncode == 1, "a subdirectory invocation reported a false pass"
    assert "does_not_exist.py" in from_sub.stderr


def test_doc_refs_refuses_when_it_finds_no_input_documents(tmp_path: Path) -> None:
    """Checking nothing is not passing.

    Belt two: even anchored at the root, a tree with no CLAUDE.md and no rules
    has nothing to check, and reporting clean there would be a lie.
    """
    _init_repo(tmp_path)

    result = _run(DOC_REFS, tmp_path)

    assert result.returncode == 2
    assert "Checking nothing is not passing" in result.stderr


def test_sweep_still_fires_from_a_subdirectory(tmp_path: Path) -> None:
    """The sweep scopes its diff with `-- .`, so cwd could silently narrow it."""
    repo = _repo_with_change(tmp_path, "This is future work, tracked separately.\n")
    (repo / "sub").mkdir()

    from_root = subprocess.run(
        ["bash", "scripts/check-deferrals.sh", "HEAD~1"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    from_sub = subprocess.run(
        ["bash", "../scripts/check-deferrals.sh", "HEAD~1"],
        cwd=repo / "sub",
        capture_output=True,
        text=True,
        check=False,
    )

    assert from_root.returncode == 1
    assert from_sub.returncode == 1, "a subdirectory invocation missed the deferral"


def test_sweep_finds_its_field_checker_from_a_subdirectory(tmp_path: Path) -> None:
    """A relative $0 must be resolved before the script changes directory.

    Regression guard. The repo-root anchoring introduced this: after `cd "$ROOT"`,
    `$(dirname "../scripts/check-deferrals.sh")` no longer points at the scripts
    directory, and the checker resolved to `/check-deferral-fields.sh`. A
    legitimately justified deferral was then rejected with exit 2.

    The existing subdirectory test does not cover this path, because without a
    justification block the checker is never consulted.
    """
    repo = _repo_with_change(tmp_path, f"This is future work.\n\n{FOUR_FIELDS}")
    (repo / "sub").mkdir()

    from_root = subprocess.run(
        ["bash", "scripts/check-deferrals.sh", "HEAD~1"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    from_sub = subprocess.run(
        ["bash", "../scripts/check-deferrals.sh", "HEAD~1"],
        cwd=repo / "sub",
        capture_output=True,
        text=True,
        check=False,
    )

    assert from_root.returncode == 0, from_root.stderr
    assert from_sub.returncode == 0, (
        "the field checker was not found from a subdirectory: " + from_sub.stderr
    )
