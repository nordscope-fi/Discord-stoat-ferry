# Design: DCE parser rewrite (issue #23)

**Date:** 2026-05-15 (revised 2026-05-16 after first critique pass; revised again 2026-05-16 after second critique pass)
**Issue:** [#23](https://github.com/nordscope-fi/discord-stoat-ferry/issues/23)
**Ships as:** v2.2.0 (minor) — single release; the original v2.1.4/v2.2.0 split was ceremony, see Phasing
**Status:** Spec — awaiting implementation (deferred to next session per user)

## Revisions from second critique pass (2026-05-16, later)

The second-pass critique caught implementation-blocking bugs the first revision introduced. Fixes applied:

| Second-pass finding | Resolution |
|---------------------|------------|
| `PerChannel` regex `^.+?:\s\d+%$` non-greedy contradicts the channel-with-colon test (would match only `"category"` from `"category: subname / channel: 50%"`) | **Made regex greedy + anchored: `^(?P<channel>.+):\s(?P<pct>\d+)%$`** — backtracking lets it match the largest channel that still leaves `: <digits>%` at the end |
| `channels_done` overshoots on DCE channel retries (a channel that hits 100%, fails, retries from 25%, hits 100% again, double-counts) | **Replaced counter with set: `state.channels_completed: set[str]`; `channels_done = len(channels_completed)`** — name dedup is structural |
| `gui.py` changes missing from the change table | **Added row to Phasing table; new "GUI consumer changes" section** |
| `LimitOverrunError` handler consumed `exc.consumed` bytes but left the rest of the over-long line in the buffer (next `readuntil` would return the remainder as a "line") | **Replaced with full-drain loop that keeps consuming until next `\n` (or EOF) is reached, then emits one truncated-byte-count event** |
| `Phase.kind: str` is weaker than the rest of the union (typos slip past type checks) | **Changed to `Literal["fetching_channels", "fetched_channels", "fetching_threads", "fetched_threads", "exporting_header"]`** |
| `_emit_for_parsed` location was tentative ("Tentative: runner.py") | **Decided: runner.py** (state lives there); removed from open questions |
| `MigrationEvent.current/total` semantics flip from per-channel-pct to overall-channels — undocumented breaking shape | **Added explicit CHANGELOG `### Changed` entry calling this out for any external consumer** |
| `gui.py:608-609` hard-codes `f"Exporting #{event.channel_name}..."` — `#` prefix is wrong for DCE's hierarchical names like `Information / general / my-thread` | **Spec now explicitly removes the `#` prefix in `on_export_event`; channel_label uses `f"{channel} ({pct}%)"` directly** |
| Critique claimed `CTRL_BREAK_EVENT` signals Ferry too — actually mitigated by `CREATE_NEW_PROCESS_GROUP` (child is in its own group, signal targets only that group) | **Added clarifying paragraph in Windows cancellation section explaining the isolation** |

## Revisions from first critique pass (2026-05-16)

The first critique identified six critical findings + significant gaps. Resolutions:

| First-pass finding | Resolution |
|--------------------|------------|
| Phasing rationale collapses (v2.1.4 vs v2.2.0 split is weak semver) | Collapsed to single v2.2.0 release |
| Per-channel UI bar reset is wrong UX | Reversed: overall progress (channels_done / total) for bar; per-channel pct in label |
| Cancel path on Windows unanalyzed | New section "Windows cancellation semantics" |
| `/tmp/dce-stdout-piped.log` already exists | Used as captured prefix; hybrid-derived suffix from DCE source |
| `ParsedDceLine` conflates tagged union | Restructured as per-kind dataclasses (Python 3.10 union) |
| 64KB StreamReader limit unaddressed | Error handling for `LimitOverrunError` (full-drain in second-pass revision) |
| Adversarial test coverage gap | Added 6 new test cases |
| Success vs Exporting count delta | Surface as warning event |

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

### Captured trace coverage and limits

The capture (`/tmp/dce-stdout-piped.log`, 973 bytes, 13 lines, captured 2026-05-15 23:05) ran with an intentionally invalid token, so it covers the **pre-auth** sequence only:

- Lines 1-9: Ukraine support banner (full 11-line box).
- Line 10: blank.
- Line 11: `Fetching channels...`
- Line 12: `...` (Spectre status-dot fallback).
- Line 13: termination on auth failure (no further DCE-side output).

Post-auth headlines (`Fetched N channel(s).`, `Fetching threads...`, `Fetched N thread(s).`, `Exporting N channel(s)...`, per-channel `name: NN%` lines, `Successfully exported N channel(s).`) are not in this capture. They are derived from DCE source (`ExportGuildCommand.cs`, `ExportCommandBase.cs`, Spectre's `FallbackProgressRenderer.cs`) read at the same `2.47.1` tag. The fixture is therefore **hybrid provenance**: captured prefix + source-derived suffix. Replacing the suffix with a real capture is tracked as #35 (requires test Discord server credentials).

## Architecture

New module `src/discord_ferry/exporter/dce_output.py`. The parser returns one of seven per-kind dataclasses; `ParsedDceLine` is the Python 3.10+ type union of those:

```python
from __future__ import annotations
from dataclasses import dataclass
from typing import Literal

@dataclass(frozen=True, slots=True)
class PerChannel:
    channel: str   # e.g. "Information / general / my-thread"
    pct: int       # 0-100, integer

PhaseKind = Literal[
    "fetching_channels", "fetched_channels",
    "fetching_threads", "fetched_threads",
    "exporting_header",
]

@dataclass(frozen=True, slots=True)
class Phase:
    """Headline phase line from DCE.

    count is None for 'fetching_*' (no integer in the line),
    int for 'fetched_*' and 'exporting_header'.
    """
    kind: PhaseKind   # Literal-typed; mypy/pyright catches typos at static-check time
    count: int | None
    message: str  # original line text, for log panel display

@dataclass(frozen=True, slots=True)
class Success:
    count: int   # from "Successfully exported N channel(s)."
    message: str

@dataclass(frozen=True, slots=True)
class Banner:
    message: str  # one Ukraine-banner line (box-drawing or text inside)

@dataclass(frozen=True, slots=True)
class StatusDot:
    """Spectre status-ticker fallback: a line of '...'."""
    message: str  # always "..." but kept for log faithfulness

@dataclass(frozen=True, slots=True)
class Error:
    """Reserved for future use; parser does not currently emit Error.
    DCE writes errors to stderr, which is handled by _read_stderr.
    Kept in the union so error-path lines added later are non-breaking.
    """
    message: str

@dataclass(frozen=True, slots=True)
class Raw:
    message: str  # any line that didn't match a more specific kind

ParsedDceLine = PerChannel | Phase | Success | Banner | StatusDot | Error | Raw


def parse_dce_line(line: str) -> ParsedDceLine:
    """Pure, sync, no I/O. Maps one DCE stdout line to a typed result.

    Tries patterns in order:
      1. PerChannel:   ^(?P<channel>.+):\s(?P<pct>\d+)%$
                       (greedy + anchored — channel may itself contain colons,
                       e.g. "category: subname / channel: 50%". Backtracking
                       resolves to the largest channel that leaves ': <digits>%'
                       at the end.)
      2. Phase:        Fetching/Fetched headlines + Exporting header
      3. Success:      ^Successfully exported (?P<n>\d+) channel\(s\)\.$
      4. StatusDot:    ^\.{3,}$
      5. Banner:       Ukraine box-drawing line (^[box-edge] ... [box-edge]$ or text inside)
      6. Raw:          fallthrough (never raises; total function)
    """
```

Callers `match` on the union:

```python
match parsed:
    case PerChannel(channel=ch, pct=p):
        ...
    case Phase(kind="exporting_header", count=n):
        ...
    case Success(count=n):
        ...
    case Banner() | StatusDot() | Raw():
        ...
```

`runner.py`'s stdout loop becomes thin orchestration:

```python
async for raw_line in process.stdout:
    if config.cancel_event and config.cancel_event.is_set():
        await _terminate_process(process)  # platform-aware (see Windows section)
        raise asyncio.CancelledError("Export cancelled by user")
    line = raw_line.decode("utf-8", errors="replace").strip()
    if not line:
        continue
    parsed = parse_dce_line(line)
    _emit_for_parsed(parsed, on_event, state)
```

`_emit_for_parsed` (private to `runner.py`) maps `ParsedDceLine` to `MigrationEvent` and calls `on_event`. It receives a small `state` object that tracks `total_channels` (set when `Phase(kind="exporting_header")` arrives) and `channels_done` (incremented when a `PerChannel` reaches `pct == 100`).

### Boundary

The parser is pure and module-local. The runner orchestrates subprocess + cancel + event emission. Parser is unit-testable without subprocess mocking. `parse_dce_line` is also reusable by issue #35's captured-fixture replay tests.

## Components

| Component | Responsibility |
|-----------|----------------|
| `dce_output.parse_dce_line` | Pure, sync, no I/O. Maps one DCE stdout line to a `ParsedDceLine` union. |
| `dce_output.{PerChannel, Phase, Success, Banner, StatusDot, Error, Raw}` | Per-kind frozen-slots dataclasses; `ParsedDceLine = ...` union alias. |
| `runner._RunState` | Mutable per-export state (total_channels, channels_done) used to compute overall progress. |
| `runner._emit_for_parsed` | Maps a `ParsedDceLine` (matched on type) to `MigrationEvent` and calls `on_event`. |
| `runner._terminate_process` | Platform-aware termination: POSIX `terminate()`, Windows `CTRL_BREAK_EVENT` (see Windows section). |
| `runner.run_dce_export` | Orchestration: spawn, drain stdout/stderr, emit, wait, cleanup. |
| `runner._build_dce_command` | Unchanged (already correct). |

## Data flow

```
DCE stdout (LF-terminated UTF-8)
    -> async for raw_line in process.stdout  (StreamReader, 64KiB line limit)
.strip() and .decode()
    -> parse_dce_line(line) -> ParsedDceLine union
    -> match parsed: case PerChannel | Phase | Success | Banner | StatusDot | Raw
    -> _emit_for_parsed(parsed, on_event, state)
    -> MigrationEvent(current=channels_done, total=total_channels,
                      channel_name=<current>, message=<per-channel pct in label>)
    -> on_export_event (gui.py:600) -> log_display.push() + progress_bar.set_value() + channel_label.set_text()
```

## UI behavior decisions (revised from brainstorming)

### Progress bar = overall progress, not per-channel

**Reversed from the original brainstorming choice.** A 229-channel guild under per-channel-bar UX = 229 reset cycles (0->25->50->75->100->0->25->...). That's user-hostile and the "complexity defense" was false: tracking `(channels_done, total)` is one extra integer.

- `Phase(kind="exporting_header", count=N)` arrives -> `state.total_channels = N` (set-once; subsequent headers are ignored to defend against DCE retries re-emitting the header), emit `MigrationEvent(current=0, total=N, message="Exporting N channel(s)...")`.
- Each `PerChannel(channel, pct)` event:
  - Updates `channel_label` to `f"{channel} ({pct}%)"`.
  - When `pct == 100`, **adds `channel` to `state.channels_completed: set[str]`** (set, not counter) and emits a fresh `MigrationEvent(current=len(channels_completed), total=total_channels, channel_name=channel, message=f"Finished {channel}")`.
  - For `pct < 100`, emits `MigrationEvent(current=len(channels_completed), total=total_channels, channel_name=channel, message=f"{channel}: {pct}%")` so the log gets a line but the bar value doesn't move backward.
- **Set-based dedup is structural defense against DCE channel retries.** DCE re-emits `name: 25%` from the start when a channel transient-fails and retries (verified by reading `ChannelExporter.cs` retry loop in DCE source). With a counter, the same channel hitting 100% twice would double-count and overshoot `total_channels`. With a set, idempotence is automatic.
- The overall bar is monotonic non-decreasing (set size never shrinks).
- The per-channel detail (`pct`) lives in the channel label, where reset is expected on channel change.

### Pre-export phase UI

Before `Exporting N channel(s)...` arrives, `state.total_channels` is `None`. During this window:
- `progress_bar` stays at 0 / indeterminate.
- `channel_label` shows the most recent phase message (`"Fetching channels..."`, `"Fetched 142 channel(s)."`, `"Fetching threads..."`, `"Fetched 87 thread(s)."`).
- All raw lines (banner, status-dot, unrecognized lines) are shown in the log panel as `[dce] <line>`.

### DCE Ukraine banner

Show all 11 lines in Ferry's log panel as `[dce] <line>`. No filtering, no `FUCK_RUSSIA=1` env var. User sees what DCE actually emits.

### GUI consumer changes (`gui.py`)

The current `on_export_event` at `gui.py:600-614` hard-codes `f"Exporting #{event.channel_name}..."` for the channel label and uses `event.current / event.total` as a 0-100 progress fraction. With #23 v2.2.0:

- **Drop the `#` prefix.** DCE emits hierarchical names like `Information / general / my-thread`; prefixing with `#` produces the misleading `#Information / general / my-thread`. Use `event.message` directly when set (the parser controls the format), falling back to `event.channel_name` only if `message` is empty.
- **`event.current / event.total` is now `channels_done / total_channels`** (per-export semantics), no longer a per-channel percentage. The fraction calculation `progress_bar.set_value(event.current / event.total)` still works correctly because the new semantics also produce a 0-1 fraction (just with different meaning) — but the user-visible label must change to reflect "channel X of Y" rather than "X% complete."
- `channel_label` for `PerChannel` events: render as `event.message` (which the parser sets to `f"{channel} ({pct}%)"` or `f"Finished {channel}"`).
- `channel_label` for `Phase` events: render the phase line directly (`"Fetching channels..."`, etc.) so users see the pre-export phase activity in the label, not just the log.

Implementation note: `on_export_event` should `match event.status / event.message` to dispatch correctly. The single-line `log_display.push(f"[{event.status}] {event.message}")` is preserved (every event still flows to the log).

### Success vs Exporting count delta = warning

If `Success(count=S)` arrives and `S < state.total_channels`, some channels failed to export silently. Surface as:

```python
on_event(MigrationEvent(
    phase="export", status="warning",
    message=f"DCE reports {S} of {state.total_channels} channels exported successfully. "
            f"{state.total_channels - S} channel(s) appear to have failed silently.",
))
```

Then emit the normal completion event.

## Windows cancellation semantics

The original spec wrote `process.terminate()` for cancel. On POSIX this sends `SIGTERM` and DCE has a chance to flush partial JSON files before exit. On Windows, `asyncio.subprocess.Process.terminate()` calls `TerminateProcess` (Windows API). Per Microsoft docs: `TerminateProcess` is **immediate**, **asynchronous from the target's perspective**, and **does not give the process any opportunity to clean up** (no atexit, no buffer flush, no destructor). DCE on Windows would be killed mid-write to a JSON file, leaving partial/truncated files in `export_dir`.

Two acceptable resolutions:

1. **Accept the asymmetry.** Document in user-facing release notes: "On Windows, cancelling a running export may leave partial JSON files in the output folder; delete the export folder before retrying." Pro: zero implementation cost. Con: silently bad UX for cancel-mid-export.

2. **Send `CTRL_BREAK_EVENT` instead.** Spawn DCE with `creationflags |= subprocess.CREATE_NEW_PROCESS_GROUP` on Windows, then on cancel call `process.send_signal(signal.CTRL_BREAK_EVENT)`. .NET's default console handler for Ctrl+Break runs a graceful shutdown (CancelKeyPress event in System.Console). DCE is a normal .NET console app and should honor it; partial files are still possible but DCE has at least a chance to close handles.

**Process group isolation (clarifying second-pass critique concern):** the second-pass critique flagged that `process.send_signal(CTRL_BREAK_EVENT)` could signal Ferry too if both share a console group. **`CREATE_NEW_PROCESS_GROUP` prevents this:** it places the child in its own process group (group ID = child PID), and Python's `Popen.send_signal(CTRL_BREAK_EVENT)` on Windows calls `GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, child_group_id)` — the signal targets only the child's group, not Ferry's. (Per CPython source `Lib/asyncio/windows_utils.py` and `_winapi.GenerateConsoleCtrlEvent` docs.) Ferry is unaffected.

**Decision: option 2.** Implementation cost is ~10 lines (a `_terminate_process(process)` helper + the `creationflags` bit at spawn time). Code:

```python
import signal, subprocess, sys

_CREATE_NEW_PROCESS_GROUP = (
    subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
)
_CREATE_NO_WINDOW = (
    subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
)

# at spawn:
process = await asyncio.create_subprocess_exec(
    *cmd,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
    creationflags=_CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP,
)

async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    if sys.platform == "win32":
        try:
            process.send_signal(signal.CTRL_BREAK_EVENT)
        except (ValueError, OSError):
            process.terminate()  # fallback: hard kill
        # Give DCE up to 3 s to clean up; then hard-kill if still alive.
        try:
            await asyncio.wait_for(process.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
    else:
        process.terminate()
        await process.wait()
```

**Documented caveat (release notes):** even with `CTRL_BREAK_EVENT`, DCE may not always finish writing the file currently being exported. Users should still expect "may have one truncated JSON" in the output folder when cancelling mid-channel.

## Error handling

- `parse_dce_line` is total -- every input maps to some `ParsedDceLine`. Never raises.
- Runner's existing exit-code check (`runner.py:195-197`) preserved unchanged.
- `_read_stderr` task drained on **all** exit paths via `try/finally` (currently leaked on cancel path).
- **`asyncio.LimitOverrunError` on long lines.** `asyncio.create_subprocess_exec` defaults to a 64 KiB StreamReader line buffer. A DCE log line >64 KiB (improbable but possible -- e.g., a malformed JSON path printed in an error trace) raises `LimitOverrunError` from `async for raw_line in process.stdout`. Without a handler this would bubble up, bypass cancel checks, and crash the GUI.

```python
try:
    # ... process.stdout reading + cancel checks
    while True:
        try:
            raw_line = await process.stdout.readuntil(b"\n")
        except asyncio.IncompleteReadError as exc:
            raw_line = exc.partial
            if not raw_line:
                break  # clean EOF
        except asyncio.LimitOverrunError:
            # Line longer than 64 KiB. Drain to next \n entirely so the
            # remainder doesn't get parsed as a separate "line".
            consumed_total = await _drain_overlong_line(process.stdout)
            on_event(MigrationEvent(
                phase="export", status="progress",
                message=f"[dce] <truncated {consumed_total} bytes; line exceeded 64 KiB>",
            ))
            continue
        # ... cancel check, decode, parse, emit
except asyncio.CancelledError:
    await _terminate_process(process)
    raise
finally:
    if not stderr_task.done():
        stderr_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stderr_task


async def _drain_overlong_line(reader: asyncio.StreamReader) -> int:
    """Consume bytes from `reader` until we reach a `\n` (or EOF).

    Used after `readuntil(b"\\n")` raised LimitOverrunError to ensure the
    remainder of the over-long line doesn't end up returned by the next
    `readuntil` call as if it were its own line. Returns total bytes consumed.
    """
    total = 0
    while True:
        try:
            tail = await reader.readuntil(b"\n")
            total += len(tail)
            return total
        except asyncio.LimitOverrunError as exc:
            # Still no \n in the next 64 KiB window; consume and keep draining.
            chunk = await reader.readexactly(exc.consumed)
            total += len(chunk)
        except asyncio.IncompleteReadError as exc:
            # EOF reached without ever finding \n.
            total += len(exc.partial)
            return total
```

(Switching from `async for raw_line in process.stdout` to an explicit `readuntil` loop is what gives us the seam to catch `LimitOverrunError`. The behavior on normal-length lines is identical. The `_drain_overlong_line` helper closes the second-pass-critique gap where the first revision's handler consumed only `exc.consumed` bytes and left the line's remainder in the buffer.)

## Phasing

**Single release: v2.2.0.** The original spec split the work into v2.1.4 (patch -- emit only `per_channel` and `success`) and v2.2.0 (minor -- also emit `phase`, `banner`, `raw`, `status_dot`). On reflection this was ceremony, not honest semver:

- Both releases ship the **same code** (parser + runner orchestration). The split was purely a routing toggle in `_emit_for_parsed`.
- The user-visible behavior change in either alone wouldn't justify a separate semver bump:
  - "v2.1.4-only" = per-channel progress visible (good), but user still sees nothing for the 5-15 minute pre-export silence (the actual frustration in #23). Calling that a "patch" while leaving the headline gap unfixed is dishonest.
  - "v2.2.0-only" = full output. This is what users actually need.
- The risk-isolation argument ("ship the bugfix faster, polish later") is weak: the parser is the same code in both PRs, so the risky surface is identical.

Going with single v2.2.0:

| Change | File |
|--------|------|
| New `dce_output.py` module with parser + per-kind dataclasses | new |
| `runner.py` calls `parse_dce_line` and matches on the union | runner.py |
| `_emit_for_parsed` emits `MigrationEvent` for all kinds (overall-progress bar + per-channel label + log-panel for everything) | runner.py |
| `_terminate_process` helper with Windows `CTRL_BREAK_EVENT` path | runner.py |
| `creationflags=CREATE_NO_WINDOW \| CREATE_NEW_PROCESS_GROUP` on Windows for both `runner.py` subprocess and `manager.py:detect_dotnet` (latter only `CREATE_NO_WINDOW`) | runner.py, manager.py |
| `_read_stderr` task cleaned up on all exit paths via `try/finally` | runner.py |
| Switch `async for` over stdout to explicit `readuntil` loop with `LimitOverrunError` handler + `_drain_overlong_line` helper | runner.py |
| **`gui.py:on_export_event` updated**: stop hard-coding `f"Exporting #{event.channel_name}..."` (the `#` prefix is wrong for DCE's hierarchical names like `Information / general / my-thread`); use `event.message` for label updates so the parser controls the format | gui.py |
| **CHANGELOG `### Changed` entry: `MigrationEvent.current/total` semantics flip from per-channel-pct to overall-channels (channels_done / total_channels)** for export events. Any external consumer reading those fields needs to know. | CHANGELOG.md |
| `tests/fixtures/dce_stdout_sample.txt` REPLACED with hybrid (captured prefix + source-derived suffix) fixture | fixtures |
| New `tests/test_dce_output.py` with ~18 cases (12 happy + 6 adversarial) | new test |
| CHANGELOG `### Fixed` entries (parser, Windows console flash, stderr-task leak, LimitOverrunError crash) | CHANGELOG.md |

### What's NOT in this release (covered by other issues)

- Heartbeat / silence-breaker emit -> [#39](https://github.com/nordscope-fi/discord-stoat-ferry/issues/39)
- Real-DCE contract test in CI -> [#34](https://github.com/nordscope-fi/discord-stoat-ferry/issues/34)
- Fully-captured (vs hybrid) fixture replacement -> [#35](https://github.com/nordscope-fi/discord-stoat-ferry/issues/35)
- `isInline` embed field parser fix -> [#36](https://github.com/nordscope-fi/discord-stoat-ferry/issues/36)
- ARM DCE checksum pinning -> [#37](https://github.com/nordscope-fi/discord-stoat-ferry/issues/37)
- Windows `xdg-open` -> `os.startfile` -> [#38](https://github.com/nordscope-fi/discord-stoat-ferry/issues/38)

## Testing

### Unit tests for the parser (`tests/test_dce_output.py` -- NEW, ~18 cases)

#### Happy-path cases

| Test | Input | Expected |
|------|-------|----------|
| `test_per_channel_25` | `"general: 25%"` | `PerChannel(channel="general", pct=25)` |
| `test_per_channel_hierarchical` | `"Information / general / my-thread: 50%"` | `PerChannel(channel="Information / general / my-thread", pct=50)` |
| `test_per_channel_100` | `"announcements: 100%"` | `PerChannel(pct=100)` |
| `test_phase_fetching_channels` | `"Fetching channels..."` | `Phase(kind="fetching_channels", count=None)` |
| `test_phase_fetched_channels` | `"Fetched 142 channel(s)."` | `Phase(kind="fetched_channels", count=142)` |
| `test_phase_fetching_threads` | `"Fetching threads..."` | `Phase(kind="fetching_threads", count=None)` |
| `test_phase_fetched_threads` | `"Fetched 87 thread(s)."` | `Phase(kind="fetched_threads", count=87)` |
| `test_phase_exporting_header` | `"Exporting 229 channel(s)..."` | `Phase(kind="exporting_header", count=229)` |
| `test_success` | `"Successfully exported 229 channel(s)."` | `Success(count=229)` |
| `test_status_dot` | `"..."` | `StatusDot` |
| `test_banner_line` | Box-drawing line from Ukraine banner | `Banner` |
| `test_raw_fallthrough` | `"Some unknown DCE line"` | `Raw(message="Some unknown DCE line")` |

#### Adversarial cases (added in this revision)

| Test | Input | Expected | Why |
|------|-------|----------|-----|
| `test_per_channel_no_percent_falls_through` | `"general: 25"` | `Raw` (NOT `PerChannel`) | Regex requires `%` suffix; bare "25" might be a count or a name |
| `test_per_channel_no_space_falls_through` | `"general:25%"` | `Raw` (NOT `PerChannel`) | Spectre always emits `: ` (colon-space); be strict |
| `test_per_channel_decimal_pct_falls_through` | `"general: 25.5%"` | `Raw` (NOT `PerChannel`) | DCE per-source emits integer-only milestone percents; reject decimals |
| `test_per_channel_with_colon_in_name` | `"category: subname / channel: 50%"` | `PerChannel(channel="category: subname / channel", pct=50)` | Greedy match on the trailing `: NN%` suffix; channel names may contain `:` |
| `test_long_line_handled_at_runner` | (line >64 KiB) | parser is unaffected; runner test asserts `Raw` event | LimitOverrunError is a runner concern, but parse_dce_line on a long string is still total |
| `test_success_count_zero` | `"Successfully exported 0 channel(s)."` | `Success(count=0)` | Edge case: empty server or all channels filtered out |

### Integration tests for runner (existing `tests/test_exporter_runner.py` updated)

- `test_per_channel_emits_overall_progress`: feeds the new fixture; asserts `MigrationEvent.current` is monotonic non-decreasing and reaches `total` at the end. Asserts `channel_name` updates per channel.
- `test_phase_lines_emit_progress_events`: same fixture; asserts headline lines produce `MigrationEvent`s visible in the GUI log panel.
- `test_raw_lines_routed_to_gui`: feeds an unrecognized line; asserts it surfaces as `[dce] <line>` event (not just `logger.debug`).
- `test_success_count_less_than_total_emits_warning`: simulates `Phase(exporting_header, count=10)` then `Success(count=7)`; asserts a `status="warning"` event mentioning `7 of 10` and `3 ... failed silently`.
- `test_stderr_task_drained_on_cancel`: triggers `cancel_event`; asserts `_read_stderr` task is `done()` after `run_dce_export` returns.
- `test_create_no_window_on_windows`: monkeypatches `sys.platform = "win32"`; asserts `create_subprocess_exec` called with `creationflags` including `CREATE_NO_WINDOW` and `CREATE_NEW_PROCESS_GROUP`.
- `test_cancel_sends_ctrl_break_on_windows`: monkeypatches `sys.platform = "win32"` + a fake `process.send_signal`; asserts `CTRL_BREAK_EVENT` was sent on cancel, not raw `terminate`.
- `test_long_line_emits_raw_truncated_event`: feeds a >64 KiB single line into the runner's stdout reader; asserts a `[dce] <truncated N bytes; line exceeded 64 KiB>` event was emitted and the loop did NOT crash.

### Replaced fixture (`tests/fixtures/dce_stdout_sample.txt`)

Hybrid: prefix is the captured `/tmp/dce-stdout-piped.log` (Ukraine banner + `Fetching channels...` + status-dot); suffix is source-derived (post-auth headlines, per-channel `name: NN%` milestones, `Successfully exported N channel(s).`).

Header comment in fixture:

```
# Hybrid DCE 2.47.1 stdout fixture for Ferry parser tests.
#
# Lines 1-13: captured 2026-05-15 from real DCE 2.47.1 invocation
# (macOS-arm64, stdout piped, intentionally invalid token to keep
# the capture short). Original at /tmp/dce-stdout-piped.log.
#
# Lines 14+: derived from DCE source at tag 2.47.1 -- specifically
# ExportGuildCommand.cs, ExportCommandBase.cs, and Spectre's
# FallbackProgressRenderer.cs. The post-auth path is not yet
# captured because we don't have a test Discord server. See #35
# for the plan to replace the suffix with a real capture.
```

Full sequence: Ukraine banner (11 lines) -> blank -> `Fetching channels...` -> `...` (status dot) -> [HYBRID BOUNDARY] -> `Fetched 142 channel(s).` -> `Fetching threads...` -> `Fetched 87 thread(s).` -> `Exporting 229 channel(s)...` -> `general: 25%` -> `general: 50%` -> `general: 75%` -> `general: 95%` -> `general: 100%` -> `announcements: 25%` -> ... -> `Successfully exported 229 channel(s).`

## Decision Accountability gate

| Claim | Verdict | Evidence |
|-------|---------|----------|
| "Pure-function parser with per-kind union is the right boundary" | VERIFIED | Reusable by #35; testable without subprocess mocking; `match` on union is more readable than `kind`-string dispatch |
| "Single v2.2.0 release is honest semver" | VERIFIED | Critique pass showed the v2.1.4/v2.2.0 split was ceremony -- same code, only routing toggle differed; user-visible behavior change in either alone unjustified the bump |
| "Hybrid (captured + source-derived) fixture is acceptable for v2.2.0" | PARTIALLY VERIFIED | Captured prefix is real; source-derived suffix is honest about its provenance; #35 tracks full-capture replacement |
| "Overall-progress bar with per-channel pct in label is the right UX" | VERIFIED | Reversed from original brainstorming choice after critique; 229-channel guild = monotonic bar instead of 229 reset cycles; complexity cost is one extra integer in `_RunState` |
| "`CTRL_BREAK_EVENT` on Windows is worth ~10 lines vs `TerminateProcess` immediate-kill" | VERIFIED | Microsoft docs: `TerminateProcess` gives no cleanup chance, leaving partial JSON files. `CTRL_BREAK_EVENT` lets DCE's CancelKeyPress handler run before the 3 s timeout falls back to hard-kill |
| "`LimitOverrunError` handling is needed even though >64 KiB lines are improbable" | VERIFIED | A single unhandled exception bypasses cancel checks AND crashes the GUI's event loop; cost to handle is ~10 lines and the behavior is well-defined |

**Foundational?** YES (parser is the contract with DCE). Cannot defer; ships in v2.2.0.

## Deferrals

| Item | Why not now | Tracking | Trigger |
|------|-------------|----------|---------|
| Fully-captured fixture (replace source-derived suffix) | Requires test Discord server | #35 | After #23 ships |
| Real-DCE invocation in CI | Requires CI workflow changes | #34 | After #23 ships |
| Headline silence-breaker heartbeat | Out of scope; #23's fix already breaks up most silence | #39 | After v2.2.0 ships if user reports persistent silence on largest guilds |
| Stronger Windows-cancel guarantees (kill child process tree, recover partial JSON) | Out of scope; `CTRL_BREAK_EVENT` + 3 s timeout is the 80/20 | -- | If users report partial-file issues on Windows post v2.2.0 |

## Open questions (for implementation, not blocking spec)

- Exact regex for Ukraine banner detection (matches box-drawing edge characters). Decide during implementation; Banner classification is cosmetic (line still surfaces in log either way).
- Whether to suppress the empty line after the Ukraine banner box. Cosmetic; current decision is to NOT suppress (`if not line: continue` already drops empty lines before parse).

## Resolved during second-pass critique

- **`_emit_for_parsed` location:** **runner.py** (state lives there; coupling is local). Removed from open questions.
- **`Phase.kind` typing:** **`Literal[...]`** (already in the dataclass definition above). The static-check value justifies the one extra import.

## Cross-references

- Originating issue: [#23](https://github.com/nordscope-fi/discord-stoat-ferry/issues/23) (DCE parser broken)
- Critique pass: `docs/superpowers/specs/2026-05-15-critique-pass.md` (this revision addresses the #23 section)
- Captured trace: `/tmp/dce-stdout-piped.log` (973 bytes, 13 lines, captured 2026-05-15 23:05; may need regeneration if `/tmp` is cleared)
- DCE source reference: `Tyrrrz/DiscordChatExporter@2.47.1` -- `ExportGuildCommand.cs`, `ExportCommandBase.cs`
- Related deferred issues: #34 (contract test), #35 (captured fixtures), #36 (isInline parser), #37 (ARM checksums), #38 (Windows compat), #39 (heartbeat)
