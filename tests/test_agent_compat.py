from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import time
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")
NODE_EXECUTABLE = str(Path(NODE).resolve()) if NODE else None


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=REPO, capture_output=True, text=True, check=False)


def _canonical_repo() -> Path:
    result = _run("git", "rev-parse", "--path-format=absolute", "--git-common-dir")
    assert result.returncode == 0, result.stderr
    return Path(result.stdout.strip()).resolve().parent


def _write_plain_english_fixture(fake_bin: Path) -> Path:
    fake_bin.mkdir(parents=True, exist_ok=True)
    command = fake_bin / "plain-english"
    shutil.copy2(REPO / "tests/fixtures/plain_english_cli_fixture.cjs", command)
    command.chmod(0o755)
    return command


def _installer_checkout(
    tmp_path: Path, *, version: str = "1.0.0", command: bool = True
) -> tuple[Path, Path, dict[str, str]]:
    root = tmp_path / "repo"
    user_home = tmp_path / "home"
    fake_bin = tmp_path / "bin"
    root.mkdir()
    user_home.mkdir()
    fake_bin.mkdir()
    assert NODE is not None
    (fake_bin / "node").symlink_to(NODE)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    shutil.copytree(REPO / "config/agent-compat", root / "config/agent-compat")
    shutil.copytree(REPO / "scripts/agent-compat", root / "scripts/agent-compat")
    fixture_skill = root / ".claude/skills/fixture-skill/SKILL.md"
    fixture_skill.parent.mkdir(parents=True)
    fixture_skill.write_text("# Fixture skill\n")
    brainstorm_command = (
        'node "$CLAUDE_PROJECT_DIR/scripts/agent-compat/brainstorm-evidence.mjs" --host claude'
    )

    def claude_group(matcher: str | None = None) -> dict[str, object]:
        return {
            **({} if matcher is None else {"matcher": matcher}),
            "hooks": [
                {
                    "type": "command",
                    "command": brainstorm_command,
                    "timeout": 10,
                }
            ],
        }

    (root / ".claude/settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "UserPromptSubmit": [claude_group()],
                    "PreToolUse": [claude_group(".*")],
                    "PostToolUse": [claude_group(".*")],
                    "PostToolUseFailure": [claude_group(".*")],
                    "Stop": [claude_group()],
                }
            }
        )
    )
    worktree_script = root / ".claude/scripts/new-worktree.sh"
    worktree_script.parent.mkdir(parents=True)
    worktree_script.write_text(
        "for host_dir in .agents .codex .vibe .qwen; do\n"
        'ln -s "../../$host_dir" "$WT/$host_dir"\n'
        "done\n"
        "for link in CLAUDE.md .claude/rules .claude/skills AGENTS.md .agents "
        ".codex .vibe .qwen; do\n"
        "done\n"
    )
    (root / ".worktreeinclude").write_text("\n")
    writer = "During the build, run `mkdir -p docs/plans`, then write "
    writer += "`docs/plans/change-manifest.md`.\n"
    (root / "AGENTS.md").write_text(f"# Host Compatibility\n{writer}")
    (root / "CLAUDE.md").write_text(writer)
    ship_skill = root / ".claude/skills/df-ship/SKILL.md"
    ship_skill.parent.mkdir(parents=True)
    ship_skill.write_text(
        "head -1 docs/plans/change-manifest.md\n"
        "If the manifest names this branch or this change, use it.\n"
        "If no manifest exists, grep the codebase pattern.\n"
        "If `docs/plans/change-manifest.md` exists, include it as manifest context.\n"
    )
    if command:
        _write_plain_english_fixture(fake_bin)
    env = {
        **os.environ,
        "HOME": str(user_home),
        "CODEX_HOME": str(user_home / ".codex"),
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "FERRY_PLAIN_ENGLISH_VERSION": version,
    }
    return root, user_home, env


def _worktree_contract_fixture(
    tmp_path: Path,
) -> tuple[Path, Path, Path, Path, Path]:
    script = tmp_path / "new-worktree.sh"
    script.write_text(
        "for host_dir in .agents .codex .vibe .qwen; do\n"
        'ln -s "../../$host_dir" "$WT/$host_dir"\n'
        "done\n"
        "for link in CLAUDE.md .claude/rules .claude/skills AGENTS.md .agents "
        ".codex .vibe .qwen; do\n"
        "done\n"
    )
    include = tmp_path / ".worktreeinclude"
    include.write_text("CLAUDE.md\n.claude/**\ndocs/architecture/**\n")
    writer = "During the build, run `mkdir -p docs/plans`, then write "
    writer += "`docs/plans/change-manifest.md`.\n"
    agents = tmp_path / "AGENTS.md"
    claude = tmp_path / "CLAUDE.md"
    agents.write_text(writer)
    claude.write_text(writer)
    ship = tmp_path / "df-ship.md"
    ship.write_text(
        "head -1 docs/plans/change-manifest.md\n"
        "If the manifest names this branch or this change, use it.\n"
        "If no manifest exists, grep the codebase pattern.\n"
        "If `docs/plans/change-manifest.md` exists, include it as manifest context.\n"
    )
    return script, include, agents, claude, ship


def _critique_contract_fixture(tmp_path: Path) -> tuple[Path, Path]:
    skill = tmp_path / "df-critique.md"
    skill.write_text(
        "allowed-tools: Bash, Read, Grep, Glob\n"
        "Run before it creates the fresh-context reviewer:\n"
        "node scripts/agent-compat/critique-budget.mjs claim --design <path>\n"
        "node scripts/agent-compat/critique-budget.mjs complete --design <path>\n"
        "node scripts/agent-compat/critique-budget.mjs decide --design <path>\n"
        "Round three uses evidence-investigation mode.\n"
        "The owner choices are accept, return-to-design, and restart.\n"
        "No fourth reviewer starts.\n"
    )
    command = tmp_path / "critique-budget.mjs"
    shutil.copy2(REPO / "scripts/agent-compat/critique-budget.mjs", command)
    return skill, command


def _generated_host_snapshot(root: Path) -> dict[str, tuple[int, bytes] | None]:
    records: dict[str, tuple[int, bytes] | None] = {}
    for host in (".agents", ".codex", ".vibe", ".qwen"):
        directory = root / host
        if not directory.exists():
            records[host] = None
            continue
        for path in sorted(item for item in directory.rglob("*") if item.is_file()):
            records[str(path.relative_to(root))] = (
                path.stat().st_mode & 0o777,
                path.read_bytes(),
            )
    return records


def test_adr_027_records_the_consequential_defaults() -> None:
    if not (REPO / "AGENTS.md").exists():
        pytest.skip("snapshot instruction layer is absent")
    adr = REPO / "docs/architecture/adr/027-codex-readiness-and-review-quorum.md"
    assert adr.exists()
    text = adr.read_text()
    normalized = " ".join(text.split()).lower()
    assert "dispatch fixed `mistral-vibe` and `qwen` slots" in text
    assert "reviewer availability does not block" in normalized
    assert "codex requests escalated execution on the first attempt" in normalized
    assert "2,097,152 bytes" in text
    assert "only stdout participates" in normalized
    assert "60-second outer timeout" in text
    assert "Plain-English is a required setup prerequisite" in text
    assert "Every `$df-writing-plans` result" in text
    assert "qwen3.8-max" in text
    assert "medium reasoning effort" in normalized
    assert "32,768 completion tokens" in normalized
    assert "60-second connection deadline" in normalized
    assert "30-second idle deadline" in normalized
    assert "600-second total deadline" in normalized
    assert "`selected_provider`" in text
    assert "`plan-opus`" in text
    assert "never starts Opus automatically" in text
    assert "never starts the other provider" in normalized
    assert "at most two" in normalized
    assert "before provider dispatch" in normalized
    assert "selected provider stays fixed" in normalized
    assert "provider failure is advisory" in normalized
    assert "owner decision" in normalized
    assert "no allow rule targets the writable checkout" in normalized


def test_adr_028_records_the_plain_english_host_contract() -> None:
    if not (REPO / "AGENTS.md").exists():
        pytest.skip("snapshot instruction layer is absent")
    contract = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "plain-english-contract",
        "accepted",
        "--json",
    )
    assert contract.returncode == 0, contract.stderr
    expected = json.loads(contract.stdout)["expected"]
    adr = REPO / "docs/architecture/adr/028-pin-plain-english-host-contract.md"
    text = adr.read_text()
    changelog = (REPO / "CHANGELOG.md").read_text()
    assert f"plain-English {expected}" in text
    assert f"plain-english@{expected}" in text
    assert f"plain-English {expected}" in changelog
    assert "CI-only" in text
    assert "linked-worktree" in text


def test_codex_template_pins_runtime_and_live_servers() -> None:
    text = (REPO / "config/agent-compat/codex-config.toml").read_text()
    config = tomllib.loads(text)
    assert config["model"] == "gpt-5.6-sol"
    assert config["model_reasoning_effort"] == "high"
    assert config["approval_policy"] == "on-request"
    assert config["sandbox_mode"] == "workspace-write"
    assert config["web_search"] == "disabled"
    assert config["sandbox_workspace_write"] == {"network_access": True}
    assert config["features"]["network_proxy"] == {
        "enabled": True,
        "domains": {"api.github.com": "allow", "github.com": "allow"},
    }
    assert "[mcp_servers.qmd]" in text
    assert "[mcp_servers.serena]" in text
    assert "[mcp_servers.context7]" in text
    assert "mcp_servers.second-opinion" not in text


