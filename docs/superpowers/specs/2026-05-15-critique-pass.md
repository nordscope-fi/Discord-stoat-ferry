# External Critique Pass — 7 specs, 2026-05-15

External code-reviewer agents critiqued each spec against the actual codebase + DCE source. Findings ranked by severity.

## Headline findings

- **#37 has a SHIPPING-BUG-CLASS issue:** Spec says "hash the binary at `/tmp/dce-test/DiscordChatExporter.Cli`" (extracted binary) — but `manager.py:51-79` hashes `zip_data` (raw zip bytes). Implementing as written would generate a hash that never matches at runtime → **bricks Apple Silicon on first run after merge**. Must be corrected in the spec before implementation.
- **#36 and #38 are likely over-engineered.** Brainstorming expanded scope based on a hypothesis that "more bugs of this pattern likely exist." Spot-checks during critique:
  - **#36:** Spot-check of all `obj.get("...")` keys in `transforms.py` and `dce_parser.py` shows `isInline` is the only obvious typo. Likely Category B count = 1-3, not 50. Two-PR + audit-script structure is over-engineered for n≤3.
  - **#38:** Audit grep results: 3 subprocess sites total (2 owned by #23, 1 is the bug); 0 `os.fork`/`shell=True`/missing-encoding; `chmod` already gated by `platform.system() != "Windows"`. **Audit finds zero new fixes.** PR scope is one function rewrite + 2 tests.
- **#23, #34, #35, #39 each have real design gaps** that are fixable but require spec revisions before writing-plans.

## Per-spec critical findings

### #23 (DCE parser rewrite)
1. **Phasing rationale collapses under inspection.** v2.1.4 vs v2.2.0 split as semver is weak — both ship the same code with different routing toggles; user-visible behavior changes in BOTH. Either ship as single v2.2.0 or admit the split is for risk-isolation, not semver.
2. **Per-channel UI bar reset is wrong UX.** A 229-channel guild = 229 bar reset cycles. Overall progress (`channels_done / total`) is one extra integer; the "complexity" defense is false. Brainstorming user-choice may have been wrong.
3. **Cancel path on Windows unanalyzed.** `process.terminate()` on Windows = `TerminateProcess` (immediate, no flush, partial JSON files possible). Spec adds `try/finally` for stderr but doesn't address the asymmetric cancel semantics.
4. **`ParsedDceLine` shape conflates a tagged union into one optional-everything dataclass.** Per-kind dataclasses + `match` would be cleaner.
5. **Captured trace `/tmp/dce-stdout-piped.log` already exists** — why is it being replaced with hand-derived instead of being committed as the fixture?
6. **64KB StreamReader limit unaddressed.** `LimitOverrunError` would bypass cancel check and crash the GUI.

### #34 (DCE contract test)
1. **Cache + monkeypatch contradiction is genuinely unresolved.** As written, the cache is dead code — every CI run re-downloads ~30MB hitting unauthenticated GitHub rate limit (60/hr per IP).
2. **`REQUIRED_FLAGS` will silently drift** from `_build_dce_command`. Need to import + render the actual command, not duplicate the list.
3. **`setup-dotnet@v4` is unnecessary.** `actions/runner-images` ubuntu-24.04 ships .NET 8 SDK preinstalled, documented. Dead 5-10s per CI run.
4. **Skipif version is dead weight.** Local devs without .NET get no signal; with .NET they hit network on every test run. Drop skipif; keep dedicated job.
5. **`EXPECTED_RAW_LINES` allowlist is the brittleness it claims to prevent.** "Discover via failures" = same judgment-call pattern that caused #23.

### #35 (captured-real fixtures)
1. **Test server recipe misses ~half the parser branches.** Audit of `dce_parser.py` and `transforms.py` shows zero coverage for: system messages (GuildMemberJoin, ChannelPinnedMessage, RecipientAdd, UserPremiumGuildSubscription), polls, stickers, in-content custom emoji, mentions, jump/invite link rewriting, spoilers/underline/code spans, reply-to-deleted-message, expired CDN URLs.
2. **Bot vs user token unresolved + ToS issue.** `validate_discord_token` currently hard-codes user-token shape (no `Bot ` prefix). Capturing with a user token = Discord ToS violation (selfbotting).
3. **Capture script reusing `run_dce_export()` will need significant glue** that the spec hand-waves.
4. **`IdMapper` will produce structurally invalid snowflakes** (HMAC truncation = garbage timestamps, broken sort order). Spec asserts "fake-but-realistic" without defining "realistic."
5. **CDN scrub is incomplete.** Real exports have ~6 hosts beyond `cdn.discordapp.com` (`media.discordapp.net`, `tenor.com`, `images-ext-1.discordapp.net`, etc.) plus URL-embedded IDs.
6. **Test rewrite scope undercounted.** ~223 asserts across `test_parser.py`/`test_transforms.py` depend on hand-crafted IDs. Closer to 300 lines of churn than 50.
7. **Bus factor on credentials.** `.env` only on Peter's machine; if machine dies, recovery requires re-creating server + bot account + salt.

### #36 (isInline + parser audit)
1. **Audit script regex will produce false positives** — matches `.get("foo", "default-value")` defaults and URL query param keys (e.g. `qs.get("ex")` in `dce_parser.py:139` would be flagged as a Ferry-reads key with no DCE producer).
2. **DCE source has Newtonsoft `[JsonProperty]` attributes** the script's `_writer.WriteX(...)` regex won't capture.
3. **Categorization step has no decision criteria.** PR 1 ships findings; how do reviewers categorize TYPO vs DEFENSIVE without a decision tree?
4. **Spot-check shows likely n=1-3 fixes**, not 50. Two-PR + script architecture is over-engineered.
5. **Sequencing claim "after #23" is wrong** — #23 doesn't touch `transforms.py` or `dce_parser.py`. No conflict.

