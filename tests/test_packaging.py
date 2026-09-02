"""Packaging invariants — guards for defects invisible to the normal test run.

The frozen PyInstaller binary is never exercised by pytest (tests run from source),
so a data-file omission in ferry.spec passes every test yet breaks the shipped app.
These lightweight existence/text checks are the cheap first line of defense until a
full build-and-inspect smoke test exists.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Anchored with a leading newline on purpose. ferry.spec's icon block contains
# `elif sys.platform == "darwin":`, whose text CONTAINS `if sys.platform == "darwin":`
# as a substring, so an unanchored split matches there first -- which made an earlier
# draft of these tests inspect icon code and pass vacuously. The `\n` cannot match
# inside `elif` because the character before `if` is `l`, not a newline.
_DARWIN_BRANCH = '\nif sys.platform == "darwin":'


def test_feedback_minor_release_surfaces_agree() -> None:
    """The feature release and its public promises move as one contract."""

    expected = "2.40.3"
    pyproject = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((_REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    runtime = (_REPO_ROOT / "src" / "discord_ferry" / "__init__.py").read_text(encoding="utf-8")
    changelog = (_REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert pyproject["project"]["version"] == expected
    assert f'__version__ = "{expected}"' in runtime
    root_records = [item for item in lock["package"] if item["name"] == "discord-ferry"]
    assert len(root_records) == 1
    assert root_records[0]["version"] == expected

    added_source = changelog.split("## [Unreleased]", 1)[1].split("### Changed", 1)[0]
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
    without dce_checksums.json. exporter/manager._verify_dce_checksum then hits
    FileNotFoundError and silently returns, skipping the supply-chain checksum gate
    added in v2.2.12 — but only in the shipped app, so source-run tests never saw it.
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
