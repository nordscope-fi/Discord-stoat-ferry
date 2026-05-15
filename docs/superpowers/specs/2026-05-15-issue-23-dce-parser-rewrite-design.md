# Design: DCE parser rewrite (issue #23)

**Date:** 2026-05-15
**Issue:** [#23](https://github.com/nordscope-fi/discord-stoat-ferry/issues/23)
**Ships as:** v2.1.4 (patch) + v2.2.0 (minor) — see Phasing below
**Status:** Spec — awaiting implementation (deferred to next session per user)

## Problem

Ferry's per-channel progress regex `\[\d+/\d+\] Exporting #(?P<channel>[^\s.]+)\.{3}\s*(?:(?P<pct>[\d.]+)%)?` (`src/discord_ferry/exporter/runner.py:26-28`) does not match any line DCE 2.47.1 actually prints. Verified by reading `Tyrrrz/DiscordChatExporter@2.47.1` source and code-searching the entire DCE repo for `Exporting #` and `Exporting {0}` (zero hits). Bug has been latent since the very first commit on 2026-02-28.

User-visible symptom (issue #23): GUI sits on "Discord Chat Exporter — Started" indefinitely. On large servers @The-Red-Priest waited 30 minutes with no progress shown. The Windows `conhost.exe` window also pops up empty (DCE's stdout is piped to Ferry, leaving the new console blank), reinforcing the perception that DCE is hung.

A secondary GUI gap: `gui.py:604` only pushes structured `MigrationEvent`s to the `log_display` panel. Raw DCE stdout that doesn't match a regex goes to `logger.debug` only, never reaches the GUI. So even if DCE is alive and printing `Fetching channels...`, the user sees nothing.

## Empirical confirmation of DCE 2.47.1 behavior

Captured via running DCE 2.47.1 on macOS-arm64 with stdout piped (the way Ferry invokes it). Full traces saved at `/tmp/dce-stdout-piped.log`, `/tmp/dce-stderr-piped.log`. Key findings:

- Line endings are **LF only** (`\n`); zero `\r` bytes in piped mode.
- No ANSI escape codes when piped (Spectre.Console detects piped stdout via `AnsiSupport.Detect` and falls back to plain text).
- Per-channel progress emitted as `<hierarchical channel name>: NN%` — only at milestones {25, 50, 75, 95, 96, 97, 98, 99, 100}%. Not continuous.
- Hierarchical names join with ` / ` (e.g. `Information / general / my-thread`), not Discord's `#channel` syntax.
- Headline lines (verbatim from `ExportGuildCommand.cs` and `ExportCommandBase.cs`):
  - `Fetching channels...`
  - `Fetched N channel(s).`
  - `Fetching threads...` (only if `--include-threads All`, which Ferry passes)
  - `Fetched N thread(s).`
  - `Exporting N channel(s)...`
  - `Successfully exported N channel(s).`
- The 11-line Ukraine support banner is printed first.
- Spectre's status ticker fallback emits `...` (literal three dots) during silent enumeration phases.

## Architecture

New module `src/discord_ferry/exporter/dce_output.py`:

```python
@dataclass(frozen=True)
class ParsedDceLine:
    kind: Literal["per_channel", "phase", "banner", "status_dot", "success", "error", "raw"]
    channel: str | None = None    # for per_channel
    pct: int | None = None        # for per_channel (0-100)
    count: int | None = None      # for phase / success ("Fetched 142 channel(s).")
    message: str | None = None    # original line text


def parse_dce_line(line: str) -> ParsedDceLine:
    """Pure, sync, no I/O. Maps one DCE stdout line to a typed result.

    Tries patterns in order:
      1. per_channel: ^(?P<channel>.+?):\s(?P<pct>\d+)%$
      2. phase: Fetching/Fetched headlines
      3. success: Successfully exported N channel(s).
      4. status_dot: ^\.{3,}$
      5. banner: Ukraine box-drawing line
      6. raw: anything else (fallthrough)
    """
```

`runner.py`'s stdout loop becomes thin orchestration:

```python
async for raw_line in process.stdout:
    if config.cancel_event and config.cancel_event.is_set():
        process.terminate()
        await process.wait()
        raise asyncio.CancelledError("Export cancelled by user")
    line = raw_line.decode("utf-8", errors="replace").strip()
    if not line:
        continue
    parsed = parse_dce_line(line)
    _emit_for_parsed(parsed, on_event)
```

`_emit_for_parsed` (private to `runner.py`) maps `ParsedDceLine` → `MigrationEvent` and calls `on_event`. The mapping table is the load-bearing UX decision (see Phasing).

### Boundary

The parser is pure and module-local. The runner orchestrates subprocess + cancel + event emission. Parser is unit-testable without subprocess mocking. `parse_dce_line` is also reusable by issue #35's captured-fixture replay tests.

## Components

| Component | Responsibility |
|-----------|----------------|
| `dce_output.parse_dce_line` | Pure, sync, no I/O. Maps one DCE stdout line to a typed result. |
| `dce_output.ParsedDceLine` (dataclass, frozen) | Tagged union of parse outcomes via `kind` field. |
| `runner._emit_for_parsed` | Maps `ParsedDceLine` to `MigrationEvent` and calls `on_event`. |
| `runner.run_dce_export` | Orchestration: spawn, drain stdout/stderr, emit, wait, cleanup. |
| `runner._build_dce_command` | Unchanged (already correct). |

## Data flow

```
DCE stdout (LF-terminated UTF-8)
    ↓ async for raw_line in process.stdout
.strip() and .decode()
    ↓
parse_dce_line(line)
    ↓
ParsedDceLine (one of: PerChannel, Phase, Banner, StatusDot, Success, Error, Raw)
    ↓
_emit_for_parsed(parsed, on_event)
    ↓
MigrationEvent → on_export_event (gui.py:600) → log_display.push() + progress_bar.set_value() + channel_label.set_text()
```

## Error handling

- `parse_dce_line` is total — every input maps to some `ParsedDceLine`. Never raises.
- Runner's existing exit-code check (`runner.py:195-197`) preserved unchanged.
- `_read_stderr` task drained on **all** exit paths via `try/finally` (currently leaked on cancel path).

```python
try:
    # ... process.stdout reading + cancel checks
except asyncio.CancelledError:
    process.terminate()
    await process.wait()
    raise
finally:
    if not stderr_task.done():
        stderr_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stderr_task
```

## Phasing

This work ships in two releases (decision recorded during brainstorming 2026-05-15):

### v2.1.4 (patch — ships first, ASAP)

| Change | File | Why patch (not minor) |
|--------|------|------------------------|
| New `dce_output.py` module with parser + all 6 regexes | new | Code is structural; behavior toggle is what makes it patch vs minor |
| `runner.py` calls `parse_dce_line` | runner.py | Plumbing |
| `_emit_for_parsed` emits `MigrationEvent` ONLY for `kind="per_channel"` and `kind="success"` | runner.py | These two are pure bugfix — channels were silent, now they show progress |
| Other parsed kinds → `logger.debug` only (preserves current GUI behavior) | runner.py | Keeps patch scope tight |
| `creationflags=subprocess.CREATE_NO_WINDOW` on Windows for both `runner.py` subprocess and `manager.py:detect_dotnet` | runner.py, manager.py | Windows console-window bug — pure bugfix |
| `_read_stderr` task cleaned up on all exit paths via `try/finally` | runner.py | Resource leak — pure bugfix |
| `tests/fixtures/dce_stdout_sample.txt` REPLACED with hand-derived-from-DCE-source full sequence | fixtures | Test infrastructure |
| New `tests/test_dce_output.py` with ~12 cases | new test | Unit coverage for parser |
| CHANGELOG `### Fixed` entry under v2.1.4 | CHANGELOG.md | Standard |

### v2.2.0 (minor — ships after v2.1.4, soon after)

| Change | File | Why minor |
|--------|------|-----------|
| `_emit_for_parsed` emits `MigrationEvent(progress)` for `kind="phase"` (Fetching/Fetched headlines) and `kind="banner"` and `kind="raw"` and `kind="status_dot"` | runner.py | User-visible behavior change: GUI log panel now shows ALL DCE output |
| Headline messages get a clear prefix in the GUI log (`[phase] Fetching channels...` vs `[dce] <raw>`) | runner.py / gui.py | UX polish |
| CHANGELOG `### Changed` entry under v2.2.0 | CHANGELOG.md | Standard |

### Honest framing of the split

- v2.1.4 is strictly bugfix — broken regex now correct, broken Windows window now hidden, leaked task now drained. A user upgrading from v2.1.3 to v2.1.4 sees the export they couldn't see before.
- v2.2.0 is additive UX — same code path, different routing decisions. A user upgrading from v2.1.4 to v2.2.0 also sees pre-export phase activity in the log panel.
- v2.1.4 alone delivers @The-Red-Priest's primary fix (per-channel progress visible). v2.2.0 closes the remaining gap (5–15 minute pre-export silence on large guilds).

### What's NOT in either release (covered by other issues)

- Heartbeat / silence-breaker emit → [#39](https://github.com/nordscope-fi/discord-stoat-ferry/issues/39)
- Real-DCE contract test in CI → [#34](https://github.com/nordscope-fi/discord-stoat-ferry/issues/34)
- Captured-real (vs hand-derived) fixture replacement → [#35](https://github.com/nordscope-fi/discord-stoat-ferry/issues/35)
- `isInline` embed field parser fix → [#36](https://github.com/nordscope-fi/discord-stoat-ferry/issues/36)
- ARM DCE checksum pinning → [#37](https://github.com/nordscope-fi/discord-stoat-ferry/issues/37)
- Windows `xdg-open` → `os.startfile` → [#38](https://github.com/nordscope-fi/discord-stoat-ferry/issues/38)

## UI behavior decisions (from brainstorming)

- **DCE Ukraine banner:** show all 11 lines in Ferry's log panel as raw `[dce] <line>` (v2.2.0). No filtering, no `FUCK_RUSSIA=1` env var. User sees what DCE actually emits.
- **Per-channel progress visualization:** progress bar shows current channel's % (jumps 0→25→50→75→95→100, resets per channel). Channel label shows current channel name. Simpler than overall-progress tracking; trades smoothness for implementation simplicity.
- **Pre-export phase UI** (v2.2.0 only): channel_label stays at "Preparing..." and progress_bar stays at 0; activity is visible only in the log panel.

## Testing

### Unit tests for the parser (`tests/test_dce_output.py` — NEW, ~12 cases)

| Test | Input | Expected `kind` |
|------|-------|-----------------|
| `test_per_channel_25` | `"general: 25%"` | `per_channel`, channel="general", pct=25 |
| `test_per_channel_hierarchical` | `"Information / general / my-thread: 50%"` | `per_channel`, channel="Information / general / my-thread", pct=50 |
| `test_per_channel_100` | `"announcements: 100%"` | `per_channel`, pct=100 |
| `test_phase_fetching_channels` | `"Fetching channels..."` | `phase` |
| `test_phase_fetched_channels` | `"Fetched 142 channel(s)."` | `phase`, count=142 |
| `test_phase_fetching_threads` | `"Fetching threads..."` | `phase` |
| `test_phase_fetched_threads` | `"Fetched 87 thread(s)."` | `phase`, count=87 |
| `test_phase_exporting_header` | `"Exporting 229 channel(s)..."` | `phase`, count=229 |
| `test_success` | `"Successfully exported 229 channel(s)."` | `success`, count=229 |
| `test_status_dot` | `"..."` | `status_dot` |
| `test_banner_line` | Box-drawing line from Ukraine banner | `banner` |
| `test_raw_fallthrough` | `"Some unknown DCE line"` | `raw`, message=line |

### Integration tests for runner (existing `tests/test_exporter_runner.py` updated)

v2.1.4 PR adds:
- `test_v2_1_4_per_channel_only_emits_progress`: feeds the new fixture, asserts `MigrationEvent`s fire ONLY for per_channel and success lines.
- `test_stderr_task_drained_on_cancel`: triggers `cancel_event`, asserts `_read_stderr` task is in `done()` state after `run_dce_export` returns.
- `test_create_no_window_on_windows`: monkeypatches `sys.platform = "win32"`, asserts `create_subprocess_exec` was called with `creationflags=subprocess.CREATE_NO_WINDOW`.

v2.2.0 PR adds:
- `test_v2_2_0_phase_lines_emit_progress_events`: same fixture, asserts headline lines now produce `MigrationEvent`s visible to the GUI.
- `test_v2_2_0_raw_lines_routed_to_gui`: feeds an unrecognized line, asserts it surfaces as `[dce] <line>` event.

### Replaced fixture (`tests/fixtures/dce_stdout_sample.txt`)

Contents: full DCE 2.47.1 output sequence, hand-derived from DCE source files (`ExportGuildCommand.cs`, `ExportCommandBase.cs`, Spectre's `FallbackProgressRenderer.cs`). Header comment in fixture:

```
# Hand-derived from Tyrrrz/DiscordChatExporter@2.47.1 source.
# NOT a real capture. See issue #35 for replacement plan with captured fixtures.
```

Full sequence: Ukraine banner (11 lines) → blank → `Fetching channels...` → `...` (status dot) → `Fetched 142 channel(s).` → `Fetching threads...` → `Fetched 87 thread(s).` → `Exporting 229 channel(s)...` → `general: 25%` → `general: 50%` → `general: 75%` → `general: 95%` → `general: 100%` → `announcements: 25%` → ... → `Successfully exported 229 channel(s).`

## Decision Accountability gate

| Claim | Verdict | Evidence |
|-------|---------|----------|
| "Pure-function parser is the right boundary" | VERIFIED | Reusable by #35; testable without subprocess mocking |
| "v2.1.4 patch is honest semver" | VERIFIED | Strictly bugfix scope; no API changes; behavior matches user expectation of "patch" |
| "Hand-derived fixture is acceptable for v2.1.4" | PARTIALLY VERIFIED | Defended by #34 (contract test) and #35 (captured fixtures) as separate issues; documented in fixture header comment |
| "Per-channel-only progress is sufficient UX" | ACCEPTED TRADEOFF | Simpler than overall-progress tracking; bar resets per channel |

**Foundational?** YES (parser is the contract with DCE). Cannot defer; ships in v2.1.4.

## Deferrals

| Item | Why not now | Tracking | Trigger |
|------|-------------|----------|---------|
| Captured-real fixture (vs hand-derived) | Requires test Discord server | #35 | After #23 ships |
| Real-DCE invocation in CI | Requires CI workflow changes | #34 | After #23 ships |
| Headline silence-breaker heartbeat | Out of scope; #23's fix already breaks up most silence | #39 | After v2.2.0 ships if user reports persistent silence on largest guilds |

## Open questions (for implementation, not blocking spec)

- Exact regex for Ukraine banner detection (matches `^[┌│└][─\s]+[┐│┘]$`?). Decide during implementation.
- Whether to suppress the empty-line after the Ukraine banner box. Cosmetic.
- Whether `_emit_for_parsed` lives in `dce_output.py` or `runner.py`. Both work; runner.py keeps the on_event coupling local.