### #37 (ARM checksums)
1. **Hash-target ambiguity is wrong, not unclear.** Spec instructs hashing extracted binary; manager.py hashes raw zip bytes. **Would brick Apple Silicon.**
2. **`DCENotFoundError` vs `RuntimeError` inconsistency.** Existing taxonomy uses `DCENotFoundError`.
3. **No user-facing escape hatch.** `download_dce(skip_verify=True)` already exists; fail-loud message tells users to "file an issue" without mentioning the workaround.
4. **`make-checksums.py` provenance risk.** If wired to CI, would silently endorse compromised releases. Must be human-run-only.
5. **No schema-validation test for `dce_checksums.json`** itself.
6. **Atomicity claim overstated.** Pin in v2.1.5 (additive, no behavior change) → fail-loud in v2.2.0 (release-note migration) is genuinely safer.

### #38 (Windows compat audit)
1. **Audit found nothing actionable.** 0 `os.fork`, 0 `shell=True`, 0 missing-encoding, 1 `chmod` already gated. The "audit framing" is unfounded; spec should be re-scoped to "fix xdg-open + commit-message audit summary."
2. **`os.startfile` test assertion is fragile.** Mocks `sys.platform` but not `Path` semantics; `Path("/test/path")` round-trips differently on Windows.
3. **Coordination with #23 is a merge-conflict trap.** After #23 ships, #38's subprocess audit collapses to no-op. Spec should explicitly state the protocol.
4. **Manual smoke test owner unspecified.** No named maintainer with Windows machine = aspiration, not verification.

### #39 (heartbeat)
1. **Mock-clock plan is broken.** Mocking `asyncio.sleep` globally breaks `_read_stderr`'s `async for`, the stdout `async for`, `process.wait()`, `aiohttp.ClientSession`. Test strategy needs narrower-scope mocking (inject `_sleep` callable into `_heartbeat`).
2. **Backoff prose ≠ code.** Prose claims "interval doesn't reset just because last_activity advances." Code resets `interval=60` on EVERY tick where `last_activity > last_heartbeat_fire_at`. Functionally OK but the prose is inaccurate.
3. **Race in `process.returncode is None` check.** Heartbeat tick after stdout exits but before `await process.wait()` could fire false "Still working" event.
4. **v2.2.0 dependency not enforced.** Implementation sketch references `parse_dce_line` (won't compile against current runner.py). Spec should state: "Blocked by #23; PR fails CI if merged first."
5. **Status type collision.** Heartbeat uses `status="progress"` — indistinguishable from real progress in log panel. Open question #3 should be promoted to decided.
6. **Backoff cap (1200s = 20 min) untested assumption.** Realistic user tolerance is ~5 min before they kill it.

## Cross-cutting findings

- **Two specs (#36, #38) had scope expanded by brainstorming based on speculation that turned out to be wrong.** Critical lesson: when brainstorming says "the bug is one instance of a pattern," verify with a 5-minute spot-check before committing to the broader scope.
- **Three specs (#34, #35, #39) have testability gaps** that would create implementation pain. The mock-clock pattern (#39), the cache/monkeypatch contradiction (#34), and the test-rewrite scope undercount (#35) all require spec revision before writing-plans.
- **One spec (#37) has a SHIPPING-BUG-CLASS instruction error** (hash target mismatch) that critique caught. Without this critique pass, this would have shipped and bricked Apple Silicon.

## Recommended actions before tomorrow's writing-plans

| Issue | Action |
|-------|--------|
| #23 | Reconsider per-channel UI choice (overall progress is trivially achievable); reconsider v2.1.4/v2.2.0 split (single v2.2.0 may be honest); commit `/tmp/dce-stdout-piped.log` as fixture (replace hand-derived) |
| #34 | Resolve cache/monkeypatch contradiction (drop monkeypatch); drop `setup-dotnet@v4`; replace `REQUIRED_FLAGS` duplication with introspection of `_build_dce_command`; drop skipif-gated test |
| #35 | Re-scope test server recipe to cover all parser branches (audit needed); decide bot vs user token; show capture-script glue concretely; redesign `IdMapper` for snowflake validity; expand CDN host scrub list |
| #36 | Validate audit-script regexes against real code (false-positive examples found); spot-check actual finding count (likely 1-3); collapse to single PR if n≤3 |
| #37 | **CRITICAL:** Fix hash-target instruction (zip bytes, not extracted binary); use `DCENotFoundError`; document `skip_verify` workaround; consider phased rollout (pin first, fail-loud later) |
| #38 | Re-scope to "fix xdg-open + commit-message audit summary" (audit is empty); fix test assertion to use `Path` not str; document #23 coordination protocol |
| #39 | Redesign mock-clock approach (inject `_sleep` into `_heartbeat`); fix backoff prose to match code; add race-condition mitigation for `process.returncode` check; reduce 20-min cap to ~5 min |

## What's strong across the board

- Decision-accountability gate sections genuinely attempted falsification rather than affirmation.
- Empirical confirmation in #23 (captured DCE traces with file paths) is exactly the right artifact.
- Cross-references between specs are present and mostly correct.
- Deferrals are well-bounded and named with triggers.

## Bottom line

**1 of 7 specs has a critical bug; 2 of 7 are over-scoped; 4 of 7 have real but fixable gaps.** Critique caught the shipping bug — that alone justifies the critique pass. Tomorrow's writing-plans must update each spec to address its critical findings before implementation begins.