def test_installer_renders_project_pins_with_conflicting_global_defaults(
    tmp_path: Path,
) -> None:
    if not (REPO / "AGENTS.md").exists():
        pytest.skip("snapshot instruction layer is absent")
    root, user_home, env = _installer_checkout(tmp_path)
    codex_home = user_home / ".codex"
    codex_home.mkdir(parents=True)
    global_config = (
        'model = "personal-model"\nmodel_reasoning_effort = "low"\n'
        'approval_policy = "never"\nsandbox_mode = "read-only"\n'
        'web_search = "live"\n'
        f'[projects."{root}"]\ntrust_level = "trusted"\n'
    ).encode()
    global_path = codex_home / "config.toml"
    global_path.write_bytes(global_config)
    installed = subprocess.run(
        ["node", "scripts/agent-compat/install-local.mjs"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert installed.returncode == 0, installed.stderr
    rendered = (root / ".codex/config.toml").read_text()
    assert 'model = "gpt-5.6-sol"' in rendered
    assert 'approval_policy = "on-request"' in rendered
    assert 'model_reasoning_effort = "high"' in rendered
    assert 'sandbox_mode = "workspace-write"' in rendered
    assert 'web_search = "disabled"' in rendered
    config = tomllib.loads(rendered)
    assert config["sandbox_workspace_write"] == {"network_access": True}
    assert config["features"]["network_proxy"] == {
        "enabled": True,
        "domains": {"api.github.com": "allow", "github.com": "allow"},
    }


def test_installer_routes_context7_through_the_protected_launcher(tmp_path: Path) -> None:
    root, user_home, env = _installer_checkout(tmp_path)
    installed = subprocess.run(
        [NODE, "scripts/agent-compat/install-local.mjs"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert installed.returncode == 0, installed.stderr
    launcher = str(
        user_home / ".local/share/discord-ferry/reviewer-runtime/current/context7-mcp.mjs"
    )

    codex = tomllib.loads((root / ".codex/config.toml").read_text())
    vibe = tomllib.loads((root / ".vibe/config.toml").read_text())
    qwen = json.loads((root / ".qwen/settings.json").read_text())
    context7_servers = [
        codex["mcp_servers"]["context7"],
        next(server for server in vibe["mcp_servers"] if server["name"] == "context7"),
        qwen["mcpServers"]["context7"],
    ]

    for server in context7_servers:
        assert server["command"] == "node"
        assert server["args"] == [launcher]
        assert "env" not in server
        assert "@upstash/context7-mcp" not in json.dumps(server)
    assert codex["mcp_servers"]["context7"]["startup_timeout_sec"] == 30
    assert context7_servers[1]["transport"] == "stdio"
    assert qwen["mcpServers"]["context7"]["timeout"] == 30_000
    rendered = "\n".join(
        [
            (root / ".codex/config.toml").read_text(),
            (root / ".vibe/config.toml").read_text(),
            (root / ".qwen/settings.json").read_text(),
        ]
    )
    assert "@upstash/context7-mcp" not in rendered
    assert "CONTEXT7_API_KEY" not in rendered
    assert "FERRY_SECRET_CANARY" not in rendered


def test_generated_state_check_rejects_context7_launcher_drift(tmp_path: Path) -> None:
    root, _, env = _installer_checkout(tmp_path)
    installed = subprocess.run(
        [NODE, "scripts/agent-compat/install-local.mjs"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert installed.returncode == 0, installed.stderr
    config = root / ".codex/config.toml"
    config.write_text(config.read_text().replace("context7-mcp.mjs", "context8-mcp.mjs", 1))

    checked = subprocess.run(
        [NODE, "scripts/agent-compat/check.mjs", "--generated-only"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert checked.returncode == 1
    assert ".codex/config.toml does not match template" in checked.stdout


def test_custom_data_home_routes_every_context7_host_to_the_installed_launcher(
    tmp_path: Path,
) -> None:
    root, user_home, env = _installer_checkout(tmp_path)
    data_home = tmp_path / "custom data"
    env["XDG_DATA_HOME"] = str(data_home)
    installed = subprocess.run(
        [NODE, "scripts/agent-compat/install-local.mjs"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert installed.returncode == 0, installed.stderr
    launcher = str(data_home / "discord-ferry/reviewer-runtime/current/context7-mcp.mjs")

    codex = tomllib.loads((root / ".codex/config.toml").read_text())
    vibe = tomllib.loads((root / ".vibe/config.toml").read_text())
    qwen = json.loads((root / ".qwen/settings.json").read_text())
    assert codex["mcp_servers"]["context7"]["args"] == [launcher]
    assert next(
        server["args"] for server in vibe["mcp_servers"] if server["name"] == "context7"
    ) == [launcher]
    assert qwen["mcpServers"]["context7"]["args"] == [launcher]

    readiness = subprocess.run(
        [
            NODE,
            "tests/fixtures/agent_compat_runner.mjs",
            "readiness-static",
            "healthy",
            "--root",
            str(root),
            "--home",
            str(user_home),
            "--json",
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert readiness.returncode == 0, readiness.stderr
    record = next(
        item
        for item in json.loads(readiness.stdout)["records"]
        if item["id"] == "context7-credential"
    )
    assert record["status"] == "ok"


@pytest.mark.parametrize(
    ("version", "command"),
    [("0.24.1", True), ("plain-english 1.0.0", True), ("1.0.0", False)],
)
def test_installer_rejects_unsupported_plain_english_before_writes(
    tmp_path: Path, version: str, command: bool
) -> None:
    root, _, env = _installer_checkout(tmp_path, version=version, command=command)
    before = _generated_host_snapshot(root)
    result = subprocess.run(
        [NODE, "scripts/agent-compat/install-local.mjs"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert _generated_host_snapshot(root) == before
    assert "expected 1.0.0" in result.stderr
    assert "npm install -g plain-english@1.0.0" in result.stderr


def test_installer_keeps_native_plain_english_artifacts(tmp_path: Path) -> None:
    root, _, env = _installer_checkout(tmp_path)
    result = subprocess.run(
        [NODE, "scripts/agent-compat/install-local.mjs"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (root / ".codex/hooks/plain-english.mjs").exists()
    assert (root / ".vibe/hooks/plain-english.mjs").exists()
    assert (root / ".vibe/hooks/plain-english-docs.prompt.md").exists()
    assert not (root / ".codex/bin/plain-english-chat-hook.mjs").exists()
    assert "npx" not in (root / ".codex/hooks.json").read_text()
    assert "npm exec" not in (root / ".vibe/hooks.toml").read_text()
    document = json.loads((root / ".codex/hooks.json").read_text())
    native = [
        hook
        for event in ("Stop", "SubagentStop")
        for group in document["hooks"][event]
        for hook in group["hooks"]
        if "plain-english.mjs" in hook["command"]
    ]
    expected = f"node '{root}/.codex/hooks/plain-english.mjs' hook chat --agent codex"
    assert [hook["command"] for hook in native] == [expected, expected]
    assert all("git rev-parse" not in hook["command"] for hook in native)


def test_installed_codex_chat_hooks_run_outside_repository(tmp_path: Path) -> None:
    root, _, env = _installer_checkout(tmp_path)
    installed = subprocess.run(
        [NODE, "scripts/agent-compat/install-local.mjs"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert installed.returncode == 0, installed.stderr
    fake_cli = Path(env["PATH"].split(":", 1)[0]) / "plain-english"
    fake_cli.write_text("#!/bin/sh\nexit 0\n")
    fake_cli.chmod(0o755)
    event_cwd = tmp_path / "event-cwd"
    event_cwd.mkdir()
    document = json.loads((root / ".codex/hooks.json").read_text())

    for event in ("Stop", "SubagentStop"):
        native = [
            hook
            for group in document["hooks"][event]
            for hook in group["hooks"]
            if "plain-english.mjs" in hook["command"]
        ]
        assert len(native) == 1
        result = subprocess.run(
            ["/bin/sh", "-c", native[0]["command"]],
            cwd=event_cwd,
            env=env,
            input=json.dumps({"hook_event_name": event}),
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr


def test_installer_second_run_preserves_native_bytes_and_modes(tmp_path: Path) -> None:
    root, _, env = _installer_checkout(tmp_path)
    first = subprocess.run(
        [NODE, "scripts/agent-compat/install-local.mjs"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert first.returncode == 0, first.stderr
    first_snapshot = _generated_host_snapshot(root)
    linked_root = tmp_path / "linked repo"
    linked_root.symlink_to(root)
    second = subprocess.run(
        [NODE, "scripts/agent-compat/install-local.mjs"],
        cwd=linked_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert second.returncode == 0, second.stderr
    assert _generated_host_snapshot(root) == first_snapshot


def test_native_plain_english_launcher_terminates_its_process_tree(tmp_path: Path) -> None:
    assert NODE is not None
    root, _, env = _installer_checkout(tmp_path)
    installed = subprocess.run(
        [NODE, "scripts/agent-compat/install-local.mjs"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert installed.returncode == 0, installed.stderr
    cli_pid = tmp_path / "cli.pid"
    child_pid = tmp_path / "child.pid"
    launcher = subprocess.Popen(
        [NODE, str(root / ".codex/hooks/plain-english.mjs"), "hook", "chat", "--agent", "codex"],
        cwd=root,
        env={
            **env,
            "FERRY_PLAIN_ENGLISH_CLI_PID": str(cli_pid),
            "FERRY_PLAIN_ENGLISH_CHILD_PID": str(child_pid),
        },
    )
    for _ in range(100):
        if cli_pid.exists() and child_pid.exists():
            break
        time.sleep(0.02)
    descendant_pids = [int(cli_pid.read_text()), int(child_pid.read_text())]
    launcher.send_signal(signal.SIGTERM)
    assert launcher.wait(timeout=5) == -signal.SIGTERM
    for pid in descendant_pids:
        for _ in range(200):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.01)
        else:
            pytest.fail(f"plain-English descendant {pid} survived SIGTERM")


def test_agents_names_tested_mcp_calls_and_review_quorum() -> None:
    agents = REPO / "AGENTS.md"
    if not agents.exists():
        pytest.skip("snapshot-backed instruction layer is absent in CI")
    text = agents.read_text()
    normalized = " ".join(text.split()).lower()
    assert "qmd health: call `status` with `{}`" in text
    assert "codex requests escalated execution on the first attempt" in normalized
    assert "never tries the workspace sandbox first" in normalized
    assert "never runs a separate credential login" in normalized
    assert "verdict evaluation stay in the workspace sandbox" in normalized
    assert "Serena overview: call `get_symbols_overview`" in text
    assert "Context7 documentation: call `resolve-library-id`" in text
    assert "provider failures are recorded" in normalized
    assert "at most two external plan-review attempts" in text
    assert "provider failure is advisory" in normalized
    assert "selected provider stays fixed" in normalized
    assert "Sonnet 5 is an explicit owner-selected route" in text
    assert "Every Sonnet call uses medium effort" in text


def test_claude_policy_and_decision_select_sonnet_5() -> None:
    settings_path = REPO / ".claude/settings.local.json"
    agents_path = REPO / "AGENTS.md"
    claude_path = REPO / "CLAUDE.md"
    decision_path = REPO / "docs/architecture/adr/033-sonnet-5-df-workflow.md"
    index_path = REPO / "docs/architecture/adr/README.md"
    targets = [settings_path, agents_path, claude_path, decision_path, index_path]
    if not all(target.exists() for target in targets):
        pytest.skip("snapshot-backed instruction layer is absent in CI")

    settings = json.loads(settings_path.read_text())
    agents = agents_path.read_text()
    claude = claude_path.read_text()
    decision = decision_path.read_text()
    index = index_path.read_text()

    assert settings["model"] == "claude-sonnet-5"
    assert settings["effortLevel"] == "medium"
    assert 'latest Sonnet (model: "sonnet")' in agents
    assert 'latest Sonnet (`model: "sonnet"`)' in claude
    assert "**Status:** Accepted" in decision
    assert "**Supersedes:** ADR-017's model choice" in decision
    assert "[033](033-sonnet-5-df-workflow.md)" in index

    live_policy = "\n".join([agents, claude, settings_path.read_text()])
    assert "latest Opus" not in live_policy
    assert "claude-opus-4-8" not in live_policy
    assert "Opus 5 is an explicit owner-selected route" not in live_policy


def test_codex_and_vibe_adapters_skip_native_plain_english_routes() -> None:
    result = subprocess.run(
        ["node", "tests/fixtures/agent_compat_runner.mjs", "hook-routes", "--json"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    routes = json.loads(result.stdout)
    assert routes["codex_write"] == ["brainstorm-evidence", "write"]
    assert routes["vibe_write"] == ["write"]
    assert "docs" in routes["qwen_write"]
    assert "github-docs" not in routes["codex_bash"]
    assert "github-docs" not in routes["vibe_bash"]
    assert "github-docs" in routes["qwen_bash"]


def test_brainstorm_hook_parity_names_all_five_events_and_host_dispositions() -> None:
    result = subprocess.run(
        ["node", "tests/fixtures/agent_compat_runner.mjs", "hook-routes", "--json"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    entries = report["brainstorm_entries"]
    assert [entry["event"] for entry in entries] == [
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "Stop",
    ]
    assert [entry["codex_disposition"] for entry in entries] == [
        "ported",
        "ported",
        "ported",
        "compensated",
        "ported",
    ]
    assert {entry["vibe_disposition"] for entry in entries} == {"unsupported"}
    assert {entry["qwen_disposition"] for entry in entries} == {"unsupported"}


def test_brainstorm_hook_parity_matches_open_ended_codex_tools_only() -> None:
    result = subprocess.run(
        ["node", "tests/fixtures/agent_compat_runner.mjs", "hook-routes", "--json"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["brainstorm_codex_external_before"] == ["brainstorm-evidence"]
    assert report["brainstorm_codex_external_after"] == ["brainstorm-evidence"]
    assert re.fullmatch(report["brainstorm_codex_post_matcher"], "mcp__context7__query-docs")
    assert report["vibe_post_matcher"] == "re:^(apply_patch|edit|write_file)$"
    assert report["qwen_post_matcher"] == "edit|write_file"


def test_claude_brainstorm_hooks_register_all_five_direct_events() -> None:
    settings = REPO / ".claude/settings.json"
    if not settings.exists():
        pytest.skip("snapshot-backed Claude settings are absent in CI")
    document = json.loads(settings.read_text())
    command = (
        'node "$CLAUDE_PROJECT_DIR/scripts/agent-compat/brainstorm-evidence.mjs" --host claude'
    )
    expected_matchers = {
        "UserPromptSubmit": None,
        "PreToolUse": ".*",
        "PostToolUse": ".*",
        "PostToolUseFailure": ".*",
        "Stop": None,
    }
    for event, matcher in expected_matchers.items():
        matches = [
            (group, hook)
            for group in document["hooks"][event]
            for hook in group["hooks"]
            if hook.get("command") == command
        ]
        assert len(matches) == 1
        group, hook = matches[0]
        assert group.get("matcher") == matcher
        assert hook == {"type": "command", "command": command, "timeout": 10}

    stop_commands = [
        hook["command"]
        for group in document["hooks"]["Stop"]
        for hook in group["hooks"]
        if hook["type"] == "command"
    ]
    assert stop_commands.count("$CLAUDE_PROJECT_DIR/.claude/hooks/plain-english-chat.sh") == 1


def test_claude_brainstorm_skill_requires_saved_evidence_before_recommendation() -> None:
    skill = REPO / ".claude/skills/df-brainstorm/SKILL.md"
    if not skill.exists():
        pytest.skip("snapshot-backed brainstorm skill is absent in CI")
    text = skill.read_text()
    normalized = " ".join(text.split()).lower()
    assert "give every approach and drawback a stable identifier" in normalized
    assert "docs/plans/.brainstorm-evidence/ledger.json" in text
    assert "before the source call" in normalized
    assert "completed source receipt" in normalized
    assert "exact quoted line" in normalized
    assert "falsifying outcome before running" in normalized
    assert "completed challenge receipt" in normalized
    assert "$df-brainstorm cancel" in text
    assert "validator must pass before presenting the recommendation" in normalized


def test_codex_patch_policy_finishes_inside_the_outer_budget() -> None:
    result = subprocess.run(
        [
            "node",
            "tests/fixtures/agent_compat_runner.mjs",
            "pre-tool-timing",
            "patch",
            "--json",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["plain_english_children"] == 0
    assert report["evidence_children"] == ["brainstorm-evidence"]
    assert report["security_children"] == ["write"]
    assert report["duration_ms"] < 2000


@pytest.mark.parametrize(
    ("fixture", "status"),
    [
        ("accepted", "accepted"),
        ("missing", "missing"),
        ("failed", "malformed"),
        ("empty", "malformed"),
        ("prefixed", "malformed"),
        ("old", "mismatched"),
        ("near-miss", "mismatched"),
        ("prerelease", "mismatched"),
    ],
)
def test_plain_english_contract_classifies_exact_version_output(fixture: str, status: str) -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "plain-english-contract",
        fixture,
        "--json",
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == status


def test_plain_english_contract_reports_actionable_failure() -> None:
    mismatch = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "plain-english-contract",
        "old",
        "--json",
    )
    missing = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "plain-english-contract",
        "missing",
        "--json",
    )
    mismatch_report = json.loads(mismatch.stdout)
    missing_report = json.loads(missing.stdout)
    assert mismatch_report["expected"] == "1.0.0"
    assert mismatch_report["detected"] == "0.24.1"
    assert "npm install -g plain-english@1.0.0" in mismatch_report["message"]
    assert missing_report["detected"] is None
    assert "no version detected" in missing_report["message"]


def test_codex_chat_transform_root_collapses_a_symlinked_checkout(
    tmp_path: Path,
) -> None:
    primary = tmp_path / "primary"
    primary.mkdir()
    subprocess.run(["git", "init", "-q", str(primary)], check=True)
    linked = tmp_path / "linked"
    linked.symlink_to(primary, target_is_directory=True)
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "canonical-checkout-root",
        "--root",
        str(linked),
        "--json",
    )
    assert result.returncode == 0, result.stderr
    assert Path(json.loads(result.stdout)["root"]) == primary.resolve()


def test_agent_check_accepts_native_plain_english_state(tmp_path: Path) -> None:
    root, _, env = _installer_checkout(tmp_path)
    installed = subprocess.run(
        [NODE, "scripts/agent-compat/install-local.mjs"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert installed.returncode == 0, installed.stderr
    result = subprocess.run(
        [NODE, "scripts/agent-compat/check.mjs", "--generated-only"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("mode", "requires_tool"),
    [
        ("default", True),
        ("generated-only", True),
        ("focused-codex", True),
        ("ci-focused-codex", True),
        ("ci-only", False),
        ("worktree-only", False),
    ],
)
def test_agent_check_gates_only_generated_state_modes_on_the_exact_version(
    tmp_path: Path, mode: str, requires_tool: bool
) -> None:
    root, _, env = _installer_checkout(tmp_path)
    installed = subprocess.run(
        [NODE, "scripts/agent-compat/install-local.mjs"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert installed.returncode == 0, installed.stderr
    marker = tmp_path / "init-called.jsonl"
    env = {
        **env,
        "FERRY_PLAIN_ENGLISH_VERSION": "0.24.1",
        "FERRY_PLAIN_ENGLISH_INIT_MARKER": str(marker),
    }
    arguments = {
        "default": [],
        "generated-only": ["--generated-only"],
        "focused-codex": ["--check-codex-hooks", str(root / ".codex/hooks.json")],
        "ci-focused-codex": [
            "--ci",
            "--check-codex-hooks",
            str(root / ".codex/hooks.json"),
        ],
        "ci-only": ["--ci"],
        "worktree-only": [
            "--check-worktree-contract",
            str(root / ".claude/scripts/new-worktree.sh"),
            str(root / ".worktreeinclude"),
            str(root / "AGENTS.md"),
            str(root / "CLAUDE.md"),
            str(root / ".claude/skills/df-ship/SKILL.md"),
        ],
    }[mode]
    result = subprocess.run(
        [NODE, "scripts/agent-compat/check.mjs", *arguments],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert not marker.exists(), result.stdout + result.stderr
    if requires_tool:
        assert result.returncode == 1
        assert "plain-English mismatched: expected 1.0.0" in result.stdout + result.stderr
    else:
        assert result.returncode == 0, result.stdout + result.stderr


def _alter_plain_english_state(root: Path, fixture: str) -> None:
    if fixture in {
        "codex-command",
        "duplicate-codex-hook",
        "codex-timeout",
        "event-time-git",
        "outside-codex-launcher",
    }:
        hooks_path = root / ".codex/hooks.json"
        document = json.loads(hooks_path.read_text())
        native = [
            hook
            for event in ("Stop", "SubagentStop")
            for group in document["hooks"][event]
            for hook in group["hooks"]
            if ".codex/hooks/plain-english.mjs" in hook["command"]
        ]
        if fixture == "codex-command":
            native[0]["command"] += " --changed"
        elif fixture == "duplicate-codex-hook":
            document["hooks"]["Stop"][0]["hooks"].append(dict(native[0]))
        elif fixture == "codex-timeout":
            native[0]["timeout"] = 10
        elif fixture == "event-time-git":
            native[0]["command"] = (
                'node "$(git rev-parse --show-toplevel)/.codex/hooks/plain-english.mjs" '
                "hook chat --agent codex"
            )
        else:
            outside = root.parent / "outside/plain-english.mjs"
            outside.parent.mkdir()
            outside.write_text("process.exit(0);\n")
            outside.chmod(0o755)
            native[0]["command"] = f"node '{outside}' hook chat --agent codex"
        hooks_path.write_text(f"{json.dumps(document, indent=2)}\n")
    elif fixture == "missing-codex-launcher":
        (root / ".codex/hooks/plain-english.mjs").unlink()
    elif fixture == "changed-vibe-prompt":
        (root / ".vibe/hooks/plain-english-docs.prompt.md").write_text("changed\n")
    elif fixture == "stale-ferry-wrapper":
        wrapper = root / ".codex/bin/plain-english-chat-hook.mjs"
        wrapper.parent.mkdir(parents=True, exist_ok=True)
        wrapper.write_text("stale\n")
    elif fixture == "missing-vibe-launcher":
        (root / ".vibe/hooks/plain-english.mjs").unlink()
    else:
        raise AssertionError(f"unknown fixture: {fixture}")


@pytest.mark.parametrize(
    ("fixture", "failure"),
    [
        ("codex-command", "expected one native plain-English chat hook for Stop"),
        ("duplicate-codex-hook", "expected one native plain-English chat hook for Stop"),
        ("codex-timeout", "unexpected native plain-English chat timeout for Stop: 10"),
        ("missing-codex-launcher", "missing native Codex plain-English launcher"),
        ("changed-vibe-prompt", "Vibe plain-English artifact differs"),
        ("stale-ferry-wrapper", "stale Ferry plain-English wrapper remains"),
        ("missing-vibe-launcher", "missing Vibe plain-English artifact"),
        ("event-time-git", "expected one native plain-English chat hook for Stop"),
        (
            "outside-codex-launcher",
            "expected one native plain-English chat hook for Stop",
        ),
    ],
)
def test_agent_check_rejects_native_plain_english_drift(
    tmp_path: Path, fixture: str, failure: str
) -> None:
    root, _, env = _installer_checkout(tmp_path)
    installed = subprocess.run(
        [NODE, "scripts/agent-compat/install-local.mjs"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert installed.returncode == 0, installed.stderr
    _alter_plain_english_state(root, fixture)
    result = subprocess.run(
        [NODE, "scripts/agent-compat/check.mjs", "--generated-only"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert failure in result.stdout + result.stderr


@pytest.mark.parametrize("agent", ["codex", "vibe"])
def test_staged_plain_english_timeout_is_bounded_and_cleans_up(tmp_path: Path, agent: str) -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "plain-english-staged-timeout",
        agent,
        "--base",
        str(tmp_path),
        "--json",
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["calls"] == (["codex"] if agent == "codex" else ["codex", "vibe"])
    assert report["timeout_ms"] == 30_000
    assert report["kill_signal"] == "SIGTERM"
    assert report["comparisons"] == 0
    assert report["stage_removed"] is True


def test_agent_check_rejects_a_ten_second_native_chat_hook(tmp_path: Path) -> None:
    root, _, env = _installer_checkout(tmp_path)
    installed = subprocess.run(
        [NODE, "scripts/agent-compat/install-local.mjs"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert installed.returncode == 0, installed.stderr
    hooks = root / ".codex/hooks.json"
    document = json.loads(hooks.read_text())
    native = [
        hook
        for event in ("Stop", "SubagentStop")
        for group in document["hooks"][event]
        for hook in group["hooks"]
        if ".codex/hooks/plain-english.mjs" in hook["command"]
    ]
    assert len(native) == 2
    native[0]["timeout"] = 10
    hooks.write_text(json.dumps(document))
    result = subprocess.run(
        [NODE, "scripts/agent-compat/check.mjs", "--check-codex-hooks", str(hooks)],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "unexpected native plain-English chat timeout for Stop: 10" in (
        result.stdout + result.stderr
    )


def test_native_codex_chat_normalization_is_repeat_safe() -> None:
    owner = "/fixture/canonical owner"
    stable = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "hook-regeneration",
        "current",
        "--owner",
        owner,
        "--runs",
        "2",
        "--json",
    )
    assert stable.returncode == 0, stable.stderr
    report = json.loads(stable.stdout)
    assert report["hook_count"] == 2
    assert (
        report["commands"]
        == [
            "node '/fixture/canonical owner/.codex/hooks/plain-english.mjs' "
            "hook chat --agent codex",
        ]
        * 2
    )
    assert report["timeouts"] == [60, 60]


def test_native_codex_chat_normalization_rejects_upstream_shape_drift() -> None:
    altered = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "hook-regeneration",
        "altered-command",
        "--owner",
        "/fixture/canonical-owner",
        "--json",
    )
    assert altered.returncode == 1
    assert "expected one native plain-English chat hook for Stop" in altered.stderr


def test_native_codex_chat_normalization_rejects_unknown_timeout() -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "hook-regeneration",
        "timeout-30",
        "--owner",
        "/fixture/canonical-owner",
        "--json",
    )
    assert result.returncode == 1
    assert "unexpected plain-English chat timeout for Stop: 30" in result.stderr


def test_codex_chat_command_shell_literal_survives_metacharacters(
    tmp_path: Path,
) -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "hook-command-execution",
        "current",
        "--base",
        str(tmp_path),
        "--json",
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    encoded_token = "a b'\\''c$HOME$(printf expanded)*?[ab]"
    expected_launcher = f"'{tmp_path}/{encoded_token}/.codex/hooks/plain-english.mjs'"
    assert report["command"] == (f"node {expected_launcher} hook chat --agent codex")
    assert report["status"] == 0, report["stderr"]
    assert json.loads(report["stdout"]) == ["hook", "chat", "--agent", "codex"]


def test_worktree_script_names_codex_and_vibe_links() -> None:
    script = REPO / ".claude/scripts/new-worktree.sh"
    if not script.exists():
        pytest.skip("snapshot-backed instruction layer is absent in CI")
    text = script.read_text()
    assert "for host_dir in .agents .codex .vibe .qwen" in text
    assert 'ln -s "../../$host_dir" "$WT/$host_dir"' in text
    assert (
        "for link in CLAUDE.md .claude/rules .claude/skills AGENTS.md .agents "
        ".codex .vibe .qwen" in text
    )


def test_worktreeinclude_does_not_copy_canonical_host_directories() -> None:
    include = _canonical_repo() / ".worktreeinclude"
    if not include.exists():
        pytest.skip("snapshot-backed instruction layer is absent in CI")
    entries = set(include.read_text().splitlines())
    assert {".agents/**", ".codex/**", ".vibe/**", ".qwen/**"}.isdisjoint(entries)


def test_installer_skips_linked_host_directories() -> None:
    text = (REPO / "scripts/agent-compat/install-local.mjs").read_text()
    assert "hostDirIsLinked(agentsDir)" in text
    assert "hostDirIsLinked(codexDir)" in text
    assert "hostDirIsLinked(vibeDir)" in text
    assert "hostDirIsLinked(qwenDir)" in text


def test_installer_does_not_write_through_linked_host_directories(
    tmp_path: Path,
) -> None:
    if not (REPO / ".claude/skills").exists():
        pytest.skip("snapshot-backed instruction layer is absent in CI")
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "install-linked-hosts",
        "--base",
        str(tmp_path),
        "--json",
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["ran_real_installer"] is True
    assert report["primary_bytes_unchanged"] == {
        ".agents": True,
        ".codex": True,
        ".vibe": True,
        ".qwen": True,
    }


def test_qwen_template_pins_models_without_an_inline_credential() -> None:
    template = json.loads((REPO / "config/agent-compat/qwen-settings.json").read_text())
    encoded = json.dumps(template)
    assert "env" not in template
    assert "sk-" not in encoded
    assert template["model"]["name"] == "qwen3.8-max"
    providers = {item["id"]: item for item in template["modelProviders"]["openai"]}
    assert providers["qwen3.8-max"]["envKey"] == "BAILIAN_TOKEN_PLAN_API_KEY"
    assert providers["qwen3.6-flash"]["envKey"] == "BAILIAN_TOKEN_PLAN_API_KEY"


@pytest.mark.parametrize("host", ["qwen", "codex", "vibe"])
def test_generated_host_secret_scan_rejects_each_host_fixture(host: str) -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "generated-host-secret-scan",
        host,
        "--json",
    )
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["violations"] == [host]
    assert "FERRY_SECRET_CANARY" not in result.stdout + result.stderr


def test_generated_host_secret_scan_allows_environment_references() -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "generated-host-secret-scan",
        "clean",
        "--json",
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"violations": []}


def test_qwen_checker_uses_the_active_worktree_template_source() -> None:
    source = (REPO / "scripts/agent-compat/check.mjs").read_text()
    section = source[
        source.index("function checkQwenState()") : source.index("// --- Check: Skills")
    ]
    assert "templateDir," in section
    assert "templateDir: join(canonicalRoot" not in section


@pytest.mark.skipif(NODE is None, reason="Node.js is required")
def test_node_entrypoints_run_when_invoked_through_symlinks(tmp_path: Path) -> None:
    adapter_link = tmp_path / "codex-hook-adapter.mjs"
    adapter_link.symlink_to(REPO / "scripts/agent-compat/codex-hook-adapter.mjs")
    adapter = subprocess.run(
        [NODE, str(adapter_link), "bogus-mode"],
        cwd=REPO,
        input="{}",
        capture_output=True,
        text=True,
        check=False,
    )
    assert adapter.returncode == 1
    assert "unknown mode" in adapter.stderr

    checker_link = tmp_path / "check.mjs"
    checker_link.symlink_to(REPO / "scripts/agent-compat/check.mjs")
    checker = _run(NODE, str(checker_link), "--ci")
    assert checker.returncode == 0, checker.stderr
    assert "All checks passed" in checker.stdout


@pytest.mark.skipif(NODE is None, reason="Node.js is required")
def test_codex_adapter_modes_run_from_a_non_repository_directory(
    tmp_path: Path,
) -> None:
    adapter = REPO / "scripts/agent-compat/codex-hook-adapter.mjs"
    event_cwd = tmp_path / "event"
    event_cwd.mkdir()
    clean = subprocess.run(
        [NODE, str(adapter), "stop"],
        cwd=event_cwd,
        input=json.dumps({"last_assistant_message": "Verification passed."}),
        capture_output=True,
        text=True,
        check=False,
    )
    assert clean.returncode == 0, clean.stderr
    assert clean.stdout == ""

    contradictory = subprocess.run(
        [NODE, str(adapter), "stop"],
        cwd=event_cwd,
        input=json.dumps({"last_assistant_message": "Fixed. Not yet tested."}),
        capture_output=True,
        text=True,
        check=False,
    )
    assert contradictory.returncode == 0, contradictory.stderr
    assert json.loads(contradictory.stdout) == {
        "decision": "block",
        "reason": (
            "Completion claimed but unfinished-work language detected. "
            "Finish the work, file it, or close the task."
        ),
    }

    unknown = subprocess.run(
        [NODE, str(adapter), "bogus-mode"],
        cwd=event_cwd,
        input="{}",
        capture_output=True,
        text=True,
        check=False,
    )
    assert unknown.returncode == 1
    assert 'codex-hook-adapter: unknown mode "bogus-mode"' in unknown.stderr


def test_brainstorm_hook_template_registers_codex_user_prompt_and_open_post() -> None:
    document = json.loads((REPO / "config/agent-compat/codex-hooks.json").read_text())
    submitted = document["hooks"]["UserPromptSubmit"]
    assert len(submitted) == 1
    assert "matcher" not in submitted[0]
    assert submitted[0]["hooks"] == [
        {
            "type": "command",
            "command": (
                'node "__PROJECT_ROOT__/scripts/agent-compat/codex-hook-adapter.mjs" user-prompt'
            ),
            "timeout": 10,
        }
    ]
    assert document["hooks"]["PostToolUse"][0]["matcher"] == "__POST_TOOL_MATCHER__"


def _run_copied_codex_adapter(
    root: Path,
    home: Path,
    event_cwd: Path,
    mode: str,
    payload: dict[str, object],
    *,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    assert NODE is not None
    return subprocess.run(
        [NODE, str(root / "scripts/agent-compat/codex-hook-adapter.mjs"), mode],
        cwd=event_cwd,
        env={**os.environ, "HOME": str(home), **(extra_env or {})},
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
    )


def test_codex_adapter_routes_brainstorm_events_from_unrelated_directory(
    tmp_path: Path,
) -> None:
    root, home, _ = _installer_checkout(tmp_path)
    event_cwd = tmp_path / "event"
    event_cwd.mkdir()
    source = root / "docs/reference.md"
    source.parent.mkdir(parents=True)
    source.write_text("Independent source line.\n")
    subprocess.run(["git", "-C", str(root), "add", "docs/reference.md"], check=True)
    requirements = root / "docs/plans/specs/feature.md"
    requirements.parent.mkdir(parents=True)
    requirements.write_text("# Requirements\n")
    edit_payload = {
        "session_id": "session-1",
        "turn_id": "turn-1",
        "tool_use_id": "edit-spec",
        "tool_name": "Write",
        "tool_input": {"file_path": str(requirements)},
        "tool_response": {"status": "ok"},
    }
    assert (
        _run_copied_codex_adapter(root, home, event_cwd, "post-tool", edit_payload).returncode == 0
    )

    prompt = _run_copied_codex_adapter(
        root,
        home,
        event_cwd,
        "user-prompt",
        {
            "session_id": "session-1",
            "turn_id": "turn-2",
            "prompt": "continue",
        },
    )
    assert prompt.returncode == 0, prompt.stderr

    tool_input = {"libraryId": "/nodejs/node", "query": "file replacement"}
    before = _run_copied_codex_adapter(
        root,
        home,
        event_cwd,
        "pre-tool",
        {
            "session_id": "session-1",
            "turn_id": "turn-2",
            "tool_use_id": "external-source",
            "tool_name": "mcp__context7__query-docs",
            "tool_input": tool_input,
        },
    )
    assert before.returncode == 0, before.stderr
    pending = root / "docs/plans/.brainstorm-evidence/receipts/pending"
    assert len(list(pending.iterdir())) == 1

    after = _run_copied_codex_adapter(
        root,
        home,
        event_cwd,
        "post-tool",
        {
            "session_id": "session-1",
            "turn_id": "turn-2",
            "tool_use_id": "external-source",
            "tool_name": "mcp__context7__query-docs",
            "tool_input": tool_input,
            "tool_response": {"content": [{"type": "text", "text": "Source line."}]},
        },
    )
    assert after.returncode == 0, after.stderr
    completed = root / "docs/plans/.brainstorm-evidence/receipts/completed"
    assert list(pending.iterdir()) == []
    assert len(list(completed.iterdir())) == 1


def test_codex_adapter_keeps_qmd_edit_behavior_and_stable_fields(tmp_path: Path) -> None:
    root, home, _ = _installer_checkout(tmp_path)
    event_cwd = tmp_path / "event"
    event_cwd.mkdir()
    marker = tmp_path / "qmd-event.json"
    hook = home / ".claude/hooks/qmd-live-update.sh"
    hook.parent.mkdir(parents=True)
    hook.write_text(
        '#!/bin/sh\nIFS= read -r payload\nprintf \'%s\' "$payload" > "$FERRY_QMD_MARKER"\n'
    )
    hook.chmod(0o755)
    target = root / "docs/example.md"
    target.parent.mkdir(parents=True)
    target.write_text("# Example\n")
    payload = {
        "session_id": "session-1",
        "turn_id": "turn-1",
        "tool_use_id": "edit-doc",
        "tool_name": "Write",
        "tool_input": {"file_path": str(target)},
        "tool_response": {"status": "ok"},
    }

    result = _run_copied_codex_adapter(
        root,
        home,
        event_cwd,
        "post-tool",
        payload,
        extra_env={"FERRY_QMD_MARKER": str(marker)},
    )

    assert result.returncode == 0, result.stderr
    observed = json.loads(marker.read_text())
    assert observed["session_id"] == "session-1"
    assert observed["tool_use_id"] == "edit-doc"
    assert observed["tool_input"]["file_path"] == str(target)


def test_codex_adapter_unfinished_guard_runs_before_brainstorm_stop(tmp_path: Path) -> None:
    root, home, _ = _installer_checkout(tmp_path)
    event_cwd = tmp_path / "event"
    event_cwd.mkdir()
    result = _run_copied_codex_adapter(
        root,
        home,
        event_cwd,
        "stop",
        {
            "session_id": "session-1",
            "turn_id": "turn-1",
            "last_assistant_message": (
                "The recommendation is complete, with remaining work not yet tested."
            ),
        },
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["reason"].startswith(
        "Completion claimed but unfinished-work language detected."
    )


@pytest.mark.parametrize("mode", ["user-prompt", "pre-tool", "post-tool", "stop"])
def test_codex_adapter_malformed_brainstorm_events_allow_from_unrelated_directory(
    tmp_path: Path, mode: str
) -> None:
    root, home, _ = _installer_checkout(tmp_path)
    event_cwd = tmp_path / "event"
    event_cwd.mkdir()

    result = _run_copied_codex_adapter(root, home, event_cwd, mode, {})

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


@pytest.mark.skipif(NODE is None, reason="Node.js is required")
def test_codex_adapter_symlink_uses_the_installed_project_root(tmp_path: Path) -> None:
    adapter_link = tmp_path / "codex-hook-adapter.mjs"
    adapter_link.symlink_to(REPO / "scripts/agent-compat/codex-hook-adapter.mjs")
    event_cwd = tmp_path / "event"
    event_cwd.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    result = subprocess.run(
        [NODE, str(adapter_link), "session-start"],
        cwd=event_cwd,
        env={**os.environ, "HOME": str(home)},
        input="{}",
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    context = output["hookSpecificOutput"]["additionalContext"]
    assert context.startswith("Discord Ferry v")


def test_checker_uses_the_installer_plain_english_command() -> None:
    text = (REPO / "scripts/agent-compat/check.mjs").read_text()
    assert "'npx'," not in text
    assert "'plain-english'," in text


def test_focused_codex_hook_check_stops_when_path_is_missing() -> None:
    result = _run("node", "scripts/agent-compat/check.mjs", "--check-codex-hooks")
    assert result.returncode == 1
    assert "--check-codex-hooks requires a path" in result.stdout
    assert "Warnings" not in result.stdout


@pytest.mark.skipif(NODE is None, reason="Node.js is required")
def test_focused_critique_contract_accepts_the_shared_policy(tmp_path: Path) -> None:
    skill, command = _critique_contract_fixture(tmp_path)

    result = _run(
        "node",
        "scripts/agent-compat/check.mjs",
        "--check-critique-contract",
        str(skill),
        str(command),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "All checks passed" in result.stdout


@pytest.mark.skipif(NODE is None, reason="Node.js is required")
@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("limit", "three-attempt limit"),
        ("claim", "pre-dispatch claim"),
        ("bash", "shell access"),
        ("evidence", "final evidence mode"),
    ],
)
def test_focused_critique_contract_rejects_policy_drift(
    tmp_path: Path,
    mutation: str,
    expected: str,
) -> None:
    skill, command = _critique_contract_fixture(tmp_path)
    if mutation == "limit":
        command.write_text(
            command.read_text().replace("CRITIQUE_BUDGET = 3", "CRITIQUE_BUDGET = 4")
        )
    else:
        old, new = {
            "claim": (" claim --design <path>", " inspect --design <path>"),
            "bash": ("Bash, ", ""),
            "evidence": ("evidence-investigation", "ordinary-review"),
        }[mutation]
        skill.write_text(skill.read_text().replace(old, new))

    result = _run(
        "node",
        "scripts/agent-compat/check.mjs",
        "--check-critique-contract",
        str(skill),
        str(command),
    )

    assert result.returncode == 1
    assert expected in result.stdout + result.stderr


@pytest.mark.skipif(NODE is None, reason="Node.js is required")
def test_focused_critique_contract_rejects_a_missing_command(tmp_path: Path) -> None:
    skill, command = _critique_contract_fixture(tmp_path)
    command.unlink()

    result = _run(
        "node",
        "scripts/agent-compat/check.mjs",
        "--check-critique-contract",
        str(skill),
        str(command),
    )

    assert result.returncode == 1
    assert "critique budget command missing" in result.stdout + result.stderr


@pytest.mark.skipif(NODE is None, reason="Node.js is required")
def test_installed_hosts_resolve_one_critique_skill() -> None:
    shared = REPO / ".claude/skills/df-critique/SKILL.md"
    bridge = REPO / ".agents/skills/df-critique/SKILL.md"
    if not shared.exists() or not bridge.exists():
        pytest.skip("snapshot-backed instruction layer is absent in CI")

    assert bridge.resolve() == shared.resolve()


def test_agent_check_requires_every_worktree_contract_path(tmp_path: Path) -> None:
    script, include, _, _, _ = _worktree_contract_fixture(tmp_path)
    result = _run(
        "node",
        "scripts/agent-compat/check.mjs",
        "--check-worktree-contract",
        str(script),
        str(include),
    )
    assert result.returncode == 1
    assert (
        "requires script, include, AGENTS.md, CLAUDE.md, and ship skill paths"
        in result.stdout + result.stderr
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("agents", "writer contract is invalid in AGENTS.md"),
        ("claude", "writer contract is invalid in CLAUDE.md"),
        ("ship", "reader contract is invalid in df-ship"),
    ],
)
def test_agent_check_rejects_shared_worktree_manifest_contract(
    tmp_path: Path, source: str, expected: str
) -> None:
    script, include, agents, claude, ship = _worktree_contract_fixture(tmp_path)
    changed = {"agents": agents, "claude": claude, "ship": ship}[source]
    changed.write_text("use .claude/change-manifest.md\n")
    result = _run(
        "node",
        "scripts/agent-compat/check.mjs",
        "--check-worktree-contract",
        str(script),
        str(include),
        str(agents),
        str(claude),
        str(ship),
    )
    assert result.returncode == 1
    assert expected in result.stdout + result.stderr


def test_agent_check_rejects_an_incomplete_linked_host_contract(
    tmp_path: Path,
) -> None:
    if not (REPO / ".claude/scripts/new-worktree.sh").exists():
        pytest.skip("snapshot-backed instruction layer is absent in CI")
    broken_script = tmp_path / "new-worktree.sh"
    broken_script.write_text("for host_dir in .agents .qwen; do\n  :\ndone\n")
    include = tmp_path / ".worktreeinclude"
    include.write_text("CLAUDE.md\n")
    contract_root = tmp_path / "contract"
    contract_root.mkdir()
    _, _, agents, claude, ship = _worktree_contract_fixture(contract_root)
    result = _run(
        "node",
        "scripts/agent-compat/check.mjs",
        "--check-worktree-contract",
        str(broken_script),
        str(include),
        str(agents),
        str(claude),
        str(ship),
    )
    assert result.returncode == 1
    assert "four canonical host links" in result.stdout + result.stderr


def test_linked_worktrees_keep_change_manifests_independent(tmp_path: Path) -> None:
    if not (REPO / ".claude/scripts/new-worktree.sh").exists():
        pytest.skip("snapshot-backed instruction layer is absent in CI")
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "worktree-manifest-isolation",
        "--base",
        str(tmp_path),
        "--json",
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report == {
        "distinct_paths": True,
        "first_unchanged_after_second_write": True,
        "primary_unchanged": True,
        "legacy_ignored": True,
        "parent_created_twice": True,
        "statuses_clean": True,
        "shared_instruction_links": True,
        "existing_worktree_reused": True,
    }


def test_codex_bootstrap_preserves_unowned_global_state(tmp_path: Path) -> None:
    home = tmp_path / "home"
    codex = home / ".codex"
    codex.mkdir(parents=True)
    config = codex / "config.toml"
    config.write_text(
        'model = "personal-model"\nmodel_reasoning_effort = "low"\n'
        'approval_policy = "never"\nsandbox_mode = "read-only"\nweb_search = "live"\n'
        '[projects."/another/repo"]\ntrust_level = "trusted"\n'
    )
    config.chmod(0o600)
    command = [
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "bootstrap",
        "--home",
        str(home),
        "--root",
        str(REPO),
        "--json",
    ]
    first = subprocess.run(command, cwd=REPO, capture_output=True, text=True, check=False)
    first_config = config.read_text()
    second = subprocess.run(command, cwd=REPO, capture_output=True, text=True, check=False)
    assert first.returncode == second.returncode == 0
    assert config.read_text() == first_config
    assert 'model = "personal-model"' in first_config
    assert 'model_reasoning_effort = "low"' in first_config
    assert 'approval_policy = "never"' in first_config
    assert 'sandbox_mode = "read-only"' in first_config
    assert 'web_search = "live"' in first_config
    assert '[projects."/another/repo"]' in first_config
    assert first_config.count(f'[projects."{REPO}"]') == 1
    assert config.stat().st_mode & 0o777 == 0o600


def test_codex_bootstrap_dry_run_writes_nothing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "bootstrap",
        "--home",
        str(home),
        "--root",
        str(REPO),
        "--dry-run",
        "--json",
    )
    assert result.returncode == 0
    assert not home.exists()


@pytest.mark.parametrize(
    "body",
    [
        'projects."{root}".trust_level = "trusted"\n',
        '[projects]\n"{root}" = {{ trust_level = "trusted" }}\n',
    ],
)
def test_codex_bootstrap_refuses_noncanonical_existing_project_syntax(
    tmp_path: Path,
    body: str,
) -> None:
    home = tmp_path / "home"
    config = home / ".codex/config.toml"
    config.parent.mkdir(parents=True)
    original = body.format(root=REPO).encode()
    config.write_bytes(original)
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "bootstrap",
        "--home",
        str(home),
        "--root",
        str(REPO),
        "--json",
    )
    assert result.returncode == 1
    assert config.read_bytes() == original
    assert "normalize the existing Ferry project trust entry" in result.stderr


def test_codex_bootstrap_interruption_preserves_original_bytes(tmp_path: Path) -> None:
    home = tmp_path / "home"
    config = home / ".codex/config.toml"
    config.parent.mkdir(parents=True)
    original = b'model = "personal"\n'
    config.write_bytes(original)
    interrupted = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "bootstrap",
        "--home",
        str(home),
        "--root",
        str(REPO),
        "--fail-before-rename",
        "--json",
    )
    assert interrupted.returncode == 1
    assert config.read_bytes() == original
    rerun = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "bootstrap",
        "--home",
        str(home),
        "--root",
        str(REPO),
        "--json",
    )
    assert rerun.returncode == 0
    assert config.read_text().count(f'[projects."{REPO}"]') == 1


def test_codex_bootstrap_validates_reconciled_toml_before_rename(tmp_path: Path) -> None:
    home = tmp_path / "home"
    config = home / ".codex/config.toml"
    config.parent.mkdir(parents=True)
    original = (f'[projects."{REPO}"]\ntrust_level.kind = "noncanonical-but-valid"\n').encode()
    config.write_bytes(original)
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "bootstrap",
        "--home",
        str(home),
        "--root",
        str(REPO),
        "--json",
    )
    assert result.returncode == 1
    assert config.read_bytes() == original


def test_codex_bootstrap_owns_globals_for_the_primary_checkout(tmp_path: Path) -> None:
    home = tmp_path / "home"
    worktree = tmp_path / "linked-worktree"
    canonical = tmp_path / "primary"
    worktree.mkdir()
    canonical.mkdir()
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "bootstrap",
        "--home",
        str(home),
        "--root",
        str(worktree),
        "--canonical-root",
        str(canonical),
        "--json",
    )
    assert result.returncode == 0, result.stderr
    config = (home / ".codex/config.toml").read_text()
    assert f'[projects."{canonical}"]' in config
    assert str(worktree) not in config


def test_bootstrap_creates_one_vault_scoped_reviewer_agent(tmp_path: Path) -> None:
    fake = tmp_path / "pass-cli"
    log = tmp_path / "calls"
    state = tmp_path / "agent-created"
    fake.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> {log}\n"
        "if [ \"$1 $2 $3\" = 'agent create --help' ]; then "
        "printf 'NAME --expiration 3m --vault'; "
        "elif [ \"$1 $2\" = 'vault list' ]; then "
        'printf \'{"vaults":[{"name":"PortalPilot"}]}\'; '
        "elif [ \"$1 $2\" = 'agent list' ]; then "
        f"if [ -f {state} ]; then "
        'printf \'[{"name":"discord-ferry-reviewers","expire_time":1999999999}]\'; '
        "else printf '[]'; fi; "
        "elif [ \"$1 $2\" = 'agent create' ]; then "
        f"touch {state}; "
        'printf \'{"agent":{"credentials":{"token":"'
        'pst_testfixture12345678901234567890::tokenkey"}},"instruction":"test"}\'; fi\n'
    )
    fake.chmod(0o755)
    home = tmp_path / "home"
    command = [
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "bootstrap-proton",
        "--home",
        str(home),
        "--root",
        str(REPO),
        "--pass-cli",
        str(fake),
        "--json",
    ]
    first = subprocess.run(command, cwd=REPO, capture_output=True, text=True, check=False)
    second = subprocess.run(command, cwd=REPO, capture_output=True, text=True, check=False)
    assert first.returncode == second.returncode == 0
    calls = log.read_text()
    assert (
        calls.count("agent create discord-ferry-reviewers --expiration 3m --vault PortalPilot") == 1
    )
    token = home / ".config/discord-ferry/reviewer-agent.pat"
    assert token.stat().st_mode & 0o777 == 0o600
    assert "pst_testfixture" not in first.stdout + first.stderr + second.stdout + second.stderr


def test_bootstrap_renews_an_expired_reviewer_agent(tmp_path: Path) -> None:
    fake = tmp_path / "pass-cli"
    log = tmp_path / "calls"
    fake.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> {log}\n"
        "if [ \"$1 $2 $3\" = 'agent create --help' ]; then "
        "printf 'NAME --expiration 3m --vault'; "
        "elif [ \"$1 $2\" = 'vault list' ]; then "
        'printf \'[{"name":"PortalPilot"}]\'; '
        "elif [ \"$1 $2\" = 'agent list' ]; then "
        'printf \'[{"name":"discord-ferry-reviewers","expire_time":1}]\'; '
        "elif [ \"$1 $2\" = 'agent renew' ]; then "
        'printf \'{"token":"pst_newfixture123456789012345678901234567890"}\'; fi\n'
    )
    fake.chmod(0o755)
    home = tmp_path / "home"
    token = home / ".config/discord-ferry/reviewer-agent.pat"
    token.parent.mkdir(parents=True)
    token.write_text("pst_oldfixture123456789012345678901234567890")

    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "bootstrap-proton",
        "--home",
        str(home),
        "--root",
        str(REPO),
        "--pass-cli",
        str(fake),
        "--json",
    )

    assert result.returncode == 0, result.stderr
    assert "agent renew --expiration 3m --output json discord-ferry-reviewers" in log.read_text()
    assert token.read_text().startswith("pst_newfixture")
    assert "pst_newfixture" not in result.stdout + result.stderr


def test_bootstrap_refuses_an_orphaned_reviewer_token(tmp_path: Path) -> None:
    home = tmp_path / "home"
    token = home / ".config/discord-ferry/reviewer-agent.pat"
    token.parent.mkdir(parents=True)
    token.write_text("pst_testfixture12345678901234567890::tokenkey")
    fake = tmp_path / "pass-cli"
    fake.write_text(
        "#!/bin/sh\n"
        "if [ \"$1 $2 $3\" = 'agent create --help' ]; then "
        "printf 'NAME --expiration 3m --vault'; "
        "elif [ \"$1 $2\" = 'vault list' ]; then "
        'printf \'[{"name":"PortalPilot"}]\'; '
        "elif [ \"$1 $2\" = 'agent list' ]; then printf '[]'; fi\n"
    )
    fake.chmod(0o755)
    result = subprocess.run(
        [
            "node",
            "tests/fixtures/agent_compat_runner.mjs",
            "bootstrap-proton",
            "--home",
            str(home),
            "--root",
            str(REPO),
            "--pass-cli",
            str(fake),
            "--json",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "pass-cli agent renew" in result.stderr
    assert "pst_testfixture" not in result.stdout + result.stderr


def test_context7_agent_is_item_limited_and_repeatable(tmp_path: Path) -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "context7-agent",
        "create-repeat",
        "--home",
        str(tmp_path),
        "--json",
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["first"] == {"created": True, "renewed": False, "recovered": False}
    assert report["second"] == {"created": False, "renewed": False, "recovered": False}
    assert report["grant"] == [
        "agent",
        "access",
        "grant",
        "discord-ferry-context7",
        "--vault-name",
        "Personal",
        "--item-title",
        "Context7 API Key",
        "--role",
        "viewer",
    ]
    assert report["grant_count"] == report["create_count"] == 1
    assert report["field_reads"] == 2
    assert report["token_mode"] == report["ownership_mode"] == 0o600
    assert report["ownership"] == {
        "version": 2,
        "agent_id": "context7-agent-id-1",
        "agent_name": "discord-ferry-context7",
        "state": "ready",
        "share_id": "context7-item-share-id",
        "item_id": "context7-item-id",
        "grant_sha256": report["ownership"]["grant_sha256"],
    }
    assert len(report["ownership"]["grant_sha256"]) == 64
    assert "FERRY_SECRET_CANARY" not in result.stdout + result.stderr


def _context7_agent_fixture(tmp_path: Path, fixture: str) -> subprocess.CompletedProcess[str]:
    return _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "context7-agent",
        fixture,
        "--home",
        str(tmp_path),
        "--json",
    )


def test_context7_agent_renews_without_repeating_the_item_grant(tmp_path: Path) -> None:
    result = _context7_agent_fixture(tmp_path, "expired")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["first"] == {"created": True, "renewed": False, "recovered": False}
    assert report["second"] == {"created": False, "renewed": True, "recovered": False}
    assert report["create_count"] == report["grant_count"] == report["renew_count"] == 1
    assert report["field_reads"] == 2


def test_context7_agent_recovers_its_matching_interrupted_creation(tmp_path: Path) -> None:
    result = _context7_agent_fixture(tmp_path, "interrupted")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["first"] is None
    assert report["second"] == {"created": True, "renewed": False, "recovered": True}
    assert report["delete_calls"] == [["agent", "delete", "discord-ferry-context7"]]
    assert report["create_count"] == report["grant_count"] == 2
    assert report["ownership"]["agent_id"] == "context7-agent-id-2"


def test_context7_agent_recovers_when_post_create_list_fails(tmp_path: Path) -> None:
    result = _context7_agent_fixture(tmp_path, "post-create-list-failure")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["first"] is None
    assert report["second"] == {"created": True, "renewed": False, "recovered": True}
    assert report["delete_calls"] == [["agent", "delete", "discord-ferry-context7"]]
    assert report["create_count"] == 2
    assert report["grant_count"] == 1
    assert report["ownership"]["agent_id"] == "context7-agent-id-2"


@pytest.mark.parametrize("fixture", ["unmanaged", "duplicate-agent", "duplicate-item"])
def test_context7_agent_refuses_ambiguous_remote_state(tmp_path: Path, fixture: str) -> None:
    result = _context7_agent_fixture(tmp_path, fixture)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["error"]
    assert report["create_count"] == report["grant_count"] == 0
    assert report["delete_calls"] == []
    assert report["field_reads"] == 0


@pytest.mark.parametrize(
    "fixture, expected",
    [
        ("unsafe-token", "context7-agent.pat must have mode 0600"),
        ("unsafe-ownership", "context7-agent.json must have mode 0600"),
        ("invalid-ownership", "Context7 ownership record is invalid"),
    ],
)
def test_context7_agent_refuses_unsafe_local_state(
    tmp_path: Path, fixture: str, expected: str
) -> None:
    result = _context7_agent_fixture(tmp_path, fixture)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["error"] == expected
    assert report["create_count"] == report["grant_count"] == 1
    assert report["renew_count"] == 0
    assert report["delete_calls"] == []


def test_bootstrap_provisions_reviewer_and_context7_credentials() -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "bootstrap-provisioners",
        "--json",
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "calls": [
            ["reviewer", "/fixture/home", "/fixture/pass-cli"],
            ["context7", "/fixture/home", "/fixture/pass-cli"],
        ],
        "report": {
            "reviewer": {"created": False, "renewed": False},
            "context7": {"created": True, "renewed": False, "recovered": False},
        },
    }


def test_bootstrap_returns_value_free_claude_context7_commands(tmp_path: Path) -> None:
    home = tmp_path / "home with spaces"
    root = tmp_path / "project"
    root.mkdir()
    protected_config = root / ".mcp.json"
    protected_config.write_bytes(b'FERRY_PROTECTED_CANARY\x00{"do_not_parse":true}\n')
    protected_config.chmod(0o640)
    before = (
        protected_config.read_bytes(),
        protected_config.stat().st_mode,
        protected_config.stat().st_mtime_ns,
    )

    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "bootstrap-claude-handoff",
        "--home",
        str(home),
        "--root",
        str(root),
        "--json",
    )

    assert result.returncode == 0, result.stderr
    assert (
        protected_config.read_bytes(),
        protected_config.stat().st_mode,
        protected_config.stat().st_mtime_ns,
    ) == before
    document = json.loads(result.stdout)
    launcher = str(home / ".local/share/discord-ferry/reviewer-runtime/current/context7-mcp.mjs")
    action = document["report"]["claudeContext7"]
    assert action["launcher"] == launcher
    assert action["commands"] == [
        ["claude", "mcp", "remove", "--scope", "project", "context7"],
        [
            "claude",
            "mcp",
            "add",
            "--scope",
            "project",
            "context7",
            "--",
            "node",
            launcher,
        ],
    ]
    assert "claude mcp remove --scope project context7" in document["human"]
    assert "claude mcp add --scope project context7 -- node" in document["human"]
    assert "Fresh Claude Code sessions" in document["human"]
    assert "FERRY_PROTECTED_CANARY" not in result.stdout + result.stderr
    assert "CONTEXT7_API_KEY" not in result.stdout + result.stderr


def _context7_launcher_fixture(fixture: str) -> subprocess.CompletedProcess[str]:
    return _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "context7-launch",
        fixture,
        "--json",
    )


def test_context7_launcher_passes_only_the_required_environment() -> None:
    result = _context7_launcher_fixture("success")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["command"] == "npx"
    assert report["args"] == ["-y", "@upstash/context7-mcp"]
    assert report["stdio"] == "inherit"
    assert report["child_env_names"] == [
        "CONTEXT7_API_KEY",
        "HOME",
        "LANG",
        "NODE_EXTRA_CA_CERTS",
        "PATH",
        "TMPDIR",
    ]
    assert report["child_has_context7_key"] is True
    assert report["child_has_parent_canary"] is False
    assert report["field_descriptor"]["shareId"] == "context7-item-share-id"
    assert report["field_descriptor"]["itemId"] == "context7-item-id"
    assert "vaultName" not in report["field_descriptor"]
    assert "itemTitle" not in report["field_descriptor"]
    assert report["result"] == {"status": 23, "signal": None, "ready": False}
    assert "FERRY_SECRET_CANARY" not in result.stdout + result.stderr


def test_context7_launcher_forwards_signals_and_removes_listeners() -> None:
    result = _context7_launcher_fixture("signal")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["result"] == {"status": 0, "signal": "SIGTERM", "ready": False}
    assert report["forwarded"] == ["SIGTERM"]
    assert report["remaining_signal_listeners"] == 0


def test_context7_launcher_stops_before_spawn_when_key_access_fails() -> None:
    result = _context7_launcher_fixture("credential-failure")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["error"] == "Context7 credential unavailable"
    assert report["spawn_count"] == 0
    assert "FERRY_SECRET_CANARY" not in result.stdout + result.stderr


def test_context7_launcher_check_reads_the_key_without_starting_child() -> None:
    result = _context7_launcher_fixture("check")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["result"] == {"status": 0, "signal": None, "ready": True}
    assert report["spawn_count"] == 0


def test_static_readiness_returns_named_value_free_records(tmp_path: Path) -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "readiness-static",
        "healthy",
        "--root",
        str(REPO),
        "--home",
        str(tmp_path),
        "--json",
    )
    assert result.returncode == 0, result.stderr
    assert str(tmp_path) not in result.stdout
    report = json.loads(result.stdout)
    assert report["mode"] == "static"
    assert {record["id"] for record in report["records"]} >= {
        "codex-version",
        "project-config",
        "global-trust",
        "instructions",
        "skills",
        "hooks",
        "roles",
        "mcp-registration",
        "worktree-parity",
        "reviewer-clients",
        "context7-credential",
    }
    assert all(
        set(record) == {"id", "class", "status", "duration_ms", "remediation", "details"}
        for record in report["records"]
    )
    records = {record["id"]: record for record in report["records"]}
    assert records["project-config"]["details"] == {
        "pins": 5,
        "network_hosts": 2,
    }
    assert records["hooks"]["details"] == {
        "native_chat_hooks": 2,
        "brainstorm_hooks": [
            "UserPromptSubmit",
            "PreToolUse",
            "PostToolUse",
            "Stop",
        ],
        "claude_brainstorm_hooks": [
            "UserPromptSubmit",
            "PreToolUse",
            "PostToolUse",
            "PostToolUseFailure",
            "Stop",
        ],
        "timeout_seconds": 60,
    }
    assert records["worktree-parity"]["details"] == {
        "canonical_hosts": 4,
        "manifest_contract_sources": 3,
    }


def test_static_readiness_reports_context7_credential_without_values(tmp_path: Path) -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "readiness-static",
        "healthy",
        "--root",
        str(REPO),
        "--home",
        str(tmp_path),
        "--json",
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    record = next(item for item in report["records"] if item["id"] == "context7-credential")
    assert record["status"] == "ok"
    assert record["details"] == {"credential": "ready"}
    assert "FERRY_SECRET_CANARY" not in result.stdout + result.stderr


def test_static_readiness_redacts_context7_credential_failure(tmp_path: Path) -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "readiness-static",
        "context7-unavailable",
        "--root",
        str(REPO),
        "--home",
        str(tmp_path),
        "--json",
    )
    assert result.returncode == 1
    report = json.loads(result.stdout)
    record = next(item for item in report["records"] if item["id"] == "context7-credential")
    assert record["status"] == "fail"
    assert record["details"] == {
        "reason": "The protected Context7 credential or launcher is unavailable"
    }
    assert record["remediation"] == "Run node scripts/agent-compat/codex-bootstrap.mjs."
    assert "FERRY_SECRET_CANARY" not in result.stdout + result.stderr


def test_brainstorm_readiness_live_sequence_is_value_free_and_tree_clean(
    tmp_path: Path,
) -> None:
    root, _, _ = _installer_checkout(tmp_path)
    event_cwd = tmp_path / "event"
    event_cwd.mkdir()
    changelog = root / "CHANGELOG.md"
    changelog.write_text("# Changelog\n")
    subprocess.run(["git", "-C", str(root), "add", "CHANGELOG.md"], check=True)
    before = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=all"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "brainstorm-readiness-live",
        "--root",
        str(root),
        "--event-cwd",
        str(event_cwd),
        "--json",
    )

    assert result.returncode == 0, result.stderr
    assert set(json.loads(result.stdout).values()) == {"ok"}
    assert "# Requirements" not in result.stdout
    assert "# Changelog" not in result.stdout
    after = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=all"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert after == before


def test_static_readiness_accepts_semantic_single_quoted_trust(tmp_path: Path) -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "readiness-static",
        "single-quoted-trust",
        "--root",
        str(REPO),
        "--home",
        str(tmp_path),
        "--json",
    )

    assert result.returncode == 0, result.stderr
    records = {record["id"]: record for record in json.loads(result.stdout)["records"]}
    assert records["global-trust"]["status"] == "ok"


@pytest.mark.parametrize(
    ("fixture", "failed_id"),
    [
        ("missing-codex", "codex-version"),
        ("missing-role", "roles"),
        ("missing-skill-bridge", "skills"),
        ("missing-hook", "hooks"),
        ("missing-brainstorm-prompt", "hooks"),
        ("broken-brainstorm-matcher", "hooks"),
        ("missing-brainstorm-failed-tool", "hooks"),
        ("missing-tool-server", "mcp-registration"),
        ("missing-client", "reviewer-clients"),
        ("misdistributed-hook", "hooks"),
        ("stale-wrapper", "hooks"),
        ("commented-model", "project-config"),
        ("missing-network-grant", "project-config"),
        ("disabled-network-proxy", "project-config"),
        ("incomplete-network-policy", "project-config"),
        ("widened-network-policy", "project-config"),
        ("local-binding-network-policy", "project-config"),
        ("missing-review-boundary", "instructions"),
    ],
)
def test_static_readiness_scopes_each_missing_prerequisite(
    tmp_path: Path,
    fixture: str,
    failed_id: str,
) -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "readiness-static",
        fixture,
        "--root",
        str(REPO),
        "--home",
        str(tmp_path),
        "--json",
    )
    report = json.loads(result.stdout)
    failed = [record for record in report["records"] if record["status"] == "fail"]
    assert result.returncode == 1
    assert [record["id"] for record in failed] == [failed_id]
    assert failed[0]["remediation"]


def test_live_readiness_does_not_translate_network_timeout_to_auth_failure() -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "readiness-live",
        "doctor-network-timeout",
        "--json",
    )
    report = json.loads(result.stdout)
    records = {record["id"]: record for record in report["records"]}
    assert result.returncode == 1
    assert report["overall"] == "incomplete"
    assert records["codex-auth"]["status"] == "ok"
    assert records["codex-provider-smoke"]["status"] == "ok"
    assert records["codex-update-probe"]["status"] == "warning"
    assert "unauthenticated" not in result.stdout.lower()


@pytest.mark.parametrize(
    ("fixture", "record_id", "status"),
    [
        ("doctor-auth-fail", "codex-auth", "fail"),
        ("doctor-install-fail", "codex-install", "fail"),
        ("doctor-config-fail", "codex-config", "fail"),
        ("doctor-git-fail", "codex-git", "fail"),
        ("doctor-sandbox-fail", "codex-sandbox", "fail"),
        ("provider-timeout", "codex-provider-smoke", "fail"),
        ("missing-tool-server", "codex-provider-smoke", "fail"),
        ("stale-version", "codex-update-probe", "warning"),
    ],
)
def test_live_readiness_keeps_failures_on_their_named_record(
    fixture: str,
    record_id: str,
    status: str,
) -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "readiness-live",
        fixture,
        "--json",
    )
    report = json.loads(result.stdout)
    changed = [record for record in report["records"] if record["status"] != "ok"]
    assert result.returncode == 1
    assert [(record["id"], record["status"]) for record in changed] == [(record_id, status)]


def test_reviewer_readiness_names_vibe_and_qwen_without_claude() -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "readiness-reviewers",
        "healthy",
        "--json",
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    records = {record["id"]: record for record in report["records"]}
    assert set(records) == {"vibe-reviewer", "qwen-reviewer"}
    assert records["vibe-reviewer"]["details"] == {"model": "zai-glm-5-2"}
    assert records["qwen-reviewer"]["details"] == {"model": "qwen3.8-max"}
    assert report["calls"] == {"vibe": 1, "qwen": 1}
    assert "sonnet" not in result.stdout.lower()
    assert "claude" not in result.stdout.lower()


@pytest.mark.parametrize(
    ("fixture", "failed_id"),
    [
        ("vibe-fails", "vibe-reviewer"),
        ("qwen-fails", "qwen-reviewer"),
        ("qwen-wrong-model", "qwen-reviewer"),
        ("qwen-empty", "qwen-reviewer"),
        ("qwen-denied", "qwen-reviewer"),
    ],
)
def test_reviewer_readiness_scopes_value_free_failures(
    fixture: str,
    failed_id: str,
) -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "readiness-reviewers",
        fixture,
        "--json",
    )
    report = json.loads(result.stdout)
    changed = [record for record in report["records"] if record["status"] != "ok"]
    assert result.returncode == 1
    assert [record["id"] for record in changed] == [failed_id]
    assert changed[0]["details"] == {"reason": "Exact reviewer probe failed"}
    assert report["calls"] == {"vibe": 1, "qwen": 1}
    assert "FERRY_COMMAND_CANARY" not in result.stdout + result.stderr


def test_reviewer_readiness_redacts_adapter_failures() -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "readiness-reviewers",
        "all-fail-canary",
        "--json",
    )
    assert result.returncode == 1
    assert "FERRY_SECRET_CANARY" not in result.stdout + result.stderr


def test_live_worktree_probe_checks_both_stop_events() -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "readiness-worktree",
        "worktree-healthy",
        "--json",
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    records = {record["id"]: record for record in report["records"]}
    assert records["primary-session"]["status"] == "ok"
    assert records["worktree-session"]["status"] == "ok"
    assert records["hook-session-start"]["status"] == "ok"
    assert records["hook-pre-tool-allow"]["status"] == "ok"
    assert records["hook-pre-tool-block"]["status"] == "ok"
    assert records["hook-post-tool"]["status"] == "ok"
    assert records["brainstorm-prompt-activation"]["status"] == "ok"
    assert records["brainstorm-evidence-pre-tool"]["status"] == "ok"
    assert records["brainstorm-evidence-result"]["status"] == "ok"
    assert records["brainstorm-incomplete-stop"]["status"] == "ok"
    assert records["brainstorm-complete-stop"]["status"] == "ok"
    assert records["brainstorm-outside-directory"]["status"] == "ok"
    assert records["stop-main-agent"]["details"]["timeout_seconds"] == 60
    assert records["stop-child-agent"]["details"]["timeout_seconds"] == 60
    for record_id in ("stop-main-agent", "stop-child-agent"):
        details = records[record_id]["details"]
        assert details["owner_root"] == "/fixture/primary"
        assert details["event_cwd"] == "/fixture/event"
        assert "git rev-parse" not in details["command"]
    primary = records["primary-session"]["details"]
    worktree = records["worktree-session"]["details"]
    assert primary["markers"] == worktree["markers"]
    assert primary["tree_hash_before"] == primary["tree_hash_after"]
    assert worktree["tree_hash_before"] == worktree["tree_hash_after"]
    assert records["role-coordinator"]["details"] == {
        "model": "gpt-5.6-sol",
        "sandbox": "workspace-write",
        "selected": True,
    }
    assert records["role-reviewer"]["details"] == {
        "model": "gpt-5.6-terra",
        "sandbox": "read-only",
        "selected": True,
    }
    assert records["role-explorer"]["details"] == {
        "model": "gpt-5.6-terra",
        "sandbox": "read-only",
        "selected": True,
    }
    assert records["role-locator"]["details"] == {
        "model": "gpt-5.6-luna",
        "sandbox": "read-only",
        "selected": True,
    }


@pytest.mark.parametrize(
    ("fixture", "failed_id"),
    [
        ("missing-links", "worktree-session"),
        ("hook-nonzero", "hook-post-tool"),
        ("brainstorm-missing-prompt", "brainstorm-prompt-activation"),
        ("brainstorm-broken-matcher", "brainstorm-evidence-pre-tool"),
        ("brainstorm-adapter-inside-repo", "brainstorm-outside-directory"),
        ("stop-ten-second", "stop-main-agent"),
        ("stop-timeout", "stop-child-agent"),
        ("stop-event-inside-owner", "stop-main-agent"),
        ("wrong-role", "role-locator"),
    ],
)
def test_worktree_probe_scopes_parity_failures(fixture: str, failed_id: str) -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "readiness-worktree",
        fixture,
        "--json",
    )
    report = json.loads(result.stdout)
    failed = [record["id"] for record in report["records"] if record["status"] == "fail"]
    assert result.returncode == 1
    assert failed == [failed_id]


def _setup_dispatch_fixture(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    root = tmp_path / "repo"
    compat = root / "scripts/agent-compat"
    compat.mkdir(parents=True)
    shutil.copy2(REPO / "scripts/codex-setup.sh", root / "scripts/codex-setup.sh")
    log = tmp_path / "calls"
    (root / "scripts/agent-install.sh").write_text(
        '#!/bin/sh\nprintf "install\\n" >> "$FERRY_SETUP_LOG"\n'
    )
    (root / "scripts/agent-install.sh").chmod(0o755)
    (compat / "codex-bootstrap.mjs").write_text(
        "import {appendFileSync} from 'node:fs';"
        "appendFileSync(process.env.FERRY_SETUP_LOG, 'bootstrap\\n');"
    )
    (compat / "codex-readiness.mjs").write_text(
        "import {appendFileSync} from 'node:fs';"
        "const live=process.argv.includes('--live');"
        "const worktree=process.argv.includes('--worktree');"
        "const reviewers=process.argv.includes('--reviewers');"
        "appendFileSync(process.env.FERRY_SETUP_LOG, "
        "live ? `live:${worktree}:${reviewers}\\n` : "
        "'static-readiness\\n');"
    )
    return root, log, {**os.environ, "FERRY_SETUP_LOG": str(log)}


def test_codex_setup_orders_bootstrap_install_and_readiness(tmp_path: Path) -> None:
    root, log, env = _setup_dispatch_fixture(tmp_path)
    result = subprocess.run(
        ["bash", str(root / "scripts/codex-setup.sh")],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert log.read_text().splitlines() == [
        "bootstrap",
        "install",
        "static-readiness",
    ]


def test_codex_setup_live_adds_live_readiness(tmp_path: Path) -> None:
    root, log, env = _setup_dispatch_fixture(tmp_path)
    result = subprocess.run(
        ["bash", str(root / "scripts/codex-setup.sh"), "--live"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert log.read_text().splitlines() == [
        "bootstrap",
        "install",
        "static-readiness",
        "live:true:false",
    ]


def test_codex_setup_second_run_is_byte_identical(tmp_path: Path) -> None:
    required_snapshot_paths = [
        REPO / "AGENTS.md",
        REPO / "CLAUDE.md",
        REPO / ".claude/skills",
        REPO / ".claude/scripts/new-worktree.sh",
        _canonical_repo() / ".worktreeinclude",
    ]
    if not all(path.exists() for path in required_snapshot_paths):
        pytest.skip("snapshot-backed instruction layer is absent in CI")
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "setup-repeat-real",
        "--base",
        str(tmp_path),
        "--json",
    )
    report = json.loads(result.stdout)
    assert result.returncode == 0, result.stderr
    assert report["ran_real_setup"] is True
    assert report["first_hashes"] == report["second_hashes"]
    assert report["trust_entries"] == 1
    assert report["rule_entries"] == 4
    assert report["leftover_temp_or_backup_files"] == []


def test_review_contract_rejects_bad_findings_and_redacts_children() -> None:
    result = _run("node", "scripts/agent-compat/review-contract.mjs", "--self-test")
    assert result.returncode == 0, result.stderr
    assert "all checks passed" in result.stderr
    assert "plan-gate fixtures: exercised" in result.stderr


def test_review_contract_redacts_an_injected_child_failure() -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "review-contract",
        "canary-child-error",
        "--json",
    )
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["stage"] == "child-exit"
    assert "FERRY_SECRET_CANARY" not in result.stdout + result.stderr


def test_review_contract_entrypoint_runs_through_a_symlink(tmp_path: Path) -> None:
    link = tmp_path / "review-contract.mjs"
    link.symlink_to(REPO / "scripts/agent-compat/review-contract.mjs")
    result = _run("node", str(link), "--self-test")
    assert result.returncode == 0, result.stderr
    assert "review-contract self-test: all checks passed" in result.stderr


def test_review_contract_requires_distinct_structured_verification_outcomes() -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "review-schema",
        "--json",
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "structured": True,
        "prose": False,
        "identical": False,
        "overlapping": False,
        "exclusive_same_exit": True,
        "unreachable_non_search_exit": False,
        "empty_text": False,
        "invalid_exit": False,
    }


def test_provider_records_preserve_structurally_valid_denied_commands(
    tmp_path: Path,
) -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "review-provider-authorization",
        "--tmp",
        str(tmp_path),
        "--json",
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "record_valid": {
            "safe": True,
            "shell": True,
            "python": True,
            "missing": True,
            "outside": True,
            "linked": True,
        },
        "command_authorized": {
            "safe": True,
            "shell": False,
            "python": False,
            "missing": False,
            "outside": False,
            "linked": False,
        },
        "commands_run": ["rg"],
        "verdicts": ["CONFIRMED", "INCONCLUSIVE"],
        "ensemble_status": "valid",
        "plan_status": "valid",
        "plan_accepted": True,
    }


def test_plan_gate_rejects_a_malformed_accepted_finding() -> None:
    input_sha256 = "a" * 64
    finding = {
        "severity": "blocker",
        "category": "correctness",
        "file": "docs/plan.md",
        "line": None,
        "description": "bad",
        "suggestion": "fix",
        "verification": {
            "command": "true",
            "confirms_if": {
                "exit_code": 0,
                "stdout_contains": "present",
                "stdout_excludes": None,
            },
            "refutes_if": {
                "exit_code": 1,
                "stdout_contains": None,
                "stdout_excludes": None,
            },
        },
    }
    accepted = {
        "slot": "plan-qwen",
        "status": "valid",
        "resolved_model": "qwen3.8-max",
        "session_id": "session",
        "substitution_for": None,
        "summary": "bad",
        "confidence": "high",
        "findings": [finding],
    }
    route = {"accepted": accepted, "attempts": [accepted], "input_sha256": input_sha256}
    result = _run(
        "node",
        "scripts/agent-compat/review-contract.mjs",
        "--evaluate-plan-record",
        json.dumps(route),
        json.dumps(["CONFIRMED"]),
        input_sha256,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout)["ready"] is False


def test_plan_gate_rejects_a_route_for_different_plan_content() -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "plan-stale-route",
        "--json",
    )

    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report == {
        "ready": False,
        "reason": "plan review input mismatch",
        "minor_findings": [],
    }


def test_codex_review_keeps_bare_findings_and_design_mode() -> None:
    source = (REPO / "scripts/agent-compat/codex-review.mjs").read_text()
    assert "from './review-contract.mjs'" in source
    result = _run("node", "scripts/agent-compat/codex-review.mjs", "--self-test")
    assert result.returncode == 0, result.stderr
    assert "design instruction carries design directives" in result.stderr


def test_proton_helper_uses_isolated_session_and_returns_only_the_field(
    tmp_path: Path,
) -> None:
    result = _run(
        "node",
        "scripts/agent-compat/proton-credential.mjs",
        "--self-test",
        str(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    assert "all checks passed" in result.stderr


def test_proton_field_reader_uses_the_caller_descriptor(tmp_path: Path) -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "proton-field-descriptor",
        "--home",
        str(tmp_path),
        "--json",
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report == {
        "context7_args": [
            "item",
            "view",
            "--share-id",
            "context7-share-id",
            "--item-id",
            "context7-item-id",
            "--field",
            "API Key",
        ],
        "context7_token_selected": True,
        "reviewer_vault": "PortalPilot",
        "reviewer_token_selected": True,
        "sessions_removed": True,
    }


def test_proton_helper_redacts_injected_child_streams(tmp_path: Path) -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "proton-field",
        "canary-child-error",
        "--home",
        str(tmp_path),
        "--json",
    )
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["stage"] in {"login", "field-read"}
    assert "FERRY_SECRET_CANARY" not in result.stdout + result.stderr


def test_proton_helper_rejects_a_symbolic_token_before_login(tmp_path: Path) -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "proton-field",
        "symlink-token",
        "--home",
        str(tmp_path),
        "--json",
    )
    assert result.returncode == 1
    assert json.loads(result.stdout) == {
        "stage": "local",
        "error": "reviewer-agent.pat must be a regular file",
        "child_calls": 0,
    }


def test_vibe_review_pins_glm_and_disables_tools() -> None:
    result = _run("node", "scripts/agent-compat/vibe-review.mjs", "--self-test")
    assert result.returncode == 0, result.stderr
    assert "zai-glm-5-2" in result.stderr
    assert "--enabled-tools __none__" in result.stderr
    assert "--disabled-tools re:.*" in result.stderr


def test_vibe_review_rejects_a_tool_call_in_the_history() -> None:
    result = _run("node", "tests/fixtures/agent_compat_runner.mjs", "vibe-review", "tool-call")
    assert result.returncode == 1
    assert "tool call" in (result.stdout + result.stderr).lower()


def test_vibe_review_redacts_an_injected_child_failure() -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "vibe-review",
        "canary-child-error",
        "--json",
    )
    assert result.returncode == 1
    assert json.loads(result.stdout)["stage"] == "vibe-child"
    assert "FERRY_SECRET_CANARY" not in result.stdout + result.stderr


def test_vibe_review_sends_the_prompt_only_through_stdin() -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "vibe-review",
        "stdin-prompt",
        "--json",
    )
    report = json.loads(result.stdout)
    assert report["argv_contains_canary"] is False
    assert report["stdin_received_canary"] is True


def test_qwen_review_uses_direct_api_and_requires_exact_model() -> None:
    result = _run("node", "scripts/agent-compat/qwen-review.mjs", "--self-test")
    assert result.returncode == 0, result.stderr
    assert "qwen3.8-max" in result.stderr
    assert "/chat/completions" in result.stderr
    assert "qwen-ferry" not in result.stderr


def test_qwen_review_sends_explicit_stream_contract() -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "qwen-request-contract",
        "--json",
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["url"].endswith("/chat/completions")
    assert report["authorization_is_bearer"] is True
    assert report["request"] == {
        "model": "qwen3.8-max",
        "stream": True,
        "enable_thinking": True,
        "reasoning_effort": "medium",
        "max_completion_tokens": 32768,
        "response_format_type": "json_schema",
        "schema_name": "ferry_review",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "findings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "severity": {
                                "type": "string",
                                "enum": ["critical", "important", "minor"],
                            },
                            "category": {
                                "type": "string",
                                "enum": [
                                    "security",
                                    "correctness",
                                    "performance",
                                    "maintainability",
                                ],
                            },
                            "file": {"type": "string"},
                            "line": {"type": ["integer", "null"]},
                            "description": {"type": "string"},
                            "suggestion": {"type": "string"},
                            "verification": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "command": {"type": "string"},
                                    "confirms_if": {
                                        "type": "object",
                                        "additionalProperties": False,
                                        "properties": {
                                            "exit_code": {
                                                "type": "integer",
                                                "enum": [0, 1],
                                            },
                                            "stdout_contains": {"type": ["string", "null"]},
                                            "stdout_excludes": {"type": ["string", "null"]},
                                        },
                                        "required": [
                                            "exit_code",
                                            "stdout_contains",
                                            "stdout_excludes",
                                        ],
                                    },
                                    "refutes_if": {
                                        "type": "object",
                                        "additionalProperties": False,
                                        "properties": {
                                            "exit_code": {
                                                "type": "integer",
                                                "enum": [0, 1],
                                            },
                                            "stdout_contains": {"type": ["string", "null"]},
                                            "stdout_excludes": {"type": ["string", "null"]},
                                        },
                                        "required": [
                                            "exit_code",
                                            "stdout_contains",
                                            "stdout_excludes",
                                        ],
                                    },
                                },
                                "required": [
                                    "command",
                                    "confirms_if",
                                    "refutes_if",
                                ],
                            },
                        },
                        "required": [
                            "severity",
                            "category",
                            "file",
                            "line",
                            "description",
                            "suggestion",
                            "verification",
                        ],
                    },
                },
                "summary": {"type": "string"},
                "confidence": {
                    "type": "string",
                    "enum": ["high", "medium", "low"],
                },
            },
            "required": ["findings", "summary", "confidence"],
        },
    }


def test_qwen_review_reassembles_split_stream_without_reasoning_content() -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "qwen-stream",
        "valid-split",
        "--json",
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "ok": True,
        "session_id": "stream-session",
        "result": {
            "findings": [],
            "summary": "clean café",
            "confidence": "high",
        },
    }
    assert "private reasoning" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("fixture", "code"),
    [
        ("missing-done", "INVALID_SCHEMA"),
        ("length", "INVALID_SCHEMA"),
        ("missing-content", "INVALID_SCHEMA"),
        ("invalid-event", "INVALID_SCHEMA"),
        ("wrong-then-right", "WRONG_MODEL"),
    ],
)
def test_qwen_review_rejects_invalid_stream_completion(
    fixture: str,
    code: str,
) -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "qwen-stream",
        fixture,
        "--json",
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["ok"] is False
    assert report["code"] == code


@pytest.mark.parametrize(
    ("fixture", "reason"),
    [
        ("stream-body", "stream-body"),
        ("invalid-event", "stream-event"),
        ("missing-done", "stream-completion"),
        ("trailing-data", "stream-trailing-data"),
        ("missing-model", "stream-model"),
        ("length", "stream-finish-reason"),
        ("missing-content", "stream-content"),
        ("response-envelope", "response-envelope"),
        ("response-json", "response-json"),
        ("response-findings", "response-findings"),
        ("response-unclassified", "response-unclassified"),
    ],
)
def test_qwen_review_reports_each_schema_failure_reason(
    fixture: str,
    reason: str,
) -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "qwen-schema-reason",
        fixture,
        "--json",
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "code": "INVALID_SCHEMA",
        "failure_reason": reason,
    }
    assert "FERRY_SECRET_CANARY" not in result.stdout + result.stderr


def test_qwen_schema_failure_fixture_covers_the_declared_enum() -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "qwen-schema-reason",
        "declared",
        "--json",
    )
    assert result.returncode == 0, result.stderr
    assert set(json.loads(result.stdout)["reasons"]) == {
        "stream-body",
        "stream-event",
        "stream-completion",
        "stream-trailing-data",
        "stream-model",
        "stream-finish-reason",
        "stream-content",
        "response-envelope",
        "response-json",
        "response-findings",
        "response-unclassified",
    }


@pytest.mark.parametrize(
    ("fixture", "deadline"),
    [
        ("connection", "connection"),
        ("idle", "idle"),
        ("partial-event", "idle"),
        ("total-credential", "total"),
        ("total-stream", "total"),
    ],
)
def test_qwen_review_classifies_each_deadline(
    fixture: str,
    deadline: str,
) -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "qwen-deadline",
        fixture,
        "--json",
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["ok"] is False
    assert report.get("stuck") is not True
    assert report["code"] == "ETIMEDOUT"
    assert report["deadline"] == deadline
    assert report["duration_ms"] >= 0


def test_qwen_review_progress_completes_inside_idle_deadline() -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "qwen-deadline",
        "progress",
        "--json",
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["ok"] is True
    assert report["record"]["status"] == "valid"
    assert report["record"]["session_id"] == "deadline-session"


@pytest.mark.parametrize(
    (
        "fixture",
        "status",
        "failure_class",
        "stage",
        "http_status",
        "failure_reason",
    ),
    [
        ("timeout", "timed_out", "timeout", "qwen-response", None, None),
        ("credential", "failed", "credential", "qwen-credential", None, None),
        ("wrong-model", "failed", "wrong-model", "qwen-response", None, None),
        (
            "schema",
            "failed",
            "schema",
            "qwen-response",
            None,
            "response-findings",
        ),
        ("http-401", "failed", "credential", "qwen-response", 401, None),
        ("http-403", "failed", "credential", "qwen-response", 403, None),
        ("http-429", "failed", "rate-limit", "qwen-response", 429, None),
        ("http-400", "failed", "request", "qwen-response", 400, None),
        ("http-500", "failed", "provider", "qwen-response", 500, None),
        ("http-502", "failed", "provider", "qwen-response", 502, None),
        ("unknown", "failed", "unknown", "qwen-response", None, None),
    ],
)
def test_qwen_failure_record_keeps_safe_diagnostics(
    fixture: str,
    status: str,
    failure_class: str,
    stage: str,
    http_status: int | None,
    failure_reason: str | None,
) -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "qwen-failure-record",
        fixture,
        "--json",
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    for route in (report["advisory"], report["plan"]):
        assert route["status"] == status
        assert route["failure_class"] == failure_class
        assert route["failure_stage"] == stage
        assert route["http_status"] == http_status
        assert route["duration_ms"] == 25
        assert route["failure_reason"] == failure_reason
    assert "FERRY_SECRET_CANARY" not in result.stdout + result.stderr


def test_failure_reason_is_null_outside_qwen_schema_failures() -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "provider-failure-reasons",
        "matrix",
        "--json",
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["vibe_schema"]["failure_reason"] is None
    assert report["sonnet_schema"]["failure_reason"] is None
    assert "failure_reason" not in report["qwen_valid"]
    assert "failure_reason" not in report["vibe_valid"]


def test_qwen_review_redacts_an_injected_response_and_error() -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "qwen-review",
        "canary-response-error",
        "--json",
    )
    assert result.returncode == 1
    assert json.loads(result.stdout)["stage"] == "qwen-response"
    assert "FERRY_SECRET_CANARY" not in result.stdout + result.stderr


