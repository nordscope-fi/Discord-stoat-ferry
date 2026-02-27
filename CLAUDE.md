# Discord Ferry — Claude Code Project Guide

> **Single source of truth for implementation:** `docs/discord-ferry-claude-code-brief.md`
> Read the brief before writing any code. Every section contains implementation-critical details.

## Project Identity

Python 3.10+ migration tool that moves a Discord server (exported via DiscordChatExporter) to a Stoat (formerly Revolt) instance — either the official hosted service or a self-hosted deployment. Primary interface is a local web GUI (NiceGUI). Secondary interface is a CLI (Click). Both are thin wrappers around a shared migration engine.

## Stack

| Layer | Tool | Notes |
|-------|------|-------|
| Stoat API client | aiohttp (raw HTTP) | Custom API layer in `migrator/api.py` with retry + rate limit handling |
| GUI | NiceGUI | Local web UI, FastAPI + Vue.js under the hood |
| CLI | Click + Rich | Rich for progress bars and formatted output |
| Config | python-dotenv | `.env` support |
| Package manager | **uv** | Primary. All commands use `uv run` prefix |
| Linting | ruff | Format + lint in one tool |
| Types | mypy (strict) | All public functions typed |
| Tests | pytest + pytest-asyncio | Fixtures in `tests/fixtures/` |
| Docs | MkDocs Material | Deploys to GitHub Pages |
| Packaging | PyInstaller | Single-binary for Windows/Mac |

## Verification Command

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src/ && uv run pytest
```

Run this before every commit. The `/ship` skill enforces this automatically.

## Architecture — One Engine, Two Shells

```
gui.py (NiceGUI) ──┐
                    ├──> core/engine.py + core/events.py
