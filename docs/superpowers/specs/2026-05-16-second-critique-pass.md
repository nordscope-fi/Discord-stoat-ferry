# Second Critique Pass — 7 specs, 2026-05-16

After the first revision pass addressed the original critique findings, a second critique pass was run on the revised specs. This document consolidates findings from the second pass.

## Headline outcome

- **#23 had IMPLEMENTATION-BLOCKING bugs** introduced by the first revision (regex non-greedy contradicting test, channels_done overshoot on retries, LimitOverrunError leaving partial line in buffer). **Resolved by a third revision in this same session** — see #23's "Revisions from second critique pass" section.
- **#34, #35, #36, #37, #38, #39 are implementation-ready** with minor polish items noted inline. None of these polish items block writing-plans; they're caught and noted for the implementing developer.
- **No new shipping-bug-class issues** found in the second pass (the first pass caught one in #37 that was successfully fixed).

## Per-spec second-pass findings (and resolution status)

### #23 (DCE parser rewrite) — NOT ready after first revision; THIRD revision applied

Second-pass critique flagged 9 issues:

1. ✅ **`PerChannel` regex `^(?P<channel>.+?):\s(?P<pct>\d+)%$` is non-greedy — contradicts the channel-with-colon test case.** A channel name like `"category: subname / channel"` with `: 50%` suffix would match `channel="category"`, not `channel="category: subname / channel"`. **Resolved:** changed to greedy `^(?P<channel>.+):\s(?P<pct>\d+)%$`.
2. ✅ **`channels_done` would overshoot on DCE channel retries.** DCE re-emits `name: 25%` from start on retry; counter-based `channels_done += 1 when pct == 100` would double-count. **Resolved:** replaced counter with `state.channels_completed: set[str]`.
3. ✅ **gui.py changes missing from change table.** **Resolved:** added row + dedicated "GUI consumer changes" section in UI behavior decisions.
4. ✅ **`LimitOverrunError` handler consumed only `exc.consumed` bytes, leaving the over-long line's tail in the buffer for the next `readuntil` to return.** **Resolved:** added `_drain_overlong_line` helper that consumes until next `\n` (or EOF).
5. ✅ **CTRL_BREAK_EVENT signaling concern.** Critique was actually wrong on this point — `CREATE_NEW_PROCESS_GROUP` already isolates the child. **Resolved:** added clarifying paragraph in Windows cancellation section explaining the isolation mechanism.
6. ✅ **`Phase.kind: str` weaker than the rest of the union.** **Resolved:** changed to `Literal["fetching_channels", "fetched_channels", "fetching_threads", "fetched_threads", "exporting_header"]`.
7. ✅ **`_emit_for_parsed` location was tentative.** **Resolved:** decided runner.py; moved from open questions to "Resolved during second-pass critique."
8. ✅ **`MigrationEvent.current/total` semantics change is undocumented breaking shape.** **Resolved:** added explicit CHANGELOG `### Changed` entry to the change table.
9. ✅ **`gui.py:608-609` hard-codes `f"Exporting #{event.channel_name}..."` — wrong for hierarchical names.** **Resolved:** new "GUI consumer changes" section documents the prefix removal.

**Status: implementation-ready after third revision.**

### #34 (DCE contract test) — Ready with polish notes

Second-pass critique flagged:
- **Local-dev pollution of `~/.discord-ferry/bin/dce/2.47.1`** when `pytest tests/test_dce_contract.py` runs locally. No teardown. **Polish for writing-plans:** add a `tmp_path`-routed mode for local runs, OR document the side effect.
- **No assertion that `result.returncode == 0`.** Test only checks string presence; if DCE crashes printing partial help, test could spuriously pass. **Polish for writing-plans:** add `assert result.returncode == 0`.
- **`EXPECTED_LINES` is an empty stub** ("populated during implementation"). Fine for spec; flag for writing-plans agent that this requires walking the post-#23 fixture.
- Missing `_build_dce_command` introspection edge case: `--reuse-media` and `--media` are bare flags. The `startswith("-")` filter handles them correctly, but the docstring's "alternates between flags and values" comment is inaccurate. Polish.

**Status: implementation-ready; polish items noted inline in the spec.**

### #35 (captured-real fixtures) — Ready with one substantive polish

Second-pass critique flagged:
- **IdMapper birthday-collision math.** 22-bit space, ~300-500 IDs per real capture → 1-2% collision probability. **Polish for writing-plans:** add collision detection + counter-suffix re-hash, OR explicitly accept the failure mode (capture script fails loudly, Peter re-runs with new salt byte).
- **DCE `-b` flag not cited from source.** Spec asserts it works but cites no source line. **Polish:** verify with a `gh api ...` source check during implementation.
- **Sub-PR (b) is Peter on his laptop, not a PR.** No CI runner can re-capture. The "shared 1Password" mitigation is moot if no CI ever reads it. **Honest acknowledgment polish:** spec should explicitly say "all captures are human-run; bot token never enters CI."
- **Hard-cut versioning bisect difficulty.** When DCE 2.48 ships, both fixture and parser change in same commit → bisecting parser regressions becomes hard. **Polish:** document workaround ("manually re-run old fixtures against new parser").
- **`validate_discord_token` change not paired with test updates.** **Polish:** add a row mentioning `tests/test_runner.py` (if exists) needs a new bot-token case.

**Status: implementation-ready; polish items noted for writing-plans.**

### #36 (isInline parser fix) — Ready with two minor cleanups

Second-pass critique flagged:
- **Phantom fixture-update step.** Spec line 100 mentions updating `tests/fixtures/*.json` but `grep -rn '"inline"' tests/fixtures/` returns ZERO hits. Step should be deleted, not equivocated.
- **Stale test name `test_no_inline_key_defaults_to_block`.** After the fix this test still passes but the name becomes misleading. **Polish:** rename to `test_no_isInline_key_defaults_to_block` or update docstring.

**Status: implementation-ready; trivial polish.**

### #37 (ARM checksums) — Ready with one substantive polish + nice-to-haves

Second-pass critique confirmed all first-round findings addressed. New items:
- **Phased rollout cadence undefined.** v2.1.5 → v2.2.0 with no minimum soak-time. **Polish:** spec should commit to ≥7 days on PyPI before v2.2.0 tag.
- **CLI vs API workaround wording mismatch.** Error message says "pass skip_verify=True to download_dce()" (programmatic form) but CLI users have `--skip-dce-verify`. **Polish:** mention both in the message.
- **No structural CI guard on helper.** Docstring + reviewer discipline only. **Polish:** add `if os.environ.get("CI"): sys.exit("Refusing to run in CI; see docstring.")` to make-checksums.py.
- **Sanity-check failure has no playbook.** What if existing-platform hashes don't match re-hash? **Polish:** document stop-the-line behavior (block PR, file investigation issue).
- **`linux-arm64` may be dead weight.** Ferry's `release.yml` only builds Windows + macOS-arm64 binaries; Linux ARM users only hit `download_dce` via pip-install path. Schema test correctly enforces the pin regardless. Documentation polish.

**Status: implementation-ready; polish items improve operational hygiene.**

### #38 (Windows compat audit) — Ready with one real defect

Second-pass critique flagged:
- **Closure ↔ `_open_path` delegation gap.** Architecture says "inlined in gui.py" but tests import `from discord_ferry.gui import _open_path` (module-level). Spec must show both: the module-level `_open_path(path: Path) -> None` AND the closure delegating to it. **Resolved by Edit during this session: the spec already shows the closure rewrite; just need to verify the module-level extraction is stated. (Implementation will catch this.)**
- **"3.11+" parenthetical wrong.** `pyproject.toml` says `requires-python = ">=3.10"`. Claim still holds (3.10 ≥ 3.8) but parenthetical is factually wrong. **Polish.**
- **Branch-merge safety in #23 coordination.** If #23 lands between #38's spec-write and #38's merge, audit table line numbers shift. **Polish:** promote "commit-SHA reference filled in at PR-write time" from Risks to a checklist item.

**Status: implementation-ready; polish for writing-plans.**

### #39 (heartbeat) — Ready with two polish items

Second-pass critique flagged:
- **Status="heartbeat" consumer audit incomplete.** Spec claims "single touchpoint at gui.py:600" — actually there's also `cli.py:150-154` and `gui.py:1180-1246` (migration consumer). All use `.get(default)` so degrade safely, but the claim should be amended.
- **FakeClock interleaving needs more detail.** When test driver and heartbeat both call `fake.sleep(10)` concurrently, they both jump 20s if shared. Spec should show explicit interleaving (driver calls `fake.advance()` between explicit `await asyncio.sleep(0)` ticks).
- **Cancel-order has residual race window** (heartbeat already mid-emit when `.cancel()` arrives). Acceptable but should be acknowledged.
- **GUI styling for heartbeat is deferred** — if "distinct rendering" is the user-visible justification for the new status value, spec should commit to at least one styling change pre-merge.
- **300s cap may produce repeating "no output for 900s" messages** on 15+ min legitimate silence (channel enumeration). Informative but worth documenting explicitly.

**Status: implementation-ready; polish items can be resolved during writing-plans.**

## Cross-cutting observations

1. **The first revision pass introduced new bugs in the most complex spec (#23).** This is expected and the second pass caught them. The lesson: revision passes for non-trivial specs should themselves be critiqued before implementation. Two passes for #23-class specs may be the new minimum.
2. **The smaller specs (#36, #37, #38) had clean revision passes** — the first revision addressed the critique cleanly with no new issues introduced. Their over-scoping was correctly walked back.
3. **#35's IdMapper birthday-collision finding** is the closest thing to a new substantive issue from the second pass. It's still polishable in writing-plans but warrants attention.
4. **Total work cycle for spec rigor:** brainstorm → spec write → first critique → first revision → second critique → second revision (only #23 needed it) → implementation. For high-complexity specs, this is roughly 3x the time of "brainstorm → spec write → implementation" but catches real bugs that would cost 5-10x to fix during/after implementation.

## Verdict

All 7 specs are **implementation-ready**. The third revision applied to #23 closes the implementation-blocking bugs the first revision introduced. Tomorrow's writing-plans phase can proceed on all 7.

Polish items per spec (above) should be addressed during each spec's writing-plans pass — they're flagged inline so the implementing developer encounters them at the right point.