def test_claude_review_requires_sonnet_5_without_mislabeling_sandbox_failure() -> None:
    result = _run("node", "scripts/agent-compat/claude-review.mjs", "--self-test")
    assert result.returncode == 0, result.stderr
    assert "--model sonnet" in result.stderr
    assert "claude-sonnet-5" in result.stderr
    assert "--effort medium" in result.stderr
    assert "--safe-mode --tools" in result.stderr
    assert "sandbox visibility" in result.stderr
    assert "account is unauthenticated" not in result.stderr


def test_claude_review_redacts_an_injected_host_child_failure() -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "claude-review",
        "canary-child-error",
        "--json",
    )
    assert result.returncode == 1
    assert json.loads(result.stdout)["stage"] == "claude-child"
    assert "FERRY_SECRET_CANARY" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("fixture", "valid_slots"),
    [("both-primary", 2), ("vibe-fails", 1), ("both-fail", 0)],
)
def test_review_ensemble_records_provider_availability_without_sonnet(
    fixture: str, valid_slots: int
) -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "ensemble",
        fixture,
        "--mode",
        "chunk",
        "--json",
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["automatic_sonnet_calls"] == 0
    assert set(report["slots"]) == {"mistral-vibe", "qwen"}
    assert report["valid_slots"] == valid_slots
    assert report["availability_blocks"] is False


