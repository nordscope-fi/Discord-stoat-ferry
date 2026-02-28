# Design: Phase 0 — DCE Orchestration & Streaming Parser

**Brief**: `docs/plans/briefs/2026-02-28-phase0-orchestration.md`
**Status**: Approved
**Complexity**: Large (new module, engine changes, config changes, GUI/CLI overhaul, new dependency)

## Resolved Questions

| Question | Decision |
|----------|----------|
| DCE version strategy | Pin specific version (v2.46.1). Bump via Ferry update. |
| Progress granularity | Per-channel progress matching existing messages phase pattern |
| Disk space check | Warn if < 5GB free (non-blocking) |
| Cancel during export | Full cancel — SIGTERM the DCE subprocess |

## Architecture Decisions

1. **Thin subprocess wrapper** — DCE does the heavy lifting. Ferry orchestrates it via `asyncio.create_subprocess_exec` and parses stdout for progress.
2. **Hybrid streaming parser** — `json.loads` for metadata (tiny), `ijson` for messages (potentially huge). Flat memory regardless of export size.
3. **Export as inline pre-phase** — Handled in `run_migration()` alongside validate and report, not in `_DEFAULT_PHASES` dict. Clean separation from migration phases.

---

## 1. Exporter Module

```
src/discord_ferry/exporter/
├── __init__.py    # Public: run_export(), DCE_VERSION
├── manager.py     # DCE binary download/verify/locate
└── runner.py      # Async subprocess + stdout parsing
```

### manager.py

```python
DCE_VERSION = "2.46.1"

async def get_dce_path() -> Path | None:
    """Check ~/.discord-ferry/bin/dce/{version}/ for existing binary."""

async def download_dce(on_event: EventCallback) -> Path:
    """Download correct DCE release for OS/arch from GitHub Releases API."""

def detect_dotnet() -> bool:
    """Check for .NET 8+ runtime. Always True on Windows (self-contained)."""
```

- Storage: `~/.discord-ferry/bin/dce/{version}/`
- Platform: `platform.system()` + `platform.machine()` → asset name
- GitHub API: `GET /repos/Tyrrrz/DiscordChatExporter/releases/tags/v{version}`

### runner.py

```python
async def run_dce_export(
    config: FerryConfig,
    dce_path: Path,
    on_event: EventCallback,
) -> Path:
    """Run DCE exportguild subprocess, stream progress, return export_dir."""
```

DCE command:
```
dce exportguild --token {discord_token} -g {server_id}
    --media --reuse-media --markdown false --format Json
    --include-threads All --output {export_dir}
```

- Parse stdout regex: channel name + percentage per line
- Emit `MigrationEvent(phase="export", status="progress", channel_name=..., current=pct, total=100)`
- Cancel: monitor `config.cancel_event`, SIGTERM process
- Disk check: `shutil.disk_usage()` < 5GB → warning event

---

## 2. Config Changes

```python
@dataclass
class FerryConfig:
    # ... existing fields ...

    # New: Discord credentials (orchestrated mode, never persisted)
    discord_token: str | None = field(default=None, repr=False)
    discord_server_id: str | None = None

    # New: skip export phase (auto-set when export_dir provided directly)
    skip_export: bool = False
```

**Mode detection**:
- `discord_token` + `discord_server_id` provided → orchestrated mode
- `export_dir` provided by user → `skip_export = True` (offline mode)
- Both → error

**Token security**: `discord_token` is `repr=False`, never serialized to state.json, never written to disk.

---

## 3. Engine Integration

Export handled inline in `run_migration()`, same pattern as validate and report:

```python
async def run_migration(config, on_event, phase_overrides=None):
    # Phase 0: Export (inline)
    if not config.skip_export:
        # Validate Discord token first
        await _validate_discord_token(config.discord_token)
        # Ensure DCE binary
        dce_path = await get_dce_path() or await download_dce(on_event)
        if not detect_dotnet():
            raise DotNetMissingError(...)
        # Run export
        await run_dce_export(config, dce_path, on_event)

    # Phase 1: Parse + Validate (existing)
    exports = parse_export_directory(config.export_dir)
    ...
```

`"export"` added to `PHASE_ORDER` at index 0 for display/tracking.

### State addition

```python
export_completed: bool = False  # Set True after successful DCE export
```

**Smart resume**: if `export_completed and config.export_dir has .json files` → offer to skip export.

---

## 4. Streaming Parser

### Validate phase — metadata only

`parse_export_directory()` returns `DCEExport` objects with `messages=[]`. Uses existing `messageCount` from JSON metadata for counts. Lightweight markdown check scans first ~50 messages without loading all.

### Messages phase — ijson streaming

```python
def stream_messages(json_path: Path) -> Iterator[DCEMessage]:
    """Yield messages one at a time from a DCE JSON file."""
    with open(json_path, "rb") as f:
        for raw_msg in ijson.items(f, "messages.item"):
            yield _parse_message(raw_msg)
```

