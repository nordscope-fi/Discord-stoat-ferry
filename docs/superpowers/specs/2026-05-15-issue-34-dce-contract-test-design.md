# Design: DCE contract test in CI (issue #34)

**Date:** 2026-05-15
**Issue:** [#34](https://github.com/nordscope-fi/discord-stoat-ferry/issues/34)
**Ships as:** Single PR after #23 lands
**Status:** Spec — awaiting implementation (deferred to next session per user)

## Problem

Issue #23's root cause was a regex written against an assumed DCE output format with no test that ever exercised real DCE. #23's PR replaces the parser with a correct one + adds a hand-derived fixture. But the moment we bump `DCE_VERSION` in the future, the same shape of bug can recur silently:

- DCE could drop or rename a CLI flag Ferry passes (`--token`, `-g`, `--media`, etc.) — the export would fail at startup.
- DCE could change the per-channel progress format — Ferry's parser would silently miss every line again.
- The hand-derived fixture would silently rot relative to reality.

A contract test that runs **real DCE** in CI catches drift on every PR, not at a customer's first export.

## Architecture

Two test files, both gated appropriately so they don't slow down every CI cell:

### Design A: real DCE binary `--help` test

**`tests/test_dce_contract.py`** (NEW):

```python
"""Contract test: real DCE binary still exposes the flags Ferry depends on."""

from __future__ import annotations

import platform
import subprocess
import sys

import pytest

from discord_ferry.exporter.manager import DCE_VERSION, detect_dotnet, download_dce


# Flags Ferry passes in `_build_dce_command()` (src/discord_ferry/exporter/runner.py:33).
# If DCE drops or renames any of these, the export will fail at runtime.
REQUIRED_FLAGS = (
    "exportguild",
    "--token",
    "-g",
    "--media",
    "--reuse-media",
    "--markdown",
    "--format",
    "--include-threads",
    "--output",
)


@pytest.mark.skipif(
    platform.system() != "Linux",
    reason="Contract test only runs on Linux CI; mac/win covered by mocked tests.",
)
@pytest.mark.skipif(
    sys.version_info[:2] != (3, 12),
    reason="Run real-DCE contract once per CI run, not per Python version.",
)
@pytest.mark.skipif(not detect_dotnet(), reason=".NET 8+ runtime not available")
@pytest.mark.asyncio
async def test_dce_help_lists_all_flags_ferry_uses(tmp_path, monkeypatch):
    """Real DCE --help output still contains every flag _build_dce_command() passes."""
    monkeypatch.setattr(
        "discord_ferry.exporter.manager._get_dce_dir",
        lambda: tmp_path / "dce" / DCE_VERSION,
    )
    dce_path = await download_dce(lambda _: None)
    assert dce_path.exists(), "download_dce returned a non-existent path"

    result = subprocess.run(
        [str(dce_path), "exportguild", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    output = result.stdout + result.stderr

    missing = [flag for flag in REQUIRED_FLAGS if flag not in output]
    assert not missing, (
        f"DCE v{DCE_VERSION} dropped these flags Ferry depends on: {missing}. "
        f"Either DCE renamed/removed the flag (update _build_dce_command in "
        f"src/discord_ferry/exporter/runner.py:33) or pin a different DCE_VERSION "
        f"in src/discord_ferry/exporter/manager.py."
    )
```

### Design C: parser fixture replay test

**`tests/test_dce_output_replay.py`** (NEW):

```python
"""Replay test: parser handles every line shape in the captured fixture.

Catches accidental regex regression even when DCE itself hasn't changed.
Complementary to Design A (which catches DCE-side drift).
"""

from pathlib import Path

from discord_ferry.exporter.dce_output import parse_dce_line


# Lines that are expected to fall through to kind="raw" (e.g., empty post-banner line).
# Keep this allowlist tiny and explicit; growth is a smell.
EXPECTED_RAW_LINES: frozenset[str] = frozenset({
    # populated during implementation as we identify legitimate raw cases
})


def test_parse_dce_line_handles_every_fixture_line(fixtures_dir: Path) -> None:
    sample = (fixtures_dir / "dce_stdout_sample.txt").read_text(encoding="utf-8")
    unexpected_raw: list[str] = []
    for line in sample.splitlines():
        if not line.strip() or line.startswith("#"):  # skip blanks and fixture-file comments
            continue
        parsed = parse_dce_line(line)
        if parsed.kind == "raw" and line not in EXPECTED_RAW_LINES:
            unexpected_raw.append(line)
    assert not unexpected_raw, (
        f"{len(unexpected_raw)} fixture lines fell through to raw "
        f"(expected: known parser kinds). First few: {unexpected_raw[:3]}"
    )
```

## CI workflow changes (`.github/workflows/ci.yml`)

Add three changes to the existing `lint-and-test` job:

### 1. .NET 8 setup (defensive)

```yaml
- name: Set up .NET 8 (for DCE contract test)
  if: matrix.python-version == '3.12'
  uses: actions/setup-dotnet@v4
  with:
    dotnet-version: "8.0.x"
```

`ubuntu-latest` currently ships .NET 8 SDK preinstalled, but a future image change could drop it. This step is cheap insurance (~5s on cold cache).

### 2. DCE binary cache

```yaml
- name: Cache DCE binary
  if: matrix.python-version == '3.12'
  uses: actions/cache@v4
  with:
    path: ~/.discord-ferry/bin/dce/2.47.1
    key: dce-${{ runner.os }}-2.47.1
```

Saves ~15s per CI run after first warm. Cache invalidates correctly on `DCE_VERSION` bumps because the version is in the cache key.

Note: the test uses `monkeypatch.setattr` to redirect `_get_dce_dir` into `tmp_path`, which would bypass this cache. Implementation must reconcile: either the cache covers the original `~/.discord-ferry/bin/dce/...` location AND the test uses that location (no monkeypatch), or accept the cache is unused by the contract test specifically (still useful for any future test/job that uses the canonical location). Pick during writing-plans.

### 3. Matrix sanity guard

The skipif gates mean if all 4 Python versions are removed except 3.11, the contract test silently doesn't run. Defend against this:

**Option (chosen):** A separate workflow job, hardcoded to Linux + Python 3.12, that runs ONLY the contract test:

```yaml
contract-test:
  name: Contract test (DCE binary)
  runs-on: ubuntu-latest
  steps:
    - uses: actions/checkout@v4
    - uses: astral-sh/setup-uv@v7
    - uses: actions/setup-dotnet@v4
      with:
        dotnet-version: "8.0.x"
    - name: Cache DCE binary
      uses: actions/cache@v4
      with:
        path: ~/.discord-ferry/bin/dce/2.47.1
        key: dce-${{ runner.os }}-2.47.1
    - run: uv sync --locked --extra dev --extra native
    - run: uv run pytest tests/test_dce_contract.py tests/test_dce_output_replay.py -v
```

This guarantees the contract test runs even if the matrix shape changes. Adds ~30s to CI total (parallelizable with `lint-and-test`).

## Behavior on failure / drift

Per brainstorming decision: **fail loudly with actionable message**.

Failure messages must:
1. Name the specific missing flag(s).
2. Point at `_build_dce_command()` in `src/discord_ferry/exporter/runner.py:33`.
3. Suggest two paths: update Ferry's flag list to match new DCE, OR revert the `DCE_VERSION` bump in `src/discord_ferry/exporter/manager.py`.
4. Include the `DCE_VERSION` value so failure output is self-contained.

## Components

| Component | Responsibility |
|-----------|----------------|
| `test_dce_contract.py` | Real DCE `--help` invocation; flag presence assertion |
| `test_dce_output_replay.py` | Fixture replay through `parse_dce_line`; raw-fallthrough guard |
| `.github/workflows/ci.yml` (job: `contract-test`) | Hardcoded Linux + Python 3.12 + .NET 8; runs the two tests |
| `.github/workflows/ci.yml` (existing `lint-and-test` job) | Gets `setup-dotnet` + cache steps for the python-3.12 matrix cell (so the skipif test runs) |

## Data flow

```
PR opened/pushed
    ↓
GitHub Actions triggers ci.yml
    ↓
parallel:
  - lint-and-test (matrix: python 3.10/3.11/3.12/3.13)
      ↓ on python-3.12 cell:
      .NET 8 set up + DCE cache restore
      ↓
      pytest runs everything including test_dce_contract.py (skipif gates pass)
  - contract-test (hardcoded: ubuntu-latest + python 3.12)
      ↓
      .NET 8 set up + DCE cache restore
      ↓
      pytest runs JUST the contract + replay tests
```

Both jobs run the contract test. The duplication is intentional — the per-matrix-cell run is convenient for developers running `pytest` locally; the dedicated job is the safety net.

## Error handling

- `download_dce()` raises on download failure (existing behavior). Test bubbles that up — failed download = failed CI = good signal.
- `subprocess.run` `timeout=30` — if DCE hangs on `--help`, test fails after 30s instead of CI's job timeout.
- `detect_dotnet()` returning False on `lint-and-test` cell triggers `skipif` and test silently passes — but the dedicated `contract-test` job has explicit `setup-dotnet` so .NET will always be present there.

## Testing the test

The test asserts presence of strings in DCE output. To validate the test itself works:

1. **Positive case:** test passes against current DCE 2.47.1 (manually verified during agent research; all 9 flags present).
2. **Negative case:** create a synthetic PR that adds a flag to `REQUIRED_FLAGS` that doesn't exist in DCE (e.g., `--non-existent-flag`). Test should fail with the expected message.
3. **Skip case:** verify on macOS local that the test skips (no .NET 8 standard) without error.

## Decision Accountability gate

| Claim | Verdict | Evidence |
|-------|---------|----------|
| "Captured fixture is enough" | FALSIFIED | Fixtures rot when DCE bumps; only live invocation catches drift |
| "Design A is sufficient" | PARTIALLY VERIFIED | A catches flag drift (worse failure mode); C catches regex regression at zero cost; both are warranted |
| "Design B (invalid-token weekly cron) needed for v1" | FALSIFIED | Narrow incremental coverage; defer until A+C prove themselves |
| "Matrix-only gating is sufficient" | FALSIFIED | If matrix shape changes, test silently disappears; dedicated job is needed |
| "Fail loudly is the right behavior" | VERIFIED | Whole point of contract test is to block bad bumps; warning is corner-cutting |

**Foundational?** YES — drift-prevention mechanism. Cannot defer.

## Phasing

Single PR. No split. The test infrastructure ships together; either both Design A and C work or the PR isn't ready.

**Sequencing:** Lands AFTER #23's PR (v2.1.4) because Design C depends on `parse_dce_line` existing, and the captured fixture needs to be the new one (not the broken old one).

## Deferrals

| Item | Why not now | Tracking | Trigger |
|------|-------------|----------|---------|
| Design B (invalid-token weekly cron) | Catches narrow incremental coverage; adds CI complexity; needs Discord-network access in CI | (will file if needed) | After A+C prove themselves for ≥2 weeks |
| Auto-bump `DCE_VERSION` via Renovate/Dependabot | Coupled to having confidence in the contract test | (out of scope for now) | After A+C prove themselves for ≥1 month |
| Property-based testing on parser (Hypothesis) | Higher value but bigger lift; Hypothesis dependency added | (will file if interest emerges) | If parser regressions still slip through |

## Open questions (for implementation, not blocking spec)

- Cache path reconciliation: monkeypatch redirects `_get_dce_dir` to `tmp_path`, bypassing the cache. Either change the test to use canonical location (no monkeypatch) so the cache works, or accept cache is unused by this specific test. Pick during writing-plans.
- Whether `EXPECTED_RAW_LINES` allowlist starts empty (and we discover legitimate cases via test failures) or is pre-populated from the fixture. Empty is more honest.
- Whether to add a `pytest --markers` entry for visibility. The project doesn't use marks today; adding one for "contract" is a precedent decision worth deferring.

## Cross-references

- Issue #23 — provides `parse_dce_line` (which this test depends on) and the new captured fixture
- Issue #35 — replaces the hand-derived fixture with a captured-real one; complements but doesn't replace this test
- Issue #36 (`isInline`) — independent, separate parser; could benefit from a similar contract approach but out of scope here