def test_review_ensemble_never_calls_the_optional_sonnet_adapter() -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "ensemble-no-sonnet",
        "both-fail",
        "--mode",
        "whole-branch",
        "--json",
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["sonnet_calls"] == 0


def test_vibe_credential_failure_crosses_adapter_and_gate_boundaries() -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "ensemble-boundary",
        "vibe-credential-fails",
        "--mode",
        "chunk",
        "--json",
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["valid_slots"] == 1
    assert report["slots"]["mistral-vibe"]["status"] == "failed"
    assert report["slots"]["mistral-vibe"]["failure_class"] == "credential"
    assert report["slots"]["qwen"]["adapter"] == "qwen-api"
    assert "FERRY_SECRET_CANARY" not in result.stdout + result.stderr


def test_review_verification_runs_every_approved_finding_command() -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "review-verification",
        "three",
        "--json",
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["commands_run"] == ["verify-a", "verify-b", "verify-c"]
    assert [record["verdict"] for record in report["records"]] == [
        "CONFIRMED",
        "REFUTED",
        "INCONCLUSIVE",
    ]
    assert report["actionable"] == ["finding-a"]


def test_review_verification_classifies_structured_results() -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "review-classification",
        "structured",
        "--json",
    )

    assert result.returncode == 0, result.stderr
    records = json.loads(result.stdout)
    assert {record["name"]: record["verdict"] for record in records} == {
        "rg-match": "CONFIRMED",
        "rg-no-match": "REFUTED",
        "non-search-nonzero": "INCONCLUSIVE",
        "search-error": "INCONCLUSIVE",
        "timeout": "INCONCLUSIVE",
        "signal": "INCONCLUSIVE",
        "approval-denied": "INCONCLUSIVE",
        "stderr-only": "INCONCLUSIVE",
        "both-match": "INCONCLUSIVE",
        "neither-match": "INCONCLUSIVE",
    }
    by_name = {record["name"]: record for record in records}
    assert by_name["stderr-only"]["result"]["stderr"] == "needle"
    assert by_name["timeout"]["result"] == {
        "status": "failed",
        "failure_class": "timeout",
    }
    assert by_name["approval-denied"]["result"] == {
        "status": "failed",
        "failure_class": "approval-denied",
    }


