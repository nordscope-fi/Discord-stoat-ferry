from __future__ import annotations

import hashlib
import json
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="Developer tooling is covered by the Linux CI jobs (#174)"
)

REPO = Path(__file__).resolve().parent.parent
MODULE = REPO / "scripts/agent-compat/brainstorm-evidence.mjs"
NODE = shutil.which("node")
STATE_ROOT = Path("docs/plans/.brainstorm-evidence")
LEDGER_PATH = STATE_ROOT / "ledger.json"
PROMPT_MARKERS = STATE_ROOT / "prompt-markers"
PENDING_RECEIPTS = STATE_ROOT / "receipts/pending"
COMPLETED_RECEIPTS = STATE_ROOT / "receipts/completed"


def run_hook(root: Path, host: str, payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
    assert NODE is not None
    return subprocess.run(
        [NODE, str(MODULE), "--host", host, "--root", str(root)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
    )


def read_ledger(root: Path) -> dict[str, object]:
    return json.loads((root / LEDGER_PATH).read_text())


def post_edit(root: Path, relative_path: str, content: str) -> None:
    target = root / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    result = run_hook(
        root,
        "claude",
        {
            "hook_event_name": "PostToolUse",
            "session_id": "session-1",
            "tool_use_id": "edit-1",
            "tool_name": "Write",
            "tool_input": {"file_path": str(target)},
            "tool_response": {"status": "ok"},
        },
    )
    assert result.returncode == 0, result.stderr


def submit_prompt(
    root: Path, prompt: str, prompt_id: str = "prompt-1"
) -> subprocess.CompletedProcess[str]:
    result = run_hook(
        root,
        "claude",
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-1",
            "prompt_id": prompt_id,
            "prompt": prompt,
        },
    )
    assert result.returncode == 0, result.stderr
    return result


def stop_turn(
    root: Path, message: str, *, stop_hook_active: bool = False
) -> subprocess.CompletedProcess[str]:
    result = run_hook(
        root,
        "claude",
        {
            "hook_event_name": "Stop",
            "session_id": "session-1",
            "last_assistant_message": message,
            "stop_hook_active": stop_hook_active,
        },
    )
    assert result.returncode == 0, result.stderr
    return result


def submit_prompt_for_host(root: Path, host: str, prompt: str) -> None:
    payload: dict[str, object] = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "session-1",
        "prompt": prompt,
    }
    if host == "claude":
        payload["prompt_id"] = "turn-1"
    elif host == "codex":
        payload["turn_id"] = "turn-1"
    # Qwen sends neither field; the module derives its own turn identity.
    result = run_hook(root, host, payload)
    assert result.returncode == 0, result.stderr


def stop_turn_for_host(root: Path, host: str, message: str) -> subprocess.CompletedProcess[str]:
    payload: dict[str, object] = {
        "hook_event_name": "Stop",
        "session_id": "session-1",
        "last_assistant_message": message,
        "stop_hook_active": False,
    }
    if host == "codex":
        payload["turn_id"] = "turn-1"
    result = run_hook(root, host, payload)
    assert result.returncode == 0, result.stderr
    return result


def before_tool(
    root: Path,
    tool_name: str,
    tool_input: dict[str, object],
    *,
    tool_use_id: str = "source-1",
    host: str = "claude",
) -> subprocess.CompletedProcess[str]:
    payload: dict[str, object] = {
        "hook_event_name": "PreToolUse",
        "session_id": "session-1",
        "tool_use_id": tool_use_id,
        "tool_name": tool_name,
        "tool_input": tool_input,
    }
    if host != "qwen":
        payload["turn_id"] = "turn-1"
    result = run_hook(root, host, payload)
    assert result.returncode == 0, result.stderr
    return result


def after_tool(
    root: Path,
    tool_name: str,
    tool_input: dict[str, object],
    tool_response: object,
    *,
    tool_use_id: str = "source-1",
    host: str = "claude",
    event_name: str = "PostToolUse",
) -> subprocess.CompletedProcess[str]:
    payload: dict[str, object] = {
        "hook_event_name": event_name,
        "session_id": "session-1",
        "tool_use_id": tool_use_id,
        "tool_name": tool_name,
        "tool_input": tool_input,
        "tool_response": tool_response,
    }
    if host != "qwen":
        payload["turn_id"] = "turn-1"
    result = run_hook(root, host, payload)
    assert result.returncode == 0, result.stderr
    return result


def claude_bash_response(
    stdout: str = "", *, interrupted: bool = False, background_task_id: str | None = None
) -> dict[str, object]:
    # The Bash tool_response Claude Code sends on PostToolUse. It has no exit code: the
    # event fires only for a command that exited 0 (code.claude.com/docs/en/agent-sdk/python).
    response: dict[str, object] = {
        "stdout": stdout,
        "stderr": "",
        "interrupted": interrupted,
        "isImage": False,
    }
    if background_task_id is not None:
        response["backgroundTaskId"] = background_task_id
    return response


def claude_bash_failure(
    root: Path,
    tool_input: dict[str, object],
    error: str,
    *,
    tool_use_id: str,
    is_interrupt: bool = False,
) -> None:
    # A non-zero exit fires PostToolUseFailure with a top-level error whose first line is
    # "Exit code N", and no tool_response (code.claude.com/docs/en/hooks).
    payload: dict[str, object] = {
        "hook_event_name": "PostToolUseFailure",
        "session_id": "session-1",
        "turn_id": "turn-1",
        "tool_use_id": tool_use_id,
        "tool_name": "Bash",
        "tool_input": tool_input,
        "error": error,
        "is_interrupt": is_interrupt,
    }
    result = run_hook(root, "claude", payload)
    assert result.returncode == 0, result.stderr


def active_checkout(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "checkout"
    root.mkdir()
    source = root / "docs/reference.md"
    source.parent.mkdir(parents=True)
    source.write_text("# Reference\n\nAtomic rename replaces a file.\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "docs/reference.md"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Ferry Test",
            "-c",
            "user.email=ferry@example.invalid",
            "commit",
            "-qm",
            "seed",
        ],
        check=True,
    )
    post_edit(root, "docs/plans/specs/feature.md", "# Requirements\n")
    submit_prompt(root, "continue")
    return root, source


def receipt_files(root: Path, relative: Path) -> list[Path]:
    directory = root / relative
    return sorted(directory.iterdir()) if directory.exists() else []


def write_ledger(root: Path, ledger: dict[str, object]) -> None:
    (root / LEDGER_PATH).write_text(json.dumps(ledger, indent=2) + "\n")


def add_challenge(root: Path, challenge: dict[str, object]) -> None:
    ledger = read_ledger(root)
    challenges = list(ledger.get("alternative_challenges", []))
    challenges.append(challenge)
    ledger["alternative_challenges"] = challenges
    write_ledger(root, ledger)


def receipt_digest(receipt: dict[str, object]) -> str:
    content = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(content.encode()).hexdigest()


