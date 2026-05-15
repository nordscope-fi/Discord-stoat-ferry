# Design: silence-breaker heartbeat during DCE export (issue #39)

**Date:** 2026-05-15 (revised 2026-05-16 after critique pass)
**Issue:** [#39](https://github.com/nordscope-fi/discord-stoat-ferry/issues/39)
**Ships as:** Single PR after #23 v2.2.0 lands
**Status:** Spec revised post-critique — ready for writing-plans

## Revisions from critique pass (2026-05-16)

The critique pass identified six critical findings in this spec. Resolutions:

| Critique finding | Resolution in this revision |
|------------------|---------------------------|
| Mock-clock plan is broken | **Redesigned: inject `_sleep` callable into `_heartbeat`** |
| Backoff prose ≠ code | **Prose updated to accurately describe code** |
| Race in `process.returncode is None` check | **Cancel order in `finally`: heartbeat_task before stderr_task** |
| v2.2.0 dependency not enforced | **"Blocked by #23" stated explicitly** |
| Status type collision | **Use `status="heartbeat"` as new MigrationEvent status** |
| Backoff cap (1200s) untested | **Reduced to 300s (5 min)** |
| 10s polling tick wastes wakeups | Adaptive sleep based on remaining silence |
| `mutable list-as-cell` idiom smell | Use `nonlocal` in inner closure |
| Negative test coverage missing | Added 3 new test cases |

## Problem

After #23's parser rewrite (v2.2.0), Ferry surfaces all DCE output to the GUI, including headline lines (`Fetching channels...`, `Fetched N channel(s).`, etc.). This breaks up most silence — but there can still be 5–15 minute silences between phase markers on large guilds (during channel enumeration via Discord REST pagination, where Spectre.Console's status ticker is suppressed in piped mode).

Add a periodic "still working" heartbeat emit while DCE is alive but no meaningful output has arrived for a threshold duration.

## Blocking dependency on #23 (v2.2.0)

This spec is **blocked by #23**. The implementation sketch references `parse_dce_line` and the `ParsedDceLine.kind` field, neither of which exist in `runner.py` today (current `runner.py` uses a single inline regex `_DCE_PROGRESS_RE` for per-channel progress only). If this PR were merged before #23, CI would fail at import time.

Sequencing protocol:
- #23 must merge first; the PR for this issue must rebase on top of #23.
- The PR description must explicitly state: "Blocked by #23 (v2.2.0). Do not merge before #23."
- CI on this PR must run against a branch rebased on top of #23, otherwise it will fail to import `parse_dce_line` from the new `dce_output` module.

## Architecture

A new asyncio task in `runner.py`, launched alongside `_read_stderr`, that monitors a shared "last activity" timestamp and emits heartbeat MigrationEvents when silence exceeds a threshold.

### Adaptive backoff schedule

Per brainstorming decision (and revised cap per critique), intervals double on each consecutive heartbeat (capped at 5 min):

```
First fire:  60s after last activity
Second fire: 120s after first heartbeat fired (with no real activity in between)
Third fire:  240s after second heartbeat
Fourth+ fire: 300s (capped at 5 min)

Any "real activity" RESETS the schedule back to the 60s initial state.
```

**Cap rationale (revised):** The original spec capped at 1200s (20 min). Critique noted that realistic user tolerance before they kill an apparently-stuck export is ~5 min. After 5 min of total silence with no output, the heartbeat itself becomes noise — the user already knows the export is suspect and either lets it run or cancels. Continuing to back off to 20 min provides no signal: a heartbeat at minute 20 is indistinguishable from a heartbeat at minute 5 from a user's perspective. Cap at 300s keeps the cadence informative for the entire window the user is plausibly still watching.

### What counts as "real activity"

For v2.2.0+ (when #23's parser routes all kinds to GUI):

| Parsed kind | Resets heartbeat? | Why |
|-------------|-------------------|-----|
| `per_channel` | YES | Clear progress signal |
| `phase` | YES | Phase transition; user expects pause until next |
| `success` | YES (and ends heartbeat — process about to exit) | Completion |
| `raw` | YES | Unrecognized output is still output |
| `banner` | NO | Initial chrome; not progress |
| `status_dot` | NO | Spectre's silence placeholder |

### New MigrationEvent status: `"heartbeat"`

Heartbeat events use a dedicated `status="heartbeat"` value (not `"progress"`). This avoids collision with real progress events in the GUI log panel and lets `on_export_event` render heartbeats distinctly.

Touchpoints:
- `src/discord_ferry/core/events.py` — update the `MigrationEvent.status` field's docstring comment to add `"heartbeat"` to the enumerated set:
  ```python
  status: str  # "started", "progress", "completed", "error", "warning", "skipped", "confirm", "heartbeat"
  ```
- `src/discord_ferry/gui.py:600-615` (`on_export_event`) — add a branch for `event.status == "heartbeat"` that pushes the line with a visually quieter style (e.g., italic via `[heartbeat]` prefix preserved, or — preferred — push with a faded color class). The progress bar and channel label MUST NOT be touched on heartbeat events; only the log line is appended. The current `log_display.push(f"[{event.status}] {event.message}")` already covers the prefix automatically since `event.status == "heartbeat"`.

### Implementation sketch

In `src/discord_ferry/exporter/runner.py` (post-#23):

```python
import time
import asyncio
import contextlib
from collections.abc import Awaitable, Callable

# Type alias for the injected sleep callable (kept module-private; tests pass a fake).
_SleepFn = Callable[[float], Awaitable[None]]

async def _heartbeat(
    process: asyncio.subprocess.Process,
    on_event: EventCallback,
    process_start: float,
    get_last_activity: Callable[[], float],
    *,
    sleep: _SleepFn = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    initial_interval: float = 60.0,
    max_interval: float = 300.0,
) -> None:
    """Emit "Still working" heartbeats during prolonged DCE silence.

    The `sleep` and `monotonic` callables are injectable so tests can drive a
    fake clock without monkey-patching the global `asyncio.sleep` (which would
    break the stdout/stderr `async for` loops, `process.wait()`, and aiohttp).
    """
    interval = initial_interval
    last_fire_at = process_start
    try:
        while process.returncode is None:
            now = monotonic()
            silence = now - get_last_activity()
            # Activity since the last heartbeat fire => baseline backoff.
            if get_last_activity() > last_fire_at:
                interval = initial_interval
            if silence >= interval:
                elapsed_min = int((now - process_start) / 60)
                silence_sec = int(silence)
                on_event(MigrationEvent(
                    phase="export",
                    status="heartbeat",
                    message=(
                        f"Still working - DCE has been running for {elapsed_min} min, "
                        f"no new output for {silence_sec}s..."
                    ),
                ))
                last_fire_at = now
                interval = min(interval * 2, max_interval)
                # Sleep at least a short tick before the next check after firing.
                await sleep(min(10.0, interval))
                continue
            # Adaptive: wake up at most when the next fire could occur, but no
            # less than 1s and no more than 10s, so cancellation stays snappy.
            remaining = max(1.0, min(10.0, interval - silence))
            await sleep(remaining)
    except asyncio.CancelledError:
        return


async def run_dce_export(config, dce_path, on_event):
    # ... existing setup, _check_disk_space, banner emit, process spawn ...

    process = await asyncio.create_subprocess_exec(...)
    process_start = time.monotonic()
    last_activity = time.monotonic()

    def _record_activity() -> None:
        nonlocal last_activity
        last_activity = time.monotonic()

    def _get_last_activity() -> float:
        return last_activity

    stderr_lines: list[str] = []
    stderr_task: asyncio.Task | None = None
    heartbeat_task: asyncio.Task | None = None

    try:
        async def _read_stderr() -> None:
            assert process.stderr is not None
            async for raw_line in process.stderr:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if line:
                    stderr_lines.append(line)

        stderr_task = asyncio.create_task(_read_stderr())
        heartbeat_task = asyncio.create_task(
            _heartbeat(process, on_event, process_start, _get_last_activity)
        )

        async for raw_line in process.stdout:
            if config.cancel_event and config.cancel_event.is_set():
                process.terminate()
                await process.wait()
                raise asyncio.CancelledError("Export cancelled by user")

            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            parsed = parse_dce_line(line)  # provided by #23
            if parsed.kind in {"per_channel", "phase", "success", "raw"}:
                _record_activity()
            _emit_for_parsed(parsed, on_event)  # provided by #23

        await stderr_task
        await process.wait()

    except asyncio.CancelledError:
        process.terminate()
        await process.wait()
        raise

    finally:
        # Order matters: cancel heartbeat FIRST so it cannot fire a false
        # "Still working" event after stdout drains but before process.wait()
        # records returncode. Then drain stderr.
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task
        if stderr_task is not None and not stderr_task.done():
            stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stderr_task

    if process.returncode != 0:
        last_err = stderr_lines[-1] if stderr_lines else "Unknown error"
        raise ExportError(f"DCE export failed (exit code {process.returncode}): {last_err}")

    return config.export_dir
```

### Backoff semantics (prose matches code)

The heartbeat task wakes adaptively (sleeping `max(1, min(10, interval - silence))` seconds between checks). On each wake:

1. Compute `silence = now - last_activity`.
2. If `last_activity > last_fire_at` — i.e. real activity arrived since the last heartbeat we fired — reset `interval` to 60s. This is the unconditional reset; it happens on every tick where the condition holds, not just the first tick after activity. Functionally that's fine: once `interval` has been reset to 60s on tick N, on tick N+1 the same condition is still true (no heartbeat fired in between), and it resets to 60s again, which is a no-op.
3. If `silence >= interval`, fire the heartbeat, set `last_fire_at = now`, double `interval` (cap 300s).
4. Otherwise, sleep until the silence window could plausibly cross `interval`.

The earlier prose claimed "interval doesn't reset just because last_activity advances." That was inaccurate — the code does reset on every tick where activity is fresher than the last fire. The behavior is correct (silence backs off, activity returns to baseline), but the prose now matches the code.

### Concurrency / race avoidance

The cleanup order in `finally` matters. Without it, this race exists:

1. DCE closes stdout (`async for raw_line in process.stdout` exits).
2. `await stderr_task` runs.
3. While we're awaiting stderr, the heartbeat task wakes, observes `process.returncode is None` (because we haven't called `process.wait()` yet), and emits a false "Still working" event after the export has actually finished.

Mitigation: in the `finally` block, cancel `heartbeat_task` BEFORE awaiting `stderr_task`. The heartbeat task is cancelled and awaited for its `CancelledError` before any further stdout/stderr drain happens, so there is no window in which it can fire post-completion.

## Components

| Component | Responsibility |
|-----------|----------------|
| `runner._heartbeat` (NEW, async function) | Adaptive-backoff heartbeat task; takes injected `sleep` and `monotonic` callables for testability |
| `runner.run_dce_export` (UPDATED) | Tracks `last_activity` via `nonlocal`; spawns heartbeat task; calls `_record_activity` on relevant parses; cancels heartbeat task BEFORE stderr task in `finally` |
| `core/events.py` (UPDATED) | Add `"heartbeat"` to the `MigrationEvent.status` docstring enum |
| `gui.py:on_export_event` (UPDATED) | Handle `status == "heartbeat"` (log only; do not touch progress bar or channel label) |
| `tests/test_exporter_runner.py` (UPDATED) | New tests using injected fake sleep + monotonic; covers fire, no-fire, backoff, reset, cleanup, plus 3 new negative cases |

## Data flow

```
DCE stdout line arrives
    |
parse_dce_line(line) -> ParsedDceLine
    |
if kind in {per_channel, phase, success, raw}:
    last_activity = now()  <- updates closure variable via nonlocal
    |
_emit_for_parsed(parsed, on_event)  <- still emits to GUI

Concurrently (heartbeat task):
    sleep(adaptive remaining, [1..10s])
    if last_activity > last_fire_at: interval = 60s
    if (now - last_activity) >= interval:
        emit MigrationEvent(status="heartbeat")
        last_fire_at = now
        interval = min(interval * 2, 300s)
```

## Error handling

- Heartbeat task wraps its main loop in `try/except asyncio.CancelledError: return` to handle cancellation cleanly.
- Heartbeat task does NOT raise on `on_event` failure — emits are best-effort.
- On cancel via `cancel_event` OR on the normal exit path, `finally` cancels `heartbeat_task` first, then `stderr_task`, then awaits both.

## Testing

Unit tests in `tests/test_exporter_runner.py`. The test strategy uses **dependency injection of `sleep` and `monotonic`**, NOT global monkey-patching of `asyncio.sleep` — global patching breaks `_read_stderr`'s `async for`, the stdout `async for`, `process.wait()`, and `aiohttp.ClientSession`.

### Fake clock helper

```python
class FakeClock:
    """Deterministic clock for heartbeat tests.

    `sleep(d)` records the requested delay, advances `now` by `d`, and yields
    control once via `asyncio.sleep(0)` so other tasks (e.g. an event-driver
    coroutine that pushes mock activity) can run.
    """
    def __init__(self, start: float = 0.0) -> None:
        self._now = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self._now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self._now += delay
        await asyncio.sleep(0)  # let the event loop schedule other tasks

    def advance(self, delta: float) -> None:
        self._now += delta
```

The heartbeat task is then constructed directly (not via `run_dce_export`) for unit tests, with a fake `process` (a `SimpleNamespace` or `Mock` with a mutable `returncode`) and a controllable `last_activity` getter. This isolates `_heartbeat` from subprocess/IO entirely.

### Test cases

Positive:

- `test_heartbeat_fires_after_60s_of_silence`: `last_activity = 0`; advance fake clock by 70s; assert at least one `status="heartbeat"` event emitted.
- `test_heartbeat_does_not_fire_when_activity_present`: bump `last_activity` every 10s for 90s; assert zero heartbeat events.
- `test_heartbeat_backoff_doubles_on_consecutive_fires`: pure silence for 600s; assert heartbeats fire at approximately 60s, 180s, 420s, 720s, 1020s elapsed (60+120, 60+120+240, then capped at 300s onward).
- `test_heartbeat_resets_to_60s_on_real_activity`: silence 90s (heartbeat fires at 60s, interval bumps to 120s); bump activity at 90s; silence again. Next heartbeat should fire ~60s after the activity, not 120s.
- `test_heartbeat_status_is_heartbeat_not_progress`: assert emitted event has `status == "heartbeat"` (so GUI can render distinctly).
- `test_heartbeat_task_cancelled_on_cancel_event`: integration test with a fake subprocess; trigger `cancel_event`; assert `heartbeat_task.done()` after `run_dce_export` returns and that no heartbeat fired between `cancel_event.set()` and process exit.
- `test_heartbeat_cancelled_before_stderr_in_finally`: integration test asserting cleanup order — instrument both tasks and verify heartbeat is cancelled first (e.g., via a sentinel order list).
- `test_heartbeat_cap_at_300s`: silence for 30 minutes; assert no `interval` ever exceeds 300s (heartbeats keep firing every ~5 min indefinitely until process exits).

Negative (NEW per critique):

- `test_no_fire_at_59s_silence`: advance to 59s of silence; assert zero heartbeats fired (off-by-one guard at the lower bound).
- `test_fire_at_exact_tick_boundary`: advance to exactly 60.0s of silence; assert exactly one heartbeat fires (boundary equality of `silence >= interval`).
- `test_back_to_back_activity_bursts_keep_baseline`: bump activity at t=10, t=20, t=30, ..., t=290; then silence. First heartbeat after the burst should fire 60s after the last activity (at t=350), not earlier. Confirms that frequent activity holds the interval at baseline indefinitely without accumulating drift.

Manual smoke test:

- Run real export against a large server (or simulate via slow DCE mock).
- Confirm heartbeat appears at expected intervals in GUI log panel and is visually distinguishable from per-channel progress.

## Phasing

Single PR. Lands AFTER #23 v2.2.0 (which provides the new `dce_output` module structure including `parse_dce_line` and `ParsedDceLine`).

## Decision Accountability gate

| Claim | Verdict | Evidence |
|-------|---------|----------|
| "Parser fix makes heartbeat unnecessary" | PARTIALLY FALSIFIED | Phase markers still leave 5–15 min gaps on large guilds during enumeration |
| "Adaptive backoff is right" | VERIFIED via brainstorming | Avoids log spam on long-running exports |
| "60s initial is right starting point" | VERIFIED via brainstorming | Acknowledges user without being noisy |
| "Heartbeat could be implemented at engine layer" | FALSIFIED | Engine is too far up; runner has direct subprocess context |
| "Mock-clock testing is the right approach" | REVISED PER CRITIQUE | Global mock of `asyncio.sleep` is broken (would break `_read_stderr`/aiohttp). Replaced with **dependency injection** of `sleep` and `monotonic` into `_heartbeat` |
| "20-min backoff cap is right" | FALSIFIED PER CRITIQUE | Realistic user tolerance is ~5 min before kill; backoff to 20 min provides no informative signal. Reduced to 300s |
| "Heartbeat status type should be distinct" | PROMOTED FROM OPEN-Q TO DECIDED | Use `status="heartbeat"` (avoids collision with `"progress"`); GUI handles in `on_export_event` |
| "`process.returncode is None` check is sufficient to avoid false post-exit fires" | FALSIFIED PER CRITIQUE | Race between stdout EOF and `process.wait()`. Mitigated by cancelling heartbeat BEFORE stderr in `finally` |
| "Mutable list-as-cell for `last_activity` is fine" | REVISED PER CRITIQUE | Replaced with `nonlocal` + closure (idiomatic Python; one less layer of indirection) |
| "10s polling tick is fine" | REVISED PER CRITIQUE | Replaced with adaptive `max(1, min(10, interval - silence))` to avoid wasteful wakeups while keeping cancellation snappy |

**Foundational?** No — UX enhancement. But once it ships it becomes part of the user-facing UX contract.

## Risks

| Risk | Mitigation |
|------|------------|
| Heartbeat fires during legitimate brief silence (network blip), confuses user | 60s initial is long enough that brief blips don't trigger; adaptive backoff prevents repeated false alarms |
| Injecting `sleep`/`monotonic` adds API surface | Both are keyword-only with sensible defaults (`asyncio.sleep`, `time.monotonic`); production callers never pass them |
| Heartbeat task leaks on exception | `try/finally` cancels heartbeat task first, then stderr task, then awaits both with `CancelledError` suppressed |
| Heartbeat fires after process completed but before stdout fully drained | Cancel `heartbeat_task` BEFORE `await stderr_task` in `finally` (eliminates race) |
| 300s cap may be too short if DCE legitimately needs minutes between phases | At 300s the heartbeat keeps firing every 5 min indefinitely — user still gets liveness signal; cap is on the *interval growth*, not on emission |
| Adaptive sleep could starve the heartbeat after a fire (e.g., `sleep(min(10, interval))` → 10s) | After firing, we always sleep at least a short tick (max 10s) before the next check, preventing tight-loop waste while keeping cancellation responsive |
| New `status="heartbeat"` value could break consumers expecting only the existing set | Only consumer is the GUI (single touchpoint at `gui.py:600`); engine doesn't switch on `status` for heartbeat semantics |

## Deferrals

| Item | Why not now | Tracking | Trigger |
|------|-------------|----------|---------|
| Tunable interval via Ferry config | Premature; ship default first | — | If users complain about cadence |
| Show "current Discord rate-limit window" in heartbeat message | Requires Discord API integration in runner; out of scope | — | Never likely |
| Diagnose button in GUI to dump current DCE state | Different UX paradigm; might supplement heartbeat | (will file as enhancement) | If users still report "stuck" perception with heartbeat enabled |
| Heartbeat for the upload phase (after export) | Different code path; scope creep | (will file as enhancement) | After this lands and proves valuable |
| Phase-aware heartbeat message ("Still fetching channels...") | Requires tracking current phase from headline parses; can be added in a follow-up | — | After v2.2.0 routes phase events through |

## Open questions (for implementation, not blocking spec)

- Heartbeat message phrasing: my draft says "Still working - DCE has been running for 5 min, no new output for 65s..." — alternative: "DCE is taking a while: 5 min elapsed, last output 65s ago. Large guilds can take 30+ minutes." Decide during writing-plans based on tone.
- GUI rendering of `status="heartbeat"`: italic vs faded color vs unchanged-but-prefixed. Defer to writing-plans; a single-line diff in `on_export_event` can change it cheaply.

## Cross-references

- Issue #23 v2.2.0 — provides the parser + routing infrastructure this depends on (`parse_dce_line`, `ParsedDceLine`, `_emit_for_parsed`). **Hard blocker.**
- Issue #34 — independent
- Issue #35 — captured fixtures could include "long silence" scenario for replay testing of heartbeat
- `src/discord_ferry/exporter/runner.py` — current state (pre-#23) uses inline `_DCE_PROGRESS_RE`; do NOT attempt to integrate this PR against current `runner.py`.
- `src/discord_ferry/core/events.py` — `MigrationEvent.status` docstring requires update.
- `src/discord_ferry/gui.py:600-615` — `on_export_event` requires a new branch for `status == "heartbeat"`.