def test_review_verification_uses_bounded_checkout_artifacts(tmp_path: Path) -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "review-artifacts",
        "bounded",
        "--tmp",
        str(tmp_path),
        "--json",
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["authorization_status"] == 0
    assert report["authorization"]["authorized"] is True
    assert report["authorization"]["argv"] == [
        "rg",
        "-n",
        "--",
        "needle",
        "src/inside.txt",
    ]
    assert report["classification_status"] == 0
    assert report["classification"] == {"verdict": "CONFIRMED"}
    assert report["exact_limit_status"] == 0
    assert report["rejected"] == {
        "linked": True,
        "linked_directory": True,
        "outside": True,
        "malformed": True,
        "directory": True,
        "oversized": True,
    }
    assert report["raw_modes_rejected"] is True


@pytest.mark.parametrize(
    "command",
    [
        "rg token src/; rm -rf build",
        "git reset --hard HEAD",
        "rg --pre sh token src/",
        "git -c core.pager=sh log",
        "git log --ext-diff",
        "git grep -Osh token",
        'python -c \'open("owned", "w").write("x")\'',
        "rg token src/ | sh",
        "rg $(printenv) src/",
        "rg -n -- token ../outside",
        "head /etc/passwd -- pyproject.toml",
    ],
)
def test_review_verification_default_authorizer_rejects_unsafe_commands(
    command: str,
) -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "review-authorize",
        "--command",
        command,
        "--json",
    )
    report = json.loads(result.stdout)
    assert result.returncode == 1
    assert report["authorized"] is False
    assert report["executed"] is False