def test_specification_edit_prepares_and_matching_design_edit_closes(tmp_path: Path) -> None:
    root = tmp_path / "checkout"
    root.mkdir()
    requirements = "docs/plans/specs/2026-09-02-feature.md"
    post_edit(root, requirements, "# Requirements\n\nEvidence is required.\n")

    prepared = read_ledger(root)
    assert prepared["schema_version"] == 1
    assert prepared["state"] == "prepared"
    assert prepared["requirements_path"] == requirements
    assert (
        prepared["requirements_sha256"]
        == hashlib.sha256((root / requirements).read_bytes()).hexdigest()
    )
    assert prepared["required_next_step"] == "brainstorm"

    submit_prompt(root, "ok")
    active = read_ledger(root)
    assert active["state"] == "active"
    assert active["generation"] == prepared["generation"]
    assert active["activation"] == {
        "host": "claude",
        "session_id": "session-1",
        "turn_id": "prompt-1",
        "kind": "continue",
    }

    post_edit(
        root,
        "docs/plans/designs/2026-09-02-feature.md",
        "# Approved design\n",
    )
    closed = read_ledger(root)
    assert closed["state"] == "closed"
    assert closed["design_path"] == "docs/plans/designs/2026-09-02-feature.md"
    assert (
        closed["design_sha256"]
        == hashlib.sha256(
            (root / "docs/plans/designs/2026-09-02-feature.md").read_bytes()
        ).hexdigest()
    )


def test_unrelated_prompt_leaves_prepared_generation_unchanged(tmp_path: Path) -> None:
    root = tmp_path / "checkout"
    root.mkdir()
    post_edit(root, "docs/plans/specs/feature.md", "# Requirements\n")
    prepared = read_ledger(root)

    submit_prompt(root, "Recommend a database for an unrelated service")

    current = read_ledger(root)
    assert current["state"] == "prepared"
    assert current["generation"] == prepared["generation"]
    assert "activation" not in current


@pytest.mark.parametrize("activate_first", [False, True])
def test_cancelled_generation_requires_explicit_restart(
    tmp_path: Path, activate_first: bool
) -> None:
    root = tmp_path / "checkout"
    root.mkdir()
    post_edit(root, "docs/plans/specs/feature.md", "# Requirements\n")
    if activate_first:
        submit_prompt(root, "continue")

    submit_prompt(root, "/df-brainstorm cancel", "prompt-cancel")
    cancelled = read_ledger(root)
    assert cancelled["state"] == "cancelled"

    submit_prompt(root, "yes", "prompt-short")
    still_cancelled = read_ledger(root)
    assert still_cancelled["state"] == "cancelled"
    assert still_cancelled["generation"] == cancelled["generation"]

    submit_prompt(root, "$df-brainstorm", "prompt-restart")
    restarted = read_ledger(root)
    assert restarted["state"] == "active"
    assert restarted["generation"] != cancelled["generation"]
    assert restarted["activation"]["kind"] == "invoke"


def test_suspended_record_from_an_earlier_version_restarts_on_invocation(
    tmp_path: Path,
) -> None:
    # v2.41.11 and earlier could leave a record suspended. Nothing writes that state now,
    # but an explicit invocation must still restart it, or it would have no way out.
    root = tmp_path / "checkout"
    root.mkdir()
    post_edit(root, "docs/plans/specs/feature.md", "# Requirements\n")
    submit_prompt(root, "continue")
    ledger = read_ledger(root)
    ledger["state"] = "suspended"
    ledger.pop("activation")
    write_ledger(root, ledger)

    submit_prompt(root, "continue", "prompt-short")
    assert read_ledger(root)["state"] == "suspended"

    submit_prompt(root, "/df-brainstorm", "prompt-restart")
    restarted = read_ledger(root)
    assert restarted["state"] == "active"
    assert restarted["generation"] != ledger["generation"]


def test_changed_requirements_start_new_prepared_generation(tmp_path: Path) -> None:
    root = tmp_path / "checkout"
    root.mkdir()
    requirements = "docs/plans/specs/feature.md"
    post_edit(root, requirements, "# Requirements\n\nFirst version.\n")
    submit_prompt(root, "/df-brainstorm")
    first = read_ledger(root)

    post_edit(root, requirements, "# Requirements\n\nSecond version.\n")
    refreshed = read_ledger(root)

    assert refreshed["state"] == "prepared"
    assert refreshed["generation"] != first["generation"]
    assert refreshed["requirements_sha256"] != first["requirements_sha256"]
    assert "activation" not in refreshed


