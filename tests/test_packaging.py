"""Packaging invariants — guards for defects invisible to the normal test run.

The frozen PyInstaller binary is never exercised by pytest (tests run from source),
so a data-file omission in ferry.spec passes every test yet breaks the shipped app.
These lightweight existence/text checks are the cheap first line of defense until a
full build-and-inspect smoke test exists.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Anchored with a leading newline on purpose. ferry.spec's icon block contains
# `elif sys.platform == "darwin":`, whose text CONTAINS `if sys.platform == "darwin":`
# as a substring, so an unanchored split matches there first -- which made an earlier
# draft of these tests inspect icon code and pass vacuously. The `\n` cannot match
# inside `elif` because the character before `if` is `l`, not a newline.
_DARWIN_BRANCH = '\nif sys.platform == "darwin":'


def test_feedback_minor_release_surfaces_agree() -> None:
    """The feature release and its public promises move as one contract."""

    # The promise list belongs to the feedback release's changelog section. The
    # four version files must agree with each other at whatever the current
    # version is, so the current version is read, not pinned here.
    feedback_release = "2.41.0"
    pyproject = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((_REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    runtime = (_REPO_ROOT / "src" / "discord_ferry" / "__init__.py").read_text(encoding="utf-8")
    changelog = (_REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    expected = pyproject["project"]["version"]
    assert f'__version__ = "{expected}"' in runtime
    root_records = [item for item in lock["package"] if item["name"] == "discord-ferry"]
    assert len(root_records) == 1
    assert root_records[0]["version"] == expected

    added_source = changelog.split(f"## [{feedback_release}]", 1)[1].split("### Changed", 1)[0]
    added = " ".join(added_source.casefold().split())
    for promise in (
        "feedback",
        "ferry feedback",
        "app",
        "issue",
        "discussion",
        "public preview",
        "diagnostics",
        "private contact",
        "retry",
        "copy",
        "save",
        "coolify",
        "personal apps",
    ):
        assert promise in added, f"Unreleased Added does not cover {promise!r}"


def _fails_the_build_after(text: str, anchor: str, terminator: str) -> bool:
    """True when `exit 1` appears between `anchor` and the `terminator` that closes its block.

    Issue #146. A condition is not an assertion. The four release.yml guards below
    used to anchor only on the condition text, so deleting the `exit 1` from the
    branch body -- the single most likely way the gate gets weakened -- left every
    one of them green while the workflow stopped failing the build. Measured, not
    assumed: with one `exit 1` removed the whole group still passed.

    The region is bounded by the block's own closing token, not by a character count.
    `release.yml` carries seventeen `exit 1` lines, so a region that runs past the end
    of its block would find the NEXT gate's exit and pass even with its own deleted --
    the same vacuous shape this test exists to prevent. A fixed span cannot be right in
    both directions: the Windows union branch already sat 272 characters from its exit,
    so any budget loose enough to survive one added log line was also loose enough to
    reach the following branch.

    `terminator` is the block's closing token: the `}` at the step's indent for the
    PowerShell `if` blocks, and `esac` for the bash `case` statements, whose `exit 1`
    lives in the `*)` fallback rather than in the matched arm.
    """
    idx = text.find(anchor)
    assert idx != -1, f"anchor not found in release.yml: {anchor!r}"
    end = text.find(terminator, idx)
    assert end != -1, f"terminator {terminator!r} not found after anchor {anchor!r}"
    return "exit 1" in text[idx:end]


def test_ferry_spec_bundles_dce_checksums() -> None:
    """ferry.spec must bundle dce_checksums.json into the frozen binary.

    Regression: the spec only bundled templates/*.json, so the frozen binary shipped
    without dce_checksums.json. exporter/manager._verify_dce_checksum then hit
    FileNotFoundError and returned, skipping the supply-chain checksum gate added in
    v2.2.12, but only in the shipped app, so source-run tests never saw it. Since #973
    a missing checksum file refuses the download, so a binary built without it can no
    longer download DiscordChatExporter at all.
    """
    checksums = _REPO_ROOT / "src" / "discord_ferry" / "dce_checksums.json"
    assert checksums.is_file(), f"missing source data file: {checksums}"

    spec_text = (_REPO_ROOT / "ferry.spec").read_text(encoding="utf-8")
    # Must map into the "discord_ferry" package dir so manager.py's
    # importlib.resources.files("discord_ferry").joinpath("dce_checksums.json") resolves it.
    # A wrong dest dir would silently reintroduce the skipped-verification bug.
    assert re.search(r'dce_checksums\.json"\s*,\s*"discord_ferry"', spec_text), (
        "ferry.spec must map dce_checksums.json -> the 'discord_ferry' package dir in "
        "all_datas, else the frozen binary skips DCE checksum verification."
    )


def test_ferry_spec_collects_certifi() -> None:
    """SC-134-12: cacert.pem must reach the frozen binary.

    pyinstaller-hooks-contrib ships hook-certifi.py, which fires only if the
    analysis sees certifi imported. core/http.py imports it at module level, so
    that holds today. Naming it in the spec too means the bundle does not
    depend on that import surviving a future refactor.
    """
    spec_text = (_REPO_ROOT / "ferry.spec").read_text(encoding="utf-8")
    # Match the CALL, not the word. ferry.spec carries a comment mentioning
    # certifi, so a substring check would pass even with the collection deleted.
    assert re.search(r'collect_data_files\(\s*["\']certifi["\']\s*\)', spec_text), (
        "ferry.spec must call collect_data_files('certifi'), else cacert.pem may "
        "not be bundled and the TLS trust fix is inert in the shipped app"
    )
    # Anchored to the all_datas line. A bare certifi_datas search matches the
    # assignment above it, so dropping it from the concatenation would leave
    # this test green while nothing was bundled.
    assert re.search(r"all_datas\s*=[^\n]*certifi_datas", spec_text), (
        "certifi_datas must appear in the all_datas concatenation, not merely be assigned"
    )


def test_ferry_spec_builds_onedir_on_darwin() -> None:
    """macOS must build onedir, not onefile.

    Onefile puts a PyInstaller bootloader in front of Python; that bootloader is the
    process macOS registers with LaunchServices, and it waits in usleep() while the
    real app runs. macOS flags it unresponsive (a 99s hang report was recorded against
    v2.7.1), users force-quit it, and the SIGTERM orphans the pywebview window child,
    which then shows "Connection lost. Trying to reconnect..." forever.
    """
    spec_text = (_REPO_ROOT / "ferry.spec").read_text(encoding="utf-8")
    parts = spec_text.split(_DARWIN_BRANCH, 1)
    assert len(parts) == 2, "ferry.spec must branch packaging on sys.platform"
    body = parts[1]
    assert "exclude_binaries=True" in body, "darwin EXE must exclude binaries (onedir)"
    assert "COLLECT(" in body, "darwin build must COLLECT into a directory"
    assert "BUNDLE(" in body and "coll," in body, "BUNDLE must wrap the COLLECT output"


def test_troubleshooting_covers_the_connection_lost_banner() -> None:
    """Users search for the exact banner text when the window goes dead."""
    guide = (_REPO_ROOT / "docs" / "guides" / "troubleshooting.md").read_text(encoding="utf-8")
    assert "Connection lost" in guide


def test_release_workflow_archives_macos_bundle_with_ditto() -> None:
    """The macOS artifact must be archived with a symlink-preserving tool.

    The onedir bundle contains ~119 symlinks cross-linking Contents/MacOS,
    Contents/Frameworks and Contents/Resources. `zip -r` follows them: measured
    91 MB instead of 45 MB, and the extracted tree has real files where
    Contents/_CodeSignature/CodeResources recorded symlinks -- an invalid seal,
    which for a quarantined ad-hoc-signed app is the "Ferry.app is damaged"
    dialog rather than the Gatekeeper flow documented in install.md.
    """
    workflow = (_REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    macos_job = workflow.split("build-macos:", 1)[1].split("\n  release:", 1)[0]
    assert "ditto -c -k --sequesterRsrc --keepParent Ferry.app" in macos_job, (
        "macOS archive step must use ditto (symlink-preserving), naming Ferry.app "
        "explicitly so COLLECT's dist/Ferry/ directory is not swept in"
    )
    assert "zip -r" not in macos_job, "zip -r dereferences the bundle's symlinks"


def test_release_workflow_can_be_dispatched_manually() -> None:
    """The packaging path must be runnable before a tag exists.

    release.yml otherwise triggers only on tag push, so a packaging failure would
    first surface on an already-published tag.
    """
    workflow = (_REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    triggers = workflow.split("jobs:", 1)[0]
    assert "workflow_dispatch:" in triggers, "release.yml must allow manual dispatch"
    # workflow_dispatch alone is not enough: GitHub only offers it from the default
    # branch's copy, and auto-tag.yml pushes the tag the moment a version bump lands on
    # main, so there is no window between "dispatch available" and "tag created". The
    # pull_request trigger is what actually exercises packaging before a tag exists.
    assert "pull_request:" in triggers, (
        "release.yml must build on PRs that touch the packaging path"
    )
    assert "ferry.spec" in triggers, "the PR trigger must watch ferry.spec"


def _windows_smoke_step() -> str:
    workflow = (_REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    marker = "- name: Smoke-test the built binary"
    assert marker in workflow, "Windows binary smoke step not found"
    return workflow.split(marker, 1)[1].split("\n      - name:", 1)[0]


def _run_with_actions_runner_pwsh_wrapper(
    pwsh: str, tmp_path: Path, name: str, body: str
) -> subprocess.CompletedProcess[str]:
    script = tmp_path / f"{name}.ps1"
    script.write_text(
        "$ErrorActionPreference = 'stop'\n"
        f"{body}\n"
        "if ((Test-Path -LiteralPath variable:\\LASTEXITCODE)) { exit $LASTEXITCODE }\n",
        encoding="utf-8",
    )
    quoted_path = str(script).replace("'", "''")
    return subprocess.run(
        [pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", f". '{quoted_path}'"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_windows_smoke_cleanup_status_is_diagnostic_not_authoritative() -> None:
    """A best-effort process-tree cleanup must not override passed smoke assertions."""
    smoke_step = _windows_smoke_step()
    cleanup = smoke_step.split("# Kill the whole tree:", 1)[1].split("if (-not $served)", 1)[0]

    expected_in_order = (
        "$cleanupOutput = taskkill /T /F /PID $proc.Id 2>&1",
        "$cleanupExit = $LASTEXITCODE",
        "if ($cleanupExit -ne 0)",
        'Write-Host "GUI process tree cleanup returned $cleanupExit"',
        "$cleanupOutput | ForEach-Object { Write-Host $_ }",
        "$global:LASTEXITCODE = 0",
    )
    positions = [cleanup.find(fragment) for fragment in expected_in_order]
    assert all(position >= 0 for position in positions), (
        "Windows cleanup must capture taskkill output/status, report a nonzero cleanup, "
        "then clear the non-authoritative native exit status"
    )
    assert positions == sorted(positions), "Windows cleanup status handling is out of order"


def test_windows_smoke_cleanup_reset_preserves_real_assertion_failures(tmp_path: Path) -> None:
    """Exercise the workflow reset through PowerShell and the runner's exact exit wrapper."""
    pwsh = os.environ.get("FERRY_TEST_PWSH") or shutil.which("pwsh")
    if pwsh is None and sys.platform != "win32":
        pytest.skip("PowerShell is required on Windows CI and unavailable on this host")
    assert pwsh is not None, "Windows CI must provide pwsh for the release workflow contract test"

    smoke_step = _windows_smoke_step()
    cleanup = textwrap.dedent(
        "$cleanupOutput = taskkill"
        + smoke_step.split("$cleanupOutput = taskkill", 1)[1].split("if (-not $served)", 1)[0]
    )
    taskkill = "$cleanupOutput = taskkill /T /F /PID $proc.Id 2>&1"
    fake_cleanup = (
        "$cleanupOutput = & $pwsh -NoLogo -NoProfile -NonInteractive "
        "-Command '\"mock cleanup diagnostic\"; exit 5' 2>&1"
    )
    assert taskkill in cleanup, "cleanup capture command not found in the Windows smoke step"
    cleanup = cleanup.replace(taskkill, fake_cleanup, 1)
    setup = "$pwsh = (Get-Process -Id $PID).Path\n"

    stale = _run_with_actions_runner_pwsh_wrapper(
        pwsh,
        tmp_path,
        "stale-cleanup-status",
        setup + "$served = $true\n& $pwsh -NoLogo -NoProfile -NonInteractive -Command 'exit 5'\n",
    )
    assert stale.returncode != 0, stale.stdout + stale.stderr

    fixed = _run_with_actions_runner_pwsh_wrapper(
        pwsh,
        tmp_path,
        "cleanup-status-reset",
        setup + "$served = $true\n" + cleanup + "if (-not $served) { exit 1 }\n",
    )
    assert fixed.returncode == 0, fixed.stdout + fixed.stderr
    assert "GUI process tree cleanup returned 5" in fixed.stdout
    assert "mock cleanup diagnostic" in fixed.stdout

    real_failure = _run_with_actions_runner_pwsh_wrapper(
        pwsh,
        tmp_path,
        "real-smoke-failure",
        setup + "$served = $false\n" + cleanup + "if (-not $served) { exit 1 }\n",
    )
    assert real_failure.returncode == 1, real_failure.stdout + real_failure.stderr


def test_ferry_spec_keeps_onefile_off_darwin() -> None:
    """Windows must keep shipping a single Ferry-windows-x86_64.exe."""
    spec_text = (_REPO_ROOT / "ferry.spec").read_text(encoding="utf-8")
    non_darwin = spec_text.split(_DARWIN_BRANCH, 1)[1].split("\nelse:", 1)
    assert len(non_darwin) == 2, "ferry.spec must keep an else: branch for other platforms"
    body = non_darwin[1]
    assert "runtime_tmpdir=None" in body, "non-darwin build must stay onefile"
    assert "exclude_binaries" not in body, "non-darwin build must not become onedir"
    # Without these the branch still "looks onefile" while shipping an executable with
    # no bundled assets at all.
    assert "a.binaries" in body and "a.datas" in body, (
        "the onefile EXE must still receive the collected binaries and data files"
    )


def test_exe_common_carries_every_shared_argument() -> None:
    """Both branches build their EXE from _EXE_COMMON, which the branch-body tests
    never see -- so a silently dropped argument (icon, console, codesign_identity...)
    would change the shipped binary with a green suite."""
    spec_text = (_REPO_ROOT / "ferry.spec").read_text(encoding="utf-8")
    common = spec_text.split("_EXE_COMMON = dict(", 1)[1].split("\n)", 1)[0]
    for arg in (
        "name=",
        "debug=",
        "bootloader_ignore_signals=",
        "strip=",
        "upx=",
        "upx_exclude=",
        "console=",
        "disable_windowed_traceback=",
        "argv_emulation=",
        "target_arch=",
        "codesign_identity=",
        "entitlements_file=",
        "icon=",
    ):
        assert arg in common, f"{arg} dropped from the shared EXE arguments"


def test_release_job_only_publishes_on_a_tag() -> None:
    """workflow_dispatch runs must not reach the publish step.

    On a dispatch, github.ref is a branch ref and softprops/action-gh-release hard-fails
    ("GitHub Releases requires a tag"), which would make every manual run go red at the
    publish step -- indistinguishable at a glance from the packaging failure the dispatch
    exists to detect.
    """
    workflow = (_REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    release_job = workflow.split("\n  release:", 1)[1].split("steps:", 1)[0]
    assert "startsWith(github.ref, 'refs/tags/')" in release_job, (
        "the release job must be gated on a tag ref"
    )


def test_release_job_disables_uv_cache_before_requesting_pypi_oidc() -> None:
    """#1176. The privileged publishing job must not restore a build-job cache."""
    release_job = _jobs(_workflow_texts()["release.yml"])["release"]

    assert "id-token: write" in release_job, "test must inspect the PyPI publishing job"
    setup_uv_step = release_job.split("uses: astral-sh/setup-uv@", 1)[1].split("\n      - ", 1)[0]
    assert re.search(r"^\s+enable-cache:\s*false\s*$", setup_uv_step, re.MULTILINE), (
        "the PyPI publishing job must disable setup-uv caching"
    )


def test_release_workflow_asserts_the_union_branch_on_macos() -> None:
    """SC-134-18 companion: macOS must gate on the union branch too.

    Windows has asserted this since v2.13.0. Without the same check on macOS, a
    bundling regression that drops certifi from the .app ships unseen, because
    the macOS job otherwise only inspects symlinks and archive size.

    Anchored to the literal assertion text, not to the words "tls-check" or
    "trust-source" appearing somewhere. Matching those alone would stay green if
    the case statement were weakened to accept any output, which is exactly the
    hole this file already had to close once for the Windows job.
    """
    text = (_REPO_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    macos_block = text.split("build-macos:")[1]
    assert "tls-check" in macos_block, "the macOS job must run tls-check"
    assert re.search(r'\*"trust-source: union"\*', macos_block), (
        "the macOS job must gate on the union branch, not merely run the command"
    )
    assert "Contents/MacOS/Ferry" in macos_block, (
        "it must run the extracted bundle's inner binary, so the ditto round-trip is covered"
    )
    assert _fails_the_build_after(macos_block, '*"trust-source: union"*', "esac"), (
        "the union case must reach an exit 1, not merely echo an ::error:: and continue"
    )


def test_release_workflow_uses_supported_arm64_runner_and_checks_minimum_macos() -> None:
    """F17: the arm64 build must run on macos-15 and inspect both app manifests."""
    text = (_REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    macos_job = text.split("build-macos:", 1)[1].split("\n  release:", 1)[0]

    arm64_matrix = re.search(r"- runner: (macos-[^\n]+)\n\s+arch: arm64", macos_job)
    assert arm64_matrix is not None, "the macOS matrix must retain the arm64 build"
    assert arm64_matrix.group(1) == "macos-15", (
        "the arm64 build must use the supported macos-15 runner, not the retiring macos-14 image"
    )

    helper = "plist_minimum_system_version() {"
    assert helper in macos_job, "the archive check must inspect each app manifest"
    minimum_check = macos_job.split(helper, 1)[1].split("codesign --verify", 1)[0]
    assert "plutil -extract LSMinimumSystemVersion" in minimum_check
    assert "minimum_macos_version" in minimum_check, (
        "the build must inspect the Mach-O deployment target when the plist has no declaration"
    )
    assert 'test -n "$BUILT_MINIMUM"' in minimum_check
    assert 'test -n "$EXTRACTED_MINIMUM"' in minimum_check
    assert '"$BUILT_MINIMUM" != "$EXTRACTED_MINIMUM"' in minimum_check, (
        "the archive check must reject a bundle whose deployment target changes on extraction"
    )


@pytest.mark.parametrize(
    ("built_value", "extracted_value", "expected_status", "expected_report"),
    [
        ("11.0", "11.0", 0, "LSMinimumSystemVersion: built 11.0, extracted 11.0"),
        (
            "__ABSENT__",
            "__ABSENT__",
            0,
            "LSMinimumSystemVersion: built not declared, extracted not declared",
        ),
        ("11.0", "12.0", 1, "::error::archive changed LSMinimumSystemVersion"),
    ],
)
def test_release_workflow_checks_built_and_extracted_macos_manifests(
    tmp_path: Path,
    built_value: str,
    extracted_value: str,
    expected_status: int,
    expected_report: str,
) -> None:
    """Exercise the workflow's plist check for matching, absent, and changed values."""
    workflow = (_REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    macos_job = workflow.split("build-macos:", 1)[1].split("\n  release:", 1)[0]
    manifest_marker = "          # macos-15 replaces"
    manifest_check = macos_job[macos_job.index(manifest_marker) :].split(
        "          minimum_macos_version() {", 1
    )[0]

    built_plist = tmp_path / "dist" / "Ferry.app" / "Contents" / "Info.plist"
    extracted_plist = tmp_path / "_verify" / "Ferry.app" / "Contents" / "Info.plist"
    for plist, value in (
        (built_plist, built_value),
        (extracted_plist, extracted_value),
    ):
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(f"{value}\n", encoding="utf-8", newline="\n")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    plutil = bin_dir / "plutil"
    plutil.write_text(
        "#!/usr/bin/env bash\n"
        'printf \'%s\\n\' "$4" >> "$PLUTIL_CALLS"\n'
        "value=$(tr -d '\\n' < \"$4\")\n"
        'if [ "$value" = "__ABSENT__" ]; then exit 1; fi\n'
        "printf '%s\\n' \"$value\"\n",
        encoding="utf-8",
        newline="\n",
    )
    plutil.chmod(0o755)
    calls = tmp_path / "plutil-calls.log"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "PLUTIL_CALLS": str(calls),
    }
    bash = shutil.which("bash")
    assert bash is not None, "CI runs repository shell checks through bash"

    result = subprocess.run(
        [bash, "-o", "pipefail", "-c", 'APP="_verify/Ferry.app"\n' + manifest_check],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == expected_status, result.stdout + result.stderr
    assert expected_report in result.stdout
    assert calls.read_text(encoding="utf-8").splitlines() == [
        "dist/Ferry.app/Contents/Info.plist",
        "_verify/Ferry.app/Contents/Info.plist",
    ]


def test_macos_manifest_fixture_works_with_windows_default_newlines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plist fixture data must not inherit Windows text-file line endings."""
    write_text = Path.write_text

    def windows_write_text(
        path: Path,
        data: str,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> int:
        return write_text(
            path,
            data,
            encoding=encoding,
            errors=errors,
            newline="\r\n" if newline is None and path.suffix == ".plist" else newline,
        )

    monkeypatch.setattr(Path, "write_text", windows_write_text)
    test_release_workflow_checks_built_and_extracted_macos_manifests(
        tmp_path,
        "__ABSENT__",
        "__ABSENT__",
        0,
        "LSMinimumSystemVersion: built not declared, extracted not declared",
    )


def test_release_workflow_extracts_modern_and_legacy_macos_minimums() -> None:
    """The archive check must read each Mach-O minimum-version record PyInstaller ships."""
    workflow = (_REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    macos_job = workflow.split("build-macos:", 1)[1].split("\n  release:", 1)[0]
    function = macos_job.split("minimum_macos_version() {", 1)[1].split("\n          }", 1)[0]
    awk_program = function.split("awk '\n", 1)[1].rsplit("\n            '", 1)[0]

    modern = """\\
Load command 10
      cmd LC_BUILD_VERSION
  cmdsize 32
 platform 1
    minos 11.0
      sdk 15.5
"""
    legacy = """\\
Load command 9
      cmd LC_VERSION_MIN_MACOSX
  cmdsize 16
 version 10.13
     sdk 11.0
"""

    bash = shutil.which("bash")
    assert bash is not None, "CI runs repository shell checks through bash"
    for metadata, expected in ((modern, "11.0"), (legacy, "10.13")):
        result = subprocess.run(
            [bash, "-c", 'awk "$1"', "--", awk_program],
            input=metadata,
            check=True,
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == expected


def test_release_workflow_asserts_the_union_branch() -> None:
    """SC-134-18.

    A bare "trust-source" substring check would stay green even if the workflow's
    PowerShell condition were weakened from `-notmatch 'trust-source:\\s*union'` to
    `-notmatch 'trust-source'`, or if the `$tls.Code -ne 0` check next to it were
    deleted -- either change would stop the Windows gate from gating while this test
    kept passing. Assert the union branch itself is checked, not merely mentioned.
    """
    text = (_REPO_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert "tls-check" in text
    assert "trust-source" in text
    assert "core/http.py" in text, "http.py must be in the paths filter"
    assert re.search(r"trust-source:\\s\*union", text), (
        "release.yml must assert the union branch, not merely mention trust-source"
    )
    assert _fails_the_build_after(text, "-notmatch 'trust-source:\\s*union'", "\n          }"), (
        "the Windows union branch must reach an exit 1, not merely write an ::error:: line"
    )


def test_dependency_floors_cover_the_symbols_the_code_imports() -> None:
    """Each declared dependency floor must be high enough for the symbol our code imports.

    Regression #382: pyproject declared `nicegui>=2.0` but gui.py imports
    `ClientConnectionTimeout`, which first exists in nicegui 3.0.0. uv.lock pins 3.12.0
    and CI installs from the lock, so the declared floor was never exercised and an
    environment resolving nicegui 2.x failed with ImportError at import time.

    The inline comments on the floored dependencies already name the symbol and the
    version it landed in. This test parses those comments and asserts the floor is at
    least that version, so lowering a floor below the import's need, or adding an
    import that needs a higher version without raising the floor, fails here rather
    than at a user's import time.

    A dynamic variant (install the floor version and import the symbol) would be
    stronger but needs network in CI; this static guard matches the style of the rest
    of this file and closes the gap the lock was hiding.
    """
    pyproject = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    deps_block = pyproject.split("dependencies = [", 1)[1].split("]", 1)[0]

    # Each entry: (package, symbol, version_it_landed_in). Sourced from the inline
    # comment on the dependency line. Add a row when a new floor gets a symbol reason.
    constraints = [
        ("aiohttp", "encode_basic_auth", "3.14.0"),
        ("nicegui", "ClientConnectionTimeout", "3.0.0"),
    ]

    for package, _symbol, landed in constraints:
        pattern = re.compile(rf'"{re.escape(package)}>=(\d+(?:\.\d+)*(?:\.\d+)*)"')
        match = pattern.search(deps_block)
        assert match is not None, (
            f"{package} has a declared floor with a symbol reason but no '>=' floor "
            f"was found in [project.dependencies]"
        )
        declared = tuple(int(p) for p in match.group(1).split("."))
        required = tuple(int(p) for p in landed.split("."))
        # Pad to equal length for the tuple comparison.
        width = max(len(declared), len(required))
        declared = declared + (0,) * (width - len(declared))
        required = required + (0,) * (width - len(required))
        assert declared >= required, (
            f"{package} floor {match.group(1)} is below {landed}, the version "
            f"{_symbol} landed in and the code imports"
        )


def test_release_workflow_asserts_the_proxy_keys() -> None:
    """SC-135-45. Killing: a workflow test that checks the key is MENTIONED
    rather than ASSERTED. That exact defect shipped in v2.13.0's plan, where
    weakening the Windows gate would have left the test green. Anchored to the
    assertion's structure.
    """
    text = (_REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert re.search(r"proxy-source:\\s\*", text), "the Windows regex must assert, not mention"
    assert re.search(r'\*"proxy-source: "\*', text), "the macOS glob must assert"
    assert _fails_the_build_after(text, "-notmatch 'proxy-source:\\s*'", "\n          }"), (
        "the Windows proxy-key branch must reach an exit 1"
    )
    assert _fails_the_build_after(text, '*"proxy-source: "*', "esac"), (
        "the macOS proxy-key case must reach an exit 1"
    )


_USES_LINE = re.compile(r"^\s*(?:-\s+)?uses:\s*(\S+)(.*)$", re.MULTILINE)
_FULL_SHA = re.compile(r"@[0-9a-f]{40}$")
_RELEASE_COMMENT = re.compile(r"^\s+#\s+v\d+\.\d+\.\d+\s*$")


def test_workflow_actions_are_pinned_to_commit_shas() -> None:
    """#1003. A moving selector such as `@v6` or `@release/v1` lets a retag change what CI runs.

    Every remote `uses:` must name a 40-hex commit SHA and carry a trailing `# vX.Y.Z` comment so
    a reader can tell which release the SHA is. Local (`./`) and `docker://` references are not
    remote actions and are skipped. The test also asserts it found `uses:` lines at all, so a
    glob that matches nothing cannot pass vacuously.
    """
    workflows = sorted((_REPO_ROOT / ".github" / "workflows").glob("*.yml"))
    assert workflows, "no workflow files found"
    seen = 0
    problems: list[str] = []
    for workflow in workflows:
        for match in _USES_LINE.finditer(workflow.read_text(encoding="utf-8")):
            ref, rest = match.group(1), match.group(2)
            if ref.startswith(("./", "docker://")):
                continue
            seen += 1
            if not _FULL_SHA.search(ref):
                problems.append(f"{workflow.name}: {ref} is not pinned to a 40-hex commit SHA")
            elif not _RELEASE_COMMENT.match(rest):
                problems.append(f"{workflow.name}: {ref} has no trailing '# vX.Y.Z' comment")
    assert seen >= 10, f"expected the remote actions to be found, saw {seen}"
    assert not problems, "\n".join(problems)


def test_build_backend_is_pinned_to_an_exact_version() -> None:
    """#1003. `requires = ["hatchling"]` lets any future release build the shipped wheel."""
    pyproject = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requires = pyproject["build-system"]["requires"]
    assert pyproject["build-system"]["build-backend"] == "hatchling.build"
    assert requires, "build-system.requires is empty"
    for requirement in requires:
        assert re.fullmatch(r"[A-Za-z0-9_.-]+==\d+(\.\d+)*", requirement), (
            f"build requirement {requirement!r} is not pinned with =="
        )


# --- Workflow token authority (#968, #969) -------------------------------------------
#
# A workflow with no `permissions:` block gets the repository default, which is an
# owner setting this repo cannot pin. A top-level block that grants write scope to every
# job hands that scope to jobs that only build, including the jobs a pull request starts.
# These checks read the workflow text with the standard library only, because PyYAML is
# not a dev dependency of this project.

_WORKFLOW_DIR = _REPO_ROOT / ".github" / "workflows"
_TOP_PERMISSIONS = re.compile(r"^permissions:[ \t]*(.*)$", re.MULTILINE)
_JOB_HEADER = re.compile(r"^  ([A-Za-z0-9_-]+):[ \t]*$", re.MULTILINE)


def _workflow_texts() -> dict[str, str]:
    paths = sorted(_WORKFLOW_DIR.glob("*.yml"))
    assert paths, "no workflow files found"
    return {path.name: path.read_text(encoding="utf-8") for path in paths}


def _top_level_permissions(text: str) -> list[str] | None:
    """Return the `scope: level` lines of the top-level block, or None if it is missing."""
    match = _TOP_PERMISSIONS.search(text)
    if match is None:
        return None
    if match.group(1).strip():  # `permissions: read-all` or `{}` on one line
        return [match.group(1).strip()]
    lines: list[str] = []
    for line in text[match.end() :].splitlines()[1:]:
        if line.startswith("  ") and line.strip():
            lines.append(line.strip())
        elif line.strip():
            break
    return lines


def _jobs(text: str) -> dict[str, str]:
    """Split the `jobs:` section into {job id: job body}."""
    head, sep, body = text.partition("\njobs:\n")
    assert sep, "workflow has no jobs: section"
    headers = list(_JOB_HEADER.finditer(body))
    jobs: dict[str, str] = {}
    for index, header in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(body)
        jobs[header.group(1)] = body[header.end() : end]
    assert jobs, "no jobs found"
    return jobs


def _run_ferry_spec_with_modules(
    platform: str, module_names: list[str], *, collect_webview: bool = True
) -> subprocess.CompletedProcess[str]:
    """Execute the spec with a minimal PyInstaller build graph.

    The fake Analysis supplies only the analyzed pure modules. This makes the
    test exercise the spec's release guard without building or inspecting a
    host-specific executable.
    """
    harness = textwrap.dedent(
        """
        import runpy
        import sys
        import types

        class Analysis:
            def __init__(self, *args, **kwargs):
                self.pure = [(name, None, "PYMODULE") for name in sys.argv[2:]]
                self.scripts = []
                self.binaries = []
                self.datas = []

        class Artifact:
            def __init__(self, *args, **kwargs):
                pass

        hooks = types.ModuleType("PyInstaller.utils.hooks")
        hooks.collect_all = lambda name: ([], [], ["webview"] if sys.argv[3] == "1" else [])
        hooks.collect_data_files = lambda name: []
        hooks.collect_submodules = lambda name: []
        utils = types.ModuleType("PyInstaller.utils")
        package = types.ModuleType("PyInstaller")
        sys.modules.update(
            {
                "PyInstaller": package,
                "PyInstaller.utils": utils,
                "PyInstaller.utils.hooks": hooks,
            }
        )
        sys.platform = sys.argv[2]

        runpy.run_path(
            sys.argv[1],
            init_globals={
                "Analysis": Analysis,
                "PYZ": Artifact,
                "EXE": Artifact,
                "COLLECT": Artifact,
                "BUNDLE": Artifact,
            },
        )
        """
    )
    return subprocess.run(
        [
            sys.executable,
            "-c",
            harness,
            str(_REPO_ROOT / "ferry.spec"),
            platform,
            "1" if collect_webview else "0",
            *module_names,
        ],
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_ferry_spec_refuses_a_build_without_pywebview(platform: str) -> None:
    """#1281: a desktop build must stop before packaging without webview."""
    result = _run_ferry_spec_with_modules(platform, [])

    assert result.returncode != 0
    assert "webview" in result.stderr


def test_ferry_spec_refuses_a_build_when_collection_omits_pywebview() -> None:
    """#1281: a successful import is insufficient when collection is empty."""
    result = _run_ferry_spec_with_modules("darwin", ["webview"], collect_webview=False)

    assert result.returncode != 0
    assert "did not collect pywebview" in result.stderr


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_ferry_spec_allows_a_build_with_pywebview(platform: str) -> None:
    """#1281: the guard permits an Analysis that contains pywebview."""
    result = _run_ferry_spec_with_modules(platform, ["webview"])

    assert result.returncode == 0, result.stderr


def _release_asset_preflight() -> str:
    """Return the PR-reachable shell guard from the release workflow."""
    workflow = yaml.safe_load(
        (_REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["verify-release-assets"]["steps"]
    for step in steps:
        if step.get("name") == "Verify release assets":
            return str(step["run"])
    raise AssertionError("release asset verification step not found")


@pytest.mark.parametrize(
    ("assets", "empty_assets", "expected_exit"),
    [
        (
            [
                "Ferry-windows-x86_64.exe",
                "Ferry-macos-arm64.zip",
                "Ferry-macos-x86_64.zip",
            ],
            set(),
            0,
        ),
        (["Ferry-windows-x86_64.exe", "Ferry-macos-arm64.zip"], set(), 1),
        (
            [
                "Ferry-windows-x86_64.exe",
                "Ferry-macos-arm64.zip",
                "Ferry-macos-x86_64.zip",
            ],
            {"Ferry-macos-arm64.zip"},
            1,
        ),
        (
            [
                "Ferry-windows-x86_64.exe",
                "Ferry-macos-arm64.zip",
                "Ferry-macos-x86_64.zip",
                "unexpected.zip",
            ],
            set(),
            1,
        ),
    ],
)
def test_release_asset_preflight_accepts_only_the_three_published_assets(
    tmp_path: Path, assets: list[str], empty_assets: set[str], expected_exit: int
) -> None:
    """#1279: the read-only PR guard rejects missing and unexpected downloads."""
    if sys.platform == "win32":
        pytest.skip("The Ubuntu release guard requires a POSIX shell")
    asset_dir = tmp_path / "release-assets"
    asset_dir.mkdir()
    for asset in assets:
        (asset_dir / asset).write_bytes(b"" if asset in empty_assets else b"asset")

    result = subprocess.run(
        ["bash", "-c", _release_asset_preflight()],
        check=False,
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode == expected_exit, result.stderr


def test_release_asset_preflight_does_not_launch_windows_linux_stub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Ubuntu shell fixture must not launch Windows' Linux launcher."""
    workflow = yaml.safe_load(
        (_REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    )
    assert workflow["jobs"]["verify-release-assets"]["runs-on"] == "ubuntu-latest"
    monkeypatch.setattr(sys, "platform", "win32")

    def unexpected_run(*args: object, **kwargs: object) -> None:
        raise AssertionError("The Ubuntu-only fixture must not start Windows bash")

    monkeypatch.setattr(subprocess, "run", unexpected_run)
    with pytest.raises(pytest.skip.Exception, match="POSIX shell"):
        test_release_asset_preflight_accepts_only_the_three_published_assets(tmp_path, [], set(), 0)


def _job_permissions(job: str) -> list[str]:
    match = re.search(r"^    permissions:[ \t]*(.*)$", job, re.MULTILINE)
    if match is None:
        return []
    if match.group(1).strip():
        return [match.group(1).strip()]
    lines: list[str] = []
    for line in job[match.end() :].splitlines()[1:]:
        if line.startswith("      ") and line.strip():
            lines.append(line.strip())
        elif line.strip():
            break
    return lines


def _runs_on_pull_request(text: str) -> bool:
    triggers = text.split("\njobs:\n", 1)[0]
    return re.search(r"^  pull_request(_target)?:", triggers, re.MULTILINE) is not None


def test_every_workflow_declares_top_level_permissions() -> None:
    """#968. A workflow that omits `permissions:` inherits a setting this repo cannot pin."""
    missing = [
        name for name, text in _workflow_texts().items() if _top_level_permissions(text) is None
    ]
    assert not missing, f"workflows without a top-level permissions block: {missing}"


def test_no_workflow_uses_pull_request_target() -> None:
    """#969. `pull_request_target` runs with secrets and a write token on fork-supplied refs."""
    offenders = [
        name
        for name, text in _workflow_texts().items()
        if re.search(r"^\s*pull_request_target:", text, re.MULTILINE)
    ]
    assert not offenders, f"pull_request_target is not allowed: {offenders}"


def test_pull_request_jobs_hold_no_write_or_oidc_authority() -> None:
    """#969. A job a pull request can start must not hold a write token or an OIDC token.

    Same-repository pull requests receive the permissions the workflow declares, and
    release.yml also builds on `pull_request`. Write scope and `id-token: write` therefore
    have to sit on a job that only runs for a tag (an `if:` naming `refs/tags/`), never at
    the top level and never on a build job. The test asserts it saw a pull_request
    workflow and a gated job, so a parser that finds nothing cannot pass.
    """
    problems: list[str] = []
    pr_workflows = 0
    gated_jobs = 0
    for name, text in _workflow_texts().items():
        if not _runs_on_pull_request(text):
            continue
        pr_workflows += 1
        for line in _top_level_permissions(text) or []:
            if line.endswith(": write") or line == "write-all":
                problems.append(f"{name}: top-level permission {line!r} reaches pull_request jobs")
        for job_id, job in _jobs(text).items():
            writes = [p for p in _job_permissions(job) if p.endswith(": write")]
            if not writes:
                continue
            if_line = re.search(r"^    if:[ \t]*(.*)$", job, re.MULTILINE)
            if if_line is not None and "refs/tags/" in if_line.group(1):
                gated_jobs += 1
            else:
                problems.append(f"{name}: job {job_id!r} holds {writes} without a tag-only if:")
    assert pr_workflows >= 2, (
        f"expected ci.yml and release.yml to run on pull_request: {pr_workflows}"
    )
    assert gated_jobs >= 1, "expected the release job to hold its write scope behind a tag-only if:"
    assert not problems, "\n".join(problems)


def test_windows_signing_step_runs_only_for_tags() -> None:
    """#969. The signing certificate must not be handed to a step a pull request can reach."""
    text = _workflow_texts()["release.yml"]
    marker = "- name: Sign Windows binary"
    assert marker in text, "signing step not found"
    step = text.split(marker, 1)[1].split("\n      - ", 1)[0]
    assert "secrets.WINDOWS_CERT_PFX" in step, "signing step no longer reads the certificate"
    if_line = re.search(r"^        if:[ \t]*(.*)$", step, re.MULTILINE)
    assert if_line is not None, "signing step has no if:"
    assert "refs/tags/" in if_line.group(1), "signing step must be limited to tag runs"