def test_review_verification_default_authorizer_accepts_direct_read_only_argv() -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "review-authorize",
        "--command",
        "rg -n -- canonicalCheckoutRoot scripts/agent-compat",
        "--json",
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["argv"] == [
        "rg",
        "-n",
        "--",
        "canonicalCheckoutRoot",
        "scripts/agent-compat",
    ]


def test_review_verification_disables_repository_git_text_converters(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    marker = root / "textconv-ran"
    converter = root / "textconv"
    converter.write_text(f'#!/bin/sh\ntouch {marker}\ncat "$1"\n')
    converter.chmod(0o755)
    (root / ".gitattributes").write_text("*.probe diff=probe\n")
    target = root / "target.probe"
    target.write_text("before\n")

    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "fixture@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Fixture"], cwd=root, check=True)
    subprocess.run(["git", "config", "diff.probe.textconv", str(converter)], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)
    target.write_text("after\n")

    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "review-authorize",
        "--command",
        "git diff -- target.probe",
        "--root",
        str(root),
        "--json",
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    completed = subprocess.run(
        report["argv"],
        cwd=report["cwd"],
        env=report["env"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert marker.exists() is False


def test_review_verification_disables_repository_git_file_monitors(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    marker = root / "fsmonitor-ran"
    monitor = root / "fsmonitor"
    monitor.write_text(f"#!/bin/sh\ntouch {marker}\nprintf 'version 2\\n'\n")
    monitor.chmod(0o755)

    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "core.fsmonitor", str(monitor)], cwd=root, check=True)

    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "review-authorize",
        "--command",
        "git status --short",
        "--root",
        str(root),
        "--json",
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    completed = subprocess.run(
        report["argv"],
        cwd=report["cwd"],
        env=report["env"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert marker.exists() is False


def test_review_verification_rejects_a_symlink_target_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("private fixture\n")
    (root / "linked-file").symlink_to(outside)

    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "review-authorize",
        "--command",
        "head -- linked-file",
        "--root",
        str(root),
        "--json",
    )

    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["authorized"] is False
    assert report["reason"] == "path leaves the checkout"


def test_review_gate_thresholds_carveouts_and_context_tiers() -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "review-policy",
        "--json",
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "chunk_10": False,
        "chunk_11": True,
        "ship_20": False,
        "ship_21": True,
        "docs_only": False,
        "carveout": True,
        "tier_1": "tier-1",
        "tier_2": "tier-2",
        "tier_3": "tier-3",
        "split": "split-hunks",
    }


def test_dual_provider_ship_findings_remain_separate_through_verification() -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "ensemble-verification",
        "dual-primary",
        "--json",
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert set(report["slots"]) == {"mistral-vibe", "qwen"}
    assert report["commands_run"] == ["verify-vibe-slot", "verify-qwen-slot"]
    assert set(report["verified_findings"]) == {"mistral-vibe", "qwen"}


def test_confirmed_findings_are_verified_again_after_fixes() -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "post-fix-reverification",
        "--json",
    )
    report = json.loads(result.stdout)
    assert result.returncode == 0, result.stderr
    assert report["fix_applied"] is True
    assert report["commands_run"] == ["verify-fixed-finding", "verify-fixed-finding"]
    assert report["before"] == "CONFIRMED"
    assert report["after"] == "REFUTED"
    assert report["gate_ready"] is True


