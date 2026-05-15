# Design: ARM DCE checksum pinning + fail-loud on missing (issue #37)

**Date:** 2026-05-15 (revised after critique 2026-05-16)
**Issue:** [#37](https://github.com/nordscope-fi/Discord-stoat-ferry/issues/37)
**Ships as:** Two PRs — v2.1.5 (additive pin only) → v2.2.0 (fail-loud + helper)
**Status:** Spec — awaiting implementation (deferred to next session per user)

## Revisions from critique pass (2026-05-16)

The original spec contained a SHIPPING-BUG-CLASS error: it instructed hashing the extracted DCE binary, but `manager.py:79` actually hashes raw `.zip` bytes. Implementing as-written would have generated hashes that never matched at runtime → bricked Apple Silicon on first run. This revision corrects that and addresses six other findings.

| Critique finding | Resolution in this revision |
|------------------|---------------------------|
| Hash target ambiguous (extracted binary vs zip) | **Section "What to hash" added — unambiguous: hash the `.zip` archive bytes, not extracted binary** |
| `DCENotFoundError` vs `RuntimeError` inconsistency | Use `DCENotFoundError` (the existing taxonomy) — Section "Error type" |
| No user-facing escape hatch documented | Error message points at `download_dce(skip_verify=True)` workaround — Section "Fail-loud message" |
| `make-checksums.py` provenance risk if wired to CI | Helper marked human-run-only with explicit safety notes — Section "Helper safety model" |
| No schema-validation test for `dce_checksums.json` | Added — Section "Tests" |
| Atomicity claim for single PR was overstated | **Two-PR phased rollout adopted** — v2.1.5 pin only (additive); v2.2.0 fail-loud (breaking) |
| Missing-version handling unaddressed | Added — fail on missing version key OR missing platform within version |

## Problem

`src/discord_ferry/dce_checksums.json` only pins SHA-256 for `win-x64`, `linux-x64`, `osx-x64`. Apple Silicon (`osx-arm64`) and ARM Linux (`linux-arm64`) binaries are downloaded **without** integrity verification because `_verify_dce_checksum` (in `src/discord_ferry/exporter/manager.py:75-77`) silently returns when no hash is pinned for the requested platform.

Apple Silicon is the default Mac since 2020. Silent failure mode means users have no signal anything is wrong.

## What to hash (CRITICAL — corrected from original spec)

`manager.py:79` computes `hashlib.sha256(zip_data).hexdigest()` where `zip_data` is the raw `.zip` archive bytes downloaded from the GitHub release (line 192: `data = await resp.read()`). The hash is computed BEFORE extraction (line 218: `_verify_dce_checksum(data, DCE_VERSION, platform_key)` runs before `zf.extractall`).

**Acquisition steps:**
- `osx-arm64`: hash the `.zip` archive that was downloaded during #23 investigation. The path was the GitHub release asset, e.g. `DiscordChatExporter.Cli.osx-arm64.zip` — NOT the extracted `DiscordChatExporter.Cli` binary. If the .zip is no longer on disk, re-download from `https://github.com/Tyrrrz/DiscordChatExporter/releases/download/2.47.1/DiscordChatExporter.Cli.osx-arm64.zip` and `shasum -a 256` the file.
- `linux-arm64`: download `https://github.com/Tyrrrz/DiscordChatExporter/releases/download/2.47.1/DiscordChatExporter.Cli.linux-arm64.zip` once, hash it.

**Verification:** before committing the new hashes, sanity-check the existing entries by re-hashing the existing platforms' zips. They MUST match what `dce_checksums.json` already contains. If they don't, that's a separate investigation (the existing hashes might be wrong, or the asset URL changed, or we hashed something different historically).

## Architecture

### v2.1.5 PR (additive pin only)

| Change | File | Why this PR |
|--------|------|-------------|
| Add `osx-arm64` SHA-256 to `dce_checksums.json` | `src/discord_ferry/dce_checksums.json` | Pure additive — silent-skip path becomes silent-pass for ARM Mac users |
| Add `linux-arm64` SHA-256 to `dce_checksums.json` | `src/discord_ferry/dce_checksums.json` | Pure additive — same |
| `_verify_dce_checksum` behavior unchanged | (no change) | Atomicity: keep silent-skip behavior so anyone who somehow ends up on a different platform still gets a working install |
| Schema-validation test for `dce_checksums.json` | `tests/test_exporter_manager.py` | Catches typo'd JSON commits going forward |
| CHANGELOG entry under `Fixed` (security note: silent-skip becomes silent-pass for ARM platforms) | CHANGELOG.md | Accurate framing |

This PR is risk-free: any user it affects (Apple Silicon, ARM Linux) goes from "unverified download" to "verified download." No user gets a worse experience.

### v2.2.0 PR (fail-loud + helper)

| Change | File | Why this PR |
|--------|------|-------------|
| Change `_verify_dce_checksum` to **raise `DCENotFoundError`** when version OR platform missing | `src/discord_ferry/exporter/manager.py:75-77` | Closes the silent-skip vulnerability for any future platform we add to `_PLATFORM_MAP` without a corresponding hash |
| Add `scripts/make-checksums.py` helper for future regeneration | `scripts/make-checksums.py` | Reusable for future `DCE_VERSION` bumps |
| New unit tests exercising fail-loud paths | `tests/test_exporter_manager.py` | Coverage |
| CHANGELOG entry under `Changed` with explicit upgrade guide ("if you encounter `DCENotFoundError: No SHA-256 pinned`, file an issue or use `download_dce(skip_verify=True)` as workaround") | CHANGELOG.md | Migration guidance |

This PR is breaking for any user on a platform we forgot to pin between v2.1.5 and v2.2.0. The v2.1.5 → v2.2.0 release notes must call this out explicitly.

## Error type

Use `DCENotFoundError` (the existing taxonomy in `src/discord_ferry/errors.py`), not `RuntimeError`. The original spec proposed `RuntimeError`; that was inconsistent with how `manager.py:81-84` (existing mismatch path) handles errors and would break upstream callers that catch `DCENotFoundError`.

## Fail-loud message

For missing version key:

```python
raise DCENotFoundError(
    f"DCE_VERSION '{DCE_VERSION}' is not pinned in dce_checksums.json. "
    f"Refusing to use unverified DCE binary. "
    f"Workaround: pass skip_verify=True to download_dce() at your own risk. "
    f"Long-term fix: file an issue at "
    f"https://github.com/nordscope-fi/Discord-stoat-ferry/issues/new "
    f"to add the hash for v{DCE_VERSION}, or pin a different DCE_VERSION in "
    f"src/discord_ferry/exporter/manager.py."
)
```

For missing platform within a known version:

```python
raise DCENotFoundError(
    f"No SHA-256 hash pinned for platform '{platform_key}' in dce_checksums.json "
    f"(version v{DCE_VERSION}). "
    f"Refusing to use unverified DCE binary. "
    f"Workaround: pass skip_verify=True to download_dce() at your own risk. "
    f"Long-term fix: file an issue at "
    f"https://github.com/nordscope-fi/Discord-stoat-ferry/issues/new "
    f"to add the hash for this platform."
)
```

The `--skip-dce-verify` workaround exists today (`download_dce(skip_verify=True)` per `manager.py:136,141,215`); the error message must mention it so users have a path forward instead of being walled off.

## Helper safety model

`scripts/make-checksums.py` MUST be human-run-only. Safety mechanisms:

1. **No CI integration.** Spec explicitly forbids wiring this script to GitHub Actions, Renovate, Dependabot, or any automation. A compromised Tyrrrz release would propagate hashes that endorse the compromise. The only safe runner is a human reviewing the diff against the Tyrrrz release page.
2. **Print Tyrrrz release URL alongside hashes** so the human can manually verify against the release page (`https://github.com/Tyrrrz/DiscordChatExporter/releases/tag/<version>`).
3. **Default behavior: print to stdout.** `--write` flag for in-place mutation. Human reviews `git diff` before committing.
4. **Banner in script docstring** stating: "Do NOT run this script from CI. The hashes it produces are only as trustworthy as the runtime environment + GitHub's CDN at the moment of execution. Always cross-check against the Tyrrrz release page."

```python
# scripts/make-checksums.py
"""Regenerate src/discord_ferry/dce_checksums.json for a specific DCE version.

WARNING: HUMAN-RUN ONLY. Do NOT wire to CI, Renovate, Dependabot, or any
automation. The hashes this script produces are only trustworthy if a human
verifies them against the Tyrrrz release page at the time of execution.

Usage:
    python scripts/make-checksums.py 2.47.1            # print JSON to stdout
    python scripts/make-checksums.py 2.47.1 --write    # update dce_checksums.json in-place

After running, ALWAYS:
    1. Open https://github.com/Tyrrrz/DiscordChatExporter/releases/tag/<version>
    2. Cross-check each printed hash against any published hash list (when available)
    3. Review `git diff src/discord_ferry/dce_checksums.json` before committing
"""
```

## Components

| Component | Responsibility |
|-----------|----------------|
| `src/discord_ferry/dce_checksums.json` (UPDATED in v2.1.5) | All 5 platforms pinned for 2.47.1 |
| `src/discord_ferry/exporter/manager.py:_verify_dce_checksum` (BEHAVIOR CHANGE in v2.2.0) | Raises `DCENotFoundError` on missing version OR missing platform |
| `scripts/make-checksums.py` (NEW in v2.2.0) | Reusable hash regeneration tool, human-run-only |
| `tests/test_exporter_manager.py` (UPDATED in BOTH PRs) | New tests: schema validation (v2.1.5), fail-loud paths (v2.2.0) |

## Data flow

```
User installs Ferry, runs first export
    -> download_dce(...) detects platform via _PLATFORM_MAP
    -> Downloads .zip from Tyrrrz release (raw bytes)
    -> _verify_dce_checksum(zip_data, version, platform_key):
        if version not in checksums:
            v2.1.5: silent return (additive)
            v2.2.0: raise DCENotFoundError(...)
        if platform_key not in checksums[version]:
            v2.1.5: silent return (additive)
            v2.2.0: raise DCENotFoundError(...)
        if hashlib.sha256(zip_data).hexdigest() != checksums[version][platform_key]:
            raise DCENotFoundError(...)  # existing behavior, unchanged
    -> proceed to extraction (existing behavior)
```

## Tests

### v2.1.5 PR adds:

- `test_dce_checksums_json_is_well_formed`: parses the file as JSON; asserts top-level is a dict; asserts at least one version key exists.
- `test_dce_checksums_json_covers_current_version`: asserts `DCE_VERSION` is a key in the JSON.
- `test_dce_checksums_json_covers_all_supported_platforms`: asserts every value in `_PLATFORM_MAP` is a key under `DCE_VERSION` in the JSON. **This is the test that prevents future ARM-style silent-skip vulnerabilities.**
- `test_dce_checksums_json_values_look_like_sha256`: each value matches `^[0-9a-f]{64}$`.

### v2.2.0 PR adds:

- `test_verify_checksum_raises_on_missing_version`: pass a synthetic version not in the JSON; assert `DCENotFoundError` raised with expected message substring including "skip_verify=True".
- `test_verify_checksum_raises_on_missing_platform`: pass a synthetic platform key not in the JSON; assert `DCENotFoundError` raised with expected message substring including "skip_verify=True".
- `test_verify_checksum_passes_on_known_platform_with_correct_hash`: existing test, ensure still passes.
- `test_verify_checksum_raises_on_known_platform_with_wrong_hash`: existing test, ensure still passes.
- `test_make_checksums_script_runs`: subprocess-invoke `scripts/make-checksums.py 2.47.1` (against the live Tyrrrz release — only run when `pytest --integration` flag set, mark `@pytest.mark.integration`); assert output is valid JSON. Skipped by default; run on releases.

### Manual smoke tests:

- Apple Silicon: install Ferry, run first export. Confirm `download_dce()` succeeds with the new hash.
- Synthetic regression: temporarily remove `osx-arm64` from `dce_checksums.json` on a v2.2.0 build → run export on Apple Silicon → confirm `DCENotFoundError` fires with workaround message.

## Decision Accountability gate

| Claim | Verdict | Evidence |
|-------|---------|----------|
| "Silent skip is fine because no one's reported it" | FALSIFIED | Silent failure mode = users have no signal; absence of report ≠ absence of risk |
| "Apple Silicon is a small minority" | FALSIFIED | Default Mac since 2020 |
| "Hard-fail on missing hash will break Ferry for new platforms" | VERIFIED but acceptable | Failure is loud, message is actionable, `skip_verify=True` workaround exists |
| "Helper script is worth the upfront work" | VERIFIED | Pays back on every DCE_VERSION bump; replaces error-prone manual hashing |
| "In-place mutation by default is too dangerous" | VERIFIED | Print-to-stdout is friendlier for diff review; `--write` flag explicit when wanted |
| "Single-PR atomicity" | **FALSIFIED in critique** | Pin in v2.1.5 (additive, no behavior change) → fail-loud in v2.2.0 (release-note migration) is genuinely safer |
| "Hash the extracted binary" | **FALSIFIED in critique** | `manager.py:79` hashes `zip_data` (raw zip bytes) — would have shipped a broken hash if implemented as originally specified |
| "RuntimeError is the right error type" | **FALSIFIED in critique** | Existing taxonomy uses `DCENotFoundError`; consistency required |
| "Helper script is safe to run from CI" | **FALSIFIED in critique** | Would silently endorse compromised releases; human-run-only is the only safe model |

**Foundational?** YES — security/integrity contract.

## Risks

| Risk | Mitigation |
|------|------------|
| Hash mismatch on existing platforms during sanity-check | If existing `dce_checksums.json` hashes don't match re-hashed zips, file separate investigation; do not commit revised hashes for existing platforms in v2.1.5 |
| Tyrrrz changes release asset naming convention | `_PLATFORM_MAP` would need update; not specific to this PR |
| Apple Silicon binary becomes unavailable upstream | Apple Silicon is supported by upstream DCE; if dropped, separate work item |
| Helper script run for an unreleased DCE_VERSION | Download fails with 404; clear error |
| Helper script run by mistake against compromised Tyrrrz release | Docstring banner + manual cross-check process; can't be fully prevented technically |
| User on already-broken platform between v2.1.5 and v2.2.0 | v2.1.5 pins all known platforms; only NEW platforms added between releases would hit this; v2.2.0 release notes must remind contributors to update `dce_checksums.json` when adding to `_PLATFORM_MAP` |

## Deferrals

| Item | Why not now | Tracking | Trigger |
|------|-------------|----------|---------|
| Sigstore/cosign signature verification (richer than SHA-256) | Tyrrrz/DCE doesn't sign releases | (will file if upstream signing happens) | If Tyrrrz starts signing |
| Auto-update `dce_checksums.json` from a GitHub Action when DCE_VERSION bumps | Per critique: would silently endorse compromised releases | (will NOT file) | Never — explicitly out of scope as security-incompatible |
| Windows ARM (`win-arm64`) support | Not currently in `_PLATFORM_MAP`; no user demand | — | If a user reports Windows ARM |
| Pre-commit hook ensuring `DCE_VERSION` bumps come with `dce_checksums.json` updates | Nice-to-have automation | (will file as follow-up) | If a contributor forgets to update the JSON in a future bump |

## Cross-references

- Issue #34's contract test (`download_dce` invocation) will exercise the new fail-loud behavior on CI's Linux x64 — should pass cleanly.
- Independent of #23 / #35 / #36 / #38 / #39.
