# Design: replace hand-authored fixtures with captured-real (issue #35)

**Date:** 2026-05-15
**Issue:** [#35](https://github.com/nordscope-fi/discord-stoat-ferry/issues/35)
**Ships as:** Single PR after #23 (v2.2.0) and #36 land
**Status:** Spec — awaiting implementation (deferred to next session per user)

## Problem

Every fixture under `tests/fixtures/` is hand-authored by the same developer in the same commits as the consumer code. At least one (`dce_stdout_sample.txt`) was provably wrong since 2026-02-28 (caused #23). At least one other (`simple_channel.json`) uses the correct schema (`isInline`) but is otherwise idealized: round timestamps, sequential IDs, no real-world noise. The same anti-pattern likely affects more.

Replace each fixture with a transcript captured from a real DCE 2.47.1 run against a dedicated test Discord server, so the fixtures are the contract definition for the parser layer.

## Architecture

### One capture script (`scripts/capture-fixtures.py`)

Python script (matches project language; can reuse Ferry's own `download_dce()` and `run_dce_export()`). Steps:

1. Read configuration: test-server ID + Discord token from env vars (`FERRY_TEST_SERVER_ID`, `FERRY_TEST_DISCORD_TOKEN`).
2. Invoke DCE via Ferry's existing `run_dce_export()` to export the test server.
3. For each captured JSON file in the export output, run `IdMapper` to deterministically replace real Discord IDs with fake-but-realistic snowflakes.
4. For each, replace user display names with `TestUser1`, `TestUser2`, etc. (also via deterministic mapping).
5. Replace CDN URLs (`cdn.discordapp.com/...`) with placeholder URLs (`cdn.example.test/...`).
6. Write the scrubbed JSON files to `tests/fixtures/` overwriting the existing ones.
7. Also capture the DCE stdout/stderr transcript to `tests/fixtures/dce_stdout_sample.txt`.

### Test server requirements (provisioned by user, not in scope of this PR)

A dedicated Discord test server with the following content:
- 1 text channel ("general") with ~10 messages including:
  - One plain text message
  - One message with an attachment (small image)
  - One message with an embed containing 3 inline fields and 2 non-inline fields (covers #36's isInline test)
  - One message with a reaction (custom emoji)
  - One pinned message
- 1 thread (under "general") with 3 messages
- 1 forum channel with 1 forum post + 2 replies
- (Optional) 1 voice channel (Ferry skips voice channels but DCE may emit them)

User credentials:
- A Discord bot token OR user token with read access to the test server
- Server ID

These go into `.env` (gitignored) for local capture; into GitHub Actions secrets if we ever automate re-capture (out of scope).

### `IdMapper` class

```python
@dataclass
class IdMapper:
    """Deterministically map real Discord snowflakes to fake ones.

    Same real ID → same fake ID across all files in one capture run.
    Reproducible: rerunning against the same input produces the same output.
    """
    salt: bytes  # fixed salt committed to the script for reproducibility

    def map_id(self, real_id: str) -> str:
        # Generate fake snowflake from HMAC(salt, real_id), formatted as 18-19 digit string
        ...
```

Why deterministic: if message M references user U's avatar, the same user U in another message should map to the same fake ID. Hand-written replacement would be tedious and error-prone.

### Replaced fixtures

Per the issue's audit table:

| Fixture | Action |
|---------|--------|
| `dce_stdout_sample.txt` | REPLACED (captured DCE 2.47.1 stdout transcript) |
| `simple_channel.json` | REPLACED (captured single-channel real export) |
| `edge_cases.json` | REPLACED (captured channel with mixed media, edits, pins, stickers) |
| `markdown_rendered.json` | REPLACED (captured from a markdown-heavy channel) |
| `Test Server - general - Cool Thread [...].json` | REPLACED (captured real thread export) |
| `Test Server - Feedback Forum - Bug Report [...].json` | REPLACED (captured real forum-thread export) |
| `rollback_state.json` | KEEP (Ferry-internal state, not external) |

## Data flow

```
Peter runs:  uv run python scripts/capture-fixtures.py
    ↓
Loads FERRY_TEST_SERVER_ID + FERRY_TEST_DISCORD_TOKEN from .env
    ↓
Invokes Ferry's download_dce() → run_dce_export() against test server
    ↓
DCE writes raw JSON files + media to tmp directory
    ↓
For each JSON file:
  IdMapper rewrites IDs
  Display names replaced
  CDN URLs replaced
    ↓
Scrubbed files written to tests/fixtures/, overwriting hand-authored ones
    ↓
Capture script also dumps the DCE stdout transcript to dce_stdout_sample.txt
    ↓
Peter inspects, runs `git diff tests/fixtures/`, commits if right.
```

## Components

| Component | Responsibility |
|-----------|----------------|
| `scripts/capture-fixtures.py` | Orchestrates capture + scrubbing; user-invoked, not run in CI |
| `scripts/_id_mapper.py` (or inline) | Deterministic ID/name/URL scrubbing |
| `tests/fixtures/README.md` | Documents how to regenerate, what test server should contain |
| `.env.example` | Template showing required env vars (`FERRY_TEST_SERVER_ID`, `FERRY_TEST_DISCORD_TOKEN`) |
| `tests/fixtures/*.json` | Replaced contents (captured + scrubbed) |
| `tests/test_*.py` | Updated to use structural assertions (not exact values) where they previously asserted on hand-crafted IDs |

## Test brittleness mitigation

**Strategy: structural assertions, not value assertions.**

Today's tests assert things like `assert msg["author"]["id"] == "111"`. After this PR, those tests must assert structurally: `assert isinstance(msg["author"]["id"], str)` or `assert len(msg["author"]["id"]) >= 17` (snowflake length).

Where exact values matter to prove behavior (e.g., "embed has 3 inline fields"), tests assert on the SHAPE of the data not the literal IDs:

```python
# Before:
assert embed["fields"][0]["isInline"] == True

# After (still works, no change needed for boolean fields):
assert embed["fields"][0]["isInline"] == True

# Before:
assert message["author"]["name"] == "Peter"

# After:
assert message["author"]["name"].startswith("TestUser")
```

Where re-capturing might shift the order of returned fields (Discord doesn't guarantee order across API calls), tests use `set()` comparisons or `any()` predicates.

## Error handling

- Capture script fails fast if env vars missing (clear error message).
- Capture script fails fast if test server is empty / unreachable.
- IdMapper raises if asked to map non-snowflake input (catches scrub bugs).
- Tests using new fixtures fail loudly if structural shape changes (e.g., test expected 3 inline fields but only got 2 — test runner shows the actual fixture content).

## Phasing

Single PR. Within the PR:

1. **Capture infrastructure first:** `scripts/capture-fixtures.py` + `scripts/_id_mapper.py` + `tests/fixtures/README.md`.
2. **First capture:** user runs the script against their test server; commits the resulting fixtures.
3. **Test updates:** rewrite assertions to structural where they were value-based; ensure all tests pass against new fixtures.
4. **Cleanup:** remove old hand-authored fixtures (they should be overwritten in step 2 but make sure nothing is orphaned).

**Sequencing:** lands AFTER #23's PRs (v2.1.4 + v2.2.0) AND #36 (isInline) — see issue #35 for sequencing rationale.

## Decision Accountability gate

| Claim | Verdict | Evidence |
|-------|---------|----------|
| "Hand-crafted fixtures are fine for unit tests" | FALSIFIED | At least one (`dce_stdout_sample.txt`) is provably wrong; high likelihood others are too |
| "Captured fixtures are too much work" | FALSIFIED | One-time script + small test server; pays back on every DCE bump |
| "Schema is documented elsewhere so we don't need real fixtures" | FALSIFIED | DCE has no public schema doc; the JsonMessageWriter source is the only spec |
| "Deterministic ID mapping is the right scrubbing approach" | VERIFIED | Preserves cross-file ID relationships; reproducible across re-captures |
| "Test server should be dedicated, not shared" | VERIFIED | Avoids PII risk; gives us full control over content for edge-case coverage |

**Foundational?** YES — fixtures are the contract definition for the parser layer.

## Risks

| Risk | Mitigation |
|------|------------|
| PII leak (real Discord IDs/avatars in committed fixtures) | IdMapper scrubs all IDs; CI grep guard for snowflake-shaped strings that aren't in the known fake-ID range |
| Fixture size inflation (real exports 10-100x larger than hand-crafted) | Test server is tiny by design; if files are still too big, compress with `git-lfs` (defer until measured) |
| Test brittleness (exact counts/IDs change on re-capture) | Structural assertions, not value assertions; documented in `tests/fixtures/README.md` |
| Test server availability (Peter's test server gets deleted, can't re-capture) | `tests/fixtures/README.md` documents server contents in detail so it can be re-created |
| DCE behavior changes between captures (e.g., new fields added) | This is a feature, not a risk — we WANT to detect upstream change; tests fail and we update accordingly |
| Capture script becomes stale (Ferry changes invalidate the script) | Capture script imports from `discord_ferry` namespace, so any breaking change in Ferry's exporter API forces a script update |

## Deferrals

| Item | Why not now | Tracking | Trigger |
|------|-------------|----------|---------|
| Automated weekly fixture re-capture in CI | Requires test-server credentials in CI secrets; substantial workflow work | (will file if needed) | After this issue closes |
| Property-based testing (Hypothesis) on top of captured fixtures | Higher value but bigger lift | (will file if interest emerges) | After this issue closes |
| Multiple fixtures per scenario (small/medium/large) | YAGNI; one per scenario is enough until we have a reason | — | If a specific test needs a different size |
| Git-lfs for fixture storage | Premature; measure repo size first | — | If `du -sh tests/fixtures/` exceeds ~5 MB after capture |

## Open questions (for implementation, not blocking spec)

- Salt value for `IdMapper`: hardcoded constant in the script, or read from env? Hardcoded is reproducible; env is more secure but reproducibility breaks. Pick during writing-plans.
- Whether to also capture `dce_stderr_sample.txt` (stderr transcript) for stderr-handling tests. Likely yes; cheap to add.
- Bot token vs user token: bot is more correct (selfbot ToS concern) but DCE was originally designed for user tokens. Document both options in README.
- Whether to keep the old hand-authored fixtures in git history (annotated as "replaced in commit X") for archaeology, or just `git rm` them cleanly. Lean toward clean rm.

## Cross-references

- Issue #23 — provides `parse_dce_line` (which the captured `dce_stdout_sample.txt` will be replayed through via #34's Design C)
- Issue #34 — Design C (fixture replay) consumes the new captured `dce_stdout_sample.txt`; Design A is independent
- Issue #36 — when this fixture replacement lands, the captured fixture will have real `isInline` keys, validating #36's parser fix end-to-end
