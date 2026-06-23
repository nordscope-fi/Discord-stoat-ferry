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
