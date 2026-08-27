from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=REPO, capture_output=True, text=True, check=False)


def _canonical_repo() -> Path:
    result = _run("git", "rev-parse", "--path-format=absolute", "--git-common-dir")
    assert result.returncode == 0, result.stderr
    return Path(result.stdout.strip()).resolve().parent


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


def test_codex_template_pins_runtime_and_live_servers() -> None:
    text = (REPO / "config/agent-compat/codex-config.toml").read_text()
    assert 'model = "gpt-5.6-sol"' in text
    assert 'model_reasoning_effort = "high"' in text
    assert 'approval_policy = "on-request"' in text
    assert 'sandbox_mode = "workspace-write"' in text
    assert 'web_search = "disabled"' in text
    assert "[mcp_servers.qmd]" in text
    assert "[mcp_servers.serena]" in text
    assert "[mcp_servers.context7]" in text
    assert "mcp_servers.second-opinion" not in text


def test_installer_renders_project_pins_with_conflicting_global_defaults(
    tmp_path: Path,
) -> None:
    if not (REPO / "AGENTS.md").exists():
        pytest.skip("snapshot instruction layer is absent")
    root = tmp_path / "repo"
    user_home = tmp_path / "home"
    codex_home = user_home / ".codex"
    root.mkdir()
    codex_home.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    shutil.copytree(REPO / "config/agent-compat", root / "config/agent-compat")
    shutil.copytree(REPO / "scripts/agent-compat", root / "scripts/agent-compat")
    shutil.copy2(REPO / "scripts/agent-install.sh", root / "scripts/agent-install.sh")
    shutil.copytree(REPO / ".claude/skills", root / ".claude/skills", symlinks=True)
    shutil.copytree(REPO / ".agents/skills", root / ".agents/skills", symlinks=True)
    shutil.copy2(REPO / "AGENTS.md", root / "AGENTS.md")
    global_config = (
        'model = "personal-model"\nmodel_reasoning_effort = "low"\n'
        'approval_policy = "never"\nsandbox_mode = "read-only"\n'
        'web_search = "live"\n'
        f'[projects."{root}"]\ntrust_level = "trusted"\n'
    ).encode()
    global_path = codex_home / "config.toml"
    global_path.write_bytes(global_config)
    env = {**os.environ, "HOME": str(user_home), "CODEX_HOME": str(codex_home)}
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
    assert "Opus 5 is an explicit owner-selected route" in text


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
    assert routes["codex_write"] == ["write"]
    assert routes["vibe_write"] == ["write"]
    assert "docs" in routes["qwen_write"]
    assert "github-docs" not in routes["codex_bash"]
    assert "github-docs" not in routes["vibe_bash"]
    assert "github-docs" in routes["qwen_bash"]


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
    assert report["security_children"] == ["write"]
    assert report["duration_ms"] < 2000