cli.py (Click)   ──┘
```

**engine.py NEVER imports from gui or cli.** All progress reporting uses the event emitter pattern in `core/events.py`. The engine accepts a callback function; the GUI subscribes to update its UI, the CLI subscribes to print Rich output.

## Stoat API — Critical Rules

- **British spelling**: `colour` not `color` in ALL Stoat API code (masquerade, embeds, roles)
- **No ADMINISTRATOR permission**: Grant individual permissions explicitly (see brief §5.11)
- **Two-step categories**: Create channel first, then PATCH server's categories array
- **Rate buckets**: `/servers` = 5/10s (shared for channels, roles, emoji), messages = 10/10s
- **Always use `nonce`**: `f"ferry-{discord_msg_id}"` for message deduplication on resume
- **Masquerade colour requires ManageRole** (bit 3) — not just Masquerade (bit 28)
- See `.claude/rules/stoat-api.md` for full reference

## Spec Before Code

| Tier | Threshold | Action |
|------|-----------|--------|
| Trivial | <5 lines, config, doc-only, single-file bugfix | Just do it |
| Medium | New component, 3+ files, new data wiring | Mini-PRD in `docs/plans/` -> approval -> implement |
| Large | New feature, architecture change, 5+ files | Brainstorming -> full design doc -> approval -> plan -> implement |

**When in doubt, classify UP, not down.**

Every medium+ spec must include: Problem & Context, Scope (in/out), Technical Approach with at least one alternative (2+ for large), Files to touch, Acceptance Criteria, Tasks. Do NOT start implementation until the user explicitly approves the spec.

## Cognitive Tiering

| Phase | Model | Rationale |
|-------|-------|-----------|
| Main session (planning, design, coordination) | Opus (default) | Strategy needs strongest reasoning |
| Build/implementation subagents | Sonnet (`model: "sonnet"`) | Clear specs don't need Opus |
| Code review subagents | Opus (default) | Review quality matters |
| Design critique | Opus (default) | Critique quality is the whole point |
| Exploration subagents | Haiku or Sonnet | Fast codebase search |

When dispatching implementation subagents, always set `model: "sonnet"` unless the task involves design decisions or architectural choices.

## Subagent Discipline

**NEVER** use Task tool to:
- Search for files or patterns (use Glob/Grep directly)
- Read 1-3 known files (use Read directly)
- Run a single command (use Bash directly)
- Answer something already in MEMORY.md or CLAUDE.md

**ONLY** spawn a subagent when:
- Exploring >5 files across unrelated directories
- Doing genuinely parallel independent work
- The task would take >10 sequential tool calls inline

Maximum 3 subagents at a time. Prefer 1.

## Session Handoff

When context window fills or work session ends, write handoff to `claude_log/session-handoff-YYYY-MM-DD.md`:

```markdown
## Session -- HH:MM (morning/afternoon/evening)
### Done -- what was completed (with commit hashes)
### Pending -- what's left to do
### Decisions -- key choices made and why
### Gotchas -- anything the next session needs to know
```

Use Edit tool to append (Write would overwrite previous sessions in the same day).

## Memory Tags

When updating MEMORY.md:
- `[LESSON]` -- mistakes and debugging insights, durable, rarely changes
- `[DECISION]` -- architectural choices with rationale, update when reversed
- `[STATE]` -- current facts (test counts, file structures), refresh every ~10 versions

Prune `[STATE]` entries that are >10 versions stale.

## Workflow Discipline

- **Do NOT stop mid-refactor for verification.** When making a batch of related edits across multiple files (e.g., adding an import to 6 files), complete the entire batch first, then run verification once. Hooks and PostToolUse prompts must not interrupt multi-file refactors.
- Run the verification command (`uv run ruff check . && uv run ruff format --check . && uv run mypy src/ && uv run pytest`) at natural checkpoints: after completing a task, before committing, or when switching to a different area of work.

## Drift Check

After completing each task in a plan:
1. Files created/modified match what the plan specified
2. Acceptance criteria from the plan are met
3. Implementation does not exceed what the design document specified (no scope creep)

If drift is found, fix it before proceeding.

## Code Style

- **ruff** for linting and formatting (line-length 100, target Python 3.10)
- **mypy strict** for type checking
- **Google-style docstrings** on public functions only (not every function)
- **Type hints** on all public function signatures
- **`pathlib.Path`** not `os.path`
- **`dataclasses`** for data models (as specified in brief)
- **Custom exceptions** in `errors.py`, never bare `except:`
- **Async-first**: all API/IO code uses async/await
- **Python 3.10+ features**: match/case, `X | Y` union types

## Edge-Case Tools

### `python-pro` persona
Use `Task(octo:personas:python-pro)` for Python-specific architecture decisions: async patterns, dataclass design, type system questions, packaging decisions. Do NOT use for routine implementation.

### `octo:tdd` skill
Invoke `/octo:tdd` when implementing any module with a corresponding test file. Red-green-refactor: write failing test first, then implement, then refactor. Critical for `parser/`, `transforms.py`, and `state.py`.

### `superpowers` skills
- `superpowers:brainstorming` -- before any new feature or design decision
- `superpowers:test-driven-development` -- before writing implementation code
- `superpowers:systematic-debugging` -- before proposing fixes for any bug
- `superpowers:writing-plans` -- after brainstorming, to create implementation plans
- `superpowers:requesting-code-review` -- in `/ship` Step 3
- `superpowers:verification-before-completion` -- before claiming any work is done
- `superpowers:dispatching-parallel-agents` -- for independent implementation tasks
- `superpowers:session-handoff` -- when context window fills or work session ends

## Workflow Pipeline

```
/brief -> /brainstorm -> /critique -> plan -> implement -> /ship
```

The `/ship` skill (`.claude/skills/ship/SKILL.md`) is the ONLY way to commit. It runs verification, audit, code review, second opinion, documentation, and commit in sequence.

## Key Directories

| Path | Purpose |
|------|---------|
| `src/discord_ferry/` | All source code |
| `src/discord_ferry/core/` | Engine + events (shared by CLI and GUI) |
| `src/discord_ferry/parser/` | DCE JSON parsing and data models |
| `src/discord_ferry/uploader/` | Autumn file uploads |
| `src/discord_ferry/migrator/` | Migration phases (structure, messages, emoji, reactions, pins) |
| `tests/` | pytest tests |
| `tests/fixtures/` | Sample DCE JSON for testing |
| `docs/` | MkDocs Material documentation site |
| `docs/plans/` | Design documents and plans |
| `.claude/rules/` | Domain-specific rules (auto-loaded by glob) |
| `.claude/skills/ship/` | Custom shipping gate skill |
| `claude_log/` | Session handoffs (gitignored) |
