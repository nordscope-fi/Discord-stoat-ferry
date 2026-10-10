"""Classification of destructive git commands across equivalent spellings (#996).

The detector in scripts/agent-compat/destructive-git.mjs is shared by the Qwen guard and the
Codex and Vibe adapters. These tests only classify text. Nothing here runs a git command, and
the host tests start each adapter with an empty home directory so no user hook runs.

Claude Code is not covered: it uses an inline substring check in the untracked
.claude/settings.local.json, which is not part of this repository.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")
RUNNER = REPO / "tests" / "fixtures" / "destructive_git_runner.mjs"

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

# (id, command). Every one of these must be classified as destructive.
BLOCKED = [
    # reset --hard
    ("reset-plain", "git reset --hard"),
    ("reset-with-ref", "git reset --hard HEAD~1"),
    ("reset-flag-after-ref", "git reset HEAD~1 --hard"),
    ("reset-abbreviated", "git reset --har"),
    ("reset-dir-option", "git -C /some/dir reset --hard"),
    ("reset-dir-option-attached", "git -C/some/dir reset --hard"),
    ("reset-config-option", "git -c core.x=1 reset --hard"),
    ("reset-git-dir-equals", "git --git-dir=/x/.git reset --hard"),
    ("reset-git-dir-separate", "git --git-dir /x/.git --work-tree /x reset --hard"),
    ("reset-no-pager", "git --no-pager reset --hard"),
    ("reset-extra-spaces", "git    reset     --hard"),
    ("reset-tabs", "git\treset\t--hard"),
    ("reset-leading-space", "   git reset --hard"),
    ("reset-quoted-binary", '"git" reset --hard'),
    ("reset-quoted-subcommand", "git 'reset' '--hard'"),
    ("reset-double-quoted-flag", 'git reset "--hard"'),
    ("reset-backslash-binary", "\\git reset --hard"),
    # prefixes and wrappers
    ("env-prefix", "FOO=1 git reset --hard"),
    ("env-prefix-two", "FOO=1 BAR=2 git reset --hard"),
    ("env-prefix-quoted-space", 'FOO="a b" git reset --hard'),
    ("env-command", "env FOO=1 git reset --hard"),
    ("env-command-option", "env -i git reset --hard"),
    ("command-builtin", "command git reset --hard"),
    ("exec-builtin", "exec git reset --hard"),
    ("sudo", "sudo git reset --hard"),
    ("sudo-user-option", "sudo -u root git reset --hard"),
    ("time", "time git reset --hard"),
    ("nohup", "nohup git reset --hard"),
    ("timeout", "timeout 5 git reset --hard"),
    ("xargs", "echo x | xargs git reset --hard"),
    ("full-path", "/usr/bin/git reset --hard"),
    ("full-path-homebrew", "/opt/homebrew/bin/git reset --hard"),
    ("relative-path", "./git reset --hard"),
    # chaining and grouping
    ("after-and", "echo hi && git reset --hard"),
    ("after-semicolon", "echo hi; git reset --hard"),
    ("after-pipe", "echo hi | git reset --hard"),
    ("after-or", "false || git reset --hard"),
    ("after-newline", "echo hi\ngit reset --hard"),
    ("subshell", "(git reset --hard)"),
    ("brace-group", "{ git reset --hard; }"),
    ("command-substitution", "echo $(git reset --hard)"),
    ("backticks", "echo `git reset --hard`"),
    ("after-then", "if true; then git reset --hard; fi"),
    # nested shells
    ("bash-c", "bash -c 'git reset --hard'"),
    ("bash-lc", 'bash -lc "git reset --hard"'),
    ("sh-c", "sh -c 'git reset --hard'"),
    ("zsh-c", "zsh -c 'git reset --hard'"),
    ("eval", "eval 'git reset --hard'"),
    ("bash-c-chained", "bash -c 'echo a; git reset --hard'"),
    ("bash-c-nested", "bash -c \"sh -c 'git reset --hard'\""),
    # push: force
    ("push-force", "git push --force"),
    ("push-force-remote", "git push --force origin main"),
    ("push-force-last", "git push origin main --force"),
    ("push-short", "git push -f"),
    ("push-short-last", "git push origin main -f"),
    ("push-cluster-fu", "git push -fu origin main"),
    ("push-cluster-uf", "git push -uf origin main"),
    ("push-lease", "git push --force-with-lease"),
    ("push-lease-value", "git push --force-with-lease=main origin main"),
    ("push-lease-abbreviated", "git push --force-w origin main"),
    ("push-force-abbreviated", "git push --forc origin main"),
    ("push-force-if-includes", "git push --force-if-includes origin main"),
    ("push-dir-option", "git -C /some/dir push --force"),
    ("push-config-option", "git -c push.default=simple push -f"),
    ("push-env-prefix", "FOO=1 git push --force"),
    ("push-chained", "git status && git push --force"),
    ("push-bash-c", "bash -c 'git push --force'"),
    # push: a leading + on a refspec forces that ref
    ("push-plus-branch", "git push origin +main"),
    ("push-plus-head", "git push origin +HEAD:main"),
    ("push-plus-src-dst", "git push origin +feature:main"),
    ("push-plus-quoted", "git push origin '+main'"),
    ("push-plus-glob", "git push origin +refs/heads/*:refs/heads/*"),
    # clean -f
    ("clean-fd", "git clean -fd"),
    ("clean-df", "git clean -df"),
    ("clean-xdf", "git clean -xdf"),
    ("clean-fdx", "git clean -fdx"),
    ("clean-separate-f-d", "git clean -f -d"),
    ("clean-separate-d-f", "git clean -d -f"),
    ("clean-double-f", "git clean -ffd"),
    ("clean-long", "git clean --force"),
    ("clean-long-after", "git clean -d --force"),
    ("clean-long-abbreviated", "git clean --forc -d"),
    ("clean-with-path", "git clean -f path/"),
    ("clean-dir-option", "git -C /some/dir clean -fd"),
    ("clean-env-prefix", "FOO=1 git clean -fd"),
    ("clean-chained", "echo a; git clean -fd"),
    ("clean-bash-c", "bash -c 'git clean -fd'"),
    # branch deletion that ignores merge state
    ("branch-D", "git branch -D feature"),
    ("branch-D-many", "git branch -D a b c"),
    ("branch-D-after-name", "git branch feature -D"),
    ("branch-delete-force", "git branch --delete --force feature"),
    ("branch-force-delete", "git branch --force --delete feature"),
    ("branch-cluster-df", "git branch -df feature"),
    ("branch-cluster-fd", "git branch -fd feature"),
    ("branch-separate-d-f", "git branch -d -f feature"),
    ("branch-separate-f-d", "git branch -f -d feature"),
    ("branch-long-abbreviated", "git branch --delet --forc feature"),
    ("branch-dir-option", "git -C /some/dir branch -D feature"),
    ("branch-env-prefix", "FOO=1 git branch -D feature"),
    ("branch-chained", "git fetch && git branch -D feature"),
    ("branch-bash-c", "bash -c 'git branch -D feature'"),
    # checkout of the whole tree
    ("checkout-dashdash-dot", "git checkout -- ."),
    ("checkout-ref-dashdash-dot", "git checkout HEAD -- ."),
    ("checkout-bare-dot", "git checkout ."),
    ("checkout-ref-bare-dot", "git checkout HEAD ."),
    ("checkout-dot-slash", "git checkout -- ./"),
    ("checkout-quoted-dot", "git checkout -- '.'"),
    ("checkout-extra-spaces", "git   checkout    --   ."),
    ("checkout-dir-option", "git -C /some/dir checkout -- ."),
    ("checkout-env-prefix", "FOO=1 git checkout -- ."),
    ("checkout-chained", "echo a && git checkout -- ."),
    ("checkout-bash-c", "bash -c 'git checkout -- .'"),
    # restore of the whole tree
    ("restore-dot", "git restore ."),
    ("restore-dot-slash", "git restore ./"),
    ("restore-worktree", "git restore --worktree ."),
    ("restore-staged-and-worktree", "git restore --staged --worktree ."),
    ("restore-source-equals", "git restore --source=HEAD ."),
    ("restore-source-separate", "git restore --source HEAD ."),
    ("restore-short-W", "git restore -W ."),
    ("restore-short-SW", "git restore -SW ."),
    ("restore-dashdash", "git restore -- ."),
    ("restore-dir-option", "git -C /some/dir restore ."),
    ("restore-env-prefix", "FOO=1 git restore ."),
    ("restore-chained", "echo a; git restore ."),
    ("restore-bash-c", "bash -c 'git restore .'"),
    # Found by the security review of this change: spellings the shell reads
    # differently from a plain word splitter, and a value glued to -s.
    ("restore-source-value-holds-capital-s", "git restore -sSTABLE ."),
    ("reset-ansi-c-quoted-subcommand", "git $'reset' --hard"),
    ("reset-ansi-c-quoted-flag", "git reset $'--hard'"),
    ("reset-locale-quoted-subcommand", 'git $"reset" --hard'),
    ("reset-line-continuation-before-flag", "git reset \\\n--hard"),
    ("reset-line-continuation-before-subcommand", "git \\\nreset --hard"),
    ("reset-bash-c-double-dash", "bash -c -- 'git reset --hard'"),
    ("reset-env-split-string", "env -S 'git reset --hard'"),
    ("push-env-word-after-the-command", "git push env -S +main"),
    # Found by the release review: a separator or bracket inside quotes or
    # after a backslash is part of a word, so it must not split the command.
    ("push-branch-with-brackets-double-quoted", 'git push origin "feat(x)" --force'),
    ("push-branch-with-brackets-single-quoted", "git push origin 'feat(x)' --force"),
    ("push-branch-with-escaped-brackets", "git push origin feat\\(x\\) --force"),
    ("push-branch-with-semicolon-quoted", 'git push origin "a;b" --force'),
    ("push-url-with-ampersand-quoted", 'git push "https://x/?a=1&b=2" --force'),
    ("reset-dir-with-brackets", 'git -C "proj(copy)" reset --hard'),
    ("reset-work-tree-with-brackets", 'git --work-tree="a(b)" reset --hard'),
    ("reset-config-value-with-brackets", 'git -c user.name="Foo(bar)" reset --hard'),
    ("clean-path-with-brackets", 'git clean "dir(1)" -f'),
    ("branch-name-with-brackets", 'git branch "fix(a)" -D'),
    ("checkout-ref-with-brackets", 'git checkout "a(b)" -- .'),
    ("restore-path-with-brackets", 'git restore "p(q)" .'),
    ("push-ansi-c-escaped-quote", "git push origin $'it\\'s' --force"),
    # The shell runs $(...) and backticks even inside double quotes.
    ("reset-substitution-in-double-quotes", 'echo "$(git reset --hard)"'),
    ("reset-backticks-in-double-quotes", 'echo "`git reset --hard`"'),
    ("reset-nested-substitution", "echo $(echo $(git reset --hard))"),
    ("reset-bash-c-with-separator-inside", "bash -c 'git status; git reset --hard'"),
    # A # that starts a word begins a comment, so flags after it never run.
    ("restore-staged-flag-only-in-comment", "git restore . # -S"),
    ("restore-source-flag-before-double-dash", "git restore . -s -- -S"),
    ("restore-staged-flag-after-comment-word", "git restore . #c -S a && true"),
    ("reset-with-trailing-comment", "git reset --hard # tidy up"),
    ("push-hash-inside-a-word-is-not-a-comment", "git push origin main#tag --force"),
    ("reset-after-comment-line", "# note\ngit reset --hard"),
]

# (id, command). Every one of these must stay allowed.
ALLOWED = [
    ("reset-soft", "git reset --soft HEAD~1"),
    ("restore-staged-with-separate-source", "git restore -s HEAD -S ."),
    ("commit-message-quoting-a-command", 'git commit -m "x; git reset --hard"'),
    ("echo-single-quoted-substitution", "echo '$(git reset --hard)'"),
    ("destructive-flags-only-in-comment", "git branch --force x # --delete -f"),
    ("whole-command-commented-out", "# git reset --hard"),
    ("reset-mixed", "git reset --mixed HEAD~1"),
    ("reset-plain", "git reset"),
    ("reset-unstage-file", "git reset HEAD path/file"),
    ("clean-dry-run-short", "git clean -n"),
    ("clean-dry-run-cluster", "git clean -nd"),
    ("clean-dry-run-long", "git clean --dry-run -d"),
    ("push-plain", "git push"),
    ("push-remote-branch", "git push origin main"),
    ("push-set-upstream", "git push -u origin main"),
    ("push-tags", "git push --tags"),
    ("push-follow-tags", "git push --follow-tags"),
    ("push-plus-inside-name", "git push origin feature/a+b"),
    ("branch-delete-merged", "git branch -d feature"),
    ("branch-list", "git branch -a"),
    ("branch-move", "git branch -m old new"),
    ("branch-format", "git branch --format=%(refname)"),
    ("checkout-branch", "git checkout main"),
    ("checkout-new-branch", "git checkout -b feature"),
    ("checkout-path", "git checkout -- path/file"),
    ("checkout-ref-path", "git checkout HEAD -- path/file"),
    ("checkout-dotfile", "git checkout -- .gitignore"),
    ("restore-path", "git restore path/file"),
    ("restore-staged-long", "git restore --staged ."),
    ("restore-staged-short", "git restore -S ."),
    ("restore-staged-path", "git restore --staged path/file"),
    ("status", "git status"),
    ("diff", "git diff --stat"),
    ("log", "git log --oneline -5"),
    ("commit-message-names-commands", "git commit -m 'explain reset --hard and push --force'"),
    ("echo-names-command", "echo git reset --hard"),
    ("grep-names-command", "grep -n 'reset --hard' file.txt"),
    ("gh-merge", "gh pr merge 5 --squash"),
    ("dir-option-status", "git -C /some/dir status"),
    ("config-option-status", "git -c core.x=1 status"),
    ("env-prefix-status", "FOO=1 git status"),
    ("bash-c-status", "bash -c 'git status'"),
    ("uppercase-binary", "GIT reset --hard"),
    ("empty", ""),
    ("not-git", "ls -la"),
]


def _classify(commands: list[str]) -> list[bool]:
    assert NODE is not None
    result = subprocess.run(
        [NODE, str(RUNNER)],
        input=json.dumps(commands),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    verdicts = json.loads(result.stdout)
    assert isinstance(verdicts, list) and len(verdicts) == len(commands)
    return [bool(v) for v in verdicts]


@pytest.fixture(scope="module")
def blocked_verdicts() -> dict[str, bool]:
    return dict(
        zip([name for name, _ in BLOCKED], _classify([cmd for _, cmd in BLOCKED]), strict=True)
    )


@pytest.fixture(scope="module")
def allowed_verdicts() -> dict[str, bool]:
    return dict(
        zip([name for name, _ in ALLOWED], _classify([cmd for _, cmd in ALLOWED]), strict=True)
    )


@pytest.mark.parametrize(("name", "command"), BLOCKED, ids=[name for name, _ in BLOCKED])
def test_equivalent_spelling_is_destructive(
    blocked_verdicts: dict[str, bool], name: str, command: str
) -> None:
    assert blocked_verdicts[name] is True, command


@pytest.mark.parametrize(("name", "command"), ALLOWED, ids=[name for name, _ in ALLOWED])
def test_near_miss_stays_allowed(
    allowed_verdicts: dict[str, bool], name: str, command: str
) -> None:
    assert allowed_verdicts[name] is False, command


def test_non_string_input_is_not_destructive() -> None:
    script = (
        "import { isDestructiveGitCommand as d } from "
        f"'{REPO / 'scripts' / 'agent-compat' / 'destructive-git.mjs'}';"
        "process.stdout.write(JSON.stringify([d(undefined), d(null), d(42), d({})]));"
    )
    assert NODE is not None
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [False, False, False, False]


# --- Host adapters: the same semantic command gets each host's documented deny response ---

HOST_BLOCKED = [
    "git reset --hard",
    "FOO=1 git -C . reset --hard",
    "git  push\t--force-with-lease origin main",
    "git push origin +main",
    "bash -c 'git clean -fd'",
    "git branch -fd feature",
    "git checkout .",
]
HOST_ALLOWED = ["git status", "git reset --soft HEAD~1", "git checkout -- path/file"]
DENY_REASON = "Destructive git operation. Confirm with the user first."


def _run_host(
    script: str, args: list[str], payload: dict[str, object], home: Path
) -> subprocess.CompletedProcess[str]:
    assert NODE is not None
    return subprocess.run(
        [NODE, str(REPO / "scripts" / "agent-compat" / script), *args],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO,
        env={"HOME": str(home), "PATH": str(Path(NODE).parent) + ":/usr/bin:/bin"},
        timeout=30,
    )


@pytest.fixture
def empty_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    return home


@pytest.mark.parametrize("command", HOST_BLOCKED)
def test_qwen_guard_exits_2_with_reason(command: str, empty_home: Path) -> None:
    result = _run_host(
        "destructive-git-guard.mjs",
        [],
        {"tool_input": {"command": command}},
        empty_home,
    )
    assert result.returncode == 2, command
    assert DENY_REASON in result.stderr


@pytest.mark.parametrize("command", HOST_BLOCKED)
def test_codex_adapter_blocks_with_json(command: str, empty_home: Path) -> None:
    result = _run_host(
        "codex-hook-adapter.mjs",
        ["pre-tool"],
        {"tool_name": "Bash", "tool_input": {"command": command}},
        empty_home,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"decision": "block", "reason": DENY_REASON}


@pytest.mark.parametrize("command", HOST_BLOCKED)
def test_vibe_adapter_denies_with_json(command: str, empty_home: Path) -> None:
    result = _run_host(
        "vibe-hook-adapter.mjs",
        ["pre-tool"],
        {"tool_name": "bash", "tool_input": {"command": command}},
        empty_home,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"decision": "deny", "reason": DENY_REASON}


@pytest.mark.parametrize("command", HOST_ALLOWED)
def test_hosts_allow_near_misses(command: str, empty_home: Path) -> None:
    qwen = _run_host(
        "destructive-git-guard.mjs", [], {"tool_input": {"command": command}}, empty_home
    )
    codex = _run_host(
        "codex-hook-adapter.mjs",
        ["pre-tool"],
        {"tool_name": "Bash", "tool_input": {"command": command}},
        empty_home,
    )
    vibe = _run_host(
        "vibe-hook-adapter.mjs",
        ["pre-tool"],
        {"tool_name": "bash", "tool_input": {"command": command}},
        empty_home,
    )
    assert (qwen.returncode, qwen.stderr) == (0, "")
    assert (codex.returncode, codex.stdout) == (0, "")
    assert (vibe.returncode, vibe.stdout) == (0, "")
