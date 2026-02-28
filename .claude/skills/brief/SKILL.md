---
name: brief
description: Crystallize a vague request into a concrete requirements brief. First step in the design pipeline before brainstorming.
user_invocable: true
---

# /brief — Requirements Crystallization

Six sequential phases. Do NOT skip any phase. Output is a brief document that feeds into `superpowers:brainstorming`.

## Phase 1: Understand the Request

Read the user's request carefully. Identify:
- **What** they want (feature, fix, refactor, etc.)
- **Why** they want it (user pain, missing capability, tech debt)
- **Scope signal** — does this sound Trivial, Medium, or Large? (per CLAUDE.md tiers)

Do NOT start implementing. Do NOT open an editor. Just understand.

## Phase 2: Domain Scan

Search the codebase for related work. Check:
- **Migration phases**: Does this touch an existing phase in `migrator/`? Create a new one?
- **Parser models**: Does this need new DCE format handling in `parser/`?
- **Stoat API calls**: Does this require new API endpoints? Check `stoat-api.md` rule and consider Context7 lookup.
- **Transforms**: Does `transforms.py` need new mapping logic?
- **State management**: Does `state.py` need new fields for resume support?
- **GUI/CLI surfaces**: Which interface(s) need changes?
- **Test fixtures**: Do we need new sample DCE JSON in `tests/fixtures/`?

Report findings concisely. This scan prevents duplicate work and identifies integration points.

## Phase 3: Clarifying Q&A

Ask the user targeted questions. Standard questions to consider (ask only what's relevant):

1. What migration phase does this touch? (CONNECT through REPORT)
2. Does this need new Stoat API calls? (If yes, trigger Context7 check before Phase 4)
3. What existing patterns should we follow? (e.g., how other phases handle errors, how masquerade works)
4. What's explicitly out of scope?
5. Does this affect resume/checkpoint behavior?
6. Does this need both GUI and CLI support?

Wait for answers before proceeding.

## Phase 4: Write the Brief

Create `docs/plans/briefs/YYYY-MM-DD-<name>.md` with this structure:

```markdown
# Brief: <Name>

## Problem & Context
What problem does this solve? Why now?

## Requirements
Numbered list of concrete, testable requirements.

## Scope
### In scope
- ...

### Out of scope
- ...

## Domain Scan Results
Key findings from Phase 2 (related files, existing patterns, integration points).

## Open Questions
Anything unresolved from Phase 3 Q&A.

## Complexity Tier
Trivial / Medium / Large (with justification per CLAUDE.md thresholds)
```

## Phase 5: Classify and Gate

Apply the tier from CLAUDE.md:

| Tier | Threshold | Next step |
|------|-----------|-----------|
| Trivial | <5 lines, config, doc-only, single-file bugfix | Skip brainstorming, just implement |
| Medium | New component, 3+ files, new data wiring | Proceed to `superpowers:brainstorming` |
| Large | New feature, architecture change, 5+ files | Proceed to `superpowers:brainstorming` |

**When in doubt, classify UP, not down.**

## Phase 6: Handoff

Tell the user:
- **Trivial**: "Brief complete. This is small enough to implement directly. Shall I proceed?"
- **Medium/Large**: "Brief complete at `docs/plans/briefs/<file>`. Next step: `/brainstorm` to explore approaches, or jump to planning if the approach is obvious."

Direct them to invoke `superpowers:brainstorming` next (or skip to `superpowers:writing-plans` if the approach is already clear).

## NEVERs

- **Never** start implementing during a brief — this is requirements only
- **Never** skip the domain scan — it prevents duplicate work
- **Never** skip clarifying questions for Medium/Large — assumptions kill projects
- **Never** write the brief without Phase 2 scan results — they ground the brief in reality
- **Never** classify DOWN when unsure — a brief that's "too thorough" wastes 5 minutes; one that's too shallow wastes hours
