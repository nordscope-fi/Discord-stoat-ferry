# Design: DCE contract test in CI (issue #34)

**Date:** 2026-05-15 (revised 2026-05-16)
**Issue:** [#34](https://github.com/nordscope-fi/discord-stoat-ferry/issues/34)
**Ships as:** Single PR after #23 lands
**Status:** Spec — awaiting implementation (deferred to next session per user)
**Blocked by:** [#23](https://github.com/nordscope-fi/discord-stoat-ferry/issues/23) — Design C depends on `parse_dce_line` existing, and the captured fixture must be the new (post-#23) one. PR for #34 will fail CI if merged before #23.

## Revisions from critique pass (2026-05-16)

The critique pass identified five critical findings + significant gaps in this spec. Resolutions:

| Critique finding | Resolution in this revision |
|------------------|---------------------------|
| Cache + monkeypatch contradiction (cache was dead code) | **Dropped monkeypatch; test uses real `_get_dce_dir()` location** |
| `REQUIRED_FLAGS` would silently drift | **Replaced duplicated list with introspection of `_build_dce_command`** |
| `setup-dotnet@v4` defensive step is unnecessary | **Dropped; trust ubuntu-24.04's preinstalled .NET 8** |
| Skipif version is dead weight | **Dropped; keep only the dedicated `contract-test` job** |
| `EXPECTED_RAW_LINES` allowlist = brittleness it claims to prevent | **Replaced with explicit per-line `(line_number, expected_kind)` assertions** |
| `timeout=30` uncalibrated | Bumped to 60s |
| Failure message line-number rot | Use function name only |
| Sequencing dependency on #23 understated | Explicit "Blocked by #23" wording added |

## Problem

Issue #23's root cause was a regex written against an assumed DCE output format with no test that ever exercised real DCE. #23's PR replaces the parser with a correct one + adds a hand-derived fixture. But the moment we bump `DCE_VERSION` in the future, the same shape of bug can recur silently:

- DCE could drop or rename a CLI flag Ferry passes (`--token`, `-g`, `--media`, etc.) — the export would fail at startup.
- DCE could change the per-channel progress format — Ferry's parser would silently miss every line again.
- The hand-derived fixture would silently rot relative to reality.

A contract test that runs **real DCE** in CI catches drift on every PR, not at a customer's first export.

## Architecture

Two test files, both run by a single dedicated `contract-test` CI job (Linux + Python 3.12 + .NET 8 from the preinstalled SDK on ubuntu-24.04):

### Design A: real DCE binary `--help` test

**`tests/test_dce_contract.py`** (NEW):

```python
"""Contract test: real DCE binary still exposes the flags Ferry depends on.

Uses the canonical `_get_dce_dir()` location (NO monkeypatch) so the CI cache
key `dce-${{ runner.os }}-2.47.1` actually populates and reuses the binary
across runs. Without this, every CI run would re-download ~30MB and hit
GitHub's unauthenticated rate limit (60/hr per IP).
"""

from __future__ import annotations

import subprocess
from unittest.mock import sentinel

import pytest

from discord_ferry.exporter.manager import DCE_VERSION, download_dce
from discord_ferry.exporter.runner import _build_dce_command


def _required_flags_from_runner() -> tuple[str, ...]:
    """Render `_build_dce_command` with sentinels and extract the flag tokens.

    This avoids duplicating Ferry's flag list in the test (which would silently
    drift from `_build_dce_command`). Any flag Ferry passes — present or future —
    is automatically picked up.

    Returns:
        Ordered tuple of every token in the rendered argv that looks like a
        subcommand or flag (i.e. starts with '-' or is the leading subcommand
        word). Values like the token string and server id are filtered out.
    """

    class _Cfg:
        discord_token = "SENTINEL_TOKEN"
        discord_server_id = "SENTINEL_GUILD"
        export_dir = sentinel.export_dir  # str() call below stringifies it

    argv = _build_dce_command(_Cfg(), sentinel.dce_path)
    # argv[0] is the dce path; argv[1] is the subcommand ("exportguild");
    # the rest alternates between flags ("--token", "-g", ...) and values.
    flags: list[str] = [argv[1]]  # subcommand
    flags.extend(tok for tok in argv[2:] if tok.startswith("-"))
    return tuple(flags)


REQUIRED_FLAGS = _required_flags_from_runner()


@pytest.mark.asyncio
async def test_dce_help_lists_all_flags_ferry_uses() -> None:
    """Real DCE --help output still contains every flag _build_dce_command() passes."""
    dce_path = await download_dce(lambda _: None)
    assert dce_path.exists(), "download_dce returned a non-existent path"

    result = subprocess.run(
        [str(dce_path), "exportguild", "--help"],
        capture_output=True,
        text=True,
        timeout=60,  # 60s headroom: cold-start dotnet + ~10s JIT on slow CI runners
    )
    output = result.stdout + result.stderr

    missing = [flag for flag in REQUIRED_FLAGS if flag not in output]
    assert not missing, (
        f"DCE v{DCE_VERSION} dropped these flags Ferry depends on: {missing}. "
        f"Either DCE renamed/removed the flag (update `_build_dce_command`) or "
        f"pin a different DCE_VERSION in `discord_ferry.exporter.manager`."
    )
```

Key design points:
- **No `monkeypatch`.** The test calls `download_dce()` straight, which writes to `~/.discord-ferry/bin/dce/2.47.1` — exactly the path the CI cache key targets. Cache hits skip the ~30MB download on every run after the first.
- **No `skipif`.** This file lives only in the dedicated `contract-test` job, which is hardcoded Linux + Python 3.12 + .NET 8 (from the runner image). If .NET vanishes from ubuntu-24.04, `download_dce` + `--help` will fail loudly — exactly the signal we want.
- **`REQUIRED_FLAGS` is derived from `_build_dce_command`.** Cannot drift. Adding a flag to `_build_dce_command` automatically adds it to the contract assertion.
- **`timeout=60`.** Cold-start dotnet + JIT on the cheapest GitHub runners (`ubuntu-latest` 2-core) can hit ~10-15s; 60s is comfortable headroom without dragging out a flake.

### Design C: parser fixture replay test

**`tests/test_dce_output_replay.py`** (NEW):

```python
"""Replay test: parser handles every line shape in the captured fixture.

Catches accidental regex regression even when DCE itself hasn't changed.
Complementary to Design A (which catches DCE-side drift).

Design choice: explicit per-line (line_number, expected_kind) assertions
rather than an allowlist of "expected raw" strings. The allowlist pattern
would let new fixture lines silently fall into raw — exactly the
judgment-call drift that caused #23.
"""

from __future__ import annotations

from pathlib import Path

from discord_ferry.exporter.dce_output import parse_dce_line

# Each tuple is (1-indexed line number in fixture, expected `parsed.kind`).
# When the fixture changes, this list MUST change in lockstep — that is the
# point. Adding a fixture line without claiming what it parses to will fail
# the length assertion below.
#
# The fixture path is `tests/fixtures/dce_stdout_sample.txt`; numbers below
# count every line including blanks (no skipping).
EXPECTED_LINES: list[tuple[int, str]] = [
    # Populated during implementation by walking the post-#23 fixture in order.
    # Example shape (illustrative, not final):
    # (1, "banner"),
    # (2, "raw"),       # post-banner blank → raw is the documented behavior
    # (3, "channel_progress"),
    # (4, "channel_progress"),
    # ...
]


def test_parse_dce_line_matches_expected_kinds_per_line(fixtures_dir: Path) -> None:
    """Every fixture line parses to the kind explicitly claimed for that line."""
    sample_path = fixtures_dir / "dce_stdout_sample.txt"
    lines = sample_path.read_text(encoding="utf-8").splitlines()

    # Length guard: forces the test to be updated when the fixture grows or
    # shrinks. Without this, new lines could silently go unverified.
    assert len(EXPECTED_LINES) == len(lines), (
        f"EXPECTED_LINES has {len(EXPECTED_LINES)} entries but fixture has "
        f"{len(lines)} lines. Update EXPECTED_LINES in lockstep with the fixture."
    )

    mismatches: list[str] = []
    for (line_no, expected_kind), line in zip(EXPECTED_LINES, lines, strict=True):
        parsed = parse_dce_line(line)
        if parsed.kind != expected_kind:
            mismatches.append(
                f"  line {line_no}: expected kind={expected_kind!r}, "
                f"got kind={parsed.kind!r} for: {line!r}"
            )

    assert not mismatches, "Parser kind mismatches:\n" + "\n".join(mismatches)
```

Key design points:
- **No allowlist.** Every fixture line has an explicit expected kind. A new fixture line with no entry triggers the length-guard assertion; a changed fixture line that parses differently triggers a per-line mismatch.
- **`(line_number, expected_kind)` tuples** make the failure message point at exactly which line drifted.
- **Length guard** is the lockstep enforcer. Without it, you could append a line to the fixture and the test would silently skip it.

## CI workflow changes (`.github/workflows/ci.yml`)

Single new job, parallel to `lint-and-test`. The `lint-and-test` matrix is unchanged — no skipif tests in matrix cells, no `setup-dotnet` step in matrix cells, no DCE cache in matrix cells.

```yaml
  contract-test:
    name: Contract test (DCE binary)
    runs-on: ubuntu-latest  # ubuntu-24.04 ships .NET 8 SDK preinstalled
    steps:
      - uses: actions/checkout@v6

      - name: Set up uv
        uses: astral-sh/setup-uv@v7
        with:
          python-version: "3.12"
          enable-cache: true
          cache-dependency-glob: "uv.lock"

      - name: Cache DCE binary
        uses: actions/cache@v4
        with:
          path: ~/.discord-ferry/bin/dce/2.47.1
          key: dce-${{ runner.os }}-2.47.1

      - name: Install dependencies
        run: uv sync --locked --extra dev --extra native

      - name: Sanity-check .NET 8
        run: dotnet --version

      - name: Run contract + replay tests
        run: uv run pytest tests/test_dce_contract.py tests/test_dce_output_replay.py -v
```

Notes:
- **No `actions/setup-dotnet@v4`.** ubuntu-24.04 (`ubuntu-latest`) ships .NET 8 SDK preinstalled — see `actions/runner-images` Ubuntu2404-Readme. The `dotnet --version` step exists only as a loud-fail signal if the runner image ever changes.
- **DCE cache key includes the version** (`dce-${{ runner.os }}-2.47.1`). When `DCE_VERSION` bumps, the cache key changes, the old binary is not restored, the contract test downloads the new one and re-asserts the flag contract.
- **Cache path matches the canonical `_get_dce_dir()` location** because the test no longer monkeypatches. This is the fix for the original cache-was-dead-code finding.

## Behavior on failure / drift

Per brainstorming decision: **fail loudly with actionable message**.

Failure messages must:
1. Name the specific missing flag(s) (or, for Design C, the specific line number + observed-vs-expected kinds).
2. Point at `_build_dce_command` **by function name only** (no `src/.../runner.py:33`-style line refs — they rot when files are reformatted).
3. Suggest two paths: update Ferry's flag list to match new DCE, OR revert the `DCE_VERSION` bump in `discord_ferry.exporter.manager`.
4. Include the `DCE_VERSION` value so failure output is self-contained.

## Components

| Component | Responsibility |
|-----------|----------------|
| `test_dce_contract.py` | Real DCE `--help` invocation; flag presence assertion derived from `_build_dce_command` |
| `test_dce_output_replay.py` | Fixture replay through `parse_dce_line`; explicit per-line kind assertions + length guard |
| `.github/workflows/ci.yml` (job: `contract-test`) | Hardcoded Linux + Python 3.12; relies on ubuntu-24.04's preinstalled .NET 8; DCE cache; runs the two tests |

## Data flow

```
PR opened/pushed
    ↓
GitHub Actions triggers ci.yml
    ↓
parallel:
  - lint-and-test (matrix: python 3.10/3.11/3.12/3.13)  ← unchanged
  - contract-test (hardcoded: ubuntu-latest + python 3.12)
      ↓
      DCE cache restore (key: dce-Linux-2.47.1)
      ↓
      sanity: dotnet --version
      ↓
      pytest runs the contract + replay tests
        ↓ contract test:
        download_dce() → writes to ~/.discord-ferry/bin/dce/2.47.1 (cached)
          ↓ subprocess: dce exportguild --help (timeout=60s)
          ↓ assert REQUIRED_FLAGS (introspected from _build_dce_command) ⊆ output
        ↓ replay test:
        read fixture → parse each line → assert (line_no, kind) matches EXPECTED_LINES
```

## Error handling

- `download_dce()` raises on download failure (existing behavior). Test bubbles that up — failed download = failed CI = good signal.
- `subprocess.run` `timeout=60` — 60s is comfortable headroom for cold-start dotnet + JIT on the cheapest GitHub runners. If DCE genuinely hangs on `--help`, test fails after 60s instead of CI's job timeout.
- `dotnet --version` step fails loudly if a future ubuntu-latest image drops the .NET 8 SDK. No silent skip.

## Testing the test

The test asserts presence of strings in DCE output. To validate the test itself works:

1. **Positive case:** test passes against current DCE 2.47.1 (manually verified during agent research; all 9 flags present).
2. **Negative case:** add a synthetic flag (e.g., `"--non-existent-flag", "x"`) to `_build_dce_command` in a throwaway branch and confirm the contract test fails with a message naming `--non-existent-flag`.
3. **Cache hit case:** run the workflow twice on the same SHA; second run should restore the cache and skip the ~30MB download (visible in the `Post Cache DCE binary` step's "Cache hit" log line).

## Decision Accountability gate

| Claim | Verdict | Evidence |
|-------|---------|----------|
| "Captured fixture is enough" | FALSIFIED | Fixtures rot when DCE bumps; only live invocation catches drift |
| "Design A is sufficient" | PARTIALLY VERIFIED | A catches flag drift (worse failure mode); C catches regex regression at zero cost; both are warranted |
| "Design B (invalid-token weekly cron) needed for v1" | FALSIFIED | Narrow incremental coverage; defer until A+C prove themselves |
| "Matrix-only gating is sufficient" | FALSIFIED | If matrix shape changes, test silently disappears; dedicated job is needed |
| "Fail loudly is the right behavior" | VERIFIED | Whole point of contract test is to block bad bumps; warning is corner-cutting |
| "Skipif-gated copy in `lint-and-test` adds defense-in-depth" | FALSIFIED (revised) | Local devs without .NET get no signal; with .NET they hit network on every test run; dedicated job is the only honest signal |
| "`setup-dotnet@v4` is cheap insurance" | FALSIFIED (revised) | ubuntu-24.04 ships .NET 8 SDK preinstalled and documented; the step is dead 5-10s per run; `dotnet --version` sanity check is the right shape |
| "`REQUIRED_FLAGS` constant is fine for now" | FALSIFIED (revised) | Duplicating Ferry's flag list invites drift; introspecting `_build_dce_command` is the only way to guarantee lockstep |
| "`EXPECTED_RAW_LINES` allowlist is acceptable" | FALSIFIED (revised) | "Discover via failures" is the same judgment-call pattern that caused #23; explicit per-line kind assertions are the honest design |

**Foundational?** YES — drift-prevention mechanism. Cannot defer.

## Phasing

Single PR. No split. The test infrastructure ships together; either both Design A and C work or the PR isn't ready.

**Sequencing:** **Blocked by #23.** Lands AFTER #23's PR (v2.1.4) because:
1. Design C depends on `parse_dce_line` (introduced by #23).
2. The captured fixture must be the new post-#23 one, not the broken pre-#23 one.
3. The `EXPECTED_LINES` table is built by walking that fixture in order; without #23 the fixture and parser don't exist in their final shape.

A PR for #34 opened before #23 lands will fail CI on the import of `parse_dce_line`. This is intentional — the dependency is enforced by the build, not by reviewer discipline.

## Deferrals

| Item | Why not now | Tracking | Trigger |
|------|-------------|----------|---------|
| Design B (invalid-token weekly cron) | Catches narrow incremental coverage; adds CI complexity; needs Discord-network access in CI | (will file if needed) | After A+C prove themselves for ≥2 weeks |
| Auto-bump `DCE_VERSION` via Renovate/Dependabot | Coupled to having confidence in the contract test | (out of scope for now) | After A+C prove themselves for ≥1 month |
| Property-based testing on parser (Hypothesis) | Higher value but bigger lift; Hypothesis dependency added | (will file if interest emerges) | If parser regressions still slip through |
| `pytest --markers` entry for "contract" | Project doesn't use marks today; precedent decision worth deferring | (out of scope) | If a third gated-test category emerges |

## Open questions (for implementation, not blocking spec)

- Whether to hash the DCE binary post-extract in the contract job and warn if it differs from the pinned `dce_checksums.json` value. Probably belongs to #37, not here.
- Whether to also surface the contract job's pass/fail badge separately in the README. Cosmetic; defer until a real signal emerges that contributors miss the failure.

## Cross-references

- Issue #23 — provides `parse_dce_line` (which Design C depends on) and the new captured fixture; **#34 is blocked by #23**
- Issue #35 — replaces the hand-derived fixture with a captured-real one; complements but doesn't replace this test
- Issue #36 (`isInline`) — independent, separate parser; could benefit from a similar contract approach but out of scope here
- Issue #37 (ARM checksums) — owns binary hashing; if a "verify checksum in CI" check is wanted, it should be threaded through #37's `_verify_dce_checksum`, not duplicated here
