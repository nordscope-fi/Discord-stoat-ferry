"""Packaging invariants — guards for defects invisible to the normal test run.

The frozen PyInstaller binary is never exercised by pytest (tests run from source),
so a data-file omission in ferry.spec passes every test yet breaks the shipped app.
These lightweight existence/text checks are the cheap first line of defense until a
full build-and-inspect smoke test exists.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Anchored to column 0 on purpose: ferry.spec also contains an indented
# `elif sys.platform == "darwin":` in the icon-selection block, and an unanchored
# split matches THAT first -- which made an earlier draft of these tests inspect
# icon code and pass vacuously.
_DARWIN_BRANCH = '\nif sys.platform == "darwin":'


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


def test_ferry_spec_keeps_onefile_off_darwin() -> None:
    """Windows must keep shipping a single Ferry-windows-x86_64.exe."""
    spec_text = (_REPO_ROOT / "ferry.spec").read_text(encoding="utf-8")
    non_darwin = spec_text.split(_DARWIN_BRANCH, 1)[1].split("\nelse:", 1)
    assert len(non_darwin) == 2, "ferry.spec must keep an else: branch for other platforms"
    body = non_darwin[1]
    assert "runtime_tmpdir=None" in body, "non-darwin build must stay onefile"
    assert "exclude_binaries" not in body, "non-darwin build must not become onedir"