def test_reviewer_rules_target_only_the_user_runtime(tmp_path: Path) -> None:
    home = tmp_path / "home"
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "reviewer-runtime",
        "--home",
        str(home),
        "--root",
        str(REPO),
        "--json",
    )
    assert result.returncode == 0, result.stderr
    runtime = home / ".local/share/discord-ferry/reviewer-runtime"
    current = runtime / "current"
    assert current.is_symlink()
    assert current.resolve().parent == runtime / "releases"
    rules = (home / ".codex/rules/ferry-reviewers.rules").read_text()
    assert str(REPO) not in rules
    assert str(current / "review-ensemble.mjs") in rules
    assert str(current / "claude-review.mjs") in rules
    assert str(current / "review-verification.mjs") in rules
    assert str(current / "review-contract.mjs") in rules
    assert "codex-setup.sh" not in rules
    manifest = json.loads((current / "manifest.json").read_text())
    assert set(manifest["files"]) >= {
        "review-contract.mjs",
        "proton-credential.mjs",
        "vibe-review.mjs",
        "qwen-review.mjs",
        "claude-review.mjs",
        "review-ensemble.mjs",
        "review-verification.mjs",
    }
    assert all((current / name).stat().st_mode & 0o222 == 0 for name in manifest["files"])


def test_reviewer_runtime_interruption_keeps_the_previous_release(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    base = [
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "reviewer-runtime-fixture",
        "--home",
        str(home),
        "--json",
    ]
    first = subprocess.run(
        [*base, "--version", "one"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    current = home / ".local/share/discord-ferry/reviewer-runtime/current"
    first_target = current.resolve()
    stopped = subprocess.run(
        [*base, "--version", "two", "--fail-before-activate"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert first.returncode == 0, first.stderr
    assert stopped.returncode == 1
    assert current.resolve() == first_target


def test_reviewer_runtime_readiness_rejects_a_writable_release_file(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    install = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "reviewer-runtime",
        "--home",
        str(home),
        "--root",
        str(REPO),
        "--json",
    )
    assert install.returncode == 0, install.stderr
    current = home / ".local/share/discord-ferry/reviewer-runtime/current"
    (current / "review-contract.mjs").chmod(0o600)
    checked = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "reviewer-runtime-check",
        "--home",
        str(home),
        "--root",
        str(REPO),
        "--json",
    )
    assert checked.returncode == 1
    assert "writable" in checked.stderr.lower()


def test_chunk_review_uses_full_route_provider_ensemble() -> None:
    skill = REPO / ".claude/skills/df-chunk-review/SKILL.md"
    if not skill.exists():
        pytest.skip("snapshot-backed instruction layer is absent in CI")
    text = skill.read_text()
    normalized = " ".join(text.split()).lower()
    assert 'review-ensemble.mjs" --mode chunk' in text
    assert "FERRY_REVIEWER_RUNTIME" in text
    assert "node scripts/agent-compat/review-ensemble.mjs" not in text
    assert "node scripts/agent-compat/review-verification.mjs" not in text
    assert 'review-verification.mjs" --gate --threshold' not in text
    assert "use this skill only for multipart full-route work" in normalized
    assert "file and line counts never skip this review" in normalized
    assert 'review-verification.mjs" --context-tier' in text
    assert 'review-verification.mjs" --authorize-finding' in text
    assert 'review-verification.mjs" --classify-files' in text
    assert "require_escalated" in text
    assert "never try the workspace sandbox first" in normalized
    assert "provider availability does not block the chunk" in normalized
    assert (
        "confirmed critical finding blocks the chunk only when it also passes "
        "all four authority tests" in normalized
    )
    assert "automatic sonnet" not in normalized


def test_ship_routes_the_two_provider_ensemble_from_the_final_diff() -> None:
    skill = REPO / ".claude/skills/df-ship/SKILL.md"
    if not skill.exists():
        pytest.skip("snapshot-backed instruction layer is absent in CI")
    text = skill.read_text()
    normalized = " ".join(text.split()).lower()
    assert 'review-ensemble.mjs" --mode whole-branch' in text
    assert "FERRY_REVIEWER_RUNTIME" in text
    assert "node scripts/agent-compat/review-ensemble.mjs" not in text
    assert "node scripts/agent-compat/review-verification.mjs" not in text
    assert 'review-verification.mjs" --gate --threshold' not in text
    assert "derive the final route" in normalized
    assert "direct and bounded work do not run fixed external providers" in normalized
    assert "full work always attempts both fixed provider slots" in normalized
    assert 'review-verification.mjs" --authorize-finding' in text
    assert 'review-verification.mjs" --classify-files' in text
    assert "require_escalated" in text
    assert "never try the workspace sandbox first" in normalized
    assert "provider availability does not block shipping" in normalized
    assert (
        "confirmed critical finding that passes all four authority tests blocks shipping"
        in normalized
    )
    assert "automatic sonnet" not in normalized
    assert "Second Opinion: skipped (unavailable)" not in text
    assert "Co-Authored-By: Claude Sonnet 5 (1M context)" in text
    assert "Co-Authored-By: Claude Opus" not in text


def test_writing_plans_requires_qwen_before_user_approval() -> None:
    skill = REPO / ".claude/skills/df-writing-plans/SKILL.md"
    if not skill.exists():
        pytest.skip("snapshot-backed instruction layer is absent in CI")
    text = skill.read_text()
    normalized = " ".join(text.split()).lower()
    assert text.count('review-ensemble.mjs" --plan') == 2
    assert "FERRY_REVIEWER_RUNTIME" in text
    assert "--plan-id" in text
    assert "--plan-ledger" in text
    assert "qwen3.8-max" in text
    assert "--plan-provider sonnet" in text
    assert "claude-sonnet-5" in text
    assert "--plan-provider opus" not in text
    assert "claude-opus-5" not in text
    assert "medium effort" in normalized
    assert "only the owner may choose" in normalized
    assert "never starts the other provider" in normalized
    assert "failure class, stage, http status" in normalized
    assert "duration" in normalized
    assert "two total attempts" in normalized
    assert "selected provider stays fixed" in normalized
    assert "provider failure is advisory" in normalized
    assert "owner decision" in normalized
    assert "run a fresh qwen review" not in normalized
    assert 'review-verification.mjs" --authorize-finding' in text
    assert 'review-verification.mjs" --classify-files' in text
    assert "require_escalated" in text
    assert "never try it in the workspace sandbox first" in normalized
    assert text.index("## Independent plan review") < text.index(
        "After the plan is saved and the user approves it"
    )


def test_critique_separates_provider_collection_from_local_verification() -> None:
    skill = REPO / ".claude/skills/df-critique/SKILL.md"
    if not skill.exists():
        pytest.skip("snapshot-backed instruction layer is absent in CI")
    text = skill.read_text()
    normalized = " ".join(text.split()).lower()
    assert "require_escalated" in text
    assert "never try the workspace sandbox first" in normalized
    assert "--authorize-finding" in text
    assert "--classify-files" in text
    assert "2,097,152 bytes" in text


@pytest.mark.parametrize(
    ("fixture", "slot", "qwen_calls", "sonnet_calls", "model", "owner_calls"),
    [
        ("qwen-valid", "plan-qwen", 1, 0, "qwen3.8-max", 0),
        ("sonnet-valid", "plan-sonnet", 0, 1, "claude-sonnet-5", 1),
    ],
)
def test_plan_route_uses_only_selected_provider(
    fixture: str,
    slot: str,
    qwen_calls: int,
    sonnet_calls: int,
    model: str,
    owner_calls: int,
) -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "plan-route",
        fixture,
        "--json",
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["attempt_slots"] == [slot]
    assert report["qwen_calls"] == qwen_calls
    assert report["sonnet_calls"] == sonnet_calls
    assert report["accepted_model"] == model
    assert report["automatic_sonnet_calls"] == 0
    assert report["owner_selected_sonnet_calls"] == owner_calls
    assert report["sonnet_record"] == (True if fixture == "sonnet-valid" else None)


@pytest.mark.parametrize(
    ("fixture", "slot", "qwen_calls", "sonnet_calls", "failure_class"),
    [
        ("qwen-fails", "plan-qwen", 1, 0, "credential"),
        ("sonnet-fails", "plan-sonnet", 0, 1, "child-exit"),
        ("sonnet-schema", "plan-sonnet", 0, 1, "schema"),
    ],
)
def test_plan_route_failure_blocks_without_calling_unselected_provider(
    fixture: str,
    slot: str,
    qwen_calls: int,
    sonnet_calls: int,
    failure_class: str,
) -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "plan-route",
        fixture,
        "--json",
    )
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["attempt_slots"] == [slot]
    assert report["qwen_calls"] == qwen_calls
    assert report["sonnet_calls"] == sonnet_calls
    assert report["automatic_sonnet_calls"] == 0
    assert report["failure_class"] == failure_class
    assert report["ready"] is False


@pytest.mark.parametrize(
    "fixture",
    [
        "unselected-sonnet",
        "mixed-attempts",
        "substituted",
        "wrong-selected-model",
        "wrong-requested-model",
    ],
)
def test_plan_route_rejects_altered_or_mismatched_evidence(fixture: str) -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "plan-route",
        fixture,
        "--json",
    )
    assert result.returncode == 1
    assert json.loads(result.stdout)["ready"] is False


@pytest.mark.parametrize(
    "fixture",
    ["legacy-opus-slot", "legacy-opus-model", "legacy-ledger-policy"],
)
def test_plan_gate_rejects_legacy_opus_evidence_before_provider_dispatch(
    fixture: str,
) -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "plan-budget-gate",
        fixture,
        "--json",
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["decision"]["ready"] is False
    assert report["provider_calls"] == 0


def test_plan_budget_rejects_a_third_attempt_before_calling_a_reviewer(
    tmp_path: Path,
) -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "plan-budget",
        "third-blocked",
        "--tmp",
        str(tmp_path),
        "--json",
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["rounds"] == [1, 2]
    assert report["qwen_calls"] == 2
    assert report["sonnet_calls"] == 0
    assert report["rejected"] == "plan review budget exhausted"
    assert len(report["ledger"]["attempts"]) == 2


def test_plan_budget_locks_the_first_selected_reviewer(tmp_path: Path) -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "plan-budget",
        "provider-lock",
        "--tmp",
        str(tmp_path),
        "--json",
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["rounds"] == [1]
    assert report["qwen_calls"] == 1
    assert report["sonnet_calls"] == 0
    assert report["rejected"] == "plan review provider is locked to qwen"


@pytest.mark.parametrize("fixture", ["failure-counts", "started-counts"])
def test_plan_budget_counts_failed_or_interrupted_attempts(
    fixture: str,
    tmp_path: Path,
) -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "plan-budget",
        fixture,
        "--tmp",
        str(tmp_path),
        "--json",
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["rounds"] == [1, 2]
    assert report["rejected"] == "plan review budget exhausted"
    assert len(report["ledger"]["attempts"]) == 2


def test_plan_budget_rejects_a_ledger_under_a_linked_external_directory(
    tmp_path: Path,
) -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "plan-budget-safety",
        "symlink-escape",
        "--tmp",
        str(tmp_path),
        "--json",
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report == {
        "rejected": "plan review ledger must stay within the checkout",
        "outside_ledger_exists": False,
    }


def test_plan_budget_completes_overlapping_attempts_by_round(tmp_path: Path) -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "plan-budget-safety",
        "overlapping",
        "--tmp",
        str(tmp_path),
        "--json",
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["outcomes"] == ["fulfilled", "fulfilled"]
    assert [attempt["status"] for attempt in report["attempts"]] == ["valid", "valid"]


def test_plan_gate_treats_a_failed_selected_reviewer_as_advisory() -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "plan-budget-gate",
        "failure-advisory",
        "--json",
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)["decision"]
    assert report["ready"] is True
    assert report["decision_required"] is False
    assert report["warning"] == {
        "failure_class": "schema",
        "failure_stage": "qwen-response",
        "failure_reason": "response-findings",
        "http_status": None,
        "duration_ms": 4,
    }


def test_plan_gate_preserves_safe_failure_reason() -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "plan-budget-gate",
        "failure-advisory",
        "--json",
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["decision"]["ready"] is True
    assert report["decision"]["warning"]["failure_reason"] == "response-findings"
    assert report["ledger_attempt"]["failure_reason"] == "response-findings"


@pytest.mark.parametrize(
    "fixture",
    ["altered-failure-reason", "missing-failure-reason"],
)
def test_plan_gate_rejects_altered_failure_reason(fixture: str) -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "plan-budget-gate",
        fixture,
        "--json",
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["decision"] == {
        "ready": False,
        "reason": "invalid plan review ledger",
        "minor_findings": [],
    }


@pytest.mark.parametrize(
    ("fixture", "reason"),
    [
        ("undeclared-record-failure-reason", "invalid plan review record"),
        ("missing-record-failure-reason", "invalid plan review ledger"),
        ("non-schema-record-failure-reason", "invalid plan review record"),
    ],
)
def test_plan_gate_rejects_invalid_record_failure_reason(
    fixture: str,
    reason: str,
) -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "plan-budget-gate",
        fixture,
        "--json",
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["decision"] == {
        "ready": False,
        "reason": reason,
        "minor_findings": [],
    }
    assert "FERRY_SECRET_CANARY" not in result.stdout + result.stderr


def test_plan_gate_ignores_an_inconclusive_finding() -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "plan-budget-gate",
        "inconclusive",
        "--json",
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "ready": True,
        "reason": None,
        "minor_findings": [],
        "accepted_slot": "plan-qwen",
        "accepted_model": "qwen3.8-max",
        "decision_required": False,
        "warning": None,
    }


