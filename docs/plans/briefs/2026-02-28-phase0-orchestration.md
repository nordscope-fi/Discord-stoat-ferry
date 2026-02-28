# Brief: Phase 0 — DCE Orchestration & Streaming Parser

## Problem & Context

Discord Ferry currently requires users to manually download, configure, and run DiscordChatExporter (DCE) via command line before pointing Ferry at the resulting JSON exports. This two-tool manual workflow is hostile to the target audience (non-technical Discord server admins). Additionally, the parser loads all JSON into memory at once, which will fail on large servers (100k+ messages) that the orchestration layer makes more accessible.

**Origin**: External codebase critique and orchestration design document (`Discord Ferry Orchestration Design.md`).

**Goal**: Transform Ferry from a "DCE-to-Stoat Importer" into a true "1-click Discord-to-Stoat Migrator" — user provides tokens, Ferry handles everything else.

## Requirements

1. **DCE Binary Management**: Auto-download the correct DCE release binary for the host OS (Windows/macOS/Linux) and architecture (x64/arm64). Store in `~/.discord-ferry/bin/dce/`. Verify existing binary version before re-downloading.
2. **.NET Runtime Detection**: Detect .NET 8 runtime on macOS/Linux (Windows uses self-contained DCE build). Prompt user with install instructions if missing.
3. **Subprocess Execution**: Run DCE as an async subprocess (`asyncio.create_subprocess_exec`). Parse stdout line-by-line to emit `MigrationEvent` progress updates.
4. **Discord Token Validation**: Before launching DCE, validate the Discord token via `GET https://discord.com/api/v10/users/@me`. Fail fast on 401.
5. **Token Security**: Discord token is never persisted to disk. Not saved in `FerryConfig` state serialization. Wiped on crash/close. On resume, if cached JSONs exist, offer choice to skip export or re-enter token.
6. **Smart Resume**: On resume, detect existing DCE JSON files in the export cache directory. Present user with: (a) proceed with cached exports, or (b) re-export (requires Discord token). Show summary of what was exported (channel count, file count, total size).
7. **Config Expansion**: Add `discord_token: str | None`, `discord_server_id: str | None` to `FerryConfig`. Make `export_dir` default to `./ferry-output/dce_cache/{server_id}` when orchestrating (still overridable for offline/advanced use).
8. **Engine Phase 0**: Insert `"export"` as the first entry in `PHASE_ORDER`. Runs DCE subprocess and produces the JSON export directory. Skipped when `export_dir` is user-provided (offline mode).
9. **GUI Overhaul**: New credential screen with Discord section (token, server ID, "how to find these?" modal), Stoat section, and mode toggle (orchestrated vs. offline/advanced). New Export progress screen between Setup and Validate.
10. **CLI Update**: Replace positional `EXPORT_DIR` with optional `--export-dir` override. Add `--discord-token` and `--discord-server` options. Default to orchestrated mode when Discord credentials provided.
11. **Streaming Parser**: Replace `json.loads()` with `ijson` for the messages phase. Validate phase uses lightweight scanning (metadata only, no message content in RAM). Memory footprint stays under ~100MB regardless of export size.
12. **ToS Disclaimer**: Display Discord ToS warning in both GUI and CLI before using Discord token. User must acknowledge.
13. **Documentation Update**: Rewrite MkDocs user guide to reflect the new "1-click" flow. Keep advanced/offline instructions as a secondary path.

## Scope

### In scope
- New `src/discord_ferry/exporter/` module (manager.py, runner.py, __init__.py)
- FerryConfig and engine.py changes for Phase 0
- GUI credential screen redesign + export progress screen
- CLI argument restructuring
- `ijson` streaming parser for messages phase
- Lightweight validate scan (no full message loading)
- .NET runtime detection and user prompting
- Discord token validation endpoint
- MkDocs documentation rewrite
- Tests for all new code

### Out of scope
- Bot token support for DCE (user tokens only, as DCE requires)
- Bundling DCE binary inside PyInstaller builds (download on demand)
- .NET auto-installation (detect + instruct only)
- Discord API interactions beyond token validation (all Discord work goes through DCE)
- Changing existing migration phases 2-11
- Forum/thread export mode selection (DCE exports all by default)

## Domain Scan Results

| Area | Impact | Key files |
|------|--------|-----------|
| **Engine** | Add "export" to `PHASE_ORDER`, new phase function, skip logic for offline mode | `src/discord_ferry/core/engine.py` (lines 31-43, 66-144) |
| **Config** | Add 2 new fields, change `export_dir` default logic | `src/discord_ferry/config.py` |
| **State** | Add `export_completed: bool` field for resume, possibly `export_file_count: int` | `src/discord_ferry/state.py` |
| **Events** | New event status values for export progress (DCE output parsing) | `src/discord_ferry/core/events.py` — existing `MigrationEvent` sufficient, use `phase="export"` |
| **Parser** | Replace `json.loads()` with `ijson`. Split into metadata scan vs full parse. | `src/discord_ferry/parser/dce_parser.py` (lines 40-46, 73) |
| **GUI** | Redesign setup screen, add export progress screen, add ToS modal | `src/discord_ferry/gui.py` (lines 188-400, 516-797) |
| **CLI** | Restructure arguments, add Discord options, update progress tracker | `src/discord_ferry/cli.py` (lines 1-50 command signatures) |
| **New module** | Entirely new `exporter/` package | `src/discord_ferry/exporter/{__init__, manager, runner}.py` |
| **Dependencies** | Add `ijson` to production deps | `pyproject.toml` |
| **Tests** | New test files for exporter, updated parser tests, CLI/GUI integration tests | `tests/` |
| **Docs** | Full user guide rewrite | `docs/` |

**Existing patterns to reuse**:
- Phase function signature: `async def run_export(config, state, exports, on_event)` — but note: exports won't exist yet at Phase 0, so signature may need `list[DCEExport] | None`
- `MigrationEvent` with `phase="export"` and `status="progress"` for DCE output streaming
- `aiohttp` session for Discord token validation (reuse config.session or create ephemeral)
- Retry pattern from `migrator/api.py` for GitHub API calls (DCE download)
- `_run_phases()` skip logic in engine.py already handles resume — extend for export phase

## Open Questions

1. **DCE version pinning**: Should Ferry pin a specific DCE version (e.g., v2.46.1) or always fetch latest? Pinning is safer but requires Ferry updates to track DCE releases.
2. **Export progress granularity**: DCE outputs channel-by-channel progress. Should we show per-channel progress in the GUI, or just an overall "Exporting..." with log lines?
3. **Disk space check**: Large servers can produce multi-GB exports. Should Ferry check available disk space before starting export?
4. **Cancellation during export**: The existing engine supports pause/cancel events. Should DCE subprocess be killable mid-export via the same mechanism?

## Complexity Tier

**Large** — New module (exporter/), architecture change (Phase 0), 8+ files modified, new dependency (ijson), GUI redesign, CLI restructuring, documentation overhaul. This is the largest change since the initial implementation.

Next step: `/brainstorm` to explore implementation approaches, then `/critique` before planning.