`DCEExport` gains `json_path: Path` field so messages phase knows which file to stream from.

`run_messages()` calls `stream_messages()` per export instead of reading `export.messages`.

**Memory**: ~1 message in memory at a time vs. entire export.

**Dependency**: `ijson` added to `pyproject.toml` production deps.

---

## 5. GUI Changes

### New screen flow

```
Screen 0: Mode Selection
  ├─ "1-Click Migration" → Screen 1a
  └─ "I already have exports" → Screen 1b

Screen 1a: Credentials (new)
  - Discord: token (masked), server ID, "How to find these?" modal
  - Stoat: URL (official/self-hosted toggle), token (masked)
  - ToS disclaimer checkbox
  - Advanced options (expandable)

Screen 1b: Offline Setup (existing, minor tweaks)

Screen 2: Export Progress (new, orchestrated only)
  - Phase chip progression
  - Per-channel progress bar
  - Log stream, cancel button
  - Auto-transition to Validate

Screens 3-5: Validate, Migrate, Done (existing)
```

### Smart resume dialog

If `state.export_completed and cached files exist`:
- "Found cached exports (X channels, Y files, Z MB)"
- [Use Cached] → skip to Validate
- [Re-export] → prompt Discord token → Export screen

---

## 6. CLI Changes

```bash
# Orchestrated (new default)
ferry migrate --discord-token TOKEN --discord-server ID \
    --stoat-url URL --token STOAT_TOKEN

# Offline (advanced)
ferry migrate --export-dir /path --stoat-url URL --token STOAT_TOKEN
```

- `export_dir` changes from positional to `--export-dir` (optional flag)
- `--discord-token` and `--discord-server` are new options
- Mutual exclusion: `--export-dir` vs `--discord-token`/`--discord-server`
- ToS disclaimer: printed on first orchestrated run, requires `yes` confirmation

---

## 7. Error Handling

### New exceptions (errors.py)

```python
class ExportError(MigrationError): ...
class DCENotFoundError(ExportError): ...
class DotNetMissingError(ExportError): ...
class DiscordAuthError(ExportError): ...
```

### Error matrix

| Scenario | Detection | Behavior |
|----------|-----------|----------|
| Invalid Discord token | 401 from /users/@me | Fail before DCE launch |
| .NET missing | dotnet --version check | Error with install URL |
| DCE download fails | HTTP error | Retry once, then error |
| DCE non-zero exit | returncode | Error with stderr |
| Low disk space | shutil.disk_usage() | Warning (non-blocking) |
| User cancels | cancel_event | SIGTERM → "cancelled" |
| Cached exports exist | state + filesystem | Smart resume dialog |
| Conflicting flags | CLI validation | "Cannot use both" error |

---

## 8. Testing

| Module | Approach | Tests |
|--------|----------|-------|
| manager.py | Mock GitHub API + filesystem | ~8 |
| runner.py | Mock subprocess, fake stdout | ~10 |
| engine.py export | Mock exporter, test skip/resume | ~6 |
| config.py | Mode detection, defaults | ~4 |
| dce_parser.py streaming | Fixtures + temp files | ~8 |
| CLI changes | Click test runner | ~6 |
| GUI flow | NiceGUI test client | ~4 |
| **Total** | | **~46 new tests** |

### New fixtures

- `tests/fixtures/dce_stdout_sample.txt` — DCE output for regex tests
- Large message streaming tested via in-test temp file generation

---

## 9. Files Changed

### New
- `src/discord_ferry/exporter/__init__.py`
- `src/discord_ferry/exporter/manager.py`
- `src/discord_ferry/exporter/runner.py`
- `tests/test_exporter_manager.py`
- `tests/test_exporter_runner.py`
- `tests/fixtures/dce_stdout_sample.txt`

### Modified
- `src/discord_ferry/config.py` — 3 new fields
- `src/discord_ferry/core/engine.py` — export pre-phase, PHASE_ORDER
- `src/discord_ferry/state.py` — export_completed field
- `src/discord_ferry/errors.py` — 4 new exception classes
- `src/discord_ferry/parser/dce_parser.py` — stream_messages(), metadata-only mode
- `src/discord_ferry/parser/models.py` — json_path field on DCEExport
- `src/discord_ferry/migrator/messages.py` — use stream_messages()
- `src/discord_ferry/gui.py` — new screens, mode selection, export progress
- `src/discord_ferry/cli.py` — argument restructuring
- `pyproject.toml` — add ijson dependency
- `docs/` — user guide rewrite

---

## 10. Milestones (Implementation Order)

1. **Config + Errors** — Add fields, exception classes. No behavior change yet.
2. **Exporter module** — manager.py + runner.py with tests. Standalone, testable.
3. **Engine integration** — Wire export into run_migration(). Smart resume.
4. **Streaming parser** — ijson integration, stream_messages(), update messages phase.
5. **GUI/CLI updates** — New screens, argument changes.
6. **Documentation** — MkDocs rewrite.