def test_plan_gate_requires_an_owner_decision_after_a_second_blocker() -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "plan-budget-gate",
        "second-blocker",
        "--json",
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["ready"] is False
    assert report["decision_required"] is True
    assert report["reason"] == "owner decision required after final plan review"


def test_plan_gate_accepts_risk_bound_to_the_current_plan_and_findings() -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "plan-budget-gate",
        "accepted-risk",
        "--json",
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["ready"] is True
    assert report["owner_decision"] == "accept_recorded_risk"


def test_plan_gate_rejects_a_stale_owner_decision() -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "plan-budget-gate",
        "stale-risk",
        "--json",
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["ready"] is False
    assert report["decision_required"] is True


def test_plan_gate_rejects_route_evidence_altered_after_ledger_write() -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "plan-budget-gate",
        "altered-route",
        "--json",
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report == {
        "ready": False,
        "reason": "invalid plan review ledger",
        "minor_findings": [],
    }


@pytest.mark.parametrize(
    ("fixture", "provider"),
    [("default", "qwen"), ("sonnet", "sonnet")],
)
def test_plan_provider_argument_requires_plan_mode(
    fixture: str,
    provider: str,
) -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "plan-args",
        fixture,
        "--json",
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "ok": True,
        "plan": True,
        "plan_provider": provider,
        "plan_id": "docs/plans/fixture.md",
        "plan_ledger": "docs/plans/.review/fixture-ledger.json",
    }


@pytest.mark.parametrize(
    "fixture",
    [
        "invalid",
        "legacy-opus-provider",
        "missing-id",
        "missing-ledger",
        "without-plan-qwen",
        "without-plan-sonnet",
    ],
)
def test_plan_provider_argument_rejects_invalid_context(fixture: str) -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "plan-args",
        fixture,
        "--json",
    )
    assert result.returncode == 1
    assert json.loads(result.stdout)["ok"] is False


def test_plan_revision_gets_a_fresh_qwen_review_before_approval() -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "plan-revision-loop",
        "--json",
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["accepted_reviews"] == 2
    assert report["attempt_slots"] == [["plan-qwen"], ["plan-qwen"]]
    assert len(report["session_ids"]) == len(set(report["session_ids"])) == 2
    assert report["withheld_versions"] == [1]
    assert report["approval_version"] == 2
    assert report["sonnet_calls"] == 0


def test_complete_verification_runs_every_layer_in_order() -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "verify-all",
        "all-pass",
        "--json",
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["completed"] == [
        "compatibility",
        "project",
        "documentation",
        "helper-self-tests",
    ]
    project = [command for command in report["commands"] if command[:3] == ["uv", "run", "ruff"]]
    assert project == [
        ["uv", "run", "ruff", "check", "."],
        ["uv", "run", "ruff", "format", "--check", "."],
    ]
    documentation = report["commands"][report["documentation_start"] :]
    assert documentation[0] == [
        NODE_EXECUTABLE,
        "scripts/agent-compat/plain-english-contract.mjs",
        "--check",
    ]
    assert ["plain-english", "lint", "CHANGELOG.md"] in documentation
    assert not any(command[0] == "npx" for command in documentation)
    assert report["self_test_detection"] == {"helper": True, "aggregate": False}


def test_complete_verification_stops_before_lint_when_the_contract_fails() -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "verify-all",
        "documentation-prerequisite-fails",
        "--json",
    )
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["failed_layer"] == "documentation"
    documentation = report["commands"][report["documentation_start"] :]
    assert documentation == [
        [NODE_EXECUTABLE, "scripts/agent-compat/plain-english-contract.mjs", "--check"]
    ]


@pytest.mark.parametrize(
    "failed_layer",
    ["compatibility", "project", "documentation", "helper-self-tests"],
)
def test_complete_verification_stops_and_names_the_failed_layer(failed_layer: str) -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "verify-all",
        f"fail-{failed_layer}",
        "--json",
    )
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["failed_layer"] == failed_layer
    assert report["failed_check"]
    layers = ["compatibility", "project", "documentation", "helper-self-tests"]
    assert report["completed"] == layers[: layers.index(failed_layer)]


# --- Vibe readiness and config tests -------------------------------------------


def test_vibe_config_includes_bash_allowlist(tmp_path: Path) -> None:
    root, _user_home, env = _installer_checkout(tmp_path)
    installed = subprocess.run(
        [NODE, "scripts/agent-compat/install-local.mjs"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert installed.returncode == 0, installed.stderr
    config = (root / ".vibe/config.toml").read_text()
    assert "[tools.bash]" in config
    assert "allowlist" in config
    assert '"git"' in config
    assert '"uv"' in config
    parsed = tomllib.loads(config)
    assert "mcp_servers" in parsed
    server_names = [s["name"] for s in parsed["mcp_servers"]]
    assert "serena" in server_names
    assert "qmd" in server_names
    assert "context7" in server_names


def test_vibe_readiness_self_test_passes() -> None:
    result = _run("node", "scripts/agent-compat/vibe-readiness.mjs", "--self-test")
    assert result.returncode == 0, result.stderr
    assert "self-test: structural checks passed" in result.stdout


def test_vibe_readiness_detects_missing_config(tmp_path: Path) -> None:
    root, user_home, env = _installer_checkout(tmp_path)
    installed = subprocess.run(
        [NODE, "scripts/agent-compat/install-local.mjs"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert installed.returncode == 0, installed.stderr

    config_path = root / ".vibe/config.toml"
    assert config_path.exists()
    config_path.unlink()

    result = subprocess.run(
        [
            NODE,
            "scripts/agent-compat/vibe-readiness.mjs",
            "--root",
            str(root),
            "--home",
            str(user_home),
            "--json",
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["overall"] == "incomplete"
    ids = [r["id"] for r in report["records"] if r["status"] == "fail"]
    assert "project-config" in ids


def test_vibe_readiness_detects_missing_hooks(tmp_path: Path) -> None:
    root, user_home, env = _installer_checkout(tmp_path)
    installed = subprocess.run(
        [NODE, "scripts/agent-compat/install-local.mjs"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert installed.returncode == 0, installed.stderr

    hooks_path = root / ".vibe/hooks.toml"
    assert hooks_path.exists()
    hooks_path.unlink()

    result = subprocess.run(
        [
            NODE,
            "scripts/agent-compat/vibe-readiness.mjs",
            "--root",
            str(root),
            "--home",
            str(user_home),
            "--json",
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["overall"] == "incomplete"
    ids = [r["id"] for r in report["records"] if r["status"] == "fail"]
    assert "vibe-hooks" in ids


def test_vibe_readiness_records_have_required_fields(tmp_path: Path) -> None:
    root, user_home, env = _installer_checkout(tmp_path)
    installed = subprocess.run(
        [NODE, "scripts/agent-compat/install-local.mjs"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert installed.returncode == 0, installed.stderr

    result = subprocess.run(
        [
            NODE,
            "scripts/agent-compat/vibe-readiness.mjs",
            "--root",
            str(root),
            "--home",
            str(user_home),
            "--json",
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    report = json.loads(result.stdout)
    assert "mode" in report
    assert "overall" in report
    assert "records" in report
    for record in report["records"]:
        assert "id" in record
        assert "class" in record
        assert "status" in record
        assert "duration_ms" in record
        assert record["status"] in ("ok", "fail", "warning")


def test_vibe_readiness_lists_expected_check_ids(tmp_path: Path) -> None:
    root, user_home, env = _installer_checkout(tmp_path)
    installed = subprocess.run(
        [NODE, "scripts/agent-compat/install-local.mjs"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert installed.returncode == 0, installed.stderr

    result = subprocess.run(
        [
            NODE,
            "scripts/agent-compat/vibe-readiness.mjs",
            "--root",
            str(root),
            "--home",
            str(user_home),
            "--json",
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    report = json.loads(result.stdout)
    ids = {r["id"] for r in report["records"]}
    expected = {
        "vibe-version",
        "project-config",
        "vibe-trust",
        "instructions",
        "skills",
        "vibe-hooks",
        "mcp-registration",
        "worktree-parity",
        "reviewer-clients",
        "reviewer-runtime",
        "context7-credential",
        "generated-state",
    }
    assert expected.issubset(ids), f"missing: {expected - ids}"