def test_plain_english_chat_wrapper_preserves_streams_and_exit(tmp_path: Path) -> None:
    assert NODE is not None
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_npx = fake_bin / "npx"
    fake_npx.write_text("#!/bin/sh\nread value\nprintf 'wrapped:%s\\n' \"$value\"\nexit 7\n")
    fake_npx.chmod(0o755)
    env = {"PATH": f"{fake_bin}:/usr/bin:/bin"}
    result = subprocess.run(
        [NODE, str(REPO / "scripts/agent-compat/plain-english-chat-hook.mjs")],
        input="payload\n",
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 7
    assert result.stdout == "wrapped:payload\n"


def test_plain_english_chat_wrapper_terminates_its_child_group(tmp_path: Path) -> None:
    assert NODE is not None
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_npx = fake_bin / "npx"
    child_pid = tmp_path / "child.pid"
    fake_npx.write_text('#!/bin/sh\nsleep 60 &\nprintf "%s" "$!" > "$FERRY_CHILD_PID"\nwait\n')
    fake_npx.chmod(0o755)
    wrapper = subprocess.Popen(
        [NODE, str(REPO / "scripts/agent-compat/plain-english-chat-hook.mjs")],
        env={"PATH": f"{fake_bin}:/usr/bin:/bin", "FERRY_CHILD_PID": str(child_pid)},
    )
    for _ in range(50):
        if child_pid.exists():
            break
        time.sleep(0.02)
    pid = int(child_pid.read_text())
    wrapper.send_signal(signal.SIGTERM)
    wrapper.wait(timeout=5)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_plain_english_chat_wrapper_self_test() -> None:
    assert NODE is not None
    result = subprocess.run(
        [
            NODE,
            str(REPO / "scripts/agent-compat/plain-english-chat-hook.mjs"),
            "--self-test",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "all checks passed" in result.stderr


def test_codex_chat_transform_requires_and_replaces_both_events(tmp_path: Path) -> None:
    fixture = tmp_path / "hooks.json"
    fixture.write_text(
        '{"hooks":{"Stop":[{"matcher":"*","hooks":[{"type":"command",'
        '"command":"npx --no-install plain-english hook chat --agent codex",'
        '"timeout":10}]}],"SubagentStop":[{"matcher":"*","hooks":'
        '[{"type":"command","command":"npx --no-install plain-english hook chat '
        '--agent codex","timeout":10}]}]}}'
    )
    result = _run(
        "node",
        "scripts/agent-compat/plain-english-chat-hook.mjs",
        "--transform",
        str(fixture),
        str(REPO),
    )
    assert result.returncode == 0, result.stderr
    transformed = json.loads(fixture.read_text())
    hooks = [
        hook
        for event in ("Stop", "SubagentStop")
        for group in transformed["hooks"][event]
        for hook in group["hooks"]
        if "plain-english-chat-hook.mjs" in hook["command"]
    ]
    assert len(hooks) == 2
    assert all(hook["timeout"] == 60 for hook in hooks)


def test_codex_chat_transform_rejects_a_missing_event(tmp_path: Path) -> None:
    fixture = tmp_path / "hooks.json"
    fixture.write_text('{"hooks":{"Stop":[]}}')
    result = _run(
        "node",
        "scripts/agent-compat/plain-english-chat-hook.mjs",
        "--transform",
        str(fixture),
        str(REPO),
    )
    assert result.returncode == 1
    assert "expected one direct chat hook for Stop" in result.stderr


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


def test_agent_check_accepts_the_ferry_chat_transform() -> None:
    if not (REPO / ".codex/hooks.json").exists():
        pytest.skip("generated Codex state is absent in CI")
    result = _run("node", "scripts/agent-compat/check.mjs", "--generated-only")
    assert result.returncode == 0, result.stdout + result.stderr


def test_installer_stages_chat_wrapper_in_the_canonical_owner() -> None:
    generated = _canonical_repo() / ".codex/bin/plain-english-chat-hook.mjs"
    if not (_canonical_repo() / ".codex/hooks.json").exists():
        pytest.skip("generated Codex state is absent in CI")
    assert (
        generated.read_bytes()
        == (REPO / "scripts/agent-compat/plain-english-chat-hook.mjs").read_bytes()
    )


def test_agent_check_rejects_a_ten_second_chat_wrapper(tmp_path: Path) -> None:
    hooks = REPO / ".codex/hooks.json"
    if not hooks.exists():
        pytest.skip("generated Codex state is absent in CI")
    document = json.loads(hooks.read_text())
    wrappers = [
        hook
        for event in ("Stop", "SubagentStop")
        for group in document["hooks"][event]
        for hook in group["hooks"]
        if "plain-english-chat-hook.mjs" in hook["command"]
    ]
    assert len(wrappers) == 2
    wrappers[0]["timeout"] = 10
    probe = tmp_path / "hooks.json"
    probe.write_text(json.dumps(document))
    result = _run("node", "scripts/agent-compat/check.mjs", "--check-codex-hooks", str(probe))
    assert result.returncode == 1
    assert "exactly two 60-second Ferry chat wrappers" in result.stdout + result.stderr


def test_hook_regeneration_is_stable_and_rejects_upstream_shape_drift() -> None:
    stable = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "hook-regeneration",
        "current",
        "--runs",
        "2",
        "--json",
    )
    assert stable.returncode == 0, stable.stderr
    report = json.loads(stable.stdout)
    assert report["wrapper_count"] == 2
    assert report["timeouts"] == [60, 60]
    altered = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "hook-regeneration",
        "altered-command",
        "--json",
    )
    assert altered.returncode == 1
    assert "expected one direct chat hook" in altered.stderr


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


def test_checker_uses_the_installer_plain_english_command() -> None:
    text = (REPO / "scripts/agent-compat/check.mjs").read_text()
    assert "'npx'," not in text
    assert "'plain-english'," in text


def test_focused_codex_hook_check_stops_when_path_is_missing() -> None:
    result = _run("node", "scripts/agent-compat/check.mjs", "--check-codex-hooks")
    assert result.returncode == 1
    assert "--check-codex-hooks requires a path" in result.stdout
    assert "Warnings" not in result.stdout


def test_agent_check_rejects_an_incomplete_linked_host_contract(
    tmp_path: Path,
) -> None:
    if not (REPO / ".claude/scripts/new-worktree.sh").exists():
        pytest.skip("snapshot-backed instruction layer is absent in CI")
    broken_script = tmp_path / "new-worktree.sh"
    broken_script.write_text("for host_dir in .agents .qwen; do\n  :\ndone\n")
    include = tmp_path / ".worktreeinclude"
    include.write_text("CLAUDE.md\n")
    result = _run(
        "node",
        "scripts/agent-compat/check.mjs",
        "--check-worktree-contract",
        str(broken_script),
        str(include),
    )
    assert result.returncode == 1
    assert "four canonical host links" in result.stdout + result.stderr


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
    }
    assert all(
        set(record) == {"id", "class", "status", "duration_ms", "remediation", "details"}
        for record in report["records"]
    )


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
        ("missing-tool-server", "mcp-registration"),
        ("missing-client", "reviewer-clients"),
        ("misdistributed-hook", "hooks"),
        ("commented-model", "project-config"),
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


