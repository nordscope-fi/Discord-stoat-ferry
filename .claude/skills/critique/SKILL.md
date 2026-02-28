---
name: critique
description: Review a design document across 7 dimensions before implementation. Use after brainstorming, before writing a plan.
user_invocable: true
---

# /critique — 7-Dimension Design Review

Reviews a design document or plan in `docs/plans/` and produces a structured critique. Use after brainstorming, before implementation.

## Input

The user provides a path to a design document (e.g., `docs/plans/my-feature.md`). Read it fully before proceeding.

If no path is given, check `docs/plans/` for the most recently modified file and confirm with the user.

## The 7 Dimensions

Review the design document against each dimension. Score each: **PASS**, **CONCERN** (minor issue, non-blocking), or **BLOCK** (must fix before implementing).

### 1. Feasibility

Can this actually be built within Stoat's constraints?

Check against:
- **Rate limits**: `/servers` bucket = 5/10s (shared for channels, roles, emoji), messages = 10/10s. Will the design exceed these?
- **Autumn upload limits**: attachments 20MB, avatars 4MB, icons 2.5MB, banners 6MB, emojis 500KB
- **Server limits**: 200 channels, 200 roles, 100 emoji per server
- **Message limits**: 2,000 chars, 5 attachments, 5 embeds, 20 reactions per message
- **No ADMINISTRATOR permission**: Does the design assume a blanket admin permission?
- **Two-step categories**: Does the design correctly account for create-then-patch?

### 2. Pattern Alignment

Does the design follow established project conventions?

Check against:
- `python-conventions.md`: async-first, dataclasses, pathlib, type hints, ruff/mypy
- `stoat-api.md`: British spelling, nonce deduplication, masquerade rules
- **Engine/shell separation**: Does engine.py remain independent of GUI/CLI? Event emitter pattern?
- **Existing phase patterns**: Does a new phase follow the same structure as existing ones in `migrator/`?
- **Error handling**: Custom exceptions in `errors.py`, errors logged to `MigrationState.errors`

### 3. Over-Engineering

Is the design doing more than necessary?

Watch for:
- Abstractions for single-use cases
- Configurability nobody asked for
- "Future-proofing" for hypothetical requirements
- Complex retry/fallback logic where simple would suffice
- New dependencies when stdlib/existing deps cover it

**YAGNI is the default.** The burden of proof is on complexity, not simplicity.

### 4. Missing Edge Cases

What could go wrong that the design doesn't address?

Common Discord Ferry edge cases:
- **Empty exports**: No messages, no channels, no members in DCE JSON
- **Malformed DCE JSON**: Missing fields, unexpected types, DCE version differences
- **Network failures mid-migration**: Connection drops during message upload, Autumn timeout
- **Resume-after-crash**: Can the migration resume cleanly from `MigrationState`? Are nonces correct?
- **Forwarded messages**: Empty content + empty attachments (DCE bug #1322)
- **System messages**: Empty `content` but non-empty `type`
- **Exceeding server limits**: What happens at channel 201 or emoji 101?
- **Rate limit exhaustion**: Sustained 429s, not just occasional ones

### 5. Type Safety

Will this pass mypy strict?

Check for:
- Dataclass field types — are they precise or overly broad (`Any`, `dict`)?
- Union types using `X | Y` syntax (not `Optional[X]` or `Union[X, Y]`)
- Return types on all public functions
- `pathlib.Path` for file paths (not `str`)
- Generic types using lowercase (`dict[str, str]` not `Dict[str, str]`)

### 6. Scope Creep

Does the design stay within what was requested?

- Compare against the brief (if one exists in `docs/plans/briefs/`)
- Flag any requirements that appeared in the design but weren't in the brief
- Flag any "while we're at it" additions
- Is there a simpler version that solves the core problem?

### 7. Better Alternatives

Is there a fundamentally better approach?

- Could an existing pattern be extended instead of creating something new?
- Is there a library that already does this?
- Could the problem be solved at a different layer (parser vs transform vs migrator)?
- Would a different data model make the code simpler?

## Output Format

```markdown
# Critique: <Design Document Name>

## Verdict: PASS / ITERATE / RETHINK

### Summary
<2-3 sentence overall assessment>

### Dimension Scores

| # | Dimension | Score | Notes |
|---|-----------|-------|-------|
| 1 | Feasibility | PASS/CONCERN/BLOCK | ... |
| 2 | Pattern Alignment | PASS/CONCERN/BLOCK | ... |
| 3 | Over-Engineering | PASS/CONCERN/BLOCK | ... |
| 4 | Missing Edge Cases | PASS/CONCERN/BLOCK | ... |
| 5 | Type Safety | PASS/CONCERN/BLOCK | ... |
| 6 | Scope Creep | PASS/CONCERN/BLOCK | ... |
| 7 | Better Alternatives | PASS/CONCERN/BLOCK | ... |

### Detailed Findings
<Per-dimension details for any CONCERN or BLOCK>

### Recommended Changes
<Numbered list of specific changes before implementation>
```

## Verdict Rules

- **PASS**: 0 BLOCKs, <=2 CONCERNs. Proceed to implementation planning.
- **ITERATE**: 0 BLOCKs, 3+ CONCERNs, or 1 BLOCK that's easy to fix. Revise the design and re-critique.
- **RETHINK**: 2+ BLOCKs, or 1 BLOCK that requires fundamental redesign. Go back to brainstorming.

## NEVERs

- **Never** pass a design with unaddressed BLOCKs
- **Never** critique without reading the full design document first
- **Never** rubber-stamp — if everything is genuinely PASS, say so, but earn it with specific observations
- **Never** propose implementation details — this is design review, not implementation planning
- **Never** expand scope — critique what's written, don't add requirements
