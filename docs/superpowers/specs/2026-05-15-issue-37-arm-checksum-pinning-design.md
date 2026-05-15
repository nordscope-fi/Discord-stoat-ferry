# Design: ARM DCE checksum pinning + fail-loud on missing (issue #37)

**Date:** 2026-05-15
**Issue:** [#37](https://github.com/nordscope-fi/discord-stoat-ferry/issues/37)
**Ships as:** Single PR
**Status:** Spec — awaiting implementation (deferred to next session per user)

## Problem

`src/discord_ferry/dce_checksums.json` only pins SHA-256 for `win-x64`, `linux-x64`, `osx-x64`. Apple Silicon (`osx-arm64`) and ARM Linux (`linux-arm64`) binaries are downloaded **without** integrity verification because `_verify_dce_checksum` (in `src/discord_ferry/exporter/manager.py:76-77`) silently returns when no hash is pinned for the requested platform.

Apple Silicon is the default Mac since 2020. Silent failure mode means users have no signal anything is wrong.

## Architecture

Three changes:

### 1. Add ARM hashes to `dce_checksums.json`

Acquired by:
- `osx-arm64`: hash the already-downloaded binary at `/tmp/dce-test/DiscordChatExporter.Cli` from #23 investigation. `shasum -a 256 /tmp/dce-test/DiscordChatExporter.Cli.zip` (or unzipped path, depending on whether we hash the archive or the extracted binary — match whatever current `dce_checksums.json` does).
- `linux-arm64`: download once from `https://github.com/Tyrrrz/DiscordChatExporter/releases/download/2.47.1/DiscordChatExporter.Cli.linux-arm64.zip`, hash, commit.

### 2. Add `scripts/make-checksums.py` helper

Reusable script for future `DCE_VERSION` bumps. Behavior:

```python
# Usage:
#   python scripts/make-checksums.py 2.47.1            # print JSON to stdout
#   python scripts/make-checksums.py 2.47.1 --write    # update dce_checksums.json in-place

def main(version: str, write: bool):
    """Download every platform's DCE release asset, hash, output JSON."""
    platforms = ["win-x64", "linux-x64", "linux-arm64", "osx-x64", "osx-arm64"]
    hashes = {}
    for platform in platforms:
        url = f"https://github.com/Tyrrrz/DiscordChatExporter/releases/download/{version}/DiscordChatExporter.Cli.{platform}.zip"
        digest = _download_and_hash(url)
        hashes[platform] = digest
    output = {version: hashes}  # match existing dce_checksums.json schema
    if write:
        path = Path(__file__).parent.parent / "src/discord_ferry/dce_checksums.json"
        existing = json.loads(path.read_text()) if path.exists() else {}
        existing.update(output)
        path.write_text(json.dumps(existing, indent=2, sort_keys=True))
    else:
        json.dump(output, sys.stdout, indent=2, sort_keys=True)
```

Default behavior is print-to-stdout so users can review the diff before committing. `--write` flag for the update-in-place path.

### 3. Make `_verify_dce_checksum` fail loudly on missing hash

Current behavior (`exporter/manager.py:76-77`, per audit):
```python
if platform not in checksums:
    return  # silent pass
```

New behavior:
```python
if platform not in checksums:
    raise RuntimeError(
        f"No SHA-256 hash pinned for platform '{platform}' in dce_checksums.json. "
        f"Refusing to use unverified DCE binary. "
        f"File an issue at https://github.com/nordscope-fi/Discord-stoat-ferry/issues/new "
        f"to add the hash for this platform, or pin a different DCE_VERSION."
    )
```

## Components

| Component | Responsibility |
|-----------|----------------|
| `src/discord_ferry/dce_checksums.json` (UPDATED) | All 5 platforms pinned for 2.47.1 |
| `src/discord_ferry/exporter/manager.py:_verify_dce_checksum` (BEHAVIOR CHANGE) | Raises on missing hash instead of silent pass |
| `scripts/make-checksums.py` (NEW) | Reusable hash regeneration tool for future DCE bumps |
| `tests/test_exporter_manager.py` (UPDATED) | New test exercising the missing-checksum raise path |

## Data flow

```
User installs Ferry, runs first export
    ↓
download_dce(...) detects platform via _PLATFORM_MAP
    ↓
Downloads .zip from Tyrrrz release
    ↓
_verify_dce_checksum(platform, downloaded_bytes, pinned_hashes):
    if platform not in pinned_hashes:
        raise RuntimeError(...)  # NEW behavior
    if hash(downloaded_bytes) != pinned_hashes[platform]:
        raise RuntimeError(...)  # existing behavior
    # else: pass through to extraction
```

## Error handling

- Missing hash: clear actionable error message (filed-issue URL + `DCE_VERSION` reference).
- Hash mismatch: existing behavior preserved.
- Helper script: fails fast on download error with HTTP status; resumes on next platform if user retries (per-platform isolation).

## Testing

Unit tests in `tests/test_exporter_manager.py`:

- `test_verify_checksum_raises_on_missing_platform`: pass a synthetic platform key not in the JSON; assert `RuntimeError` raised with expected message substring.
- `test_verify_checksum_passes_on_known_platform_with_correct_hash`: existing test, ensure still passes.
- `test_verify_checksum_raises_on_known_platform_with_wrong_hash`: existing test, ensure still passes.
- `test_make_checksums_script_produces_valid_json`: invoke `scripts/make-checksums.py` against a synthetic version; assert output is valid JSON matching schema.

Manual smoke tests:
- Install Ferry on Apple Silicon → run first export → confirm `download_dce()` succeeds end-to-end with the new hash.
- Synthetic regression: temporarily remove `osx-arm64` from `dce_checksums.json` → confirm error message fires.

## Phasing

Single PR. All changes ship together because the new ARM hashes and the new fail-loud behavior must land atomically — landing the fail-loud first would break Apple Silicon users; landing the hashes first leaves the silent-skip vulnerability.

**Sequencing:** Independent of #23 / #34 / #35 / #36. Can ship anytime.

## Decision Accountability gate

| Claim | Verdict | Evidence |
|-------|---------|----------|
| "Silent skip is fine because no one's reported it" | FALSIFIED | Silent failure mode = users have no signal; absence of report ≠ absence of risk |
| "Apple Silicon is a small minority" | FALSIFIED | Default Mac since 2020 |
| "Hard-fail on missing hash will break Ferry for new platforms" | VERIFIED but acceptable | Failure is loud, message is actionable, new platforms get a clear path |
| "Helper script is worth the upfront work" | VERIFIED | Pays back on every DCE_VERSION bump; replaces error-prone manual hashing |
| "In-place mutation by default is too dangerous" | VERIFIED | Print-to-stdout is friendlier for diff review; `--write` flag explicit when wanted |

**Foundational?** YES — security/integrity contract.

## Risks

| Risk | Mitigation |
|------|------------|
| Hash for downloaded binary differs from canonical (e.g., GitHub re-encoded the zip) | Download once during this PR, hash, commit; future re-runs of helper script verify against the same canonical source |
| Tyrrrz changes release asset naming convention | Already a risk; `_PLATFORM_MAP` would need update; not specific to this PR |
| Apple Silicon binary becomes unavailable | Apple Silicon is supported by upstream DCE; if dropped, Ferry needs to either bundle a cross-arch binary or x64-via-Rosetta fallback — out of scope here |
| Helper script run for an unreleased DCE_VERSION | Download fails with 404; clear error |

## Deferrals

| Item | Why not now | Tracking | Trigger |
|------|-------------|----------|---------|
| Sigstore/cosign signature verification (richer than SHA-256) | Tyrrrz/DCE doesn't sign releases | (will file if upstream signing happens) | If Tyrrrz starts signing |
| Auto-update `dce_checksums.json` from a GitHub Action when DCE_VERSION bumps | Nice-to-have automation | (will file later) | After this PR proves the helper script works |
| Windows ARM (`win-arm64`) support | Not currently in `_PLATFORM_MAP`; no user demand | — | If a user reports Windows ARM |

## Open questions (for implementation, not blocking spec)

- Whether `make-checksums.py` should validate that the JSON it writes is well-formed before writing (defensive) or trust `json.dumps`. Lean toward trust + run pytest after.
- Whether to git-blame the existing `dce_checksums.json` to confirm we don't regress existing entries' hashes. Should match exactly; if they don't, that's a separate investigation.
- Whether to also document the helper script in `docs/` (operations guide) or keep instructions only in script docstring. Lean toward docstring + linked from `tests/fixtures/README.md` (which #35 introduces).

## Cross-references

- Issue #34's contract test (`download_dce` invocation) will exercise the new fail-loud behavior on CI's Linux x64 — should pass cleanly.
- Independent of #23 / #35 / #36 / #38 / #39.
