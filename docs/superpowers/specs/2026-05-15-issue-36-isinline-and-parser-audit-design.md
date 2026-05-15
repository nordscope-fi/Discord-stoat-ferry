# Design: isInline parser fix + DCE-vs-Ferry parser audit (issue #36)

**Date:** 2026-05-15
**Issue:** [#36](https://github.com/nordscope-fi/discord-stoat-ferry/issues/36)
**Ships as:** Two PRs — PR 1 (audit doc) → PR 2 (fixes)
**Status:** Spec — awaiting implementation (deferred to next session per user)

## Problem

`src/discord_ferry/parser/transforms.py:210` reads `field_obj.get("inline", False)`, but DCE 2.47.1 serializes embed field inline as `isInline` (verified at [`JsonMessageWriter.cs:259`](https://github.com/Tyrrrz/DiscordChatExporter/blob/2.47.1/DiscordChatExporter.Core/Exporting/JsonMessageWriter.cs#L259)). Every multi-field embed in every real export currently renders without inline grouping.

This is one **known** instance of a structural pattern: Ferry's parser was written from assumptions about DCE's JSON schema, with hand-authored fixtures matching those assumptions. The codebase audit during #23 investigation flagged dozens of other potentially-unverified field names in `transforms.py:188-285` and `dce_parser.py:308-407`. The known bug is one example; there are likely more.

Per brainstorming decision, the scope of #36 is expanded: fix `inline` AND audit BOTH `transforms.py` AND `dce_parser.py` against DCE source, fix every typo found.

## Architecture

Two-phase, two-PR structure:

### PR 1: audit infrastructure + findings doc

**`scripts/audit-dce-parser.py`** (NEW):

Python script that:
1. Reads DCE 2.47.1 source from `~/.discord-ferry-cache/dce-source/2.47.1/` (or downloads from GitHub if absent — release source-tar.gz, ~5MB).
2. Extracts every `_writer.Write{X}("key", ...)` call from DCE JsonMessageWriter.cs via regex `_writer\.Write\w+\("(\w+)"` — produces set of keys DCE writes.
3. Reads Ferry's `src/discord_ferry/parser/transforms.py` + `src/discord_ferry/parser/dce_parser.py`.
4. Extracts every `obj.get("key", ...)` and `obj["key"]` and `obj['key']` access via regex `(?:\.get\(|\[)["'](\w+)["']` — produces set of keys Ferry reads.
5. Computes set diff:
   - **Category A** — Ferry reads ∩ DCE writes — match, no action.
   - **Category B** — Ferry reads − DCE writes — TYPO or DEFENSIVE; investigate each.
   - **Category C** — DCE writes − Ferry reads — POTENTIAL gap; assess if Ferry should consume.
6. Writes findings to `docs/parser-audit-2026-05-15.md` (committed).

**Findings doc structure:**

```markdown
# DCE 2.47.1 ↔ Ferry Parser Audit (2026-05-15)

## Summary
- N keys DCE writes
- M keys Ferry reads
- A in both (Category A)
- B Ferry reads but DCE doesn't write (Category B — typo/defensive)
- C DCE writes but Ferry doesn't read (Category C — gap)

## Category A — Verified matches
| Ferry path | DCE path | OK? |

## Category B — Ferry reads, DCE doesn't write (likely typos)
| Ferry file:line | Ferry key | DCE key (closest match) | Action |
| transforms.py:210 | "inline" | "isInline" | TYPO — fix in PR 2 |
| ... | ... | ... | ... |

## Category C — DCE writes, Ferry doesn't read
| DCE key | Ferry could surface as | Action |
| ... | ... | DEFER (out of scope) |
```

### PR 2: apply fixes

For each Category B finding categorized as TYPO in PR 1's audit doc:
- Apply the corrected key.
- Add unit test asserting the parser handles the fixture correctly.
- Reference the audit doc row in the commit message.

For each Category B finding categorized as DEFENSIVE (e.g., backward-compat with older DCE versions):
- Add inline comment explaining why the key isn't in current DCE.
- Leave code as-is.

For each Category C finding:
- Out of scope for #36. File a separate issue if Ferry should start consuming the field.

## Components

| Component | Responsibility |
|-----------|----------------|
| `scripts/audit-dce-parser.py` (NEW) | One-shot audit; produces findings doc |
| `docs/parser-audit-2026-05-15.md` (NEW, committed in PR 1) | Audit findings, categorized |
| `tests/test_transforms.py`, `tests/test_dce_parser.py` (UPDATED in PR 2) | New test per fixed typo |
| `src/discord_ferry/parser/transforms.py` (FIXED in PR 2) | All Category B typos resolved |
| `src/discord_ferry/parser/dce_parser.py` (FIXED in PR 2) | All Category B typos resolved |

## Data flow

```
DCE 2.47.1 source files
    ↓ (audit script reads)
regex extraction → set of keys DCE writes
                                     ↓
                              set diff → audit doc
                                     ↑
regex extraction → set of keys Ferry reads
    ↑
src/discord_ferry/parser/{transforms,dce_parser}.py
```

## Error handling

- Audit script fails fast if DCE source can't be downloaded (clear error message; suggest manual download).
- Audit script handles malformed regex matches by skipping with a warning (don't crash; log for review).
- Audit script doesn't enforce a "no findings" exit code — its job is to surface findings, not to fail builds.

PR 2's fixes are individual code changes, error handling per fix is per-key.

## Testing

### PR 1: audit script

- `tests/test_audit_dce_parser.py` (NEW): unit tests for the regex extraction functions on synthetic DCE/Ferry source snippets.
- The audit doc itself is the manual artifact; its correctness is reviewed by humans, not asserted by tests.

### PR 2: fixes

For each typo fixed:
- New test in the appropriate `tests/test_*.py` file.
- Assertion uses the existing fixture (or the new captured fixture if #35 has already landed).
- Asserts on the SHAPE of the parsed result (e.g., `assert any(f.inline for f in fields)` for the isInline fix).

Example for `isInline`:

```python
def test_embed_field_inline_grouping_parsed_correctly(fixtures_dir):
    raw = json.loads((fixtures_dir / "simple_channel.json").read_text())
    msg_with_embed = next(m for m in raw["messages"] if m.get("embeds"))
    embed = transform_embed(msg_with_embed["embeds"][0])
    inline_fields = [f for f in embed.fields if f.inline]
    non_inline_fields = [f for f in embed.fields if not f.inline]
    assert inline_fields, "expected at least one inline field; got none (parser may be reading wrong key)"
    assert non_inline_fields, "expected at least one non-inline field; got none"
```

## Phasing

### PR 1: audit (lands first)

| Change | Why |
|--------|-----|
| `scripts/audit-dce-parser.py` (NEW) | Reusable for future DCE bumps |
| `docs/parser-audit-2026-05-15.md` (NEW) | Findings, dated |
| `tests/test_audit_dce_parser.py` (NEW) | Tests for the script's extraction logic |
| Audit doc committed; no parser fixes in this PR | Separation of concerns: review the audit BEFORE applying fixes |

### PR 2: fixes (lands after PR 1 review)

| Change | Why |
|--------|-----|
| `src/discord_ferry/parser/transforms.py` (FIXED) | All Category B typos in this file |
| `src/discord_ferry/parser/dce_parser.py` (FIXED) | All Category B typos in this file |
| `tests/test_transforms.py` (UPDATED) | One new test per typo fixed in transforms.py |
| `tests/test_dce_parser.py` (UPDATED) | One new test per typo fixed in dce_parser.py |
| CHANGELOG `### Fixed` entry per fix (or one summary entry if many) | Standard |

### Sequencing relative to other issues

- **Lands AFTER #23 (v2.1.4)** — so the parser changes don't conflict with the new `dce_output.py` module.
- **Lands BEFORE #35** — so when #35 captures real DCE output, all known typos are already fixed and the captured fixtures validate the fixes end-to-end.

## Decision Accountability gate

| Claim | Verdict | Evidence |
|-------|---------|----------|
| "Just fix isInline" (one-line fix) | FALSIFIED via brainstorming | The bug is one instance of a pattern; fixing only the known instance leaves the pattern intact |
| "Script-assisted audit catches more than manual reading" | VERIFIED | Set-diff math is exhaustive in a way human reading isn't |
| "Two PRs (audit doc → fixes) is right separation" | VERIFIED | Audit findings deserve their own review pass before fixes are committed; bisectable; clean commit history |
| "Category C (DCE keys Ferry doesn't read) is out of scope" | VERIFIED | Adding richer parsing is feature work, not bugfix; file separate issues if specific gaps matter |

**Foundational?** YES — the parser is the contract with DCE; the audit produces the documentation of that contract that should have existed since v1.

## Risks

| Risk | Mitigation |
|------|------------|
| Audit script regex misses a DCE write call (edge case syntax) | Manual review of audit output; sanity-check by counting findings vs human spot-check |
| Audit script flags false positives (defensive `.get` with sensible default; dynamic key) | Manual review classifies each finding into TYPO/DEFENSIVE/dynamic before PR 2 |
| Fix introduces regression in another part of the parser | Each fix has a unit test; full pytest suite runs |
| PR 2 grows too large to review | If audit finds >10 fixes, split PR 2 by file (transforms.py fixes vs dce_parser.py fixes) — decide during PR 2 |
| DCE source isn't downloadable at script run time | Cache downloaded source in `~/.discord-ferry-cache/dce-source/2.47.1/`; fail with clear instructions if download blocked |

## Deferrals

| Item | Why not now | Tracking | Trigger |
|------|-------------|----------|---------|
| Category C consumption (Ferry starts reading more DCE fields) | Feature work, not bugfix | (will file per-field if interest emerges) | When a specific Category C field is judged valuable |
| Audit run in CI on every DCE bump | Coupled to #34's contract test maturity; substantial CI work | (will file later) | After #34 lands and proves itself |
| Audit script extended to nested key paths (e.g. `author.name`) | First pass uses flat key names; manual review catches false positives from this | (will file if false-positive volume justifies) | If false-positive count exceeds ~30% of findings |

## Open questions (for implementation, not blocking spec)

- Whether to commit the cached DCE source (~5MB) to the repo or always re-download. Lean toward re-download with a clear failure path.
- Audit script: include `parser/__init__.py`? It re-exports — likely no Ferry-side keys there, but check during implementation.
- Whether to also extract DCE 2.46.1 source and audit against THAT (in case Ferry was written against an older DCE). Defer — current `DCE_VERSION` is the source of truth; older versions are archaeological.
- Whether `docs/parser-audit-2026-05-15.md` should be ephemeral (deleted after PR 2) or permanent (kept as historical record). Lean toward permanent + regenerated alongside future DCE bumps.

## Cross-references

- Issue #23 — provides `parse_dce_line` and the new `dce_output.py` module (orthogonal); doesn't conflict
- Issue #34 — Design A (DCE `--help` test) is unrelated; could conceptually grow to also assert audit-doc parity, but out of scope
- Issue #35 — captured-real fixtures will validate every fix made here end-to-end