def test_reviewer_readiness_names_vibe_and_qwen_without_opus() -> None:
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
    assert "opus" not in result.stdout.lower()
    assert "claude" not in result.stdout.lower()


@pytest.mark.parametrize(
    ("fixture", "failed_id"),
    [
        ("vibe-fails", "vibe-reviewer"),
        ("qwen-fails", "qwen-reviewer"),
        ("qwen-wrong-model", "qwen-reviewer"),
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
    assert records["stop-main-agent"]["details"]["timeout_seconds"] == 60
    assert records["stop-child-agent"]["details"]["timeout_seconds"] == 60
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
        ("stop-ten-second", "stop-main-agent"),
        ("stop-timeout", "stop-child-agent"),
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


def test_provider_records_require_root_authorized_verification_commands(
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
        "accepted": {
            "safe": True,
            "shell": False,
            "python": False,
            "missing": False,
            "outside": False,
            "linked": False,
        },
        "ensemble_status": "failed",
        "ensemble_failure": "schema",
        "plan_status": "failed",
        "plan_failure": "schema",
        "plan_accepted": False,
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
    ("fixture", "status", "failure_class", "stage", "http_status"),
    [
        ("timeout", "timed_out", "timeout", "qwen-response", None),
        ("credential", "failed", "credential", "qwen-credential", None),
        ("wrong-model", "failed", "wrong-model", "qwen-response", None),
        ("schema", "failed", "schema", "qwen-response", None),
        ("http-401", "failed", "credential", "qwen-response", 401),
        ("http-403", "failed", "credential", "qwen-response", 403),
        ("http-429", "failed", "rate-limit", "qwen-response", 429),
        ("http-400", "failed", "request", "qwen-response", 400),
        ("http-500", "failed", "provider", "qwen-response", 500),
        ("http-502", "failed", "provider", "qwen-response", 502),
        ("unknown", "failed", "unknown", "qwen-response", None),
    ],
)
def test_qwen_failure_record_keeps_safe_diagnostics(
    fixture: str,
    status: str,
    failure_class: str,
    stage: str,
    http_status: int | None,
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
    assert "FERRY_SECRET_CANARY" not in result.stdout + result.stderr


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


def test_claude_review_requires_opus_5_without_mislabeling_sandbox_failure() -> None:
    result = _run("node", "scripts/agent-compat/claude-review.mjs", "--self-test")
    assert result.returncode == 0, result.stderr
    assert "claude-opus-5" in result.stderr
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
def test_review_ensemble_records_provider_availability_without_opus(
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
    assert report["automatic_opus_calls"] == 0
    assert set(report["slots"]) == {"mistral-vibe", "qwen"}
    assert report["valid_slots"] == valid_slots
    assert report["availability_blocks"] is False


def test_review_ensemble_never_calls_the_optional_opus_adapter() -> None:
    result = _run(
        "node",
        "tests/fixtures/agent_compat_runner.mjs",
        "ensemble-no-opus",
        "both-fail",
        "--mode",
        "whole-branch",
        "--json",
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["opus_calls"] == 0


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


def test_chunk_review_uses_the_two_provider_ensemble() -> None:
    skill = REPO / ".claude/skills/df-chunk-review/SKILL.md"
    if not skill.exists():
        pytest.skip("snapshot-backed instruction layer is absent in CI")
    text = skill.read_text()
    normalized = " ".join(text.split()).lower()
    assert 'review-ensemble.mjs" --mode chunk' in text
    assert "FERRY_REVIEWER_RUNTIME" in text
    assert "node scripts/agent-compat/review-ensemble.mjs" not in text
    assert "node scripts/agent-compat/review-verification.mjs" not in text
    assert 'review-verification.mjs" --gate --threshold 10' in text
    assert 'review-verification.mjs" --context-tier' in text
    assert 'review-verification.mjs" --authorize-finding' in text
    assert 'review-verification.mjs" --classify-files' in text
    assert "require_escalated" in text
    assert "never try the workspace sandbox first" in normalized
    assert "provider availability does not block the chunk" in normalized
    assert "confirmed critical finding blocks the chunk" in normalized
    assert "automatic opus" not in normalized


def test_ship_uses_the_same_two_provider_ensemble() -> None:
    skill = REPO / ".claude/skills/df-ship/SKILL.md"
    if not skill.exists():
        pytest.skip("snapshot-backed instruction layer is absent in CI")
    text = skill.read_text()
    normalized = " ".join(text.split()).lower()
    assert 'review-ensemble.mjs" --mode whole-branch' in text
    assert "FERRY_REVIEWER_RUNTIME" in text
    assert "node scripts/agent-compat/review-ensemble.mjs" not in text
    assert "node scripts/agent-compat/review-verification.mjs" not in text
    assert 'review-verification.mjs" --gate --threshold 20' in text
    assert 'review-verification.mjs" --authorize-finding' in text
    assert 'review-verification.mjs" --classify-files' in text
    assert "require_escalated" in text
    assert "never try the workspace sandbox first" in normalized
    assert "provider availability does not block shipping" in normalized
    assert "confirmed critical finding blocks shipping" in normalized
    assert "automatic opus" not in normalized
    assert "Second Opinion: skipped (unavailable)" not in text


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
    assert "--plan-provider opus" in text
    assert "claude-opus-5" in text
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
    ("fixture", "slot", "qwen_calls", "opus_calls", "model", "owner_calls"),
    [
        ("qwen-valid", "plan-qwen", 1, 0, "qwen3.8-max", 0),
        ("opus-valid", "plan-opus", 0, 1, "claude-opus-5", 1),
    ],
)
def test_plan_route_uses_only_selected_provider(
    fixture: str,
    slot: str,
    qwen_calls: int,
    opus_calls: int,
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
    assert report["opus_calls"] == opus_calls
    assert report["accepted_model"] == model
    assert report["automatic_opus_calls"] == 0
    assert report["owner_selected_opus_calls"] == owner_calls
    assert report["opus_record"] == (True if fixture == "opus-valid" else None)


@pytest.mark.parametrize(
    ("fixture", "slot", "qwen_calls", "opus_calls", "failure_class"),
    [
        ("qwen-fails", "plan-qwen", 1, 0, "credential"),
        ("opus-fails", "plan-opus", 0, 1, "child-exit"),
        ("opus-schema", "plan-opus", 0, 1, "schema"),
    ],
)
def test_plan_route_failure_blocks_without_calling_unselected_provider(
    fixture: str,
    slot: str,
    qwen_calls: int,
    opus_calls: int,
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
    assert report["opus_calls"] == opus_calls
    assert report["automatic_opus_calls"] == 0
    assert report["failure_class"] == failure_class
    assert report["ready"] is False


@pytest.mark.parametrize(
    "fixture",
    [
        "unselected-opus",
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
    assert report["opus_calls"] == 0
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
    assert report["opus_calls"] == 0
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
    report = json.loads(result.stdout)
    assert report["ready"] is True
    assert report["decision_required"] is False
    assert report["warning"] == {
        "failure_class": "timeout",
        "failure_stage": "total-timeout",
        "http_status": None,
        "duration_ms": 4,
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
    [("default", "qwen"), ("opus", "opus")],
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
        "missing-id",
        "missing-ledger",
        "without-plan-qwen",
        "without-plan-opus",
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
    assert report["opus_calls"] == 0


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
    assert report["self_test_detection"] == {"helper": True, "aggregate": False}


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