def test_saved_state_is_private_and_isolated_by_worktree(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    relative = "docs/plans/specs/feature.md"

    post_edit(first_root, relative, "# First requirements\n")
    post_edit(second_root, relative, "# Second requirements\n")
    first = read_ledger(first_root)
    second = read_ledger(second_root)

    assert first["generation"] != second["generation"]
    assert first["requirements_sha256"] != second["requirements_sha256"]
    assert stat.S_IMODE((first_root / LEDGER_PATH).stat().st_mode) == 0o600
    assert stat.S_IMODE((second_root / LEDGER_PATH).stat().st_mode) == 0o600


def test_submitted_prompt_marker_replaces_previous_without_prompt_text(tmp_path: Path) -> None:
    root = tmp_path / "checkout"
    root.mkdir()

    submit_prompt(root, "Sensitive unrelated request", "prompt-first")
    submit_prompt(root, "Another unrelated request", "prompt-second")

    markers = list((root / PROMPT_MARKERS).iterdir())
    assert len(markers) == 1
    marker = json.loads(markers[0].read_text())
    assert marker == {
        "schema_version": 1,
        "host": "claude",
        "session_id": "session-1",
        "turn_id": "prompt-second",
        "classification": "other",
    }
    assert "Sensitive" not in markers[0].read_text()
    assert "Another" not in markers[0].read_text()
    assert stat.S_IMODE(markers[0].stat().st_mode) == 0o600


def test_active_recommendation_blocks_with_matching_marker(tmp_path: Path) -> None:
    root = tmp_path / "checkout"
    root.mkdir()
    post_edit(root, "docs/plans/specs/feature.md", "# Requirements\n")
    submit_prompt(root, "continue")

    result = stop_turn(root, "## Recommendation\n\nUse the selected approach.")

    assert json.loads(result.stdout) == {
        "decision": "block",
        "reason": "Brainstorm recommendation is missing required evidence: approaches.",
    }
    assert read_ledger(root)["state"] == "active"
    assert list((root / PROMPT_MARKERS).iterdir()) == []


def test_exploration_response_allows_and_consumes_marker(tmp_path: Path) -> None:
    root = tmp_path / "checkout"
    root.mkdir()
    post_edit(root, "docs/plans/specs/feature.md", "# Requirements\n")
    submit_prompt(root, "continue")

    result = stop_turn(root, "Option A uses a file. Option B uses a database.")

    assert result.stdout == ""
    assert read_ledger(root)["state"] == "active"
    assert list((root / PROMPT_MARKERS).iterdir()) == []


@pytest.mark.parametrize("host", ["claude", "codex", "qwen"])
def test_unrelated_active_turn_does_not_trigger_recommendation_gate(
    tmp_path: Path, host: str
) -> None:
    root = tmp_path / "checkout"
    root.mkdir()
    post_edit(root, "docs/plans/specs/feature.md", "# Requirements\n")
    submit_prompt_for_host(root, host, "continue")
    stop_turn_for_host(root, host, "I am ready for the next section.")
    submit_prompt_for_host(root, host, "Recommend a restaurant")

    result = stop_turn_for_host(root, host, "I recommend a restaurant.")

    assert result.stdout == ""
    assert read_ledger(root)["state"] == "active"
    assert list((root / PROMPT_MARKERS).iterdir()) == []


@pytest.mark.parametrize("marker_state", ["missing", "malformed"])
def test_missing_prompt_marker_keeps_generation_and_clears_pending_receipts(
    tmp_path: Path, marker_state: str
) -> None:
    # A stop with no matching marker (seen after a hook ended a turn and after a Claude "!"
    # command) passes unchecked, but the brainstorm stays active on the same generation,
    # so its completed evidence still counts and the next typed turn is checked again.
    root = tmp_path / "checkout"
    root.mkdir()
    post_edit(root, "docs/plans/specs/feature.md", "# Requirements\n")
    submit_prompt(root, "continue")
    generation = read_ledger(root)["generation"]
    marker = next((root / PROMPT_MARKERS).iterdir())
    if marker_state == "missing":
        marker.unlink()
    else:
        marker.write_text("{")
    pending = root / PENDING_RECEIPTS / "call.json"
    pending.parent.mkdir(parents=True)
    pending.write_text('{"pending":true}\n')

    result = stop_turn(root, "## Recommendation\n\nUse the selected approach.")

    assert result.stdout == ""
    ledger = read_ledger(root)
    assert ledger["state"] == "active"
    assert ledger["generation"] == generation
    assert list((root / PENDING_RECEIPTS).iterdir()) == []


def test_typed_turn_after_unmarked_turn_still_checks_recommendation(tmp_path: Path) -> None:
    root = tmp_path / "checkout"
    root.mkdir()
    post_edit(root, "docs/plans/specs/feature.md", "# Requirements\n")
    submit_prompt(root, "continue")
    stop_turn(root, "Research so far.")
    assert stop_turn(root, "Merged.").stdout == ""

    submit_prompt(root, "continue", prompt_id="prompt-2")
    result = stop_turn(root, "## Recommendation\n\nUse the selected approach.")

    assert json.loads(result.stdout)["decision"] == "block"


def test_inactive_workflow_and_stop_loop_allow_recommendation(tmp_path: Path) -> None:
    absent_root = tmp_path / "absent"
    absent_root.mkdir()
    assert stop_turn(absent_root, "I recommend the file approach.").stdout == ""

    prepared_root = tmp_path / "prepared"
    prepared_root.mkdir()
    post_edit(prepared_root, "docs/plans/specs/feature.md", "# Requirements\n")
    assert stop_turn(prepared_root, "I recommend the file approach.").stdout == ""
    assert read_ledger(prepared_root)["state"] == "prepared"

    submit_prompt(prepared_root, "continue")
    loop_result = stop_turn(
        prepared_root,
        "## Recommendation\n\nUse the selected approach.",
        stop_hook_active=True,
    )
    assert loop_result.stdout == ""
    assert read_ledger(prepared_root)["state"] == "active"


def test_source_receipt_read_completes_without_raw_text(tmp_path: Path) -> None:
    root, source = active_checkout(tmp_path)
    tool_input = {"file_path": str(source)}

    before_tool(root, "Read", tool_input)
    pending = receipt_files(root, PENDING_RECEIPTS)
    assert len(pending) == 1
    assert receipt_files(root, COMPLETED_RECEIPTS) == []

    response = "Atomic rename replaces a file.\npst_abcdefghijklmnopqrstuvwxyz0123456789\n"
    after_tool(root, "Read", tool_input, response)

    assert receipt_files(root, PENDING_RECEIPTS) == []
    completed = receipt_files(root, COMPLETED_RECEIPTS)
    assert len(completed) == 1
    receipt = json.loads(completed[0].read_text())
    assert receipt["kind"] == "source"
    assert receipt["status"] == "completed"
    assert receipt["source"] == {
        "type": "repository",
        "locator": "docs/reference.md",
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    }
    assert hashlib.sha256(b"Atomic rename replaces a file.").hexdigest() in receipt["line_hashes"]
    serialized = completed[0].read_text()
    assert "Atomic rename replaces a file" not in serialized
    assert "pst_" not in serialized
    assert str(root) not in serialized
    assert stat.S_IMODE(completed[0].stat().st_mode) == 0o600


def test_source_receipt_result_without_matching_before_event_is_ignored(tmp_path: Path) -> None:
    root, source = active_checkout(tmp_path)

    after_tool(root, "Read", {"file_path": str(source)}, "Atomic rename replaces a file.")

    assert receipt_files(root, PENDING_RECEIPTS) == []
    assert receipt_files(root, COMPLETED_RECEIPTS) == []


@pytest.mark.parametrize(
    ("tool_name", "tool_input"),
    [
        ("Bash", {"command": "printf 'Atomic rename replaces a file.\\n'"}),
        ("Bash", {"command": "echo Atomic rename replaces a file."}),
        ("Bash", {"command": "python -c 'print(1)'"}),
        ("Bash", {"command": "rg -n Atomic docs/reference.md | head -1"}),
        ("Bash", {"command": "rg -n Atomic docs/reference.md > result.txt"}),
        ("Bash", {"command": "VALUE=Atomic rg -n Atomic docs/reference.md"}),
        ("Bash", {"command": "rg -n $(printf Atomic) docs/reference.md"}),
        ("WebFetch", {"url": "https://user:password@example.com/guide"}),
        ("WebFetch", {"url": "https://example.com/guide?token=value"}),
        ("WebFetch", {"url": "https://127.0.0.1/guide"}),
    ],
)
def test_output_only_and_unsafe_source_calls_create_no_receipt(
    tmp_path: Path, tool_name: str, tool_input: dict[str, object]
) -> None:
    root, _ = active_checkout(tmp_path)

    before_tool(root, tool_name, tool_input)

    assert receipt_files(root, PENDING_RECEIPTS) == []


def test_source_receipt_rejects_new_or_excluded_repository_source(tmp_path: Path) -> None:
    root, _ = active_checkout(tmp_path)
    new_source = root / "docs/new-reference.md"
    new_source.write_text("Created after activation.\n")
    design = root / "docs/plans/designs/feature.md"
    design.parent.mkdir(parents=True)
    design.write_text("# Current design\n")
    ledger = root / LEDGER_PATH

    for candidate in (new_source, design, ledger, tmp_path / "outside.md"):
        if candidate.name == "outside.md":
            candidate.write_text("Outside.\n")
        before_tool(root, "Read", {"file_path": str(candidate)}, tool_use_id=candidate.name)

    assert receipt_files(root, PENDING_RECEIPTS) == []


@pytest.mark.parametrize(
    ("tool_name", "tool_input", "response", "source_type"),
    [
        (
            "Bash",
            {"command": "rg -n Atomic docs/reference.md"},
            "3:Atomic rename replaces a file.",
            "repository",
        ),
        (
            "Bash",
            {"command": "sed -n '1,3p' docs/reference.md"},
            "# Reference\nAtomic rename replaces a file.",
            "repository",
        ),
        (
            "Bash",
            {"command": "git show HEAD:docs/reference.md"},
            "# Reference\nAtomic rename replaces a file.",
            "git",
        ),
        (
            "Bash",
            {"command": "curl -L https://docs.example.com/guide"},
            "Atomic rename replaces a file.",
            "web",
        ),
        (
            "WebFetch",
            {"url": "https://docs.example.com/guide"},
            "Atomic rename replaces a file.",
            "web",
        ),
        (
            "run_shell_command",
            {"command": "sed -n '1,3p' docs/reference.md"},
            {
                "llmContent": "# Reference\nAtomic rename replaces a file.",
                "returnDisplay": "Output:\n# Reference\nAtomic rename replaces a file.",
            },
            "repository",
        ),
        (
            "mcp__context7__query-docs",
            {"libraryId": "/nodejs/node", "query": "atomic file replacement"},
            {"content": [{"type": "text", "text": "Atomic rename replaces a file."}]},
            "documentation",
        ),
    ],
)
def test_source_receipt_supported_calls_complete_independent_evidence(
    tmp_path: Path,
    tool_name: str,
    tool_input: dict[str, object],
    response: object,
    source_type: str,
) -> None:
    root, _ = active_checkout(tmp_path)

    before_tool(root, tool_name, tool_input)
    after_tool(root, tool_name, tool_input, response)

    completed = receipt_files(root, COMPLETED_RECEIPTS)
    assert len(completed) == 1
    receipt = json.loads(completed[0].read_text())
    assert receipt["source"]["type"] == source_type
    assert len(receipt["line_hashes"]) > 0
    assert "command" not in receipt
    serialized = completed[0].read_text()
    assert '"query":' not in serialized
    if "query" in tool_input:
        assert tool_input["query"] not in serialized


def test_source_receipt_accepts_a_quoted_alternation(tmp_path: Path) -> None:
    root, _ = active_checkout(tmp_path)
    tool_input = {"command": "rg -n 'Atomic|rename' docs/reference.md"}

    before_tool(root, "Bash", tool_input)
    after_tool(root, "Bash", tool_input, "3:Atomic rename replaces a file.")

    completed = receipt_files(root, COMPLETED_RECEIPTS)
    assert len(completed) == 1
    receipt = json.loads(completed[0].read_text())
    assert receipt["source"]["type"] == "repository"
    assert receipt["source"]["locator"] == "docs/reference.md"


def test_source_receipt_keeps_a_mid_word_tilde_in_git_evidence(tmp_path: Path) -> None:
    root, _ = active_checkout(tmp_path)
    (root / "docs/second.md").write_text("Second commit.\n")
    subprocess.run(["git", "-C", str(root), "add", "docs/second.md"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Ferry Test",
            "-c",
            "user.email=ferry@example.invalid",
            "commit",
            "-qm",
            "second",
        ],
        check=True,
    )
    tool_input = {"command": "git show HEAD~1:docs/reference.md"}

    before_tool(root, "Bash", tool_input)
    after_tool(root, "Bash", tool_input, "# Reference\nAtomic rename replaces a file.")

    completed = receipt_files(root, COMPLETED_RECEIPTS)
    assert len(completed) == 1
    receipt = json.loads(completed[0].read_text())
    assert receipt["source"]["type"] == "git"
    assert receipt["source"]["locator"] == "HEAD~1:docs/reference.md"


def test_source_receipt_changed_source_discards_pending_record(tmp_path: Path) -> None:
    root, source = active_checkout(tmp_path)
    tool_input = {"file_path": str(source)}
    before_tool(root, "Read", tool_input)
    source.write_text("Changed after the source call began.\n")

    after_tool(root, "Read", tool_input, "Atomic rename replaces a file.")

    assert receipt_files(root, PENDING_RECEIPTS) == []
    assert receipt_files(root, COMPLETED_RECEIPTS) == []


def test_source_receipt_concurrent_calls_keep_separate_files(tmp_path: Path) -> None:
    root, source = active_checkout(tmp_path)
    tool_input = {"file_path": str(source)}

    before_tool(root, "Read", tool_input, tool_use_id="source-a")
    before_tool(root, "Read", tool_input, tool_use_id="source-b")
    assert len(receipt_files(root, PENDING_RECEIPTS)) == 2

    after_tool(
        root,
        "Read",
        tool_input,
        "Atomic rename replaces a file.",
        tool_use_id="source-b",
    )
    assert len(receipt_files(root, PENDING_RECEIPTS)) == 1
    assert len(receipt_files(root, COMPLETED_RECEIPTS)) == 1


def challenge_case(root: Path, method: str) -> tuple[dict[str, object], str]:
    if method == "test_runner":
        artifact = root / "tests/challenge_test.py"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("def test_small():\n    assert True\n")
        challenge = {
            "id": "challenge-test",
            "alternative_id": "alternative-b",
            "claim": "The alternative cannot pass a focused test.",
            "method": method,
            "falsifying_outcome": {"exit_code": 0},
            "artifacts": ["tests/challenge_test.py"],
        }
        return challenge, "uv run pytest tests/challenge_test.py"

    if method == "prototype_measurement":
        artifact = root / "prototypes/alternative.mjs"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("const answer = 42;\nconsole.log(answer);\n")
        challenge = {
            "id": "challenge-prototype",
            "alternative_id": "alternative-b",
            "claim": "The alternative needs more than 20 substantive lines.",
            "method": method,
            "falsifying_outcome": {"max_substantive_lines": 20},
            "artifacts": ["prototypes/alternative.mjs"],
        }
        return challenge, "node prototypes/alternative.mjs"

    if method == "concrete_comparison":
        selected = root / "prototypes/selected.mjs"
        alternative = root / "prototypes/alternative.mjs"
        selected.parent.mkdir(parents=True, exist_ok=True)
        selected.write_text("const first = 1;\nconst second = 2;\n")
        alternative.write_text("const first = 1;\n")
        challenge = {
            "id": "challenge-comparison",
            "alternative_id": "alternative-b",
            "claim": "The alternative needs more code than the selected approach.",
            "method": method,
            "falsifying_outcome": {
                "metric": "substantive_lines",
                "operator": "lte",
                "left": "alternative",
                "right": "selected",
            },
            "runner_path": "prototypes/alternative.mjs",
            "artifact_sets": {
                "selected": ["prototypes/selected.mjs"],
                "alternative": ["prototypes/alternative.mjs"],
            },
        }
        return challenge, "node prototypes/alternative.mjs"

    source = root / "docs/reference.md"
    source_input = {"file_path": str(source)}
    before_tool(root, "Read", source_input, tool_use_id="cost-source")
    after_tool(
        root,
        "Read",
        source_input,
        "Current work costs 2 units and later work costs 20 units.",
        tool_use_id="cost-source",
    )
    source_receipt = json.loads(receipt_files(root, COMPLETED_RECEIPTS)[0].read_text())
    runner = root / "prototypes/cost.mjs"
    runner.parent.mkdir(parents=True, exist_ok=True)
    runner.write_text("process.exit(0);\n")
    challenge = {
        "id": "challenge-cost",
        "alternative_id": "alternative-b",
        "claim": "Doing the work later does not cost ten times as much.",
        "method": method,
        "falsifying_outcome": {"operator": "gte", "value": 10},
        "runner_path": "prototypes/cost.mjs",
        "artifacts": ["prototypes/cost.mjs"],
        "inputs": [
            {"name": "now", "value": 2, "receipt_id": source_receipt["receipt_id"]},
            {"name": "later", "value": 20, "receipt_id": source_receipt["receipt_id"]},
        ],
        "calculation": {"operator": "divide", "left": "later", "right": "now"},
    }
    return challenge, "node prototypes/cost.mjs"


@pytest.mark.parametrize(
    "method",
    [
        "test_runner",
        "prototype_measurement",
        "concrete_comparison",
        "cost_comparison",
    ],
)
def test_challenge_supported_method_completes_predeclared_receipt(
    tmp_path: Path, method: str
) -> None:
    root, _ = active_checkout(tmp_path)
    challenge, command = challenge_case(root, method)
    add_challenge(root, challenge)
    tool_input = {"command": command}

    before_tool(root, "Bash", tool_input, tool_use_id=f"call-{method}")
    after_tool(
        root,
        "Bash",
        tool_input,
        claude_bash_response("printed values do not count as evidence"),
        tool_use_id=f"call-{method}",
    )

    matching = [
        json.loads(path.read_text())
        for path in receipt_files(root, COMPLETED_RECEIPTS)
        if json.loads(path.read_text()).get("kind") == "challenge"
    ]
    assert len(matching) == 1
    receipt = matching[0]
    assert receipt["challenge_id"] == challenge["id"]
    assert receipt["method"] == method
    assert receipt["falsified"] is True
    assert "printed values" not in json.dumps(receipt)
    assert receipt["integrity_sha256"]


@pytest.mark.parametrize("missing_field", ["claim", "falsifying_outcome"])
def test_challenge_declaration_must_exist_before_execution(
    tmp_path: Path, missing_field: str
) -> None:
    root, _ = active_checkout(tmp_path)
    challenge, command = challenge_case(root, "test_runner")
    challenge.pop(missing_field)
    add_challenge(root, challenge)

    before_tool(root, "Bash", {"command": command}, tool_use_id="missing-declaration")

    assert receipt_files(root, PENDING_RECEIPTS) == []


def test_challenge_changed_declaration_before_result_discards_receipt(tmp_path: Path) -> None:
    root, _ = active_checkout(tmp_path)
    challenge, command = challenge_case(root, "test_runner")
    add_challenge(root, challenge)
    tool_input = {"command": command}
    before_tool(root, "Bash", tool_input, tool_use_id="changed-declaration")

    ledger = read_ledger(root)
    ledger["alternative_challenges"][0]["falsifying_outcome"] = {"exit_code": 1}
    write_ledger(root, ledger)
    after_tool(
        root,
        "Bash",
        tool_input,
        claude_bash_response("1 passed"),
        tool_use_id="changed-declaration",
    )

    assert receipt_files(root, PENDING_RECEIPTS) == []
    assert receipt_files(root, COMPLETED_RECEIPTS) == []


def test_challenge_changed_artifact_before_result_discards_receipt(tmp_path: Path) -> None:
    root, _ = active_checkout(tmp_path)
    challenge, command = challenge_case(root, "prototype_measurement")
    add_challenge(root, challenge)
    tool_input = {"command": command}
    before_tool(root, "Bash", tool_input, tool_use_id="changed-artifact")
    (root / "prototypes/alternative.mjs").write_text("process.exit(1);\n")

    after_tool(
        root,
        "Bash",
        tool_input,
        claude_bash_response(),
        tool_use_id="changed-artifact",
    )

    assert receipt_files(root, PENDING_RECEIPTS) == []
    assert receipt_files(root, COMPLETED_RECEIPTS) == []


def test_challenge_stdout_cannot_forge_declared_result(tmp_path: Path) -> None:
    root, _ = active_checkout(tmp_path)
    challenge, command = challenge_case(root, "test_runner")
    challenge["falsifying_outcome"] = {"exit_code": 1}
    add_challenge(root, challenge)
    tool_input = {"command": command}

    before_tool(root, "Bash", tool_input, tool_use_id="stdout-forgery")
    after_tool(
        root,
        "Bash",
        tool_input,
        claude_bash_response("exit_code: 1; 999 tests failed"),
        tool_use_id="stdout-forgery",
    )

    receipt = json.loads(receipt_files(root, COMPLETED_RECEIPTS)[0].read_text())
    assert receipt["actual_result"] == {"exit_code": 0, "status": "success"}
    assert receipt["falsified"] is False
    assert "999 tests failed" not in json.dumps(receipt)


@pytest.mark.parametrize(
    "command",
    [
        "pytest tests/challenge_test.py",
        "uv run pytest -c /tmp/pytest.ini tests/challenge_test.py",
        "node -e 'process.exit(0)'",
        "uv run python -c 'print(1)'",
        "node prototypes/alternative.mjs | tee result.txt",
    ],
)
def test_challenge_unsupported_runner_creates_no_receipt(tmp_path: Path, command: str) -> None:
    root, _ = active_checkout(tmp_path)
    challenge, _ = challenge_case(root, "test_runner")
    add_challenge(root, challenge)

    before_tool(root, "Bash", {"command": command}, tool_use_id="unsupported-runner")

    assert receipt_files(root, PENDING_RECEIPTS) == []


def test_challenge_claude_failed_tool_event_completes_receipt(tmp_path: Path) -> None:
    root, _ = active_checkout(tmp_path)
    challenge, command = challenge_case(root, "test_runner")
    challenge["falsifying_outcome"] = {"exit_code": 2}
    add_challenge(root, challenge)
    tool_input = {"command": command}
    before_tool(root, "Bash", tool_input, tool_use_id="claude-failed")

    claude_bash_failure(
        root,
        tool_input,
        "Exit code 2\ncaptured log said Exit code 5\n1 failed",
        tool_use_id="claude-failed",
    )

    receipt = json.loads(receipt_files(root, COMPLETED_RECEIPTS)[0].read_text())
    assert receipt["actual_result"] == {"exit_code": 2, "status": "failure"}
    assert receipt["falsified"] is True


@pytest.mark.parametrize(
    "response",
    [
        claude_bash_response("partial output", interrupted=True),
        claude_bash_response("", background_task_id="bash_1"),
        {"stderr": "", "interrupted": False},
        {"stdout": "ok", "stderr": "", "isImage": False},
    ],
    ids=["interrupted", "background", "no-stdout", "no-interrupted-field"],
)
def test_claude_bash_result_without_an_outcome_completes_no_receipt(
    tmp_path: Path, response: dict[str, object]
) -> None:
    # An interrupted or backgrounded command never finished, so it has no exit code; a
    # challenge expecting exit 0 must not pass on one.
    root, _ = active_checkout(tmp_path)
    challenge, command = challenge_case(root, "test_runner")
    add_challenge(root, challenge)
    tool_input = {"command": command}
    before_tool(root, "Bash", tool_input, tool_use_id="claude-no-outcome")
    assert receipt_files(root, PENDING_RECEIPTS)

    after_tool(root, "Bash", tool_input, response, tool_use_id="claude-no-outcome")

    assert receipt_files(root, COMPLETED_RECEIPTS) == []
    assert receipt_files(root, PENDING_RECEIPTS) == []


@pytest.mark.parametrize("background_task_id", [None, ""], ids=["null", "empty"])
def test_claude_bash_result_with_no_background_task_records_exit_zero(
    tmp_path: Path, background_task_id: str | None
) -> None:
    # The SDK types backgroundTaskId as str | None, so a foreground command may carry the
    # key with no id; that must still count as finished, or no receipt could ever complete.
    root, _ = active_checkout(tmp_path)
    challenge, command = challenge_case(root, "test_runner")
    add_challenge(root, challenge)
    tool_input = {"command": command}
    before_tool(root, "Bash", tool_input, tool_use_id="claude-foreground")
    response = claude_bash_response("1 passed")
    response["backgroundTaskId"] = background_task_id

    after_tool(root, "Bash", tool_input, response, tool_use_id="claude-foreground")

    [receipt_path] = receipt_files(root, COMPLETED_RECEIPTS)
    receipt = json.loads(receipt_path.read_text())
    assert receipt["actual_result"] == {"exit_code": 0, "status": "success"}


@pytest.mark.parametrize(
    ("error", "is_interrupt"),
    [
        ("Exit code 1\nprevious run failed", True),
        ("spawn /bin/zsh ENOENT", False),
        ("spawn /bin/zsh ENOENT\nExit code 1", False),
        ("Exit code 143\nCommand timed out after 2m 0s", False),
        ("Exit code 143\npartial output\n  Command timed out after 2m 0s", False),
    ],
    ids=[
        "interrupt",
        "shell-not-started",
        "code-not-on-first-line",
        "timeout",
        "timeout-after-output",
    ],
)
def test_claude_bash_failure_without_an_outcome_completes_no_receipt(
    tmp_path: Path, error: str, is_interrupt: bool
) -> None:
    root, _ = active_checkout(tmp_path)
    challenge, command = challenge_case(root, "test_runner")
    challenge["falsifying_outcome"] = {"exit_code": 1}
    add_challenge(root, challenge)
    tool_input = {"command": command}
    before_tool(root, "Bash", tool_input, tool_use_id="claude-failure-no-outcome")
    assert receipt_files(root, PENDING_RECEIPTS)

    claude_bash_failure(
        root,
        tool_input,
        error,
        tool_use_id="claude-failure-no-outcome",
        is_interrupt=is_interrupt,
    )

    assert receipt_files(root, COMPLETED_RECEIPTS) == []
    assert receipt_files(root, PENDING_RECEIPTS) == []


def test_challenge_codex_nonzero_post_tool_result_completes_receipt(tmp_path: Path) -> None:
    root, _ = active_checkout(tmp_path)
    challenge, command = challenge_case(root, "test_runner")
    challenge["falsifying_outcome"] = {"exit_code": 1}
    add_challenge(root, challenge)
    tool_input = {"command": command}
    before_tool(root, "exec_command", tool_input, tool_use_id="codex-failed", host="codex")

    after_tool(
        root,
        "exec_command",
        tool_input,
        {"exit_code": 1, "output": "failed"},
        tool_use_id="codex-failed",
        host="codex",
    )

    receipt = json.loads(receipt_files(root, COMPLETED_RECEIPTS)[0].read_text())
    assert receipt["actual_result"] == {"exit_code": 1, "status": "failure"}
    assert receipt["falsified"] is True


@pytest.mark.parametrize(
    ("method", "artifact", "command"),
    [
        ("test_runner", "tests/challenge.test.mjs", "node --test tests/challenge.test.mjs"),
        (
            "prototype_measurement",
            "prototypes/alternative.py",
            "uv run python prototypes/alternative.py",
        ),
    ],
)
def test_challenge_supported_additional_command_shapes(
    tmp_path: Path, method: str, artifact: str, command: str
) -> None:
    root, _ = active_checkout(tmp_path)
    artifact_path = root / artifact
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text("value = 1\n")
    falsifying_outcome = (
        {"exit_code": 0} if method == "test_runner" else {"max_substantive_lines": 20}
    )
    add_challenge(
        root,
        {
            "id": f"challenge-{method}",
            "alternative_id": "alternative-b",
            "claim": "The alternative is too large.",
            "method": method,
            "falsifying_outcome": falsifying_outcome,
            "artifacts": [artifact],
        },
    )
    tool_input = {"command": command}

    before_tool(root, "Bash", tool_input, tool_use_id=f"additional-{method}")
    after_tool(
        root,
        "Bash",
        tool_input,
        claude_bash_response(),
        tool_use_id=f"additional-{method}",
    )

    receipt = json.loads(receipt_files(root, COMPLETED_RECEIPTS)[0].read_text())
    assert receipt["kind"] == "challenge"
    assert receipt["falsified"] is True


def test_forged_command_receipt_cannot_use_printed_output(tmp_path: Path) -> None:
    root, _ = active_checkout(tmp_path)
    challenge, _ = challenge_case(root, "test_runner")
    add_challenge(root, challenge)
    tool_input = {"command": "printf 'exit_code: 0\\n'"}

    before_tool(root, "Bash", tool_input, tool_use_id="forged-command")
    after_tool(
        root,
        "Bash",
        tool_input,
        claude_bash_response("exit_code: 0"),
        tool_use_id="forged-command",
    )

    assert receipt_files(root, PENDING_RECEIPTS) == []
    assert receipt_files(root, COMPLETED_RECEIPTS) == []


def complete_recommendation_coverage(
    tmp_path: Path, *, falsified: bool = False
) -> tuple[Path, str, str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    root, source = active_checkout(tmp_path)
    source_input = {"file_path": str(source)}
    before_tool(root, "Read", source_input, tool_use_id="drawback-source")
    after_tool(
        root,
        "Read",
        source_input,
        "Atomic rename replaces a file.",
        tool_use_id="drawback-source",
    )
    source_receipt = json.loads(receipt_files(root, COMPLETED_RECEIPTS)[0].read_text())

    test_path = root / "tests/challenge_test.py"
    test_path.parent.mkdir(parents=True, exist_ok=True)
    test_path.write_text("def test_small():\n    assert True\n")
    challenge = {
        "id": "challenge-b",
        "alternative_id": "alternative-b",
        "claim": "The alternative cannot pass a focused test.",
        "method": "test_runner",
        "falsifying_outcome": {"exit_code": 0 if falsified else 1},
        "artifacts": ["tests/challenge_test.py"],
    }
    ledger = read_ledger(root)
    ledger.update(
        {
            "approaches": [
                {
                    "id": "alternative-a",
                    "title": "File",
                    "drawbacks": [{"id": "drawback-a1", "claim": "A rename can replace a file."}],
                },
                {"id": "alternative-b", "title": "Database", "drawbacks": []},
            ],
            "drawback_resolutions": [
                {
                    "drawback_id": "drawback-a1",
                    "status": "resolved",
                    "finding": "The runtime supports replacement by rename.",
                    "receipt_id": source_receipt["receipt_id"],
                    "quote": "Atomic rename replaces a file.",
                }
            ],
            "alternative_challenges": [challenge],
            "recommendation": {
                "selected_approach_id": "alternative-a",
                "rejected_alternative_ids": ["alternative-b"],
            },
        }
    )
    write_ledger(root, ledger)
    tool_input = {"command": "uv run pytest tests/challenge_test.py"}
    before_tool(root, "Bash", tool_input, tool_use_id="challenge-result")
    after_tool(
        root,
        "Bash",
        tool_input,
        claude_bash_response("1 passed"),
        tool_use_id="challenge-result",
    )
    challenge_receipt = next(
        json.loads(path.read_text())
        for path in receipt_files(root, COMPLETED_RECEIPTS)
        if json.loads(path.read_text()).get("kind") == "challenge"
    )
    ledger = read_ledger(root)
    ledger["alternative_challenges"][0]["receipt_id"] = challenge_receipt["receipt_id"]
    ledger["alternative_challenges"][0]["result"] = "completed"
    write_ledger(root, ledger)
    return root, source_receipt["receipt_id"], challenge_receipt["receipt_id"]


def test_recommendation_coverage_complete_allows_turn(tmp_path: Path) -> None:
    root, _, _ = complete_recommendation_coverage(tmp_path)
    completed_before = [path.read_text() for path in receipt_files(root, COMPLETED_RECEIPTS)]

    result = stop_turn(root, "## Recommendation\n\nUse the file approach.")

    assert result.stdout == ""
    completed_after = [path.read_text() for path in receipt_files(root, COMPLETED_RECEIPTS)]
    assert completed_after == completed_before


@pytest.mark.parametrize(
    ("mutation", "expected_identifier"),
    [
        ("missing_drawback_receipt", "drawback-a1"),
        ("absent_quote_hash", "drawback-a1"),
        ("missing_rejection_challenge", "alternative-b"),
        ("changed_requirements", "docs/plans/specs/feature.md"),
    ],
)
def test_recommendation_coverage_mutation_blocks(
    tmp_path: Path, mutation: str, expected_identifier: str
) -> None:
    root, _, _ = complete_recommendation_coverage(tmp_path)
    ledger = read_ledger(root)
    if mutation == "missing_drawback_receipt":
        ledger["drawback_resolutions"][0]["receipt_id"] = "0" * 64
    elif mutation == "absent_quote_hash":
        ledger["drawback_resolutions"][0]["quote"] = "This line was never observed."
    elif mutation == "missing_rejection_challenge":
        ledger["alternative_challenges"] = []
    else:
        requirements = root / str(ledger["requirements_path"])
        requirements.write_text("# Changed requirements\n")
    write_ledger(root, ledger)

    result = stop_turn(root, "I recommend the file approach.")

    decision = json.loads(result.stdout)
    assert decision["decision"] == "block"
    assert expected_identifier in decision["reason"]
    assert "Atomic rename" not in decision["reason"]


@pytest.mark.parametrize("mutation", ["copied_generation", "damaged_receipt"])
def test_recommendation_receipt_mutation_blocks(tmp_path: Path, mutation: str) -> None:
    # A receipt from another generation is ignored rather than rejected, so the block names
    # the drawback it no longer covers. A damaged current receipt is named itself.
    root, source_receipt_id, _ = complete_recommendation_coverage(tmp_path)
    path = root / COMPLETED_RECEIPTS / f"{source_receipt_id}.json"
    receipt = json.loads(path.read_text())
    if mutation == "copied_generation":
        receipt["generation"] = "copied-generation"
        receipt.pop("integrity_sha256")
        receipt["integrity_sha256"] = receipt_digest(receipt)
    else:
        receipt["result_sha256"] = "0" * 64
    path.write_text(json.dumps(receipt, indent=2) + "\n")

    result = stop_turn(root, "Our recommendation is the file approach.")

    decision = json.loads(result.stdout)
    assert decision["decision"] == "block"
    expected = "drawback-a1" if mutation == "copied_generation" else source_receipt_id
    assert expected in decision["reason"]


def test_receipts_left_by_an_earlier_generation_do_not_block(tmp_path: Path) -> None:
    root, source_receipt_id, _ = complete_recommendation_coverage(tmp_path)
    leftover = json.loads((root / COMPLETED_RECEIPTS / f"{source_receipt_id}.json").read_text())
    leftover["generation"] = "earlier-generation"
    leftover["requirements_sha256"] = "0" * 64
    leftover["receipt_id"] = "e" * 64
    leftover.pop("integrity_sha256")
    leftover["integrity_sha256"] = receipt_digest(leftover)
    (root / COMPLETED_RECEIPTS / f"{'e' * 64}.json").write_text(json.dumps(leftover))
    (root / COMPLETED_RECEIPTS / f"{'f' * 64}.json").write_text('{"generation": "older"')

    result = stop_turn(root, "## Recommendation\n\nUse the file approach.")

    assert result.stdout == ""


def test_recommendation_falsified_rejection_mutation_blocks(tmp_path: Path) -> None:
    root, _, _ = complete_recommendation_coverage(tmp_path, falsified=True)

    result = stop_turn(root, "The recommended approach is the file approach.")

    decision = json.loads(result.stdout)
    assert decision["decision"] == "block"
    assert "alternative-b" in decision["reason"]
    assert "falsified" in decision["reason"].lower()


def test_recommendation_agent_written_receipt_object_does_not_count(tmp_path: Path) -> None:
    root, _, _ = complete_recommendation_coverage(tmp_path)
    ledger = read_ledger(root)
    resolution = ledger["drawback_resolutions"][0]
    resolution["receipt_id"] = "f" * 64
    resolution["receipt"] = {
        "kind": "source",
        "line_hashes": [hashlib.sha256(b"Atomic rename replaces a file.").hexdigest()],
    }
    write_ledger(root, ledger)

    result = stop_turn(root, "I recommend the file approach.")

    assert "drawback-a1" in json.loads(result.stdout)["reason"]


def test_recommendation_receipt_edit_event_invalidates_agent_written_record(
    tmp_path: Path,
) -> None:
    root, source_receipt_id, _ = complete_recommendation_coverage(tmp_path)
    path = root / COMPLETED_RECEIPTS / f"{source_receipt_id}.json"
    receipt = json.loads(path.read_text())
    receipt["result_sha256"] = "0" * 64
    receipt.pop("integrity_sha256")
    receipt["integrity_sha256"] = receipt_digest(receipt)
    path.write_text(json.dumps(receipt, indent=2) + "\n")
    after_tool(
        root,
        "Write",
        {"file_path": str(path)},
        {"status": "ok"},
        tool_use_id="agent-receipt-edit",
    )

    result = stop_turn(root, "I recommend the file approach.")

    assert not path.exists()
    assert "drawback-a1" in json.loads(result.stdout)["reason"]


def test_recommendation_coverage_both_hosts_name_same_missing_check(tmp_path: Path) -> None:
    reasons = []
    for host in ("claude", "codex", "qwen"):
        root, _, _ = complete_recommendation_coverage(tmp_path / host)
        ledger = read_ledger(root)
        ledger["drawback_resolutions"] = []
        write_ledger(root, ledger)
        if host in ("codex", "qwen"):
            for marker in receipt_files(root, PROMPT_MARKERS):
                marker.unlink()
            submit_prompt_for_host(root, host, "continue")

        result = stop_turn_for_host(root, host, "## Recommendation\n\nUse the file approach.")
        decision = json.loads(result.stdout)
        assert decision["decision"] == "block"
        assert "drawback-a1" in decision["reason"]
        reasons.append(decision["reason"])

    assert reasons[0] == reasons[1] == reasons[2]


def qwen_active_checkout(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "checkout"
    root.mkdir()
    source = root / "docs/reference.md"
    source.parent.mkdir(parents=True)
    source.write_text("# Reference\n\nAtomic rename replaces a file.\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "docs/reference.md"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Ferry Test",
            "-c",
            "user.email=ferry@example.invalid",
            "commit",
            "-qm",
            "seed",
        ],
        check=True,
    )
    post_edit(root, "docs/plans/specs/feature.md", "# Requirements\n")
    submit_prompt_for_host(root, "qwen", "continue")
    return root, source


def test_qwen_activation_records_host_without_prompt_or_turn_fields(
    tmp_path: Path,
) -> None:
    root, source = qwen_active_checkout(tmp_path)

    active = read_ledger(root)
    assert active["state"] == "active"
    assert active["activation"]["host"] == "qwen"
    assert active["activation"]["session_id"] == "session-1"
    assert active["activation"]["kind"] == "continue"

    source_input = {"file_path": str(source)}
    before_tool(root, "read_file", source_input, tool_use_id="qwen-source", host="qwen")
    after_tool(
        root,
        "read_file",
        source_input,
        {"llmContent": "Atomic rename replaces a file.", "returnDisplay": ""},
        tool_use_id="qwen-source",
        host="qwen",
    )

    receipt = json.loads(receipt_files(root, COMPLETED_RECEIPTS)[0].read_text())
    assert receipt["kind"] == "source"
    assert receipt["host"] == "qwen"


def qwen_shell_text(command: str, output: str, exit_code: int) -> str:
    # The text Qwen's run_shell_command builds for a finished command (qwen-code 0.25.0).
    return "\n".join(
        [
            f"Command: {command}",
            "Directory: (root)",
            f"Output: {output}",
            "Error: (none)",
            f"Exit Code: {exit_code}",
            "Signal: (none)",
            "Process Group PGID: 4242",
        ]
    )


def qwen_shell_result(
    root: Path,
    tool_input: dict[str, object],
    text: str,
    *,
    tool_use_id: str,
    failed: bool,
    is_interrupt: bool = False,
) -> None:
    # Qwen sends PostToolUse with {llmContent, returnDisplay} for a command it treats as
    # successful, and PostToolUseFailure with a top-level error and no tool_response for a
    # non-zero exit (exit 1 from grep, rg, diff and test still counts as success).
    payload: dict[str, object] = {
        "session_id": "session-1",
        "tool_use_id": tool_use_id,
        "tool_name": "run_shell_command",
        "tool_input": tool_input,
    }
    if failed:
        payload |= {
            "hook_event_name": "PostToolUseFailure",
            "error": text,
            "is_interrupt": is_interrupt,
        }
    else:
        payload |= {
            "hook_event_name": "PostToolUse",
            "tool_response": {"llmContent": text, "returnDisplay": ""},
        }
    result = run_hook(root, "qwen", payload)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("failed", "exit_code", "status"),
    [
        (False, 0, "success"),
        (False, 1, "failure"),
        (True, 2, "failure"),
    ],
)
def test_qwen_shell_result_completes_receipt_with_its_exit_code(
    tmp_path: Path, failed: bool, exit_code: int, status: str
) -> None:
    root, _ = qwen_active_checkout(tmp_path)
    challenge, command = challenge_case(root, "test_runner")
    challenge["falsifying_outcome"] = {"exit_code": exit_code}
    add_challenge(root, challenge)
    tool_input = {"command": command}
    before_tool(root, "run_shell_command", tool_input, tool_use_id="qwen-shell", host="qwen")

    qwen_shell_result(
        root,
        tool_input,
        qwen_shell_text(command, "test output", exit_code),
        tool_use_id="qwen-shell",
        failed=failed,
    )

    [receipt_path] = receipt_files(root, COMPLETED_RECEIPTS)
    receipt = json.loads(receipt_path.read_text())
    assert receipt["actual_result"] == {"exit_code": exit_code, "status": status}
    assert receipt["falsified"] is True


@pytest.mark.parametrize(
    ("failed", "text"),
    [
        (True, "Command timed out after 1000ms before it could complete."),
        (
            True,
            "Command: x\nOutput: (empty)\nError: (none)\nExit Code: (none)\nSignal: SIGKILL",
        ),
        (False, "Background shell started.\nid: bg_ab12cd34"),
    ],
    ids=["timeout", "signal", "background"],
)
def test_qwen_shell_result_without_exit_code_completes_no_receipt(
    tmp_path: Path, failed: bool, text: str
) -> None:
    # A command that never reported an exit code has no outcome; recording a default
    # code would let a challenge expecting exactly that code pass without running.
    root, _ = qwen_active_checkout(tmp_path)
    challenge, command = challenge_case(root, "test_runner")
    challenge["falsifying_outcome"] = {"exit_code": 1 if failed else 0}
    add_challenge(root, challenge)
    tool_input = {"command": command}
    before_tool(root, "run_shell_command", tool_input, tool_use_id="qwen-no-code", host="qwen")
    assert receipt_files(root, PENDING_RECEIPTS)

    qwen_shell_result(root, tool_input, text, tool_use_id="qwen-no-code", failed=failed)

    assert receipt_files(root, COMPLETED_RECEIPTS) == []
    assert receipt_files(root, PENDING_RECEIPTS) == []


def test_qwen_interrupted_shell_command_completes_no_receipt(tmp_path: Path) -> None:
    root, _ = qwen_active_checkout(tmp_path)
    challenge, command = challenge_case(root, "test_runner")
    add_challenge(root, challenge)
    tool_input = {"command": command}
    before_tool(root, "run_shell_command", tool_input, tool_use_id="qwen-cancel", host="qwen")
    assert receipt_files(root, PENDING_RECEIPTS)

    qwen_shell_result(
        root,
        tool_input,
        "Command was cancelled by user before it could complete. "
        "Below is the output before it was cancelled:\nprevious run: Exit Code: 1",
        tool_use_id="qwen-cancel",
        failed=True,
        is_interrupt=True,
    )

    assert receipt_files(root, COMPLETED_RECEIPTS) == []
    assert receipt_files(root, PENDING_RECEIPTS) == []


def test_qwen_stop_blocks_recommendation_with_missing_evidence(tmp_path: Path) -> None:
    root, _ = qwen_active_checkout(tmp_path)

    result = stop_turn_for_host(root, "qwen", "## Recommendation\n\nUse the file approach.")

    decision = json.loads(result.stdout)
    assert decision["decision"] == "block"
    assert decision["reason"]


def test_qwen_exit_code_uses_the_final_status_line(tmp_path: Path) -> None:
    root, _ = qwen_active_checkout(tmp_path)
    challenge, command = challenge_case(root, "test_runner")
    challenge["falsifying_outcome"] = {"exit_code": 2}
    add_challenge(root, challenge)
    tool_input = {"command": command}
    before_tool(root, "run_shell_command", tool_input, tool_use_id="qwen-shadow", host="qwen")

    qwen_shell_result(
        root,
        tool_input,
        qwen_shell_text(command, "captured log said Exit Code: 5", 2),
        tool_use_id="qwen-shadow",
        failed=True,
    )

    [receipt_path] = receipt_files(root, COMPLETED_RECEIPTS)
    receipt = json.loads(receipt_path.read_text())
    assert receipt["actual_result"] == {"exit_code": 2, "status": "failure"}
    assert receipt["falsified"] is True


def test_second_opinion_launcher_filters_child_environment(tmp_path: Path) -> None:
    home = tmp_path / "home"
    server_dir = home / "Documents/GitHub/portalpilot/second-opinion"
    (server_dir / ".venv/bin").mkdir(parents=True)
    (server_dir / ".venv/bin/python3").write_text("#!/bin/sh\n")
    (server_dir / "main.py").write_text("# fixture server\n")

    script = f"""
import {{ runSecondOpinion }} from '{REPO}/scripts/agent-compat/second-opinion-mcp.mjs';
const observed = {{}};
const fakeChild = {{
  once: (event, handler) => {{
    if (event === 'close') setImmediate(() => handler(0, null));
  }},
  kill: () => {{}},
}};
const result = await runSecondOpinion({{
  home: {json.dumps(str(home))},
  fieldReader: async () => 'fixture-mistral-key',
  environment: {{
    PATH: '/usr/bin',
    HOME: {json.dumps(str(home))},
    GEMINI_API_KEY: 'fixture-gemini-key',
    SOME_PARENT_SECRET: 'parent-canary',
  }},
  spawnChild: (command, args, options) => {{
    observed.command = command;
    observed.args = args;
    observed.env = options.env;
    return fakeChild;
  }},
  parent: {{ on: () => {{}}, removeListener: () => {{}} }},
}});
observed.status = result.status;
process.stdout.write(JSON.stringify(observed));
"""
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed["status"] == 0
    assert observed["command"].endswith(".venv/bin/python3")
    assert observed["args"] == [str(server_dir / "main.py")]
    env = observed["env"]
    assert env["MISTRAL_API_KEY"] == "fixture-mistral-key"
    assert env["GEMINI_API_KEY"] == "fixture-gemini-key"
    assert env["PATH"] == "/usr/bin"
    assert "SOME_PARENT_SECRET" not in env
